"""HxVault (#19) — per-tenant DEK lifecycle: generate, wrap, unwrap, shred.

A DEK is random (os.urandom) and STORED wrapped under the master KEK — never
derived — so deleting it (crypto-shred) makes the data unrecoverable.

Unwrapped DEKs are held in a process-global cache so the sync encrypt/decrypt
API stays sync. tenant_id NULL = the platform DEK (tenantless/HxFusion data).

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import os
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import TenantDekModel
from case_service.hxvault import crypto

logger = logging.getLogger(__name__)

# cache_key -> unwrapped DEK bytes.  cache_key: str(tenant_id) | "" for platform.
_DEK_CACHE: dict[str, bytes] = {}


def _norm(tenant_id) -> uuid.UUID | None:
    if tenant_id in (None, "", "default"):
        return None
    if isinstance(tenant_id, uuid.UUID):
        return tenant_id
    try:
        return uuid.UUID(str(tenant_id))
    except (ValueError, AttributeError, TypeError):
        # tenant_deks.tenant_id is a UUID column, so a non-UUID tenant identifier
        # (legacy slug) has no per-tenant DEK and falls back to the platform DEK.
        # Logged (not a 500) so the fallback is visible.
        logger.warning("HxVault: non-UUID tenant_id %r → using platform DEK", tenant_id)
        return None


def _ckey(tenant_id: uuid.UUID | None) -> str:
    return str(tenant_id) if tenant_id is not None else ""


def _wrap_aad(tenant_id: uuid.UUID | None) -> bytes:
    # Binds a wrapped DEK to its tenant row — it cannot be lifted to another tenant.
    return b"velaris-dek-wrap|" + _ckey(tenant_id).encode()


def cached_dek(tenant_id) -> bytes | None:
    """Sync cache lookup — used by the sync encrypt/decrypt path."""
    return _DEK_CACHE.get(_ckey(_norm(tenant_id)))


async def ensure_dek(session: AsyncSession, tenant_id) -> bytes:
    """Return the unwrapped DEK for a tenant, creating+storing it if absent.

    Async because it may read/write the tenant_deks table. Call this in the
    (async) write path before sync-encrypting; reads use the warmed cache.
    """
    tid = _norm(tenant_id)
    ck = _ckey(tid)
    hit = _DEK_CACHE.get(ck)
    if hit is not None:
        return hit

    stmt = select(TenantDekModel).where(
        TenantDekModel.tenant_id.is_(None) if tid is None else TenantDekModel.tenant_id == tid
    )
    row = (await session.execute(stmt)).scalar_one_or_none()

    kek = crypto.get_kek()
    if row is not None:
        if row.status != "active":
            raise KeyShreddedError(f"DEK for tenant {ck or '<platform>'} has been shredded")
        dek = crypto.open_(kek, _b64d(row.wrapped_dek), _wrap_aad(tid))
        _DEK_CACHE[ck] = dek
        return dek

    # Create a fresh DEK, wrap, store.
    dek = os.urandom(crypto.KEY_LEN)
    wrapped = _b64e(crypto.seal(kek, dek, _wrap_aad(tid)))
    session.add(TenantDekModel(tenant_id=tid, key_version=1, wrapped_dek=wrapped, status="active"))
    await session.flush()
    _DEK_CACHE[ck] = dek
    logger.info("HxVault: created DEK for tenant %s", ck or "<platform>")
    return dek


async def warm_cache(session: AsyncSession) -> int:
    """Load + unwrap all active DEKs into the cache (call at startup). Returns count."""
    rows = (await session.execute(
        select(TenantDekModel).where(TenantDekModel.status == "active")
    )).scalars().all()
    kek = crypto.get_kek()
    n = 0
    for row in rows:
        try:
            _DEK_CACHE[_ckey(row.tenant_id)] = crypto.open_(kek, _b64d(row.wrapped_dek), _wrap_aad(row.tenant_id))
            n += 1
        except Exception:  # noqa: BLE001 — one bad row must not block startup
            logger.exception("HxVault: failed to unwrap DEK id=%s", row.id)
    logger.info("HxVault: warmed %d DEK(s) into cache", n)
    return n


async def resync_cache(session: AsyncSession) -> dict:
    """Reconcile this worker's DEK cache with the DB — the multi-worker coherence fix.

    `warm_cache` only runs at startup, so a DEK created on ANOTHER worker after this
    worker started (or one shredded elsewhere) is invisible here: sync `decrypt`
    would miss-and-raise, and a shredded key would stay decryptable. Run periodically
    (lifespan loop) to make all workers eventually consistent within the interval:
    pull in newly-active DEKs and evict any no longer active (shredded/deleted)."""
    rows = (await session.execute(
        select(TenantDekModel).where(TenantDekModel.status == "active")
    )).scalars().all()
    kek = crypto.get_kek()
    live: set[str] = set()
    added = 0
    for row in rows:
        ck = _ckey(row.tenant_id)
        live.add(ck)
        if ck not in _DEK_CACHE:
            try:
                _DEK_CACHE[ck] = crypto.open_(kek, _b64d(row.wrapped_dek), _wrap_aad(row.tenant_id))
                added += 1
            except Exception:  # noqa: BLE001 — one bad row must not break the loop
                logger.exception("HxVault resync: failed to unwrap DEK id=%s", row.id)
    evicted = 0
    for ck in [k for k in _DEK_CACHE if k not in live]:
        _DEK_CACHE.pop(ck, None)
        evicted += 1
    if added or evicted:
        logger.info("HxVault resync: +%d DEK(s), -%d evicted (%d active)", added, evicted, len(live))
    return {"added": added, "evicted": evicted, "active": len(live)}


async def shred(session: AsyncSession, tenant_id) -> bool:
    """Crypto-shred: delete the wrapped DEK + evict cache. Returns True if a key existed.

    After this the data encrypted under the DEK is permanently unrecoverable.
    """
    tid = _norm(tenant_id)
    stmt = select(TenantDekModel).where(
        TenantDekModel.tenant_id.is_(None) if tid is None else TenantDekModel.tenant_id == tid
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    _DEK_CACHE.pop(_ckey(tid), None)
    if row is None:
        return False
    row.status = "shredded"
    row.wrapped_dek = ""
    await session.flush()
    logger.warning("HxVault: SHREDDED DEK for tenant %s", _ckey(tid) or "<platform>")
    return True


def clear_cache() -> None:
    """Test hook — drop all cached DEKs."""
    _DEK_CACHE.clear()


# ── base64 helpers ────────────────────────────────────────────────────────────
import base64  # noqa: E402


def _b64e(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _b64d(s: str) -> bytes:
    return base64.b64decode(s.encode())


class KeyShreddedError(Exception):
    """Raised when a DEK has been crypto-shredded — data is unrecoverable."""
