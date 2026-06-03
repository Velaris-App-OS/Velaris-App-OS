"""Backend factory — selects and optionally encrypts the storage backend."""
from __future__ import annotations

import logging
from functools import lru_cache

from .backend import StorageBackend

logger = logging.getLogger(__name__)


@lru_cache
def get_storage_backend() -> StorageBackend:
    """Return a singleton storage backend based on case-service settings.

    If HELIX_CASE_STORAGE_MASTER_KEY is set, the backend is wrapped with
    transparent AES-256-GCM encryption — every file is encrypted before being
    written and decrypted after being read, regardless of backend type.
    """
    from case_service.config import get_settings
    s = get_settings()
    mode = getattr(s, "storage_backend", "local").lower()

    # ── Select raw backend ────────────────────────────────────────────────────
    if mode == "minio":
        from .minio_backend import MinIOBackend
        raw: StorageBackend = MinIOBackend(
            endpoint=s.minio_endpoint,
            access_key=s.minio_access_key,
            secret_key=s.minio_secret_key,
            bucket=s.minio_bucket,
            secure=s.minio_secure,
        )
        logger.info("Storage backend: MinIO (%s / %s)", s.minio_endpoint, s.minio_bucket)
    else:
        from .local_fs import LocalFSBackend
        base = getattr(s, "storage_local_path", "/tmp/helix-docs")
        raw = LocalFSBackend(base_path=base)
        logger.info("Storage backend: local filesystem (%s)", base)

    # ── Wrap with encryption if master key is configured ──────────────────────
    master_key = getattr(s, "storage_master_key", "").strip()
    if master_key:
        from .encrypted_backend import EncryptedBackend
        logger.info(
            "Storage encryption: AES-256-GCM enabled — "
            "files are encrypted at rest before reaching %s", mode
        )
        return EncryptedBackend(raw, master_key)

    logger.warning(
        "Storage encryption is DISABLED. Set HELIX_CASE_STORAGE_MASTER_KEY "
        "(openssl rand -hex 32) to enable AES-256-GCM at-rest encryption."
    )
    return raw


def reset_storage_backend() -> None:
    """Test helper — clears cached backend so tests can swap backends."""
    get_storage_backend.cache_clear()
