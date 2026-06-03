"""Local filesystem storage backend — used in tests and dev-minimal mode."""
from __future__ import annotations
import asyncio
import os
from pathlib import Path

from .backend import StorageBackend, ObjectNotFound, StorageError


class LocalFSBackend:
    """Stores objects as files under a base directory.

    Key can contain ``/`` — subdirectories are created as needed.
    """

    def __init__(self, base_path: str | Path):
        self.base = Path(base_path)
        self.base.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # Reject path-traversal
        if ".." in Path(key).parts:
            raise StorageError(f"Invalid key: {key}")
        return self.base / key

    async def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(p.write_bytes, data)

    async def get(self, key: str) -> bytes:
        p = self._path(key)
        if not p.exists():
            raise ObjectNotFound(key)
        return await asyncio.to_thread(p.read_bytes)

    async def delete(self, key: str) -> None:
        p = self._path(key)
        if p.exists():
            await asyncio.to_thread(p.unlink)

    async def exists(self, key: str) -> bool:
        return self._path(key).exists()

    async def size(self, key: str) -> int:
        p = self._path(key)
        if not p.exists():
            raise ObjectNotFound(key)
        return p.stat().st_size
