"""Backend factory — selects and optionally encrypts the storage backend."""
from __future__ import annotations

import logging
from functools import lru_cache

from .backend import StorageBackend

logger = logging.getLogger(__name__)


@lru_cache
def get_storage_backend() -> StorageBackend:
    """Return a singleton storage backend based on case-service settings.

    If VELARIS_CASE_STORAGE_MASTER_KEY is set, the backend is wrapped with
    transparent AES-256-GCM encryption — every file is encrypted before being
    written and decrypted after being read, regardless of backend type.
    """
    from velaris_shared.config import get_settings
    s = get_settings()
    mode = getattr(s, "storage_backend", "local").lower()

    # ── Select raw backend ────────────────────────────────────────────────────
    # "s3" and "minio" are one backend: an S3 client pointed at the endpoint in
    # VELARIS_CASE_S3_* (or the older VELARIS_CASE_MINIO_* names). "minio" is the name every install before v3.1.2
    # wrote into .env and stays valid; "s3" is the name for an endpoint that is
    # not MinIO (SeaweedFS, Garage, Ceph RGW, a hosted S3).
    if mode in ("minio", "s3"):
        from .s3_backend import S3Backend
        raw: StorageBackend = S3Backend(
            endpoint=s.s3_endpoint,
            access_key=s.s3_access_key,
            secret_key=s.s3_secret_key,
            bucket=s.s3_bucket,
            secure=s.s3_secure,
        )
        logger.info("Storage backend: S3 (%s / %s)", s.s3_endpoint, s.s3_bucket)
        # `s3_secure` defaults to False because the endpoint used to be a
        # container on this machine. Pointed anywhere else, that default sends
        # every document over plain HTTP. Warn, never change the default: an
        # existing install on localhost:9000 depends on it.
        _host = str(s.s3_endpoint).rsplit(":", 1)[0].strip("[]").lower()
        if not s.s3_secure and _host not in ("localhost", "127.0.0.1", "::1", "minio"):
            logger.warning(
                "SECURITY WARNING: document storage endpoint %s is not on this machine "
                "and VELARIS_CASE_S3_SECURE is false — documents travel unencrypted "
                "over the network. Set VELARIS_CASE_S3_SECURE=true.", s.s3_endpoint,
            )
    else:
        from .local_fs import LocalFSBackend
        base = getattr(s, "storage_local_path", "/tmp/velaris-docs")
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
        "Storage encryption is DISABLED. Set VELARIS_CASE_STORAGE_MASTER_KEY "
        "(openssl rand -hex 32) to enable AES-256-GCM at-rest encryption."
    )
    return raw


def bundled_minio_in_use(settings) -> bool:
    """True when documents live in the MinIO container bundled before v3.1.2.

    Recognised by the S3 backend being selected, the endpoint being on this
    machine, and the container's own password being present. An S3 endpoint the
    operator supplied — even one on localhost with its own secret and no bundled
    password — is not this.
    """
    if (getattr(settings, "storage_backend", "") or "local").lower() not in ("minio", "s3"):
        return False
    host = str(settings.s3_endpoint).rsplit(":", 1)[0].strip("[]").lower()
    return bool(settings.bundled_minio_password) and host in ("localhost", "127.0.0.1", "::1", "minio")


def reset_storage_backend() -> None:
    """Test helper — clears cached backend so tests can swap backends."""
    get_storage_backend.cache_clear()
