#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  VELARIS Stop Script
#
#  Usage:
#    ./stop-velaris.sh          Stop Python services + Vite only
#    ./stop-velaris.sh --all    Also bring down Docker (DB, Temporal, etc.)
#
#  Requires system (sudo) password. This prevents accidental or
#  unauthorised shutdowns of a running production environment.
# ═══════════════════════════════════════════════════════════════════

VELARIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$VELARIS_DIR/deploy/docker-compose/docker-compose.yml"
# Which optional compose services this install runs (the bundled MinIO is kept
# only where it already exists) — see the file for the rule.
# shellcheck source=/dev/null
[ -f "$VELARIS_DIR/deploy/docker-compose/compose-profiles.sh" ] && . "$VELARIS_DIR/deploy/docker-compose/compose-profiles.sh"

red()   { echo -e "\033[0;31m$*\033[0m"; }
green() { echo -e "\033[0;32m$*\033[0m"; }
bold()  { echo -e "\033[1m$*\033[0m"; }

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║         VELARIS Stop — Auth Gate         ║"
echo "╚══════════════════════════════════════════╝"
echo ""
echo "  Stopping Velaris requires your system password."
echo "  This prevents accidental or unauthorised shutdowns."
echo ""

# Flush any cached sudo ticket so we always force a real password check
sudo -k

read -rs -p "  System password: " _STOP_PASS
echo ""

if ! echo "$_STOP_PASS" | sudo -S -v 2>/dev/null; then
    echo ""
    red "  ✗ Authentication failed. Aborting."
    echo ""
    exit 1
fi
unset _STOP_PASS

echo ""
green "  ✓ Authenticated."
echo ""

# ── Stop services ────────────────────────────────────────────────
echo "▶ Stopping Velaris services..."

# Under systemd a killed process is restarted seconds later (Restart=always):
# the kills below "stopped" nothing, and a restore run next was written into by
# the restarted services (clean box 2026-10-23). Stop the units themselves —
# the operator has just authenticated for exactly this. The kills still run
# after, for a copy started from a shell.
_units=""
for _u in velaris-engine velaris-case-service velaris-analytics-service velaris-hxgraph-service \
          velaris-hxmigrate-service velaris-commerce-service velaris-portal-service \
          velaris-user-service velaris-worker-service velaris-studio; do
  systemctl is-active --quiet "$_u" 2>/dev/null && _units="$_units $_u"
done
if [ -n "$_units" ]; then
  # shellcheck disable=SC2086
  if sudo systemctl stop $_units; then
    green "  ✓ systemd units stopped:$_units"
    echo  "    (they start again at boot; start now with: sudo systemctl start$_units)"
  else
    red "  ✗ could not stop the systemd units:$_units"
    exit 1
  fi
fi

pkill -f "uvicorn helix_engine"  2>/dev/null && green "  ✓ Engine stopped"       || echo "  Engine was not running"
pkill -f "uvicorn case_service"  2>/dev/null && green "  ✓ Case Service stopped"  || echo "  Case Service was not running"
pkill -f "uvicorn analytics_service" 2>/dev/null && green "  ✓ Analytics Service stopped" || echo "  Analytics Service was not running"
pkill -f "uvicorn hxgraph_service"   2>/dev/null && green "  ✓ HxGraph Service stopped"   || echo "  HxGraph Service was not running"
pkill -f "uvicorn hxmigrate_service" 2>/dev/null && green "  ✓ HxMigrate Service stopped" || echo "  HxMigrate Service was not running"
pkill -f "uvicorn commerce_service" 2>/dev/null && green "  ✓ Commerce Service stopped" || echo "  Commerce Service was not running"
pkill -f "uvicorn portal_service" 2>/dev/null && green "  ✓ Portal Service stopped" || echo "  Portal Service was not running"
pkill -f "uvicorn user_service" 2>/dev/null && green "  ✓ User Service stopped" || echo "  User Service was not running"
pkill -f "uvicorn case_service.worker.main" 2>/dev/null && green "  ✓ Worker Service stopped" || echo "  Worker Service was not running"
pkill -f "vite"                  2>/dev/null && green "  ✓ Studio stopped"        || echo "  Studio was not running"

# Belt-and-suspenders: free ports in case pkill missed anything.
#
# 8200 is deliberately NOT in this list. It is the nginx GATEWAY, which runs as
# a container with `network_mode: host` — its listener is in the host namespace,
# so `fuser -k 8200/tcp` reaches into a Docker-managed process and stops the
# gateway without compose knowing. The gateway is stopped by `--all` below,
# through compose, which is the only thing that can bring it back up cleanly.
# (It was listed here as "Case Service", which is also the wrong name: case
# service is 8201, and that was missing from the list entirely.)
for port_label in "8100:Engine" "8201:Case Service" "8202:Analytics Service" "8203:HxGraph Service" "8204:HxMigrate Service" "8207:Commerce Service" "8205:Portal Service" "8206:User Service" "8208:Worker Service" "5173:Studio"; do
  port="${port_label%%:*}"
  label="${port_label##*:}"
  if fuser "${port}/tcp" &>/dev/null 2>&1; then
    fuser -k "${port}/tcp" 2>/dev/null
    green "  ✓ Port $port ($label) freed"
  fi
done

if [ "${1:-}" = "--all" ]; then
  echo ""
  echo "▶ Stopping Docker infrastructure..."
  # --env-file is REQUIRED, not optional tidiness: compose interpolates the whole
  # file for every subcommand including `down`, and two services declare secrets
  # with `${VAR:?...}`. Without it this aborts with "required variable
  # VELARIS_MINIO_PASSWORD is missing" on any host that lacks a stray .env beside
  # the compose file — which is every real server.
  #
  # --remove-orphans: a plain `down` skips containers whose service is no longer
  # in the compose file, so a renamed service leaves its old container up and
  # still holding its published port. A stop must actually leave nothing behind.
  docker compose -f "$COMPOSE_FILE" --env-file "$VELARIS_DIR/.env" down --remove-orphans
  green "  ✓ Docker services stopped"
fi

echo ""
green "Velaris stopped."
