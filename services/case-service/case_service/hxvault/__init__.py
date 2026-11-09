"""HxVault — moved to `velaris_shared.hxvault` (2026-10-19, step 7 — the commerce split).

commerce-service encrypts payment credentials under the tenant's DEK, so the
vault's encrypt / decrypt / key cache live in the shared layer. One
implementation, one key cache: `crypto` and `keyring` under this package are
`sys.modules` aliases of the shared modules (the KEK trap is about keys that
silently stop matching — two caches would be exactly that).

What stayed: the two checks in front of a crypto-shred that need case-service's
domain (legal hold, HxArbiter) — `shred_guards`, registered below, so a shred
from this process runs them exactly as before. A process that registers none
(commerce-service) cannot shred at all.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from velaris_shared.hxvault import (  # noqa: F401
    DekNotLoadedError,
    KeyShreddedError,
    __all__,
    _aad,
    _b64d,
    _b64e,
    _missing_dek,
    _PREFIX,
    _VERSION,
    _ckey,
    _norm,
    cached_dek,
    clear_cache,
    crypto,
    decrypt,
    encrypt,
    ensure_dek,
    is_hxv2,
    is_shredded,
    keyring,
    resync_cache,
    shred,
    warm_cache,
)

from case_service.hxvault import shred_guards as _guards

keyring.register_shred_guard(_guards.legal_hold_guard)
keyring.register_shred_guard(_guards.arbiter_guard)
