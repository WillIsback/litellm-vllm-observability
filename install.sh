#!/usr/bin/env bash
# Installation en une commande :
#   curl -fsSL https://raw.githubusercontent.com/WillIsback/litellm-vllm-observability/main/install.sh | bash
# Mode non interactif (variables) : INSTALL_DIR, LITELLM_TARGET, VLLM_TARGETS ("nom=hôte:port,..."),
# USE_LITELLM=n, LITELLM_PG_HOST, LITELLM_PG_DB, LITELLM_PG_USER, LITELLM_PG_PASSWORD, LITELLM_METRICS_KEY, VLLM_METRICS_KEY, GF_ADMIN_PASSWORD, NO_START=1
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/WillIsback/litellm-vllm-observability.git}"
DIR="${INSTALL_DIR:-$PWD/litellm-vllm-observability}"

# --- affichage ---------------------------------------------------------------
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  B=$'\e[1m'; D=$'\e[2m'; R=$'\e[31m'; G=$'\e[32m'; Y=$'\e[33m'; C=$'\e[36m'; Z=$'\e[0m'
else B= D= R= G= Y= C= Z=; fi
TTY=0; ( : </dev/tty ) 2>/dev/null && TTY=1
say()  { printf '%s\n' "$*"; }
ok()   { say "${G}✓${Z} $*"; }
warn() { say "${Y}!${Z} $*"; }
die()  { say "${R}✗${Z} $*" >&2; exit 1; }
step() { say; say "${B}${C}▸ $*${Z}"; }

# ask VAR "Question" "défaut" ["hint multi-lignes"]   (ask_secret : saisie masquée)
ask() {
  local v="${!1:-}"
  if [ -z "$v" ] && [ "$TTY" = 1 ]; then
    [ -n "${4:-}" ] && printf '%s\n' "${D}$4${Z}" >/dev/tty
    printf '%s?%s %s %s[%s]%s : ' "$B" "$Z" "$2" "$D" "$3" "$Z" >/dev/tty
    read -r v </dev/tty || true
  fi
  printf -v "$1" '%s' "${v:-$3}"
}
ask_secret() {
  local v="${!1:-}"
  if [ -z "$v" ] && [ "$TTY" = 1 ]; then
    [ -n "${4:-}" ] && printf '%s\n' "${D}$4${Z}" >/dev/tty
    printf '%s?%s %s %s(saisie masquée, Entrée = valeur par défaut)%s : ' "$B" "$Z" "$2" "$D" "$Z" >/dev/tty
    read -rs v </dev/tty || true; printf '\n' >/dev/tty
  fi
  printf -v "$1" '%s' "${v:-$3}"
}
setenv() { # setenv CLÉ VALEUR (sans interprétation des caractères spéciaux)
  local t; t=$(mktemp)
  V="$2" awk -v k="$1" 'BEGIN{v=ENVIRON["V"]} index($0,k"=")==1{print k"="v; next} {print}' .env >"$t" && cat "$t" >.env && rm -f "$t"
}
clean_addr() { # retire espaces, crochets (copiés depuis l'affichage du défaut), guillemets et http://
  local v="${1//[\[\] \"\']/}"; v="${v#http://}"; printf '%s' "${v%/}"
}
http_code() { # http_code hôte:port chemin [clé API] -> code HTTP (000 si injoignable)
  local auth=(); [ -n "${3:-}" ] && auth=(-H "Authorization: Bearer $3")
  curl -s -m 3 -o /dev/null -w '%{http_code}' "${auth[@]}" "http://$1$2" 2>/dev/null || true
}
probe() { # probe hôte:port chemin [clé API]
  command -v curl >/dev/null || return 0
  local code; code=$(http_code "$@")
  case "$code" in
    2*) ok "$1 répond sur $2" ;;
    401|403) warn "$1 refuse l'accès ($code) : une clé API est requise pour $2" ;;
    *) warn "$1 ne répond pas depuis cet hôte (normal si l'adresse n'est valable que depuis un conteneur)" ;;
  esac
}
enable_auth() { # enable_auth job fichier-clé : décommente le bloc authorization du job dans prometheus.yml
  sed -i -E "/job_name: $1\$/,/^  - job_name/ s/^(    )# (authorization:|  type: Bearer|  credentials_file: .*$2)\$/\1\2/" prometheus/prometheus.yml
}

say "${B}LiteLLM + vLLM observability${Z} — installation"
step "Vérification des prérequis"
command -v git >/dev/null    || die "git est requis"
command -v docker >/dev/null || die "docker est requis"
docker compose version >/dev/null 2>&1 || die "docker compose (v2) est requis"
ok "git, docker, docker compose"

step "Récupération du dépôt"
if [ -d "$DIR/.git" ]; then git -C "$DIR" pull -q --ff-only --autostash && ok "mis à jour : $DIR"
else git clone -q --depth 1 "$REPO_URL" "$DIR" && ok "cloné dans $DIR"; fi
cd "$DIR"

# valeurs déjà configurées (réinstallation) : servent de défaut
CUR_LITELLM="$(grep -o '"[^"]*"' prometheus/targets/litellm.yml 2>/dev/null | tr -d '"' | head -1)" || true
CUR_VLLM="$(awk '/targets:/{gsub(/.*\["|"\].*/,"");t=$0} /provider:/{print $2"="t}' prometheus/targets/vllm.yml 2>/dev/null | paste -sd, -)" || true
[ "$CUR_LITELLM" = "10.0.0.10:4000" ] && CUR_LITELLM=""      # valeurs d'exemple du dépôt : ignorées
[ "$CUR_VLLM" = "stack-a=10.0.0.21:8000,stack-b=10.0.0.31:8000" ] && CUR_VLLM=""

step "LiteLLM"
if [ -z "${USE_LITELLM:-}" ]; then
  case "${LITELLM_TARGET:-}" in none|non|no|0) USE_LITELLM=n;; esac
fi
ask USE_LITELLM "Utilisez-vous une gateway LiteLLM ? (o/n)" "o" \
"Si vous n'avez que des moteurs vLLM (sans gateway), répondre n : le suivi par team/utilisateur/clé sera absent,
  seuls les dashboards et le rapport « moteurs » seront alimentés."
case "$USE_LITELLM" in n|N|non|no|0) USE_LITELLM=0;; *) USE_LITELLM=1;; esac

if [ ! -f .env ]; then
  step "Grafana"
  GEN="$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 16)"
  ask_secret GF_ADMIN_PASSWORD "Mot de passe admin Grafana" "$GEN" "Utilisateur : admin. Laisser vide pour utiliser un mot de passe généré (affiché à la fin)."
  cp .env.example .env
  setenv GF_ADMIN_PASSWORD "$GF_ADMIN_PASSWORD"
  setenv HOST_UID "$(id -u)"
  setenv HOST_GID "$(id -g)"
  chmod 600 .env
  ok ".env créé (droits 600)"
  NEW_ENV=1
else
  ok ".env existant conservé"
fi

if [ "$USE_LITELLM" = 1 ]; then
  if [ "${NEW_ENV:-0}" = 1 ]; then
    step "Postgres de LiteLLM (données exactes par requête)"
    PGHINT="Où trouver ces valeurs : DATABASE_URL de LiteLLM (postgresql://USER:MDP@HÔTE:5432/BASE)
  → docker inspect <conteneur-litellm> | grep DATABASE_URL    ou    grep DATABASE_URL .env docker-compose.yml
  Le rôle doit être en lecture seule (voir README, section Configuration)."
    ask LITELLM_PG_HOST "Hôte:port Postgres" "10.0.0.10:5432" "$PGHINT"
    ask LITELLM_PG_DB   "Nom de la base" "litellm" "Dernier segment de DATABASE_URL (souvent « litellm »)."
    ask LITELLM_PG_USER "Rôle lecture seule" "grafana_ro" "Rôle créé avec les GRANT SELECT du README (ne pas utiliser le rôle admin de LiteLLM)."
    ask_secret LITELLM_PG_PASSWORD "Mot de passe de ce rôle" "change-me"
    setenv LITELLM_PG_HOST "$LITELLM_PG_HOST"
    setenv LITELLM_PG_DB "$LITELLM_PG_DB"
    setenv LITELLM_PG_USER "$LITELLM_PG_USER"
    setenv LITELLM_PG_PASSWORD "$LITELLM_PG_PASSWORD"
  fi

  step "Cible LiteLLM (métriques Prometheus)"
  ask LITELLM_TARGET "Adresse du proxy LiteLLM (hôte:port)" "${CUR_LITELLM:-10.0.0.10:4000}" \
"Adresse vue depuis Docker. LiteLLM sur la même machine : host.docker.internal:4000 (sinon IP:port).
  Port : « port » du démarrage de LiteLLM (4000 par défaut) ou docker ps | grep litellm.
  Prérequis : callbacks: [\"prometheus\"] dans litellm_settings. Test : curl http://HÔTE:4000/metrics/"
  LITELLM_TARGET="$(clean_addr "$LITELLM_TARGET")"
  printf -- '- targets: ["%s"]\n  labels:\n    service: litellm-gateway\n' "$LITELLM_TARGET" > prometheus/targets/litellm.yml
  probe "$LITELLM_TARGET" /metrics/
  case "$(http_code "$LITELLM_TARGET" /metrics/)" in 401|403) LITELLM_AUTH=1;; esac

  if [ "${LITELLM_AUTH:-0}" = 1 ] || [ -n "${LITELLM_METRICS_KEY:-}" ]; then
    ask_secret LITELLM_METRICS_KEY "Clé API LiteLLM pour /metrics" "" \
"/metrics est protégé (require_auth_for_metrics_endpoint: true).
  Où la trouver : UI LiteLLM → Virtual Keys → Create key (une clé dédiée suffit), ou la master key (LITELLM_MASTER_KEY).
  Elle est stockée dans prometheus/secrets/litellm_metrics_key (hors git)."
    if [ -n "$LITELLM_METRICS_KEY" ]; then
      printf '%s' "$LITELLM_METRICS_KEY" > prometheus/secrets/litellm_metrics_key
      chmod 644 prometheus/secrets/litellm_metrics_key   # lu par le conteneur Prometheus (utilisateur nobody)
      enable_auth litellm litellm_metrics_key
      ok "clé enregistrée, authentification Bearer activée dans prometheus.yml"
      probe "$LITELLM_TARGET" /metrics/ "$LITELLM_METRICS_KEY"
    fi
  fi
else
  printf -- '[]\n' > prometheus/targets/litellm.yml
  ok "LiteLLM ignoré : aucune cible gateway"
fi

mkdir -p prometheus/secrets
step "Cibles vLLM (une par stack)"
ask VLLM_TARGETS "Masters vLLM (nom=hôte:port, séparés par des virgules)" "${CUR_VLLM:-stack-a=10.0.0.21:8000,stack-b=10.0.0.31:8000}" \
"Un « nom=hôte:port » par stack : le master qui expose l'API (port 8000 par défaut). Le nom sert dans Grafana.
  Exemple : dgx=192.168.1.87:8000 (sans crochets). Retrouver l'adresse : commande « vllm serve » (--port), docker ps,
  ou api_base dans le config.yaml de LiteLLM (model_list → litellm_params), sans le /v1.
  Test : curl http://HÔTE:8000/metrics"
: > prometheus/targets/vllm.yml
IFS=',' read -ra T <<< "$VLLM_TARGETS"
for t in "${T[@]}"; do
  t="$(clean_addr "$t")"; [ -n "$t" ] || continue
  case "$t" in *=*) ;; *) die "format attendu nom=hôte:port (ex. dgx=192.168.1.87:8000), reçu : $t";; esac
  printf -- '- targets: ["%s"]\n  labels:\n    provider: %s\n    api_base: http://%s/v1\n' "${t#*=}" "${t%%=*}" "${t#*=}" >> prometheus/targets/vllm.yml
  probe "${t#*=}" /metrics
  case "$(http_code "${t#*=}" /metrics)" in 2*) VLLM_UP=1;; 401|403) VLLM_AUTH=1;; esac
done
if [ "${VLLM_AUTH:-0}" = 1 ] || [ -n "${VLLM_METRICS_KEY:-}" ]; then
  ask_secret VLLM_METRICS_KEY "Clé API vLLM pour /metrics" "" \
"/metrics est protégé sur au moins un master. Utiliser la valeur de --api-key (ou VLLM_API_KEY) du serveur vLLM.
  Une seule clé est supportée : si les stacks ont des clés différentes, uniformiser ou ne scraper que celles-ci."
  if [ -n "$VLLM_METRICS_KEY" ]; then
    printf '%s' "$VLLM_METRICS_KEY" > prometheus/secrets/vllm_metrics_key
    chmod 644 prometheus/secrets/vllm_metrics_key
    enable_auth vllm vllm_metrics_key
    ok "clé vLLM enregistrée, authentification Bearer activée"
    for t in "${T[@]}"; do t="$(clean_addr "$t")"; [ -n "$t" ] && probe "${t#*=}" /metrics "$VLLM_METRICS_KEY"; done
  fi
elif [ "${VLLM_UP:-0}" = 1 ]; then
  ok "/metrics vLLM accessible sans clé : aucune clé nécessaire pour la collecte"
fi
ok "cibles Prometheus écrites (api_base doit être identique à celui du config.yaml LiteLLM)"

[ "${NO_START:-0}" = 1 ] && { say; say "Démarrer : ${B}cd $DIR && docker compose up -d${Z}"; exit 0; }

step "Démarrage de la stack"
docker compose up -d
# prometheus.yml / secrets ne sont relus qu'au rechargement (les fichiers de cibles, eux, sont rechargés à chaud)
docker compose kill -s SIGHUP prometheus >/dev/null 2>&1 && ok "configuration Prometheus rechargée"
PROM_PORT="$(grep '^PROM_PORT=' .env | cut -d= -f2)"; PROM_PORT="${PROM_PORT:-9090}"
if command -v curl >/dev/null; then
  say "${D}Attente du premier scrape des cibles…${Z}"
  for _ in $(seq 1 20); do
    TG="$(curl -s -m 3 "http://localhost:$PROM_PORT/api/v1/targets?state=active" 2>/dev/null || true)"
    case "$TG" in *'"health":"unknown"'*|"") sleep 2;; *) break;; esac
  done
  # cibles en erreur : afficher la raison renvoyée par Prometheus
  printf '%s' "$TG" | grep -o '"scrapeUrl":"[^"]*"[^}]*"lastError":"[^"]\+"' | sed -E 's/"scrapeUrl":"([^"]*)".*"lastError":"([^"]*)"/\1 → \2/' | while read -r l; do warn "cible en erreur : $l"; done || true
fi
step "Vérification des métriques"
docker compose run --rm reporter check || warn "check en erreur : voir la sortie ci-dessus (les cibles peuvent mettre ~30 s à passer UP)"

PORT="$(grep '^GF_PORT=' .env | cut -d= -f2)"
step "Terminé"
say "Grafana : ${B}http://localhost:${PORT:-3000}${Z}  (utilisateur ${B}admin${Z}, dossier « LLM Observability »)"
[ -n "${GEN:-}" ] && [ "${GF_ADMIN_PASSWORD:-}" = "$GEN" ] && say "Mot de passe généré : ${B}$GF_ADMIN_PASSWORD${Z}  ${D}(stocké dans $DIR/.env)${Z}"
say "Rapport : ${D}cd $DIR && docker compose run --rm reporter --previous-month${Z}"
