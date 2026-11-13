"""Synthèse pour la direction : cinq questions, réponses en langage courant.

Fonctions pures : elles reçoivent des nombres déjà calculés par report.py (dictionnaire `ctx`)
et renvoient du texte. Aucune donnée n'est inventée : une valeur absente donne une phrase
qui le dit, jamais un chiffre par défaut.
"""
from __future__ import annotations

import math

MOTS_PAR_TOKEN = 0.75   # ordre de grandeur pour le français et l'anglais
LECTURE_MOTS_S = 4      # vitesse de lecture humaine (≈ 240 mots/min)


def _ok(v) -> bool:
    return v is not None and not (isinstance(v, float) and math.isnan(v))


def pct(v: float, dec: int = 0) -> str:
    return f"{v * 100:.{dec}f} %".replace(".", ",")


def num(v: float) -> str:
    v = float(v)
    if abs(v) >= 1e6:
        return f"{v / 1e6:.1f} million{'s' if v >= 2e6 else ''}".replace(".", ",")
    return f"{v:,.0f}".replace(",", " ")


def delta(now, prev) -> str:
    """« (+12 % par rapport à la période précédente) », ou rien si la comparaison n'a pas de sens."""
    if not (_ok(now) and _ok(prev)) or prev <= 0:
        return ""
    d = now / prev - 1
    if abs(d) < 0.02:
        return " (stable par rapport à la période précédente)"
    return f" ({'+' if d > 0 else '−'}{pct(abs(d))} par rapport à la période précédente)"


def secondes(v: float) -> str:
    if v < 1:
        return "moins d'une seconde"
    if v < 90:
        return f"{v:.0f} seconde{'s' if v >= 2 else ''}"
    return f"{v / 60:.0f} minutes"


# --- les cinq questions ----------------------------------------------------------------------------
def usage(c: dict) -> dict:
    if not _ok(c.get("users")):
        return {"q": "Le service est-il utilisé ?", "lvl": "info",
                "txt": "Les données d'usage de la gateway ne sont pas disponibles sur la période."}
    parts = [f"{num(c['users'])} personnes ont utilisé le service{delta(c['users'], c.get('users_prev'))}"]
    if _ok(c.get("requests")):
        parts[0] += f", pour {num(c['requests'])} demandes{delta(c['requests'], c.get('requests_prev'))}"
    parts[0] += "."
    if _ok(c.get("target_users")) and c["target_users"] > 0:
        parts.append(f"Cela représente {pct(c['users'] / c['target_users'])} des {num(c['target_users'])} personnes ayant accès.")
    if _ok(c.get("new_users")) and c["new_users"] > 0:
        parts.append(f"{num(c['new_users'])} l'ont utilisé pour la première fois depuis la période précédente.")
    if _ok(c.get("regular_share")):
        parts.append(f"{pct(c['regular_share'])} des utilisateurs s'en servent régulièrement "
                     "(au moins un jour par semaine en moyenne).")
    if c.get("top_team") and _ok(c.get("top_team_share")):
        parts.append(f"L'équipe la plus active ({c['top_team']}) représente {pct(c['top_team_share'])} des demandes.")
    if c.get("peak"):
        parts.append(f"Le moment le plus chargé est {c['peak']}.")
    return {"q": "Le service est-il utilisé ?", "lvl": "ok", "txt": " ".join(parts)}


def fiabilite(c: dict) -> dict:
    parts, lvl = [], "ok"
    sr = c.get("success_rate")
    if _ok(sr):
        txt = f"{pct(sr, 1)} des demandes ont abouti"
        prev = c.get("success_rate_prev")
        if _ok(prev) and abs(sr - prev) >= 0.002:  # en points : un taux de succès ne se compare pas en %
            txt += (f" ({'+' if sr > prev else '−'}{abs(sr - prev) * 100:.1f} point par rapport à la période "
                    "précédente)").replace(".", ",")
        parts.append(txt + ".")
        if sr < 0.98:
            lvl = "warn"
    av = c.get("availability")
    if _ok(av):
        if av >= 0.999:
            parts.append("Les serveurs ont été joignables en permanence.")
        else:
            parts.append(f"Le serveur le moins disponible ({c.get('availability_stack', '?')}) a été joignable {pct(av, 1)} du temps.")
            if av < 0.99:
                lvl = "warn"
    fast = c.get("canary_fast_share")
    if _ok(fast):
        parts.append(f"Une question simple posée chaque minute par un utilisateur fictif a obtenu le début de sa réponse "
                     f"en moins de 10 secondes dans {pct(fast, 1)} des cas.")
        if fast < 0.95:
            lvl = "warn"
    elif _ok(c.get("small_ttft_p95")):
        parts.append(f"Pour une question courte, la réponse commence à s'afficher en moins de "
                     f"{secondes(c['small_ttft_p95'])} dans 95 % des cas.")
    ws = c.get("write_speed")
    if _ok(ws) and ws > 0:
        mots = ws * MOTS_PAR_TOKEN
        comp = "plus vite qu'on ne lit" if mots >= LECTURE_MOTS_S else "moins vite qu'on ne lit"
        parts.append(f"La réponse s'écrit ensuite à environ {mots:.0f} mots par seconde, {comp}.")
    if not parts:
        return {"q": "Le service est-il fiable ?", "lvl": "info", "txt": "Pas de mesure de fiabilité disponible sur la période."}
    return {"q": "Le service est-il fiable ?", "lvl": lvl, "txt": " ".join(parts)}


def gros_usages(c: dict) -> dict:
    q = "Les très gros usages gênent-ils les autres ?"
    v = c.get("hyp")
    if not v:
        return {"q": q, "lvl": "info", "txt": "Question non évaluée : nécessite l'historique détaillé des demandes (SpendLogs)."}
    r = v.get("ratio_p50")
    if r is None:
        return {"q": q, "lvl": "info", "txt": "Pas encore assez d'observations comparables pour répondre de façon fiable."}
    expo = v.get("exposure") or 0
    if v["lvl"] == "warn":
        return {"q": q, "lvl": "warn", "txt":
                f"Oui. Quand une personne soumet un très long document, les autres attendent environ {r:.0f} fois plus "
                f"longtemps le début de leur réponse, à affluence égale. Cela concerne {pct(expo, 1)} des demandes courtes."}
    if v["lvl"] == "ok":
        txt = ("Non. À affluence égale, les demandes courtes ne sont pas ralenties quand une autre personne soumet un très "
               "long document.")
        raw = v.get("raw_ratio_p50")
        if raw and raw >= 1.3 and raw >= 1.5 * r:
            txt += (" Les ralentissements ressentis viennent des heures de pointe, quand beaucoup de personnes utilisent "
                    "le service en même temps.")
        return {"q": q, "lvl": "ok", "txt": txt}
    return {"q": q, "lvl": "info", "txt":
            f"Effet limité ou incertain : à affluence égale, l'attente des autres est multipliée par {r:.1f} environ, "
            f"pour {pct(expo, 1)} des demandes courtes. À confirmer sur la période suivante."}


def dimensionnement(c: dict) -> dict:
    q = "L'infrastructure est-elle bien dimensionnée ?"
    parts, lvl = [], "ok"
    if _ok(c.get("conc_max")):
        parts.append(f"Au plus fort, {num(c['conc_max'])} demandes ont été traitées en même temps.")
    qs = c.get("queue_share")
    if _ok(qs):
        if qs < 0.01:
            parts.append("Les demandes n'ont pratiquement jamais eu à attendre leur tour.")
        else:
            parts.append(f"Des demandes ont dû attendre leur tour {pct(qs, 0 if qs >= 0.1 else 1)} du temps"
                         + (f" sur le serveur {c['queue_stack']}" if c.get("queue_stack") else "") + ".")
            if qs > 0.05:
                lvl = "warn"
    if c.get("plateau"):
        parts.append(f"Au-delà d'environ {c['plateau']} conversations simultanées par serveur, chaque conversation "
                     "supplémentaire ralentit les autres sans augmenter le volume traité : c'est la limite actuelle.")
    if not parts:
        return {"q": q, "lvl": "info", "txt": "Pas de mesure de capacité disponible sur la période."}
    return {"q": q, "lvl": lvl, "txt": " ".join(parts)}


def actions(c: dict, reponses: list[dict]) -> dict:
    q = "Que propose-t-on ?"
    props = []
    hyp = c.get("hyp") or {}
    if hyp.get("lvl") == "warn":
        props.append("Publier des conseils d'usage pour les très longs documents (découper, résumer d'abord) et, si la gêne "
                     "persiste, limiter la taille des demandes ou le nombre de demandes simultanées par compte.")
    qs = c.get("queue_share")
    if _ok(qs) and qs > 0.05:
        growth = (c["users"] / c["users_prev"] - 1) if _ok(c.get("users")) and _ok(c.get("users_prev")) and c["users_prev"] else None
        txt = "Limiter le nombre de demandes simultanées par compte pour répartir la capacité aux heures de pointe"
        if growth is not None and growth > 0.15:
            txt += f", et anticiper une extension : l'usage progresse de {pct(growth)} sur une période"
        props.append(txt + ".")
    av = c.get("availability")
    if _ok(av) and av < 0.99:
        props.append("Analyser les interruptions de service de la période et en informer les utilisateurs.")
    sr = c.get("success_rate")
    if _ok(sr) and sr < 0.98:
        props.append("Analyser les causes des demandes en échec (limites atteintes, délais dépassés, documents trop longs).")
    if not props:
        if any(r["lvl"] == "info" for r in reponses):
            props.append("Pas d'action sur l'infrastructure. Compléter la collecte des données manquantes avant la prochaine période.")
        else:
            props.append("Pas d'action nécessaire : le service absorbe la charge actuelle. Poursuivre le suivi mensuel.")
    return {"q": q, "lvl": "warn" if any(r["lvl"] == "warn" for r in reponses) else "ok", "txt": " ".join(props)}


def build(c: dict) -> list[dict]:
    rep = [usage(c), fiabilite(c), gros_usages(c), dimensionnement(c)]
    return rep + [actions(c, rep)]


GLOSSAIRE = [
    ("Demande", "Une question envoyée à l'assistant, avec ses documents joints éventuels."),
    ("Début de réponse", "Délai avant que la réponse commence à s'afficher (TTFT dans la suite du rapport)."),
    ("Vitesse d'écriture", "Rythme auquel la réponse s'affiche une fois commencée (tokens par seconde)."),
    ("Token", "Morceau de mot, environ 3/4 de mot : l'unité de travail de l'IA."),
    ("Serveur (stack)", "Un ensemble de machines de calcul qui fait tourner un modèle."),
    ("Utilisateur fictif (sonde)", "Programme qui pose la même question simple chaque minute pour mesurer le service."),
    ("À affluence égale", "Comparaison faite à la même heure sur le même serveur, pour ne pas confondre une gêne avec l'heure de pointe."),
]
