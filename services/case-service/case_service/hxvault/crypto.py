"""HxVault (#19) — low-level AES-256-GCM primitive + master KEK resolution.

The single crypto chokepoint. Real AEAD via `cryptography` (no homebrew).
Key material is never logged.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)

KEY_LEN = 32   # 256-bit
_NONCE_LEN = 12  # GCM standard

_kek_cache: bytes | None = None


def _decode_key_material(raw: str) -> bytes | None:
    """Accept a 32-byte key as hex (64 chars) or base64. Returns None if unusable."""
    raw = raw.strip()
    if not raw:
        return None
    # hex first (most explicit)
    try:
        b = binascii.unhexlify(raw)
        if len(b) == KEY_LEN:
            return b
    except (binascii.Error, ValueError):
        pass
    # base64 / base64url
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            b = decoder(raw + "=" * (-len(raw) % 4))
            if len(b) == KEY_LEN:
                return b
        except (binascii.Error, ValueError):
            continue
    return None


def get_kek() -> bytes:
    """Resolve the master Key-Encryption-Key.

    Prefers VELARIS_CASE_KEK (32 bytes, hex or base64). When unset, derives a
    deterministic dev KEK from auth_secret so dev/test round-trips work — crypto
    shredding is still real (DEKs are random+stored; the KEK only wraps them).
    """
    global _kek_cache  # noqa: PLW0603
    if _kek_cache is not None:
        return _kek_cache

    from case_service.config import get_settings
    s = get_settings()

    key = _decode_key_material(s.kek) if s.kek else None
    if key is None:
        if s.kek:
            logger.warning(
                "VELARIS_CASE_KEK is set but not a valid 32-byte hex/base64 key — "
                "falling back to the dev KEK. Fix it before production."
            )
        else:
            logger.warning(
                "VELARIS_CASE_KEK not set — deriving a DEV KEK from auth_secret. "
                "Set VELARIS_CASE_KEK (32 bytes) before deploying to production."
            )
        key = hashlib.sha256(b"velaris-hxvault-dev-kek-v1|" + s.auth_secret.encode()).digest()

    _kek_cache = key
    return key


def reset_kek_cache() -> None:
    """Test hook — force re-resolution of the KEK (e.g. after changing settings)."""
    global _kek_cache  # noqa: PLW0603
    _kek_cache = None


def seal(key: bytes, plaintext: bytes, aad: bytes) -> bytes:
    """AES-256-GCM encrypt. Returns nonce(12) || ciphertext || tag(16)."""
    if len(key) != KEY_LEN:
        raise ValueError("key must be 32 bytes")
    nonce = os.urandom(_NONCE_LEN)
    ct = AESGCM(key).encrypt(nonce, plaintext, aad)
    return nonce + ct


def open_(key: bytes, blob: bytes, aad: bytes) -> bytes:
    """AES-256-GCM decrypt of seal() output. Raises on tamper/wrong key/wrong AAD."""
    if len(key) != KEY_LEN:
        raise ValueError("key must be 32 bytes")
    nonce, ct = blob[:_NONCE_LEN], blob[_NONCE_LEN:]
    return AESGCM(key).decrypt(nonce, ct, aad)
