#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  VELARIS One-Time Setup Script
#  Run once after a fresh OS install. Safe to re-run (idempotent).
#
#  Usage:  chmod +x setup-velaris.sh && ./setup-velaris.sh
# ═══════════════════════════════════════════════════════════════════

set -euo pipefail

HELIX_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ERRORS=0

# ── CLI flags (DB SDK — optional; enable non-interactive DB setup) ────────────
# --database <postgresql|mysql|mariadb>  pick the backend without the interactive prompt
# --db-host/-port/-name/-user/-password   MySQL (BYO) connection details
# --non-interactive               never prompt; use flags + defaults only
DB_BACKEND_ARG=""; DB_HOST_ARG=""; DB_PORT_ARG=""; DB_NAME_ARG=""; DB_USER_ARG=""; DB_PASSWORD_ARG=""; NONINTERACTIVE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --database)        DB_BACKEND_ARG="${2:-}"; shift 2 ;;
    --database=*)      DB_BACKEND_ARG="${1#*=}"; shift ;;
    --db-host)         DB_HOST_ARG="${2:-}"; shift 2 ;;
    --db-host=*)       DB_HOST_ARG="${1#*=}"; shift ;;
    --db-port)         DB_PORT_ARG="${2:-}"; shift 2 ;;
    --db-port=*)       DB_PORT_ARG="${1#*=}"; shift ;;
    --db-name)         DB_NAME_ARG="${2:-}"; shift 2 ;;
    --db-name=*)       DB_NAME_ARG="${1#*=}"; shift ;;
    --db-user)         DB_USER_ARG="${2:-}"; shift 2 ;;
    --db-user=*)       DB_USER_ARG="${1#*=}"; shift ;;
    --db-password)     DB_PASSWORD_ARG="${2:-}"; shift 2 ;;
    --db-password=*)   DB_PASSWORD_ARG="${1#*=}"; shift ;;
    --non-interactive) NONINTERACTIVE=1; shift ;;
    *) shift ;;
  esac
done
# MySQL-only selections (kept empty for postgresql so `set -u` is satisfied everywhere)
SEL_BACKEND=""; SEL_DB_HOST=""; SEL_DB_PORT=""; SEL_DB_NAME=""; SEL_DB_USER=""; SEL_DB_PASSWORD=""

red()    { echo -e "\033[0;31m$*\033[0m"; }
green()  { echo -e "\033[0;32m$*\033[0m"; }
yellow() { echo -e "\033[0;33m$*\033[0m"; }
bold()   { echo -e "\033[1m$*\033[0m"; }

fail()   { red "  ✗ $*"; ERRORS=$((ERRORS + 1)); }
ok()     { green "  ✓ $*"; }
warn()   { yellow "  ⚠ $*"; }
step()   { echo ""; bold "▶ $*"; }

echo "╔══════════════════════════════════════════╗"
echo "║         VELARIS One-Time Setup           ║"
echo "╚══════════════════════════════════════════╝"

# ── Step 1: Prerequisites ─────────────────────────────────────────
step "Step 1/9 — Checking prerequisites..."

sudo apt update

APT_PACKAGES=()

require_cmd() {
  local cmd="$1"
  local apt_pkg="$2"
  local label="${3:-$cmd}"

  if command -v "$cmd" &>/dev/null; then
    ok "$label found: $(command -v "$cmd")"
  else
    warn "$label not found — will install"
    APT_PACKAGES+=("$apt_pkg")
  fi
}

# Core packages
require_cmd curl curl "curl"
require_cmd git git "git"
require_cmd nc netcat-openbsd "netcat (nc)"
require_cmd fuser psmisc "fuser (psmisc)"
require_cmd node nodejs "Node.js"
require_cmd npm npm "npm"
require_cmd pg_isready postgresql-client "postgresql-client"

# Docker check (do NOT install docker.io automatically)
if command -v docker &>/dev/null; then
  ok "Docker found: $(command -v docker)"
else
  fail "Docker not found."
  echo "  Install Docker first:"
  echo "  https://docs.docker.com/engine/install/ubuntu/"
fi

# Install missing apt packages
if [ "${#APT_PACKAGES[@]}" -gt 0 ]; then
  step "Installing missing apt packages..."
  sudo apt install -y "${APT_PACKAGES[@]}"
  ok "Missing apt packages installed"
fi

# Install uv if missing
if command -v uv &>/dev/null; then
  ok "uv found: $(command -v uv)"
else
  step "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh

  export PATH="$HOME/.local/bin:$PATH"

  # Persist PATH for future shells
  if ! grep -q 'HOME/.local/bin' "$HOME/.bashrc"; then
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
  fi

  if command -v uv &>/dev/null; then
    ok "uv installed successfully"
  else
    fail "uv installation failed"
  fi
fi

# Final verification
echo ""
bold "Dependency summary:"

check_cmd() {
  local cmd="$1"
  local label="${2:-$1}"

  if command -v "$cmd" &>/dev/null; then
    ok "$label ready"
  else
    fail "$label still missing"
  fi
}

check_cmd uv       "uv"
check_cmd node     "Node.js"
check_cmd npm      "npm"
check_cmd docker   "Docker"
check_cmd nc       "netcat (nc)"
check_cmd fuser    "fuser (psmisc)"
check_cmd curl     "curl"
check_cmd git      "git"
check_cmd pg_isready "postgresql-client"

# Python 3.12 via uv
if command -v uv &>/dev/null; then
  if uv python find 3.12 &>/dev/null; then
    ok "Python 3.12 available via uv"
  else
    step "Installing Python 3.12 via uv..."
    uv python install 3.12
    ok "Python 3.12 installed"
  fi
fi

[ "$ERRORS" -gt 0 ] && {
  red "Fix the above errors and re-run."
  exit 1
}
# ── Step 1b: Generate infrastructure credentials ──────────────────
step "Generating infrastructure credentials..."

ENV_FILE="$HELIX_DIR/.env"

# Create .env from .env.example if it doesn't exist yet
if [ ! -f "$ENV_FILE" ]; then
  cp "$HELIX_DIR/.env.example" "$ENV_FILE"
  ok "Created .env from .env.example"
fi

# ── Database backend selection (DB SDK; set-once at install) ─────────────────
# Source of truth for the runner = velaris.yaml `database:`; the app reads the URL
# from .env. The DB engine is chosen ONCE here — switching later is a data migration
# (HxDBMigrate), never a setup re-run. "Already configured" = .env DATABASE_URL has
# been customized away from the placeholder (the same signal the URL block uses).
_yaml_get() {
  grep -E "^\s*${1}:" "$HELIX_DIR/velaris.yaml" 2>/dev/null \
    | head -1 | sed -E "s/.*${1}:[[:space:]]*[\"']?([^\"'#]+)[\"']?.*/\1/" | xargs || true
}
DB_CONFIGURED=0
if grep -q "^HELIX_CASE_DATABASE_URL=" "$ENV_FILE" 2>/dev/null \
   && ! grep -qE "^HELIX_CASE_DATABASE_URL=\s*$" "$ENV_FILE" 2>/dev/null \
   && ! grep -q   "^HELIX_CASE_DATABASE_URL=.*REPLACE" "$ENV_FILE" 2>/dev/null; then
  DB_CONFIGURED=1
fi

SEL_BACKEND="$(_yaml_get database)"; SEL_BACKEND="${SEL_BACKEND:-postgresql}"
if [ "$DB_CONFIGURED" = "1" ]; then
  ok "Database already configured ($SEL_BACKEND) — keeping it"
  warn "Switching database engines after install is a migration (HxDBMigrate), not a setup re-run"
  # Normalise DB_FAMILY even on a re-run — the migration/superadmin steps below test it.
  case "$SEL_BACKEND" in
    postgresql) DB_FAMILY=postgresql ;;
    mysql|mariadb) DB_FAMILY=mysql ;;
    *) fail "Unsupported database backend '$SEL_BACKEND' (allowed: postgresql, mysql, mariadb)"; exit 1 ;;
  esac
else
  if [ -n "$DB_BACKEND_ARG" ]; then
    SEL_BACKEND="$DB_BACKEND_ARG"
  elif [ "$NONINTERACTIVE" = "0" ] && [ -t 0 ]; then
    echo ""; bold "  Select the database backend (set once — switching later = HxDBMigrate):"
    echo "    1) postgresql  — bundled, default"
    echo "    2) mysql       — bring-your-own MySQL 8"
    echo "    3) mariadb     — bring-your-own MariaDB 10.6+ / 11.x"
    read -rp "  Choice [1]: " _choice || _choice=""
    case "$_choice" in
      2|mysql|MySQL)     SEL_BACKEND="mysql" ;;
      3|mariadb|MariaDB) SEL_BACKEND="mariadb" ;;
      *)                 SEL_BACKEND="postgresql" ;;
    esac
  fi
  # DB_FAMILY normalises the setup path: MariaDB reuses the entire MySQL path and
  # differs only in the client image. All MySQL-path branches below test DB_FAMILY.
  case "$SEL_BACKEND" in
    postgresql) DB_FAMILY=postgresql ;;
    mysql|mariadb) DB_FAMILY=mysql ;;
    *) fail "Unsupported database backend '$SEL_BACKEND' (allowed: postgresql, mysql, mariadb)"; exit 1 ;;
  esac

  if [ "$DB_FAMILY" = "mysql" ]; then
    _ask() {  # _ask VAR "prompt" "default" "flag-value"
      local __var="$1" __prompt="$2" __def="$3" __flag="$4" __v=""
      if [ -n "$__flag" ]; then __v="$__flag"
      elif [ "$NONINTERACTIVE" = "0" ] && [ -t 0 ]; then read -rp "  $__prompt [$__def]: " __v || __v=""; __v="${__v:-$__def}"
      else __v="$__def"; fi
      printf -v "$__var" '%s' "$__v"
    }
    _ask SEL_DB_HOST "MySQL host"          "127.0.0.1" "$DB_HOST_ARG"
    _ask SEL_DB_PORT "MySQL port"          "3306"      "$DB_PORT_ARG"
    _ask SEL_DB_NAME "MySQL database name" "velaris"   "$DB_NAME_ARG"
    _ask SEL_DB_USER "MySQL user"          "velaris"   "$DB_USER_ARG"
    if [ -n "$DB_PASSWORD_ARG" ]; then
      SEL_DB_PASSWORD="$DB_PASSWORD_ARG"
    elif [ "$NONINTERACTIVE" = "0" ] && [ -t 0 ]; then
      read -rsp "  MySQL password: " SEL_DB_PASSWORD || SEL_DB_PASSWORD=""; echo ""
    fi
    ok "MySQL selected: ${SEL_DB_USER}@${SEL_DB_HOST}:${SEL_DB_PORT}/${SEL_DB_NAME}"
  fi
fi

_write_if_missing() {
  local key="$1" val="$2"
  if ! grep -q "^${key}=" "$ENV_FILE" 2>/dev/null || grep -q "^${key}=$" "$ENV_FILE"; then
    # Remove any empty placeholder line first, then append
    sed -i "/^${key}=$/d" "$ENV_FILE"
    echo "${key}=${val}" >> "$ENV_FILE"
    ok "Generated ${key}"
  else
    ok "${key} already set — keeping existing value"
  fi
}

# Treat the publicly-known dev default as unset so a real secret is generated
# (older .env files created from .env.example may carry it as a non-empty value)
sed -i "s|^HELIX_CASE_AUTH_SECRET=helix-dev-secret-change-in-production$|HELIX_CASE_AUTH_SECRET=|" "$ENV_FILE"

# PostgreSQL (bundled): generate a random DB password. MySQL (BYO): store the
# operator-provided password verbatim — both the runner's mysql client and the app
# authenticate with it. Password lives only in .env/OpenBao, never in velaris.yaml.
if [ "$DB_CONFIGURED" = "0" ] && [ "$DB_FAMILY" = "mysql" ]; then
  sed -i "/^VELARIS_DB_PASSWORD=/d" "$ENV_FILE"
  echo "VELARIS_DB_PASSWORD=${SEL_DB_PASSWORD}" >> "$ENV_FILE"
  ok "VELARIS_DB_PASSWORD set (MySQL, operator-provided)"
else
  _write_if_missing VELARIS_DB_PASSWORD            "$(openssl rand -hex 16)"
fi
_write_if_missing VELARIS_MINIO_PASSWORD         "$(openssl rand -hex 24)"
_write_if_missing VELARIS_ADMIN_PASSWORD         "$(openssl rand -hex 16)"
_write_if_missing HELIX_CASE_AUTH_SECRET         "$(openssl rand -hex 32)"
_write_if_missing HELIX_CASE_STORAGE_MASTER_KEY  "$(openssl rand -hex 32)"
# HxVault (#19) master KEK that wraps per-tenant DEKs — 32 bytes hex. When unset,
# the app derives a dev KEK from auth_secret (with a startup warning); production
# should use a dedicated key so crypto-shredding is independent of auth_secret.
_write_if_missing VELARIS_CASE_KEK               "$(openssl rand -hex 32)"
_write_if_missing HELIX_CASE_STORAGE_BACKEND     "minio"

# Generate RSA-2048 key pair for JWT RS256 signing (idempotent — skipped if already set).
# The private key stays on this host only; the public key is used by all services to verify.
# Stored as double-quoted single-line values; python-dotenv expands \n back to real newlines.
if ! grep -q "^HELIX_CASE_AUTH_RSA_PRIVATE_KEY=" "$ENV_FILE" 2>/dev/null || \
   grep -q "^HELIX_CASE_AUTH_RSA_PRIVATE_KEY=$" "$ENV_FILE" 2>/dev/null; then

  echo -n "  Generating RSA-2048 key pair for JWT signing... "
  _TMP_DIR=$(mktemp -d)
  openssl genrsa -out "$_TMP_DIR/jwt_private.pem" 2048 2>/dev/null
  openssl rsa -in "$_TMP_DIR/jwt_private.pem" -pubout -out "$_TMP_DIR/jwt_public.pem" 2>/dev/null

  # Flatten PEM to single line with \n escapes so python-dotenv can store it in .env.
  # Double-quoted .env values: python-dotenv interprets \n as actual newlines on read.
  _PRIV_LINE=$(awk '{printf "%s\\n", $0}' "$_TMP_DIR/jwt_private.pem")
  _PUB_LINE=$(awk '{printf "%s\\n", $0}' "$_TMP_DIR/jwt_public.pem")

  # Remove any empty placeholder lines first, then append
  sed -i "/^HELIX_CASE_AUTH_RSA_PRIVATE_KEY=$/d" "$ENV_FILE"
  sed -i "/^HELIX_CASE_AUTH_RSA_PUBLIC_KEY=$/d"  "$ENV_FILE"
  echo "HELIX_CASE_AUTH_RSA_PRIVATE_KEY=\"${_PRIV_LINE}\"" >> "$ENV_FILE"
  echo "HELIX_CASE_AUTH_RSA_PUBLIC_KEY=\"${_PUB_LINE}\""   >> "$ENV_FILE"

  rm -rf "$_TMP_DIR"
  green "✓ RSA key pair generated and written to .env"
else
  ok "HELIX_CASE_AUTH_RSA_PRIVATE_KEY already set — keeping existing key pair"
fi

# Update DATABASE_URL in .env to use the generated password.
# Only fill it in when it's still the .env.example placeholder ("REPLACE") or
# empty/missing — NEVER clobber a custom URL an operator set on their own
# server (e.g. a managed/remote Postgres host).
DB_PASS=$(grep "^VELARIS_DB_PASSWORD=" "$ENV_FILE" | cut -d= -f2)
if [ "$DB_CONFIGURED" = "1" ]; then
  ok "HELIX_CASE_DATABASE_URL already customized — keeping existing value"
elif [ "$DB_FAMILY" = "mysql" ]; then
  # MySQL (BYO): write TYPED connection components (not a raw URL). The backend
  # builds the async URL and URL-encodes the credentials — so a password with
  # @ : / # special chars can't corrupt it (a raw embedded URL would). Aligns with
  # the security spine: no raw URL passthrough; password only in VELARIS_DB_PASSWORD.
  # An explicit empty DATABASE_URL overrides the Postgres default so the components
  # are used. Also record the backend + components in velaris.yaml for the runner.
  sed -i "/^HELIX_CASE_DATABASE_URL=/d;/^HELIX_CASE_DB_HOST=/d;/^HELIX_CASE_DB_PORT=/d;/^HELIX_CASE_DB_NAME=/d;/^HELIX_CASE_DB_USER=/d" "$ENV_FILE"
  {
    echo "HELIX_CASE_DATABASE_URL="
    echo "HELIX_CASE_DB_HOST=${SEL_DB_HOST}"
    echo "HELIX_CASE_DB_PORT=${SEL_DB_PORT}"
    echo "HELIX_CASE_DB_NAME=${SEL_DB_NAME}"
    echo "HELIX_CASE_DB_USER=${SEL_DB_USER}"
  } >> "$ENV_FILE"
  ok "Database connection set ($SEL_BACKEND, typed components)"
  # Edit velaris.yaml reliably (password is NEVER written here — only host/port/name/user).
  # The persisted `database:` is the REAL backend ($SEL_BACKEND = mysql | mariadb), not the
  # family — the runtime picks MysqlBackend vs MariadbBackend from it.
  python3 - "$HELIX_DIR/velaris.yaml" "$SEL_BACKEND" "$SEL_DB_HOST" "$SEL_DB_PORT" "$SEL_DB_NAME" "$SEL_DB_USER" <<'PY'
import re, sys
path, backend, host, port, name, user = sys.argv[1:7]
lines = open(path).read().splitlines()
# Drop any previously-managed db_* lines so re-runs stay clean.
lines = [ln for ln in lines if not re.match(r'^\s*db_(host|port|name|user):', ln)]
out = []
for ln in lines:
    m = re.match(r'^(\s*)database:\s*.*$', ln)
    if m:
        ind = m.group(1)
        out.append(f"{ind}database: {backend}")
        out.append(f'{ind}db_host: "{host}"')
        out.append(f'{ind}db_port: {port}')
        out.append(f'{ind}db_name: "{name}"')
        out.append(f'{ind}db_user: "{user}"')
    else:
        out.append(ln)
open(path, "w").write("\n".join(out) + "\n")
PY
  ok "velaris.yaml updated (database: $SEL_BACKEND + connection components)"
else
  sed -i "/^HELIX_CASE_DATABASE_URL=/d" "$ENV_FILE"
  echo "HELIX_CASE_DATABASE_URL=postgresql+asyncpg://helix:${DB_PASS}@localhost:5432/helix" >> "$ENV_FILE"
  ok "DATABASE_URL set with generated password"
fi

# Document storage lives inside the install dir (no root, no /var/lib perms needed).
# Provision the folder and point .env at it — but never clobber an operator's own
# path (either the new VELARIS_CASE_ var or a legacy HELIX_CASE_ one).
STORAGE_DIR="$HELIX_DIR/data/documents"
mkdir -p "$STORAGE_DIR"
if grep -qE "^(VELARIS|HELIX)_CASE_STORAGE_LOCAL_PATH=" "$ENV_FILE"; then
  ok "Document storage path already set in .env — keeping existing value"
else
  echo "VELARIS_CASE_STORAGE_LOCAL_PATH=$STORAGE_DIR" >> "$ENV_FILE"
  ok "Document storage provisioned at $STORAGE_DIR"
fi

# Load the generated vars into this shell so docker compose can use them
set -o allexport; source "$ENV_FILE"; set +o allexport

# ── Step 2: Docker group ──────────────────────────────────────────
step "Step 2/9 — Checking Docker group membership..."

if groups "$USER" | grep -qw docker; then
  ok "User '$USER' is already in the docker group"
elif docker ps &>/dev/null 2>&1; then
  ok "Docker accessible"
else
  echo "  Adding '$USER' to the docker group..."
  sudo usermod -aG docker "$USER"
  warn "Added to docker group. Log out and back in, then re-run setup."
  exit 0
fi

# ── Step 3: uv workspace sync ─────────────────────────────────────
step "Step 3/9 — Syncing uv workspace..."
cd "$HELIX_DIR"
uv sync --all-packages 2>&1 | tail -5
ok "uv workspace synced"

# ── Step 4: Studio npm install ────────────────────────────────────
step "Step 4/9 — Installing Studio npm dependencies..."
if [ -d "$HELIX_DIR/studio" ]; then
  cd "$HELIX_DIR/studio"
  npm install --prefer-offline 2>&1 | tail -5
  ok "Studio dependencies installed"
else
  fail "studio/ directory not found"
fi
cd "$HELIX_DIR"

# ── Step 5: Pull Docker images ────────────────────────────────────
step "Step 5/9 — Pulling Docker images..."
COMPOSE_FILE="$HELIX_DIR/deploy/docker-compose/docker-compose.yml"
if docker info &>/dev/null 2>&1; then
  docker compose -f "$COMPOSE_FILE" --env-file "$HELIX_DIR/.env" pull 2>&1 | grep -E "Pull|pull|already|Digest" || true
  ok "Docker images up to date"
else
  warn "Docker not accessible — skipping image pull."
fi

# ── Step 5b: OpenBao secrets bootstrap (Group K — only if enabled) ─
# Runs only when the sentinel deploy/openbao/enabled is present. .env was
# already populated in Step 1b (existing values kept, missing ones generated),
# so this just brings up OpenBao, initialises it on a fresh install (or unseals
# an existing one), then imports those .env secrets into the vault — OpenBao is
# the source of truth from here on. Every failure is non-fatal (warn, continue).
if [ -f "$HELIX_DIR/deploy/openbao/enabled" ]; then
  step "Step 5b — Bootstrapping OpenBao secrets..."
  INIT_FILE="$HELIX_DIR/deploy/openbao/.bao-init.json"

  docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d openbao 2>/dev/null \
    || warn "Could not start the OpenBao container"

  # Wait for OpenBao to answer (471=uninitialised, 472=sealed, 200/429=unsealed).
  BAO_HEALTH="000"
  for i in $(seq 1 15); do
    BAO_HEALTH=$(curl -s -m 2 -o /dev/null -w '%{http_code}' \
      "http://127.0.0.1:8350/v1/sys/health?sealedcode=472&uninitcode=471" 2>/dev/null || echo "000")
    [ "$BAO_HEALTH" != "000" ] && break
    sleep 1
  done

  if [ ! -f "$INIT_FILE" ] && [ "$BAO_HEALTH" = "471" ]; then
    # Fresh OpenBao — one-time init: writes the keyfile, unseals, enables KV + AppRole.
    if "$HELIX_DIR/scripts/secrets-init.sh"; then ok "OpenBao initialised"; else warn "OpenBao init failed — continuing"; fi
  else
    # Already initialised — just make sure it's unsealed.
    if "$HELIX_DIR/scripts/secrets-unseal.sh" >/dev/null 2>&1; then ok "OpenBao unsealed"; else warn "OpenBao still sealed — run ./scripts/secrets-unseal.sh"; fi
  fi

  # Import the secrets from .env into OpenBao (source of truth going forward).
  if "$HELIX_DIR/scripts/secrets-push.sh" --sync-env >/dev/null 2>&1; then
    ok "Secrets synced from .env into OpenBao"
  else
    warn "secrets-push failed — run ./scripts/secrets-push.sh --sync-env once OpenBao is unsealed"
  fi
fi

# ── Step 6: Check migrations ──────────────────────────────────────
step "Step 6/9 — Checking migrations..."
MIGRATION_COUNT=$(find "$HELIX_DIR/migrations/postgresql" -maxdepth 1 -name "*.sql" | wc -l)
ok "Found $MIGRATION_COUNT migration files"

# ── Step 7: Start database temporarily for setup ─────────────────
if [ "$DB_FAMILY" = "mysql" ]; then
  # ── Step 7/8 (MySQL): BYO external DB — no bundled container to start ──
  # Resolve connection from this run's selection, falling back to velaris.yaml/.env
  # on a re-run (where the interactive selection was skipped).
  MY_HOST="${SEL_DB_HOST:-$(_yaml_get db_host)}"; MY_HOST="${MY_HOST:-127.0.0.1}"
  MY_PORT="${SEL_DB_PORT:-$(_yaml_get db_port)}"; MY_PORT="${MY_PORT:-3306}"
  MY_NAME="${SEL_DB_NAME:-$(_yaml_get db_name)}"; MY_NAME="${MY_NAME:-velaris}"
  MY_USER="${SEL_DB_USER:-$(_yaml_get db_user)}"; MY_USER="${MY_USER:-velaris}"
  MY_PASS=$(grep "^VELARIS_DB_PASSWORD=" "$ENV_FILE" | cut -d= -f2)
  # The mysql:8 client speaks to both MySQL 8 and MariaDB 10.6+/11 servers.
  mysql_client() {
    docker run --rm -i --network host -e MYSQL_PWD="$MY_PASS" mysql:8 \
      mysql --connect-timeout=10 -h "$MY_HOST" -P "$MY_PORT" -u "$MY_USER" "$@"
  }

  step "Step 7/9 — Connecting to MySQL ($MY_HOST:$MY_PORT)..."
  echo "  Bring-your-own MySQL — no bundled DB container is started."
  for i in $(seq 1 20); do
    if mysql_client -e "SELECT 1" >/dev/null 2>&1; then ok "MySQL reachable"; break; fi
    [ "$i" -eq 20 ] && { fail "MySQL not reachable at $MY_HOST:$MY_PORT — check the server, user, and password"; exit 1; }
    sleep 1
  done
  if mysql_client -e "CREATE DATABASE IF NOT EXISTS \`$MY_NAME\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"; then
    ok "Database '$MY_NAME' ready (utf8mb4)"
  else
    fail "Could not create/access database '$MY_NAME' (does the user have rights?)"; exit 1
  fi

  step "Step 8/9 — Running database migrations..."
  mysql_client "$MY_NAME" -e "CREATE TABLE IF NOT EXISTS schema_migrations (
  filename   VARCHAR(255) PRIMARY KEY,
  applied_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
);"
  APPLIED=0; SKIPPED=0
  for migration_file in $(find "$HELIX_DIR/migrations/mysql" -maxdepth 1 -name "*.sql" | sort); do
    filename="$(basename "$migration_file")"
    EXISTS=$(mysql_client "$MY_NAME" -N -e "SELECT 1 FROM schema_migrations WHERE filename='$filename';" 2>/dev/null || echo "")
    if [ "$EXISTS" = "1" ]; then
      SKIPPED=$((SKIPPED + 1)); continue
    fi
    echo -n "    apply $filename ... "
    if mysql_client "$MY_NAME" < "$migration_file" 2>/tmp/migration_err.log; then
      mysql_client "$MY_NAME" -e "INSERT IGNORE INTO schema_migrations(filename) VALUES('$filename');"
      green "✓"; APPLIED=$((APPLIED + 1))
    else
      red "FAILED"; cat /tmp/migration_err.log; exit 1
    fi
  done
  green "  ✓ Migrations: $APPLIED applied, $SKIPPED skipped"
else
step "Step 7/9 — Starting database for setup..."
DB_CONTAINER="docker-compose-helix-db-1"
DB_USER="helix"
DB_NAME="helix"

docker compose -f "$COMPOSE_FILE" --env-file "$HELIX_DIR/.env" up -d helix-db 2>/dev/null || \
  docker compose -f "$COMPOSE_FILE" --env-file "$HELIX_DIR/.env" up -d 2>/dev/null || true

echo "  Waiting for PostgreSQL..."
for i in $(seq 1 20); do
  if docker exec "$DB_CONTAINER" pg_isready -U "$DB_USER" -q 2>/dev/null; then
    ok "PostgreSQL ready"; break
  fi
  [ "$i" -eq 20 ] && { fail "PostgreSQL not ready — check docker logs $DB_CONTAINER"; exit 1; }
  sleep 1
done

# Sync DB user password to match .env (prevents auth failures on TCP connections)
docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -q \
  -c "ALTER USER ${DB_USER} PASSWORD '${VELARIS_DB_PASSWORD}';" 2>/dev/null \
  && ok "DB user password synced" || warn "Could not sync DB password — continuing"

# ── Step 8: Run migrations (including 071_superadmin) ─────────────
step "Step 8/9 — Running database migrations..."

docker exec -i "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -q <<'SQL'
CREATE TABLE IF NOT EXISTS schema_migrations (
  filename   TEXT PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
SQL

APPLIED=0; SKIPPED=0

for migration_file in $(find "$HELIX_DIR/migrations/postgresql" -maxdepth 1 -name "*.sql" | sort); do
  filename="$(basename "$migration_file")"
  EXISTS=$(docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -tAq \
    -c "SELECT 1 FROM schema_migrations WHERE filename='$filename';" 2>/dev/null || echo "")
  if [ "$EXISTS" = "1" ]; then
    SKIPPED=$((SKIPPED + 1)); continue
  fi
  echo -n "    apply $filename ... "
  if docker exec -i "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -q \
      < "$migration_file" 2>/tmp/migration_err.log; then
    docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -q \
      -c "INSERT INTO schema_migrations(filename) VALUES('$filename') ON CONFLICT DO NOTHING;"
    green "✓"; APPLIED=$((APPLIED + 1))
  else
    red "FAILED"; cat /tmp/migration_err.log; exit 1
  fi
done
green "  ✓ Migrations: $APPLIED applied, $SKIPPED skipped"
fi

# ── Step 8b: Ollama default models (best-effort) ──────────────────
# A fresh Ollama container ships with NO models; every AI feature (HxNexus
# chat, case Q&A, session summaries) then fails soft with empty answers.
# Pull the configured defaults now — non-fatal, skipped when offline.
step "Step 8b — Pulling default Ollama models (best-effort)..."
OLLAMA_CONTAINER=$(docker ps --format '{{.Names}}' 2>/dev/null | grep -i ollama | head -1 || true)
if [ -n "$OLLAMA_CONTAINER" ]; then
  AI_MODEL="${HELIX_CASE_AI_OLLAMA_MODEL:-llama3.2}"
  EMBED_MODEL="${HELIX_CASE_AI_OLLAMA_EMBED_MODEL:-nomic-embed-text}"
  for M in "$AI_MODEL" "$EMBED_MODEL"; do
    if docker exec "$OLLAMA_CONTAINER" ollama pull "$M" >/dev/null 2>&1; then
      green "  ✓ pulled $M"
    else
      warn "  Could not pull $M (offline?) — pull later: docker exec $OLLAMA_CONTAINER ollama pull $M"
    fi
  done
else
  warn "  Ollama container not running — models will be pulled on demand (or via start-velaris.sh hint)."
fi
echo ""

# ── Step 9: Product key + superadmin creation ─────────────────────
step "Step 9/9 — Product key validation and superadmin setup..."
echo ""

# Skip if superadmin already exists
if [ "$DB_FAMILY" = "mysql" ]; then
  SUPERADMIN_EXISTS=$(mysql_client "$MY_NAME" -N \
    -e "SELECT COUNT(*) FROM helix_users WHERE is_superadmin = TRUE;" 2>/dev/null || echo "0")
else
  SUPERADMIN_EXISTS=$(docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -tAq \
    -c "SELECT COUNT(*) FROM helix_users WHERE is_superadmin = TRUE;" 2>/dev/null || echo "0")
fi

if [ "$SUPERADMIN_EXISTS" = "1" ]; then
  warn "Superadmin already exists — skipping key + superadmin setup."
  # Still ensure .velaris-setup-complete marker exists
  touch "$HELIX_DIR/.velaris-setup-complete"
else
  if [ "$DB_FAMILY" = "mysql" ]; then
    # Install script dependencies (MySQL driver) + run the interactive setup.
    # Password via VELARIS_DB_PASSWORD env (not in the URL) so special chars are safe.
    uv pip install bcrypt httpx pymysql cryptography --quiet
    VELARIS_DB_PASSWORD="$MY_PASS" \
    DATABASE_URL="mysql://${MY_USER}@${MY_HOST}:${MY_PORT}/${MY_NAME}" \
    uv run python "$HELIX_DIR/scripts/setup_superadmin.py"
  else
    # Install script dependencies
    uv pip install bcrypt httpx psycopg2-binary cryptography --quiet

    # Run the interactive Python setup
    DATABASE_URL="postgresql://helix:${VELARIS_DB_PASSWORD:-helix}@localhost:5432/helix" \
    uv run python "$HELIX_DIR/scripts/setup_superadmin.py"
  fi
fi

# ── Done ─────────────────────────────────────────────────────────
echo ""
if [ "$ERRORS" -gt 0 ]; then
  red "╔═══════════════════════════════════════════╗"
  red "║  Setup completed with $ERRORS error(s).         ║"
  red "╚═══════════════════════════════════════════╝"
  exit 1
else
  echo "╔═══════════════════════════════════════════╗"
  green "║  Setup complete! Run ./start-velaris.sh   ║"
  echo "╚═══════════════════════════════════════════╝"
fi
