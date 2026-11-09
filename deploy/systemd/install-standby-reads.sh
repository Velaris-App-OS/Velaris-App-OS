#!/usr/bin/env bash
# Install (or remove) the unit that lets THIS main server read from its
# standby's copy of the database (R5d 11b part c).
#
#   sudo ./deploy/systemd/install-standby-reads.sh            # install + enable
#   sudo ./deploy/systemd/install-standby-reads.sh --remove   # disable + remove
#
# Run once on the MAIN server, before `standby-velaris.sh serve-reads on`.
# Installing does nothing by itself: the tunnel starts only while
# .standby/reads-tunnel.env exists, which `serve-reads on` writes and
# `serve-reads off` / `unlink` remove. Neither setup-velaris.sh nor
# update-velaris.sh runs this.
#
# Copyright (c) 2024-2026 Velaris Contributors
# SPDX-License-Identifier: BSL-1.1
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run with sudo." >&2; exit 1; }

VELARIS_DIR="${VELARIS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
VELARIS_USER="${SUDO_USER:-$(stat -c '%U' "$VELARIS_DIR")}"
SRC_DIR="$VELARIS_DIR/deploy/systemd"
UNIT_DIR=/etc/systemd/system
UNITS=(velaris-standby-reads.service velaris-standby-reads.path)

if [ "${1:-}" = "--remove" ]; then
  systemctl disable --now velaris-standby-reads.path velaris-standby-reads.service 2>/dev/null || true
  for u in "${UNITS[@]}"; do rm -f "$UNIT_DIR/$u"; done
  systemctl daemon-reload
  echo "✓ Standby reads units removed."
  exit 0
fi

[ "$VELARIS_USER" != root ] || { echo "✗ Refusing to run the tunnel as root — run sudo from the install owner's shell." >&2; exit 1; }
command -v ssh >/dev/null || { echo "✗ ssh (OpenSSH client) is not installed." >&2; exit 1; }

for unit in "${UNITS[@]}"; do
  sed -e "s|__VELARIS_USER__|$VELARIS_USER|g" \
      -e "s|__VELARIS_DIR__|$VELARIS_DIR|g" \
      "$SRC_DIR/$unit" > "$UNIT_DIR/$unit"
  echo "  installed $UNIT_DIR/$unit"
done
systemctl daemon-reload
systemctl enable velaris-standby-reads.service >/dev/null
systemctl enable --now velaris-standby-reads.path
echo
echo "✓ This server can now read from its standby. Turn it on with:"
echo "    ./standby-velaris.sh serve-reads on"
