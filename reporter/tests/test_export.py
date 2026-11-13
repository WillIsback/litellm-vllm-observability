"""Tests de l'export pour analyse par IA (python -m pytest reporter/tests)."""
import re
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import export as ex  # noqa: E402
import report as rp  # noqa: E402

# Vocabulaire qui orienterait l'analyse vers une conclusion déjà formée
ORIENTE = re.compile(r"gros prompt|petite requête|lecture lourde|lecture longue|hypothèse confirm|ralenti|gêne|verdict|satur|goulot", re.I)


def _raw():
    t = pd.Timestamp("2026-11-06 10:00", tz="UTC")
    return pd.DataFrame({
        "start": [t, t, t, t],
        "first_token": [t + pd.Timedelta(seconds=2), t + pd.Timedelta(seconds=1), None, t + pd.Timedelta(seconds=9)],
        "end_": [t + pd.Timedelta(seconds=9)] * 4,
        "prompt_tokens": [500_000, 20, 0, 1000], "completion_tokens": [300, 10, 0, 0],
        "api_base": ["http://h:8888/v1/", "http://h:8888/v1", None, "http://h:8888/v1"],
        "model": ["m"] * 4,
        "team": ["dev", "ia", "litellm-internal-health-check", "None"],
        "user_id": ["uuid-1", None, None, "uuid-1"], "user_email": ["a@x.fr", None, None, "a@x.fr"],
        "key_alias": ["perso", "canary", None, "perso"], "status": ["success"] * 4,
    })


def test_monitoring_garde_et_etiquete_pas_retire():
    r = ex.shape_requests(_raw(), rp.Anon(True), {"http://h:8888/v1": "stack-b"})
    assert r["source"].tolist() == ["usage", "sonde", "sante_litellm", "usage"]
    assert len(r) == 4


def test_colonnes_neutres_sans_classe_ni_seuil():
    r = ex.shape_requests(_raw(), rp.Anon(True), {})
    assert not any(ORIENTE.search(c) for c in r.columns)
    assert r["prompt_tokens"].iloc[0] == 500_000              # valeur brute, pas une tranche


def test_pseudonymes_stables_et_serveur_nomme():
    anon = rp.Anon(True)
    anon.keys = rp.Anon(True, prefix="clé")
    r = ex.shape_requests(_raw(), anon, {"http://h:8888/v1": "stack-b"})
    assert r.loc[0, "user"] == r.loc[3, "user"] == "user-01"
    assert "a@x.fr" not in r.to_csv()
    assert r.loc[0, "stack"] == r.loc[1, "stack"] == "stack-b"  # « …/v1/ » = « …/v1 »
    assert pd.isna(r.loc[3, "team"])                            # « None » texte = pas d'équipe


def test_ttft_vide_si_non_streame():
    r = ex.shape_requests(_raw(), rp.Anon(False), {})
    assert r.loc[0, "ttft_s"] == 2 and pd.isna(r.loc[2, "ttft_s"])
    assert r.loc[0, "user"] == "a@x.fr"                         # anonymisation désactivée


def test_consigne_tous_les_aspects_ordre_reproductible_et_neutre():
    import random
    a1, a2 = ex.ASPECTS[:], ex.ASPECTS[:]
    random.Random(3).shuffle(a1)
    random.Random(3).shuffle(a2)
    p = ex.prompt(a1, 3)
    assert a1 == a2 and all(a in p for a in ex.ASPECTS)
    assert "graine 3" in p
    assert not ORIENTE.search(p)
    assert not ORIENTE.search(ex.dictionary({"x": "stack-a"}, []))


def test_gabarit_promql_substitue_et_accolades():
    q = ex._q('sum by (provider) (rate({gtok}[1m])) + avg(up{{job="vllm"}})')
    assert rp.sel("gtok") in q and 'up{job="vllm"}' in q and "{gtok}" not in q
