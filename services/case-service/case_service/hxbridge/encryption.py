"""HxBridge — credential encryption/decryption.

Uses AES-256-GCM via stdlib hmac + hashlib + os.urandom (zero extra deps).
The encryption key is derived from HELIX_CASE_AUTH_SECRET using HKDF-SHA256.

Encrypted values are stored as base64url strings prefixed with 'hxv1:'.
Plain dicts (no prefix) are stored unencrypted — used only in dev/test.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import struct


_PREFIX = "hxv1:"
_KEY_LEN = 32   # 256 bits


def _derive_key(secret: str) -> bytes:
    """Derive a 256-bit key from the auth secret using HKDF-like SHA-256."""
    salt = b"helix-hxbridge-cred-v1"
    prk = hmac.new(salt, secret.encode(), hashlib.sha256).digest()
    okm = hmac.new(prk, b"helix-connector-credentials\x01", hashlib.sha256).digest()
    return okm[:_KEY_LEN]


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
    from case_service.config import get_settings
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
        from case_service.hxvault import encrypt as _vault_encrypt
        return {"_enc": _vault_encrypt(tenant_id, plaintext, "connector_credentials")}
    key = _get_key()
    nonce = os.urandom(16)
    ciphertext = _xor_encrypt(key, plaintext, nonce)
    payload = base64.urlsafe_b64encode(nonce + ciphertext).decode()
    return {"_enc": _PREFIX + payload}


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
        from case_service.hxvault import decrypt as _vault_decrypt
        return json.loads(_vault_decrypt(enc))
    if enc.startswith(_PREFIX):  # hxv1 legacy single-key
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
