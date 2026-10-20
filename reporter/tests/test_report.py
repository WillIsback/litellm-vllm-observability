"""Tests des helpers de report.py (python -m pytest reporter/tests).

Ne touche pas à Prometheus ni à Postgres : uniquement les fonctions pures
(sélecteurs, anonymisation, diagnostic).
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import report as rp  # noqa: E402


def setup_function():
    rp.EXCLUDE_TEAMS = ["litellm-internal-health-check"]
    rp.EXCLUDE_KEY_ALIASES = ["canary"]


# --- sel_gw : exclusion du monitoring des KPI gateway --------------------------
def test_sel_gw_exclut_team_sante_et_cle_sonde():
    s = rp.sel_gw("req")
    assert 'team_alias!~"litellm\\\\-internal\\\\-health\\\\-check"' in s
    assert 'api_key_alias!~"canary"' in s


def test_sel_gw_sans_exclusion_egale_sel():
    rp.EXCLUDE_TEAMS, rp.EXCLUDE_KEY_ALIASES = [], []
    assert rp.sel_gw("req") == rp.sel("req")
    assert rp.sel_gw("req", 'user!=""') == rp.sel("req", 'user!=""')


def test_sel_gw_echappe_regex_puis_promql():
    rp.EXCLUDE_KEY_ALIASES = ["canary.v2"]  # le point est un méta-caractère
    # échappé pour la regex (\\.) puis barre obliques doublée pour la chaîne PromQL (\\\\.)
    assert 'api_key_alias!~"canary\\\\.v2"' in rp.sel_gw("req")


def test_sel_gw_extras_presents_avant_les_filtres():
    s = rp.sel_gw("req", 'user!="",user!="None"')
    assert s.index('user!=""') < s.index("team_alias!~")


# --- (aucun) : utilisateur sans compte rattaché --------------------------------
def test_anon_ne_pseudonymise_pas_aucun_ni_vide():
    a = rp.Anon(True)
    assert a(rp.AUCUN) == "(aucun)"
    assert a("") == ""
    assert a(None) is None


def test_anon_pseudonymise_les_vrais_utilisateurs():
    a = rp.Anon(True)
    assert a("alice@example.com") == "user-01"
    assert a("bob") == "user-02"
    assert rp.Anon(False)("alice") == "alice"  # anonymisation désactivée


def test_anon_cle_prefixe_et_independant():
    users, keys = rp.Anon(True), rp.Anon(True, prefix="clé")
    assert users("alice") == "user-01" and keys("canary") == "clé-01"


# --- diagnostic ITL : la règle « batch trop gros » exige une concurrence réelle --
def _eng(itl, pic):
    return pd.DataFrame([{"Stack": "s1", "Requêtes": 10, "ITL p95 (s)": itl, "Pic en cours": pic,
                          "TTFT p95 (s)": 1.0, "% temps file": 0.0, "KV p95": 0.1,
                          "% temps KV>90%": 0.0, "Préemptions": 0, "Hit prefix": 0.9}])


def _itl_note(itl, pic):
    notes = rp.diagnose({"table": _eng(itl, pic)}, {"by_user": pd.DataFrame()}, None)
    return [n for n in notes if "ITL" in n["txt"] and "SLO" in n["txt"]]


def test_itl_eleve_mais_concurrence_faible_est_informatif():
    n = _itl_note(0.14, 2)
    assert len(n) == 1 and n[0]["lvl"] == "info"
    assert "concurrence reste faible" in n[0]["txt"] and "pic 2" in n[0]["txt"]


def test_itl_eleve_avec_concurrence_avertit():
    n = _itl_note(0.14, 5)
    assert len(n) == 1 and n[0]["lvl"] == "warn"
    assert "batch de décodage trop gros" in n[0]["txt"] and "5 requête(s) en cours" in n[0]["txt"]


def test_itl_sous_slo_silencieux():
    assert _itl_note(0.05, 5) == []


def test_itl_concurrence_inconnue_reste_prudent():
    n = _itl_note(0.14, None)
    assert len(n) == 1 and n[0]["lvl"] == "warn"
    assert "inconnue" in n[0]["txt"]
