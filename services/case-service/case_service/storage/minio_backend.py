"""MinIO (S3-compatible) storage backend."""
from __future__ import annotations
import asyncio
import io
from typing import Any

from .backend import StorageBackend, ObjectNotFound, StorageError


class MinIOBackend:
    """MinIO backend using the minio-py SDK, wrapped for async via to_thread."""

    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        secure: bool = False,
    ):
        try:
            from minio import Minio  # noqa: F401
        except ImportError as e:
            raise StorageError(f"minio package required: {e}")
        from minio import Minio
        self._client = Minio(
            endpoint, access_key=access_key, secret_key=secret_key, secure=secure,
        )
        self.bucket = bucket
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        try:
            if not self._client.bucket_exists(self.bucket):
                self._client.make_bucket(self.bucket)
        except Exception as e:
            raise StorageError(f"bucket init failed: {e}")

    async def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        def _put():
            self._client.put_object(
                self.bucket, key,
                data=io.BytesIO(data),
                length=len(data),
                content_type=content_type,
            )
        try:
            await asyncio.to_thread(_put)
        except Exception as e:
            raise StorageError(f"put failed: {e}")

    async def get(self, key: str) -> bytes:
        def _get():
            from minio.error import S3Error
            try:
                resp = self._client.get_object(self.bucket, key)
                try:
                    return resp.read()
                finally:
                    resp.close()
                    resp.release_conn()
            except S3Error as e:
                if e.code in ("NoSuchKey", "NoSuchObject"):
                    raise ObjectNotFound(key) from e
                raise StorageError(str(e)) from e
        return await asyncio.to_thread(_get)

    async def delete(self, key: str) -> None:
        def _del():
            self._client.remove_object(self.bucket, key)
        try:
            await asyncio.to_thread(_del)
        except Exception as e:
            raise StorageError(f"delete failed: {e}")

    async def exists(self, key: str) -> bool:
        def _stat():
            from minio.error import S3Error
            try:
                self._client.stat_object(self.bucket, key)
                return True
            except S3Error:
                return False
        return await asyncio.to_thread(_stat)

    async def size(self, key: str) -> int:
        def _stat():
            from minio.error import S3Error
            try:
                info = self._client.stat_object(self.bucket, key)
                return info.size
            except S3Error as e:
                if e.code in ("NoSuchKey", "NoSuchObject"):
                    raise ObjectNotFound(key) from e
                raise StorageError(str(e)) from e
        return await asyncio.to_thread(_stat)
