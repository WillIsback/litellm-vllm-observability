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

TZ = ZoneInfo(os.environ.get("TZ", "Europe/Paris"))
PROM_URL = os.environ.get("PROM_URL", "http://localhost:9090").rstrip("/")
OUT_DIR = Path(os.environ.get("REPORT_DIR", "/reports"))
SLO_TTFT = float(os.environ.get("SLO_TTFT_P95", "2.0"))
SLO_ITL = float(os.environ.get("SLO_ITL_P95", "0.10"))
ANONYMIZE = os.environ.get("ANONYMIZE_USERS", "false").lower() in ("1", "true", "yes")

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
}


def sel(key: str, extra: str = "") -> str:
    return '{__name__=~"%s"%s}' % (M[key], ("," + extra) if extra else "")


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
def gateway_section(p: Prom, per: Period, anon: Anon) -> dict:
    at, w = per.end, per.win
    out: dict = {}
    req, fail = sel("req"), sel("fail")
    out["kpi"] = {
        "requests": p.scalar(f"sum(increase({req}[{w}]))", at),
        "failures": p.scalar(f"sum(increase({fail}[{w}]))", at),
        "tin": p.scalar(f"sum(increase({sel('tin')}[{w}]))", at),
        "tout": p.scalar(f"sum(increase({sel('tout')}[{w}]))", at),
        "users": p.scalar(f"count(sum by (user) (increase({req}[{w}])) > 0)", at),
        "ttft_p50": p.scalar(f"histogram_quantile(0.5, sum by (le) (increase({sel('ttft')}[{w}])))", at),
        "ttft_p95": p.scalar(f"histogram_quantile(0.95, sum by (le) (increase({sel('ttft')}[{w}])))", at),
        "lat_p95": p.scalar(f"histogram_quantile(0.95, sum by (le) (increase({sel('lat')}[{w}])))", at),
        "ovh_p95": p.scalar(f"histogram_quantile(0.95, sum by (le) (increase({sel('ovh')}[{w}])))", at),
    }

    def by(labels: list[str]) -> pd.DataFrame:
        g = ", ".join(labels)
        parts = [
            p.vector_df(f"sum by ({g}) (increase({req}[{w}]))", at, labels, "Requêtes"),
            p.vector_df(f"sum by ({g}) (increase({fail}[{w}]))", at, labels, "Échecs"),
            p.vector_df(f"sum by ({g}) (increase({sel('tin')}[{w}]))", at, labels, "Tokens in"),
            p.vector_df(f"sum by ({g}) (increase({sel('tout')}[{w}]))", at, labels, "Tokens out"),
            p.vector_df(f"histogram_quantile(0.95, sum by (le, {g}) (increase({sel('ttft')}[{w}])))", at, labels, "TTFT p95 (s)"),
            p.vector_df(f"histogram_quantile(0.95, sum by (le, {g}) (increase({sel('lat')}[{w}])))", at, labels, "Latence p95 (s)"),
        ]
        df = parts[0]
        for part in parts[1:]:
            df = df.merge(part, on=labels, how="outer")
        if df.empty:
            return df
        if "user" in df:
            df["user"] = df["user"].map(anon)
        if "api_key_alias" in df:
            df["api_key_alias"] = df["api_key_alias"].map(anon.keys)
        tot = df["Tokens in"].fillna(0) + df["Tokens out"].fillna(0)
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
        v(hq("0.5", "vttft"), "TTFT p50 (s)"),
        v(hq("0.95", "vttft"), "TTFT p95 (s)"),
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
    return {
        "table": df,
        "chart_conc": chart_lines(conc, "Requêtes simultanées (max horaire)", "requêtes"),
        "chart_kv": chart_lines(kv, "Occupation KV cache (max horaire)", "KV cache", pct=True),
    }


# --- collecte SpendLogs ---------------------------------------------------------------
def spendlogs_section(per: Period, anon: Anon) -> dict | None:
    host = os.environ.get("LITELLM_PG_HOST")
    if not host:
        return None
    try:
        import psycopg
    except ImportError:
        return {"error": "psycopg non installé"}
    h, _, port = host.partition(":")
    try:
        conn = psycopg.connect(host=h, port=int(port or 5432), dbname=os.environ.get("LITELLM_PG_DB", "litellm"),
                               user=os.environ.get("LITELLM_PG_USER"), password=os.environ.get("LITELLM_PG_PASSWORD"),
                               sslmode=os.environ.get("LITELLM_PG_SSLMODE", "prefer"), connect_timeout=10)
    except Exception as e:  # noqa: BLE001
        return {"error": f"connexion Postgres impossible : {e}"}

    with conn:
        cols = {r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'LiteLLM_SpendLogs'").fetchall()}
        team_id = "COALESCE(s.team_id, k.team_id)" if "team_id" in cols else "k.team_id"
        status = 's.status' if "status" in cols else "NULL"
        q = f'''
        SELECT s."startTime" AS start, s."endTime" AS end_, s."completionStartTime" AS first_tok,
               s.prompt_tokens, s.completion_tokens, s.api_base, s.model_group,
               COALESCE(t.team_alias, {team_id}, '(sans team)') AS team,
               COALESCE(u.user_email, NULLIF(s."user", ''), '(inconnu)') AS user_,
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

    for c in ("start", "end_", "first_tok"):
        df[c] = pd.to_datetime(df[c], utc=True).dt.tz_convert(TZ)
    df["user_"] = df["user_"].map(anon)
    df["key_alias"] = df["key_alias"].map(anon.keys)
    df["dur"] = (df["end_"] - df["start"]).dt.total_seconds()
    ttft = (df["first_tok"] - df["start"]).dt.total_seconds()
    df["ttft"] = ttft.where((df["first_tok"] > df["start"]) & (df["first_tok"] < df["end_"]))
    df["tpot"] = ((df["end_"] - df["first_tok"]).dt.total_seconds() / df["completion_tokens"].clip(lower=1)).where(df["ttft"].notna())

    def agg(keys):
        g = df.groupby(keys, dropna=False)
        r = pd.DataFrame({
            "Requêtes": g.size(),
            "Tokens in": g["prompt_tokens"].sum(),
            "Tokens out": g["completion_tokens"].sum(),
            "Prompt médian": g["prompt_tokens"].median(),
            "Prompt p95": g["prompt_tokens"].quantile(0.95),
            "TTFT p50 (s)": g["ttft"].median(),
            "TTFT p95 (s)": g["ttft"].quantile(0.95),
            "Temps/token p95 (s)": g["tpot"].quantile(0.95),
            "Durée p95 (s)": g["dur"].quantile(0.95),
        }).reset_index()
        return r.sort_values("Requêtes", ascending=False)

    by_team = agg(["team"]).rename(columns={"team": "Team"})
    by_team.insert(2, "Utilisateurs", df.groupby("team")["user_"].nunique().reindex(by_team["Team"]).values)
    by_user = agg(["team", "user_", "key_alias"]).rename(columns={"team": "Team", "user_": "Utilisateur", "key_alias": "Clé"})
    by_base = agg(["api_base"]).rename(columns={"api_base": "Stack (api_base)"})
    by_base.insert(2, "Part", by_base["Requêtes"] / by_base["Requêtes"].sum())

    # concurrence réelle reconstituée à partir des intervalles [start, end]
    ev = pd.concat([pd.DataFrame({"t": df["start"], "d": 1}), pd.DataFrame({"t": df["end_"], "d": -1})])
    ev = ev.sort_values(["t", "d"]).reset_index(drop=True)
    ev["c"] = ev["d"].cumsum()
    conc_5m = ev.set_index("t")["c"].resample("5min").max().ffill()
    per_min = ev.set_index("t")["c"].resample("1min").max().dropna()
    conc = {
        "max": int(ev["c"].max()),
        "p99_min": float(per_min.quantile(0.99)) if len(per_min) else None,
        "p95_min": float(per_min.quantile(0.95)) if len(per_min) else None,
    }
    peak_t = ev.loc[ev["c"].idxmax(), "t"]
    conc["peak_at"] = f"{peak_t:%d/%m %H:%M}"
    daily_conc = conc_5m.resample("1D").max().to_frame("concurrence max")

    out = {
        "n": len(df), "by_team": by_team, "by_user": by_user, "by_base": by_base, "conc": conc,
        "chart_conc": chart_lines(daily_conc, "Concurrence max par jour (gateway, exacte)", "requêtes simultanées", kind="bar"),
        "ttft_global_p95": df["ttft"].quantile(0.95),
        "fail_rate": (df["status"].eq("failure").mean() if df["status"].notna().any() else None),
    }
    return out


# --- diagnostic -----------------------------------------------------------------------
def diagnose(eng: dict, gw: dict, sl: dict | None) -> list[dict]:
    notes = []
    t = eng.get("table")
    if t is not None and not t.empty:
        for _, r in t.iterrows():
            s = r["Stack"]
            g = lambda k: (None if pd.isna(r.get(k)) else r.get(k))  # noqa: E731
            wait, kvp, kv90, pre = g("% temps file"), g("KV p95"), g("% temps KV>90%"), g("Préemptions")
            ttft, itl, hit = g("TTFT p95 (s)"), g("ITL p95 (s)"), g("Hit prefix")
            sat = False
            if wait is not None and wait > 0.05 and (kvp is None or kvp < 0.8):
                sat = True
                notes.append({"lvl": "warn", "stack": s, "txt":
                    f"File d'attente présente {wait:.0%} du temps alors que le KV cache reste modéré (p95 {kvp or 0:.0%}) : "
                    "le scheduler limite. Piste : augmenter --max-num-seqs / --max-num-batched-tokens, puis revérifier l'ITL."})
            if (kv90 is not None and kv90 > 0.05) or (pre is not None and pre > 0):
                sat = True
                notes.append({"lvl": "warn", "stack": s, "txt":
                    f"Pression mémoire : KV > 90 % pendant {(kv90 or 0):.0%} du temps, {int(pre or 0)} préemption(s). "
                    "Pistes : réduire --max-model-len si inutile, KV cache FP8, ou limiter la concurrence."})
            if ttft is not None and ttft > SLO_TTFT and (itl is None or itl <= SLO_ITL):
                sat = True
                notes.append({"lvl": "warn", "stack": s, "txt":
                    f"TTFT p95 {ttft:.2f}s > SLO {SLO_TTFT}s avec un ITL correct : goulot au prefill. "
                    "Pistes : chunked prefill / --max-num-batched-tokens, vérifier le prefix caching, limiter les très gros prompts."})
            if itl is not None and itl > SLO_ITL:
                sat = True
                notes.append({"lvl": "warn", "stack": s, "txt":
                    f"ITL p95 {itl*1000:.0f} ms > SLO {SLO_ITL*1000:.0f} ms : batch de décodage trop gros pour la bande passante mémoire. "
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
    if users is not None and not users.empty:
        tot = users["Tokens in"].fillna(0) + users["Tokens out"].fillna(0)
        if tot.sum() > 0:
            share = tot.max() / tot.sum()
            who = users.iloc[int(tot.values.argmax())]
            if share > 0.4:
                any_sat = any(n_["lvl"] == "warn" for n_ in notes)
                notes.append({"lvl": "warn" if any_sat else "info", "stack": "global", "txt":
                    f"{who.get('Utilisateur', '?')} ({who.get('Clé', '?')}) concentre {share:.0%} des tokens. "
                    + ("Combiné aux signaux de saturation : envisager une limite par clé (max_parallel_requests / tpm_limit dans LiteLLM)."
                       if any_sat else "Sans saturation, pas d'action nécessaire ; à surveiller.")})
    return notes


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
        if c.startswith("%") or c.startswith("Part") or c in ("KV p95", "Hit prefix"):
            d[c] = d[c].map(lambda x: "–" if pd.isna(x) else f"{x:.1%}".replace(".", ","))
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


def build(per: Period) -> Path:
    p = Prom(PROM_URL)
    anon = Anon(ANONYMIZE)
    anon.keys = Anon(ANONYMIZE, prefix="clé")  # les alias de clé contiennent souvent un nom
    print(f"Période : {per.label}")
    print("· gateway LiteLLM…")
    gw = gateway_section(p, per, anon)
    print("· moteurs vLLM…")
    eng = engine_section(p, per)
    print("· SpendLogs…")
    sl = spendlogs_section(per, anon)
    notes = diagnose(eng, gw, sl if sl and "error" not in sl else None)

    k = gw["kpi"]
    kpis = [
        ("Requêtes", fmt_num(k["requests"])),
        ("Taux d'échec", "–" if not k["requests"] else f"{(k['failures'] or 0)/k['requests']:.1%}".replace(".", ",")),
        ("Tokens entrée", fmt_num(k["tin"])),
        ("Tokens sortie", fmt_num(k["tout"])),
        ("Utilisateurs actifs", fmt_num(k["users"])),
        ("TTFT p50 / p95", " / ".join("–" if x is None else f"{x:.2f}s".replace(".", ",") for x in (k["ttft_p50"], k["ttft_p95"]))),
        ("Latence totale p95", "–" if k["lat_p95"] is None else f"{k['lat_p95']:.1f}s".replace(".", ",")),
        ("Overhead LiteLLM p95", "–" if k["ovh_p95"] is None else f"{k['ovh_p95']*1000:.0f} ms"),
    ]
    if sl and "conc" in sl:
        c = sl["conc"]
        kpis.append(("Concurrence max (exacte)", f"{c['max']} ({c['peak_at']})"))
        kpis.append(("Concurrence p95 / min", fmt_num(c["p95_min"])))

    env = Environment(loader=FileSystemLoader(Path(__file__).parent), autoescape=select_autoescape(["html"]))
    html = env.get_template("template.html.j2").render(
        period=per.label, generated=datetime.now(TZ).strftime("%d/%m/%Y %H:%M"),
        kpis=kpis, notes=notes, slo_ttft=SLO_TTFT, slo_itl=SLO_ITL, anonymized=ANONYMIZE,
        gw={"team": df_html(gw["by_team"]), "user": df_html(gw["by_user"]),
            "chart_daily": gw["chart_daily"], "chart_heatmap": gw["chart_heatmap"]},
        eng={"table": df_html(eng["table"]), "chart_conc": eng["chart_conc"], "chart_kv": eng["chart_kv"]},
        sl=None if sl is None else ({"error": sl["error"]} if "error" in sl else {
            "n": fmt_num(sl["n"]), "team": df_html(sl["by_team"]), "user": df_html(sl["by_user"]),
            "base": df_html(sl["by_base"]), "chart_conc": sl["chart_conc"]}),
    )
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"rapport-llm_{per.slug}.html"
    out.write_text(html, encoding="utf-8")

    write_csv(per, "gateway_par_team", gw["by_team"])
    write_csv(per, "gateway_par_utilisateur", gw["by_user"])
    write_csv(per, "gateway_requetes_horaires", gw["hourly_csv"])
    write_csv(per, "moteurs_par_stack", eng["table"])
    if sl and "error" not in sl:
        write_csv(per, "spendlogs_par_team", sl["by_team"])
        write_csv(per, "spendlogs_par_utilisateur", sl["by_user"])
        write_csv(per, "spendlogs_par_stack", sl["by_base"])
    print(f"✔ {out}")
    return out


# --- check ----------------------------------------------------------------------------
def check() -> int:
    p = Prom(PROM_URL)
    ok = True
    print(f"Prometheus : {PROM_URL}")
    for r in p.instant('up{job=~"litellm|vllm"}', datetime.now(TZ)):
        m = r["metric"]
        state = "UP " if r["value"][1] == "1" else "DOWN"
        ok &= state == "UP "
        print(f"  [{state}] {m.get('job'):8} {m.get('instance'):24} {m.get('provider', '')}")
    names = p.names()
    print("\nMétriques attendues :")
    for key, rx in M.items():
        found = [nm for nm in names if re.fullmatch(rx, nm)]
        mark = "✔" if found else "✘"
        ok &= bool(found)
        print(f"  {mark} {key:8} {', '.join(found) or '(absente : ' + rx + ')'}")
    sample = p.instant(f"count by (team_alias, user, api_key_alias) ({sel('req')})", datetime.now(TZ))
    print(f"\nCombinaisons team/user/clé vues par Prometheus : {len(sample)}")
    for r in sample[:10]:
        m = r["metric"]
        print(f"  team={m.get('team_alias', '')!r} user={m.get('user', '')!r} key={m.get('api_key_alias', '')!r}")
    print("\n" + ("Tout est en place." if ok else "Des éléments manquent (voir ✘ / DOWN). Les sections concernées du rapport seront vides."))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", nargs="?", default="report", choices=["report", "check"])
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--previous-month", action="store_true")
    a = ap.parse_args()
    if a.cmd == "check":
        sys.exit(check())
    build(parse_period(a))


if __name__ == "__main__":
    main()
