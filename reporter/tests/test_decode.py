import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import decode as dec  # noqa: E402


def minutes(n, running, per_stream, prefill=0.0, provider="stack-a", spec=(None, None)):
    """n minutes identiques : `running` conversations écrivant chacune à `per_stream` tok/s."""
    gen = running * per_stream
    drafts, accepted = spec
    return pd.DataFrame({
        "provider": provider,
        "t": pd.date_range("2026-11-01", periods=n, freq="min"),
        "running": float(running), "gen": gen, "iter_tok": gen + prefill,
        "drafts": drafts if drafts is not None else np.nan,
        "accepted": accepted if accepted is not None else np.nan,
    })


def test_prepare_calcule_debit_par_conversation_et_lecture():
    d = dec.prepare(pd.concat([minutes(3, 2, 20, prefill=1500), minutes(2, 0, 0)]))
    assert len(d) == 3                                  # minutes sans écriture exclues
    assert d["per_stream"].tolist() == [20.0] * 3
    assert d["prefill"].tolist() == [1500.0] * 3
    assert set(d["prefill_lvl"]) == {dec.LVL_HEAVY}
    assert set(d["conc"].astype(str)) == {"2"}


def test_saturation_et_plateau_de_bande_passante():
    # le débit cumulé monte de 1 à 3 conversations puis plafonne à 4
    m = pd.concat([minutes(40, 1, 30), minutes(40, 2, 24), minutes(40, 3, 20), minutes(40, 4, 15.5)])
    sat = dec.saturation(dec.prepare(m))
    agg = sat.set_index("Conversations en cours")[dec.COL_AGG]
    assert agg.loc["1"] == 30 and agg.loc["3"] == 60 and agg.loc["4"] == 62
    assert dec.plateau(sat) == {"stack-a": "3"}


def test_interference_compare_a_concurrence_egale():
    m = pd.concat([minutes(40, 1, 30), minutes(40, 1, 6, prefill=2000), minutes(5, 2, 10, prefill=2000)])
    t = dec.interference(dec.prepare(m)).set_index("Conversations en cours")
    assert t.loc["1", f"{dec.LVL_NONE} (tok/s)"] == 30
    assert t.loc["1", f"{dec.LVL_HEAVY} (tok/s)"] == 6
    assert round(t.loc["1", dec.COL_LOSS], 2) == 0.8
    assert pd.isna(t.loc["2", f"{dec.LVL_HEAVY} (tok/s)"])   # moins de 30 minutes : pas de conclusion


def test_verdict_designe_la_lecture_quand_elle_domine():
    m = pd.concat([
        minutes(40, 1, 30), minutes(40, 4, 25),               # bande passante : ×1,2
        minutes(40, 1, 5, prefill=2000),                      # lecture lourde : ×6
    ])
    v = dec.verdict(dec.prepare(m))
    assert v["lvl"] == "warn" and "×6.0" in v["txt"] and "×1.2" in v["txt"]


def test_verdict_sans_assez_de_minutes():
    assert dec.verdict(dec.prepare(minutes(5, 1, 30)))["lvl"] == "info"


def test_rendement_speculatif():
    d = dec.prepare(minutes(3, 1, 30, spec=(10.0, 15.0)))
    assert d["spec_yield"].tolist() == [2.5] * 3


T0 = pd.Timestamp("2026-10-01 09:00", tz="Europe/Paris")


def rq(start_s, ttft_s, dur_s, out, prompt=2_000, base="http://a/v1"):
    s = T0 + pd.Timedelta(seconds=start_s)
    return {"start": s, "first_tok": s + pd.Timedelta(seconds=ttft_s), "end_": s + pd.Timedelta(seconds=dur_s),
            "completion_tokens": out, "prompt_tokens": prompt, "api_base": base}


def test_par_requete_lecture_longue_et_cache():
    df = pd.DataFrame([
        rq(0, 1, 41, 400),                        # écrit de 1 à 41 s (milieu 21 s)
        rq(10, 120, 130, 10, prompt=400_000),     # lecture à froid de 10 à 120 s -> lecture longue
        rq(200, 1, 41, 400),                      # écrit de 201 à 241 s (milieu 221 s)
        rq(210, 6, 30, 10, prompt=800_000),       # 800 k servis par le cache : 6 s -> pas une lecture longue
        rq(0, 1, 41, 400, base="http://b/v1"),    # autre stack
    ])
    d = dec.request_conditions(df)
    assert d.sort_values(["api_base", "start"])["lec_longue"].tolist() == [True, False, False]
    assert round(d.iloc[0]["tok_s"], 1) == 10.0
    a2 = d[(d["api_base"] == "http://a/v1") & (d["start"] == T0 + pd.Timedelta(seconds=200))]
    assert a2["ecr_autres"].item() == 1           # la requête « en cache » écrit à 221 s


def test_par_requete_verdict():
    rows = []
    for i in range(40):
        b = i * 1000
        rows.append(rq(b, 1, 21, 400))                       # seule, sans lecture : 20 tok/s
        rows.append(rq(b + 500, 1, 81, 400))                 # pendant une lecture longue : 5 tok/s
        rows.append(rq(b + 480, 100, 140, 10, prompt=300_000))  # lecture longue (trop courte pour compter)
    t = dec.request_tables(dec.request_conditions(pd.DataFrame(rows)))
    v = dec.request_verdict(t)
    assert v["lvl"] == "warn" and "lecture longue" in v["txt"] and "×4.0" in v["txt"]


def test_plateau_ignore_un_creux_isole():
    # gain < 10 % entre 3 et 4, mais reprise ensuite : pas de plafond
    m = pd.concat([minutes(40, 1, 40), minutes(40, 3, 25), minutes(40, 4, 20.5), minutes(40, 6, 16)])
    assert dec.plateau(dec.saturation(dec.prepare(m))) == {"stack-a": None}


def test_verdict_pondere_par_la_frequence():
    # lecture lourde très pénalisante mais rare, bande passante modérée mais fréquente
    m = pd.concat([minutes(300, 1, 40), minutes(400, 4, 20), minutes(30, 1, 10, prefill=2000)])
    v = dec.verdict(dec.prepare(m))
    assert v["txt"].index("bande passante") < v["txt"].index("lecture lourde")
