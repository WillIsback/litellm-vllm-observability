"""Tests de la synthèse direction (python -m pytest reporter/tests)."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import synthese as syn  # noqa: E402

JARGON = re.compile(r"TTFT|ITL|KV|prefill|p95|p50|vLLM|LiteLLM|api_base|token|SLO", re.I)

COMPLET = {
    "users": 84, "users_prev": 70, "requests": 52_300, "requests_prev": 41_000, "new_users": 17,
    "regular_share": 0.46, "target_users": 400, "top_team": "DSI", "top_team_share": 0.41, "peak": "le mardi vers 10 h",
    "success_rate": 0.994, "success_rate_prev": 0.990, "availability": 0.9995, "availability_stack": "stack-a",
    "canary_fast_share": 0.97, "write_speed": 22.0,
    "hyp": {"lvl": "ok", "ratio_p50": 1.05, "raw_ratio_p50": 2.4, "exposure": 0.03},
    "conc_max": 9, "queue_share": 0.02, "queue_stack": "stack-b", "plateau": 4,
}


def test_cinq_questions_sans_jargon():
    rep = syn.build(COMPLET)
    assert [r["q"] for r in rep] == ["Le service est-il utilisé ?", "Le service est-il fiable ?",
                                     "Les très gros usages gênent-ils les autres ?",
                                     "L'infrastructure est-elle bien dimensionnée ?", "Que propose-t-on ?"]
    for r in rep:
        assert not JARGON.search(r["txt"]), r["txt"]


def test_usage_compare_a_la_periode_precedente_et_a_la_population():
    t = syn.usage(COMPLET)["txt"]
    assert "84 personnes" in t and "+20 % par rapport à la période précédente" in t
    assert "21 % des 400 personnes" in t
    assert "17 l'ont utilisé pour la première fois" in t


def test_hypothese_infirmee_explique_l_heure_de_pointe():
    t = syn.gros_usages(COMPLET)["txt"]
    assert t.startswith("Non.") and "heures de pointe" in t


def test_hypothese_confirmee_donne_l_ampleur_et_la_frequence():
    c = {**COMPLET, "hyp": {"lvl": "warn", "ratio_p50": 3.2, "exposure": 0.042}}
    r = syn.gros_usages(c)
    assert r["lvl"] == "warn" and "3 fois" in r["txt"] and "4,2 %" in r["txt"]
    assert "conseils d'usage" in syn.build(c)[-1]["txt"]


def test_donnees_absentes_pas_de_chiffre_invente():
    rep = syn.build({})
    assert all(r["lvl"] == "info" for r in rep[:4])
    assert not any(ch.isdigit() for r in rep for ch in r["txt"])


def test_file_d_attente_et_croissance_proposent_une_action():
    c = {**COMPLET, "queue_share": 0.12}
    r = syn.dimensionnement(c)
    assert r["lvl"] == "warn" and "12 % des demandes ont dû attendre" in r["txt"]
    assert "anticiper une extension" in syn.build(c)[-1]["txt"]


def test_taux_de_succes_compare_en_points():
    assert "+0,4 point" in syn.fiabilite(COMPLET)["txt"]


def test_file_part_des_demandes_contrastee_avec_la_part_du_temps():
    c = {**COMPLET, "queue_share": 0.105, "queue_time_share": 0.016}
    t = syn.dimensionnement(c)["txt"]
    assert "10 % des demandes ont dû attendre qu'une place se libère, surtout sur le serveur stack-b" in t
    assert "que 1,6 % du temps" in t and "du temps" not in t.split("L'attente")[0]
