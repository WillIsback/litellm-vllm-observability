"""Écriture (décodage) : bande passante, interférence de la lecture, décodage spéculatif.

Sur GB10, chaque pas de décodage relit les poids actifs du modèle et le cache KV de chaque
conversation en cours : le débit d'écriture est borné par la bande passante mémoire (LPDDR5x).
Trois causes font baisser le débit ressenti par une conversation :

  1. bande passante partagée : plus de conversations écrivent en même temps, moins chacune avance ;
  2. lecture (prefill) concurrente : chaque pas porte aussi un morceau de lecture, qui l'allonge ;
  3. rendement du décodage spéculatif : moins de tokens acceptés par pas, moins de débit.

L'analyse travaille sur des minutes (une ligne = une stack × une minute) construites à partir
des compteurs Prometheus de vLLM, puis compare les minutes entre elles en isolant un facteur à la fois.
"""
from __future__ import annotations

import pandas as pd

# Lecture réellement calculée (hors cache), en tokens/s moyennés sur la minute
PREFILL_LIGHT = 100     # en dessous : pas de lecture significative
PREFILL_HEAVY = 1000    # au-dessus : une longue lecture occupe la stack (≈ 1,5–2,5 k tok/s à froid sur GB10)
MIN_MINUTES = 30        # minutes minimum par case pour conclure

LVL_NONE, LVL_LIGHT, LVL_HEAVY = "Aucune lecture", "Lecture légère", "Lecture lourde"
CONC_LABELS = ["1", "2", "3", "4", "5 – 6", "7 et plus"]

COL_MIN = "Minutes"
COL_AGG = "Débit cumulé médian (tok/s)"
COL_STREAM = "Débit par conversation médian (tok/s)"
COL_SPEC = "Tokens par pas (médiane)"
COL_LOSS = "% perte en lecture lourde"


def prepare(m: pd.DataFrame) -> pd.DataFrame:
    """m : colonnes provider, t, running, gen, iter_tok, drafts, accepted (taux par seconde, moyenne sur 1 min).

    Garde les minutes où la stack écrit réellement, et ajoute :
      per_stream : débit d'écriture par conversation en cours (gen / running) ;
      prefill    : tokens de lecture calculés par seconde (pas total − tokens générés) ;
      spec_yield : tokens produits par pas de décodage spéculatif (1 + acceptés / brouillons) ;
      conc, prefill_lvl : classes pour les tableaux.
    """
    d = m.copy()
    d = d[(d["gen"] > 0) & (d["running"] >= 0.5)]
    d["per_stream"] = d["gen"] / d["running"]
    d["prefill"] = (d["iter_tok"] - d["gen"]).clip(lower=0)
    if "drafts" in d and "accepted" in d:
        d["spec_yield"] = (1 + d["accepted"] / d["drafts"]).where(d["drafts"] > 0)
    else:
        d["spec_yield"] = float("nan")
    d["conc"] = pd.cut(d["running"].round(), [0, 1, 2, 3, 4, 6, float("inf")], labels=CONC_LABELS)
    d["prefill_lvl"] = pd.cut(d["prefill"], [-1, PREFILL_LIGHT, PREFILL_HEAVY, float("inf")],
                              labels=[LVL_NONE, LVL_LIGHT, LVL_HEAVY])
    return d


def saturation(d: pd.DataFrame) -> pd.DataFrame:
    """Courbe de bande passante : minutes SANS lecture, selon le nombre de conversations en cours.

    Si le débit cumulé cesse de monter quand la concurrence augmente, la bande passante est saturée :
    chaque conversation de plus ne fait que ralentir les autres."""
    q = d[d["prefill_lvl"] == LVL_NONE]
    g = q.groupby(["provider", "conc"], observed=True)
    t = pd.DataFrame({
        COL_MIN: g.size(),
        COL_AGG: g["gen"].median(),
        COL_STREAM: g["per_stream"].median(),
        COL_SPEC: g["spec_yield"].median(),
    }).reset_index().rename(columns={"provider": "Stack", "conc": "Conversations en cours"})
    return t


def interference(d: pd.DataFrame) -> pd.DataFrame:
    """Débit par conversation selon la lecture en cours, à concurrence égale (une ligne par stack × concurrence)."""
    piv = d.pivot_table(index=["provider", "conc"], columns="prefill_lvl", values="per_stream",
                        aggfunc="median", observed=True)
    cnt = d.pivot_table(index=["provider", "conc"], columns="prefill_lvl", values="per_stream",
                        aggfunc="size", observed=True)
    out = piv.copy()
    for lvl in [LVL_NONE, LVL_LIGHT, LVL_HEAVY]:
        if lvl not in out:
            out[lvl] = float("nan")
            cnt[lvl] = 0
        out[lvl] = out[lvl].where(cnt[lvl].fillna(0) >= MIN_MINUTES)
    out = out[[LVL_NONE, LVL_LIGHT, LVL_HEAVY]]
    out[COL_LOSS] = 1 - out[LVL_HEAVY] / out[LVL_NONE]
    out.columns = [f"{c} (tok/s)" if c != COL_LOSS else c for c in out.columns]
    return out.reset_index().rename(columns={"provider": "Stack", "conc": "Conversations en cours"})


def plateau(sat: pd.DataFrame) -> dict[str, str]:
    """Pour chaque stack, la concurrence à partir de laquelle le débit cumulé ne gagne plus 10 %."""
    res = {}
    for stack, g in sat[sat[COL_MIN] >= MIN_MINUTES].groupby("Stack"):
        vals = g.sort_values("Conversations en cours")[["Conversations en cours", COL_AGG]].to_numpy()
        res[stack] = None
        for i, (c, a) in enumerate(vals[:-1]):
            if all(later < a * 1.10 for later in vals[i + 1:, 1]):
                res[stack] = str(c)
                break
    return res


def _ratio(a: pd.Series, b: pd.Series) -> float | None:
    if len(a) < MIN_MINUTES or len(b) < MIN_MINUTES:
        return None
    lo = b.median()
    return a.median() / lo if lo > 0 else None


def verdict(d: pd.DataFrame) -> dict:
    """Compare les causes de baisse du débit par conversation, chacune à autres facteurs fixés,
    et pondère par leur fréquence : une cause sévère mais rare pèse moins qu'une cause modérée et fréquente."""
    total = len(d)
    calm = d[d["prefill_lvl"] == LVL_NONE]
    low = d["conc"].isin(["1", "2"])
    crowded = ["4", "5 – 6", "7 et plus"]
    effects = []  # (impact, ratio, exposition, libellé)

    def add(ratio, exposure, label):
        if ratio:
            effects.append(((1 - 1 / ratio) * exposure if ratio > 1 else 0.0, ratio, exposure, label))

    # 1. bande passante : 1 conversation vs 4 et plus, sans lecture ; exposition = minutes à 4 conversations et plus
    add(_ratio(calm.loc[calm["conc"] == "1", "per_stream"], calm.loc[calm["conc"].isin(crowded), "per_stream"]),
        d["conc"].isin(crowded).mean(), "la bande passante partagée (1 conversation contre 4 et plus, sans lecture)")
    # 2. lecture lourde, à faible concurrence ; exposition = minutes en lecture lourde
    add(_ratio(d.loc[low & (d["prefill_lvl"] == LVL_NONE), "per_stream"],
               d.loc[low & (d["prefill_lvl"] == LVL_HEAVY), "per_stream"]),
        (d["prefill_lvl"] == LVL_HEAVY).mean(), "une lecture lourde en cours (à 1 ou 2 conversations)")
    # 3. spéculatif : à concurrence fixée (1 conversation pleine), sans lecture
    s = calm[(calm["conc"] == "1") & (calm["running"] >= 0.9)].dropna(subset=["spec_yield"])
    spec_txt = ""
    if len(s) >= 4 * MIN_MINUTES:
        hi, lo = s["spec_yield"].quantile(0.75), s["spec_yield"].quantile(0.25)
        r = _ratio(s.loc[s["spec_yield"] >= hi, "per_stream"], s.loc[s["spec_yield"] <= lo, "per_stream"])
        if r and r >= 1:
            add(r, 0.5, f"le rendement du décodage spéculatif ({hi:.1f} contre {lo:.1f} tokens par pas, à 1 conversation)")
        elif r:
            spec_txt = " Décodage spéculatif : pas d'effet mesurable à concurrence fixée."
    if not effects:
        return {"lvl": "info", "txt": f"Pas assez de minutes (≥ {MIN_MINUTES} par situation) pour départager "
                                      "les causes de baisse du débit d'écriture."}
    effects.sort(reverse=True)
    parts = [f"{label} : ×{r:.1f} quand elle joue, {exp:.0%} des minutes d'écriture"
             for _, r, exp, label in effects]
    txt = (f"Sur {total} minutes, la baisse de débit d'écriture la plus coûteuse au total vient de {parts[0]}."
           + (f" Puis {' ; '.join(parts[1:])}." if len(parts) > 1 else "") + spec_txt)
    return {"lvl": "warn" if effects[0][1] >= 1.5 else "ok", "txt": txt}


# --- Par requête : SpendLogs de LiteLLM (tout l'historique, avant même Prometheus) -------
MIN_OUT_TOKENS = 50      # en dessous, la fin du flux pèse plus que l'écriture elle-même
HEAVY_PREFILL_S = 20     # lecture longue = premier token après 20 s ou plus : vraie lecture à froid,
                         # pas un gros prompt servi par le cache (800 k tokens en cache ≈ 6 s)
MIN_REQUESTS = 30
COL_RQ = "Requêtes"
COL_RQ_P50 = "Débit médian (tok/s)"
COL_RQ_P10 = "Débit des 10 % plus lentes (tok/s)"


def _active_at(starts, ends, t):
    """Nombre d'intervalles [start, end[ contenant chaque instant de t (tableaux triés en entrée)."""
    return starts.searchsorted(t, side="right") - ends.searchsorted(t, side="right")


def request_conditions(df: pd.DataFrame) -> pd.DataFrame:
    """Requêtes streamées (≥ 50 tokens générés) et circonstances AU MILIEU de leur écriture, sur la même stack :
      ecr_autres     : autres requêtes en train d'écrire (bande passante partagée) ;
      lec_longue     : une autre requête est dans une lecture longue (≥ 20 s avant son premier token) ;
      tok_s          : débit d'écriture de la requête = tokens générés / (fin − premier token).
    df : colonnes start, first_tok, end_, completion_tokens, prompt_tokens, api_base (horodatages tz-aware)."""
    ok = df["first_tok"].notna() & (df["first_tok"] > df["start"]) & (df["end_"] > df["first_tok"])
    d = df[ok & (df["completion_tokens"].fillna(0) >= MIN_OUT_TOKENS)].copy()
    d["tok_s"] = d["completion_tokens"] / (d["end_"] - d["first_tok"]).dt.total_seconds()
    mid = d["first_tok"] + (d["end_"] - d["first_tok"]) / 2
    d["ecr_autres"] = 0
    d["lec_longue"] = False
    all_ok = df[ok]
    long_pf = all_ok[(all_ok["first_tok"] - all_ok["start"]).dt.total_seconds() >= HEAVY_PREFILL_S]
    for base, g in d.groupby("api_base"):
        same = all_ok[all_ok["api_base"] == base]
        t = mid.loc[g.index].to_numpy()
        n = _active_at(same["first_tok"].sort_values().to_numpy(), same["end_"].sort_values().to_numpy(), t)
        d.loc[g.index, "ecr_autres"] = (n - 1).clip(min=0)        # moins la requête elle-même
        lp = long_pf[long_pf["api_base"] == base]
        if not lp.empty:
            m = _active_at(lp["start"].sort_values().to_numpy(), lp["first_tok"].sort_values().to_numpy(), t)
            d.loc[g.index, "lec_longue"] = m > 0
    return d


def _bucket_writers(n: pd.Series) -> pd.Series:
    return pd.cut(n, [-1, 0, 1, 3, float("inf")], labels=["Seule", "1 autre", "2 – 3 autres", "4 autres et plus"])


def _size_bucket(pt: pd.Series) -> pd.Series:
    return pd.cut(pt, [-1, 8_000, 32_000, 100_000, 300_000, float("inf")],
                  labels=["< 8k", "8k – 32k", "32k – 100k", "100k – 300k", "≥ 300k"])


def request_tables(d: pd.DataFrame) -> dict:
    """Un tableau par facteur, chacun mesuré à autres facteurs maîtrisés."""
    def summ(frame, by):
        g = frame.groupby(by, observed=True)["tok_s"]
        return pd.DataFrame({COL_RQ: g.size(), COL_RQ_P50: g.median(), COL_RQ_P10: g.quantile(0.10)}).reset_index()

    no_pf = d[~d["lec_longue"]]
    few = d[d["ecr_autres"] <= 1]
    band = summ(no_pf.assign(**{"Autres requêtes en écriture": _bucket_writers(no_pf["ecr_autres"])}),
                "Autres requêtes en écriture")
    pref = summ(few.assign(**{"Lecture longue en cours": few["lec_longue"].map({False: "Non", True: "Oui"})}),
                "Lecture longue en cours")
    size = summ(no_pf[no_pf["ecr_autres"] <= 1].assign(**{"Taille de son propre prompt": _size_bucket(no_pf["prompt_tokens"])}),
                "Taille de son propre prompt")
    week = summ(d.assign(Semaine=d["start"].dt.strftime("%G-S%V")), "Semaine")
    return {"bandwidth": band, "prefill": pref, "size": size, "week": week}


def request_verdict(t: dict) -> dict:
    effects = []

    def ratio(tab, col, best, worst_list, label):
        x = tab[tab[COL_RQ] >= MIN_REQUESTS].set_index(col)[COL_RQ_P50]
        worst = [w for w in worst_list if w in x.index]
        if best in x.index and worst and x[worst].min() > 0:
            w = x[worst].idxmin()
            effects.append((x[best] / x[w], f"{label} ({best.lower()} {x[best]:.0f} tok/s, {w.lower()} {x[w]:.0f} tok/s)"))

    ratio(t["bandwidth"], "Autres requêtes en écriture", "Seule", ["2 – 3 autres", "4 autres et plus"],
          "d'autres requêtes qui écrivent en même temps")
    ratio(t["prefill"], "Lecture longue en cours", "Non", ["Oui"], "une lecture longue en cours sur la stack")
    ratio(t["size"], "Taille de son propre prompt", "< 8k", ["100k – 300k", "≥ 300k"], "la taille de son propre contexte")
    if not effects:
        return {"lvl": "info", "txt": f"Pas assez de requêtes streamées (≥ {MIN_REQUESTS} par situation) pour conclure."}
    effects.sort(reverse=True)
    r, name = effects[0]
    rest = " ; ".join(f"{n} ×{x:.1f}" for x, n in effects[1:])
    return {"lvl": "warn" if r >= 1.5 else "ok",
            "txt": f"Par requête (SpendLogs), le débit d'écriture baisse surtout avec {name} : ×{r:.1f}."
                   + (f" Autres facteurs : {rest}." if rest else "")}
