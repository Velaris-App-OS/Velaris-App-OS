"""HxVault (#19) — per-tenant DEK envelope encryption.

Public surface:
  await ensure_dek(session, tenant_id)   — create/load a tenant's DEK (async write path)
  await warm_cache(session)              — load all DEKs at startup (keeps decrypt sync)
  await shred(session, tenant_id)        — crypto-shred (GDPR Art-17)
  encrypt(tenant_id, plaintext, context) — sync; DEK must be ensured/warmed first
  decrypt(ciphertext)                    — sync; tenant+context read from the envelope

Ciphertext is SELF-DESCRIBING so read sites need no tenant arg:
  "hxv2:<tenant|->:<key_version>:<context_b64>:<payload_b64>"
AAD = "<tenant>|<key_version>|<context>" — tampering with any envelope field
(e.g. swapping tenant or context) fails decryption.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import base64

from velaris_shared.hxvault import crypto
from velaris_shared.hxvault.keyring import (
    KeyShreddedError,
    cached_dek,
    clear_cache,
    ensure_dek,
    is_shredded,
    resync_cache,
    shred,
    warm_cache,
    _norm,
    _ckey,
)

__all__ = [
    "encrypt", "decrypt", "is_hxv2",
    "ensure_dek", "warm_cache", "resync_cache", "shred", "clear_cache",
    "is_shredded", "KeyShreddedError", "DekNotLoadedError",
]

_PREFIX = "hxv2:"
_VERSION = 1


class DekNotLoadedError(Exception):
    """DEK not in cache — call ensure_dek()/warm_cache() in an async context first.

    Strictly the *transient* case. A shredded key raises `KeyShreddedError`
    instead: both are a cache miss, but one is fixed by warming and the other is
    never fixed by anything, and they map to different HTTP codes.
    """


def _missing_dek(tenant_id, hint: str) -> Exception:
    """Which failure a cache miss actually is.

    The cache cannot answer this on its own — a shredded DEK and one that was
    never warmed are both simply absent — so the keyring records the shredded
    tenants separately when it reads the table.
    """
    if is_shredded(tenant_id):
        return KeyShreddedError(
            f"DEK for tenant {_ckey(_norm(tenant_id)) or '<platform>'} has been "
            "shredded; data encrypted under it is unrecoverable."
        )
    return DekNotLoadedError(hint)


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _aad(tenant_field: str, version: int, context: str) -> bytes:
    return f"{tenant_field}|{version}|{context}".encode()


def is_hxv2(value: str) -> bool:
    return isinstance(value, str) and value.startswith(_PREFIX)


def encrypt(tenant_id, plaintext: bytes, context: str) -> str:
    """Encrypt bytes under the tenant's DEK. DEK must already be cached."""
    tid = _norm(tenant_id)
    dek = cached_dek(tid)
    if dek is None:
        raise _missing_dek(
            tid,
            f"No DEK cached for tenant {_ckey(tid) or '<platform>'}; "
            "await hxvault.ensure_dek(session, tenant_id) first.",
        )
    tfield = _ckey(tid) or "-"
    ctx_b64 = _b64e(context.encode())
    blob = crypto.seal(dek, plaintext, _aad(tfield, _VERSION, context))
    return f"{_PREFIX}{tfield}:{_VERSION}:{ctx_b64}:{_b64e(blob)}"


def decrypt(ciphertext: str) -> bytes:
    """Decrypt an hxv2 envelope. Tenant + context are read from the envelope."""
    if not is_hxv2(ciphertext):
        raise ValueError("not an hxv2 ciphertext")
    try:
        _, tfield, ver_s, ctx_b64, payload = ciphertext.split(":", 4)
    except ValueError as e:
        raise ValueError("malformed hxv2 ciphertext") from e
    tenant_id = None if tfield == "-" else tfield
    dek = cached_dek(tenant_id)
    if dek is None:
        # This is the read path — the one an operator reaches when a user asks
        # why a document will not open — so it is the path that most needs to
        # say "gone forever" rather than "try again".
        raise _missing_dek(
            tenant_id,
            f"No DEK cached for tenant {tfield}; warm_cache()/ensure_dek() first.",
        )
    context = _b64d(ctx_b64).decode()
    return crypto.open_(dek, _b64d(payload), _aad(tfield, int(ver_s), context))
