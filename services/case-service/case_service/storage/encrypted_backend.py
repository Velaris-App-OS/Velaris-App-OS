"""Transparent AES-256-GCM encryption wrapper for any storage backend.

Wire-up (handled automatically by factory.py when HELIX_CASE_STORAGE_MASTER_KEY is set):

    backend = EncryptedBackend(inner_backend, master_key_hex)

On write — for every file stored:
    1. Generate 12 random bytes (nonce).
    2. Encrypt content with AES-256-GCM(master_key, nonce).
    3. Write  [MAGIC(4)] + [nonce(12)] + [ciphertext+tag(N+16)]  to the backend.

On read:
    1. Check MAGIC header — if absent the file was stored unencrypted (backwards compat).
    2. Split nonce from ciphertext.
    3. Decrypt and return plaintext.

Security properties:
    - Each file has a unique nonce → identical files produce different ciphertext.
    - AES-256-GCM is authenticated → any tampering is detected on read.
    - The master key is the only secret; rotate it by re-encrypting all files (HxVault later).
    - Admins with direct filesystem/MinIO access see only ciphertext.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import os

from .backend import StorageBackend, ObjectNotFound, StorageError

# 4-byte magic prefix so we can detect encrypted vs legacy unencrypted files
_MAGIC = b"HXE1"  # Helix Encrypted v1
_NONCE_LEN = 12   # 96-bit GCM nonce — NIST recommended
_TAG_LEN = 16     # 128-bit GCM authentication tag (included in ciphertext by cryptography lib)
_HEADER_LEN = len(_MAGIC) + _NONCE_LEN  # 16 bytes total header


def _validate_key(key_bytes: bytes) -> None:
    if len(key_bytes) != 32:
        raise StorageError(
            f"storage_master_key must be 32 bytes (64 hex chars). Got {len(key_bytes)} bytes. "
            "Generate one with: openssl rand -hex 32"
        )


def _encrypt_bytes(key: bytes, nonce: bytes, plaintext: bytes) -> bytes:
    """Encrypt with AES-256-GCM. Returns ciphertext+tag (16-byte tag appended)."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        return AESGCM(key).encrypt(nonce, plaintext, associated_data=None)
    except ImportError:
        pass
    try:
        from Crypto.Cipher import AES  # pycryptodome
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        ct, tag = cipher.encrypt_and_digest(plaintext)
        return ct + tag
    except ImportError:
        pass
    raise StorageError(
        "At-rest encryption requires either 'cryptography' or 'pycryptodome'. "
        "Run `uv sync` to install them (both are in pyproject.toml)."
    )


def _decrypt_bytes(key: bytes, nonce: bytes, ciphertext_with_tag: bytes) -> bytes:
    """Decrypt AES-256-GCM ciphertext (last 16 bytes are the tag)."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        return AESGCM(key).decrypt(nonce, ciphertext_with_tag, associated_data=None)
    except ImportError:
        pass
    try:
        from Crypto.Cipher import AES  # pycryptodome
        ct, tag = ciphertext_with_tag[:-_TAG_LEN], ciphertext_with_tag[-_TAG_LEN:]
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        return cipher.decrypt_and_verify(ct, tag)
    except ImportError:
        pass
    raise StorageError(
        "At-rest encryption requires either 'cryptography' or 'pycryptodome'. "
        "Run `uv sync` to install them."
    )


class EncryptedBackend:
    """Wraps any StorageBackend with transparent AES-256-GCM encryption.

    Unencrypted files written before encryption was enabled are returned as-is
    (detected by the absence of the HXE1 magic header) so rollout is safe.
    """

    def __init__(self, inner: StorageBackend, master_key_hex: str):
        self._inner = inner
        try:
            self._key = bytes.fromhex(master_key_hex)
        except ValueError:
            raise StorageError(
                "storage_master_key must be a valid hex string. "
                "Generate one with: openssl rand -hex 32"
            )
        _validate_key(self._key)

    # ── Encryption / decryption ───────────────────────────────────────────────

    def _encrypt(self, plaintext: bytes) -> bytes:
        nonce = os.urandom(_NONCE_LEN)
        ciphertext = _encrypt_bytes(self._key, nonce, plaintext)
        return _MAGIC + nonce + ciphertext

    def _decrypt(self, raw: bytes) -> bytes:
        if not raw.startswith(_MAGIC):
            return raw  # pre-encryption legacy file — return as-is
        nonce = raw[len(_MAGIC): _HEADER_LEN]
        ciphertext = raw[_HEADER_LEN:]
        try:
            return _decrypt_bytes(self._key, nonce, ciphertext)
        except Exception as exc:
            raise StorageError(
                "Document decryption failed — wrong master key or corrupted file."
            ) from exc

    # ── StorageBackend interface ──────────────────────────────────────────────

    async def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        await self._inner.put(key, self._encrypt(data), content_type=content_type)

    async def get(self, key: str) -> bytes:
        raw = await self._inner.get(key)
        return self._decrypt(raw)

    async def delete(self, key: str) -> None:
        await self._inner.delete(key)

    async def exists(self, key: str) -> bool:
        return await self._inner.exists(key)

    async def size(self, key: str) -> int:
        # Report logical (pre-encryption) size where possible; fall back to raw
        try:
            raw = await self._inner.get(key)
            return len(self._decrypt(raw))
        except (ObjectNotFound, StorageError):
            return await self._inner.size(key)
