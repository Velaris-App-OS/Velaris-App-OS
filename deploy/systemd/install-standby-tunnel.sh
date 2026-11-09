#!/usr/bin/env bash
# Install (or remove) the units that let THIS server be a Velaris standby.
#
#   sudo ./deploy/systemd/install-standby-tunnel.sh            # install + enable
#   sudo ./deploy/systemd/install-standby-tunnel.sh --remove   # disable + remove
#
# Run once on the server that is to become the standby (R5c, Nov_TODO step 11),
# before `standby-velaris.sh link <this server>` on the main. Installing does
# nothing by itself: the tunnel starts, and the sync timer copies documents,
# settings and vault, only while a link's .standby/tunnel.env exists.
# Neither setup-velaris.sh nor update-velaris.sh runs this.
#
# Copyright (c) 2024-2026 Velaris Contributors
# SPDX-License-Identifier: BSL-1.1
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run with sudo." >&2; exit 1; }

VELARIS_DIR="${VELARIS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
VELARIS_USER="${SUDO_USER:-$(stat -c '%U' "$VELARIS_DIR")}"
SRC_DIR="$VELARIS_DIR/deploy/systemd"
UNIT_DIR=/etc/systemd/system
UNITS=(velaris-standby-tunnel.service velaris-standby-tunnel.path
       velaris-standby-sync.service velaris-standby-sync.timer)

if [ "${1:-}" = "--remove" ]; then
  systemctl disable --now velaris-standby-tunnel.path velaris-standby-tunnel.service \
    velaris-standby-sync.timer velaris-standby-sync.service 2>/dev/null || true
  for u in "${UNITS[@]}"; do rm -f "$UNIT_DIR/$u"; done
  systemctl daemon-reload
  echo "✓ Standby tunnel units removed."
  exit 0
fi

[ "$VELARIS_USER" != root ] || { echo "✗ Refusing to run the tunnel as root — run sudo from the install owner's shell." >&2; exit 1; }
# The standby's databases run in containers the owner starts.
if ! id -nG "$VELARIS_USER" | tr ' ' '\n' | grep -qx docker; then
  echo "✗ $VELARIS_USER is not in the docker group — the standby's databases could not be started." >&2
  exit 1
fi
command -v ssh >/dev/null || { echo "✗ ssh (OpenSSH client) is not installed." >&2; exit 1; }
command -v rsync >/dev/null || { echo "✗ rsync is not installed (documents are copied with it)." >&2; exit 1; }

for unit in "${UNITS[@]}"; do
  sed -e "s|__VELARIS_USER__|$VELARIS_USER|g" \
      -e "s|__VELARIS_DIR__|$VELARIS_DIR|g" \
      "$SRC_DIR/$unit" > "$UNIT_DIR/$unit"
  echo "  installed $UNIT_DIR/$unit"
done
systemctl daemon-reload
systemctl enable velaris-standby-tunnel.service >/dev/null
systemctl enable --now velaris-standby-tunnel.path
# Runs every minute, does nothing until linked (ExecCondition).
systemctl enable --now velaris-standby-sync.timer
echo
echo "✓ This server can now be linked as a standby. On the MAIN server run:"
echo "    ./standby-velaris.sh link $VELARIS_USER@$(hostname -f 2>/dev/null || hostname)"
