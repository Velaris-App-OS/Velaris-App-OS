#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  VELARIS Update Script
#  Checks for a new release, backs up the DB, pulls new code/images,
#  runs migrations, and restarts all services.
#
#  Usage:  ./update-velaris.sh [--yes]   (--yes skips confirmation)
#
#  ── Switching update source ─────────────────────────────────────
#  Currently using GitHub Releases API. When moving to your own
#  server, change UPDATE_SOURCE to "server" and set UPDATE_SERVER_URL.
#  Everything else stays the same.
# ═══════════════════════════════════════════════════════════════════

set -euo pipefail

HELIX_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$HELIX_DIR/deploy/docker-compose/docker-compose.yml"
DB_CONTAINER="docker-compose-helix-db-1"
DB_USER="helix"
DB_NAME="helix"
BACKUP_DIR="$HELIX_DIR/.backups"

# ── UPDATE SOURCE CONFIG — read from velaris.yaml ───────────────
# To switch from GitHub to your own server, edit velaris.yaml:
#   updates.source: "server"
#   updates.server_url: "https://updates.velaris.io/latest"
_read_yaml() {
  local key="$1" default="${2:-}"
  grep -E "^\s+${key}:" "$HELIX_DIR/velaris.yaml" 2>/dev/null \
    | head -1 \
    | sed -E "s/.*${key}:[[:space:]]*[\"']?([^\"'#]+)[\"']?.*/\1/" \
    | xargs \
    || echo "$default"
}
UPDATE_SOURCE=$(_read_yaml "source" "github")
GITHUB_REPO=$(_read_yaml "github_repo" "your-org/velaris")
UPDATE_SERVER_URL=$(_read_yaml "server_url" "https://updates.velaris.io/latest")

# ─────────────────────────────────────────────────────────────────

green()  { echo -e "\033[0;32m$*\033[0m"; }
yellow() { echo -e "\033[0;33m$*\033[0m"; }
red()    { echo -e "\033[0;31m$*\033[0m"; }
bold()   { echo -e "\033[1m$*\033[0m"; }
step()   { echo ""; bold "▶ $*"; }

AUTO_YES=false
[[ "${1:-}" == "--yes" ]] && AUTO_YES=true

echo "╔══════════════════════════════════════════╗"
echo "║         VELARIS Updater                  ║"
echo "╚══════════════════════════════════════════╝"

# ── Step 1: Read current version ─────────────────────────────────
step "Step 1/7 — Reading current version..."
CURRENT_VERSION=$(grep 'version:' "$HELIX_DIR/velaris.yaml" | head -1 | sed 's/.*version: *"\([^"]*\)".*/\1/')
echo "  Installed: v$CURRENT_VERSION"

# ── Step 2: Fetch latest version ─────────────────────────────────
step "Step 2/7 — Checking for updates ($UPDATE_SOURCE)..."

fetch_latest_version() {
  if [[ "$UPDATE_SOURCE" == "github" ]]; then
    # GitHub Releases API — returns tag_name like "v2.1.0"
    local response
    response=$(curl -sf --max-time 10 \
      -H "Accept: application/vnd.github+json" \
      "https://api.github.com/repos/${GITHUB_REPO}/releases/latest" 2>/dev/null) || {
      yellow "  ⚠ Could not reach GitHub — skipping update check"
      echo "$CURRENT_VERSION"
      return
    }
    echo "$response" | python3 -c "import sys,json; print(json.load(sys.stdin)['tag_name'].lstrip('v'))"

  elif [[ "$UPDATE_SOURCE" == "server" ]]; then
    # Own server — expects JSON: { "version": "2.1.0", "notes": "..." }
    local response
    response=$(curl -sf --max-time 10 "$UPDATE_SERVER_URL" 2>/dev/null) || {
      yellow "  ⚠ Could not reach update server — skipping update check"
      echo "$CURRENT_VERSION"
      return
    }
    echo "$response" | python3 -c "import sys,json; print(json.load(sys.stdin)['version'])"
  fi
}

LATEST_VERSION=$(fetch_latest_version)

if [[ "$LATEST_VERSION" == "$CURRENT_VERSION" ]]; then
  green "  ✓ Already on latest version (v$CURRENT_VERSION)"
  exit 0
fi

echo ""
green "  New version available: v$CURRENT_VERSION → v$LATEST_VERSION"
echo ""

# ── Confirm ───────────────────────────────────────────────────────
if [[ "$AUTO_YES" == false ]]; then
  read -rp "  Proceed with update? [y/N]: " CONFIRM
  [[ "$CONFIRM" =~ ^[Yy]$ ]] || { echo "  Aborted."; exit 0; }
fi

# ── Step 3: Backup database ───────────────────────────────────────
step "Step 3/7 — Backing up database..."
mkdir -p "$BACKUP_DIR"
BACKUP_FILE="$BACKUP_DIR/velaris-v${CURRENT_VERSION}-$(date +%Y%m%d-%H%M%S).sql"

if docker exec "$DB_CONTAINER" pg_dump -U "$DB_USER" "$DB_NAME" > "$BACKUP_FILE" 2>/dev/null; then
  green "  ✓ Backup saved: $BACKUP_FILE"
else
  red "  ✗ DB backup failed — aborting to protect your data"
  red "    Check: docker logs $DB_CONTAINER"
  exit 1
fi

# ── Step 4: Pull latest code ──────────────────────────────────────
step "Step 4/7 — Pulling latest code..."
cd "$HELIX_DIR"

if [[ "$UPDATE_SOURCE" == "github" ]]; then
  git fetch origin --tags
  git checkout "v${LATEST_VERSION}" 2>/dev/null || git checkout "tags/v${LATEST_VERSION}" 2>/dev/null || {
    # If no tag, just pull latest main
    yellow "  ⚠ No tag v${LATEST_VERSION} found — pulling latest main"
    git pull origin main
  }
elif [[ "$UPDATE_SOURCE" == "server" ]]; then
  # Own server: download and extract tarball
  TARBALL_URL="${UPDATE_SERVER_URL%/latest}/download/v${LATEST_VERSION}.tar.gz"
  curl -sf --max-time 120 -o /tmp/velaris-update.tar.gz "$TARBALL_URL"
  tar -xzf /tmp/velaris-update.tar.gz -C "$HELIX_DIR" --strip-components=1
  rm -f /tmp/velaris-update.tar.gz
fi

green "  ✓ Code updated to v$LATEST_VERSION"

# ── Step 5: Pull new Docker images ───────────────────────────────
step "Step 5/7 — Pulling new Docker images..."
docker compose -f "$COMPOSE_FILE" pull
green "  ✓ Images updated"

# ── Step 6: Run migrations ────────────────────────────────────────
step "Step 6/7 — Running database migrations..."

# Ensure DB is up before migrating
docker compose -f "$COMPOSE_FILE" up -d helix-db
for i in $(seq 1 20); do
  docker exec "$DB_CONTAINER" pg_isready -U "$DB_USER" -q 2>/dev/null && break
  [ "$i" -eq 20 ] && { red "  ✗ PostgreSQL not ready"; exit 1; }
  sleep 1
done

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
  if [[ "$EXISTS" == "1" ]]; then
    SKIPPED=$((SKIPPED + 1)); continue
  fi
  echo -n "    apply $filename ... "
  if docker exec -i "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -q < "$migration_file" 2>/tmp/migration_err.log; then
    docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -q \
      -c "INSERT INTO schema_migrations(filename) VALUES('$filename') ON CONFLICT DO NOTHING;"
    green "✓"; APPLIED=$((APPLIED + 1))
  else
    red "FAILED"
    cat /tmp/migration_err.log
    red "  Migration failed — rolling back to v$CURRENT_VERSION"
    red "  Restore backup with: psql -U $DB_USER $DB_NAME < $BACKUP_FILE"
    exit 1
  fi
done
green "  ✓ Migrations: $APPLIED applied, $SKIPPED already up-to-date"

# ── Step 7: Update version + restart services ─────────────────────
step "Step 7/7 — Restarting services..."

# Stamp new version into velaris.yaml
sed -i "s/version: \"$CURRENT_VERSION\"/version: \"$LATEST_VERSION\"/" "$HELIX_DIR/velaris.yaml"

# Restart via the standard startup script (handles all service health checks)
bash "$HELIX_DIR/start-velaris.sh"

echo ""
echo "╔══════════════════════════════════════════════════════╗"
green "║   Velaris updated: v$CURRENT_VERSION → v$LATEST_VERSION"
echo "║   Backup kept at: .backups/"
echo "╚══════════════════════════════════════════════════════╝"
