"""Velaris storage — pluggable document storage (local disk / S3)."""
from .backend import StorageBackend, StorageError, ObjectNotFound
from .local_fs import LocalFSBackend
from .s3_backend import S3Backend
from .minio_backend import MinIOBackend  # the pre-3.1.2 name, kept importable
from .factory import get_storage_backend

__all__ = [
    "StorageBackend", "StorageError", "ObjectNotFound",
    "LocalFSBackend", "S3Backend", "MinIOBackend", "get_storage_backend",
]
