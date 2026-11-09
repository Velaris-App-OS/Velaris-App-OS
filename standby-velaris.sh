#!/usr/bin/env bash
# Velaris — a standby server: a second server that receives every database
# change as it happens (R5c, Nov_TODO step 11). Optional; nothing changes on
# this server until `link` runs.
#
# On the server that is to become the standby, once:
#   install the SAME Velaris release there (do not start it), then
#   sudo ./deploy/systemd/install-standby-tunnel.sh
#
# On the main server:
#   ./standby-velaris.sh link [user@]standby-host   # copy both databases, start streaming
#   ./standby-velaris.sh status [--json]            # streaming / disconnected / needs_relink
#   ./standby-velaris.sh unlink [--local-only]      # stop; the main keeps running
#
# The servers talk over SSH only: no port is opened to the network. Replication
# is asynchronous: on a switch-over, what is lost is the lag (`status`).
#
# Exit status: 0 done / streaming, 2 refused (the message says why),
# 3 linked but not streaming.
#
# Engine: scripts/standby/velaris_standby.py (standard library only).
#
# Copyright (c) 2024-2026 Velaris Contributors
# SPDX-License-Identifier: BSL-1.1
set -euo pipefail
VELARIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$VELARIS_DIR/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "✗ python3 not found" >&2; exit 1; }
exec "$PY" "$VELARIS_DIR/scripts/standby/velaris_standby.py" "$@"
