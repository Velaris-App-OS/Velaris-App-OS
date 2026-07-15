#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  VELARIS Startup Script
#  Run after every reboot to bring the full environment up.
#
#  Usage:  ./start-velaris.sh
# ═══════════════════════════════════════════════════════════════════

set -euo pipefail

HELIX_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$HELIX_DIR/deploy/docker-compose/docker-compose.yml"
DB_CONTAINER="docker-compose-helix-db-1"
DB_USER="helix"
DB_NAME="helix"

# ── Step 0: OpenBao secrets (Group K, opt-in) ─────────────────────
# Enabled by the sentinel file deploy/openbao/enabled. When active, .env is
# RENDERED from OpenBao before anything reads it. Render is fail-closed: on
# any problem the existing .env stays untouched and we continue on the
# last-known-good secrets (warning printed). No sentinel = no change at all.
if [ -f "$HELIX_DIR/deploy/openbao/enabled" ]; then
  echo "▶ Step 0: OpenBao secrets..."
  [ -f "$HELIX_DIR/.env" ] || touch "$HELIX_DIR/.env"   # compose interpolation needs the file to exist
  # Every failure below degrades to "continue on the existing .env" — Step 0
  # must never be the reason the platform fails to start (set -e is active).
  if ! docker compose -f "$COMPOSE_FILE" --env-file "$HELIX_DIR/.env" up -d openbao 2>/dev/null; then
    echo -e "  \033[0;33m⚠ Could not start the OpenBao container\033[0m"
  fi
  BAO_HEALTH="000"
  for i in $(seq 1 15); do
    BAO_HEALTH=$(curl -s -m 2 -o /dev/null -w '%{http_code}' \
      "http://127.0.0.1:8350/v1/sys/health?sealedcode=472&uninitcode=471" || echo "000")
    [ "$BAO_HEALTH" != "000" ] && break
    sleep 1
  done
  if [ "$BAO_HEALTH" = "472" ]; then
    echo "  Unsealing OpenBao from local keyfile..."
    if "$HELIX_DIR/scripts/secrets-unseal.sh" >/dev/null 2>&1; then
      BAO_HEALTH=200
    else
      echo -e "  \033[0;33m⚠ Unseal failed (bad or unreadable keyfile)\033[0m"
    fi
  fi
  if [ "$BAO_HEALTH" = "200" ] || [ "$BAO_HEALTH" = "429" ]; then
    if "$HELIX_DIR/scripts/secrets-render.sh"; then
      echo -e "  \033[0;32m✓ .env rendered from OpenBao\033[0m"
    else
      echo -e "  \033[0;33m⚠ Render failed — continuing on the existing .env\033[0m"
    fi
  else
    echo -e "  \033[0;33m⚠ OpenBao unavailable (health=$BAO_HEALTH) — continuing on the existing .env\033[0m"
  fi
  echo ""
fi

# Load .env early so VELARIS_DB_PASSWORD is available for docker compose
if [ -f "$HELIX_DIR/.env" ]; then
  set -o allexport; source "$HELIX_DIR/.env"; set +o allexport
fi

green()  { echo -e "\033[0;32m$*\033[0m"; }
yellow() { echo -e "\033[0;33m$*\033[0m"; }
red()    { echo -e "\033[0;31m$*\033[0m"; }

echo "╔══════════════════════════════════════════╗"
echo "║         VELARIS Startup                  ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ════════════════════════════════════════════════════════════════════
# GATE 1: Product key verification (MUST PASS — first check, always)
# ════════════════════════════════════════════════════════════════════
echo "▶ Verifying product key…"
if ! uv run python "$HELIX_DIR/scripts/verify_key.py"; then
  echo ""
  red "╔═══════════════════════════════════════════════════════╗"
  red "║  STARTUP BLOCKED: product key verification failed.   ║"
  red "║  Run ./setup-velaris.sh if this is a new install.    ║"
  red "║  Contact support: velaris.app.os@gmail.com                   ║"
  red "╚═══════════════════════════════════════════════════════╝"
  exit 1
fi
echo ""

# ── Update check (non-blocking notice) ──────────────────────────
_yaml_val() {
  grep -E "^\s+${1}:" "$HELIX_DIR/velaris.yaml" 2>/dev/null \
    | head -1 | sed -E "s/.*${1}:[[:space:]]*[\"']?([^\"'#]+)[\"']?.*/\1/" | xargs || echo "${2:-}"
}
_CURRENT_VER=$(_yaml_val "version" "")
_UPDATE_SOURCE=$(_yaml_val "source" "github")
_GITHUB_REPO=$(_yaml_val "github_repo" "")
_SERVER_URL=$(_yaml_val "server_url" "")
if [ -n "$_CURRENT_VER" ]; then
  _LATEST_VER=""
  if [ "$_UPDATE_SOURCE" = "github" ] && [ -n "$_GITHUB_REPO" ]; then
    _LATEST_VER=$(curl -sf --max-time 5 \
      -H "Accept: application/vnd.github+json" \
      "https://api.github.com/repos/${_GITHUB_REPO}/releases/latest" 2>/dev/null \
      | python3 -c "import sys,json; print(json.load(sys.stdin)['tag_name'].lstrip('v'))" 2>/dev/null || echo "")
  elif [ "$_UPDATE_SOURCE" = "server" ] && [ -n "$_SERVER_URL" ]; then
    _LATEST_VER=$(curl -sf --max-time 5 "$_SERVER_URL" 2>/dev/null \
      | python3 -c "import sys,json; print(json.load(sys.stdin)['version'])" 2>/dev/null || echo "")
  fi
  if [ -n "$_LATEST_VER" ] && [ "$_LATEST_VER" != "$_CURRENT_VER" ]; then
    yellow "  ⚠ Update available: v$_CURRENT_VER → v$_LATEST_VER"
    yellow "    Run ./update-velaris.sh to upgrade"
    echo ""
  fi
fi

# ── DB SDK: select the database backend early ────────────────────
# Steps 3/4/4b and the superadmin gate branch on this. PostgreSQL keeps the exact
# original docker-exec/psql path (bundled container). MySQL is a BYO external DB:
# reached over the network by a throwaway mysql:8 client container (no host mysql
# binary needed; password via MYSQL_PWD, never on the command line, from
# VELARIS_DB_PASSWORD/OpenBao — never velaris.yaml plaintext).
DATABASE_BACKEND=$(_yaml_val "database" "postgresql")
# DB_FAMILY normalises the runner path: MariaDB reuses the entire MySQL path (same
# migrations/mysql baseline, schema_migrations DDL, manifest, client protocol) and
# differs only in the client image. All MySQL-path branches below test DB_FAMILY.
case "$DATABASE_BACKEND" in
  postgresql)    DB_FAMILY=postgresql ;;
  mysql|mariadb) DB_FAMILY=mysql ;;
  *)
    red "STARTUP BLOCKED: unsupported database backend '$DATABASE_BACKEND' in velaris.yaml (allowed: postgresql, mysql, mariadb)."
    exit 1
    ;;
esac
export DATABASE_BACKEND
if [ "$DB_FAMILY" = "mysql" ]; then
  DB_HOST=$(_yaml_val "db_host" "127.0.0.1")
  DB_PORT=$(_yaml_val "db_port" "3306")
  DB_NAME=$(_yaml_val "db_name" "velaris")
  DB_USER=$(_yaml_val "db_user" "velaris")
  DB_PASSWORD="${VELARIS_DB_PASSWORD:-}"
  # The mysql:8 client speaks to both MySQL 8 and MariaDB 10.6+/11 servers (the mariadb
  # image dropped the `mysql` command, so mysql:8 is the portable client for both).
  mysql_client() {
    docker run --rm -i --network host -e MYSQL_PWD="$DB_PASSWORD" mysql:8 \
      mysql --connect-timeout=10 -h "$DB_HOST" -P "$DB_PORT" -u "$DB_USER" "$@"
  }
fi

# ── Guard: Docker accessible? ────────────────────────────────────
if ! docker info &>/dev/null 2>&1; then
  red "ERROR: Docker not accessible. Run setup-velaris.sh first, then log out/in."
  exit 1
fi

# ── Step 0: Free ports and clean old containers ──────────────────
echo "▶ Preparing ports..."
docker compose -f "$COMPOSE_FILE" --env-file "$HELIX_DIR/.env" down 2>/dev/null || true
sudo systemctl stop postgresql 2>/dev/null || true
for port in 5432 8100 8201 5173; do
  fuser -k "${port}/tcp" 2>/dev/null || true
done
sleep 2
green "  ✓ Ports ready"
echo ""

# ── Step 1: Start Docker infrastructure ──────────────────────────
echo "▶ Starting Docker infrastructure..."
docker compose -f "$COMPOSE_FILE" --env-file "$HELIX_DIR/.env" up -d
echo "  Waiting for services to initialise..."
sleep 10

# Group K: the global down/up above restarted OpenBao sealed. .env is already
# rendered (Step 0); re-unseal so day-2 secret operations work without manual
# intervention. Failure is non-fatal — the platform runs fine sealed.
if [ -f "$HELIX_DIR/deploy/openbao/enabled" ]; then
  "$HELIX_DIR/scripts/secrets-unseal.sh" 2>/dev/null \
    && green "  ✓ OpenBao unsealed" \
    || yellow "  ⚠ OpenBao still sealed — run ./scripts/secrets-unseal.sh before pushing secrets"
fi

echo ""
echo "  Checking containers:"
for svc in helix-db temporal temporal-ui minio cache mailpit; do
  if docker ps --format '{{.Names}}' | grep -q "$svc"; then
    green "    ✓ $svc running"
  else
    yellow "    ⚠ $svc not found (may be optional or slow to start)"
  fi
done
echo ""

# ── Step 2: Wait for Temporal ────────────────────────────────────
echo "▶ Waiting for Temporal (port 7233)..."
for i in $(seq 1 30); do
  if nc -z localhost 7233 2>/dev/null; then
    green "  ✓ Temporal ready"; break
  fi
  [ "$i" -eq 30 ] && yellow "  ⚠ Temporal not ready after 30s — continuing"
  sleep 1
done

# ── Step 3: Wait for the database ────────────────────────────────
if [ "$DB_FAMILY" = "mysql" ]; then
  echo "▶ Waiting for MySQL ($DB_HOST:$DB_PORT)..."
  for i in $(seq 1 20); do
    if mysql_client -e "SELECT 1" >/dev/null 2>&1; then
      green "  ✓ MySQL ready"; break
    fi
    [ "$i" -eq 20 ] && { red "  ✗ MySQL not ready at $DB_HOST:$DB_PORT — check the DB and VELARIS_DB_PASSWORD"; exit 1; }
    sleep 1
  done
else
  echo "▶ Waiting for PostgreSQL (port 5432)..."
  for i in $(seq 1 20); do
    if docker exec "$DB_CONTAINER" pg_isready -U "$DB_USER" -q 2>/dev/null; then
      green "  ✓ PostgreSQL ready"; break
    fi
    [ "$i" -eq 20 ] && { red "  ✗ PostgreSQL not ready — check: docker logs $DB_CONTAINER"; exit 1; }
    sleep 1
  done
fi
echo ""

# ── Step 4: Run all migrations ────────────────────────────────────
echo "▶ Running database migrations..."

if [ "$DB_FAMILY" = "mysql" ]; then
  # MySQL track: the consolidated baseline under migrations/mysql/ (Velaris ships
  # fresh on MySQL — no incremental PG files). schema_migrations is the MySQL
  # variant (VARCHAR PK / DATETIME(6) / INSERT IGNORE).
  # Ensure the database exists, as utf8mb4 — required for correctness (emoji/non-Latin
  # text) and for the InnoDB 3072-byte key-limit math the schema is bounded against.
  # Idempotent; a no-op if a DBA pre-created it. Issued without a default DB selected.
  mysql_client -e "CREATE DATABASE IF NOT EXISTS \`$DB_NAME\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
  mysql_client "$DB_NAME" -e "CREATE TABLE IF NOT EXISTS schema_migrations (
  filename   VARCHAR(255) PRIMARY KEY,
  applied_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
);"
  APPLIED=0; SKIPPED=0
  for migration_file in $(find "$HELIX_DIR/migrations/mysql" -maxdepth 1 -name "*.sql" | sort); do
    filename="$(basename "$migration_file")"
    EXISTS=$(mysql_client "$DB_NAME" -N -e "SELECT 1 FROM schema_migrations WHERE filename='$filename';" 2>/dev/null || echo "")
    if [ "$EXISTS" = "1" ]; then
      echo "    skip  $filename"; SKIPPED=$((SKIPPED + 1)); continue
    fi
    echo -n "    apply $filename ... "
    if mysql_client "$DB_NAME" < "$migration_file" 2>/tmp/migration_err.log; then
      mysql_client "$DB_NAME" -e "INSERT IGNORE INTO schema_migrations(filename) VALUES('$filename');"
      green "✓"; APPLIED=$((APPLIED + 1))
    else
      red "FAILED"; cat /tmp/migration_err.log
      red "  Migration failed — stopping."; exit 1
    fi
  done
  echo ""
  green "  ✓ Migrations: $APPLIED applied, $SKIPPED already up-to-date"
  echo ""
else
docker exec -i "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -q <<'SQL'
CREATE TABLE IF NOT EXISTS schema_migrations (
  filename   TEXT PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
SQL

APPLIED=0; SKIPPED=0; FAILED=0

for migration_file in $(find "$HELIX_DIR/migrations/postgresql" -maxdepth 1 -name "*.sql" | sort); do
  filename="$(basename "$migration_file")"
  EXISTS=$(docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -tAq \
    -c "SELECT 1 FROM schema_migrations WHERE filename='$filename';" 2>/dev/null || echo "")
  if [ "$EXISTS" = "1" ]; then
    echo "    skip  $filename"; SKIPPED=$((SKIPPED + 1)); continue
  fi
  echo -n "    apply $filename ... "
  if docker exec -i "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -q \
      < "$migration_file" 2>/tmp/migration_err.log; then
    docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -q \
      -c "INSERT INTO schema_migrations(filename) VALUES('$filename') ON CONFLICT DO NOTHING;"
    green "✓"; APPLIED=$((APPLIED + 1))
  else
    red "FAILED"; cat /tmp/migration_err.log; FAILED=$((FAILED + 1))
    red "  Migration failed — stopping."; exit 1
  fi
done
echo ""
green "  ✓ Migrations: $APPLIED applied, $SKIPPED already up-to-date"
echo ""
fi

# ── Step 4b: Sync release manifest ───────────────────────────────
# Counts INSERT statements in releases/manifest.sql and compares against
# rows in scheduled_releases. If they differ, new features have been added
# since the last run — applies the manifest (ON CONFLICT DO NOTHING skips
# existing rows, only inserts new ones).
# MySQL uses the dialect sibling manifest.mysql.sql (UUID() / ON DUPLICATE KEY).
if [ "$DB_FAMILY" = "mysql" ]; then
  MANIFEST_FILE="$HELIX_DIR/releases/manifest.mysql.sql"
else
  MANIFEST_FILE="$HELIX_DIR/releases/manifest.sql"
fi
if [ -f "$MANIFEST_FILE" ]; then
  MANIFEST_COUNT=$(grep -c "^INSERT" "$MANIFEST_FILE" 2>/dev/null || echo 0)
  if [ "$DB_FAMILY" = "mysql" ]; then
    DB_COUNT=$(mysql_client "$DB_NAME" -N -e "SELECT COUNT(*) FROM scheduled_releases;" 2>/dev/null | tr -d '[:space:]' || echo 0)
  else
    DB_COUNT=$(docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -tAq \
      -c "SELECT COUNT(*) FROM scheduled_releases;" 2>/dev/null | tr -d '[:space:]' || echo 0)
  fi
  if [ "$MANIFEST_COUNT" -ne "$DB_COUNT" ]; then
    echo "▶ Syncing release manifest ($DB_COUNT features in DB, $MANIFEST_COUNT in manifest)..."
    if [ "$DB_FAMILY" = "mysql" ]; then
      _man_ok=0; mysql_client "$DB_NAME" < "$MANIFEST_FILE" 2>/tmp/manifest_err.log && _man_ok=1
    else
      _man_ok=0; docker exec -i "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -q < "$MANIFEST_FILE" 2>/tmp/manifest_err.log && _man_ok=1
    fi
    if [ "$_man_ok" = "1" ]; then
      green "  ✓ Release manifest synced — $((MANIFEST_COUNT - DB_COUNT)) new feature(s) activated"
    else
      yellow "  ⚠ Release manifest sync had warnings:"; cat /tmp/manifest_err.log
    fi
  else
    green "  ✓ Release manifest up to date ($DB_COUNT features)"
  fi
  echo ""
fi

# ════════════════════════════════════════════════════════════════════
# GATE 2: Superadmin DB presence check
# ════════════════════════════════════════════════════════════════════
echo "▶ Verifying superadmin account…"
if [ "$DB_FAMILY" = "mysql" ]; then
  # No password in the URL — check_superadmin.py reads VELARIS_DB_PASSWORD from the
  # env (sourced from .env above), so a special-char password can't corrupt the URL.
  SUPERADMIN_DB_URL="mysql://${DB_USER}@${DB_HOST}:${DB_PORT}/${DB_NAME}"
else
  SUPERADMIN_DB_URL="postgresql://helix:${VELARIS_DB_PASSWORD:-helix_dev_password}@localhost:5432/helix"
fi
DATABASE_URL="$SUPERADMIN_DB_URL" \
  uv run python "$HELIX_DIR/scripts/check_superadmin.py" || {
  echo ""
  red "╔═══════════════════════════════════════════════════════╗"
  red "║  STARTUP BLOCKED: no superadmin account found.       ║"
  red "║  Run ./setup-velaris.sh to create one.               ║"
  red "╚═══════════════════════════════════════════════════════╝"
  exit 1
}
green "  ✓ Superadmin verified"
echo ""

# ── Step 4b: Sync Python workspace dependencies ──────────────────
echo "▶ Syncing Python dependencies..."
cd "$HELIX_DIR"
uv sync --all-packages --quiet 2>/dev/null && green "  ✓ Dependencies up to date" || yellow "  ⚠ uv sync had warnings — continuing"
echo ""

# ── Step 5: Set environment variables ────────────────────────────
export HELIX_SERVICE_REGISTRY='{"order-service":"http://localhost:3001","notification-service":"http://localhost:3002"}'

# DB SDK: the database backend was already detected, allowlist-gated, and exported
# near the top (so Steps 3/4/4b/Gate-2 could branch). The Python layer
# (case_service.db.backends) enforces the same allowlist — fail-closed both sides.
green "  ✓ Environment ready (database: $DATABASE_BACKEND)"

# ── Step 5b: Check Ollama ─────────────────────────────────────────
echo "▶ Checking Ollama (HxNexus AI backend, port 11434)..."
if curl -s --max-time 2 http://localhost:11434/api/tags > /dev/null 2>&1; then
  # An online Ollama with NO models pulled makes every AI feature (chat,
  # case Q&A, session summaries) fail soft with empty answers — warn loudly.
  MODEL_COUNT=$(curl -s --max-time 2 http://localhost:11434/api/tags \
    | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('models',[])))" 2>/dev/null || echo "?")
  if [ "$MODEL_COUNT" = "0" ]; then
    yellow "  ⚠ Ollama online but NO models pulled — HxNexus answers will be empty."
    yellow "    Pull the defaults:  docker exec docker-compose-ollama-1 ollama pull ${HELIX_CASE_AI_OLLAMA_MODEL:-llama3.2}"
    yellow "                        docker exec docker-compose-ollama-1 ollama pull ${HELIX_CASE_AI_OLLAMA_EMBED_MODEL:-nomic-embed-text}"
  else
    green "  ✓ Ollama online ($MODEL_COUNT model(s)) — HxNexus ready"
  fi
else
  yellow "  ⚠ Ollama not reachable — HxNexus will show offline"
fi
echo ""

# ── Step 6: Start Engine ──────────────────────────────────────────
echo "▶ Starting Velaris Engine (port 8100)..."
cd "$HELIX_DIR"
nohup uv run uvicorn helix_engine.main:app \
  --host 0.0.0.0 --port 8100 --reload \
  > /tmp/velaris-engine.log 2>&1 &
echo "  PID: $! | Logs: tail -f /tmp/velaris-engine.log"
for i in $(seq 1 20); do
  if curl -s http://localhost:8100/health > /dev/null 2>&1; then
    green "  ✓ Engine ready on port 8100"; break
  fi
  [ "$i" -eq 20 ] && yellow "  ⚠ Engine not ready after 20s"
  sleep 1
done
echo ""

# ── Step 7: Start Case Service ────────────────────────────────────
echo "▶ Starting Case Service (loopback 127.0.0.1:8201, only reachable via API gateway on 8200)..."
nohup uv run uvicorn case_service.main:app \
  --host 127.0.0.1 --port 8201 --reload \
  --app-dir services/case-service \
  > /tmp/velaris-case-service.log 2>&1 &
echo "  PID: $! | Logs: tail -f /tmp/velaris-case-service.log"
for i in $(seq 1 20); do
  if curl -s http://localhost:8201/health > /dev/null 2>&1; then
    green "  ✓ Case Service ready on port 8201"; break
  fi
  [ "$i" -eq 20 ] && yellow "  ⚠ Case Service not ready after 20s"
  sleep 1
done
echo ""

# ── Step 8: Start Studio ─────────────────────────────────────────
echo "▶ Starting Velaris Studio (port 5173)..."
cd "$HELIX_DIR/studio"
nohup npm run dev -- --host 0.0.0.0 > /tmp/velaris-studio.log 2>&1 &
echo "  PID: $! | Logs: tail -f /tmp/velaris-studio.log"
sleep 3
echo ""
cd "$HELIX_DIR"

# ── Heartbeat ping (background, non-blocking) ─────────────────────
if [ -f "$HELIX_DIR/.velaris-key" ]; then
  KEY_ID=$(python3 -c "import json; d=json.load(open('$HELIX_DIR/.velaris-key')); print(d.get('key_id',''))" 2>/dev/null || echo "")
  if [ -n "$KEY_ID" ]; then
    curl -s -X POST "${VELARIS_REGISTER_URL:-https://register.velaris.io}/heartbeat" \
      -H "Content-Type: application/json" \
      -d "{\"key_id\":\"$KEY_ID\",\"mac\":\"$(cat /sys/class/net/$(ip route show default | awk '/default/{print $5}')/address 2>/dev/null || echo unknown)\",\"version\":\"1.0.0\"}" \
      --max-time 5 > /dev/null 2>&1 &
  fi
fi

# ── Done ─────────────────────────────────────────────────────────
echo "╔═══════════════════════════════════════════════════════╗"
green "║              VELARIS is running!                      ║"
echo "╠═══════════════════════════════════════════════════════╣"
echo "║  Studio:        http://localhost:5173                 ║"
echo "║  Engine API:    http://localhost:8100                 ║"
echo "║  Case Service:  http://localhost:8200                 ║"
echo "║  Temporal UI:   http://localhost:8233                 ║"
echo "║  MinIO Console: http://localhost:9001                 ║"
echo "║  Mailpit UI:    http://localhost:8025                 ║"
echo "╠═══════════════════════════════════════════════════════╣"
echo "║  Engine logs:   tail -f /tmp/velaris-engine.log       ║"
echo "║  Case logs:     tail -f /tmp/velaris-case-service.log ║"
echo "║  Studio logs:   tail -f /tmp/velaris-studio.log       ║"
echo "╠═══════════════════════════════════════════════════════╣"
echo "║  Stop:          ./stop-velaris.sh                     ║"
echo "╚═══════════════════════════════════════════════════════╝"
