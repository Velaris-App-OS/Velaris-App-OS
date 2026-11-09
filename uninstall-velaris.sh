#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  VELARIS Uninstall Script
#
#  Removes all Velaris services, data, credentials, and generated
#  artefacts from this machine.
#
#  Usage:
#    ./uninstall-velaris.sh              Interactive (asks at each step)
#    ./uninstall-velaris.sh --yes        Skip confirmations (CI / scripted)
#    ./uninstall-velaris.sh --purge      Also remove Docker images and uv
#    ./uninstall-velaris.sh --delete-dir Also delete the project directory
#
#  What this script removes:
#    ✓ Running Velaris processes (engine, every service, studio)
#    ✓ This install's systemd units (stopped, disabled, removed)
#    ✓ Docker containers  (all services in docker-compose.yml)
#    ✓ Docker volumes     (ALL data: database, cache, search, storage)
#    ✓ Python .venv/      (virtualenv)
#    ✓ node_modules/      (studio frontend deps)
#    ✓ /var/lib/velaris/  (local document storage)
#    ✓ .env               (generated credentials and secrets)
#    ✓ .velaris-key       (product licence key)
#    ✓ .velaris-setup-complete  (setup marker)
#    ✓ /tmp/velaris-*.log and logs/ (service log files)
#
#  With --purge, also removes:
#    ✓ Docker images used by Velaris
#    ✓ uv (if installed by setup-velaris.sh)
#
#  With --delete-dir, also removes:
#    ✓ The entire project directory (IRREVERSIBLE)
#
#  What this script does NOT touch:
#    ✗ Docker Engine itself
#    ✗ Node.js / npm
#    ✗ System apt packages (curl, git, postgresql-client, etc.)
#    ✗ Ollama models (large — remove manually if desired: ~/.ollama)
# ═══════════════════════════════════════════════════════════════════

set -uo pipefail

VELARIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$VELARIS_DIR/deploy/docker-compose/docker-compose.yml"
# An uninstall takes the bundled MinIO with it whether or not it was detected.
export COMPOSE_PROFILES="${COMPOSE_PROFILES:+$COMPOSE_PROFILES,}minio"

# ── Colour helpers ────────────────────────────────────────────────
red()    { echo -e "\033[0;31m$*\033[0m"; }
green()  { echo -e "\033[0;32m$*\033[0m"; }
yellow() { echo -e "\033[0;33m$*\033[0m"; }
bold()   { echo -e "\033[1m$*\033[0m"; }
dim()    { echo -e "\033[2m$*\033[0m"; }
step()   { echo ""; bold "▶ $*"; }

# ── Argument parsing ──────────────────────────────────────────────
AUTO_YES=false
PURGE=false
DELETE_DIR=false

for arg in "$@"; do
  case "$arg" in
    --yes)        AUTO_YES=true ;;
    --purge)      PURGE=true ;;
    --delete-dir) DELETE_DIR=true ;;
    --help|-h)
      echo "Usage: $0 [--yes] [--purge] [--delete-dir]"
      echo ""
      echo "  --yes         Skip all confirmation prompts"
      echo "  --purge       Also remove Docker images and uv"
      echo "  --delete-dir  Also delete the project directory"
      exit 0
      ;;
    *)
      red "Unknown option: $arg"
      echo "Run '$0 --help' for usage."
      exit 1
      ;;
  esac
done

# ── Confirm helper ────────────────────────────────────────────────
confirm() {
  local msg="$1"
  if $AUTO_YES; then return 0; fi
  read -rp "  ${msg} [y/N] " _ans
  [[ "$_ans" =~ ^[Yy]$ ]]
}

# ─────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║              VELARIS Uninstall                           ║"
echo "╠══════════════════════════════════════════════════════════╣"
echo "║  This will permanently delete all Velaris data           ║"
echo "║  including the database, uploaded files, and secrets.    ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
yellow "  Project directory : $VELARIS_DIR"
$PURGE      && yellow "  Mode              : PURGE (images + uv will also be removed)"
$DELETE_DIR && yellow "  Mode              : DELETE DIR (project folder will be removed)"
echo ""

if ! $AUTO_YES; then
  red "  ⚠  This cannot be undone. All data will be permanently lost."
  echo ""
  if ! confirm "Are you sure you want to uninstall Velaris?"; then
    echo "  Aborted — nothing was changed."
    exit 0
  fi
fi

echo ""

# ══════════════════════════════════════════════════════════════════
# STEP 1 — Stop running services
# ══════════════════════════════════════════════════════════════════
step "Step 1 — Stopping running Velaris processes..."

_stopped_any=false

# systemd units first: they run with Restart=always, so a process killed below
# would be started again in 5 s — against a database this script is about to
# delete — and keep failing at every boot. Only units that point at THIS
# install's folder are touched.
_unit_dir="${VELARIS_UNIT_DIR:-/etc/systemd/system}"
_our_units=()
for _u in "$_unit_dir"/velaris-*.service; do
  [ -f "$_u" ] && grep -qF "$VELARIS_DIR" "$_u" && _our_units+=("$(basename "$_u")")
done
# A timer or path unit belongs here when it names this folder, or when the
# service it starts does (velaris-update.timer names neither path nor folder).
for _u in "$_unit_dir"/velaris-*.timer "$_unit_dir"/velaris-*.path; do
  [ -f "$_u" ] || continue
  _svc="$(basename "${_u%.*}").service"
  if grep -qF "$VELARIS_DIR" "$_u" || [[ " ${_our_units[*]} " == *" $_svc "* ]]; then
    _our_units+=("$(basename "$_u")")
  fi
done
if [ "${#_our_units[@]}" -gt 0 ]; then
  if sudo systemctl disable --now "${_our_units[@]}" 2>/dev/null; then
    for _u in "${_our_units[@]}"; do sudo rm -f "$_unit_dir/$_u"; done
    sudo systemctl daemon-reload 2>/dev/null || true
    green "  ✓ Stopped and removed systemd units: ${_our_units[*]}"
    _stopped_any=true
  else
    yellow "  ⚠ Could not stop the systemd units (sudo refused?): ${_our_units[*]}"
    yellow "    Run: sudo systemctl disable --now ${_our_units[*]}"
  fi
fi

for pattern in "uvicorn helix_engine" "uvicorn case_service" "uvicorn analytics_service" \
               "uvicorn hxgraph_service" "uvicorn hxmigrate_service" "uvicorn commerce_service" \
               "uvicorn portal_service" "uvicorn user_service" "vite"; do
  if pkill -f "$pattern" 2>/dev/null; then
    green "  ✓ Stopped: $pattern"
    _stopped_any=true
  fi
done

# Belt-and-suspenders: free the known ports
for port in 8100 8200 5173; do
  if fuser "${port}/tcp" &>/dev/null 2>&1; then
    fuser -k "${port}/tcp" 2>/dev/null || true
    green "  ✓ Freed port $port"
    _stopped_any=true
  fi
done

$_stopped_any || dim "  (no Velaris processes were running)"

# ══════════════════════════════════════════════════════════════════
# STEP 2 — Docker containers and volumes
# ══════════════════════════════════════════════════════════════════
step "Step 2 — Removing Docker containers and volumes..."

echo ""
yellow "  ⚠  This deletes ALL Velaris data:"
yellow "     • Main database     (velaris-db)"
yellow "     • Temporal database (temporal-db)"
yellow "     • Valkey cache      (cache-data)"
yellow "     • Kafka events      (kafka-data)"
yellow "     • OpenBao secrets   (openbao-data)"
yellow "     • Ollama models     (ollama-data)  ← may be several GB"
yellow "     • MinIO documents   (velaris-minio-data)"
yellow "     • Prometheus data   (velaris-prometheus-data)"
yellow "     • Mailpit emails    (velaris-mailpit-data)"
echo ""

# R5c standby link (standby-velaris.sh). On a MAIN, unlink before anything is
# deleted: otherwise the standby's tunnel key stays trusted in this user's
# ~/.ssh/authorized_keys after the install is gone, and the standby keeps
# replicas of a server that no longer exists. On a STANDBY, the replicas are
# plain containers compose does not own; their volumes are compose's names and
# go with the volume removal below.
if [ -f "$VELARIS_DIR/.standby/main.json" ]; then
  if "$VELARIS_DIR/standby-velaris.sh" unlink; then
    green "  ✓ Standby unlinked (its tunnel key revoked)"
  else
    yellow "  ⚠ Standby not fully unlinked (above). The tunnel key was revoked; check"
    yellow "    ~/.ssh/authorized_keys for a 'velaris-standby-tunnel' line if in doubt."
  fi
fi
for _c in velaris-standby-db velaris-standby-temporal-db; do
  if docker ps -aq --filter "name=^${_c}$" 2>/dev/null | grep -q .; then
    docker rm -f "$_c" >/dev/null 2>&1 && green "  ✓ Removed standby replica $_c"
  fi
done
# On a standby the main's reads key (serve-reads, R5d 11b) may still be trusted
# in this user's authorized_keys — it goes with the install.
if [ -x "$VELARIS_DIR/standby-velaris.sh" ] \
    && grep -q " velaris-standby-reads " "$HOME/.ssh/authorized_keys" 2>/dev/null; then
  "$VELARIS_DIR/standby-velaris.sh" remote-reads-revoke </dev/null >/dev/null 2>&1 \
    && green "  ✓ Revoked the main's reads key" \
    || yellow "  ⚠ Remove the 'velaris-standby-reads' line from ~/.ssh/authorized_keys by hand"
fi
rm -f "$VELARIS_DIR/.standby/tunnel.env" "$VELARIS_DIR/.standby/standby.json" 2>/dev/null || true

if confirm "Delete all Docker containers and volumes?"; then
  if [ -f "$COMPOSE_FILE" ]; then
    # Load .env so compose can resolve variable references.
    # --env-file as well as sourcing: sourcing breaks on values with spaces or
    # quoting, and compose refuses to run at all when a `${VAR:?...}` secret is
    # unresolved — even for `down`. Conditional because uninstall must still work
    # on a half-finished install where .env was never rendered; compose errors
    # out on an --env-file that does not exist, and the by-name volume removal
    # below is the fallback for that case.
    ENV_ARGS=()
    if [ -f "$VELARIS_DIR/.env" ]; then
      set -o allexport; source "$VELARIS_DIR/.env" 2>/dev/null || true; set +o allexport
      ENV_ARGS=(--env-file "$VELARIS_DIR/.env")
    fi
    # --profile '*': include services behind a profile (the Kafka broker, the
    # bundled MinIO, observability) — `down` ignores inactive profiles otherwise.
    docker compose -f "$COMPOSE_FILE" "${ENV_ARGS[@]}" --profile '*' down --volumes --remove-orphans 2>/dev/null \
      && green "  ✓ Docker containers and volumes removed" \
      || yellow "  ⚠ docker compose down had warnings (containers may already be stopped)"
  else
    yellow "  ⚠ docker-compose.yml not found — removing volumes by name directly"
  fi

  # Explicitly remove named volumes in case compose missed any
  VOLUMES=(
    temporal-db
    velaris-db
    cache-data
    kafka-data
    events-data      # left by Redpanda, the broker before Kafka (pre-November 2026)
    ollama-data
    velaris-minio-data
    velaris-prometheus-data
    velaris-mailpit-data
    openbao-data
  )
  for vol in "${VOLUMES[@]}"; do
    # docker-compose prefixes volumes with the compose project name
    for prefixed in "${vol}" "docker-compose_${vol}" "docker-compose-${vol}"; do
      if docker volume inspect "$prefixed" &>/dev/null 2>&1; then
        docker volume rm "$prefixed" 2>/dev/null \
          && green "  ✓ Volume removed: $prefixed" || true
      fi
    done
  done
else
  yellow "  Skipped — Docker volumes kept."
fi

# ══════════════════════════════════════════════════════════════════
# STEP 3 — Docker images (--purge only)
# ══════════════════════════════════════════════════════════════════
if $PURGE; then
  step "Step 3 — Removing Docker images (--purge)..."

  IMAGES=(
    "postgres:16-alpine"
    "temporalio/auto-setup:1.25"
    "temporalio/ui:2.31.2"
    "valkey/valkey:8-alpine"
    "ollama/ollama:latest"
    "nginx:1.27-alpine"
    "prom/prometheus:v2.54.1"
    "otel/opentelemetry-collector-contrib:0.105.0"
    "minio/minio:RELEASE.2024-10-02T17-50-41Z"
    "minio/mc:RELEASE.2024-10-02T08-27-28Z"
    "axllent/mailpit:latest"
    "openbao/openbao:2"
    # Pinned exact versions (2026-10-16). The floating tags above stay listed:
    # an install made before the pin still holds those images.
    "postgres:16.15-alpine"
    "temporalio/auto-setup:1.25.2"
    "valkey/valkey:8.1.10-alpine"
    "apache/kafka:4.3.1"
    "ollama/ollama:0.40.5"
    "livekit/livekit-server:v1.13.10"
    "livekit/egress:v1.13.0"
    "nginx:1.27.5-alpine"
    "axllent/mailpit:v1.31.6"
    "openbao/openbao:2.7.1"
    "openbao/openbao:2.5.4"      # used once by openbao-storage-migrate.sh
    "python:3.12.15-alpine"
  )

  echo ""
  yellow "  These Docker images will be removed:"
  for img in "${IMAGES[@]}"; do
    dim "    • $img"
  done
  echo ""

  if confirm "Remove all Velaris Docker images?"; then
    for img in "${IMAGES[@]}"; do
      if docker image inspect "$img" &>/dev/null 2>&1; then
        docker rmi "$img" 2>/dev/null \
          && green "  ✓ Removed: $img" \
          || yellow "  ⚠ Could not remove $img (may be used by another container)"
      fi
    done
  else
    yellow "  Skipped — Docker images kept."
  fi

  # ── Build cache ────────────────────────────────────────────────
  # Always safe: build cache holds no containers, images or volumes.
  docker builder prune -af >/dev/null 2>&1 \
    && green "  ✓ Docker build cache cleared" || true

  # ── Docker daemon config Velaris installed (Step 2b of setup) ──
  # Only offered, never assumed: by uninstall time the operator may be
  # running other workloads that now depend on this data-root.
  if [ -f /etc/docker/daemon.json ] && grep -q '"builder"' /etc/docker/daemon.json 2>/dev/null; then
    VDS_ROOT="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)"
    echo ""
    yellow "  Velaris configured Docker's storage during setup:"
    dim   "    data-root  : $VDS_ROOT"
    dim   "    daemon.json: /etc/docker/daemon.json (build-cache cap)"
    if [ "$VDS_ROOT" != "/var/lib/docker" ]; then
      yellow "  Reverting moves Docker back to /var/lib/docker — ANY other"
      yellow "  containers on this host would lose their images and volumes."
    fi
    if confirm "Revert Docker daemon config to defaults?"; then
      LATEST_BAK="$(ls -1t /etc/docker/daemon.json.bak.* 2>/dev/null | head -1)"
      if [ -n "$LATEST_BAK" ]; then
        sudo cp -a "$LATEST_BAK" /etc/docker/daemon.json \
          && green "  ✓ Restored pre-Velaris daemon.json ($LATEST_BAK)"
      else
        sudo rm -f /etc/docker/daemon.json \
          && green "  ✓ Removed /etc/docker/daemon.json (Velaris created it)"
      fi
      sudo systemctl restart docker 2>/dev/null \
        && green "  ✓ Docker restarted on default storage" \
        || yellow "  ⚠ Restart Docker by hand to apply"
      # The moved tree is left on disk deliberately — deleting a data-root
      # that may still hold non-Velaris volumes is not this script's call.
      if [ "$VDS_ROOT" != "/var/lib/docker" ] && [ -d "$VDS_ROOT" ]; then
        yellow "  Old Docker data left at: $VDS_ROOT"
        dim   "    Remove it once you are sure nothing else needs it:"
        dim   "      sudo rm -rf $VDS_ROOT"
      fi
    else
      yellow "  Skipped — Docker daemon config kept."
    fi
  fi
else
  step "Step 3 — Docker images"
  dim "  Skipped (use --purge to also remove Docker images, build cache and daemon config)"
fi

# ══════════════════════════════════════════════════════════════════
# STEP 4 — Python virtualenv
# ══════════════════════════════════════════════════════════════════
step "Step 4 — Removing Python virtualenv..."

VENV_DIR="$VELARIS_DIR/.venv"
if [ -d "$VENV_DIR" ]; then
  VENV_SIZE=$(du -sh "$VENV_DIR" 2>/dev/null | cut -f1)
  if confirm "Remove .venv/ (${VENV_SIZE})?"; then
    rm -rf "$VENV_DIR"
    green "  ✓ .venv/ removed"
  else
    yellow "  Skipped — .venv/ kept"
  fi
else
  dim "  .venv/ not found — skipping"
fi

# ══════════════════════════════════════════════════════════════════
# STEP 5 — Studio node_modules
# ══════════════════════════════════════════════════════════════════
step "Step 5 — Removing Studio node_modules..."

NODE_DIR="$VELARIS_DIR/studio/node_modules"
if [ -d "$NODE_DIR" ]; then
  NODE_SIZE=$(du -sh "$NODE_DIR" 2>/dev/null | cut -f1)
  if confirm "Remove studio/node_modules/ (${NODE_SIZE})?"; then
    rm -rf "$NODE_DIR"
    green "  ✓ studio/node_modules/ removed"
  else
    yellow "  Skipped — node_modules kept"
  fi
else
  dim "  studio/node_modules/ not found — skipping"
fi

# ══════════════════════════════════════════════════════════════════
# STEP 6 — Local document storage
# ══════════════════════════════════════════════════════════════════
step "Step 6 — Removing local document storage..."

# Read path from .env if available, fall back to default
STORAGE_PATH=$(grep "^VELARIS_CASE_STORAGE_LOCAL_PATH=" "$VELARIS_DIR/.env" 2>/dev/null \
  | cut -d= -f2 | tr -d '"' | tr -d "'") || true
STORAGE_PATH="${STORAGE_PATH:-/var/lib/velaris}"

# Walk up to the velaris root (e.g. /var/lib/velaris/documents → /var/lib/velaris)
STORAGE_ROOT=$(echo "$STORAGE_PATH" | sed 's|/documents$||')

if [ -d "$STORAGE_ROOT" ]; then
  STORAGE_SIZE=$(du -sh "$STORAGE_ROOT" 2>/dev/null | cut -f1)
  echo ""
  yellow "  ⚠  $STORAGE_ROOT (${STORAGE_SIZE}) contains all uploaded documents."
  if confirm "Delete local document storage at $STORAGE_ROOT?"; then
    sudo rm -rf "$STORAGE_ROOT"
    green "  ✓ $STORAGE_ROOT removed"
  else
    yellow "  Skipped — document storage kept at $STORAGE_ROOT"
  fi
else
  dim "  $STORAGE_ROOT not found — skipping"
fi

# ══════════════════════════════════════════════════════════════════
# STEP 7 — Credential and marker files
# ══════════════════════════════════════════════════════════════════
step "Step 7 — Removing credential and marker files..."

echo ""
yellow "  ⚠  The following files contain secrets (passwords, JWT keys, licence key):"
CREDENTIAL_FILES=(
  "$VELARIS_DIR/.env"
  "$VELARIS_DIR/.env.pre-openbao"
  "$VELARIS_DIR/.velaris-key"
  "$VELARIS_DIR/.velaris-setup-complete"
  # OpenBao (Group K) generated secrets — NOT the committed server.hcl/agent.hcl
  "$VELARIS_DIR/deploy/openbao/.bao-init.json"
  "$VELARIS_DIR/deploy/openbao/agent/role_id"
  "$VELARIS_DIR/deploy/openbao/agent/secret_id"
  "$VELARIS_DIR/deploy/openbao/enabled"
)
for f in "${CREDENTIAL_FILES[@]}"; do
  [ -f "$f" ] && dim "    • $f"
done
echo ""

if confirm "Delete credential and marker files?"; then
  for f in "${CREDENTIAL_FILES[@]}"; do
    if [ -f "$f" ]; then
      # Overwrite with zeros before deleting (basic secret hygiene)
      shred -uz "$f" 2>/dev/null || rm -f "$f"
      green "  ✓ Removed: $(basename "$f")"
    fi
  done
  # OpenBao agent renders short-lived secrets into agent/out/ — remove the whole dir
  if [ -d "$VELARIS_DIR/deploy/openbao/agent/out" ]; then
    rm -rf "$VELARIS_DIR/deploy/openbao/agent/out"
    green "  ✓ Removed: deploy/openbao/agent/out/"
  fi
else
  yellow "  Skipped — credential files kept"
fi

# ══════════════════════════════════════════════════════════════════
# STEP 8 — Temporary log files
# ══════════════════════════════════════════════════════════════════
step "Step 8 — Removing temporary log files..."

LOG_FILES=(
  /tmp/velaris-engine.log
  /tmp/velaris-case-service.log
  /tmp/velaris-analytics-service.log
  /tmp/velaris-hxgraph-service.log
  /tmp/velaris-hxmigrate-service.log
  /tmp/velaris-commerce-service.log
  /tmp/velaris-portal-service.log
  /tmp/velaris-user-service.log
  /tmp/velaris-worker-service.log
  /tmp/velaris-studio.log
  "$VELARIS_DIR"/logs/velaris-*.log*
)

_removed_logs=false
for f in "${LOG_FILES[@]}"; do
  if [ -f "$f" ]; then
    rm -f "$f"
    green "  ✓ Removed: $f"
    _removed_logs=true
  fi
done
$_removed_logs || dim "  No log files found — skipping"

# ══════════════════════════════════════════════════════════════════
# STEP 9 — uv (--purge only)
# ══════════════════════════════════════════════════════════════════
if $PURGE; then
  step "Step 9 — Removing uv (--purge)..."

  UV_PATH=$(command -v uv 2>/dev/null || echo "")
  if [ -n "$UV_PATH" ]; then
    echo ""
    yellow "  ⚠  uv is installed at $UV_PATH"
    yellow "     Only remove it if Velaris is the only project that uses uv."
    echo ""
    if confirm "Remove uv from this system?"; then
      # uv installs itself to ~/.local/bin
      rm -f "$HOME/.local/bin/uv" "$HOME/.local/bin/uvx"
      # Remove uv's own cache and data
      rm -rf "$HOME/.local/share/uv" "$HOME/.cache/uv"
      # Remove uv-managed Python installs
      rm -rf "$HOME/.local/share/uv/python"
      green "  ✓ uv removed"
      echo ""
      yellow "  Note: Remove 'export PATH=\"\$HOME/.local/bin:\$PATH\"' from ~/.bashrc manually if desired."
    else
      yellow "  Skipped — uv kept"
    fi
  else
    dim "  uv not found — skipping"
  fi
else
  step "Step 9 — uv"
  dim "  Skipped (use --purge to also remove uv)"
fi

# ══════════════════════════════════════════════════════════════════
# STEP 10 — Project directory (--delete-dir only)
# ══════════════════════════════════════════════════════════════════
if $DELETE_DIR; then
  step "Step 10 — Deleting project directory (--delete-dir)..."
  echo ""
  red "  ⚠  THIS WILL DELETE THE ENTIRE PROJECT DIRECTORY:"
  red "     $VELARIS_DIR"
  red "     All source code, migrations, scripts, and config will be lost."
  echo ""

  if confirm "Type 'delete' to confirm permanent deletion of the project directory"; then
    # Can't delete our own parent while running from it — copy script to /tmp first
    SELF="$(basename "${BASH_SOURCE[0]}")"
    cd /tmp
    rm -rf "$VELARIS_DIR"
    green "  ✓ Project directory deleted: $VELARIS_DIR"
  else
    yellow "  Skipped — project directory kept"
  fi
else
  step "Step 10 — Project directory"
  dim "  Skipped (use --delete-dir to also remove the project folder)"
fi

# ══════════════════════════════════════════════════════════════════
# Done
# ══════════════════════════════════════════════════════════════════
echo ""
echo "╔══════════════════════════════════════════════════════════╗"
green "║  Velaris uninstall complete.                             ║"
echo "╠══════════════════════════════════════════════════════════╣"
echo "║  The following are NOT removed by this script:           ║"
dim  "║  • Docker Engine                                         ║"
dim  "║  • Node.js / npm                                         ║"
dim  "║  • System apt packages (curl, git, postgresql-client…)   ║"
dim  "║  • Ollama model weights (~/.ollama) — remove manually    ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""
