#!/usr/bin/env python3
"""Sonde témoin : une petite requête fixe envoyée à intervalle régulier à chaque cible.

Elle mesure ce que vit un utilisateur ordinaire, indépendamment du mélange de trafic,
et expose le résultat au format Prometheus sur :9105/metrics. Bibliothèque standard uniquement.

Variables :
  CANARY_TARGETS   nom=url_openai[|modèle], séparés par des virgules. Sans modèle : 1er de /models.
                   ex. gateway=http://10.0.0.10:4000/v1|mistral-small,stack-a=http://10.0.0.21:8000/v1
  CANARY_API_KEY   clé par défaut (une clé LiteLLM dédiée « canary » pour la gateway)
  CANARY_KEY_<NOM> clé propre à une cible (NOM en majuscules, - remplacé par _), ex. CANARY_KEY_STACK_A
  CANARY_INTERVAL  secondes entre deux sondes par cible (défaut 60)
  CANARY_TIMEOUT   délai max d'une sonde en secondes (défaut 900)
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BUCKETS = (0.25, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600)
PROMPT = "Réponds en une phrase : à quoi sert un reverse proxy ?"
INTERVAL = float(os.environ.get("CANARY_INTERVAL", "60"))
TIMEOUT = float(os.environ.get("CANARY_TIMEOUT", "900"))

lock = threading.Lock()
hist: dict[str, list[int]] = {}       # target -> compteurs cumulés par bucket (+Inf en dernier)
hsum: dict[str, float] = {}
count: dict[tuple[str, str], int] = {}  # (target, status) -> n
last: dict[str, dict[str, float]] = {}  # target -> {ttft, tps, ok, ts}
inflight: dict[str, int] = {}


def parse_targets(raw: str) -> list[dict]:
    out = []
    for item in filter(None, (x.strip() for x in raw.split(","))):
        name, _, rest = item.partition("=")
        url, _, model = rest.partition("|")
        env = "CANARY_KEY_" + name.upper().replace("-", "_")
        out.append({"name": name, "url": url.rstrip("/"), "model": model or None,
                    "key": os.environ.get(env) or os.environ.get("CANARY_API_KEY", "")})
    return out


def http(url: str, key: str, body: dict | None = None, timeout: float = 30):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = json.dumps(body).encode() if body is not None else None
    return urllib.request.urlopen(urllib.request.Request(url, data, headers), timeout=timeout)


def resolve_model(t: dict) -> str:
    if not t["model"]:
        with http(t["url"] + "/models", t["key"]) as r:
            t["model"] = json.load(r)["data"][0]["id"]
    return t["model"]


def observe(target: str, status: str, ttft: float | None = None, tps: float | None = None):
    with lock:
        count[(target, status)] = count.get((target, status), 0) + 1
        if ttft is not None:
            b = hist.setdefault(target, [0] * (len(BUCKETS) + 1))
            for i, le in enumerate(BUCKETS):
                if ttft <= le:
                    b[i] += 1
            b[-1] += 1
            hsum[target] = hsum.get(target, 0.0) + ttft
        last[target] = {"ttft": ttft if ttft is not None else float("nan"),
                        "tps": tps if tps is not None else float("nan"),
                        "ok": 1.0 if status == "ok" else 0.0, "ts": time.time()}


def probe(t: dict):
    name = t["name"]
    with lock:
        inflight[name] = inflight.get(name, 0) + 1
    t0 = time.time()
    try:
        body = {"model": resolve_model(t), "max_tokens": 48, "temperature": 0, "stream": True,
                "stream_options": {"include_usage": True},
                # préfixe aléatoire : pas de cache de préfixe, on mesure toujours le même travail
                "messages": [{"role": "user", "content": f"[{uuid.uuid4().hex[:8]}] {PROMPT}"}],
                "chat_template_kwargs": {"enable_thinking": False}}
        ttft, tokens = None, 0
        with http(t["url"] + "/chat/completions", t["key"], body, TIMEOUT) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                ev = json.loads(line[6:])
                delta = (ev.get("choices") or [{}])[0].get("delta") or {}
                if ttft is None and (delta.get("content") or delta.get("reasoning_content") or delta.get("reasoning")):
                    ttft = time.time() - t0
                if ev.get("usage"):
                    tokens = ev["usage"].get("completion_tokens", 0)
        dt = time.time() - t0
        if ttft is None:
            observe(name, "no_token")
        else:
            observe(name, "ok", ttft, tokens / (dt - ttft) if dt > ttft and tokens else None)
    except urllib.error.HTTPError as e:
        observe(name, f"http_{e.code}")
    except Exception as e:  # noqa: BLE001
        observe(name, "timeout" if "timed out" in str(e) else "error")
        print(f"[{name}] {e!r}", flush=True)
    finally:
        with lock:
            inflight[name] -= 1


def scheduler(targets: list[dict]):
    while True:
        for t in targets:  # une sonde par cible et par intervalle, même si la précédente n'a pas fini
            threading.Thread(target=probe, args=(t,), daemon=True).start()
        time.sleep(INTERVAL)


def render() -> str:
    lines = ["# HELP canary_ttft_seconds Délai avant le premier token de la sonde témoin.",
             "# TYPE canary_ttft_seconds histogram"]
    with lock:
        for tg, b in sorted(hist.items()):
            for le, n in zip(BUCKETS, b):
                lines.append(f'canary_ttft_seconds_bucket{{target="{tg}",le="{le:g}"}} {n}')
            lines.append(f'canary_ttft_seconds_bucket{{target="{tg}",le="+Inf"}} {b[-1]}')
            lines.append(f'canary_ttft_seconds_sum{{target="{tg}"}} {hsum[tg]:.6f}')
            lines.append(f'canary_ttft_seconds_count{{target="{tg}"}} {b[-1]}')
        lines += ["# HELP canary_requests_total Sondes envoyées, par statut.", "# TYPE canary_requests_total counter"]
        for (tg, st), n in sorted(count.items()):
            lines.append(f'canary_requests_total{{target="{tg}",status="{st}"}} {n}')
        for metric, key, help_ in (("canary_last_ttft_seconds", "ttft", "TTFT de la dernière sonde terminée."),
                                   ("canary_last_tokens_per_second", "tps", "Vitesse d'écriture de la dernière sonde."),
                                   ("canary_last_success", "ok", "1 si la dernière sonde a réussi."),
                                   ("canary_last_timestamp_seconds", "ts", "Fin de la dernière sonde (epoch).")):
            lines += [f"# HELP {metric} {help_}", f"# TYPE {metric} gauge"]
            lines += [f'{metric}{{target="{tg}"}} {v[key]}' for tg, v in sorted(last.items())]
        lines += ["# HELP canary_inflight Sondes en cours (attente comprise).", "# TYPE canary_inflight gauge"]
        lines += [f'canary_inflight{{target="{tg}"}} {n}' for tg, n in sorted(inflight.items())]
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = render().encode() if self.path.startswith("/metrics") else b"ok\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def main():
    targets = parse_targets(os.environ.get("CANARY_TARGETS", ""))
    if targets:
        print("Cibles : " + ", ".join(f"{t['name']} -> {t['url']}" for t in targets), flush=True)
        threading.Thread(target=scheduler, args=(targets,), daemon=True).start()
    else:
        print("CANARY_TARGETS vide : sonde inactive (métriques vides).", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 9105), Handler).serve_forever()


if __name__ == "__main__":
    main()
