#!/usr/bin/env bash
# Make a support bundle to send with a fault report (R7) — no secrets, no
# personal data. See scripts/support_bundle.py for exactly what goes in.
#
#   ./support-bundle.sh                 velaris-support-<host>-<time>.tar.gz here
#   ./support-bundle.sh --out /tmp      written there instead
#   ./support-bundle.sh --lines 2000    more log lines per service (default 500)
#
# Copyright (c) 2024-2026 Velaris Contributors
# SPDX-License-Identifier: BSL-1.1
set -euo pipefail
VELARIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$VELARIS_DIR/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
exec "$PY" "$VELARIS_DIR/scripts/support_bundle.py" "$@"
