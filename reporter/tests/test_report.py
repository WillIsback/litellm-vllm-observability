"""Tests des helpers de report.py (python -m pytest reporter/tests).

Ne touche pas à Prometheus ni à Postgres : uniquement les fonctions pures
(sélecteurs, anonymisation, diagnostic).
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import report as rp  # noqa: E402


@pytest.fixture(autouse=True)
def _default_exclusions(monkeypatch):
    """Exclusions de monitoring par défaut, restaurées après chaque test."""
    monkeypatch.setattr(rp, "EXCLUDE_TEAMS", ["litellm-internal-health-check"])
    monkeypatch.setattr(rp, "EXCLUDE_KEY_ALIASES", ["canary"])


# --- sel_gw : exclusion du monitoring des KPI gateway --------------------------
def test_sel_gw_exclut_team_sante_et_cle_sonde():
    s = rp.sel_gw("req")
    assert 'team_alias!~"litellm\\\\-internal\\\\-health\\\\-check"' in s
    assert 'api_key_alias!~"canary"' in s


def test_sel_gw_sans_exclusion_egale_sel(monkeypatch):
    monkeypatch.setattr(rp, "EXCLUDE_TEAMS", [])
    monkeypatch.setattr(rp, "EXCLUDE_KEY_ALIASES", [])
    assert rp.sel_gw("req") == rp.sel("req")
    assert rp.sel_gw("req", 'user!=""') == rp.sel("req", 'user!=""')


def test_sel_gw_echappe_regex_puis_promql(monkeypatch):
    monkeypatch.setattr(rp, "EXCLUDE_KEY_ALIASES", ["canary.v2"])  # le point est un méta-caractère
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
    assert users("alice") == "user-01"
    assert keys("canary") == "clé-01"


# --- diagnostic ITL : la règle « batch trop gros » exige une concurrence réelle --
def _eng(itl, pic):
    return pd.DataFrame([{"Stack": "s1", "Requêtes": 10, "ITL p95 (s)": itl, "Pic en cours": pic,
                          "TTFT p95 (s)": 1.0, rp.COL_Q_TIME: 0.0, "KV p95": 0.1,
                          "% temps KV>90%": 0.0, "Préemptions": 0, "Hit prefix": 0.9}])


@pytest.fixture(autouse=True)
def _slo_itl_strict(monkeypatch):
    # SLO « académique » de 100 ms : les cas ci-dessous (140 ms) le dépassent ; le défaut réel est 0,2 s
    monkeypatch.setattr(rp, "SLO_ITL", 0.10)


def _itl_note(itl, pic):
    notes = rp.diagnose({"table": _eng(itl, pic)}, {"by_user": pd.DataFrame()}, None)
    return [n for n in notes if "ITL" in n["txt"] and "SLO" in n["txt"]]


def test_itl_eleve_mais_concurrence_faible_est_informatif():
    n = _itl_note(0.14, 2)
    assert len(n) == 1
    assert n[0]["lvl"] == "info"
    assert "concurrence reste faible" in n[0]["txt"]
    assert "pic 2" in n[0]["txt"]


def test_itl_eleve_avec_concurrence_avertit():
    n = _itl_note(0.14, 5)
    assert len(n) == 1
    assert n[0]["lvl"] == "warn"
    assert "batch de décodage trop gros" in n[0]["txt"]
    assert "5 requête(s) en cours" in n[0]["txt"]


def test_itl_sous_slo_silencieux():
    assert _itl_note(0.05, 5) == []


def test_itl_concurrence_inconnue_reste_prudent():
    n = _itl_note(0.14, None)
    assert len(n) == 1
    assert n[0]["lvl"] == "warn"
    assert "inconnue" in n[0]["txt"]


# --- file d'attente : part des requêtes, pas part du temps --------------------------------------
def test_requetes_arrivees_pendant_une_file_par_serveur():
    t0 = pd.Timestamp("2026-11-10 10:00", tz="Europe/Paris")
    minutes = pd.date_range(t0, periods=5, freq="1min")
    # file sur stack-b pendant la minute 10:01-10:02 (échantillon max_over_time[1m] pris à 10:02)
    waiting = pd.DataFrame({"stack-a": [0, 0, 0, 0, 0], "stack-b": [0, 0, 2, 0, 0]}, index=minutes)
    arrivals = pd.DataFrame({
        "start": [t0 + pd.Timedelta(seconds=s) for s in (70, 90, 70, 200, 10)],
        "api_base": ["http://b/v1/", "http://b/v1", "http://a/v1", "http://b/v1", "http://inconnu/v1"],
    })
    q = rp.queued_arrivals(arrivals, waiting, {"http://a/v1": "stack-a", "http://b/v1": "stack-b"})
    assert q["n"] == 4                                    # api_base inconnu : hors calcul
    assert q["share"] == 0.5                              # les 2 arrivées sur b entre 10:01 et 10:02
    assert q["by_stack"] == {"stack-a": 0.0, "stack-b": 2 / 3}
    assert q["count_by_stack"] == {"stack-a": 0, "stack-b": 2}


def test_serveur_cite_est_celui_qui_a_fait_attendre_le_plus_de_demandes():
    # stack-a : taux plus élevé mais peu de trafic ; stack-b : plus de demandes en attente au total
    sl = {"conc": {"max": 3}, "regular_share": None, "small_ttft_p95": None, "verdict": None,
          "queued": {"share": 0.1, "n": 1000, "by_stack": {"stack-a": 0.13, "stack-b": 0.09},
                     "count_by_stack": {"stack-a": 31, "stack-b": 69}}}
    gw = {"kpi": dict.fromkeys(["requests", "failures", "users"]), "kpi_prev": dict.fromkeys(["requests", "failures", "users"]),
          "by_team": pd.DataFrame()}
    c = rp.synth_context(gw, {"table": pd.DataFrame()}, {"summary": pd.DataFrame()}, sl)
    assert c["queue_share"] == 0.1 and c["queue_stack"] == "stack-b"


def test_file_sans_donnees_renvoie_none():
    assert rp.queued_arrivals(pd.DataFrame(columns=["start", "api_base"]), pd.DataFrame(), {}) is None


def test_diagnostic_juge_la_file_sur_les_requetes():
    eng = _eng(0.05, 6).assign(**{rp.COL_Q_REQ: 0.10, rp.COL_Q_TIME: 0.016})
    notes = rp.diagnose({"table": eng}, {"by_user": pd.DataFrame()}, None)
    n = [x for x in notes if "place" in x["txt"]]
    assert len(n) == 1 and n[0]["lvl"] == "warn"
    assert "10% des requêtes ont attendu plus de 1 s" in n[0]["txt"] and "1.6% du temps" in n[0]["txt"]
