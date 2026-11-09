"""case-service's internal API (R5e step 7 — the commerce split).

What commerce-service asks case-service to do, because only case-service may:
open an order's case, advance a case, apply a pack's bundle and rails, run the
marketplace egress policy, and read / write HxSentinel. Routes: `router.py`;
guard: `velaris_shared.internal.auth`.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
