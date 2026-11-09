#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  VELARIS Startup Script
#  Run after every reboot to bring the full environment up.
#
#  Usage:  ./start-velaris.sh          production mode (default)
#          ./start-velaris.sh --dev    development: auto-reload, Vite dev server
#
#  Mode can also be set in .env: VELARIS_RUN_MODE=production|development.
# ═══════════════════════════════════════════════════════════════════

set -euo pipefail

VELARIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# R5c: a server linked as a STANDBY runs only its two replica databases
# (standby-velaris.sh). Starting the platform here would start a second
# PostgreSQL on the replicas' own data. standby.json exists only while linked.
if [ -f "$VELARIS_DIR/.standby/standby.json" ]; then
  echo "✗ This server is linked as a Velaris standby ($VELARIS_DIR/.standby/standby.json)." >&2
  echo "  The platform must not run here while it is one. To use it on its own, unlink it" >&2
  echo "  first: ./standby-velaris.sh unlink   (on the main server)" >&2
  exit 1
fi
# An old main whose standby was promoted: running it again means two servers
# taking writes. demoted.json stays until it is linked as the new main's standby.
if [ -f "$VELARIS_DIR/.standby/demoted.json" ]; then
  echo "✗ This server was replaced by its standby ($VELARIS_DIR/.standby/demoted.json)." >&2
  echo "  Starting it would make two main servers. To bring it back as a standby:" >&2
  echo "  ./standby-velaris.sh retire   then link it from the new main." >&2
  exit 1
fi
# Step 12 (automatic failover): this main stopped itself after losing both the
# witness and its standby — the standby may have taken over. Its agent lifts
# this by itself once the witness confirms this server still holds the lease.
if [ -f "$VELARIS_DIR/.standby/fenced.json" ]; then
  echo "✗ This server stopped itself: it lost both the failover witness and its standby" >&2
  echo "  ($VELARIS_DIR/.standby/fenced.json). The standby may have taken over." >&2
  echo "  Check: ./standby-velaris.sh failover status   (it restarts by itself if it still holds the lease)" >&2
  exit 1
fi
_RUN_MODE_ARG=""
for _a in "$@"; do
  case "$_a" in
    --dev) _RUN_MODE_ARG=development ;;
    --prod|--production) _RUN_MODE_ARG=production ;;
  esac
done
COMPOSE_FILE="$VELARIS_DIR/deploy/docker-compose/docker-compose.yml"
# Scratch files and error logs go to a directory only this user can write. A
# fixed name under /tmp can be pre-created, or pointed somewhere else, by any
# other local user before this script writes to it.
VELARIS_SCRATCH="${VELARIS_SCRATCH:-${XDG_STATE_HOME:-${HOME:-}/.local/state}/velaris}"
if ! { mkdir -p "$VELARIS_SCRATCH" && chmod 700 "$VELARIS_SCRATCH" && [ -w "$VELARIS_SCRATCH" ]; } 2>/dev/null; then
  VELARIS_SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/velaris.XXXXXX")"
fi
# Which optional compose services this install runs (the bundled MinIO is kept
# only where it already exists) — see the file for the rule.
# shellcheck source=/dev/null
[ -f "$VELARIS_DIR/deploy/docker-compose/compose-profiles.sh" ] && . "$VELARIS_DIR/deploy/docker-compose/compose-profiles.sh"

# uv installs itself to ~/.local/bin, which is NOT on PATH in a fresh non-login
# shell. Add it here rather than making the operator export it — a missing uv
# used to surface as "product key verification failed", which is a lie.
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) PATH="$HOME/.local/bin:$PATH"; export PATH ;;
esac
DB_CONTAINER="docker-compose-velaris-db-1"
DB_USER="velaris"
DB_NAME="velaris"
# Database calls go through deploy/external-services.sh (sourced above with the
# compose profiles). Without that file — a partial copy — the bundled calls this
# script always made.
if ! type velaris_psql >/dev/null 2>&1; then
  velaris_is_external() { return 1; }
  velaris_psql()        { docker exec -i "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" "$@"; }
  velaris_pg_isready()  { docker exec "$DB_CONTAINER" pg_isready -h 127.0.0.1 -U "$DB_USER" -q; }
  velaris_pg_url()      { printf '%s' "${VELARIS_CASE_DATABASE_URL:-}"; }
fi

# ── Step 0: OpenBao secrets (Group K, opt-in) ─────────────────────
# Enabled by the sentinel file deploy/openbao/enabled. When active, .env is
# RENDERED from OpenBao before anything reads it. Render is fail-closed: on
# any problem the existing .env stays untouched and we continue on the
# last-known-good secrets (warning printed). No sentinel = no change at all.
if [ -f "$VELARIS_DIR/deploy/openbao/enabled" ]; then
  echo "▶ Step 0: OpenBao secrets..."
  [ -f "$VELARIS_DIR/.env" ] || touch "$VELARIS_DIR/.env"   # compose interpolation needs the file to exist
  # One-time move of file storage to raft (OpenBao 2.7 has no file backend).
  # Must run before OpenBao starts; a failure is reported and Step 0 carries on.
  "$VELARIS_DIR/scripts/openbao-storage-migrate.sh" \
    || echo -e "  \033[0;33m⚠ OpenBao storage migration failed — see above\033[0m"
  # Every failure below degrades to "continue on the existing .env" — Step 0
  # must never be the reason the platform fails to start (set -e is active).
  if ! docker compose -f "$COMPOSE_FILE" --env-file "$VELARIS_DIR/.env" up -d openbao 2>/dev/null; then
    echo -e "  \033[0;33m⚠ Could not start the OpenBao container\033[0m"
  fi
  BAO_HEALTH="000"
  for i in $(seq 1 15); do
    BAO_HEALTH=$(curl -s -m 2 -o /dev/null -w '%{http_code}' \
      "http://127.0.0.1:8350/v1/sys/health?sealedcode=472&uninitcode=471" || true)
    [ "$BAO_HEALTH" != "000" ] && break
    sleep 1
  done
  if [ "$BAO_HEALTH" = "472" ]; then
    echo "  Unsealing OpenBao from local keyfile..."
    if "$VELARIS_DIR/scripts/secrets-unseal.sh" >/dev/null 2>&1; then
      BAO_HEALTH=200
    else
      echo -e "  \033[0;33m⚠ Unseal failed (bad or unreadable keyfile)\033[0m"
    fi
  fi
  if [ "$BAO_HEALTH" = "200" ] || [ "$BAO_HEALTH" = "429" ]; then
    if "$VELARIS_DIR/scripts/secrets-render.sh"; then
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
if [ -f "$VELARIS_DIR/.env" ]; then
  set -o allexport; source "$VELARIS_DIR/.env"; set +o allexport
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
# If systemd already supervises Velaris, starting a second copy from a shell
# gives two processes racing for 8100/8201/8202/8203/8204/8205/8206/8207/8208/5173. The nohup copy usually wins
# the bind, systemd's restarts fail quietly, and the whole thing disappears at
# the next logout. Refuse instead.
if systemctl is-active --quiet velaris-case-service 2>/dev/null \
   || systemctl is-active --quiet velaris-engine 2>/dev/null; then
  yellow "  ⚠ Velaris is already running under systemd."
  # Name only the units this box has: commerce-, portal- and user-service run
  # inside case-service's unit until `sudo deploy/systemd/install.sh` installs
  # their own (R5e steps 7, 8, 10).
  _units="velaris-engine velaris-case-service velaris-analytics-service velaris-hxgraph-service velaris-hxmigrate-service"
  [ -f "${VELARIS_UNIT_DIR:-/etc/systemd/system}/velaris-commerce-service.service" ] && _units="$_units velaris-commerce-service"
  [ -f "${VELARIS_UNIT_DIR:-/etc/systemd/system}/velaris-portal-service.service" ] && _units="$_units velaris-portal-service"
  [ -f "${VELARIS_UNIT_DIR:-/etc/systemd/system}/velaris-user-service.service" ] && _units="$_units velaris-user-service"
  [ -f "${VELARIS_UNIT_DIR:-/etc/systemd/system}/velaris-worker-service.service" ] && _units="$_units velaris-worker-service"
  _units="$_units velaris-studio"
  echo   "    Status:   sudo systemctl status $_units"
  echo   "    Restart:  sudo systemctl restart $_units"
  echo   "    Logs:     sudo journalctl -u velaris-case-service -f"
  echo ""
  echo   "    To run from this shell instead:"
  echo   "      sudo systemctl stop $_units"
  exit 0
fi

# A missing toolchain is a toolchain problem — report it as one instead of
# blaming the licence, which sent the operator hunting the wrong thing.
if ! command -v uv >/dev/null 2>&1; then
  red "  ✗ 'uv' is not installed (or not on PATH)."
  echo "    Install it with:  curl -LsSf https://astral.sh/uv/install.sh | sh"
  echo "    Then re-run ./start-velaris.sh"
  exit 1
fi

echo "▶ Verifying product key…"
set +e
uv run python "$VELARIS_DIR/scripts/verify_key.py"
KEY_STATUS=$?
set -e
if [ "$KEY_STATUS" -eq 1 ]; then
  echo ""
  red "╔═══════════════════════════════════════════════════════╗"
  red "║  STARTUP BLOCKED: product key verification failed.   ║"
  red "║  Run ./setup-velaris.sh if this is a new install.    ║"
  red "║  Contact support: velaris.app.os@gmail.com                   ║"
  red "╚═══════════════════════════════════════════════════════╝"
  exit 1
elif [ "$KEY_STATUS" -eq 2 ]; then
  # Not activated yet. Starting is REQUIRED here: the registration website
  # calls this platform's API to create the customer's user, so the key
  # cannot exist until these services are up. See scripts/verify_key.py.
  yellow "  ⚠ Running unactivated — activate with ./activate-velaris.sh once you have a key."
fi
echo ""

# ── Update check (non-blocking notice) ──────────────────────────
_yaml_val() {
  grep -E "^\s+${1}:" "$VELARIS_DIR/velaris.yaml" 2>/dev/null \
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
  # TLS when VELARIS_CASE_DATABASE_URL names ?ssl_ca= (deploy/external-services.sh);
  # nothing added otherwise.
  _MY_TLS_MOUNTS=(); _MY_TLS_FLAGS=()
  if type velaris_mysql_tls_mounts >/dev/null 2>&1; then
    mapfile -t _MY_TLS_MOUNTS < <(velaris_mysql_tls_mounts)
    mapfile -t _MY_TLS_FLAGS < <(velaris_mysql_tls_flags)
  fi
  mysql_client() {
    MYSQL_PWD="$DB_PASSWORD" docker run --rm -i --network host -e MYSQL_PWD "${_MY_TLS_MOUNTS[@]}" mysql:8.4.11 \
      mysql --connect-timeout=10 -h "$DB_HOST" -P "$DB_PORT" -u "$DB_USER" "${_MY_TLS_FLAGS[@]}" "$@"
  }
fi

# ── Guard: parts on other servers must be secured ────────────────
# VELARIS_EXTERNAL_SERVICES unset = everything bundled, nothing checked. When a
# part is listed, starting without its TLS/authentication settings is refused
# (deploy/external-services.sh names what is missing).
if type velaris_external_check >/dev/null 2>&1 && ! velaris_external_check; then
  exit 1
fi

# ── Guard: Docker accessible? ────────────────────────────────────
if ! docker info &>/dev/null 2>&1; then
  red "ERROR: Docker not accessible. Run setup-velaris.sh first, then log out/in."
  exit 1
fi

# ── Step 0: Free ports and clean old containers ──────────────────
echo "▶ Preparing ports..."
# --remove-orphans is load-bearing. A plain `down` only touches services still
# named in the compose file, so anything that gets renamed leaves its old
# container running and holding its published port. That is exactly what the
# 2026-08-07 helix-*→velaris-* rename did: docker-compose-helix-db-1 survived
# the down and kept 127.0.0.1:5432, and the next `up` died with "port is
# already allocated".
docker compose -f "$COMPOSE_FILE" --env-file "$VELARIS_DIR/.env" down --remove-orphans 2>/dev/null || true
sudo systemctl stop postgresql 2>/dev/null || true

# Host processes only — these ports belong to uvicorn/vite, not to Docker.
for port in 8100 8201 8202 8203 8204 8205 8206 8207 8208 5173; do
  fuser -k "${port}/tcp" 2>/dev/null || true
done

# 5432 is handled separately and never blind-killed. When Docker holds it the
# listener is docker-proxy, and killing that does NOT release the daemon's port
# reservation — it just strands it, so every later `up` fails with "port is
# already allocated" until dockerd restarts. The `down` above is what frees a
# container-held 5432; only a genuine host Postgres gets killed here.
if fuser 5432/tcp &>/dev/null 2>&1; then
  if docker ps --format '{{.Ports}}' 2>/dev/null | grep -q ':5432->'; then
    yellow "  ⚠ Port 5432 is held by a running container — leaving it to compose"
  else
    fuser -k 5432/tcp 2>/dev/null || true
  fi
fi
sleep 2
green "  ✓ Ports ready"
echo ""

# ── Step 1: Start Docker infrastructure ──────────────────────────
echo "▶ Starting Docker infrastructure..."
# Redpanda was replaced by Kafka; remove a leftover broker (compose-profiles.sh).
type velaris_retire_redpanda >/dev/null 2>&1 && velaris_retire_redpanda
# A pooler turned off since the last start (compose-profiles.sh).
type velaris_retire_pooler >/dev/null 2>&1 && velaris_retire_pooler
# Event bus on without a broker password (bus turned on later, or an install
# from before the password): generate and store one (compose-profiles.sh).
type velaris_ensure_event_bus_password >/dev/null 2>&1 && velaris_ensure_event_bus_password
# Cache turned on without a password: give it one (Valkey restarts with it).
type velaris_ensure_cache_password >/dev/null 2>&1 && velaris_ensure_cache_password
# The internal service secret (R5e step 7) — every install has one.
type velaris_ensure_internal_secret >/dev/null 2>&1 && velaris_ensure_internal_secret
# The gateway's optional settings, as include files (never template variables).
type velaris_gateway_options >/dev/null 2>&1 && velaris_gateway_options
# deploy/tls/certs before Docker can create it as root (the gateway mounts it).
# shellcheck source=deploy/tls/certs-dir.sh
[ -f "$VELARIS_DIR/deploy/tls/certs-dir.sh" ] && . "$VELARIS_DIR/deploy/tls/certs-dir.sh" && velaris_tls_certs_dir "$VELARIS_DIR"
# Parts on other servers (VELARIS_EXTERNAL_SERVICES): their bundled containers
# are not started. Nothing external = the list is empty = a plain `up -d`.
_UP_SVCS=""
if type velaris_compose_up_services >/dev/null 2>&1; then
  velaris_retire_external_containers "$COMPOSE_FILE" "$VELARIS_DIR/.env"
  _UP_SVCS="$(velaris_compose_up_services "$COMPOSE_FILE" "$VELARIS_DIR/.env")" || exit 1
fi
# shellcheck disable=SC2086
docker compose -f "$COMPOSE_FILE" --env-file "$VELARIS_DIR/.env" up -d $_UP_SVCS
echo "  Waiting for services to initialise..."
sleep 10

# Group K: the global down/up above restarted OpenBao sealed. .env is already
# rendered (Step 0); re-unseal so day-2 secret operations work without manual
# intervention. Failure is non-fatal — the platform runs fine sealed.
if [ -f "$VELARIS_DIR/deploy/openbao/enabled" ]; then
  "$VELARIS_DIR/scripts/secrets-unseal.sh" 2>/dev/null \
    && green "  ✓ OpenBao unsealed" \
    || yellow "  ⚠ OpenBao still sealed — run ./scripts/secrets-unseal.sh before pushing secrets"
fi

echo ""
echo "  Checking containers:"
_CHECK_SVCS="velaris-db temporal temporal-ui cache mailpit"
[ "${VELARIS_BUNDLED_MINIO_ACTIVE:-false}" = true ] && _CHECK_SVCS="$_CHECK_SVCS minio"
_EXT_SVCS=" $(type velaris_external_compose_services >/dev/null 2>&1 && velaris_external_compose_services) "
for svc in $_CHECK_SVCS; do
  case "$_EXT_SVCS" in *" $svc "*) green "    – $svc on another server"; continue ;; esac
  if docker ps --format '{{.Names}}' | grep -q "$svc"; then
    green "    ✓ $svc running"
  else
    yellow "    ⚠ $svc not found (may be optional or slow to start)"
  fi
done
echo ""

# ── Step 2: Wait for Temporal ────────────────────────────────────
_TEMPORAL_TARGET="$(type velaris_temporal_target >/dev/null 2>&1 && velaris_temporal_target || echo localhost:7233)"
_TEMPORAL_HOST="${_TEMPORAL_TARGET%:*}"; _TEMPORAL_PORT="${_TEMPORAL_TARGET##*:}"
echo "▶ Waiting for Temporal ($_TEMPORAL_TARGET)..."
for i in $(seq 1 30); do
  if nc -z "$_TEMPORAL_HOST" "$_TEMPORAL_PORT" 2>/dev/null; then
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
  if velaris_is_external database; then
    echo "▶ Waiting for PostgreSQL (on another server, over TLS)..."
  else
    echo "▶ Waiting for PostgreSQL (port 5432)..."
  fi
  for i in $(seq 1 20); do
    if velaris_pg_isready 2>/dev/null; then
      green "  ✓ PostgreSQL ready"; break
    fi
    if [ "$i" -eq 20 ]; then
      if velaris_is_external database; then
        red "  ✗ PostgreSQL on another server not reachable — check VELARIS_CASE_DATABASE_URL, its sslrootcert, and the server's firewall"
      else
        red "  ✗ PostgreSQL not ready — check: docker logs $DB_CONTAINER"
      fi
      exit 1
    fi
    sleep 1
  done
  # Query statistics (VELARIS_DB_QUERY_STATS): applied before the services
  # connect, so the one restart it may need lands on an idle database. Unset =
  # untouched; a database on another server is skipped by the script.
  "$VELARIS_DIR/scripts/db-query-stats.sh" || yellow "  ⚠ query statistics not applied — ./scripts/db-query-stats.sh"
  # R5d 6a: the platform will connect through the bundled pooler, so it must be
  # up. Never a silent switch to direct: that would put on the database the
  # load the operator chose to pool. Not active (setting empty) = no wait.
  if [ "${VELARIS_POOLER_ACTIVE:-false}" = true ]; then
    echo "▶ Waiting for the connection pooler (port 6432)..."
    for i in $(seq 1 20); do
      _pcid="$(docker compose -f "$COMPOSE_FILE" --env-file "$VELARIS_DIR/.env" ps -q velaris-pooler 2>/dev/null || true)"
      if [ -n "$_pcid" ] && [ "$(docker inspect -f '{{.State.Health.Status}}' "$_pcid" 2>/dev/null)" = healthy ]; then
        # Over its socket (VELARIS_CASE_DATABASE_POOLER_SOCKET): the socket must
        # be there too — the platform has no TCP fallback, by design.
        _psock="$(grep -E '^VELARIS_CASE_DATABASE_POOLER_SOCKET=' "$VELARIS_DIR/.env" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d "\"' " | tr '[:upper:]' '[:lower:]')"
        case "$_psock" in
          1|true|yes|on)
            if [ -S "$VELARIS_DIR/deploy/docker-compose/pooler-socket/.s.PGSQL.6432" ]; then
              green "  ✓ Connection pooler ready (socket)"; break
            fi ;;
          *) green "  ✓ Connection pooler ready"; break ;;
        esac
      fi
      if [ "$i" -eq 20 ]; then
        red "  ✗ Connection pooler not ready — check: docker compose logs velaris-pooler"
        red "    (or empty VELARIS_CASE_DATABASE_POOLER_URL in .env to connect directly)"
        red "    With VELARIS_CASE_DATABASE_POOLER_SOCKET on: deploy/docker-compose/pooler-socket must"
        red "    belong to this install's owner (the pooler refuses a root-owned one)."
        exit 1
      fi
      sleep 1
    done
  fi
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
  for migration_file in $(find "$VELARIS_DIR/migrations/mysql" -maxdepth 1 -name "*.sql" | sort); do
    filename="$(basename "$migration_file")"
    EXISTS=$(mysql_client "$DB_NAME" -N -e "SELECT 1 FROM schema_migrations WHERE filename='$filename';" 2>/dev/null || echo "")
    if [ "$EXISTS" = "1" ]; then
      echo "    skip  $filename"; SKIPPED=$((SKIPPED + 1)); continue
    fi
    echo -n "    apply $filename ... "
    if mysql_client "$DB_NAME" < "$migration_file" 2>"$VELARIS_SCRATCH/migration_err.log"; then
      mysql_client "$DB_NAME" -e "INSERT IGNORE INTO schema_migrations(filename) VALUES('$filename');"
      green "✓"; APPLIED=$((APPLIED + 1))
    else
      red "FAILED"; cat "$VELARIS_SCRATCH/migration_err.log"
      red "  Migration failed — stopping."; exit 1
    fi
  done
  echo ""
  green "  ✓ Migrations: $APPLIED applied, $SKIPPED already up-to-date"
  echo ""
else
velaris_psql -q <<'SQL'
CREATE TABLE IF NOT EXISTS schema_migrations (
  filename   TEXT PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
SQL

APPLIED=0; SKIPPED=0; FAILED=0

# What is already applied, read ONCE: one query, not one per file — against a
# database on another server each query is a client container (~1 s).
_APPLIED_MIGRATIONS="$(velaris_psql -tAq -c "SELECT filename FROM schema_migrations;" </dev/null 2>/dev/null || true)"
for migration_file in $(find "$VELARIS_DIR/migrations/postgresql" -maxdepth 1 -name "*.sql" | sort); do
  filename="$(basename "$migration_file")"
  EXISTS=""; grep -qxF "$filename" <<<"$_APPLIED_MIGRATIONS" && EXISTS=1
  if [ "$EXISTS" = "1" ]; then
    echo "    skip  $filename"; SKIPPED=$((SKIPPED + 1)); continue
  fi
  echo -n "    apply $filename ... "
  # ON_ERROR_STOP is what makes a failure a failure. Without it psql runs the
  # rest of the file, exits 0, and the migration is recorded as applied — which
  # is how an install ended up with 080 "applied" and its column unchanged
  # (repaired by 217). setup-velaris.sh and update-velaris.sh already had it;
  # this runner was missed until 2026-10-11.
  if velaris_psql -q \
      -v ON_ERROR_STOP=1 < "$migration_file" 2>"$VELARIS_SCRATCH/migration_err.log"; then
    velaris_psql -q \
      -c "INSERT INTO schema_migrations(filename) VALUES('$filename') ON CONFLICT DO NOTHING;" </dev/null
    green "✓"; APPLIED=$((APPLIED + 1))
  else
    red "FAILED"; cat "$VELARIS_SCRATCH/migration_err.log"; FAILED=$((FAILED + 1))
    red "  Migration failed — stopping."; exit 1
  fi
done
echo ""
green "  ✓ Migrations: $APPLIED applied, $SKIPPED already up-to-date"
echo ""
fi

# ── Step 4b: Sync release manifest ───────────────────────────────
# Applies releases/manifest.sql on EVERY start. The file is an idempotent
# upsert, so this is cheap and self-correcting.
#
# It used to run only when the INSERT count differed from the row count in
# scheduled_releases. That made a version bump unreachable: adding a feature
# changed the count and applied, but editing an existing feature's version left
# the counts equal, so the manifest was never re-applied — and even if it had
# been, ON CONFLICT DO NOTHING would have skipped the row. Both halves of that
# are fixed; the count is now reported, not used as a gate.
# MySQL uses the dialect sibling manifest.mysql.sql (UUID() / ON DUPLICATE KEY).
if [ "$DB_FAMILY" = "mysql" ]; then
  MANIFEST_FILE="$VELARIS_DIR/releases/manifest.mysql.sql"
else
  MANIFEST_FILE="$VELARIS_DIR/releases/manifest.sql"
fi
if [ -f "$MANIFEST_FILE" ]; then
  MANIFEST_COUNT=$(grep -c "^INSERT" "$MANIFEST_FILE" 2>/dev/null || echo 0)
  if [ "$DB_FAMILY" = "mysql" ]; then
    DB_COUNT=$(mysql_client "$DB_NAME" -N -e "SELECT COUNT(*) FROM scheduled_releases;" 2>/dev/null | tr -d '[:space:]' || echo 0)
  else
    DB_COUNT=$(velaris_psql -tAq \
      -c "SELECT COUNT(*) FROM scheduled_releases;" </dev/null 2>/dev/null | tr -d '[:space:]' || echo 0)
  fi
  echo "▶ Syncing release manifest ($DB_COUNT features in DB, $MANIFEST_COUNT in manifest)..."
  if [ "$DB_FAMILY" = "mysql" ]; then
    _man_ok=0; mysql_client "$DB_NAME" < "$MANIFEST_FILE" 2>"$VELARIS_SCRATCH/manifest_err.log" && _man_ok=1
  else
    _man_ok=0; velaris_psql -q < "$MANIFEST_FILE" 2>"$VELARIS_SCRATCH/manifest_err.log" && _man_ok=1
  fi
  if [ "$_man_ok" = "1" ]; then
    if [ "$MANIFEST_COUNT" -gt "$DB_COUNT" ]; then
      green "  ✓ Release manifest synced — $((MANIFEST_COUNT - DB_COUNT)) new feature(s) activated"
    else
      green "  ✓ Release manifest synced ($MANIFEST_COUNT features, versions refreshed)"
    fi
  else
    yellow "  ⚠ Release manifest sync had warnings:"; cat "$VELARIS_SCRATCH/manifest_err.log"
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
elif velaris_is_external database; then
  # The platform's own URL and TLS (check_superadmin.py reads the TLS from
  # VELARIS_CASE_DATABASE_URL). Passed in the environment, never on a command line.
  SUPERADMIN_DB_URL="$(velaris_pg_url)"
else
  SUPERADMIN_DB_URL="postgresql://velaris:${VELARIS_DB_PASSWORD:-velaris_dev_password}@localhost:5432/velaris"
fi
DATABASE_URL="$SUPERADMIN_DB_URL" \
  uv run python "$VELARIS_DIR/scripts/check_superadmin.py" || {
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
cd "$VELARIS_DIR"
# Optional ML extras are NOT part of the default install (a base box stays lean,
# no model weights). But `uv sync` is exact by default — it prunes anything not
# declared, so an operator who installed an extra would silently lose it on the
# next run. Detect what is already present and ask for it again.
_EXTRAS=""
uv run python -c "import faster_whisper" >/dev/null 2>&1 && _EXTRAS="$_EXTRAS --extra intelligence"
uv run python -c "import av" >/dev/null 2>&1 && _EXTRAS="$_EXTRAS --extra kyc"
# The MySQL/MariaDB driver (aiomysql + pymysql) is an extra: without it a
# MySQL/MariaDB install cannot reach its own database, and `uv sync` (exact)
# would remove one installed by hand. Fixed 2026-10-16 — it was never installed.
case "${DATABASE_BACKEND:-postgresql}" in mysql|mariadb) _EXTRAS="$_EXTRAS --extra mysql" ;; esac
# The event bus client: needed when the bus is on (compose-profiles.sh), kept
# when already present. Without it the relay stays off even with the bus "on".
if [ "${VELARIS_EVENT_BROKER_ACTIVE:-false}" = true ] \
   || uv run python -c "import aiokafka" >/dev/null 2>&1; then
  _EXTRAS="$_EXTRAS --extra events"
fi
uv sync --all-packages $_EXTRAS --quiet 2>/dev/null && green "  ✓ Dependencies up to date$([ -n "$_EXTRAS" ] && echo " (extras preserved:$_EXTRAS)")" || yellow "  ⚠ uv sync had warnings — continuing"
echo ""

# ── Step 5: Set environment variables ────────────────────────────
export VELARIS_SERVICE_REGISTRY='{"order-service":"http://localhost:3001","notification-service":"http://localhost:3002"}'

# DB SDK: the database backend was already detected, allowlist-gated, and exported
# near the top (so Steps 3/4/4b/Gate-2 could branch). The Python layer
# (velaris_shared.db.backends) enforces the same allowlist — fail-closed both sides.
green "  ✓ Environment ready (database: $DATABASE_BACKEND)"

# ── Step 5b: Check Ollama ─────────────────────────────────────────
if velaris_is_external ai; then
  # Ollama on another server, behind a proxy that checks the bearer key. The
  # key goes to curl on stdin (-K -), never on a command line.
  _OLLAMA_URL="${VELARIS_CASE_AI_OLLAMA_URL%/}"
  echo "▶ Checking Ollama (HxNexus AI backend, on another server)..."
  if [ -z "$_OLLAMA_URL" ]; then
    green "  – no Ollama URL set — the platform uses no Ollama"
  elif printf 'header = "Authorization: Bearer %s"\n' "${VELARIS_CASE_AI_OLLAMA_API_KEY:-}" \
       | curl -sf --max-time 5 ${VELARIS_CASE_AI_OLLAMA_TLS_CA:+--cacert "$VELARIS_CASE_AI_OLLAMA_TLS_CA"} \
           -K - "$_OLLAMA_URL/api/tags" > /dev/null 2>&1; then
    green "  ✓ Ollama reachable at $_OLLAMA_URL"
  else
    yellow "  ⚠ Ollama at $_OLLAMA_URL not reachable (or the key was refused) — HxNexus will show offline"
  fi
else
echo "▶ Checking Ollama (HxNexus AI backend, port 11434)..."
if curl -s --max-time 2 http://localhost:11434/api/tags > /dev/null 2>&1; then
  # An online Ollama with NO models pulled makes every AI feature (chat,
  # case Q&A, session summaries) fail soft with empty answers — warn loudly.
  MODEL_COUNT=$(curl -s --max-time 2 http://localhost:11434/api/tags \
    | python3 -c "import sys,json; print(len(json.load(sys.stdin).get('models',[])))" 2>/dev/null || echo "?")
  if [ "$MODEL_COUNT" = "0" ]; then
    yellow "  ⚠ Ollama online but NO models pulled — HxNexus answers will be empty."
    yellow "    Pull the defaults:  docker exec docker-compose-ollama-1 ollama pull ${VELARIS_CASE_AI_OLLAMA_MODEL:-llama3.2}"
    yellow "                        docker exec docker-compose-ollama-1 ollama pull ${VELARIS_CASE_AI_OLLAMA_EMBED_MODEL:-nomic-embed-text}"
  else
    green "  ✓ Ollama online ($MODEL_COUNT model(s)) — HxNexus ready"
  fi
else
  yellow "  ⚠ Ollama not reachable — HxNexus will show offline"
fi
fi
echo ""

# ── Internal TLS (roadmap #18) ────────────────────────────────────
# Off by default. `_velaris_tls_args` prints nothing unless
# VELARIS_INTERNAL_TLS_ENABLED=true, so the launch commands below are byte-for-
# byte what they were. Defined as a no-op FIRST so the script still runs if the
# helper file is absent — a missing optional file must not stop the platform.
# One source for the ports (2026-08-24). Defaults are the historical values, so
# an install that sets none of these keeps exactly the ports it had.
# shellcheck source=/dev/null
[ -f "$VELARIS_DIR/deploy/ports/velaris-ports.sh" ] && . "$VELARIS_DIR/deploy/ports/velaris-ports.sh"
: "${VELARIS_ENGINE_PORT:=8100}" "${VELARIS_CASE_PORT:=8201}" "${VELARIS_ANALYTICS_PORT:=8202}"
: "${VELARIS_HXGRAPH_PORT:=8203}" "${VELARIS_HXMIGRATE_PORT:=8204}" "${VELARIS_STUDIO_PORT:=5173}"
: "${VELARIS_COMMERCE_PORT:=8207}" "${VELARIS_PORTAL_PORT:=8205}" "${VELARIS_WORKER_PORT:=8208}"
: "${VELARIS_USER_PORT:=8206}"
# A mistyped port is the default too, with a line saying so (R1).
. "$VELARIS_DIR/deploy/ports.sh"
for _p in VELARIS_ENGINE_PORT:8100 VELARIS_CASE_PORT:8201 VELARIS_ANALYTICS_PORT:8202 \
          VELARIS_HXGRAPH_PORT:8203 VELARIS_HXMIGRATE_PORT:8204 VELARIS_STUDIO_PORT:5173 \
          VELARIS_COMMERCE_PORT:8207 VELARIS_PORTAL_PORT:8205 VELARIS_WORKER_PORT:8208 \
          VELARIS_USER_PORT:8206; do
  printf -v "${_p%%:*}" '%s' "$(_velaris_port "${_p%%:*}" "${_p##*:}")"
done

_velaris_tls_args() { :; }

# ── Run mode (Nov_TODO R5e step 1, 2026-10-16) ───────────────────
# production (default): no auto-reload, Studio served from its built bundle,
#   logs kept. development (--dev, or VELARIS_RUN_MODE=development): what this
#   script always did — uvicorn --reload and the Vite dev server.
# ONE worker per service in both modes, deliberately: case-service starts its
#   background loops (backup and update watchers, event-bus relay, schedulers)
#   in every process, so N workers would run them N times, and without the
#   shared cache live updates and per-process caches would split across workers.
#   More workers come after shared state (R5e item 4) and the worker-service
#   split (item 6) — not before.
VELARIS_RUN_MODE="${_RUN_MODE_ARG:-${VELARIS_RUN_MODE:-production}}"
case "$VELARIS_RUN_MODE" in development|dev) VELARIS_RUN_MODE=development ;; *) VELARIS_RUN_MODE=production ;; esac
# Logs: an owner-only directory in the install, rotated at every start (5 kept).
# Never fixed names under /tmp: another local user can pre-create those as
# symlinks and have the service's output written into a file of their choosing.
VELARIS_LOG_DIR="${VELARIS_LOG_DIR:-$VELARIS_DIR/logs}"
mkdir -p "$VELARIS_LOG_DIR" && chmod 700 "$VELARIS_LOG_DIR"
_svc_log() {
  local f="$VELARIS_LOG_DIR/velaris-$1.log" i
  for i in 4 3 2 1; do [ -f "$f.$i" ] && mv -f "$f.$i" "$f.$((i + 1))"; done
  [ -f "$f" ] && mv -f "$f" "$f.1"
  ( umask 077; : > "$f" )
  printf '%s' "$f"
}
# uvicorn reload flags — only in development. Each argument is a source dir to watch.
_reload_args() {
  [ "$VELARIS_RUN_MODE" = development ] || return 0
  printf -- '--reload'
  local d; for d in "$@"; do printf -- ' --reload-dir %s' "$d"; done
}
echo "  Run mode: $VELARIS_RUN_MODE (logs: $VELARIS_LOG_DIR)"

# Same reasoning: a plain http probe is exactly what the script did before.
_velaris_probe() { curl -sf --max-time 3 "http://localhost:$1/health" >/dev/null 2>&1; }
# shellcheck source=/dev/null
[ -f "$VELARIS_DIR/deploy/tls/uvicorn-tls-args.sh" ] && . "$VELARIS_DIR/deploy/tls/uvicorn-tls-args.sh"
# Processes per service (VELARIS_WORKERS_<SERVICE>) — prints nothing when unset or 1.
_velaris_workers_args() { :; }
# shellcheck source=/dev/null
[ -f "$VELARIS_DIR/deploy/workers-args.sh" ] && . "$VELARIS_DIR/deploy/workers-args.sh"

# ── Step 6: Start Engine ──────────────────────────────────────────
echo "▶ Starting Velaris Engine (port 8100)..."
cd "$VELARIS_DIR"
# Loopback-only: the engine is called by case-service (VELARIS_CASE_ENGINE_API_URL)
# and by Studio's vite proxy, both from this host. Bound to 0.0.0.0 it was a
# public, unauthenticated port on any server whose firewall was not up.
# Watch only this service's own source and the shared package. Without
# --reload-dir the reloader watches the whole repo — .venv, node_modules,
# Velaris/, dist — which is ~18,600 inotify watches EACH. Since #13b-e made
# this four processes that is 65,000+ of the kernel's 65,536 default, and the
# next thing to start (Studio's vite) dies with ENOSPC. Seen 2026-08-24.
_LOG_ENGINE="$(_svc_log engine)"
nohup uv run uvicorn helix_engine.main:app \
  --host 127.0.0.1 --port "${VELARIS_ENGINE_PORT}" $(_reload_args engine) \
  --proxy-headers \
  > "$_LOG_ENGINE" 2>&1 &
echo "  PID: $! | Logs: tail -f $_LOG_ENGINE"
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
# shellcheck disable=SC2046 - deliberate word splitting; the values are ours
_LOG_CASE_SERVICE="$(_svc_log case-service)"
nohup uv run uvicorn case_service.main:app $(_velaris_tls_args case-service) $(_velaris_workers_args case-service) \
  --host 127.0.0.1 --port "${VELARIS_CASE_PORT}" $(_reload_args services/case-service services/_shared) \
  --proxy-headers \
  --app-dir services/case-service \
  > "$_LOG_CASE_SERVICE" 2>&1 &
echo "  PID: $! | Logs: tail -f $_LOG_CASE_SERVICE"
for i in $(seq 1 20); do
  if _velaris_probe "${VELARIS_CASE_PORT}"; then
    green "  ✓ Case Service ready on port 8201"; break
  fi
  [ "$i" -eq 20 ] && yellow "  ⚠ Case Service not ready after 20s"
  sleep 1
done
echo ""

# ── Step 7b: Start Analytics Service ──────────────────────────────
# Extracted from case-service at roadmap 13c. Loopback-only for the same
# reason case-service is: the API gateway on 8200 is the only door, and a
# second door on 0.0.0.0 would go around it.
echo "▶ Starting Analytics Service (loopback 127.0.0.1:8202, reachable via the gateway on 8200)..."
cd "$VELARIS_DIR"
# shellcheck disable=SC2046 - deliberate word splitting; the values are ours
_LOG_ANALYTICS_SERVICE="$(_svc_log analytics-service)"
nohup uv run uvicorn analytics_service.main:app $(_velaris_tls_args analytics-service) $(_velaris_workers_args analytics-service) \
  --host 127.0.0.1 --port "${VELARIS_ANALYTICS_PORT}" $(_reload_args services/analytics-service services/_shared) \
  --proxy-headers \
  --app-dir services/analytics-service \
  > "$_LOG_ANALYTICS_SERVICE" 2>&1 &
echo "  PID: $! | Logs: tail -f $_LOG_ANALYTICS_SERVICE"
for i in $(seq 1 20); do
  if _velaris_probe "${VELARIS_ANALYTICS_PORT}"; then
    green "  ✓ Analytics Service ready on port 8202"; break
  fi
  [ "$i" -eq 20 ] && yellow "  ⚠ Analytics Service not ready after 20s"
  sleep 1
done
echo ""

# ── Step 7c: Start HxGraph Service ────────────────────────────────
# Extracted from case-service at roadmap 13d. Loopback-only, like the rest.
echo "▶ Starting HxGraph Service (loopback 127.0.0.1:8203, reachable via the gateway on 8200)..."
cd "$VELARIS_DIR"
# shellcheck disable=SC2046 - deliberate word splitting; the values are ours
_LOG_HXGRAPH_SERVICE="$(_svc_log hxgraph-service)"
nohup uv run uvicorn hxgraph_service.main:app $(_velaris_tls_args hxgraph-service) $(_velaris_workers_args hxgraph-service) \
  --host 127.0.0.1 --port "${VELARIS_HXGRAPH_PORT}" $(_reload_args services/hxgraph-service services/_shared) \
  --proxy-headers \
  --app-dir services/hxgraph-service \
  > "$_LOG_HXGRAPH_SERVICE" 2>&1 &
echo "  PID: $! | Logs: tail -f $_LOG_HXGRAPH_SERVICE"
for i in $(seq 1 20); do
  if _velaris_probe "${VELARIS_HXGRAPH_PORT}"; then
    green "  ✓ HxGraph Service ready on port 8203"; break
  fi
  [ "$i" -eq 20 ] && yellow "  ⚠ HxGraph Service not ready after 20s"
  sleep 1
done
echo ""

# ── Step 7d: Start HxMigrate Service ──────────────────────────────
# Extracted from case-service at roadmap 13e — the migration domain
# (hxmigrate + bpm_importer + scout + orchestrator). Loopback-only.
echo "▶ Starting HxMigrate Service (loopback 127.0.0.1:8204, reachable via the gateway on 8200)..."
cd "$VELARIS_DIR"
# shellcheck disable=SC2046 - deliberate word splitting; the values are ours
_LOG_HXMIGRATE_SERVICE="$(_svc_log hxmigrate-service)"
nohup uv run uvicorn hxmigrate_service.main:app $(_velaris_tls_args hxmigrate-service) $(_velaris_workers_args hxmigrate-service) \
  --host 127.0.0.1 --port "${VELARIS_HXMIGRATE_PORT}" $(_reload_args services/hxmigrate-service services/_shared) \
  --proxy-headers \
  --app-dir services/hxmigrate-service \
  > "$_LOG_HXMIGRATE_SERVICE" 2>&1 &
echo "  PID: $! | Logs: tail -f $_LOG_HXMIGRATE_SERVICE"
for i in $(seq 1 20); do
  if _velaris_probe "${VELARIS_HXMIGRATE_PORT}"; then
    green "  ✓ HxMigrate Service ready on port 8204"; break
  fi
  [ "$i" -eq 20 ] && yellow "  ⚠ HxMigrate Service not ready after 20s"
  sleep 1
done
echo ""

# ── Step 7e: Start Commerce Service ──────────────────────────────
# Extracted from case-service at R5e step 7 — checkout, payments, storefront,
# marketplace and the model market. Loopback-only; asks case-service for case
# work over its internal API (VELARIS_INTERNAL_SERVICE_SECRET).
echo "▶ Starting Commerce Service (loopback 127.0.0.1:${VELARIS_COMMERCE_PORT}, reachable via the gateway on 8200)..."
cd "$VELARIS_DIR"
# shellcheck disable=SC2046 - deliberate word splitting; the values are ours
_LOG_COMMERCE_SERVICE="$(_svc_log commerce-service)"
VELARIS_SERVICE_NAME=commerce-service \
nohup uv run uvicorn commerce_service.main:app $(_velaris_tls_args commerce-service) $(_velaris_workers_args commerce-service) \
  --host 127.0.0.1 --port "${VELARIS_COMMERCE_PORT}" $(_reload_args services/commerce-service services/_shared) \
  --proxy-headers \
  --app-dir services/commerce-service \
  > "$_LOG_COMMERCE_SERVICE" 2>&1 &
echo "  PID: $! | Logs: tail -f $_LOG_COMMERCE_SERVICE"
for i in $(seq 1 20); do
  if _velaris_probe "${VELARIS_COMMERCE_PORT}"; then
    green "  ✓ Commerce Service ready on port ${VELARIS_COMMERCE_PORT}"; break
  fi
  [ "$i" -eq 20 ] && yellow "  ⚠ Commerce Service not ready after 20s"
  sleep 1
done
echo ""

# ── Step 7f: Start Portal Service ────────────────────────────────
# Extracted from case-service at R5e step 8 — the customer portal, customer
# accounts and portal admin. Loopback-only; asks case-service for case work
# over its internal API (VELARIS_INTERNAL_SERVICE_SECRET).
echo "▶ Starting Portal Service (loopback 127.0.0.1:${VELARIS_PORTAL_PORT}, reachable via the gateway on 8200)..."
cd "$VELARIS_DIR"
# shellcheck disable=SC2046 - deliberate word splitting; the values are ours
_LOG_PORTAL_SERVICE="$(_svc_log portal-service)"
VELARIS_SERVICE_NAME=portal-service \
nohup uv run uvicorn portal_service.main:app $(_velaris_tls_args portal-service) $(_velaris_workers_args portal-service) \
  --host 127.0.0.1 --port "${VELARIS_PORTAL_PORT}" $(_reload_args services/portal-service services/_shared) \
  --proxy-headers \
  --app-dir services/portal-service \
  > "$_LOG_PORTAL_SERVICE" 2>&1 &
echo "  PID: $! | Logs: tail -f $_LOG_PORTAL_SERVICE"
for i in $(seq 1 20); do
  if _velaris_probe "${VELARIS_PORTAL_PORT}"; then
    green "  ✓ Portal Service ready on port ${VELARIS_PORTAL_PORT}"; break
  fi
  [ "$i" -eq 20 ] && yellow "  ⚠ Portal Service not ready after 20s"
  sleep 1
done
echo ""

# ── Step 7h: Start User Service ──────────────────────────────────
# Extracted from case-service at R5e step 10 — sign-in, MFA, SSO, SAML, SCIM
# and the user directory. Loopback-only; asks case-service only to send the
# password-reset mail (VELARIS_INTERNAL_SERVICE_SECRET). Every service still
# checks tokens itself, so nothing waits on it per request.
echo "▶ Starting User Service (loopback 127.0.0.1:${VELARIS_USER_PORT}, reachable via the gateway on 8200)..."
cd "$VELARIS_DIR"
# shellcheck disable=SC2046 - deliberate word splitting; the values are ours
_LOG_USER_SERVICE="$(_svc_log user-service)"
VELARIS_SERVICE_NAME=user-service \
nohup uv run uvicorn user_service.main:app $(_velaris_tls_args user-service) $(_velaris_workers_args user-service) \
  --host 127.0.0.1 --port "${VELARIS_USER_PORT}" $(_reload_args services/user-service services/_shared) \
  --proxy-headers \
  --app-dir services/user-service \
  > "$_LOG_USER_SERVICE" 2>&1 &
echo "  PID: $! | Logs: tail -f $_LOG_USER_SERVICE"
for i in $(seq 1 20); do
  if _velaris_probe "${VELARIS_USER_PORT}"; then
    green "  ✓ User Service ready on port ${VELARIS_USER_PORT}"; break
  fi
  [ "$i" -eq 20 ] && yellow "  ⚠ User Service not ready after 20s"
  sleep 1
done
echo ""

# ── Step 7g: Start Worker Service ────────────────────────────────
# case-service's code in worker mode (R5e step 9): the background jobs, each
# under a job lease — so while case-service also starts them
# (VELARIS_CASE_RUN_JOBS unset) one of the two runs each. No API; health only.
echo "▶ Starting Worker Service (loopback 127.0.0.1:${VELARIS_WORKER_PORT}, background jobs)..."
cd "$VELARIS_DIR"
# shellcheck disable=SC2046 - deliberate word splitting; the values are ours
_LOG_WORKER_SERVICE="$(_svc_log worker-service)"
VELARIS_SERVICE_NAME=worker-service \
nohup uv run uvicorn case_service.worker.main:app $(_velaris_tls_args worker-service) $(_velaris_workers_args worker-service) \
  --host 127.0.0.1 --port "${VELARIS_WORKER_PORT}" $(_reload_args services/case-service services/_shared) \
  --app-dir services/case-service \
  > "$_LOG_WORKER_SERVICE" 2>&1 &
echo "  PID: $! | Logs: tail -f $_LOG_WORKER_SERVICE"
for i in $(seq 1 30); do
  if _velaris_probe "${VELARIS_WORKER_PORT}"; then
    green "  ✓ Worker Service ready on port ${VELARIS_WORKER_PORT}"; break
  fi
  [ "$i" -eq 30 ] && yellow "  ⚠ Worker Service not ready after 30s"
  sleep 1
done
echo ""

# ── Step 8: Start Studio ─────────────────────────────────────────
echo "▶ Starting Velaris Studio (port 5173)..."
cd "$VELARIS_DIR/studio"

# Reinstall npm deps if package.json changed since the last install, OR if the
# installed tree doesn't actually satisfy package.json (a release can add
# packages, e.g. livekit-client in 2.1.0; a plain `git pull` + start would
# otherwise leave node_modules stale and Vite fails to resolve imports).
# node_modules lives at the workspace root in dev (npm workspaces) or inside
# studio/ in a standalone clone — check whichever marker exists.
_NM_MARK=""
for _m in "$VELARIS_DIR/node_modules/.package-lock.json" "$VELARIS_DIR/studio/node_modules/.package-lock.json"; do
  [ -f "$_m" ] && _NM_MARK="$_m" && break
done
_NEED_NPM=0
if [ -z "$_NM_MARK" ] \
   || [ "$VELARIS_DIR/studio/package.json" -nt "$_NM_MARK" ] \
   || { [ -f "$VELARIS_DIR/package.json" ] && [ "$VELARIS_DIR/package.json" -nt "$_NM_MARK" ]; }; then
  _NEED_NPM=1
elif ! npm ls --depth=0 >/dev/null 2>&1; then
  # mtimes look fine but the tree is broken/incomplete (failed earlier install)
  _NEED_NPM=1
fi
if [ "$_NEED_NPM" = 1 ]; then
  echo "  Studio dependencies missing/stale — running npm install..."
  _NPM_LOG="$VELARIS_SCRATCH/velaris-npm-install.log"
  if npm install --prefer-offline >"$_NPM_LOG" 2>&1 \
     || npm install >"$_NPM_LOG" 2>&1; then   # retry online if offline-first failed
    rm -rf "$VELARIS_DIR/studio/node_modules/.vite"
    green "  ✓ Studio dependencies installed"
  else
    tail -5 "$_NPM_LOG"
    yellow "  ⚠ npm install FAILED — Studio may not load. Full log: $_NPM_LOG"
  fi
else
  green "  ✓ Studio dependencies up to date"
fi

_LOG_STUDIO="$(_svc_log studio)"
if [ "$VELARIS_RUN_MODE" = development ]; then
  nohup npm run dev -- --host 0.0.0.0 --port "$VELARIS_STUDIO_PORT" --strictPort \
    > "$_LOG_STUDIO" 2>&1 &
else
  # Production serves the BUILT bundle (as the systemd unit does). The Vite dev
  # server is a development tool: bound to 0.0.0.0 it exposed its module and
  # file-serving endpoints to the network. Rebuild only when a source is newer.
  if [ ! -f dist/index.html ] \
     || [ -n "$(find src public index.html package.json vite.config.ts -newer dist/index.html -print -quit 2>/dev/null)" ]; then
    echo "  Building Studio (sources changed)..."
    if npm run build >"$VELARIS_LOG_DIR/velaris-studio-build.log" 2>&1; then
      green "  ✓ Studio built"
    else
      yellow "  ⚠ Studio build FAILED — see $VELARIS_LOG_DIR/velaris-studio-build.log"
    fi
  fi
  nohup npm run preview -- --host 0.0.0.0 --port "$VELARIS_STUDIO_PORT" --strictPort \
    > "$_LOG_STUDIO" 2>&1 &
fi
echo "  PID: $! | Logs: tail -f $_LOG_STUDIO"
sleep 3
echo ""
cd "$VELARIS_DIR"

# ── Heartbeat ping (background, non-blocking) ─────────────────────
if [ -f "$VELARIS_DIR/.velaris-key" ]; then
  KEY_ID=$(python3 -c "import json; d=json.load(open('$VELARIS_DIR/.velaris-key')); print(d.get('key_id',''))" 2>/dev/null || echo "")
  if [ -n "$KEY_ID" ]; then
    curl -s -X POST "${VELARIS_REGISTER_URL:-https://register.velaris.io}/heartbeat" \
      -H "Content-Type: application/json" \
      -d "{\"key_id\":\"$KEY_ID\",\"mac\":\"$(cat /sys/class/net/$(ip route show default | awk '/default/{print $5}')/address 2>/dev/null || echo unknown)\",\"version\":\"1.0.0\"}" \
      --max-time 5 > /dev/null 2>&1 &
  fi
fi

# ── Speech capability notice ─────────────────────────────────────
# Voice input ships enabled (releases/manifest.sql) but needs BOTH halves:
#   1. a local Whisper engine — the optional `intelligence` extra. Without it
#      /stt/status reports no engine and the mic renders nothing at all (an
#      absent capability is absent, never a dead button), so this is a notice
#      rather than a failure.
#   2. a SECURE CONTEXT in the browser. `navigator.mediaDevices` is undefined
#      on a plain-http origin, so the mic works at http://localhost but is
#      silently unavailable to anyone opening Studio or the customer portal
#      over http://<LAN-IP> — most obviously from a phone. Put a TLS
#      terminator in front for those users.
if ! uv run python -c "import faster_whisper" >/dev/null 2>&1; then
  yellow "  ⚠ Speech-to-text engine not installed — voice input will be hidden."
  echo   "    Enable it with:  uv sync --all-packages --extra intelligence"
  echo ""
fi

# ── Done ─────────────────────────────────────────────────────────
echo "╔═══════════════════════════════════════════════════════╗"
green "║              VELARIS is running!                      ║"
echo "╠═══════════════════════════════════════════════════════╣"
echo "║  Studio:        http://localhost:5173                 ║"
echo "║  Engine API:    http://localhost:8100                 ║"
echo "║  Case Service:  http://localhost:8200                 ║"
echo "║  Temporal UI:   http://localhost:8233                 ║"
[ "${VELARIS_BUNDLED_MINIO_ACTIVE:-false}" = true ] && \
echo "║  MinIO Console: http://localhost:9001                 ║"
echo "║  Mailpit UI:    http://localhost:8025                 ║"
echo "╠═══════════════════════════════════════════════════════╣"
echo "║  Logs:          $VELARIS_LOG_DIR/velaris-<service>.log"
echo "║  Run mode:      $VELARIS_RUN_MODE (default production; --dev = auto-reload)"
echo "╠═══════════════════════════════════════════════════════╣"
echo "║  Stop:          ./stop-velaris.sh                     ║"
echo "╠═══════════════════════════════════════════════════════╣"
echo "║  Microphone features (voice input, HxMeet captions)   ║"
echo "║  need https or localhost — browsers refuse mic access ║"
echo "║  over plain http on a LAN address.                    ║"
echo "╚═══════════════════════════════════════════════════════╝"
