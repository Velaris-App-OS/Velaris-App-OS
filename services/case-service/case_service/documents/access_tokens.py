"""Moved to `velaris_shared.documents.access_tokens` (2026-10-21, step 8 — the portal split).

Both case-service and portal-service call this code in-process, so it lives in
the shared layer. This module IS that module (`sys.modules` alias): every name,
every cache and every test patch made through this path reaches the one real
implementation.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
import sys as _sys

import velaris_shared.documents.access_tokens as _moved

_sys.modules[__name__] = _moved
