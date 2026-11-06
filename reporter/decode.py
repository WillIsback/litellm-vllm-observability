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
    out["Perte en lecture lourde"] = 1 - out[LVL_HEAVY] / out[LVL_NONE]
    out.columns = [f"{c} (tok/s)" if c != "Perte en lecture lourde" else c for c in out.columns]
    return out.reset_index().rename(columns={"provider": "Stack", "conc": "Conversations en cours"})


def plateau(sat: pd.DataFrame) -> dict[str, str]:
    """Pour chaque stack, la concurrence à partir de laquelle le débit cumulé ne gagne plus 10 %."""
    res = {}
    for stack, g in sat[sat[COL_MIN] >= MIN_MINUTES].groupby("Stack"):
        vals = g.sort_values("Conversations en cours")[["Conversations en cours", COL_AGG]].to_numpy()
        res[stack] = None
        for (c_prev, a_prev), (_, a) in zip(vals, vals[1:]):
            if a < a_prev * 1.10:
                res[stack] = str(c_prev)
                break
    return res


def _ratio(a: pd.Series, b: pd.Series) -> float | None:
    if len(a) < MIN_MINUTES or len(b) < MIN_MINUTES:
        return None
    lo = b.median()
    return a.median() / lo if lo > 0 else None


def verdict(d: pd.DataFrame) -> dict:
    """Compare les trois causes de baisse du débit par conversation, chacune à autres facteurs fixés."""
    calm = d[d["prefill_lvl"] == LVL_NONE]
    low = d["conc"].isin(["1", "2"])
    effects = []
    # 1. bande passante : 1 conversation vs 4 et plus, sans lecture
    r = _ratio(calm.loc[calm["conc"] == "1", "per_stream"],
               calm.loc[calm["conc"].isin(["4", "5 – 6", "7 et plus"]), "per_stream"])
    if r:
        effects.append((r, "la bande passante partagée (1 conversation contre 4 et plus, sans lecture)"))
    # 2. lecture : sans lecture vs lecture lourde, à faible concurrence
    r = _ratio(d.loc[low & (d["prefill_lvl"] == LVL_NONE), "per_stream"],
               d.loc[low & (d["prefill_lvl"] == LVL_HEAVY), "per_stream"])
    if r:
        effects.append((r, "une lecture lourde en cours (à 1 ou 2 conversations)"))
    # 3. spéculatif : quart des minutes au meilleur rendement vs quart au pire, sans lecture
    s = calm.dropna(subset=["spec_yield"])
    if len(s) >= 4 * MIN_MINUTES:
        hi, lo = s["spec_yield"].quantile(0.75), s["spec_yield"].quantile(0.25)
        r = _ratio(s.loc[s["spec_yield"] >= hi, "per_stream"], s.loc[s["spec_yield"] <= lo, "per_stream"])
        if r:
            effects.append((r, f"le rendement du décodage spéculatif ({hi:.1f} contre {lo:.1f} tokens par pas)"))
    if not effects:
        return {"lvl": "info", "txt": f"Pas assez de minutes (≥ {MIN_MINUTES} par situation) pour départager "
                                      "les causes de baisse du débit d'écriture."}
    effects.sort(reverse=True)
    main = effects[0]
    rest = " ; ".join(f"{name} ×{r:.1f}" for r, name in effects[1:])
    txt = (f"Le débit d'écriture par conversation baisse surtout avec {main[1]} : ×{main[0]:.1f}."
           + (f" Autres facteurs : {rest}." if rest else ""))
    return {"lvl": "warn" if main[0] >= 1.5 else "ok", "txt": txt}
