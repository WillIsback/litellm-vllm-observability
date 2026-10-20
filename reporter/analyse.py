"""Analyses SpendLogs pour tester l'hypothèse « les très gros prompts ralentissent les autres ».

Toutes les fonctions prennent le DataFrame SpendLogs construit par report.spendlogs_section :
colonnes start, end_, first_tok (datetimes), ttft (s, NaN si non streamé), prompt_tokens,
api_base, key_id (hash de la clé, jamais affiché), user_, key_alias (déjà anonymisés si demandé).
"""
from __future__ import annotations

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
        "Utilisateurs": g["user_"].nunique(),
        "Part tokens in": g["prompt_tokens"].sum() / tot if tot else 0,
        TTFT_P50: g["ttft"].median(),
        TTFT_P95: g["ttft"].quantile(0.95),
        DUREE_P95: g["dur"].quantile(0.95),
    }).reset_index().rename(columns={"tranche": "Taille du prompt"})


def heavy_in_flight(df: pd.DataFrame, small: int, large: int) -> pd.DataFrame:
    """Petites requêtes streamées, marquées si un gros prompt d'une AUTRE clé était en lecture
    (entre son début et son premier token) sur la même stack pendant qu'elles étaient en cours."""
    small_rq = df[(df["prompt_tokens"] < small) & df["ttft"].notna()].copy()
    heavy = df[df["prompt_tokens"] >= large]
    flag = pd.Series(False, index=small_rq.index)
    for h in heavy.itertuples():
        prefill_end = h.first_tok if pd.notna(h.first_tok) and h.first_tok > h.start else h.end_
        flag |= ((small_rq["api_base"] == h.api_base) & (small_rq["key_id"] != h.key_id)
                 & (small_rq["start"] < prefill_end) & (small_rq["end_"] > h.start))
    small_rq["gros_en_cours"] = flag
    return small_rq


def contention(df: pd.DataFrame, small: int, large: int) -> tuple[pd.DataFrame, dict]:
    """Compare le TTFT des petites requêtes avec / sans gros prompt en lecture. Renvoie (table, verdict)."""
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
    return table, verdict(table)


MIN_SAMPLES = 30


def verdict(table: pd.DataFrame) -> dict:
    """Lecture prudente : il faut assez d'échantillons des deux côtés avant de conclure."""
    without, with_ = table.iloc[0], table.iloc[1]
    n0, n1 = int(without[REQUETES]), int(with_[REQUETES])
    if n0 < MIN_SAMPLES or n1 < MIN_SAMPLES:
        return {"lvl": "info", "ratio_p50": None, "ratio_p95": None, "txt":
                f"Pas encore assez d'échantillons pour conclure ({n0} petites requêtes sans gros prompt en lecture, "
                f"{n1} avec ; il en faut au moins {MIN_SAMPLES} de chaque côté)."}
    r50 = with_[TTFT_P50] / max(without[TTFT_P50], 1e-3)
    r95 = with_[TTFT_P95] / max(without[TTFT_P95], 1e-3)
    rd = with_[DUREE_P95] / max(without[DUREE_P95], 1e-3)
    if r50 >= 2 or r95 >= 3:
        return {"lvl": "warn", "ratio_p50": r50, "ratio_p95": r95, "txt":
                f"Hypothèse CONFIRMÉE : quand un gros prompt d'un autre utilisateur est en lecture sur la même stack, "
                f"le TTFT des petites requêtes est multiplié par {r50:.1f} (médiane) et {r95:.1f} (p95) "
                f"(durée totale p95 ×{rd:.1f}) — {n1} requêtes concernées sur {n0 + n1}."}
    if r50 <= 1.3 and r95 <= 1.5:
        return {"lvl": "ok", "ratio_p50": r50, "ratio_p95": r95, "txt":
                f"Hypothèse NON CONFIRMÉE : le TTFT des petites requêtes ne change pas significativement quand un gros "
                f"prompt est en lecture (×{r50:.1f} médiane, ×{r95:.1f} p95). Chercher la cause ailleurs "
                "(concurrence globale, routage, décodage)."}
    return {"lvl": "info", "ratio_p50": r50, "ratio_p95": r95, "txt":
            f"Effet modéré : TTFT des petites requêtes ×{r50:.1f} (médiane) et ×{r95:.1f} (p95) quand un gros prompt "
            "est en lecture. À recouper avec la sonde témoin et la concurrence par clé."}


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
