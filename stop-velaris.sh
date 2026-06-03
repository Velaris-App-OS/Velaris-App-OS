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

HELIX_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$HELIX_DIR/deploy/docker-compose/docker-compose.yml"

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

pkill -f "uvicorn helix_engine"  2>/dev/null && green "  ✓ Engine stopped"       || echo "  Engine was not running"
pkill -f "uvicorn case_service"  2>/dev/null && green "  ✓ Case Service stopped"  || echo "  Case Service was not running"
pkill -f "vite"                  2>/dev/null && green "  ✓ Studio stopped"        || echo "  Studio was not running"

# Belt-and-suspenders: free ports in case pkill missed anything
for port_label in "8100:Engine" "8200:Case Service" "5173:Studio"; do
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
  docker compose -f "$COMPOSE_FILE" down
  green "  ✓ Docker services stopped"
fi

echo ""
green "Velaris stopped."
