"""HxVault (#19) — per-tenant DEK lifecycle: generate, wrap, unwrap, shred.

A DEK is random (os.urandom) and STORED wrapped under the master KEK — never
derived — so deleting it (crypto-shred) makes the data unrecoverable.

Unwrapped DEKs are held in a process-global cache so the sync encrypt/decrypt
API stays sync. tenant_id NULL = the platform DEK (tenantless/HxFusion data).

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import os
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import TenantDekModel
from velaris_shared.hxvault import crypto

logger = logging.getLogger(__name__)

# cache_key -> unwrapped DEK bytes.  cache_key: str(tenant_id) | "" for platform.
_DEK_CACHE: dict[str, bytes] = {}

#: Cache keys whose DEK is *known shredded*, as opposed to merely absent.
#:
#: Without this the sync read path cannot tell the two apart — both are a miss in
#: `_DEK_CACHE` — so a permanently unrecoverable read reported `DekNotLoadedError`
#: (503, "the data itself is intact"), telling an operator to retry data that is
#: gone. That is precisely the confusion the two error codes exist to prevent.
#: Populated wherever the DB is read anyway: `shred`, `warm_cache`, `resync_cache`.
_SHREDDED: set[str] = set()


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


def is_shredded(tenant_id) -> bool:
    """Sync: is this tenant's DEK known to have been shredded?

    False is not a promise the key exists — only that this worker has no
    evidence it was destroyed. A hard-deleted row is unknowable either way.
    """
    return _ckey(_norm(tenant_id)) in _SHREDDED


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
            # Record it so the *sync* read path can raise the same distinction;
            # this async path is not the one an operator usually hits.
            _SHREDDED.add(ck)
            raise KeyShreddedError(f"DEK for tenant {ck or '<platform>'} has been shredded")
        dek = crypto.open_(kek, _b64d(row.wrapped_dek), _wrap_aad(tid))
        _DEK_CACHE[ck] = dek
        return dek

    # Create a fresh DEK, wrap, store.
    dek = os.urandom(crypto.KEY_LEN)
    wrapped = _b64e(crypto.seal(kek, dek, _wrap_aad(tid)))
    session.add(TenantDekModel(tenant_id=tid, key_version=1, wrapped_dek=wrapped, status="active"))
    await session.flush()
    # Other processes load the new key now, instead of failing reads of this
    # tenant's data until their next resync (R5e step 5).
    from velaris_shared import generations
    await generations.bump(session, "vault", reason="dek created")
    _DEK_CACHE[ck] = dek
    logger.info("HxVault: created DEK for tenant %s", ck or "<platform>")
    return dek


#: DEK row ids already reported as unreadable, so a permanent condition is
#: stated once instead of every 30 seconds forever.
#:
#: A DEK that will not unwrap will not unwrap on the next tick either — the KEK
#: and the row are both unchanged. Logging a fresh stack trace each cycle buries
#: the events that ARE new, and says nothing about what to do. Report it once,
#: name the tenant, and say what it means.
_UNREADABLE: set[str] = set()


def _report_unreadable(row, where: str) -> None:
    key = str(row.id)
    if key in _UNREADABLE:
        return
    _UNREADABLE.add(key)
    logger.error(
        "HxVault %s: DEK id=%s (tenant %s) cannot be unwrapped with the current "
        "KEK. Anything encrypted under it is unreadable — check whether "
        "VELARIS_CASE_KEK changed, or whether auth_secret changed while "
        "VELARIS_CASE_KEK was unset (the KEK is derived from it). This is "
        "reported once per process; the row is skipped, not retried.",
        where, row.id, _ckey(row.tenant_id) or "<platform>",
    )


async def warm_cache(session: AsyncSession) -> int:
    """Load + unwrap all active DEKs into the cache (call at startup). Returns count.

    Reads *every* row, not just the active ones: a shredded row carries no key to
    warm, but it is the only record that the key was destroyed rather than never
    created, and the sync read path needs that to report 410 instead of 503.
    """
    rows = (await session.execute(select(TenantDekModel))).scalars().all()
    kek = crypto.get_kek()
    n = 0
    for row in rows:
        ck = _ckey(row.tenant_id)
        if row.status != "active":
            _SHREDDED.add(ck)
            continue
        try:
            _DEK_CACHE[ck] = crypto.open_(kek, _b64d(row.wrapped_dek), _wrap_aad(row.tenant_id))
            n += 1
        except Exception:  # noqa: BLE001 — one bad row must not block startup
            _report_unreadable(row, "startup")
    logger.info(
        "HxVault: warmed %d DEK(s) into cache (%d shredded)", n, len(_SHREDDED),
    )
    return n


async def resync_cache(session: AsyncSession) -> dict:
    """Reconcile this worker's DEK cache with the DB — the multi-worker coherence fix.

    `warm_cache` only runs at startup, so a DEK created on ANOTHER worker after this
    worker started (or one shredded elsewhere) is invisible here: sync `decrypt`
    would miss-and-raise, and a shredded key would stay decryptable. Run periodically
    (lifespan loop) to make all workers eventually consistent within the interval:
    pull in newly-active DEKs and evict any no longer active (shredded/deleted).

    Also reconciles the shredded set, so a key shredded on another worker starts
    reading as 410 here within one interval instead of as a transient 503."""
    rows = (await session.execute(select(TenantDekModel))).scalars().all()
    kek = crypto.get_kek()
    live: set[str] = set()
    shredded: set[str] = set()
    added = 0
    for row in rows:
        ck = _ckey(row.tenant_id)
        if row.status != "active":
            shredded.add(ck)
            continue
        live.add(ck)
        if ck not in _DEK_CACHE:
            try:
                _DEK_CACHE[ck] = crypto.open_(kek, _b64d(row.wrapped_dek), _wrap_aad(row.tenant_id))
                added += 1
            except Exception:  # noqa: BLE001 — one bad row must not break the loop
                _report_unreadable(row, "resync")
    evicted = 0
    for ck in [k for k in _DEK_CACHE if k not in live]:
        _DEK_CACHE.pop(ck, None)
        evicted += 1
    # Replaced, not unioned: the DB is the authority. A row that is gone entirely
    # leaves no evidence of shredding, and inventing some would be a guess.
    _SHREDDED.clear()
    _SHREDDED.update(shredded)
    if added or evicted:
        logger.info(
            "HxVault resync: +%d DEK(s), -%d evicted (%d active, %d shredded)",
            added, evicted, len(live), len(shredded),
        )
    return {
        "added": added, "evicted": evicted,
        "active": len(live), "shredded": len(shredded),
    }


#: Pre-shred checks, in order (see `shred`). Registered by case-service.
_SHRED_GUARDS: list = []


def register_shred_guard(fn) -> None:
    """Add a pre-shred check: `await fn(session, tenant_uuid_or_None)`; raise to refuse."""
    if fn not in _SHRED_GUARDS:
        _SHRED_GUARDS.append(fn)


async def shred(session: AsyncSession, tenant_id) -> bool:
    """Crypto-shred: delete the wrapped DEK + evict cache. Returns True if a key existed.

    After this the data encrypted under the DEK is permanently unrecoverable.
    """
    tid = _norm(tenant_id)

    # The checks that need case-service's domain — legal hold first, then
    # HxArbiter — are registered by case-service (`case_service.hxvault`,
    # step 7). A process with none registered refuses: commerce-service has no
    # business shredding a tenant's key, and must not do it unchecked.
    if not _SHRED_GUARDS:
        raise PermissionError(
            "HxVault: crypto-shred is not available in this process (no shred guards).")
    for guard in _SHRED_GUARDS:
        await guard(session, tid)

    stmt = select(TenantDekModel).where(
        TenantDekModel.tenant_id.is_(None) if tid is None else TenantDekModel.tenant_id == tid
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    _DEK_CACHE.pop(_ckey(tid), None)
    if row is None:
        return False
    _SHREDDED.add(_ckey(tid))
    row.status = "shredded"
    row.wrapped_dek = ""
    await session.flush()
    # Every other process drops the key within seconds, not at its next ~30 s
    # resync: a shredded key must stop decrypting everywhere (R5e step 5).
    from velaris_shared import generations
    await generations.bump(session, "vault", reason="dek shredded")
    logger.warning("HxVault: SHREDDED DEK for tenant %s", _ckey(tid) or "<platform>")
    return True


# A key was created or shredded in another process: reconcile now.
from velaris_shared import generations as _generations  # noqa: E402


async def _on_vault_change(session: AsyncSession) -> None:
    await resync_cache(session)


_generations.on_change("vault", _on_vault_change)


def clear_cache() -> None:
    """Test hook — drop all cached DEKs and the shredded set."""
    _DEK_CACHE.clear()
    _SHREDDED.clear()


# ── base64 helpers ────────────────────────────────────────────────────────────
import base64  # noqa: E402


def _b64e(b: bytes) -> str:
    return base64.b64encode(b).decode()


def _b64d(s: str) -> bytes:
    return base64.b64decode(s.encode())


class KeyShreddedError(Exception):
    """Raised when a DEK has been crypto-shredded — data is unrecoverable."""
