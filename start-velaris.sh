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

# ── Guard: Docker accessible? ────────────────────────────────────
if ! docker info &>/dev/null 2>&1; then
  red "ERROR: Docker not accessible. Run setup-velaris.sh first, then log out/in."
  exit 1
fi

# ── Step 0: Free ports and clean old containers ──────────────────
echo "▶ Preparing ports..."
docker compose -f "$COMPOSE_FILE" --env-file "$HELIX_DIR/.env" down 2>/dev/null || true
sudo systemctl stop postgresql 2>/dev/null || true
for port in 5432 8100 8200 5173; do
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

# ── Step 3: Wait for PostgreSQL ──────────────────────────────────
echo "▶ Waiting for PostgreSQL (port 5432)..."
for i in $(seq 1 20); do
  if docker exec "$DB_CONTAINER" pg_isready -U "$DB_USER" -q 2>/dev/null; then
    green "  ✓ PostgreSQL ready"; break
  fi
  [ "$i" -eq 20 ] && { red "  ✗ PostgreSQL not ready — check: docker logs $DB_CONTAINER"; exit 1; }
  sleep 1
done
echo ""

# ── Step 4: Run all migrations ────────────────────────────────────
echo "▶ Running database migrations..."

docker exec -i "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -q <<'SQL'
CREATE TABLE IF NOT EXISTS schema_migrations (
  filename   TEXT PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
SQL

APPLIED=0; SKIPPED=0; FAILED=0

for migration_file in $(find "$HELIX_DIR/migrations" -name "*.sql" | sort); do
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

# ── Step 4b: Sync release manifest ───────────────────────────────
# Counts INSERT statements in releases/manifest.sql and compares against
# rows in scheduled_releases. If they differ, new features have been added
# since the last run — applies the manifest (ON CONFLICT DO NOTHING skips
# existing rows, only inserts new ones).
MANIFEST_FILE="$HELIX_DIR/releases/manifest.sql"
if [ -f "$MANIFEST_FILE" ]; then
  MANIFEST_COUNT=$(grep -c "^INSERT" "$MANIFEST_FILE" 2>/dev/null || echo 0)
  DB_COUNT=$(docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -tAq \
    -c "SELECT COUNT(*) FROM scheduled_releases;" 2>/dev/null | tr -d '[:space:]' || echo 0)
  if [ "$MANIFEST_COUNT" -ne "$DB_COUNT" ]; then
    echo "▶ Syncing release manifest ($DB_COUNT features in DB, $MANIFEST_COUNT in manifest)..."
    if docker exec -i "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -q \
        < "$MANIFEST_FILE" 2>/tmp/manifest_err.log; then
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
DATABASE_URL="postgresql://helix:${VELARIS_DB_PASSWORD:-helix_dev_password}@localhost:5432/helix" \
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
green "  ✓ Environment ready"

# ── Step 5b: Check Ollama ─────────────────────────────────────────
echo "▶ Checking Ollama (HxNexus AI backend, port 11434)..."
if curl -s --max-time 2 http://localhost:11434/api/tags > /dev/null 2>&1; then
  green "  ✓ Ollama online — HxNexus ready"
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
echo "▶ Starting Case Service (port 8200)..."
nohup uv run uvicorn case_service.main:app \
  --host 0.0.0.0 --port 8200 --reload \
  --app-dir services/case-service \
  > /tmp/velaris-case-service.log 2>&1 &
echo "  PID: $! | Logs: tail -f /tmp/velaris-case-service.log"
for i in $(seq 1 20); do
  if curl -s http://localhost:8200/health > /dev/null 2>&1; then
    green "  ✓ Case Service ready on port 8200"; break
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
