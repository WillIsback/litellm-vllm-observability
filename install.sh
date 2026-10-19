#!/usr/bin/env bash
# Installation en une commande :
#   curl -fsSL https://raw.githubusercontent.com/WillIsback/litellm-vllm-observability/main/install.sh | bash
# Variables optionnelles : INSTALL_DIR, LITELLM_TARGET, VLLM_TARGETS ("nom=hôte:port,..."),
# LITELLM_PG_HOST, LITELLM_PG_PASSWORD, GF_ADMIN_PASSWORD, NO_START=1
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/WillIsback/litellm-vllm-observability.git}"
DIR="${INSTALL_DIR:-$PWD/litellm-vllm-observability}"

for c in git docker; do command -v "$c" >/dev/null || { echo "✗ $c est requis" >&2; exit 1; }; done
docker compose version >/dev/null 2>&1 || { echo "✗ docker compose (v2) est requis" >&2; exit 1; }

if [ -d "$DIR/.git" ]; then git -C "$DIR" pull --ff-only; else git clone --depth 1 "$REPO_URL" "$DIR"; fi
cd "$DIR"

ask() { # ask VAR "question" défaut
  local v="${!1:-}"
  if [ -z "$v" ] && [ -r /dev/tty ]; then read -r -p "$2 [$3] : " v </dev/tty || true; fi
  printf -v "$1" '%s' "${v:-$3}"
}
setenv() { sed -i "s|^$1=.*|$1=$2|" .env; }

if [ ! -f .env ]; then
  cp .env.example .env
  ask GF_ADMIN_PASSWORD "Mot de passe admin Grafana" "$(head -c 12 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 16)"
  ask LITELLM_PG_HOST "Postgres LiteLLM (hôte:port)" "10.0.0.10:5432"
  ask LITELLM_PG_PASSWORD "Mot de passe du rôle grafana_ro" "change-me"
  setenv GF_ADMIN_PASSWORD "$GF_ADMIN_PASSWORD"
  setenv LITELLM_PG_HOST "$LITELLM_PG_HOST"
  setenv LITELLM_PG_PASSWORD "$LITELLM_PG_PASSWORD"
  setenv HOST_UID "$(id -u)"
  setenv HOST_GID "$(id -g)"
  echo "✓ .env créé"
fi

ask LITELLM_TARGET "Adresse LiteLLM (hôte:port)" "10.0.0.10:4000"
printf -- '- targets: ["%s"]\n  labels:\n    service: litellm-gateway\n' "$LITELLM_TARGET" > prometheus/targets/litellm.yml

ask VLLM_TARGETS "Masters vLLM (nom=hôte:port,...)" "stack-a=10.0.0.21:8000,stack-b=10.0.0.31:8000"
: > prometheus/targets/vllm.yml
IFS=',' read -ra T <<< "$VLLM_TARGETS"
for t in "${T[@]}"; do
  printf -- '- targets: ["%s"]\n  labels:\n    provider: %s\n    api_base: http://%s/v1\n' "${t#*=}" "${t%%=*}" "${t#*=}" >> prometheus/targets/vllm.yml
done
echo "✓ cibles Prometheus écrites"

[ "${NO_START:-0}" = 1 ] && { echo "Démarrer : cd $DIR && docker compose up -d"; exit 0; }
docker compose up -d
docker compose run --rm reporter check || true
echo "✓ Grafana : http://localhost:$(grep ^GF_PORT= .env | cut -d= -f2)  (dossier « LLM - Équipe dev »)"
