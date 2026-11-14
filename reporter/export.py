"""Export des données brutes pour une analyse par IA, sans conclusion préalable.

Le rapport mensuel interprète (seuils, verdicts, hypothèse « gros prompts ») : le donner à une IA
l'ancre sur ces conclusions. Cet export ne contient que des mesures, décrites de façon factuelle :

  requests.csv        une ligne par requête vue par la gateway (SpendLogs), monitoring compris et étiqueté
  engine_minutes.csv  une ligne par serveur vLLM et par minute (Prometheus)
  probe_minutes.csv   une ligne par cible de la sonde et par minute
  gateway_minutes.csv une ligne par équipe et par minute (Prometheus, utile sans SpendLogs)
  dictionary.md       description des fichiers, colonnes, unités et limites de mesure
  prompt.md           consigne d'analyse ouverte, sans hypothèse, aspects dans un ordre tiré au hasard
  manifest.json       période, volumes, couverture, graine, empreintes des fichiers

Aucune colonne n'est une classe ou un seuil (« gros prompt », « lecture lourde »…) : seulement des
valeurs mesurées ou calculées par une opération arithmétique documentée.
"""
from __future__ import annotations

import hashlib
import json
import random
import zipfile
from datetime import datetime, timezone

import pandas as pd

import report as rp

ASPECTS = [
    "Qui utilise le service : personnes, équipes, clés, concentration de l'usage",
    "Évolution dans le temps : jours, heures, semaines, tendance",
    "Profil des demandes : taille des entrées et des sorties, modèles, types d'appels",
    "Délai avant le début des réponses et ce qui le fait varier",
    "Vitesse d'écriture des réponses et ce qui la fait varier",
    "Fiabilité : échecs, disponibilité des serveurs, résultats de la sonde",
    "Répartition des demandes entre serveurs et utilisation de chacun",
    "Capacité : demandes simultanées, files d'attente, mémoire, marge restante",
    "Effets des demandes les unes sur les autres quand elles sont simultanées",
    "Cache de préfixe : part des entrées réutilisées et conséquences",
]

ENGINE_QUERIES = {  # nom de colonne -> expression PromQL par minute, agrégée par serveur (provider)
    "requests_running": "sum by (provider) (avg_over_time({running}[1m]))",
    "requests_waiting": "sum by (provider) (avg_over_time({waiting}[1m]))",
    "kv_cache_usage_max": "max by (provider) (max_over_time({kv}[1m]))",
    "requests_finished": "sum by (provider) (increase({vreq}[1m]))",
    "prompt_tokens_per_s": "sum by (provider) (rate({ptok}[1m]))",
    "generation_tokens_per_s": "sum by (provider) (rate({gtok}[1m]))",
    "scheduled_tokens_per_s": "sum by (provider) (rate({iter}[1m]))",
    "prefix_cache_queries_per_s": "sum by (provider) (rate({pq}[1m]))",
    "prefix_cache_hits_per_s": "sum by (provider) (rate({phit}[1m]))",
    "preemptions": "sum by (provider) (increase({preempt}[1m]))",
    "spec_drafts_per_s": "sum by (provider) (rate({sd_drafts}[1m]))",
    "spec_draft_tokens_per_s": "sum by (provider) (rate({sd_dtok}[1m]))",
    "spec_accepted_tokens_per_s": "sum by (provider) (rate({sd_acc}[1m]))",
    "scrape_up": 'avg by (provider) (avg_over_time(up{{job="vllm"}}[1m]))',
}


def _q(expr: str) -> str:
    """Remplace {clé} par le sélecteur de rp.M ; {{ }} donne une accolade (sélecteurs déjà écrits intacts)."""
    for k in rp.M:
        expr = expr.replace("{" + k + "}", rp.sel(k))
    return expr.replace("{{", "{").replace("}}", "}")


def _minutes(p: rp.Prom, per: rp.Period, queries: dict, label: str) -> pd.DataFrame:
    """Une ligne par (minute, valeur du label), une colonne par requête."""
    long = []
    for col, expr in queries.items():
        df = p.range_df_chunked(_q(expr), per.start, per.end, 60, label)
        if df.empty:
            continue
        s = df.stack()
        s.index.names = ["t", label]
        long.append(s.rename(col))
    if not long:
        return pd.DataFrame()
    out = pd.concat(long, axis=1).reset_index()
    return _time_cols(out, "t").sort_values(["t_utc", label])


def _time_cols(df: pd.DataFrame, col: str, prefix: str = "t") -> pd.DataFrame:
    t = pd.to_datetime(df.pop(col))
    df.insert(0, f"{prefix}_local", t.dt.tz_convert(rp.TZ).dt.strftime("%Y-%m-%dT%H:%M:%S"))
    df.insert(0, f"{prefix}_utc", t.dt.tz_convert("UTC").dt.strftime("%Y-%m-%dT%H:%M:%SZ"))
    return df


stack_names = rp.stack_names  # api_base -> nom du serveur (partagé avec le rapport)


def requests_table(per: rp.Period, anon: rp.Anon, stacks: dict[str, str]) -> tuple[pd.DataFrame, str | None]:
    conn, err = rp._spendlogs_connect()
    if err:
        return pd.DataFrame(), err["error"]
    if conn is None:
        return pd.DataFrame(), "SpendLogs non configuré (LITELLM_PG_HOST absent)"
    with conn:
        cols = {r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'LiteLLM_SpendLogs'").fetchall()}
        team_id = "COALESCE(s.team_id, k.team_id)" if "team_id" in cols else "k.team_id"
        opt = [f's.{c}' for c in ("status", "call_type", "cache_hit") if c in cols]
        q = f'''
        SELECT s."startTime" AS start, s."completionStartTime" AS first_token, s."endTime" AS end_,
               s.prompt_tokens, s.completion_tokens, s.api_base, s.model_group AS model,
               COALESCE(t.team_alias, {team_id}) AS team,
               NULLIF(NULLIF(s."user", ''), 'None') AS user_id, u.user_email,
               k.key_alias {"".join(", " + c for c in opt)}
        FROM "LiteLLM_SpendLogs" s
        LEFT JOIN "LiteLLM_VerificationToken" k ON k.token = s.api_key
        LEFT JOIN "LiteLLM_TeamTable" t ON t.team_id = {team_id}
        LEFT JOIN "LiteLLM_UserTable" u ON u.user_id = s."user"
        WHERE s."startTime" >= %s AND s."startTime" < %s
        ORDER BY s."startTime"'''
        try:
            cur = conn.execute(q, (per.start, per.end))
            df = pd.DataFrame(cur.fetchall(), columns=[d.name for d in cur.description])
        except Exception as e:  # noqa: BLE001
            return pd.DataFrame(), f"requête SpendLogs en échec : {e}"
    if df.empty:
        return df, "aucune ligne SpendLogs sur la période"
    return shape_requests(df, anon, stacks), None


def shape_requests(df: pd.DataFrame, anon: rp.Anon, stacks: dict[str, str]) -> pd.DataFrame:
    """Colonnes neutres. Le monitoring est gardé et étiqueté (source), pas retiré : c'est à l'analyse de choisir."""
    for c in ("start", "first_token", "end_"):
        df[c] = pd.to_datetime(df[c], utc=True)
    source = pd.Series("usage", index=df.index)
    source[df["key_alias"].isin(rp.EXCLUDE_KEY_ALIASES)] = "sonde"
    source[df["team"].isin(rp.EXCLUDE_TEAMS)] = "sante_litellm"
    keys = getattr(anon, "keys", None) or rp.Anon(anon.enabled, prefix="clé")
    ident = df["user_id"] if anon.enabled else df["user_email"].fillna(df["user_id"])
    base = df["api_base"].fillna("").str.rstrip("/")
    streamed = df["first_token"].notna() & (df["first_token"] > df["start"]) & (df["first_token"] < df["end_"])
    out = pd.DataFrame({
        "start": df["start"],
        "ttft_s": ((df["first_token"] - df["start"]).dt.total_seconds()).where(streamed),
        "duration_s": (df["end_"] - df["start"]).dt.total_seconds(),
        "prompt_tokens": df["prompt_tokens"],
        "completion_tokens": df["completion_tokens"],
        "streamed": streamed,
        "stack": base.map(lambda b: stacks.get(b) or b or None),
        "model": df["model"],
        "team": df["team"].where(~df["team"].isin(["", "None"])),
        "user": ident.map(lambda v: anon(v) if pd.notna(v) else None),
        "key": df["key_alias"].map(lambda v: keys(v) if pd.notna(v) else None),
        "source": source,
    })
    for c in ("status", "call_type", "cache_hit"):
        if c in df:
            out[c] = df[c]
    out = _time_cols(out, "start", "start")
    return out


def dictionary(stacks: dict[str, str], notes: list[str]) -> str:
    srv = ", ".join(sorted(set(stacks.values()))) or "inconnus"
    return f"""# Dictionnaire des données

Service : une gateway LiteLLM reçoit les demandes (API compatible OpenAI) et les transmet à des serveurs vLLM
({srv}). Une sonde envoie la même requête courte (question d’une phrase, 48 tokens de réponse au plus) chaque minute à chaque cible. Horodatages : `*_utc` en UTC,
`*_local` en heure de Paris. Séparateur `,`, décimale `.`. Une cellule vide = valeur non mesurée.

## requests.csv : une ligne par requête vue par la gateway (table LiteLLM_SpendLogs)

| Colonne | Signification |
|---|---|
| start_utc, start_local | réception de la requête par la gateway |
| ttft_s | secondes entre la réception et le premier token reçu ; vide si la réponse n'était pas en streaming |
| duration_s | secondes entre la réception et la fin de la réponse |
| prompt_tokens | tokens d'entrée (question, historique, documents), y compris ceux servis par le cache du serveur |
| completion_tokens | tokens générés |
| streamed | réponse envoyée au fil de l'eau (true) ou d'un bloc (false) |
| stack | serveur vLLM qui a traité la requête ; vide si aucun (échec avant envoi, santé interne) |
| model | nom du modèle demandé à la gateway |
| team, user, key | équipe, personne et clé d'accès ; `user` et `key` sont des pseudonymes si l'anonymisation est active |
| source | `usage` (demandes réelles), `sonde` (requêtes de mesure), `sante_litellm` (contrôles internes de la gateway) |
| status, call_type, cache_hit | colonnes LiteLLM, si la version les fournit |

## engine_minutes.csv : une ligne par serveur et par minute (métriques vLLM)

| Colonne | Signification |
|---|---|
| requests_running | requêtes en cours de traitement, moyenne sur la minute |
| requests_waiting | requêtes en attente d'une place, moyenne sur la minute |
| kv_cache_usage_max | occupation maximale de la mémoire des conversations (0 à 1) |
| requests_finished | requêtes terminées pendant la minute |
| prompt_tokens_per_s | tokens d'entrée des requêtes terminées ou démarrées (cache compris), par seconde |
| generation_tokens_per_s | tokens générés par seconde |
| scheduled_tokens_per_s | tokens traités par le moteur par seconde (entrées calculées hors cache + tokens générés) |
| prefix_cache_queries_per_s, prefix_cache_hits_per_s | tokens d'entrée cherchés dans le cache / trouvés |
| preemptions | requêtes interrompues faute de mémoire pendant la minute |
| spec_drafts_per_s, spec_draft_tokens_per_s, spec_accepted_tokens_per_s | décodage spéculatif : propositions, tokens proposés, tokens acceptés |
| scrape_up | 1 si le serveur a répondu à la collecte pendant la minute, 0 sinon |

## probe_minutes.csv : une ligne par cible de la sonde et par minute

| Colonne | Signification |
|---|---|
| target | cible interrogée : la gateway (parcours complet) ou un serveur vLLM en direct |
| probes, failures | sondes terminées et sondes en échec pendant la minute |
| ttft_mean_s | délai moyen avant le premier token des sondes terminées pendant la minute |
| probes_over_10s | sondes dont le premier token est arrivé après plus de 10 s |
| inflight | sondes en cours (attente comprise) |

## gateway_minutes.csv : une ligne par équipe et par minute (métriques LiteLLM)

`requests`, `failures`, `input_tokens`, `output_tokens` comptés pendant la minute.

## Limites de mesure connues

- `prompt_tokens_per_s` n'avance qu'à la fin de la lecture d'une requête : une longue lecture apparaît d'un bloc.
- La sonde est une requête courte et fixe ; ses résultats ne représentent pas les demandes longues.
- `scrape_up` mesure la réponse à la collecte, pas directement la disponibilité pour les utilisateurs.
- Une requête qui échoue avant d'atteindre un serveur peut n'avoir ni `stack` ni équipe.
{"".join(f"- {n}" + chr(10) for n in notes)}"""


def prompt(aspects: list[str], seed: int) -> str:
    liste = "\n".join(f"- {a}" for a in aspects)
    return f"""# Consigne d'analyse

Tu analyses l'usage d'un service d'IA générative interne, à partir des fichiers joints. Lis d'abord
`dictionary.md`. Charge et calcule avec du code (pandas ou équivalent) ; ne lis pas les CSV à l'œil.

Aucune hypothèse ne t'est fournie. Ne cherche pas à confirmer une idée reçue sur ce type de service :
décris ce que montrent les données, puis seulement ensuite propose des explications.

## Étapes

1. **Qualité des données.** Couverture de la période, trous, doublons, valeurs aberrantes, écarts entre
   les sources (requests.csv contre gateway_minutes.csv contre engine_minutes.csv). Indique ce que tu exclus et pourquoi.
2. **Description.** Pour chacun des aspects ci-dessous, les faits chiffrés les plus importants.
   Leur ordre a été tiré au hasard (graine {seed}) : il n'indique aucune priorité. Traite-les tous avec
   le même soin, puis classe toi-même tes constats par importance.
{liste}
3. **Explications.** Pour chaque constat notable : au moins deux explications concurrentes, l'analyse qui les
   départage, et le résultat de cette analyse. Contrôle les facteurs de confusion (heure, jour, charge du moment,
   serveur, taille de la demande, cache, utilisateur). Donne l'ampleur d'un effet, sa fréquence et son incertitude
   (intervalle de confiance, effectif). Distingue corrélation et cause.
4. **Limites.** Ce que ces données ne permettent pas de conclure, et la mesure qui manquerait.
5. **Synthèse pour la direction.** Une page en langage courant, sans jargon : usage, qualité de service,
   capacité, points d'attention, propositions. Chaque phrase doit s'appuyer sur un chiffre établi plus haut.

## Règles

- N'invente aucune valeur. Si un calcul est impossible, dis-le.
- Montre le code ou la méthode de chaque chiffre important.
- Les monitorings (`source` = `sonde` ou `sante_litellm`) ne sont pas de l'usage réel ; décide et
  justifie, pour chaque analyse, si tu les inclus.
- Les noms de colonnes, d'équipes ou de serveurs ne sont pas des indices de cause.
"""


def run(per: rp.Period, seed: int | None = None) -> str:
    p = rp.Prom(rp.PROM_URL)
    anon = rp.Anon(rp.ANONYMIZE)
    anon.keys = rp.Anon(rp.ANONYMIZE, prefix="clé")
    seed = seed if seed is not None else random.SystemRandom().randrange(10_000)
    out = rp.OUT_DIR / f"export-ia_{per.slug}"
    out.mkdir(parents=True, exist_ok=True)
    print(f"Période : {per.label} → {out}")
    stacks = stack_names(p)
    notes = []

    print("· requêtes (SpendLogs)…")
    req, err = requests_table(per, anon, stacks)
    if err:
        notes.append(f"requests.csv absent ou vide : {err}.")
    print("· serveurs vLLM, minute par minute…")
    eng = _minutes(p, per, ENGINE_QUERIES, "provider").rename(columns={"provider": "stack"})
    print("· sonde…")
    c_le10, c_inf = rp.sel("canary_ttft", r'le=~"10(\\.0)?"'), rp.sel("canary_ttft", 'le="+Inf"')
    probe = _minutes(p, per, {
        "probes": "sum by (target) (increase({canary_req}[1m]))",
        "failures": f'sum by (target) (increase({rp.sel("canary_req", 'status!="ok"')}[1m]))',
        "ttft_mean_s": "sum by (target) (rate(canary_ttft_seconds_sum[1m])) / sum by (target) (rate(canary_ttft_seconds_count[1m]))",
        "probes_over_10s": f"sum by (target) (increase({c_inf}[1m])) - sum by (target) (increase({c_le10}[1m]))",
        "inflight": "max by (target) (max_over_time(canary_inflight[1m]))",
    }, "target")
    print("· gateway, minute par minute…")
    gw = _minutes(p, per, {
        "requests": "sum by (team_alias) (increase({req}[1m]))",
        "failures": "sum by (team_alias) (increase({fail}[1m]))",
        "input_tokens": "sum by (team_alias) (increase({tin}[1m]))",
        "output_tokens": "sum by (team_alias) (increase({tout}[1m]))",
    }, "team_alias").rename(columns={"team_alias": "team"})

    files = {"requests.csv": req, "engine_minutes.csv": eng, "probe_minutes.csv": probe, "gateway_minutes.csv": gw}
    for name, df in files.items():
        if df.empty:
            notes.append(f"{name} vide : métriques absentes sur la période.")
            continue
        df.to_csv(out / name, index=False, encoding="utf-8")
    (out / "dictionary.md").write_text(dictionary(stacks, notes), encoding="utf-8")
    aspects = ASPECTS[:]
    random.Random(seed).shuffle(aspects)
    (out / "prompt.md").write_text(prompt(aspects, seed), encoding="utf-8")

    written = sorted(f for f in out.iterdir() if f.is_file())
    manifest = {
        "period_start": per.start.isoformat(), "period_end": per.end.isoformat(),
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "anonymized": rp.ANONYMIZE, "aspect_order_seed": seed,
        "rows": {n: len(df) for n, df in files.items()},
        "sha256": {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in written},
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    archive = rp.OUT_DIR / f"export-ia_{per.slug}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(out.iterdir()):
            z.write(f, f"export-ia_{per.slug}/{f.name}")
    print(f"✔ {archive}")
    return str(archive)
