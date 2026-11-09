"""Moved to `velaris_shared.hxvault.crypto` (2026-10-19, step 7 — the commerce split).

This module IS that module (`sys.modules` alias): one implementation, one key
cache — every name, cache and test patch through this path reaches it.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
import sys as _sys

import velaris_shared.hxvault.crypto as _moved

_sys.modules[__name__] = _moved
