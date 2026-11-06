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
    assert round(t.loc["1", "Perte en lecture lourde"], 2) == 0.8
    assert pd.isna(t.loc["2", f"{dec.LVL_HEAVY} (tok/s)"])   # moins de 30 minutes : pas de conclusion


def test_verdict_designe_la_lecture_quand_elle_domine():
    m = pd.concat([
        minutes(40, 1, 30), minutes(40, 4, 25),               # bande passante : ×1,2
        minutes(40, 1, 5, prefill=2000),                      # lecture lourde : ×6
    ])
    v = dec.verdict(dec.prepare(m))
    assert v["lvl"] == "warn" and "lecture lourde" in v["txt"] and "×6.0" in v["txt"]


def test_verdict_sans_assez_de_minutes():
    assert dec.verdict(dec.prepare(minutes(5, 1, 30)))["lvl"] == "info"


def test_rendement_speculatif():
    d = dec.prepare(minutes(3, 1, 30, spec=(10.0, 15.0)))
    assert d["spec_yield"].tolist() == [2.5] * 3
