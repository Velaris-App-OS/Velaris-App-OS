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

_write_if_missing VELARIS_DB_PASSWORD            "$(openssl rand -hex 16)"
_write_if_missing VELARIS_ADMIN_PASSWORD         "$(openssl rand -hex 16)"
_write_if_missing VELARIS_SEARCH_PASSWORD        "$(openssl rand -hex 16)"
_write_if_missing HELIX_CASE_AUTH_SECRET         "$(openssl rand -hex 32)"
_write_if_missing HELIX_CASE_STORAGE_MASTER_KEY  "$(openssl rand -hex 32)"
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

# Update DATABASE_URL in .env to use the generated password
DB_PASS=$(grep "^VELARIS_DB_PASSWORD=" "$ENV_FILE" | cut -d= -f2)
sed -i "s|HELIX_CASE_DATABASE_URL=.*|HELIX_CASE_DATABASE_URL=postgresql+asyncpg://helix:${DB_PASS}@localhost:5432/helix|" "$ENV_FILE"
ok "DATABASE_URL updated with generated password"

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

# ── Step 6: Check migrations ──────────────────────────────────────
step "Step 6/9 — Checking migrations..."
MIGRATION_COUNT=$(find "$HELIX_DIR/migrations" -name "*.sql" | wc -l)
ok "Found $MIGRATION_COUNT migration files"

# ── Step 7: Start database temporarily for setup ─────────────────
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

for migration_file in $(find "$HELIX_DIR/migrations" -name "*.sql" | sort); do
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

# ── Step 9: Product key + superadmin creation ─────────────────────
step "Step 9/9 — Product key validation and superadmin setup..."
echo ""

# Skip if superadmin already exists
SUPERADMIN_EXISTS=$(docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -tAq \
  -c "SELECT COUNT(*) FROM helix_users WHERE is_superadmin = TRUE;" 2>/dev/null || echo "0")

if [ "$SUPERADMIN_EXISTS" = "1" ]; then
  warn "Superadmin already exists — skipping key + superadmin setup."
  # Still ensure .velaris-setup-complete marker exists
  touch "$HELIX_DIR/.velaris-setup-complete"
else
  # Install script dependencies
  uv pip install bcrypt httpx psycopg2-binary cryptography --quiet

  # Run the interactive Python setup
  DATABASE_URL="postgresql://helix:${VELARIS_DB_PASSWORD:-helix}@localhost:5432/helix" \
  uv run python "$HELIX_DIR/scripts/setup_superadmin.py"
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
