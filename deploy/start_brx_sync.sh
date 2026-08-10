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
#   EXPECTED_AWS_ACCOUNT_ID=<12 digits> ./start_brx_sync.sh
# ============================================================================

set -euo pipefail

# ── Configurazione non sensibile ──────────────────────────────────────────────
AWS_REGION="eu-south-1"
ECR_REGISTRY=""
COMPOSE_FILE="docker-compose.prod.yml"
export CARDTRADER_WRITES_ENABLED="${CARDTRADER_WRITES_ENABLED:-false}"

REDIS_URL_VALUE="redis://brx-sync-redis:6379"
WEBHOOK_PUBLIC_URL_VALUE="https://sync.ebartex.com"
RDS_CA_URL="https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem"
RDS_CA_FILE="certs/aws-rds-global-bundle.pem"
RDS_CA_TMP="${RDS_CA_FILE}.tmp"

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
  unset DB_USER DB_PASS DB_PASS_ENC
  unset DB_HOST DB_NAME DB_USER_ENC DB_NAME_ENC
  unset SYNC_MIGRATION_DB_HOST SYNC_MIGRATION_DB_NAME
  unset SYNC_MIGRATION_DB_USER SYNC_MIGRATION_DB_PASSWORD
  unset MYSQL_HOST MYSQL_USER MYSQL_PASSWORD MYSQL_DATABASE
  unset REDIS_URL
  unset FERNET_KEY
  unset JWT_PUBLIC_KEY
  unset INTERNAL_API_TOKEN
  unset FERNET_PREVIOUS_KEYS
  unset INTERNAL_CALLER_TOKENS
  unset PUBLIC_BASE_URL
  unset BRX_SYNC_IMAGE
  unset REDIS_IMAGE
  rm -f "${RDS_CA_TMP}"
  if [[ -n "${ECR_REGISTRY:-}" ]]; then
    docker logout "$ECR_REGISTRY" >/dev/null 2>&1 || true
  fi
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

command -v curl >/dev/null 2>&1 \
  && log_ok "curl trovato" \
  || { log_err "curl non trovato"; exit 1; }

command -v openssl >/dev/null 2>&1 \
  && log_ok "openssl trovato" \
  || { log_err "openssl non trovato"; exit 1; }

docker compose version >/dev/null 2>&1 \
  && log_ok "docker compose v2 trovato" \
  || { log_err "docker compose v2 non trovato — installa con: apt-get install docker-compose-v2"; exit 1; }

[[ -f "$COMPOSE_FILE" ]] \
  && log_ok "$COMPOSE_FILE trovato" \
  || { log_err "File non trovato: $COMPOSE_FILE (esegui questo script dalla stessa cartella)"; exit 1; }

log_step "Aggiorno il bundle CA ufficiale Amazon RDS..."
install -d -m 0755 "$(dirname "$RDS_CA_FILE")"
curl --fail --silent --show-error --location \
  --proto '=https' --tlsv1.2 \
  "$RDS_CA_URL" \
  --output "$RDS_CA_TMP"
if ! openssl crl2pkcs7 -nocrl -certfile "$RDS_CA_TMP" \
    | openssl pkcs7 -print_certs -noout \
    | grep -Eq "CN ?= ?Amazon RDS eu-south-1 Root CA RSA2048 G1"; then
  log_err "Il bundle Amazon RDS non contiene la CA attesa per eu-south-1"
  exit 1
fi
chmod 0644 "$RDS_CA_TMP"
mv -f "$RDS_CA_TMP" "$RDS_CA_FILE"
log_ok "Bundle CA Amazon RDS verificato"

# Verifica identity AWS (usa instance profile, zero credenziali su disco)
log_step "Verifico credenziali AWS (instance profile)..."
: "${EXPECTED_AWS_ACCOUNT_ID:?EXPECTED_AWS_ACCOUNT_ID deploy configuration is required}"
[[ "$EXPECTED_AWS_ACCOUNT_ID" =~ ^[0-9]{12}$ ]] \
  || { log_err "EXPECTED_AWS_ACCOUNT_ID must be a 12-digit AWS account id"; exit 1; }
CALLER_ACCOUNT_ID=$(aws sts get-caller-identity \
  --query Account --output text --region "$AWS_REGION") \
  || { log_err "Unable to verify the active AWS deploy identity"; exit 1; }
[[ "$CALLER_ACCOUNT_ID" =~ ^[0-9]{12}$ ]] \
  && [[ "$CALLER_ACCOUNT_ID" == "$EXPECTED_AWS_ACCOUNT_ID" ]] \
  || { log_err "Active AWS identity is not authorized for this deployment"; exit 1; }
ECR_REGISTRY="${CALLER_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
unset CALLER_ACCOUNT_ID
export ECR_REGISTRY
IMAGE_DIGEST="${IMAGE_DIGEST:?set IMAGE_DIGEST to the approved sha256 digest}"
[[ "$IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] \
  || { log_err "IMAGE_DIGEST must be sha256:<64 lowercase hex>"; exit 1; }
export BRX_SYNC_IMAGE="${ECR_REGISTRY}/ebartex-sync@${IMAGE_DIGEST}"
REDIS_IMAGE="${REDIS_IMAGE:?set REDIS_IMAGE to the approved image@sha256 digest}"
[[ "$REDIS_IMAGE" =~ ^[^[:space:]@]+@sha256:[0-9a-f]{64}$ ]] \
  || { log_err "REDIS_IMAGE must be an immutable image@sha256 digest"; exit 1; }
export REDIS_IMAGE
log_ok "Identita AWS e registry ECR derivato verificati"

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

validate_database_host() {
  local label="$1"
  local value="$2"
  local expected_suffix=".${AWS_REGION}.rds.amazonaws.com"
  [[ "$value" =~ ^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$ ]] \
    && [[ "$value" != *..* ]] \
    && [[ "$value" == *"$expected_suffix" ]] \
    || { log_err "$label hostname from SSM is outside the RDS allowlist"; exit 1; }
  python3 -c 'import ipaddress,socket,sys; values={item[4][0] for item in socket.getaddrinfo(sys.argv[1], None)}; assert values; addresses=[ipaddress.ip_address(item) for item in values]; assert all(address.is_private and not (address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified) for address in addresses)' "$value" \
    || { log_err "$label must resolve only to private network addresses"; exit 1; }
}

validate_database_name() {
  local label="$1"
  local value="$2"
  [[ "$value" =~ ^[A-Za-z_][A-Za-z0-9_]{0,62}$ ]] \
    || { log_err "$label database name from SSM is invalid"; exit 1; }
}

# ── Parametri da SSM condivisi (/prod/ebartex/*) ──────────────────────────────
log_step "Dedicated brx_sync PostgreSQL credentials..."
DB_HOST="$(get_ssm "/prod/ebartex/auth_db_host")"
DB_NAME="$(get_ssm "/prod/ebartex/auth_db_name")"
DB_USER="$(get_ssm "/prod/ebartex/brx_sync_db_user")"
DB_PASS="$(get_ssm "/prod/ebartex/brx_sync_db_password")"
validate_database_host "PostgreSQL" "$DB_HOST"
validate_database_name "PostgreSQL" "$DB_NAME"
[[ "$DB_USER" != "postgres" && "$DB_USER" != "admin" && "$DB_USER" != "root" && "$DB_USER" != "brx_bd_admin" ]] \
  || { log_err "brx_sync PostgreSQL role must be dedicated and least-privilege"; exit 1; }
DB_PASS_ENC="$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=""))' "$DB_PASS")"
DB_USER_ENC="$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=""))' "$DB_USER")"
DB_NAME_ENC="$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=""))' "$DB_NAME")"
export DATABASE_URL="postgresql+asyncpg://${DB_USER_ENC}:${DB_PASS_ENC}@${DB_HOST}:5432/${DB_NAME_ENC}"
log_ok "DATABASE_URL costruito da parametri SSM validati"

log_step "Dedicated brx_sync PostgreSQL migration credentials..."
export SYNC_MIGRATION_DB_HOST="$DB_HOST"
export SYNC_MIGRATION_DB_NAME="$DB_NAME"
export SYNC_MIGRATION_DB_USER="$(get_ssm "/prod/ebartex/brx_sync_migration_db_user")"
export SYNC_MIGRATION_DB_PASSWORD="$(get_ssm "/prod/ebartex/brx_sync_migration_db_password")"
[[ "$SYNC_MIGRATION_DB_USER" =~ ^brx_sync_migration_[a-z0-9_]{1,44}$ ]] \
  || { log_err "brx_sync migration role must be dedicated and explicitly named"; exit 1; }
[[ "$SYNC_MIGRATION_DB_USER" != "$DB_USER" ]] \
  || { log_err "Runtime and migration PostgreSQL roles must be distinct"; exit 1; }
[[ ${#SYNC_MIGRATION_DB_PASSWORD} -ge 32 ]] \
  || { log_err "brx_sync migration password must contain at least 32 characters"; exit 1; }
log_ok "Credenziali migration PostgreSQL distinte e validate"

log_step "Dedicated read-only brx_sync MySQL credentials..."
export MYSQL_HOST="$(get_ssm "/prod/ebartex/search_mysql_host")"
export MYSQL_USER="$(get_ssm "/prod/ebartex/brx_sync_mysql_user")"
[[ "$MYSQL_USER" != "root" && "$MYSQL_USER" != "admin" && "$MYSQL_USER" != "brx_bd_admin" ]] \
  || { log_err "brx_sync MySQL role must be dedicated and read-only"; exit 1; }
export MYSQL_PASSWORD="$(get_ssm "/prod/ebartex/brx_sync_mysql_password")"
export MYSQL_DATABASE="$(get_ssm "/prod/ebartex/search_mysql_database")"
validate_database_host "MySQL" "$MYSQL_HOST"
validate_database_name "MySQL" "$MYSQL_DATABASE"
log_ok "MySQL credentials e destinazione recuperate da SSM e validate"

# Produzione sync usa sync_encryption_key (token CT cifrati con questa chiave).
# NON usare fernet_key se i due hash SSM differiscono — altrimenti i token utente non si decifrano.
log_step "FERNET_KEY (/prod/ebartex/sync_encryption_key)..."
export FERNET_KEY="$(get_ssm "/prod/ebartex/sync_encryption_key")"
log_ok "FERNET_KEY recuperato (sync_encryption_key)"

log_step "Chiave Fernet legacy per la sola rotazione..."
LEGACY_FERNET_KEY="$(get_ssm "/prod/ebartex/fernet_key")"
if [[ "$LEGACY_FERNET_KEY" != "$FERNET_KEY" ]]; then
  export FERNET_PREVIOUS_KEYS="$LEGACY_FERNET_KEY"
else
  export FERNET_PREVIOUS_KEYS=""
fi
unset LEGACY_FERNET_KEY
log_ok "Compatibilita Fernet legacy configurata per il job one-shot"

log_step "JWT_PUBLIC_KEY (/prod/ebartex/jwt_public_key)..."
export JWT_PUBLIC_KEY="$(get_ssm "/prod/ebartex/jwt_public_key")"
log_ok "JWT_PUBLIC_KEY recuperato"

log_step "Caller map scoped per brx_sync..."
# Coordinated rollout: keep the `auction` token equal to
# /prod/ebartex/sync_internal_token until Auction has rotated to the new value.
export INTERNAL_CALLER_TOKENS="$(get_ssm "/prod/ebartex/sync_internal_caller_tokens")"
export INTERNAL_API_TOKEN=""
log_ok "Caller map scoped recuperata; token legacy disabilitato"

export REDIS_URL="$REDIS_URL_VALUE"
export PUBLIC_BASE_URL="$WEBHOOK_PUBLIC_URL_VALUE"
log_ok "Endpoint Redis configurato (valore non stampato)"
log_ok "PUBLIC_BASE_URL=${PUBLIC_BASE_URL}"

# ── Config non-sensitivi hardcoded ───────────────────────────────────────────
export AWS_REGION="$AWS_REGION"
export ENVIRONMENT="production"
export DEBUG="false"
export AWS_SSM_ENABLED="false"   # I segreti sono già stati caricati sopra
export MYSQL_PORT="3306"
export ALLOWED_ORIGINS="https://www.ebartex.com,https://ebartex.com"
export TRUSTED_HOSTS="${TRUSTED_HOSTS:-sync.ebartex.com,brx-sync-api,localhost,127.0.0.1}"
export JWT_ISSUER="${JWT_ISSUER:-ebartex-auth}"
export JWT_AUDIENCE="${JWT_AUDIENCE:-ebartex-services}"
export JWT_REQUIRE_ISSUER_AUDIENCE="${JWT_REQUIRE_ISSUER_AUDIENCE:-true}"
export JWT_REQUIRE_JTI="${JWT_REQUIRE_JTI:-true}"
export JWT_LEGACY_ROLLOUT_ACK="${JWT_LEGACY_ROLLOUT_ACK:-}"
export JWT_LEGACY_ROLLOUT_EXPIRES_AT="${JWT_LEGACY_ROLLOUT_EXPIRES_AT:-}"
export JWT_MAX_ACCESS_TOKEN_SECONDS="${JWT_MAX_ACCESS_TOKEN_SECONDS:-3660}"
export INTERNAL_API_ALLOWED_CIDRS="${INTERNAL_API_ALLOWED_CIDRS:-127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16}"

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

# Migrations and credential rewrites run without old API/worker processes racing
# on the shared tables. Redis remains available and durable.
docker compose -f "$COMPOSE_FILE" stop brx-sync-api brx-sync-worker

log_header "MIGRAZIONI DATABASE"

log_step "Applico lo schema tramite il job DB-only..."
docker compose -f "$COMPOSE_FILE" run --rm --no-deps brx-sync-schema-migrate
log_ok "Migrazioni schema applicate dal ruolo migration dedicato"

log_step "Cifro i webhook secret legacy e ruoto le credenziali Fernet..."
docker compose -f "$COMPOSE_FILE" run --rm --no-deps brx-sync-credential-rotation
log_ok "Credenziali cifrate con la chiave primaria"
unset FERNET_PREVIOUS_KEYS

# ── Avvio container ───────────────────────────────────────────────────────────
log_header "AVVIO CONTAINER"

log_step "Avvio brx-sync (API + worker + redis)..."
docker compose -f "$COMPOSE_FILE" up -d --force-recreate --remove-orphans
log_ok "Container avviati"

# ── Health check ──────────────────────────────────────────────────────────────
log_header "HEALTH CHECK"

log_step "Attendo readiness verificata..."
ready="false"
for i in $(seq 1 20); do
  if curl -sf --max-time 5 http://localhost:8002/health/ready >/dev/null 2>&1; then
    ready="true"
    break
  fi
  [[ $i -lt 20 ]] && sleep 3
done
if [[ "$ready" != "true" ]]; then
  log_err "Readiness non raggiunta; deploy fallito"
  docker compose -f "$COMPOSE_FILE" logs --tail=80 brx-sync-api brx-sync-worker
  exit 1
fi
log_ok "API e dipendenze sono ready"

# ── Riepilogo ─────────────────────────────────────────────────────────────────
log_header "BRX-SYNC ONLINE"

echo ""
echo -e "${GREEN}Il servizio brx-sync e' attivo!${NC}"
echo ""
echo "  API:         ${PUBLIC_BASE_URL}"
echo "  Health live: http://localhost:8002/health/live"
echo "  Health full: http://localhost:8002/health"
echo "  Docs:        disabilitate in produzione"
echo ""
echo "  Segreti:     AWS SSM Parameter Store (solo in memoria, mai su disco)"
echo "  Region:      ${AWS_REGION}"
echo ""
echo "  Log live:    docker compose -f ${COMPOSE_FILE} logs -f brx-sync-api"
echo "  Worker log:  docker compose -f ${COMPOSE_FILE} logs -f brx-sync-worker"
echo "  Stop:        docker compose -f ${COMPOSE_FILE} down"
echo ""
