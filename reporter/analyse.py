"""Analyses SpendLogs pour tester l'hypothèse « les très gros prompts ralentissent les autres ».

Toutes les fonctions prennent le DataFrame SpendLogs construit par report.spendlogs_section :
colonnes start, end_, first_tok (datetimes), ttft (s, NaN si non streamé), prompt_tokens,
api_base, key_id (hash de la clé, jamais affiché), user_, key_alias (déjà anonymisés si demandé).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# Noms de colonnes réutilisés dans plusieurs tableaux (évite les littéraux dupliqués).
REQUETES = "Requêtes"
TTFT_P50 = "TTFT p50 (s)"
TTFT_P95 = "TTFT p95 (s)"
DUREE_P95 = "Durée p95 (s)"
PIC_SIMULTANE = "Pic simultané"


def bucket_labels(small: int, large: int) -> list[str]:
    k = lambda n: f"{n // 1000}k"  # noqa: E731
    return [f"< {k(small)}", f"{k(small)} – 32k", f"32k – {k(large)}", f"≥ {k(large)}"]


def prompt_bucket(pt: pd.Series, small: int, large: int) -> pd.Series:
    edges = [-1, small - 1, max(32_000, small) - 1, max(large, 32_001) - 1, float("inf")]
    return pd.cut(pt.fillna(0), bins=edges, labels=bucket_labels(small, large))


def ttft_by_bucket(df: pd.DataFrame, small: int, large: int) -> pd.DataFrame:
    """TTFT par taille de prompt : sépare la lenteur « normale » des gros prompts de celle subie par les petits."""
    d = df.assign(tranche=prompt_bucket(df["prompt_tokens"], small, large))
    g = d.groupby("tranche", observed=False)
    tot = d["prompt_tokens"].sum()
    return pd.DataFrame({
        REQUETES: g.size(),
        "Utilisateurs": d[d["user_"] != "(aucun)"].groupby("tranche", observed=False)["user_"].nunique(),
        "Part tokens in": g["prompt_tokens"].sum() / tot if tot else 0,
        TTFT_P50: g["ttft"].median(),
        TTFT_P95: g["ttft"].quantile(0.95),
        DUREE_P95: g["dur"].quantile(0.95),
    }).reset_index().rename(columns={"tranche": "Taille du prompt"})


# Lecture réellement calculée : un gros prompt servi par le cache de préfixe donne son premier token
# en quelques secondes et ne gêne personne. Même seuil que l'analyse d'écriture (decode.HEAVY_PREFILL_S).
HEAVY_PREFILL_S = 20
MIN_SAMPLES = 30
N_BOOT = 400


def heavy_in_flight(df: pd.DataFrame, small: int, large: int) -> pd.DataFrame:
    """Petites requêtes streamées, marquées si un gros prompt d'une AUTRE clé était en lecture
    (entre son début et son premier token) sur la même stack pendant qu'elles étaient en cours.
    Un gros prompt dont le premier token arrive en moins de HEAVY_PREFILL_S (cache) n'est pas une lecture."""
    small_rq = df[(df["prompt_tokens"] < small) & df["ttft"].notna()].copy()
    heavy = df[df["prompt_tokens"] >= large]
    heavy = heavy[~(heavy["ttft"].notna() & (heavy["ttft"] < HEAVY_PREFILL_S))]
    flag = pd.Series(False, index=small_rq.index)
    for h in heavy.itertuples():
        prefill_end = h.first_tok if pd.notna(h.first_tok) and h.first_tok > h.start else h.end_
        flag |= ((small_rq["api_base"] == h.api_base) & (small_rq["key_id"] != h.key_id)
                 & (small_rq["start"] < prefill_end) & (small_rq["end_"] > h.start))
    small_rq["gros_en_cours"] = flag
    return small_rq


def stratified_effect(s: pd.DataFrame, n_boot: int = N_BOOT, seed: int = 0) -> dict:
    """Effet des gros prompts à affluence comparable.

    Les gros prompts arrivent surtout aux heures chargées, où tout est plus lent pour d'autres raisons :
    comparer « avec » et « sans » sur tout le mois attribue aux gros prompts la lenteur due à l'affluence.
    On compare donc à l'intérieur d'une même strate (même stack, même heure du même jour) : chaque TTFT est
    divisé par le TTFT médian des petites requêtes SANS gros prompt de sa strate, puis on agrège.
    Intervalle de confiance à 90 % par bootstrap sur les strates."""
    s = s.assign(strate=s["api_base"].astype(str) + "|" + s["start"].dt.floor("h").astype(str))
    flag = s["gros_en_cours"]
    ref = s[~flag].groupby("strate")["ttft"].median()
    both = set(ref.index) & set(s.loc[flag, "strate"])
    m = s[s["strate"].isin(both)].copy()
    m["rel"] = m["ttft"] / m["strate"].map(ref).clip(lower=1e-3)
    w, wo = m.loc[m["gros_en_cours"], "rel"], m.loc[~m["gros_en_cours"], "rel"]
    out = {"n_with": len(w), "n_without": len(wo), "strata": len(both),
           "exposure": float(flag.mean()) if len(s) else 0.0,
           "r50": None, "r95": None, "ci": (None, None)}
    if len(w) < MIN_SAMPLES or len(wo) < MIN_SAMPLES:
        return out

    def ratios(a, b):
        return (np.median(a) / max(np.median(b), 1e-6), np.quantile(a, 0.95) / max(np.quantile(b, 0.95), 1e-6))

    out["r50"], out["r95"] = ratios(w.to_numpy(), wo.to_numpy())
    groups = [(g.loc[g["gros_en_cours"], "rel"].to_numpy(), g.loc[~g["gros_en_cours"], "rel"].to_numpy())
              for _, g in m.groupby("strate")]
    rng = np.random.default_rng(seed)
    boots = []
    for _ in range(n_boot):
        pick = rng.integers(len(groups), size=len(groups))
        a = np.concatenate([groups[i][0] for i in pick])
        b = np.concatenate([groups[i][1] for i in pick])
        boots.append(np.median(a) / max(np.median(b), 1e-6))
    out["ci"] = (float(np.quantile(boots, 0.05)), float(np.quantile(boots, 0.95)))
    return out


def contention(df: pd.DataFrame, small: int, large: int) -> tuple[pd.DataFrame, dict]:
    """Compare le TTFT des petites requêtes avec / sans gros prompt en lecture. Renvoie (table, verdict).
    La table donne les valeurs brutes ; le verdict repose sur la comparaison à affluence comparable."""
    s = heavy_in_flight(df, small, large)
    rows = []
    for flag, label in ((False, "Sans gros prompt en lecture"), (True, "Avec gros prompt en lecture")):
        sub = s[s["gros_en_cours"] == flag]
        x, d = sub["ttft"], sub["dur"]
        rows.append({"Petites requêtes": label, REQUETES: len(x),
                     TTFT_P50: x.median() if len(x) else None,
                     TTFT_P95: x.quantile(0.95) if len(x) else None,
                     # la gêne passe aussi par l'écriture : premier token rapide, puis réponse qui rampe
                     "Durée p50 (s)": d.median() if len(d) else None,
                     DUREE_P95: d.quantile(0.95) if len(d) else None})
    table = pd.DataFrame(rows)
    return table, verdict(table, stratified_effect(s))


def _raw_ratio(table: pd.DataFrame) -> float | None:
    without, with_ = table.iloc[0], table.iloc[1]
    if pd.isna(with_[TTFT_P50]) or pd.isna(without[TTFT_P50]):
        return None
    return with_[TTFT_P50] / max(without[TTFT_P50], 1e-3)


def verdict(table: pd.DataFrame, st: dict) -> dict:
    """Lecture prudente : assez d'échantillons comparables, intervalle de confiance, fréquence de la gêne."""
    n_with_all = int(table.iloc[1][REQUETES])
    base = {"ratio_p50": st["r50"], "ratio_p95": st["r95"], "ci": st["ci"], "exposure": st["exposure"],
            "raw_ratio_p50": _raw_ratio(table)}
    if st["r50"] is None:
        return {**base, "lvl": "info", "txt":
                f"Pas encore assez d'échantillons comparables pour conclure : {st['n_with']} petites requêtes avec gros "
                f"prompt en lecture et {st['n_without']} sans, à la même heure sur la même stack "
                f"({n_with_all} avec gros prompt au total) ; il en faut au moins {MIN_SAMPLES} de chaque côté."}
    r50, r95, (lo, hi), exp = st["r50"], st["r95"], st["ci"], st["exposure"]
    expo = f"{exp:.1%}".replace(".", ",")
    ci = f"intervalle de confiance à 90 % sur la médiane : ×{lo:.1f} – ×{hi:.1f}"
    raw = base["raw_ratio_p50"]
    biais = (f" Sans cette correction, la comparaison brute aurait donné ×{raw:.1f} : l'écart vient de l'affluence, "
             "pas des gros prompts." if raw and raw >= 1.3 and raw >= 1.5 * r50 else "")
    if (r50 >= 2 or r95 >= 3) and lo > 1:
        return {**base, "lvl": "warn", "txt":
                f"Hypothèse CONFIRMÉE : à affluence comparable (même stack, même heure), quand un gros prompt d'un autre "
                f"utilisateur est en lecture, le TTFT des petites requêtes est multiplié par {r50:.1f} (médiane) et "
                f"{r95:.1f} (p95) ; {ci}. Cela touche {expo} des petites requêtes."}
    if r50 <= 1.3 and r95 <= 1.5:
        return {**base, "lvl": "ok", "txt":
                f"Hypothèse NON CONFIRMÉE : à affluence comparable, le TTFT des petites requêtes ne change pas "
                f"significativement quand un gros prompt est en lecture (×{r50:.1f} médiane, ×{r95:.1f} p95 ; {ci})."
                f"{biais} Chercher la cause ailleurs (concurrence globale, routage, décodage)."}
    return {**base, "lvl": "info", "txt":
            f"Effet modéré ou incertain : à affluence comparable, TTFT des petites requêtes ×{r50:.1f} (médiane) et "
            f"×{r95:.1f} (p95) quand un gros prompt est en lecture ; {ci}. Touche {expo} des petites requêtes."
            f"{biais} À recouper avec la sonde témoin et la concurrence par clé."}


def per_minute_level(ev: pd.DataFrame) -> pd.Series:
    """Niveau de concurrence max par minute à partir d'événements (t, c cumulé).
    Une minute sans début ni fin de requête garde le niveau de la précédente."""
    lvl = ev.set_index("t")["c"]
    peak = lvl.resample("1min").max()
    carried = lvl.resample("1min").last().ffill().shift(1).fillna(0)
    return pd.concat([peak, carried], axis=1).max(axis=1)


def key_concurrency(df: pd.DataFrame, limits: tuple[int, ...] = (2, 3, 4)) -> pd.DataFrame:
    """Requêtes simultanées par clé, pour dimensionner max_parallel_requests dans LiteLLM.
    « Minutes > N » = minutes d'activité où la clé aurait été bridée par une limite à N."""
    rows = []
    for (key_id, user, alias), g in df.groupby(["key_id", "user_", "key_alias"], dropna=False):
        ev = pd.concat([pd.DataFrame({"t": g["start"], "d": 1}), pd.DataFrame({"t": g["end_"], "d": -1})])
        ev = ev.sort_values(["t", "d"]).reset_index(drop=True)
        ev["c"] = ev["d"].cumsum()
        per_min = per_minute_level(ev)
        per_min = per_min[per_min > 0]
        row = {"Utilisateur": user, "Clé": alias, REQUETES: len(g),
               PIC_SIMULTANE: int(ev["c"].max()),
               "Simultané p95 (minutes actives)": float(per_min.quantile(0.95)) if len(per_min) else 0.0,
               "Minutes actives": len(per_min)}
        for n in limits:
            row[f"Minutes > {n}"] = int((per_min > n).sum())
        rows.append(row)
    cols = ["Utilisateur", "Clé", REQUETES, PIC_SIMULTANE, "Simultané p95 (minutes actives)", "Minutes actives"] + \
        [f"Minutes > {n}" for n in limits]
    return pd.DataFrame(rows, columns=cols).sort_values(PIC_SIMULTANE, ascending=False)


def top_prompts(df: pd.DataFrame, n: int = 20) -> pd.DataFrame:
    """Les plus gros prompts, horodatés : de quoi relier un ralentissement signalé à un événement précis."""
    d = df.nlargest(n, "prompt_tokens")
    return pd.DataFrame({
        "Début": d["start"].dt.strftime("%d/%m %H:%M:%S"),
        "Utilisateur": d["user_"], "Clé": d["key_alias"], "Stack": d["api_base"],
        "Tokens in": d["prompt_tokens"], "TTFT (s)": d["ttft"], "Durée (s)": d["dur"],
    })
