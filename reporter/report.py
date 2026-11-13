#!/usr/bin/env python3
"""Rapport d'usage de l'infra LLM (LiteLLM + stacks vLLM).

Sources :
  - Prometheus (métriques LiteLLM /metrics et vLLM /metrics)
  - Postgres LiteLLM, table LiteLLM_SpendLogs (optionnel, données exactes par requête)

Usage :
  report.py                       # 30 derniers jours
  report.py --days 7
  report.py --previous-month      # mois calendaire précédent
  report.py --start 2026-10-01 --end 2026-11-01
  report.py check                 # vérifie cibles et noms de métriques disponibles
  report.py export --days 30      # données brutes + consigne neutre pour une analyse par IA
"""
from __future__ import annotations

import argparse
import base64
import io
import math
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
import requests  # noqa: E402
from jinja2 import Environment, FileSystemLoader, select_autoescape  # noqa: E402

import analyse as hyp  # noqa: E402
import decode as dec  # noqa: E402
import synthese as syn  # noqa: E402

TZ = ZoneInfo(os.environ.get("TZ", "Europe/Paris"))
PROM_URL = os.environ.get("PROM_URL", "http://localhost:9090").rstrip("/")
OUT_DIR = Path(os.environ.get("REPORT_DIR", "/reports"))
SLO_TTFT = float(os.environ.get("SLO_TTFT_P95", "2.0"))
SLO_ITL = float(os.environ.get("SLO_ITL_P95", "0.2"))  # même défaut que .env.example (plafond de décodage GB10)
# Nombre de personnes ayant accès au service (optionnel) : donne le taux d'adoption dans la synthèse
SCRAPE_S = 15  # scrape_interval de prometheus/prometheus.yml
TARGET_USERS = int(os.environ.get("TARGET_USERS", "0") or 0) or None
ANONYMIZE = os.environ.get("ANONYMIZE_USERS", "true").lower() in ("1", "true", "yes")
# Seuils de l'analyse « gros prompts vs petites requêtes » (tokens d'entrée)
SMALL_PROMPT = int(os.environ.get("SMALL_PROMPT_TOKENS", "8000"))
LARGE_PROMPT = int(os.environ.get("LARGE_PROMPT_TOKENS", "100000"))
# Équipes et alias de clés sans usage réel, exclus des KPI et des totaux :
# le health-check interne de LiteLLM et la clé de la sonde témoin gonflent les compteurs.
EXCLUDE_TEAMS = [t.strip() for t in os.environ.get("EXCLUDE_TEAMS", "litellm-internal-health-check").split(",") if t.strip()]
EXCLUDE_KEY_ALIASES = [k.strip() for k in os.environ.get("EXCLUDE_KEY_ALIASES", "canary").split(",") if k.strip()]
# Concurrence (requêtes en cours) au-delà de laquelle un ITL élevé est imputé au batch de décodage ;
# en deçà, c'est la vitesse de décodage du modèle (bande passante mémoire), pas une saturation.
CONC_ITL_MIN = 3
AUCUN = "(aucun)"  # utilisateur sans compte rattaché (Open WebUI, sonde…) : jamais pseudonymisé ni compté comme actif
# Libellés de colonnes réutilisés dans plusieurs tableaux (évite les littéraux dupliqués).
COL_TOKENS_IN = "Tokens in"
COL_TOKENS_OUT = "Tokens out"
COL_TTFT_P50 = "TTFT p50 (s)"
COL_TTFT_P95 = "TTFT p95 (s)"

# --- noms de métriques (regex tolérantes aux changements de version) ---------
M = {
    # LiteLLM
    "req": "litellm_proxy_total_requests_metric(_total)?",
    "fail": "litellm_proxy_failed_requests_metric(_total)?",
    "tin": "litellm_input_tokens_metric(_total)?|litellm_input_tokens(_total)?|litellm_prompt_tokens_metric(_total)?",
    "tout": "litellm_output_tokens_metric(_total)?|litellm_output_tokens(_total)?|litellm_completion_tokens_metric(_total)?",
    "lat": "litellm_request_total_latency_metric_bucket",
    "ttft": "litellm_llm_api_time_to_first_token_metric_bucket",
    "ovh": "litellm_overhead_latency_metric_bucket",
    # vLLM
    "running": "vllm:num_requests_running",
    "waiting": "vllm:num_requests_waiting",
    "kv": "vllm:kv_cache_usage_perc|vllm:gpu_cache_usage_perc",
    "preempt": "vllm:num_preemptions(_total)?",
    "vreq": "vllm:request_success(_total)?",
    "ptok": "vllm:prompt_tokens(_total)?",
    "gtok": "vllm:generation_tokens(_total)?",
    "vttft": "vllm:time_to_first_token_seconds_bucket",
    "itl": "vllm:inter_token_latency_seconds_bucket|vllm:time_per_output_token_seconds_bucket",
    "e2e": "vllm:e2e_request_latency_seconds_bucket",
    "queue": "vllm:request_queue_time_seconds_bucket",
    "phit": "vllm:(gpu_)?prefix_cache_hits(_total)?",
    "pq": "vllm:(gpu_)?prefix_cache_queries(_total)?",
    # écriture : pas de calcul, temps de décodage par requête, décodage spéculatif
    "iter": "vllm:iteration_tokens_total_sum",
    "dec_time": "vllm:request_decode_time_seconds_sum",
    "req_gen": "vllm:request_generation_tokens_sum",
    "sd_drafts": "vllm:spec_decode_num_drafts(_total)?",
    "sd_dtok": "vllm:spec_decode_num_draft_tokens(_total)?",
    "sd_acc": "vllm:spec_decode_num_accepted_tokens(_total)?",
    # Sonde témoin (service canary)
    "canary_ttft": "canary_ttft_seconds_bucket",
    "canary_req": "canary_requests_total",
}
OPTIONAL = {"canary_ttft", "canary_req",   # absentes si la sonde n'est pas configurée
            "sd_drafts", "sd_dtok", "sd_acc"}  # absentes sans décodage spéculatif


def sel(key: str, extra: str = "") -> str:
    return '{__name__=~"%s"%s}' % (M[key], ("," + extra) if extra else "")


def sel_gw(key: str, extra: str = "") -> str:
    """sel() des métriques gateway, hors équipes/clés sans usage réel (KPI et totaux).

    Les noms sont des littéraux : les caractères spéciaux d'une regex sont échappés,
    puis les barres obliques doublées pour survivre à l'analyse de la chaîne PromQL.
    """
    parts = [extra] if extra else []
    if EXCLUDE_TEAMS:
        parts.append('team_alias!~"%s"' % "|".join(re.escape(t).replace("\\", "\\\\") for t in EXCLUDE_TEAMS))
    if EXCLUDE_KEY_ALIASES:
        parts.append('api_key_alias!~"%s"' % "|".join(re.escape(k).replace("\\", "\\\\") for k in EXCLUDE_KEY_ALIASES))
    return sel(key, ",".join(parts))


# --- client Prometheus --------------------------------------------------------
class Prom:
    def __init__(self, url: str):
        self.url = url
        self.s = requests.Session()

    def _get(self, path: str, params: dict) -> list:
        r = self.s.get(f"{self.url}{path}", params=params, timeout=120)
        r.raise_for_status()
        body = r.json()
        if body.get("status") != "success":
            raise RuntimeError(body)
        return body["data"]["result"] if "result" in body["data"] else body["data"]

    def instant(self, expr: str, at: datetime) -> list:
        return self._get("/api/v1/query", {"query": expr, "time": at.timestamp()})

    def range(self, expr: str, start: datetime, end: datetime, step: int) -> list:
        return self._get("/api/v1/query_range",
                         {"query": expr, "start": start.timestamp(), "end": end.timestamp(), "step": step})

    def names(self) -> list[str]:
        return self._get("/api/v1/label/__name__/values", {})

    # helpers
    def scalar(self, expr: str, at: datetime) -> float | None:
        try:
            res = self.instant(expr, at)
        except Exception as e:  # noqa: BLE001
            print(f"  ! requête en échec : {e}", file=sys.stderr)
            return None
        if not res:
            return None
        v = float(res[0]["value"][1])
        return None if math.isnan(v) else v

    def vector_df(self, expr: str, at: datetime, labels: list[str], col: str) -> pd.DataFrame:
        try:
            res = self.instant(expr, at)
        except Exception as e:  # noqa: BLE001
            print(f"  ! requête en échec : {e}", file=sys.stderr)
            res = []
        rows = []
        for r in res:
            v = float(r["value"][1])
            rows.append({**{lab: r["metric"].get(lab, "") for lab in labels}, col: None if math.isnan(v) else v})
        return pd.DataFrame(rows, columns=labels + [col])

    def range_df_chunked(self, expr: str, start: datetime, end: datetime, step: int, label: str,
                         chunk: timedelta = timedelta(days=7)) -> pd.DataFrame:
        """range_df par tranches : Prometheus refuse plus de 11 000 points par série et par requête."""
        parts, t = [], start
        while t < end:
            t2 = min(t + chunk, end)
            part = self.range_df(expr, t, t2, step, label)
            if not part.empty:
                parts.append(part)
            t = t2 + timedelta(seconds=step)
        if not parts:
            return pd.DataFrame()
        df = pd.concat(parts)
        return df[~df.index.duplicated()].sort_index()

    def range_df(self, expr: str, start: datetime, end: datetime, step: int, label: str) -> pd.DataFrame:
        """DataFrame index=temps (TZ locale), colonnes=valeurs du label."""
        try:
            res = self.range(expr, start, end, step)
        except Exception as e:  # noqa: BLE001
            print(f"  ! requête en échec : {e}", file=sys.stderr)
            return pd.DataFrame()
        series = {}
        for r in res:
            name = r["metric"].get(label, "") or "(vide)"
            s = pd.Series({pd.Timestamp(float(t), unit="s", tz="UTC"): float(v) for t, v in r["values"]})
            series[name] = s
        if not series:
            return pd.DataFrame()
        df = pd.DataFrame(series)
        df.index = df.index.tz_convert(TZ)
        return df.sort_index()


# --- période ------------------------------------------------------------------
@dataclass
class Period:
    start: datetime
    end: datetime

    @property
    def secs(self) -> int:
        return int((self.end - self.start).total_seconds())

    @property
    def win(self) -> str:
        return f"{self.secs}s"

    @property
    def label(self) -> str:
        return f"{self.start:%d/%m/%Y} → {(self.end - timedelta(seconds=1)):%d/%m/%Y}"

    @property
    def slug(self) -> str:
        return f"{self.start:%Y-%m-%d}_{(self.end - timedelta(seconds=1)):%Y-%m-%d}"


def parse_period(a: argparse.Namespace) -> Period:
    now = datetime.now(TZ)
    if a.previous_month:
        first_this = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        first_prev = (first_this - timedelta(days=1)).replace(day=1)
        return Period(first_prev, first_this)
    if a.start:
        start = datetime.fromisoformat(a.start).replace(tzinfo=TZ)
        end = datetime.fromisoformat(a.end).replace(tzinfo=TZ) if a.end else now
        return Period(start, min(end, now))
    return Period(now - timedelta(days=a.days), now)


# --- anonymisation --------------------------------------------------------------
@dataclass
class Anon:
    enabled: bool
    prefix: str = "user"
    mapping: dict = field(default_factory=dict)

    def __call__(self, v):
        if not self.enabled or v in ("", None) or str(v).startswith("("):
            return v
        if v not in self.mapping:
            self.mapping[v] = f"{self.prefix}-{len(self.mapping) + 1:02d}"
        return self.mapping[v]


# --- graphiques -----------------------------------------------------------------
def fig_to_b64(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def chart_lines(df: pd.DataFrame, title: str, ylabel: str, pct: bool = False, kind: str = "line") -> str | None:
    if df is None or df.empty:
        return None
    fig, ax = plt.subplots(figsize=(10, 3.4))
    if kind == "bar":
        df.index = df.index.strftime("%d/%m")
        df.plot(kind="bar", stacked=True, ax=ax, width=0.85)
    else:
        df.plot(ax=ax, linewidth=1.2)
    ax.set_title(title, fontsize=11, loc="left")
    ax.set_ylabel(ylabel)
    ax.set_xlabel("")
    ax.grid(alpha=0.3)
    if pct:
        ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0))
    ax.legend(fontsize=8, loc="upper left", ncols=min(4, len(df.columns)))
    return fig_to_b64(fig)


def chart_heatmap(df_hourly: pd.DataFrame, title: str) -> str | None:
    if df_hourly is None or df_hourly.empty:
        return None
    s = df_hourly.sum(axis=1)
    t = pd.DataFrame({"v": s.values, "dow": s.index.dayofweek, "h": s.index.hour})
    piv = t.pivot_table(index="dow", columns="h", values="v", aggfunc="mean").reindex(index=range(7), columns=range(24))
    fig, ax = plt.subplots(figsize=(10, 3.2))
    im = ax.imshow(piv.values, aspect="auto", cmap="viridis")
    ax.set_yticks(range(7), ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"])
    ax.set_xticks(range(0, 24, 2), [f"{h}h" for h in range(0, 24, 2)])
    ax.set_title(title, fontsize=11, loc="left")
    fig.colorbar(im, ax=ax, label="requêtes / h (moyenne)")
    return fig_to_b64(fig)


# --- collecte Prometheus : gateway --------------------------------------------------
def gateway_kpis(p: Prom, at: datetime, w: str) -> dict:
    """Indicateurs de tête sur la fenêtre [at - w, at] (appelé aussi sur la période précédente)."""
    req, fail = sel_gw("req"), sel_gw("fail")
    req_user = sel_gw("req", 'user!="",user!="None"')  # compteur « Utilisateurs actifs » : hors comptes rattachés à rien
    return {
        "requests": p.scalar(f"sum(increase({req}[{w}]))", at),
        "failures": p.scalar(f"sum(increase({fail}[{w}]))", at),
        "tin": p.scalar(f"sum(increase({sel_gw('tin')}[{w}]))", at),
        "tout": p.scalar(f"sum(increase({sel_gw('tout')}[{w}]))", at),
        "users": p.scalar(f"count(sum by (user) (increase({req_user}[{w}])) > 0)", at),
        "ttft_p50": p.scalar(f"histogram_quantile(0.5, sum by (le) (increase({sel_gw('ttft')}[{w}])))", at),
        "ttft_p95": p.scalar(f"histogram_quantile(0.95, sum by (le) (increase({sel_gw('ttft')}[{w}])))", at),
        "lat_p95": p.scalar(f"histogram_quantile(0.95, sum by (le) (increase({sel_gw('lat')}[{w}])))", at),
        "ovh_p95": p.scalar(f"histogram_quantile(0.95, sum by (le) (increase({sel_gw('ovh')}[{w}])))", at),
    }


def peak_moment(hourly: pd.DataFrame) -> str | None:
    """« le mardi vers 10 h » : créneau jour × heure le plus chargé en moyenne."""
    if hourly is None or hourly.empty:
        return None
    s = hourly.sum(axis=1)
    if s.max() <= 0:
        return None
    t = s.groupby([s.index.dayofweek, s.index.hour]).mean()
    dow, h = t.idxmax()
    jours = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
    return f"le {jours[dow]} vers {h} h"


def gateway_section(p: Prom, per: Period, anon: Anon) -> dict:
    at, w = per.end, per.win
    out: dict = {}
    req, fail = sel_gw("req"), sel_gw("fail")
    out["kpi"] = gateway_kpis(p, at, w)
    # Part de la fenêtre réellement collectée (scrape toutes les SCRAPE_S s) : sans historique complet,
    # une comparaison avec la période précédente montrerait une fausse croissance.
    cov = f'max(count_over_time(up{{job="litellm"}}[{w}])) / {per.secs / SCRAPE_S}'
    out["coverage"], out["coverage_prev"] = p.scalar(cov, at), p.scalar(cov, per.start)
    prev_ok = (out["coverage_prev"] or 0) >= 0.9
    out["kpi_prev"] = gateway_kpis(p, per.start, w) if prev_ok else dict.fromkeys(out["kpi"])
    req_user = sel_gw("req", 'user!="",user!="None"')
    out["new_users"] = p.scalar(
        f"count((sum by (user) (increase({req_user}[{w}])) > 0) unless on (user) "
        f"(sum by (user) (increase({req_user}[{w}] offset {w})) > 0))", at) if out["kpi_prev"]["users"] else None

    def by(labels: list[str]) -> pd.DataFrame:
        g = ", ".join(labels)
        parts = [
            p.vector_df(f"sum by ({g}) (increase({req}[{w}]))", at, labels, "Requêtes"),
            p.vector_df(f"sum by ({g}) (increase({fail}[{w}]))", at, labels, "Échecs"),
            p.vector_df(f"sum by ({g}) (increase({sel_gw('tin')}[{w}]))", at, labels, COL_TOKENS_IN),
            p.vector_df(f"sum by ({g}) (increase({sel_gw('tout')}[{w}]))", at, labels, COL_TOKENS_OUT),
            p.vector_df(f"histogram_quantile(0.95, sum by (le, {g}) (increase({sel_gw('ttft')}[{w}])))", at, labels, COL_TTFT_P95),
            p.vector_df(f"histogram_quantile(0.95, sum by (le, {g}) (increase({sel_gw('lat')}[{w}])))", at, labels, "Latence p95 (s)"),
        ]
        df = parts[0]
        for part in parts[1:]:
            df = df.merge(part, on=labels, how="outer")
        if df.empty:
            return df
        df = df[df["Requêtes"].fillna(0) > 0]  # séries présentes dans Prometheus mais sans requête sur la période
        if "user" in df:
            df["user"] = df["user"].map(lambda v: anon(AUCUN if v in ("", None, "None") else v))
        if "api_key_alias" in df:
            df["api_key_alias"] = df["api_key_alias"].map(anon.keys)
        tot = df[COL_TOKENS_IN].fillna(0) + df[COL_TOKENS_OUT].fillna(0)
        df["Part tokens"] = tot / tot.sum() if tot.sum() else 0
        return df.sort_values("Requêtes", ascending=False)

    out["by_team"] = by(["team_alias"]).rename(columns={"team_alias": "Team"})
    out["by_user"] = by(["team_alias", "user", "api_key_alias"]).rename(
        columns={"team_alias": "Team", "user": "Utilisateur", "api_key_alias": "Clé"})

    step_day = 86400
    daily = p.range_df(f"sum by (team_alias) (increase({req}[1d]))", per.start + timedelta(days=1), per.end, step_day, "team_alias")
    out["chart_daily"] = chart_lines(daily, "Requêtes par jour et par team", "requêtes", kind="bar")
    hourly = p.range_df(f"sum by (team_alias) (increase({req}[1h]))", per.start + timedelta(hours=1), per.end, 3600, "team_alias")
    out["chart_heatmap"] = chart_heatmap(hourly, "Profil d'usage : jour × heure")
    out["hourly_csv"] = hourly
    out["peak"] = peak_moment(hourly)
    return out


# --- collecte Prometheus : moteurs vLLM ----------------------------------------------
def engine_section(p: Prom, per: Period) -> dict:
    at, w = per.end, per.win
    lab = ["provider"]

    def v(expr, col):
        return p.vector_df(expr, at, lab, col)

    def hq(q, key):
        return f"histogram_quantile({q}, sum by (le, provider) (increase({sel(key)}[{w}])))"

    parts = [
        v(f"sum by (provider) (increase({sel('vreq')}[{w}]))", "Requêtes"),
        v(f"sum by (provider) (increase({sel('ptok')}[{w}]))", "Tokens prompt"),
        v(f"sum by (provider) (increase({sel('gtok')}[{w}]))", "Tokens générés"),
        v(f"max_over_time(sum by (provider) ({sel('running')})[{w}:1m])", "Pic en cours"),
        v(f"max_over_time(sum by (provider) ({sel('waiting')})[{w}:1m])", "Pic en attente"),
        v(f"avg_over_time((sum by (provider) ({sel('waiting')}) > bool 0)[{w}:1m])", "% temps file"),
        v(f"quantile_over_time(0.95, max by (provider) ({sel('kv')})[{w}:1m])", "KV p95"),
        v(f"avg_over_time((max by (provider) ({sel('kv')}) > bool 0.9)[{w}:1m])", "% temps KV>90%"),
        v(f"sum by (provider) (increase({sel('preempt')}[{w}]))", "Préemptions"),
        v(f"sum by (provider) (increase({sel('phit')}[{w}])) / clamp_min(sum by (provider) (increase({sel('pq')}[{w}])), 1)", "Hit prefix"),
        v(hq("0.5", "vttft"), COL_TTFT_P50),
        v(hq("0.95", "vttft"), COL_TTFT_P95),
        v(hq("0.95", "itl"), "ITL p95 (s)"),
        v(hq("0.95", "queue"), "File p95 (s)"),
        v(hq("0.95", "e2e"), "E2E p95 (s)"),
    ]
    df = parts[0]
    for part in parts[1:]:
        df = df.merge(part, on=lab, how="outer")
    df = df.rename(columns={"provider": "Stack"})
    if not df.empty and df["Requêtes"].notna().any():
        df["Part requêtes"] = df["Requêtes"] / df["Requêtes"].sum()

    start = per.start + timedelta(hours=1)
    run = p.range_df(f"max_over_time(sum by (provider) ({sel('running')})[1h:1m])", start, per.end, 3600, "provider")
    wait = p.range_df(f"max_over_time(sum by (provider) ({sel('waiting')})[1h:1m])", start, per.end, 3600, "provider")
    kv = p.range_df(f"max_over_time(max by (provider) ({sel('kv')})[1h:1m])", start, per.end, 3600, "provider")
    conc = pd.concat([run.add_suffix(" · en cours"), wait.add_suffix(" · en attente")], axis=1) if not run.empty else wait
    canary = pd.DataFrame()
    cl = ["target"]
    c_req, c_err, c_tt = sel("canary_req"), sel("canary_req", 'status!="ok"'), sel("canary_ttft")
    c_le10, c_inf = sel("canary_ttft", r'le=~"10(\\.0)?"'), sel("canary_ttft", 'le="+Inf"')
    cparts = [
        p.vector_df(f"sum by (target) (increase({c_req}[{w}]))", at, cl, "Sondes"),
        p.vector_df(f"sum by (target) (increase({c_err}[{w}]))", at, cl, "Échecs"),
        p.vector_df(f"histogram_quantile(0.5, sum by (le, target) (increase({c_tt}[{w}])))", at, cl, COL_TTFT_P50),
        p.vector_df(f"histogram_quantile(0.95, sum by (le, target) (increase({c_tt}[{w}])))", at, cl, COL_TTFT_P95),
        # part des tranches de 10 min où plus de 5 % des sondes ont attendu plus de 10 s
        p.vector_df(f"avg_over_time((sum by (target) (rate({c_le10}[10m])) / "
                    f"clamp_min(sum by (target) (rate({c_inf}[10m])), 1e-9) < bool 0.95)[{w}:5m])",
                    at, cl, "% temps dégradé"),
    ]
    if not cparts[0].empty:
        canary = cparts[0]
        for part in cparts[1:]:
            canary = canary.merge(part, on=cl, how="outer")
        canary = canary.rename(columns={"target": "Cible"})
    fast = p.scalar(f"sum(increase({c_le10}[{w}])) / clamp_min(sum(increase({c_inf}[{w}])), 1)", at) \
        if not canary.empty else None
    # Joignabilité vue par Prometheus (up) : une cible qui ne répond pas au scrape est comptée indisponible
    avail = p.vector_df(f'avg by (provider) (avg_over_time(up{{job="vllm"}}[{w}]))', at, lab, "Disponibilité")
    ctt = p.range_df(f"histogram_quantile(0.95, sum by (le, target) (increase({sel('canary_ttft')}[1h])))",
                     start, per.end, 3600, "target")

    if not avail.empty and not df.empty:
        df = df.merge(avail, on="provider", how="left") if "provider" in df else df.merge(
            avail.rename(columns={"provider": "Stack"}), on="Stack", how="left")
    return {
        "table": df,
        "canary": canary,
        "canary_fast": fast,
        "chart_canary": chart_lines(ctt, "Sonde témoin : TTFT p95 horaire d'une petite requête fixe", "secondes"),
        "chart_conc": chart_lines(conc, "Requêtes simultanées (max horaire)", "requêtes"),
        "chart_kv": chart_lines(kv, "Occupation KV cache (max horaire)", "KV cache", pct=True),
    }


# --- collecte Prometheus : écriture et bande passante ----------------------------------
def chart_decode(sat: pd.DataFrame, inter: pd.DataFrame) -> str | None:
    if sat.empty and inter.empty:
        return None
    fig, axes = plt.subplots(1, 2, figsize=(11, 3.6))
    ok = sat[sat[dec.COL_MIN] >= dec.MIN_MINUTES]
    for stack, g in ok.groupby("Stack"):
        axes[0].plot(g["Conversations en cours"].astype(str), g[dec.COL_AGG], marker="o", label=f"{stack} cumulé")
        axes[0].plot(g["Conversations en cours"].astype(str), g[dec.COL_STREAM], marker="o", ls="--",
                     label=f"{stack} par conversation")
    axes[0].set_title("Sans lecture : bande passante partagée", fontsize=10, loc="left")
    axes[0].set_xlabel("conversations en cours")
    axes[0].set_ylabel("tok/s")
    cols = [c for c in inter.columns if c.endswith("(tok/s)")]
    for stack, g in inter.groupby("Stack"):
        for c, ls in zip(cols, ["-", ":", "--"]):
            if g[c].notna().any():
                axes[1].plot(g["Conversations en cours"].astype(str), g[c], marker="o", ls=ls,
                             label=f"{stack} · {c.replace(' (tok/s)', '').lower()}")
    axes[1].set_title("Débit par conversation selon la lecture en cours", fontsize=10, loc="left")
    axes[1].set_xlabel("conversations en cours")
    for ax in axes:
        ax.grid(alpha=0.3)
        ax.legend(fontsize=7)
    fig.tight_layout()
    return fig_to_b64(fig)


def decode_section(p: Prom, per: Period) -> dict:
    """Minutes par stack : débit d'écriture, conversations en cours, lecture calculée, rendement spéculatif."""
    at, w = per.end, per.win
    lab = ["provider"]

    def rng(expr):
        return p.range_df_chunked(expr, per.start, per.end, 60, "provider")

    series = {
        "running": rng(f"sum by (provider) (avg_over_time({sel('running')}[1m]))"),
        "gen": rng(f"sum by (provider) (rate({sel('gtok')}[1m]))"),
        "iter_tok": rng(f"sum by (provider) (rate({sel('iter')}[1m]))"),
        "drafts": rng(f"sum by (provider) (rate({sel('sd_drafts')}[1m]))"),
        "accepted": rng(f"sum by (provider) (rate({sel('sd_acc')}[1m]))"),
    }
    long = []
    for col, df in series.items():
        if df.empty:
            continue
        s_ = df.stack()
        s_.index.names = ["t", "provider"]
        long.append(s_.rename(col))
    empty = {"summary": pd.DataFrame(), "saturation": pd.DataFrame(), "interference": pd.DataFrame(),
             "verdict": None, "chart": None, "minutes": 0}
    if not long or series["gen"].empty or series["running"].empty or series["iter_tok"].empty:
        return empty
    m = pd.concat(long, axis=1).reset_index()
    d = dec.prepare(m)
    if d.empty:
        return empty
    sat, inter = dec.saturation(d), dec.interference(d)

    # récapitulatif par stack sur la période (compteurs exacts, pas des minutes)
    def v(expr, col):
        return p.vector_df(expr, at, lab, col)
    parts = [
        v(f"sum by (provider) (increase({sel('req_gen')}[{w}])) / clamp_min(sum by (provider) (increase({sel('dec_time')}[{w}])), 1e-9)",
          "Débit d'écriture moyen par requête (tok/s)"),
        v(f"sum by (provider) (increase({sel('sd_acc')}[{w}])) / clamp_min(sum by (provider) (increase({sel('sd_dtok')}[{w}])), 1)",
          "% acceptation spéculative"),
        v(f"1 + sum by (provider) (increase({sel('sd_acc')}[{w}])) / clamp_min(sum by (provider) (increase({sel('sd_drafts')}[{w}])), 1)",
          "Tokens par pas (moyenne)"),
    ]
    summ = parts[0]
    for part in parts[1:]:
        summ = summ.merge(part, on=lab, how="outer")
    by = d.groupby("provider")
    extra = pd.DataFrame({
        "Minutes d'écriture": by.size(),
        "Débit par conversation médian (tok/s)": by["per_stream"].median(),
        "% minutes en lecture lourde": by["prefill_lvl"].apply(lambda x: (x == dec.LVL_HEAVY).mean()),
    }).reset_index()
    summ = summ.merge(extra, on="provider", how="outer").rename(columns={"provider": "Stack"})
    pl = dec.plateau(sat)
    summ["Plafond de bande passante (conversations)"] = summ["Stack"].map(lambda s: pl.get(s) or "non atteint")
    return {"summary": summ, "saturation": sat, "interference": inter, "verdict": dec.verdict(d),
            "chart": chart_decode(sat, inter), "minutes": len(d)}


# --- collecte SpendLogs ---------------------------------------------------------------
def _spendlogs_connect() -> tuple[object | None, dict | None]:
    """Connexion à Postgres LiteLLM. Renvoie (connexion, erreur) ; (None, None) si non configuré."""
    host = os.environ.get("LITELLM_PG_HOST")
    if not host:
        return None, None
    try:
        import psycopg
    except ImportError:
        return None, {"error": "psycopg non installé"}
    h, _, port = host.partition(":")
    try:
        conn = psycopg.connect(host=h, port=int(port or 5432), dbname=os.environ.get("LITELLM_PG_DB", "litellm"),
                               user=os.environ.get("LITELLM_PG_USER"), password=os.environ.get("LITELLM_PG_PASSWORD"),
                               sslmode=os.environ.get("LITELLM_PG_SSLMODE", "prefer"), connect_timeout=10)
    except Exception as e:  # noqa: BLE001
        return None, {"error": f"connexion Postgres impossible : {e}"}
    return conn, None


def spendlogs_section(per: Period, anon: Anon) -> dict | None:
    conn, err = _spendlogs_connect()
    if err:
        return err
    if conn is None:
        return None

    with conn:
        cols = {r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'LiteLLM_SpendLogs'").fetchall()}
        team_id = "COALESCE(s.team_id, k.team_id)" if "team_id" in cols else "k.team_id"
        status = 's.status' if "status" in cols else "NULL"
        q = f'''
        SELECT s."startTime" AS start, s."endTime" AS end_, s."completionStartTime" AS first_tok,
               s.prompt_tokens, s.completion_tokens, s.api_base, s.model_group, s.api_key AS key_id,
               COALESCE(t.team_alias, {team_id}, '(sans team)') AS team,
               COALESCE(u.user_email, NULLIF(NULLIF(s."user", ''), 'None'), '{AUCUN}') AS user_,
               COALESCE(k.key_alias, '(sans alias)') AS key_alias,
               {status} AS status
        FROM "LiteLLM_SpendLogs" s
        LEFT JOIN "LiteLLM_VerificationToken" k ON k.token = s.api_key
        LEFT JOIN "LiteLLM_TeamTable" t ON t.team_id = {team_id}
        LEFT JOIN "LiteLLM_UserTable" u ON u.user_id = s."user"
        WHERE s."startTime" >= %s AND s."startTime" < %s'''
        try:
            cur = conn.execute(q, (per.start, per.end))
            df = pd.DataFrame(cur.fetchall(), columns=[d.name for d in cur.description])
        except Exception as e:  # noqa: BLE001
            return {"error": f"requête SpendLogs en échec : {e}"}

    if df.empty:
        return {"error": "aucune ligne dans LiteLLM_SpendLogs sur la période"}

    # monitoring (équipe santé interne LiteLLM, clé de la sonde) : exclu des totaux d'usage,
    # mais conservé pour l'analyse causale — la sonde est une « petite requête » idéale pour le test
    monitoring = pd.Series(False, index=df.index)
    for col, excl_ in (("team", EXCLUDE_TEAMS), ("key_alias", EXCLUDE_KEY_ALIASES)):
        if excl_:
            monitoring |= df[col].isin(excl_)

    for c in ("start", "end_", "first_tok"):
        df[c] = pd.to_datetime(df[c], utc=True).dt.tz_convert(TZ)
    df["user_"] = df["user_"].map(anon)
    df["key_alias"] = df["key_alias"].map(anon.keys)
    df["api_base"] = df["api_base"].fillna("").str.rstrip("/")  # « …/v1/ » (non streamé) = « …/v1 » (streamé)
    df["dur"] = (df["end_"] - df["start"]).dt.total_seconds()
    ttft = (df["first_tok"] - df["start"]).dt.total_seconds()
    df["ttft"] = ttft.where((df["first_tok"] > df["start"]) & (df["first_tok"] < df["end_"]))
    df["tpot"] = ((df["end_"] - df["first_tok"]).dt.total_seconds() / df["completion_tokens"].clip(lower=1)).where(df["ttft"].notna())
    usage = df[~monitoring]  # périmètre « usage réel » pour les totaux et la concurrence

    def agg(frame: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
        g = frame.groupby(keys, dropna=False)
        r = pd.DataFrame({
            "Requêtes": g.size(),
            COL_TOKENS_IN: g["prompt_tokens"].sum(),
            COL_TOKENS_OUT: g["completion_tokens"].sum(),
            "Prompt médian": g["prompt_tokens"].median(),
            "Prompt p95": g["prompt_tokens"].quantile(0.95),
            COL_TTFT_P50: g["ttft"].median(),
            COL_TTFT_P95: g["ttft"].quantile(0.95),
            "Temps/token p95 (s)": g["tpot"].quantile(0.95),
            "Durée p95 (s)": g["dur"].quantile(0.95),
        }).reset_index()
        return r.sort_values("Requêtes", ascending=False)

    by_team = agg(usage, ["team"]).rename(columns={"team": "Team"})
    by_team.insert(2, "Utilisateurs",
                   usage[usage["user_"] != AUCUN].groupby("team")["user_"].nunique().reindex(by_team["Team"]).values)
    by_user = agg(usage, ["team", "user_", "key_alias"]).rename(columns={"team": "Team", "user_": "Utilisateur", "key_alias": "Clé"})
    by_base = agg(usage, ["api_base"]).rename(columns={"api_base": "Stack (api_base)"})
    by_base.insert(2, "Part", by_base["Requêtes"] / by_base["Requêtes"].sum())
    by_model = agg(usage.assign(model_group=usage["model_group"].fillna("(inconnu)")), ["model_group"]).rename(
        columns={"model_group": "Modèle"})
    by_model.insert(2, "Part", by_model["Requêtes"] / by_model["Requêtes"].sum())
    by_model.insert(3, "Utilisateurs", usage[usage["user_"] != AUCUN].assign(
        model_group=usage["model_group"].fillna("(inconnu)")).groupby("model_group")["user_"].nunique()
        .reindex(by_model["Modèle"]).fillna(0).astype(int).values)
    # utilisateurs réguliers : actifs au moins un jour par semaine en moyenne sur la période
    days = usage[usage["user_"] != AUCUN].groupby("user_")["start"].apply(lambda x: x.dt.date.nunique())
    regular = int((days >= max(2, round((per.end - per.start).days / 7))).sum())

    # concurrence réelle reconstituée à partir des intervalles [start, end] (hors monitoring)
    ev = pd.concat([pd.DataFrame({"t": usage["start"], "d": 1}), pd.DataFrame({"t": usage["end_"], "d": -1})])
    ev = ev.sort_values(["t", "d"]).reset_index(drop=True)
    ev["c"] = ev["d"].cumsum()
    conc_5m = ev.set_index("t")["c"].resample("5min").max().ffill()
    per_min = hyp.per_minute_level(ev)
    per_min = per_min[per_min > 0]  # minutes avec au moins une requête en cours
    conc = {
        "max": int(ev["c"].max()),
        "p99_min": float(per_min.quantile(0.99)) if len(per_min) else None,
        "p95_min": float(per_min.quantile(0.95)) if len(per_min) else None,
    }
    peak_t = ev.loc[ev["c"].idxmax(), "t"]
    conc["peak_at"] = f"{peak_t:%d/%m %H:%M}"
    daily_conc = conc_5m.resample("1D").max().to_frame("concurrence max")

    df["prompt_tokens"] = df["prompt_tokens"].fillna(0).astype(int)
    cont_table, cont_verdict = hyp.contention(df, SMALL_PROMPT, LARGE_PROMPT)
    small_ttft = df.loc[df["prompt_tokens"] < SMALL_PROMPT, "ttft"]
    # écriture par requête : circonstances calculées sur tout le trafic, débit mesuré sur l'usage réel
    rq = dec.request_conditions(df)
    rq_tables = dec.request_tables(rq[rq.index.isin(usage.index)])

    out = {
        "n": len(usage), "by_team": by_team, "by_user": by_user, "by_base": by_base, "conc": conc,
        "by_size": hyp.ttft_by_bucket(usage, SMALL_PROMPT, LARGE_PROMPT),  # hors sonde et health-check
        "by_model": by_model, "regular_share": regular / len(days) if len(days) else None,
        "contention": cont_table, "verdict": cont_verdict,
        "rq_tables": rq_tables, "rq_verdict": dec.request_verdict(rq_tables),
        "period_from": df["start"].min(), "period_to": df["start"].max(),
        "small_ttft_p95": small_ttft.quantile(0.95) if small_ttft.notna().any() else None,
        "key_conc": hyp.key_concurrency(usage),
        "top": hyp.top_prompts(df),
        "chart_conc": chart_lines(daily_conc, "Concurrence max par jour (gateway, exacte)", "requêtes simultanées", kind="bar"),
        "ttft_global_p95": usage["ttft"].quantile(0.95),
        "fail_rate": (usage["status"].eq("failure").mean() if usage["status"].notna().any() else None),
    }
    return out


# --- diagnostic -----------------------------------------------------------------------
def diagnose(eng: dict, gw: dict, sl: dict | None) -> list[dict]:
    notes = []
    if sl and sl.get("verdict"):
        notes.append({**sl["verdict"], "stack": "hypothèse"})
    small_ttft = (sl or {}).get("small_ttft_p95")
    t = eng.get("table")
    if t is not None and not t.empty:
        for _, r in t.iterrows():
            s = r["Stack"]
            g = lambda k: (None if pd.isna(r.get(k)) else r.get(k))  # noqa: E731
            wait, kvp, kv90, pre = g("% temps file"), g("KV p95"), g("% temps KV>90%"), g("Préemptions")
            ttft, itl, hit = g(COL_TTFT_P95), g("ITL p95 (s)"), g("Hit prefix")
            sat = False
            if wait is not None and wait > 0.05 and (kvp is None or kvp < 0.8):
                sat = True
                notes.append({"lvl": "warn", "stack": s, "txt":
                    f"File d'attente présente {wait:.0%} du temps alors que le KV cache reste modéré (p95 {kvp or 0:.0%}) : "
                    "les places (--max-num-seqs) sont occupées. Avant d'en ajouter, vérifier qui les occupe (concurrence "
                    "par clé) et si de longues lectures les monopolisent : sur GB10, plus de places dégrade l'ITL de tous."})
            if (kv90 is not None and kv90 > 0.05) or (pre is not None and pre > 0):
                sat = True
                notes.append({"lvl": "warn", "stack": s, "txt":
                    f"Pression mémoire : KV > 90 % pendant {(kv90 or 0):.0%} du temps, {int(pre or 0)} préemption(s). "
                    "Pistes : réduire --max-model-len si inutile, KV cache FP8, ou limiter la concurrence."})
            # Le TTFT global mélange gros et petits prompts : on juge sur les petits (SpendLogs) quand c'est possible
            ref, ref_lbl = (small_ttft, f"des requêtes < {SMALL_PROMPT // 1000}k (global, gateway)") \
                if small_ttft is not None else (ttft, "global (inclut les gros prompts)")
            if ref is not None and ref > SLO_TTFT and (itl is None or itl <= SLO_ITL):
                sat = True
                notes.append({"lvl": "warn", "stack": s, "txt":
                    f"TTFT p95 {ref_lbl} {ref:.2f}s > SLO {SLO_TTFT}s avec un ITL correct : goulot au prefill. "
                    "Pistes : vérifier le prefix caching et l'affinité de routage, --long-prefill-token-threshold, "
                    "limiter les très gros prompts."})
            if itl is not None and itl > SLO_ITL:
                pic = g("Pic en cours")
                if pic is not None and pic < CONC_ITL_MIN:
                    # jamais plus de quelques requêtes en cours : ce n'est pas un batch trop gros,
                    # c'est la vitesse de décodage du modèle (bande passante mémoire), cf. GB10
                    notes.append({"lvl": "info", "stack": s, "txt":
                        f"ITL p95 {itl*1000:.0f} ms > SLO {SLO_ITL*1000:.0f} ms, mais la concurrence reste faible "
                        f"(pic {pic:.0f} requête(s) en cours) : plafond de décodage du modèle, pas un batch trop gros. "
                        "Relever SLO_ITL_P95 dans .env (ex. 0.2) pour refléter ce modèle sur GB10."})
                else:
                    sat = True
                    conc = f"avec {pic:.0f} requête(s) en cours" if pic is not None else "avec une concurrence élevée ou inconnue"
                    notes.append({"lvl": "warn", "stack": s, "txt":
                        f"ITL p95 {itl*1000:.0f} ms > SLO {SLO_ITL*1000:.0f} ms ({conc}) : "
                        "batch de décodage trop gros pour la bande passante mémoire. "
                        "Piste : baisser --max-num-seqs pour protéger la latence, ou plafonner la concurrence."})
            if hit is not None and hit < 0.3:
                notes.append({"lvl": "info", "stack": s, "txt":
                    f"Hit rate du prefix cache faible ({hit:.0%}) : vérifier l'affinité de routage LiteLLM et la stabilité des prompts système."})
            if not sat:
                notes.append({"lvl": "ok", "stack": s, "txt": "Aucun signal de saturation sur la période selon les seuils configurés."})
        if "Part requêtes" in t and t["Part requêtes"].notna().any() and t["Part requêtes"].max() > 0.65:
            notes.append({"lvl": "info", "stack": "routage", "txt":
                f"Répartition déséquilibrée entre stacks (max {t['Part requêtes'].max():.0%}) : vérifier la stratégie de routage LiteLLM."})

    users = (sl or {}).get("by_user") if sl and "by_user" in sl else gw.get("by_user")
    if users is not None and not users.empty and "Utilisateur" in users:
        users = users[users["Utilisateur"] != AUCUN]  # trafic non rattaché (Open WebUI…) : pas une personne
    if users is not None and not users.empty:
        tot = users[COL_TOKENS_IN].fillna(0) + users[COL_TOKENS_OUT].fillna(0)
        if tot.sum() > 0:
            share = tot.max() / tot.sum()
            who = users.iloc[int(tot.values.argmax())]
            if share > 0.4:
                # saturation des stacks seulement : le verdict de l'hypothèse n'est pas un signal de saturation
                any_sat = any(n_["lvl"] == "warn" and n_["stack"] not in ("hypothèse", "écriture") for n_ in notes)
                notes.append({"lvl": "warn" if any_sat else "info", "stack": "global", "txt":
                    f"{who.get('Utilisateur', '?')} ({who.get('Clé', '?')}) concentre {share:.0%} des tokens. "
                    + ("Combiné aux signaux de saturation : envisager une limite par clé (max_parallel_requests / tpm_limit dans LiteLLM)."
                       if any_sat else "Sans saturation, pas d'action nécessaire ; à surveiller.")})
    return notes


# --- synthèse direction -------------------------------------------------------------------
def synth_context(gw: dict, eng: dict, dcd: dict, sl: dict | None) -> dict:
    """Rassemble les chiffres utiles à la synthèse (voir synthese.py). Une valeur absente reste None."""
    k, kp = gw["kpi"], gw["kpi_prev"]

    def success(kk):
        # compteur d'échecs absent = métrique inconnue, pas « zéro échec » : SpendLogs prendra le relais
        return 1 - kk["failures"] / kk["requests"] if kk.get("requests") and kk.get("failures") is not None else None

    c = {"users": k["users"], "users_prev": kp["users"], "requests": k["requests"], "requests_prev": kp["requests"],
         "new_users": gw.get("new_users"), "target_users": TARGET_USERS, "peak": gw.get("peak"),
         "success_rate": success(k), "success_rate_prev": success(kp), "canary_fast_share": eng.get("canary_fast")}
    bt = gw["by_team"]
    if not bt.empty and len(bt) > 1 and bt["Requêtes"].sum() > 0:
        c["top_team"] = bt.iloc[0]["Team"] or "sans équipe"
        c["top_team_share"] = bt.iloc[0]["Requêtes"] / bt["Requêtes"].sum()
    t = eng["table"]
    if not t.empty:
        if "Disponibilité" in t and t["Disponibilité"].notna().any():
            i = t["Disponibilité"].idxmin()
            c["availability"], c["availability_stack"] = t.at[i, "Disponibilité"], t.at[i, "Stack"]
        if t["% temps file"].notna().any():
            i = t["% temps file"].idxmax()
            c["queue_share"] = t.at[i, "% temps file"]
            c["queue_stack"] = t.at[i, "Stack"] if len(t) > 1 else None
        if t["Pic en cours"].notna().any():
            c["conc_max"] = t["Pic en cours"].max()
    summ = dcd["summary"]
    if not summ.empty:
        col = "Débit par conversation médian (tok/s)"
        if col in summ and summ[col].notna().any():
            c["write_speed"] = float(summ[col].median())
        pl = [int(x) for x in summ["Plafond de bande passante (conversations)"] if str(x).isdigit()]
        c["plateau"] = min(pl) if pl else None
    if sl and "error" not in sl:
        c["conc_max"] = sl["conc"]["max"]  # exact (gateway) plutôt qu'échantillonné
        c["regular_share"] = sl["regular_share"]
        c["small_ttft_p95"] = sl["small_ttft_p95"]
        c["hyp"] = sl["verdict"]
        if c["success_rate"] is None and sl.get("fail_rate") is not None:
            c["success_rate"] = 1 - sl["fail_rate"]
    return c


def coverage_note(gw: dict) -> str | None:
    cov, prev = gw.get("coverage"), gw.get("coverage_prev")
    notes = []
    if cov is not None and cov < 0.95:
        notes.append(f"les mesures couvrent {cov:.0%} de la période (interruptions de collecte)")
    if (prev or 0) < 0.9:
        notes.append("pas de comparaison avec la période précédente : son historique est incomplet")
    return ("Limites : " + " ; ".join(notes) + ".") if notes else None


# --- rendu ------------------------------------------------------------------------------
def fmt_num(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "–"
    v = float(v)
    if abs(v) >= 1e6:
        return f"{v/1e6:.2f} M".replace(".", ",")
    if abs(v) >= 1e4:
        return f"{v/1e3:.1f} k".replace(".", ",")
    return f"{v:,.0f}".replace(",", "\u202f")


def df_html(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return "<p class='muted'>Aucune donnée.</p>"
    d = df.copy()
    for c in d.columns:
        if c.startswith("%") or c.startswith("Part") or c in ("KV p95", "Hit prefix", "Disponibilité"):
            d[c] = d[c].map(lambda x: "–" if pd.isna(x) else f"{x:.1%}".replace(".", ","))
        elif "par pas" in c:
            d[c] = d[c].map(lambda x: "–" if pd.isna(x) else f"{x:.1f}".replace(".", ","))
        elif "(s)" in c:
            d[c] = d[c].map(lambda x: "–" if pd.isna(x) else (f"{x*1000:.0f} ms" if x < 1 else f"{x:.2f} s".replace(".", ",")))
        elif pd.api.types.is_numeric_dtype(d[c]):
            d[c] = d[c].map(fmt_num)
    return d.to_html(index=False, classes="tbl", border=0, na_rep="–")


def write_csv(per: Period, name: str, df: pd.DataFrame | None):
    if df is None or df.empty:
        return
    d = OUT_DIR / f"csv_{per.slug}"
    d.mkdir(parents=True, exist_ok=True)
    df.to_csv(d / f"{name}.csv", index=isinstance(df.index, pd.DatetimeIndex), encoding="utf-8-sig", sep=";", decimal=",")


def _kpis(gw: dict, sl: dict | None) -> list[tuple[str, str]]:
    """Lignes d'indicateurs affichées en tête du rapport."""
    k = gw["kpi"]
    kpis = [
        ("Requêtes", fmt_num(k["requests"])),
        ("Taux d'échec", "–" if not k["requests"] or k["failures"] is None
         else f"{k['failures'] / k['requests']:.1%}".replace(".", ",")),
        ("Tokens entrée", fmt_num(k["tin"])),
        ("Tokens sortie", fmt_num(k["tout"])),
        ("Utilisateurs actifs", fmt_num(k["users"])),
        ("TTFT p50 / p95 (toutes tailles)", " / ".join("–" if x is None else f"{x:.2f}s".replace(".", ",") for x in (k["ttft_p50"], k["ttft_p95"]))),
        ("Latence totale p95", "–" if k["lat_p95"] is None else f"{k['lat_p95']:.1f}s".replace(".", ",")),
        ("Overhead LiteLLM p95", "–" if k["ovh_p95"] is None else f"{k['ovh_p95']*1000:.0f} ms"),
    ]
    if sl and "conc" in sl:
        c = sl["conc"]
        kpis.append(("Concurrence max (exacte)", f"{c['max']} ({c['peak_at']})"))
        kpis.append(("Concurrence p95 / min", fmt_num(c["p95_min"])))
        if sl.get("small_ttft_p95") is not None and not pd.isna(sl["small_ttft_p95"]):
            kpis.append((f"TTFT p95 requêtes < {SMALL_PROMPT // 1000}k", f"{sl['small_ttft_p95']:.2f}s".replace(".", ",")))
        if sl.get("fail_rate") is not None:
            kpis.append(("Taux d'échec (SpendLogs)", f"{sl['fail_rate']:.1%}".replace(".", ",")))
    return kpis


def _sl_context(sl: dict | None) -> dict | None:
    """Contexte SpendLogs transmis au template (erreur, absence ou tableaux rendus)."""
    if sl is None:
        return None
    if "error" in sl:
        return {"error": sl["error"]}
    return {
        "n": fmt_num(sl["n"]), "team": df_html(sl["by_team"]), "user": df_html(sl["by_user"]),
        "base": df_html(sl["by_base"]), "model": df_html(sl["by_model"]), "chart_conc": sl["chart_conc"],
        "by_size": df_html(sl["by_size"]), "contention": df_html(sl["contention"]), "verdict": sl["verdict"],
        "key_conc": df_html(sl["key_conc"]), "top": df_html(sl["top"]),
        "rq": {k: df_html(v) for k, v in sl["rq_tables"].items()}, "rq_verdict": sl["rq_verdict"],
        "rq_from": f"{sl['period_from']:%d/%m/%Y}", "rq_to": f"{sl['period_to']:%d/%m/%Y}"}


def build(per: Period) -> Path:
    p = Prom(PROM_URL)
    anon = Anon(ANONYMIZE)
    anon.keys = Anon(ANONYMIZE, prefix="clé")  # les alias de clé contiennent souvent un nom
    print(f"Période : {per.label}")
    print("· gateway LiteLLM…")
    gw = gateway_section(p, per, anon)
    print("· moteurs vLLM…")
    eng = engine_section(p, per)
    print("· écriture et bande passante…")
    dcd = decode_section(p, per)
    print("· SpendLogs…")
    sl = spendlogs_section(per, anon)
    notes = diagnose(eng, gw, sl if sl and "error" not in sl else None)
    if dcd["verdict"]:
        notes.insert(0, {**dcd["verdict"], "stack": "écriture"})
    if sl and "error" not in sl and sl.get("rq_verdict"):
        notes.insert(0, {**sl["rq_verdict"], "stack": "écriture"})

    kpis = _kpis(gw, sl)
    synth = syn.build(synth_context(gw, eng, dcd, sl))

    env = Environment(loader=FileSystemLoader(Path(__file__).parent), autoescape=select_autoescape(["html"]))
    html = env.get_template("template.html.j2").render(
        period=per.label, generated=datetime.now(TZ).strftime("%d/%m/%Y %H:%M"),
        kpis=kpis, notes=notes, synth=synth, glossaire=syn.GLOSSAIRE, coverage=coverage_note(gw), slo_ttft=SLO_TTFT, slo_itl=SLO_ITL, anonymized=ANONYMIZE,
        gw={"team": df_html(gw["by_team"]), "user": df_html(gw["by_user"]),
            "chart_daily": gw["chart_daily"], "chart_heatmap": gw["chart_heatmap"]},
        eng={"table": df_html(eng["table"]), "chart_conc": eng["chart_conc"], "chart_kv": eng["chart_kv"],
             "canary": df_html(eng["canary"]) if not eng["canary"].empty else None, "chart_canary": eng["chart_canary"]},
        dcd={"summary": df_html(dcd["summary"]), "saturation": df_html(dcd["saturation"]),
             "interference": df_html(dcd["interference"]), "chart": dcd["chart"], "minutes": dcd["minutes"],
             "verdict": dcd["verdict"], "light": dec.PREFILL_LIGHT, "heavy": dec.PREFILL_HEAVY},
        small=SMALL_PROMPT, large=LARGE_PROMPT,
        sl=_sl_context(sl),
    )
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"rapport-llm_{per.slug}.html"
    out.write_text(html, encoding="utf-8")

    write_csv(per, "gateway_par_team", gw["by_team"])
    write_csv(per, "gateway_par_utilisateur", gw["by_user"])
    write_csv(per, "gateway_requetes_horaires", gw["hourly_csv"])
    write_csv(per, "moteurs_par_stack", eng["table"])
    write_csv(per, "ecriture_par_stack", dcd["summary"])
    write_csv(per, "ecriture_bande_passante", dcd["saturation"])
    write_csv(per, "ecriture_interference_lecture", dcd["interference"])
    if sl and "error" not in sl:
        write_csv(per, "spendlogs_par_team", sl["by_team"])
        write_csv(per, "spendlogs_par_utilisateur", sl["by_user"])
        write_csv(per, "spendlogs_par_stack", sl["by_base"])
        write_csv(per, "spendlogs_par_modele", sl["by_model"])
        write_csv(per, "hypothese_ttft_par_taille", sl["by_size"])
        write_csv(per, "hypothese_petites_requetes", sl["contention"])
        write_csv(per, "concurrence_par_cle", sl["key_conc"])
        write_csv(per, "plus_gros_prompts", sl["top"])
        for name, tab in sl["rq_tables"].items():
            write_csv(per, f"ecriture_par_requete_{name}", tab)
    if not eng["canary"].empty:
        write_csv(per, "sonde_temoin", eng["canary"])
    print(f"✔ {out}")
    return out


# --- check ----------------------------------------------------------------------------
def _check_up(p: Prom) -> bool:
    """Affiche l'état des cibles Prometheus et renvoie True si toutes sont UP."""
    ok = True
    for r in p.instant('up{job=~"litellm|vllm|canary"}', datetime.now(TZ)):
        m = r["metric"]
        state = "UP " if r["value"][1] == "1" else "DOWN"
        ok &= state == "UP "
        print(f"  [{state}] {m.get('job'):8} {m.get('instance'):24} {m.get('provider', '')}")
    return ok


def _check_metrics(p: Prom) -> bool:
    """Affiche la présence des métriques attendues et renvoie True si rien d'obligatoire ne manque."""
    names = p.names()
    ok = True
    print("\nMétriques attendues :")
    for key, rx in M.items():
        found = [nm for nm in names if re.fullmatch(rx, nm)]
        if found:
            mark = "✔"
        elif key in OPTIONAL:
            mark = "–"
        else:
            mark = "✘"
        ok &= bool(found) or key in OPTIONAL
        print(f"  {mark} {key:8} {', '.join(found) or '(absente : ' + rx + ')'}")
    return ok


def check() -> int:
    p = Prom(PROM_URL)
    print(f"Prometheus : {PROM_URL}")
    ok = _check_up(p)
    ok &= _check_metrics(p)
    print(f"\nExclu des KPI (hors usage réel) : teams={EXCLUDE_TEAMS or 'aucune'} "
          f"alias de clés={EXCLUDE_KEY_ALIASES or 'aucun'}")
    sample = p.instant(f"count by (team_alias, user, api_key_alias) ({sel('req')})", datetime.now(TZ))
    print(f"\nCombinaisons team/user/clé vues par Prometheus : {len(sample)}")
    for r in sample[:10]:
        m = r["metric"]
        print(f"  team={m.get('team_alias', '')!r} user={m.get('user', '')!r} key={m.get('api_key_alias', '')!r}")
    print("\n" + ("Tout est en place." if ok else "Des éléments manquent (voir ✘ / DOWN). Les sections concernées du rapport seront vides."))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", nargs="?", default="report", choices=["report", "check", "export"])
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--previous-month", action="store_true")
    ap.add_argument("--seed", type=int, help="export : graine de l'ordre des aspects dans prompt.md")
    a = ap.parse_args()
    if a.cmd == "check":
        sys.exit(check())
    if a.cmd == "export":
        import export  # données brutes pour une analyse par IA, sans verdict
        export.run(parse_period(a), a.seed)
        return
    build(parse_period(a))


if __name__ == "__main__":
    main()
