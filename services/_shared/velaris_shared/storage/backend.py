"""Storage backend protocol."""
from __future__ import annotations
from typing import Protocol, BinaryIO


class StorageError(Exception):
    """Base exception for storage operations."""


class ObjectNotFound(StorageError):
    """Raised when a requested object key is missing."""


class StorageBackend(Protocol):
    """Abstract object storage. All backends must be awaitable."""

    async def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None: ...
    async def get(self, key: str) -> bytes: ...
    async def delete(self, key: str) -> None: ...
    async def exists(self, key: str) -> bool: ...
    async def size(self, key: str) -> int: ...
