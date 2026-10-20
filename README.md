# litellm-vllm-observability

Observabilité d'une infra LLM self-hosted : **LiteLLM** (gateway) + **vLLM** (moteurs) avec Prometheus,
3 dashboards Grafana, une sonde témoin et un rapport mensuel HTML/CSV.

Conçu pour une période d'observation qui doit **trancher une question** : les très gros prompts
ralentissent-ils les autres utilisateurs ? Si oui, faut-il des conseils d'usage, des limites par clé
LiteLLM ou des réglages vLLM ? Le rapport donne un verdict chiffré, pas seulement des moyennes.

## Quickstart

Prérequis : Docker (Compose v2) et git.

```bash
curl -fsSL https://raw.githubusercontent.com/WillIsback/litellm-vllm-observability/main/install.sh | bash
```

Le script clone le dépôt, génère `.env`, écrit les cibles Prometheus (questions interactives, ou variables
`LITELLM_TARGET`, `VLLM_TARGETS="stack-a=10.0.0.21:8000,stack-b=10.0.0.31:8000"`, `LITELLM_PG_HOST`,
`LITELLM_PG_PASSWORD`, `USE_LITELLM=n` pour une stack vLLM seule, `INSTALL_DIR`, `NO_START=1`), lance la stack et exécute `check`.
Grafana : http://127.0.0.1:3000 (tunnel SSH, ou `BIND_ADDR` dans `.env`), dossier **« LLM Observability »**.

> **Mise à jour d'une installation existante** : Grafana et Prometheus n'écoutent plus que sur
> `127.0.0.1` par défaut (`--web.enable-lifecycle` permet d'arrêter Prometheus par HTTP). Pour un accès
> réseau, définir `BIND_ADDR=<IP de l'interface d'admin>` dans `.env`. L'anonymisation du rapport est
> désormais activée par défaut.

## Ce qui tranche l'hypothèse

| Où | Quoi | Pourquoi |
|---|---|---|
| Dashboard SpendLogs, section du haut · rapport | TTFT des **petites** requêtes avec / sans gros prompt d'une autre clé en lecture sur la même stack | Le test causal : un écart net confirme, aucun écart infirme. Verdict automatique (≥ 30 requêtes de chaque côté) |
| idem | TTFT par taille de prompt | Le TTFT global mélange la lenteur normale des gros prompts et celle subie par les petits |
| Dashboard vLLM · rapport | **Sonde témoin** : petite requête fixe chaque minute par cible | L'expérience d'un utilisateur ordinaire, indépendante du mélange de trafic, comparable avant / après |
| Dashboard SpendLogs · rapport | Requêtes simultanées **par clé**, minutes au-dessus de 2 / 3 / 4 | Dimensionner `max_parallel_requests` sur des faits |
| Dashboard vLLM | Tokens de prompt calculés vs servis par le cache, taille des prompts, temps de prefill | La vraie charge de lecture ; un cache faible trahit des contextes réécrits ou un routage sans affinité |

Seuils : `SMALL_PROMPT_TOKENS` (8000) et `LARGE_PROMPT_TOKENS` (100000) dans `.env` (variables `petit` / `gros`
dans Grafana).

**Sonde témoin** (`canary`) : `CANARY_TARGETS=nom=url[|modèle],…` dans `.env`. L'installation la configure sur
chaque master vLLM ; ajouter la gateway avec une **clé LiteLLM dédiée « canary »** pour mesurer le parcours
complet (`gateway=http://<litellm>:4000/v1|<model_name>`, clé dans `CANARY_KEY_GATEWAY`). Ses requêtes
apparaissent dans SpendLogs sous cette clé : ce sont des « petites requêtes » idéales pour le test.

**Monitoring exclu des KPI** : les requêtes de santé interne de LiteLLM et celles de la sonde ne sont pas de
l'usage. Le rapport les exclut des totaux, des « utilisateurs actifs » et de la concurrence — mais garde la
sonde dans l'analyse causale. Par défaut `EXCLUDE_TEAMS=litellm-internal-health-check` et
`EXCLUDE_KEY_ALIASES=canary` dans `.env` (listes séparées par des virgules ; adapter l'alias de clé si la
sonde en porte un autre). De même, une requête sans utilisateur rattaché (Open WebUI, santé, sonde) est
affichée « (aucun) » et n'est jamais comptée comme un utilisateur actif.

**Affinité de routage** : avec `simple-shuffle`, une session d'agent alterne entre stacks et perd le cache de
préfixe une fois sur deux (chaque tour relit tout le contexte). Comparer le panneau « calculés vs cache » avant
de conclure que les utilisateurs sont seuls en cause.

**Tokens en cache par requête** : lancer vLLM avec `--enable-prompt-tokens-details` pour que la réponse
contienne `cached_tokens` (utile pour savoir quelle part des « 10 M tokens » d'une session a vraiment été calculée).

**Pièges de mesure (constatés sur GB10)** : `vllm:prompt_tokens_total` n'avance qu'en fin de lecture (un prefill
de 5 min est invisible puis fait un pic) ; le KV cache peut rester < 20 % alors que les utilisateurs attendent
plusieurs minutes (le goulot est le temps de lecture, pas la mémoire) ; avec dcgm-exporter,
`DCGM_FI_DEV_GPU_UTIL` reste ~96 % dès que le modèle est chargé : suivre `DCGM_FI_DEV_POWER_USAGE`.
Côté latence, un ITL p95 ~140 ms est le **plafond de décodage** de ces modèles (bande passante mémoire) et non
un symptôme de saturation tant que la concurrence reste faible : le diagnostic le sait (il ne parle de
« batch trop gros » qu'au-delà de quelques requêtes en cours) et `SLO_ITL_P95=0.2` dans `.env` en tient compte.

**Licence LiteLLM** : selon la version, le callback `prometheus` peut nécessiter une licence Enterprise. Si le
dashboard « usage LiteLLM » reste vide, SpendLogs et la sonde suffisent pour le test de l'hypothèse.

Rapport : `docker compose run --rm reporter [--previous-month | --start AAAA-MM-JJ --end AAAA-MM-JJ]`
→ `./reports/rapport-llm_<période>.html` + CSV.

## Configuration

**LiteLLM** : activer le callback Prometheus (`litellm/config-snippet.yaml`) :

```yaml
litellm_settings:
  callbacks: ["prometheus"]
```

**vLLM** : rien à faire (`/metrics` est exposé ; ne pas utiliser `--disable-log-stats`). Scraper le master de chaque stack.

**Postgres LiteLLM** : rôle en lecture seule pour les données exactes (SpendLogs) :

```sql
CREATE ROLE grafana_ro LOGIN PASSWORD '...';
GRANT CONNECT ON DATABASE litellm TO grafana_ro;
GRANT USAGE ON SCHEMA public TO grafana_ro;
GRANT SELECT ON "LiteLLM_SpendLogs" TO grafana_ro;
GRANT SELECT (team_id, team_alias)                ON "LiteLLM_TeamTable"         TO grafana_ro;
GRANT SELECT (user_id, user_email)                ON "LiteLLM_UserTable"         TO grafana_ro;
GRANT SELECT (token, key_alias, team_id, user_id) ON "LiteLLM_VerificationToken" TO grafana_ro;
```

**Fichiers à ajuster** (si besoin après l'install) :

| Fichier | Rôle |
|---|---|
| `.env` | mots de passe Grafana/Postgres, `BIND_ADDR`, ports, rétention Prometheus (120 j), SLO (`SLO_TTFT_P95`, `SLO_ITL_P95`), exclusions de monitoring (`EXCLUDE_TEAMS`, `EXCLUDE_KEY_ALIASES`), seuils petit/gros prompt, sonde (`CANARY_*`), `ANONYMIZE_USERS`, `HOST_UID/GID` |
| `prometheus/targets/litellm.yml` | adresse du proxy LiteLLM |
| `prometheus/targets/vllm.yml` | master de chaque stack ; `api_base` doit être **identique** à celui du `config.yaml` LiteLLM |

Les cibles sont rechargées à chaud.

**Données personnelles** : le rapport est anonymisé par défaut (`ANONYMIZE_USERS=true`) et les dashboards
SpendLogs affichent des pseudonymes (variable « Identités »). Le mode nominatif de Grafana n'est qu'un affichage :
restreindre l'accès au dossier Grafana, informer les agents de la finalité (dimensionnement du service partagé),
et associer le DPO si l'usage nominatif est conservé.

Tests : `python -m pytest reporter/tests` (pandas, pytest).

Cron mensuel : `0 7 1 * * cd /opt/llm-obs && docker compose run --rm reporter --previous-month`

## Licence

MIT, voir `LICENSE`.
