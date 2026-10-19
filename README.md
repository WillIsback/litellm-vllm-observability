# litellm-vllm-observability

Observabilité d'une infra LLM self-hosted : **LiteLLM** (gateway) + **vLLM** (moteurs) avec Prometheus,
3 dashboards Grafana et un rapport mensuel HTML/CSV.

## Quickstart

Prérequis : Docker (Compose v2) et git.

```bash
curl -fsSL https://raw.githubusercontent.com/WillIsback/litellm-vllm-observability/main/install.sh | bash
```

Le script clone le dépôt, génère `.env`, écrit les cibles Prometheus (questions interactives, ou variables
`LITELLM_TARGET`, `VLLM_TARGETS="stack-a=10.0.0.21:8000,stack-b=10.0.0.31:8000"`, `LITELLM_PG_HOST`,
`LITELLM_PG_PASSWORD`, `INSTALL_DIR`, `NO_START=1`), lance la stack et exécute `check`.
Grafana : http://localhost:3000, dossier **« LLM Observability »**.

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
| `.env` | mots de passe Grafana/Postgres, ports, rétention Prometheus, SLO (`SLO_TTFT_P95`, `SLO_ITL_P95`), `ANONYMIZE_USERS`, `HOST_UID/GID` |
| `prometheus/targets/litellm.yml` | adresse du proxy LiteLLM |
| `prometheus/targets/vllm.yml` | master de chaque stack ; `api_base` doit être **identique** à celui du `config.yaml` LiteLLM |

Les cibles sont rechargées à chaud. Le rapport détaille l'usage par utilisateur : informer l'équipe
(RGPD) ou activer `ANONYMIZE_USERS=true`.

Cron mensuel : `0 7 1 * * cd /opt/llm-obs && docker compose run --rm reporter --previous-month`

## Licence

MIT, voir `LICENSE`.
