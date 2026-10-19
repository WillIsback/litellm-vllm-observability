#!/usr/bin/env bash
# Publie ce dossier dans un nouveau dépôt GitHub public (nécessite gh authentifié : gh auth login)
set -euo pipefail
REPO="${1:-litellm-vllm-observability}"
cd "$(dirname "$0")"
[ -d .git ] || git init -b main
git add -A
git commit -m "Stack d'observabilité LiteLLM + vLLM : Prometheus, Grafana, rapport mensuel" || true
gh repo create "$REPO" --public --source=. --remote=origin --push \
  --description "Observabilité LiteLLM + vLLM : Prometheus, Grafana (3 dashboards) et rapport mensuel HTML/CSV"
gh repo edit --add-topic litellm,vllm,prometheus,grafana,llm-observability,llmops
echo "→ https://github.com/$(gh api user -q .login)/$REPO"
