#!/usr/bin/env bash
# ============================================================================
# BRX-SYNC — Production Startup Script
#
# - Tutti i segreti vengono letti da AWS SSM Parameter Store IN MEMORIA
# - NESSUN file .env viene creato sul disco
# - Le variabili vengono passate ai container tramite il processo host
# - Richiede: AWS CLI v2, Docker + Compose v2, instance profile con permessi:
#     ssm:GetParameter, ecr:GetAuthorizationToken, ecr:BatchGetImage
#
# Utilizzo:
#   chmod +x start_brx_sync.sh
#   ./start_brx_sync.sh
# ============================================================================

set -euo pipefail

# ── Configurazione (non-secret, hardcoded) ───────────────────────────────────
AWS_REGION="eu-south-1"
ECR_REGISTRY="000876600482.dkr.ecr.eu-south-1.amazonaws.com"
COMPOSE_FILE="docker-compose.prod.yml"

# Stesso pattern di auth / search / docker-compose.prod.yml root
DB_HOST="ebartex-db-postgres.czuw0wy289sx.eu-south-1.rds.amazonaws.com"
DB_USER="brx_bd_admin"
DB_NAME="ebartex_auth_db"
MYSQL_HOST_VALUE="ebartex-cards-db.czuw0wy289sx.eu-south-1.rds.amazonaws.com"
MYSQL_USER_VALUE="admin"
MYSQL_DATABASE_VALUE="ebartex_items"
REDIS_URL_VALUE="redis://brx-sync-redis:6379"
WEBHOOK_PUBLIC_URL_VALUE="https://sync.ebartex.com"

# ── Colors ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_header() {
  echo -e "\n${BLUE}═══════════════════════════════════════════════════${NC}"
  echo -e "${BLUE}  $1${NC}"
  echo -e "${BLUE}═══════════════════════════════════════════════════${NC}"
}
log_step()  { echo -e "${YELLOW}▶ [$(date '+%H:%M:%S')] $1${NC}"; }
log_ok()    { echo -e "${GREEN}✓ $1${NC}"; }
log_err()   { echo -e "${RED}✗ $1${NC}"; }

# ── Cleanup: rimuove variabili sensibili dalla memoria all'uscita ─────────────
cleanup() {
  unset DATABASE_URL
  unset MYSQL_HOST MYSQL_USER MYSQL_PASSWORD MYSQL_DATABASE
  unset REDIS_URL
  unset FERNET_KEY
  unset JWT_PUBLIC_KEY
  unset INTERNAL_API_TOKEN
  unset WEBHOOK_PUBLIC_URL
}
trap cleanup EXIT

# ── Pre-flight ────────────────────────────────────────────────────────────────
log_header "PRE-FLIGHT CHECKS"

command -v aws    >/dev/null 2>&1 \
  && log_ok "aws cli trovato" \
  || { log_err "aws cli non trovato — installa con: apt-get install awscli"; exit 1; }

command -v docker >/dev/null 2>&1 \
  && log_ok "docker trovato" \
  || { log_err "docker non trovato"; exit 1; }

docker compose version >/dev/null 2>&1 \
  && log_ok "docker compose v2 trovato" \
  || { log_err "docker compose v2 non trovato — installa con: apt-get install docker-compose-v2"; exit 1; }

[[ -f "$COMPOSE_FILE" ]] \
  && log_ok "$COMPOSE_FILE trovato" \
  || { log_err "File non trovato: $COMPOSE_FILE (esegui questo script dalla stessa cartella)"; exit 1; }

# Verifica identity AWS (usa instance profile, zero credenziali su disco)
log_step "Verifico credenziali AWS (instance profile)..."
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text --region "$AWS_REGION")
log_ok "Account AWS: ${ACCOUNT_ID} — credenziali instance profile OK"

# ── Helper SSM ────────────────────────────────────────────────────────────────
log_header "CONFIGURAZIONE RUNTIME — SEGRETI DA SSM"

get_ssm() {
  aws ssm get-parameter \
    --name "$1" \
    --with-decryption \
    --query "Parameter.Value" \
    --output text \
    --region "$AWS_REGION"
}

# ── Parametri da SSM condivisi (/prod/ebartex/*) ──────────────────────────────
log_step "DB password (/prod/ebartex/db_password)..."
DB_PASS="$(get_ssm "/prod/ebartex/db_password")"
DB_PASS_ENC="$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=""))' "$DB_PASS")"
export DATABASE_URL="postgresql+asyncpg://${DB_USER}:${DB_PASS_ENC}@${DB_HOST}:5432/${DB_NAME}"
log_ok "DATABASE_URL costruito per ${DB_NAME}@${DB_HOST}"

log_step "MySQL password (/prod/ebartex/mysql_password)..."
export MYSQL_HOST="$MYSQL_HOST_VALUE"
export MYSQL_USER="$MYSQL_USER_VALUE"
export MYSQL_PASSWORD="$(get_ssm "/prod/ebartex/mysql_password")"
export MYSQL_DATABASE="$MYSQL_DATABASE_VALUE"
log_ok "MySQL credentials recuperate (host=${MYSQL_HOST}, db=${MYSQL_DATABASE})"

# Produzione sync usa sync_encryption_key (token CT cifrati con questa chiave).
# NON usare fernet_key se i due hash SSM differiscono — altrimenti i token utente non si decifrano.
log_step "FERNET_KEY (/prod/ebartex/sync_encryption_key)..."
export FERNET_KEY="$(get_ssm "/prod/ebartex/sync_encryption_key")"
log_ok "FERNET_KEY recuperato (sync_encryption_key)"

log_step "JWT_PUBLIC_KEY (/prod/ebartex/jwt_public_key)..."
export JWT_PUBLIC_KEY="$(get_ssm "/prod/ebartex/jwt_public_key")"
log_ok "JWT_PUBLIC_KEY recuperato"

log_step "INTERNAL_API_TOKEN (/prod/ebartex/internal_api_token)..."
export INTERNAL_API_TOKEN="$(get_ssm "/prod/ebartex/internal_api_token")"
log_ok "INTERNAL_API_TOKEN recuperato"

export REDIS_URL="$REDIS_URL_VALUE"
export WEBHOOK_PUBLIC_URL="$WEBHOOK_PUBLIC_URL_VALUE"
log_ok "REDIS_URL=${REDIS_URL}"
log_ok "WEBHOOK_PUBLIC_URL=${WEBHOOK_PUBLIC_URL}"

# ── Config non-sensitivi hardcoded ───────────────────────────────────────────
export AWS_REGION="$AWS_REGION"
export ENVIRONMENT="production"
export DEBUG="false"
export AWS_SSM_ENABLED="false"   # I segreti sono già stati caricati sopra
export MYSQL_PORT="3306"
export ALLOWED_ORIGINS="https://www.ebartex.com,https://ebartex.com,https://main.d8ry9s45st8bf.amplifyapp.com"

log_ok "Configurazione runtime completa"

# ── ECR Login ─────────────────────────────────────────────────────────────────
log_header "AUTENTICAZIONE ECR"

log_step "Login ECR tramite instance profile..."
aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "$ECR_REGISTRY"
log_ok "ECR autenticato"

# ── Pull immagine ─────────────────────────────────────────────────────────────
log_header "PULL IMMAGINE"

log_step "Scarico ultima immagine da ECR..."
docker compose -f "$COMPOSE_FILE" pull
log_ok "Immagine aggiornata"

log_header "MIGRAZIONI DATABASE"

log_step "Applico fondazioni inventario scambi..."
docker compose -f "$COMPOSE_FILE" run --rm --no-deps brx-sync-api \
  sh -c 'DB_URL=$(printf "%s" "$DATABASE_URL" | sed "s#^postgresql+asyncpg:#postgresql:#"); psql "$DB_URL" -v ON_ERROR_STOP=1 -f migrations/20260714_trade_inventory_foundations.sql'
log_ok "Migrazione inventario scambi applicata"

# ── Avvio container ───────────────────────────────────────────────────────────
log_header "AVVIO CONTAINER"

log_step "Avvio brx-sync (API + worker + redis)..."
docker compose -f "$COMPOSE_FILE" up -d --force-recreate --remove-orphans
log_ok "Container avviati"

# ── Health check ──────────────────────────────────────────────────────────────
log_header "HEALTH CHECK"

log_step "Attendo 20s per startup..."
sleep 20

if ! docker ps --filter "name=brx-sync-api" --filter "status=running" | grep -q brx-sync-api; then
  log_err "Il container brx-sync-api NON e' in esecuzione"
  echo ""
  echo "Log ultimi 40 righe:"
  docker compose -f "$COMPOSE_FILE" logs brx-sync-api | tail -40
  exit 1
fi
log_ok "Container brx-sync-api e' running"

log_step "Test endpoint health..."
for i in 1 2 3; do
  if curl -sf --max-time 5 http://localhost:8002/health/live >/dev/null 2>&1; then
    log_ok "Health endpoint risponde OK"
    break
  fi
  [[ $i -lt 3 ]] && { log_step "Retry ${i}/3..."; sleep 10; } || log_step "Health non ancora pronto — controlla i log sotto"
done

# ── Riepilogo ─────────────────────────────────────────────────────────────────
log_header "BRX-SYNC ONLINE"

echo ""
echo -e "${GREEN}Il servizio brx-sync e' attivo!${NC}"
echo ""
echo "  API:         http://$(curl -sf http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null || echo 'IP'):8002"
echo "  Health live: http://localhost:8002/health/live"
echo "  Health full: http://localhost:8002/health"
echo "  Docs:        http://localhost:8002/docs"
echo ""
echo "  Segreti:     AWS SSM Parameter Store (solo in memoria, mai su disco)"
echo "  Region:      ${AWS_REGION}"
echo ""
echo "  Log live:    docker compose -f ${COMPOSE_FILE} logs -f brx-sync-api"
echo "  Worker log:  docker compose -f ${COMPOSE_FILE} logs -f brx-sync-worker"
echo "  Stop:        docker compose -f ${COMPOSE_FILE} down"
echo ""
