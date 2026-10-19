"""Tests des analyses SpendLogs (python -m pytest reporter/tests)."""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import analyse as hyp  # noqa: E402

T0 = pd.Timestamp("2026-10-01 10:00", tz="Europe/Paris")
SMALL, LARGE = 8000, 100_000


def req(start_s, ttft_s, dur_s, tokens, key, base="http://a/v1", user=None):
    start = T0 + pd.Timedelta(seconds=start_s)
    first = start + pd.Timedelta(seconds=ttft_s) if ttft_s is not None else start + pd.Timedelta(seconds=dur_s)
    return {"start": start, "end_": start + pd.Timedelta(seconds=dur_s), "first_tok": first,
            "ttft": ttft_s, "dur": dur_s, "prompt_tokens": tokens, "api_base": base,
            "key_id": key, "user_": user or key, "key_alias": f"alias-{key}"}


def frame(rows):
    return pd.DataFrame(rows)


def test_petite_requete_marquee_seulement_si_gros_prompt_autre_cle_meme_stack_en_lecture():
    df = frame([
        req(0, 300, 310, 500_000, "lourd"),                  # gros prompt en lecture de 0 à 300 s
        req(10, 120, 125, 500, "victime"),                    # attend pendant la lecture -> marquée
        req(10, 1, 3, 500, "lourd"),                          # même clé que le gros -> pas marquée
        req(10, 1, 3, 500, "autre", base="http://b/v1"),      # autre stack -> pas marquée
        req(400, 0.5, 2, 500, "victime"),                     # après la lecture -> pas marquée
        req(20, 290, 295, 50_000, "moyen"),                   # pas « petite » -> ignorée
    ])
    s = hyp.heavy_in_flight(df, SMALL, LARGE)
    v = s[(s["key_id"] == "victime") & (s["api_base"] == "http://a/v1")].sort_values("start")
    assert v["gros_en_cours"].tolist() == [True, False]
    assert not s.loc[s["key_id"] == "lourd", "gros_en_cours"].any()
    assert not s.loc[s["api_base"] == "http://b/v1", "gros_en_cours"].any()
    assert len(s) == 4


def test_petites_requetes_non_streamees_exclues():
    df = frame([req(0, None, 5, 500, "x")])
    df["ttft"] = float("nan")
    assert hyp.heavy_in_flight(df, SMALL, LARGE).empty


def test_verdict_confirme_quand_ttft_explose_avec_gros_prompt():
    rows = [req(0, 600, 610, 800_000, "lourd")]
    rows += [req(1000 + i * 20, 0.5, 3, 300, "c") for i in range(40)]   # calme
    rows += [req(5 + i * 10, 60, 65, 300, "c") for i in range(40)]      # pendant la lecture
    table, v = hyp.contention(frame(rows), SMALL, LARGE)
    assert table["Requêtes"].tolist() == [40, 40]
    assert v["lvl"] == "warn" and "CONFIRMÉE" in v["txt"]
    assert v["ratio_p50"] > 100


def test_verdict_non_confirme_quand_pas_d_effet():
    rows = [req(0, 600, 610, 800_000, "lourd")]
    rows += [req(1000 + i * 20, 0.5, 3, 300, "c") for i in range(40)]
    rows += [req(5 + i * 10, 0.55, 3, 300, "c") for i in range(40)]
    _, v = hyp.contention(frame(rows), SMALL, LARGE)
    assert v["lvl"] == "ok" and "NON CONFIRMÉE" in v["txt"]


def test_verdict_prudent_si_pas_assez_d_echantillons():
    rows = [req(0, 600, 610, 800_000, "lourd"), req(5, 60, 65, 300, "c"), req(1000, 0.5, 3, 300, "c")]
    _, v = hyp.contention(frame(rows), SMALL, LARGE)
    assert v["lvl"] == "info" and v["ratio_p50"] is None


def test_concurrence_par_cle_et_minutes_au_dessus_des_limites():
    # « dev » : 4 requêtes simultanées pendant 3 minutes ; « rh » : une à la fois
    rows = [req(0, 1, 180, 200_000, "dev") for _ in range(4)]
    rows += [req(i * 100, 1, 30, 1000, "rh") for i in range(3)]
    k = hyp.key_concurrency(frame(rows)).set_index("Utilisateur")
    assert k.loc["dev", "Pic simultané"] == 4
    assert k.loc["dev", "Minutes > 3"] >= 3 and k.loc["dev", "Minutes > 4"] == 0
    assert k.loc["rh", "Pic simultané"] == 1 and k.loc["rh", "Minutes > 2"] == 0


def test_ttft_par_tranche_et_part_des_tokens():
    rows = [req(0, 0.4, 2, 1000, "a"), req(5, 0.6, 2, 3000, "b"), req(10, 200, 210, 296_000, "c")]
    t = hyp.ttft_by_bucket(frame(rows), SMALL, LARGE).set_index("Taille du prompt")
    assert t.loc["< 8k", "Requêtes"] == 2 and t.loc["≥ 100k", "Requêtes"] == 1
    assert abs(t.loc["≥ 100k", "Part tokens in"] - 0.98666) < 1e-3
    assert t.loc["8k – 32k", "Requêtes"] == 0


def test_top_prompts_trie_par_taille():
    rows = [req(i, 1, 2, n, f"k{i}") for i, n in enumerate([10, 900_000, 5000])]
    assert hyp.top_prompts(frame(rows), 2)["Tokens in"].tolist() == [900_000, 5000]


def test_niveau_par_minute_conserve_les_requetes_longues():
    ev = pd.DataFrame({"t": [T0, T0 + pd.Timedelta(minutes=5)], "d": [1, -1]})
    ev["c"] = ev["d"].cumsum()
    lvl = hyp.per_minute_level(ev)
    assert (lvl.iloc[:5] == 1).all()   # 5 minutes en cours, pas seulement la minute du début


def test_gene_pendant_l_ecriture_et_api_base_normalisee_par_le_rapport():
    # Cas réel (LiteLLM) : premier token rapide, puis écriture qui rampe pendant la lecture géante,
    # et api_base écrite « …/v1/ » pour le gros prompt non streamé contre « …/v1 » pour la petite requête.
    df = frame([req(0, 134, 134, 243_400, "lourd", base="http://x/v1/"), req(6, 4.6, 114, 40, "sonde", base="http://x/v1")])
    df["api_base"] = df["api_base"].str.rstrip("/")  # normalisation faite par report.spendlogs_section
    s = hyp.heavy_in_flight(df, SMALL, LARGE)
    assert s["gros_en_cours"].tolist() == [True]
    table, _ = hyp.contention(df, SMALL, LARGE)
    assert table.loc[1, "Durée p95 (s)"] > 100
