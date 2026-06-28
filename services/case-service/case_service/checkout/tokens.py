"""HxCheckout service-token auth.

External sites authenticate order creation with a per-tenant **service token** —
a static API key (no session, no expiry; revoked manually). Only the bcrypt hash
of the secret is persisted (key invariant 1); the plaintext is shown once at
creation and never again.

Token shape:  vsk_<mode>_<keyid>_<secret>
  mode    live | test          — test orders are isolated + flagged is_test
  keyid   12 hex chars         — public; stored as token_prefix (UNIQUE) for O(1)
                                 lookup, since the salted bcrypt hash can't be queried
  secret  48 hex chars         — bcrypt-hashed; the only secret part

`token_prefix` (vsk_<mode>_<keyid>) is shown in Studio as the display hint.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import secrets

import bcrypt
from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.config import get_settings
from case_service.db.models import CheckoutServiceTokenModel, _utcnow
from case_service.db.session import get_session
from case_service.middleware.endpoint_rate_limit import SlidingWindowLimiter

# Per-token order-creation limiter (mass-order-spam guard, key invariant / security
# model). Per-process, in-memory — multi-worker multiplies the effective limit by
# worker count, acceptable for spam reduction (same trade-off as the login limiter).
# max_calls is read from config at first use.
_order_limiter: SlidingWindowLimiter | None = None


def _limiter() -> SlidingWindowLimiter:
    global _order_limiter
    if _order_limiter is None:
        _order_limiter = SlidingWindowLimiter(
            max_calls=get_settings().checkout_token_rate_limit,
            window_seconds=60.0,
            name="checkout_token",
        )
    return _order_limiter


def generate_token(
    session: AsyncSession,
    tenant_id: str,
    label: str = "",
    scope: str = "orders:create",
    mode: str = "live",
    created_by: str | None = None,
) -> tuple[str, CheckoutServiceTokenModel]:
    """Mint a new service token. Returns (plaintext_shown_once, row).

    Caller is responsible for adding the row to the session and committing."""
    if mode not in ("live", "test"):
        raise HTTPException(400, "mode must be 'live' or 'test'")
    keyid = secrets.token_hex(6)
    secret = secrets.token_hex(24)
    prefix = f"vsk_{mode}_{keyid}"
    plaintext = f"{prefix}_{secret}"
    token_hash = bcrypt.hashpw(secret.encode(), bcrypt.gensalt()).decode()
    row = CheckoutServiceTokenModel(
        tenant_id=tenant_id,
        label=label or "",
        token_hash=token_hash,
        token_prefix=prefix,
        scope=scope,
        created_by=created_by,
    )
    session.add(row)
    return plaintext, row


def token_is_test(row: CheckoutServiceTokenModel) -> bool:
    """A token minted in test mode (prefix vsk_test_…) creates isolated test orders."""
    return row.token_prefix.startswith("vsk_test_")


async def resolve_token(session: AsyncSession, raw: str) -> CheckoutServiceTokenModel:
    """Resolve + verify a raw token string to its (active, non-revoked) row.

    Raises 401 on any failure (malformed / unknown / revoked / suspended / bad
    secret). Updates last_used_at on success. Constant-time on the secret via
    bcrypt.checkpw."""
    raw = (raw or "").strip()
    parts = raw.split("_")
    # vsk_<mode>_<keyid>_<secret> → exactly 4 underscore-delimited parts.
    if len(parts) != 4 or parts[0] != "vsk" or parts[1] not in ("live", "test"):
        raise HTTPException(401, "Invalid service token")
    prefix = "_".join(parts[:3])
    secret = parts[3]
    row = (await session.execute(
        select(CheckoutServiceTokenModel).where(
            CheckoutServiceTokenModel.token_prefix == prefix,
        ).limit(1)
    )).scalar_one_or_none()
    if row is None or row.revoked_at is not None or row.suspended:
        raise HTTPException(401, "Invalid service token")
    if not bcrypt.checkpw(secret.encode(), row.token_hash.encode()):
        raise HTTPException(401, "Invalid service token")
    row.last_used_at = _utcnow()
    return row


def check_rate_limit(session: AsyncSession, row: CheckoutServiceTokenModel) -> None:
    """Enforce the per-token order rate limit. On breach, auto-suspend the token
    (security model: 'token suspended automatically on spike') and raise 429."""
    allowed, retry_after = _limiter().allow(str(row.id))
    if not allowed:
        row.suspended = True
        raise HTTPException(
            status_code=429,
            detail="Order rate limit exceeded; token suspended. Contact the store admin.",
            headers={"Retry-After": str(retry_after)},
        )


async def require_service_token(
    x_velaris_token: str | None = Header(default=None, alias="X-Velaris-Token"),
    session: AsyncSession = Depends(get_session),
) -> CheckoutServiceTokenModel:
    """FastAPI dependency: authenticate an external caller by service token.

    The token IS the tenant identifier — its row carries tenant_id, which the
    marketplace-install gate and order creation both key off."""
    if not x_velaris_token:
        raise HTTPException(401, "Missing X-Velaris-Token header")
    return await resolve_token(session, x_velaris_token)
