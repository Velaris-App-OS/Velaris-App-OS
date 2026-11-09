#!/usr/bin/env bash
# Install (or remove) the daily Velaris backup timer.
#
#   sudo ./deploy/systemd/install-backup-timer.sh            # install + enable
#   sudo ./deploy/systemd/install-backup-timer.sh --remove   # disable + remove
#
# setup-velaris.sh offers this on a NEW install. update-velaris.sh never runs it:
# turning a schedule on is the operator's decision (CLAUDE.md). Re-running is
# safe: the units are rewritten from the templates and the timer re-enabled.
#
# The timer is not the safety net — it is the thing that can quietly stop. The
# platform raises a notification when no backup has succeeded for over a day
# (HxDR), and `systemctl --failed` shows a failed run.
#
# Copyright (c) 2024-2026 Velaris Contributors
# SPDX-License-Identifier: BSL-1.1
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run with sudo." >&2; exit 1; }

VELARIS_DIR="${VELARIS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
VELARIS_USER="${SUDO_USER:-$(stat -c '%U' "$VELARIS_DIR")}"
VELARIS_HOME="$(getent passwd "$VELARIS_USER" | cut -d: -f6)"
SRC_DIR="$VELARIS_DIR/deploy/systemd"
UNIT_DIR=/etc/systemd/system
UNITS=(velaris-backup.service velaris-backup.timer
       velaris-deletions-push.service velaris-deletions-push.path)

if [ "${1:-}" = "--remove" ]; then
  systemctl disable --now velaris-backup.timer velaris-deletions-push.path 2>/dev/null || true
  for u in "${UNITS[@]}"; do rm -f "$UNIT_DIR/$u"; done
  systemctl daemon-reload
  echo "✓ Backup timer removed. Backups already taken are untouched."
  exit 0
fi

# The backup runs as the checkout's owner, who must be able to reach Docker.
if ! id -nG "$VELARIS_USER" | tr ' ' '\n' | grep -qx docker; then
  echo "✗ $VELARIS_USER is not in the docker group — the backup could not reach the containers." >&2
  exit 1
fi

for unit in "${UNITS[@]}"; do
  sed -e "s|__VELARIS_USER__|$VELARIS_USER|g" \
      -e "s|__VELARIS_DIR__|$VELARIS_DIR|g" \
      -e "s|__VELARIS_HOME__|$VELARIS_HOME|g" \
      "$SRC_DIR/$unit" > "$UNIT_DIR/$unit"
  echo "  installed $UNIT_DIR/$unit"
done

systemctl daemon-reload
systemctl enable --now velaris-backup.timer
# Purged accounts' signed records leave the box as soon as they are written.
systemctl enable --now velaris-deletions-push.path
echo
echo "✓ Daily backup enabled. Next run:"
systemctl list-timers velaris-backup.timer --no-pager | head -2
echo
echo "Run one now:   sudo systemctl start velaris-backup.service"
echo "Its output:    journalctl -u velaris-backup.service -n 50"
