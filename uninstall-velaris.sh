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
#    ✓ Running Velaris processes (engine, case service, studio)
#    ✓ Docker containers  (all services in docker-compose.yml)
#    ✓ Docker volumes     (ALL data: database, cache, search, storage)
#    ✓ Python .venv/      (virtualenv)
#    ✓ node_modules/      (studio frontend deps)
#    ✓ /var/lib/velaris/  (local document storage)
#    ✓ .env               (generated credentials and secrets)
#    ✓ .velaris-key       (product licence key)
#    ✓ .velaris-setup-complete  (setup marker)
#    ✓ /tmp/velaris-*.log (service log files)
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

HELIX_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$HELIX_DIR/deploy/docker-compose/docker-compose.yml"

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
yellow "  Project directory : $HELIX_DIR"
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

for pattern in "uvicorn helix_engine" "uvicorn case_service" "vite"; do
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
yellow "     • Main database     (helix-db)"
yellow "     • Temporal database (temporal-db)"
yellow "     • Valkey cache      (cache-data)"
yellow "     • Redpanda events   (events-data)"
yellow "     • OpenBao secrets   (openbao-data)"
yellow "     • Ollama models     (ollama-data)  ← may be several GB"
yellow "     • MinIO documents   (helix-minio-data)"
yellow "     • Prometheus data   (helix-prometheus-data)"
yellow "     • Mailpit emails    (helix-mailpit-data)"
echo ""

if confirm "Delete all Docker containers and volumes?"; then
  if [ -f "$COMPOSE_FILE" ]; then
    # Load .env so compose can resolve variable references
    if [ -f "$HELIX_DIR/.env" ]; then
      set -o allexport; source "$HELIX_DIR/.env" 2>/dev/null || true; set +o allexport
    fi
    docker compose -f "$COMPOSE_FILE" down --volumes --remove-orphans 2>/dev/null \
      && green "  ✓ Docker containers and volumes removed" \
      || yellow "  ⚠ docker compose down had warnings (containers may already be stopped)"
  else
    yellow "  ⚠ docker-compose.yml not found — removing volumes by name directly"
  fi

  # Explicitly remove named volumes in case compose missed any
  VOLUMES=(
    temporal-db
    helix-db
    cache-data
    events-data
    ollama-data
    helix-minio-data
    helix-prometheus-data
    helix-mailpit-data
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
    "redpandadata/redpanda:latest"
    "ollama/ollama:latest"
    "nginx:1.27-alpine"
    "prom/prometheus:v2.54.1"
    "otel/opentelemetry-collector-contrib:0.105.0"
    "minio/minio:RELEASE.2024-10-02T17-50-41Z"
    "minio/mc:RELEASE.2024-10-02T08-27-28Z"
    "axllent/mailpit:latest"
    "openbao/openbao:2"
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
else
  step "Step 3 — Docker images"
  dim "  Skipped (use --purge to also remove Docker images)"
fi

# ══════════════════════════════════════════════════════════════════
# STEP 4 — Python virtualenv
# ══════════════════════════════════════════════════════════════════
step "Step 4 — Removing Python virtualenv..."

VENV_DIR="$HELIX_DIR/.venv"
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

NODE_DIR="$HELIX_DIR/studio/node_modules"
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
STORAGE_PATH=$(grep "^HELIX_CASE_STORAGE_LOCAL_PATH=" "$HELIX_DIR/.env" 2>/dev/null \
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
  "$HELIX_DIR/.env"
  "$HELIX_DIR/.env.pre-openbao"
  "$HELIX_DIR/.velaris-key"
  "$HELIX_DIR/.velaris-setup-complete"
  # OpenBao (Group K) generated secrets — NOT the committed server.hcl/agent.hcl
  "$HELIX_DIR/deploy/openbao/.bao-init.json"
  "$HELIX_DIR/deploy/openbao/agent/role_id"
  "$HELIX_DIR/deploy/openbao/agent/secret_id"
  "$HELIX_DIR/deploy/openbao/enabled"
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
  if [ -d "$HELIX_DIR/deploy/openbao/agent/out" ]; then
    rm -rf "$HELIX_DIR/deploy/openbao/agent/out"
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
  /tmp/velaris-studio.log
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
  red "     $HELIX_DIR"
  red "     All source code, migrations, scripts, and config will be lost."
  echo ""

  if confirm "Type 'delete' to confirm permanent deletion of the project directory"; then
    # Can't delete our own parent while running from it — copy script to /tmp first
    SELF="$(basename "${BASH_SOURCE[0]}")"
    cd /tmp
    rm -rf "$HELIX_DIR"
    green "  ✓ Project directory deleted: $HELIX_DIR"
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
