#!/usr/bin/env bash
# Build docs/operator/Velaris-Operator-Guide.pdf from the Markdown sources (R7).
# Needs uv (for the Markdown converter, in a throwaway environment) and Chrome or
# Chromium. The Markdown files stay the source; rebuild after editing any.
#
# Copyright (c) 2024-2026 Velaris Contributors
# SPDX-License-Identifier: BSL-1.1
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
exec "$UV" run --no-project --with markdown==3.7 python scripts/operator_guide.py "$@"
