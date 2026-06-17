"""HxVault (#19) — per-tenant DEK envelope encryption.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from case_service import hxvault
from case_service.hxvault import crypto, keyring
from case_service.hxbridge.encryption import (
    encrypt_credentials,
    decrypt_credentials,
    mask_credentials,
)


@pytest_asyncio.fixture(autouse=True)
async def _isolate_vault():
    """DEK cache is process-global — clear it (and the KEK cache) each test."""
    keyring.clear_cache()
    crypto.reset_kek_cache()
    yield
    keyring.clear_cache()
    crypto.reset_kek_cache()


# ── Acceptance gate: round-trip for a real tenant AND None ────────────────────

@pytest.mark.asyncio
async def test_roundtrip_real_tenant(session):
    tid = uuid.uuid4()
    await hxvault.ensure_dek(session, tid)
    ct = hxvault.encrypt(tid, b"top-secret", "connector_credentials")
    assert ct.startswith("hxv2:")
    assert hxvault.decrypt(ct) == b"top-secret"


@pytest.mark.asyncio
async def test_roundtrip_platform_tenantless(session):
    await hxvault.ensure_dek(session, None)
    ct = hxvault.encrypt(None, b"tenantless-data", "case_data")
    assert ct.startswith("hxv2:-:")          # "-" marks the platform/None scope
    assert hxvault.decrypt(ct) == b"tenantless-data"


@pytest.mark.asyncio
async def test_two_tenants_get_distinct_keys(session):
    a, b = uuid.uuid4(), uuid.uuid4()
    await hxvault.ensure_dek(session, a)
    await hxvault.ensure_dek(session, b)
    ct_a = hxvault.encrypt(a, b"x", "ctx")
    # tenant b cannot have produced ct_a; decrypt still works because tenant is
    # self-described in the envelope and a's DEK is cached.
    assert hxvault.decrypt(ct_a) == b"x"
    assert keyring.cached_dek(a) != keyring.cached_dek(b)


# ── Crypto-shredding ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_shred_makes_data_unrecoverable(session):
    tid = uuid.uuid4()
    await hxvault.ensure_dek(session, tid)
    ct = hxvault.encrypt(tid, b"gdpr", "ctx")
    assert await hxvault.shred(session, tid) is True
    with pytest.raises(hxvault.DekNotLoadedError):
        hxvault.decrypt(ct)
    # re-ensure after shred must refuse to resurrect the shredded key
    with pytest.raises(keyring.KeyShreddedError):
        await hxvault.ensure_dek(session, tid)


@pytest.mark.asyncio
async def test_shred_nonexistent_returns_false(session):
    assert await hxvault.shred(session, uuid.uuid4()) is False


# ── Tamper / wrong-key resistance ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_aad_tamper_swap_tenant_fails(session):
    tid = uuid.uuid4()
    await hxvault.ensure_dek(session, tid)
    other = uuid.uuid4()
    await hxvault.ensure_dek(session, other)
    ct = hxvault.encrypt(tid, b"secret", "ctx")
    # rewrite the envelope's tenant field to another (cached) tenant → AAD mismatch
    parts = ct.split(":", 4)
    parts[1] = str(other)
    forged = ":".join(parts)
    with pytest.raises(Exception):
        hxvault.decrypt(forged)


@pytest.mark.asyncio
async def test_aad_tamper_swap_context_fails(session):
    tid = uuid.uuid4()
    await hxvault.ensure_dek(session, tid)
    ct = hxvault.encrypt(tid, b"secret", "ctx-a")
    import base64
    parts = ct.split(":", 4)
    parts[3] = base64.urlsafe_b64encode(b"ctx-b").decode().rstrip("=")
    with pytest.raises(Exception):
        hxvault.decrypt(":".join(parts))


@pytest.mark.asyncio
async def test_wrong_kek_cannot_unwrap(session):
    tid = uuid.uuid4()
    await hxvault.ensure_dek(session, tid)
    # Change the KEK and drop the cache → unwrap from DB must fail.
    keyring.clear_cache()
    crypto.reset_kek_cache()
    crypto._kek_cache = b"\x00" * 32  # force a wrong KEK
    with pytest.raises(Exception):
        await hxvault.ensure_dek(session, tid)


# ── Consumer: connector credentials (hxv2) + legacy hxv1 back-compat ──────────

@pytest.mark.asyncio
async def test_connector_creds_vault_roundtrip(session):
    tid = uuid.uuid4()
    await hxvault.ensure_dek(session, tid)
    creds = {"api_key": "sk_live_abc", "secret": "shh"}
    stored = encrypt_credentials(creds, tenant_id=tid, vault=True)
    assert stored["_enc"].startswith("hxv2:")
    assert decrypt_credentials(stored) == creds
    assert mask_credentials(stored) == {"_enc": "***"}


def test_legacy_hxv1_still_decrypts():
    # Default (vault=False) path writes hxv1; must keep decrypting after #19.
    creds = {"token": "legacy"}
    stored = encrypt_credentials(creds)
    assert stored["_enc"].startswith("hxv1:")
    assert decrypt_credentials(stored) == creds


def test_plain_dict_passthrough():
    assert decrypt_credentials({"user": "x"}) == {"user": "x"}
    assert decrypt_credentials({}) == {}


@pytest.mark.asyncio
async def test_non_uuid_tenant_falls_back_to_platform(session):
    # A legacy non-UUID tenant slug must NOT 500 — it maps to the platform DEK.
    await hxvault.ensure_dek(session, "t1")          # slug, not a UUID
    ct = hxvault.encrypt("t1", b"legacy", "ctx")
    assert ct.startswith("hxv2:-:")                   # "-" = platform scope
    assert hxvault.decrypt(ct) == b"legacy"


# ── Multi-worker coherence: resync_cache ──────────────────────────────────────

@pytest.mark.asyncio
async def test_resync_loads_dek_created_elsewhere(session):
    """Simulates a DEK created on another worker: this worker's cache misses, then
    resync pulls it in so the sync decrypt path stops raising."""
    tid = uuid.uuid4()
    await hxvault.ensure_dek(session, tid)                    # worker A creates + stores
    ct = hxvault.encrypt(tid, b"cross-worker", "connector_credentials")

    keyring.clear_cache()                                     # worker B: cold cache
    with pytest.raises(hxvault.DekNotLoadedError):
        hxvault.decrypt(ct)

    out = await hxvault.resync_cache(session)                 # worker B reconciles
    assert out["added"] >= 1
    assert hxvault.decrypt(ct) == b"cross-worker"             # now decryptable


@pytest.mark.asyncio
async def test_resync_evicts_shredded_dek(session):
    """A DEK shredded on another worker must be evicted here so it stops decrypting."""
    tid = uuid.uuid4()
    await hxvault.ensure_dek(session, tid)
    assert keyring.cached_dek(tid) is not None
    await hxvault.shred(session, tid)                         # shredded (DB row → shredded)
    keyring._DEK_CACHE[keyring._ckey(tid)] = b"stale-still-cached"  # simulate other worker's stale entry
    out = await hxvault.resync_cache(session)
    assert out["evicted"] >= 1
    assert keyring.cached_dek(tid) is None
