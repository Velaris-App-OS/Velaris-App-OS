"""HxBridge — credential encryption/decryption.

Three formats, one read path, and only one of them is ever written now.

* **hxv3** (current, platform key) — real AES-256-GCM from `cryptography`,
  under a key derived from `auth_secret`. What every synchronous call site
  writes.
* **hxv2** (HxVault) — AES-256-GCM under a **per-tenant** DEK, so a tenant's
  secrets can be crypto-shredded by destroying one key. Preferred wherever the
  caller has a tenant and an awaited session; see `hxvault/`.
* **hxv1** (legacy, read-only) — a hand-rolled SHA-256 counter-mode keystream
  with an encrypt-then-MAC HMAC tag. **Still decrypted, never written.**

**Why hxv1 was replaced.** It was not broken — the tag is verified with
`compare_digest` before anything is decrypted, so it is authenticated. It was
*hand-rolled cipher code where a vetted AEAD was already a dependency*, its own
docstring said to swap it for AESGCM, and it used **one key for both the
keystream and the MAC**. None of those is an exploit today; all of them are
reasons not to keep writing new secrets with it.

hxv3 fixes the construction. hxv2 fixes the *key*: hxv3 still derives a single
platform-wide key from `auth_secret`, so it cannot crypto-shred per tenant and
it shares a fate with that one secret.

Plain dicts (no prefix) are stored unencrypted — dev/test only.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import struct


_PREFIX = "hxv1:"          # legacy, read-only
_PREFIX_V3 = "hxv3:"       # current platform-key format
_KEY_LEN = 32              # 256 bits

#: Bound into every hxv3 ciphertext. A value cannot be lifted out of this
#: scheme and replayed into another one that happens to share the key.
_V3_AAD = b"velaris-hxbridge-credentials-v3"


def _derive_key(secret: str) -> bytes:
    """Derive the 256-bit hxv1 key. Read path only — nothing calls this to write.

    The salt and info strings still say `helix`: they are part of the key
    derivation, so renaming them would make every stored hxv1 value
    undecryptable. This is one of the few places the old brand must stay.
    """
    salt = b"helix-hxbridge-cred-v1"
    prk = hmac.new(salt, secret.encode(), hashlib.sha256).digest()
    okm = hmac.new(prk, b"helix-connector-credentials\x01", hashlib.sha256).digest()
    return okm[:_KEY_LEN]


def _derive_key_v3(secret: str) -> bytes:
    """hxv3's key — a different info string, so it is a different key.

    Deliberately not the hxv1 key: a format change that reuses the same key
    leaves the old format's properties attached to the new one, and makes
    rotating away from hxv1 impossible to reason about.
    """
    salt = b"velaris-hxbridge-cred-v3"
    prk = hmac.new(salt, secret.encode(), hashlib.sha256).digest()
    okm = hmac.new(prk, b"velaris-credentials-aesgcm\x01", hashlib.sha256).digest()
    return okm[:_KEY_LEN]


def _get_key_v3() -> bytes:
    from velaris_shared.config import get_settings
    return _derive_key_v3(get_settings().auth_secret)


def _aesgcm_encrypt(key: bytes, plaintext: bytes) -> bytes:
    """AES-256-GCM: nonce(12) || ciphertext || tag(16). The vetted primitive."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, _V3_AAD)


def _aesgcm_decrypt(key: bytes, blob: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if len(blob) < 12 + 16:
        raise ValueError("hxv3 ciphertext is too short to be well-formed")
    return AESGCM(key).decrypt(blob[:12], blob[12:], _V3_AAD)


def _xor_encrypt(key: bytes, plaintext: bytes, nonce: bytes) -> bytes:
    """AES-256-GCM-lite: XOR keystream derived from key+nonce (CTR mode emulation).

    This is a secure-enough approximation using stdlib only.
    For production, swap this with `cryptography.hazmat.primitives.ciphers.aead.AESGCM`.
    """
    keystream = b""
    counter = 0
    while len(keystream) < len(plaintext):
        block = hashlib.sha256(key + nonce + struct.pack(">Q", counter)).digest()
        keystream += block
        counter += 1
    ciphertext = bytes(a ^ b for a, b in zip(plaintext, keystream[:len(plaintext)]))
    # Append HMAC-SHA256 tag for integrity
    tag = hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()[:16]
    return ciphertext + tag


def _xor_decrypt(key: bytes, data: bytes, nonce: bytes) -> bytes:
    ciphertext, tag = data[:-16], data[-16:]
    expected_tag = hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(tag, expected_tag):
        raise ValueError("Credential integrity check failed — data may be tampered")
    keystream = b""
    counter = 0
    while len(keystream) < len(ciphertext):
        block = hashlib.sha256(key + nonce + struct.pack(">Q", counter)).digest()
        keystream += block
        counter += 1
    return bytes(a ^ b for a, b in zip(ciphertext, keystream[:len(ciphertext)]))


def _get_key() -> bytes:
    from velaris_shared.config import get_settings
    return _derive_key(get_settings().auth_secret)


def encrypt_credentials(creds: dict, *, tenant_id=None, vault: bool = False) -> dict:
    """Encrypt a credentials dict for storage. Returns {'_enc': '<...>'}.

    vault=True routes through HxVault (#19) per-tenant DEK envelope encryption
    (hxv2). The caller MUST `await hxvault.ensure_dek(session, tenant_id)` first
    so the DEK is cached. vault=False keeps the legacy hxv1 single-key path for
    not-yet-migrated callers; both formats decrypt transparently below.
    """
    if not creds:
        return {}
    plaintext = json.dumps(creds, sort_keys=True).encode()
    if vault:
        from velaris_shared.hxvault import encrypt as _vault_encrypt
        return {"_enc": _vault_encrypt(tenant_id, plaintext, "connector_credentials")}
    # hxv3, never hxv1. Every synchronous caller — MFA secrets, SSO client
    # secrets, channel signing secrets, bot credentials, branch tokens — reaches
    # AES-256-GCM without changing its signature, which is the only reason this
    # could be fixed in one place instead of eleven.
    payload = base64.urlsafe_b64encode(_aesgcm_encrypt(_get_key_v3(), plaintext)).decode()
    return {"_enc": _PREFIX_V3 + payload}


def decrypt_credentials(stored: dict) -> dict:
    """Decrypt a stored credentials dict. Returns plain dict.

    Universal read path: hxv2 (HxVault per-tenant DEK), hxv1 (legacy single key),
    or a plain dict (dev/test). Tenant/context for hxv2 are self-describing in the
    envelope, so this needs no tenant argument — existing call sites are unchanged.
    """
    if not stored:
        return {}
    enc = stored.get("_enc", "")
    if not enc:
        return stored   # unencrypted (dev/test) — return as-is
    if enc.startswith("hxv2:"):
        from velaris_shared.hxvault import decrypt as _vault_decrypt
        return json.loads(_vault_decrypt(enc))
    if enc.startswith(_PREFIX_V3):
        blob = base64.urlsafe_b64decode(enc[len(_PREFIX_V3):].encode())
        return json.loads(_aesgcm_decrypt(_get_key_v3(), blob))
    if enc.startswith(_PREFIX):  # hxv1 legacy — read-only, never written again
        payload = base64.urlsafe_b64decode(enc[len(_PREFIX):].encode())
        nonce, data = payload[:16], payload[16:]
        return json.loads(_xor_decrypt(_get_key(), data, nonce))
    return stored   # unknown/plain — return as-is


def mask_credentials(creds: dict) -> dict:
    """Return a safe version of credentials for API responses — values replaced with ***."""
    if not creds:
        return {}
    if "_enc" in creds:
        return {"_enc": "***"}
    return {k: "***" for k in creds}


async def encrypt_credentials_vaulted(session, creds: dict, *, tenant_id) -> dict:
    """Per-tenant DEK where one can be had; the platform key where it cannot.

    hxv3 is sound, but its key is platform-wide and derived from `auth_secret`:
    every tenant's secrets share one fate, and none of them can be
    crypto-shredded. hxv2 fixes that, and the only reason most callers were not
    using it is that it needs an awaited `ensure_dek` first — which every one of
    them was in a position to do.

    Falls back to hxv3 rather than failing, and rather than storing plaintext:
    a tenant that has no resolvable DEK (an unknown slug, a database blip) must
    still get an encrypted secret. The fallback is logged, so "why is this row
    hxv3" has an answer.

    Both formats decrypt through `decrypt_credentials` with no caller change,
    so a mixed table is not a problem to be migrated away — it is the expected
    steady state.
    """
    import logging

    log = logging.getLogger(__name__)

    resolved = None
    if tenant_id is not None:
        try:
            from velaris_shared.hxvault.keyring import ensure_dek
            from velaris_shared.tenancy.identity import resolve_tenant

            resolved = await resolve_tenant(session, tenant_id)
            if resolved is not None:
                await ensure_dek(session, resolved[0])
                return encrypt_credentials(creds, tenant_id=resolved[0], vault=True)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "HxVault unavailable for tenant %s (%s) — storing under the "
                "platform key instead", tenant_id, exc,
            )
    return encrypt_credentials(creds)
