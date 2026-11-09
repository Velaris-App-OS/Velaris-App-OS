"""Password hashing — one implementation for every service (R5e step 10a).

Moved out of `case_service/api/routers/auth_real.py` when sign-in left
case-service for user-service: hxdraft (case-service) creates accounts too, and
two copies of a bcrypt cost factor are two answers to "how strong is a stored
password". `auth_real._hash_password` / `_verify_password` are these.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import bcrypt

#: bcrypt cost. Raising it re-hashes nothing: an old hash keeps its own cost.
ROUNDS = 12


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt(rounds=ROUNDS)).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


# ── Off the event loop ─────────────────────────────────────────────────────────
# A cost-12 bcrypt is ~0.2 s of CPU. Called in an `async def` route it held the
# whole event loop: on a clean box (R5e step 10e, 2026-10-22) 150 users signing
# in together waited 15 s at the median and 28 s at p95 — and every other request
# to the service queued behind them. bcrypt releases the GIL, so a worker thread
# runs it in parallel and the loop keeps serving. Same hashes, same answers.

async def hash_password_async(plain: str) -> str:
    import asyncio
    return await asyncio.to_thread(hash_password, plain)


async def verify_password_async(plain: str, hashed: str) -> bool:
    import asyncio
    return await asyncio.to_thread(verify_password, plain, hashed)
