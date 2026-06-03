"""Shared async Redis client — singleton, lazy-initialized."""
from __future__ import annotations
import asyncio
import logging
from typing import Optional

try:
    from redis.asyncio import Redis, from_url
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    Redis = None  # type: ignore
    from_url = None  # type: ignore

log = logging.getLogger(__name__)

_client: Optional["Redis"] = None
_lock = asyncio.Lock()


async def get_redis() -> Optional["Redis"]:
    """Return the singleton async Redis client, or None if disabled/unavailable."""
    global _client
    if not REDIS_AVAILABLE:
        return None
    if _client is not None:
        return _client
    async with _lock:
        if _client is not None:
            return _client
        from case_service.config import get_settings
        s = get_settings()
        if not getattr(s, "redis_enabled", False):
            return None
        try:
            _client = from_url(
                s.redis_url,
                encoding="utf-8", decode_responses=True,
                socket_timeout=3.0, socket_connect_timeout=3.0,
            )
            await _client.ping()
            log.info("Redis connected: %s", s.redis_url)
        except Exception as e:
            log.warning("Redis connect failed: %s — falling back to in-process", e)
            _client = None
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        except Exception:
            pass
        _client = None


def reset_redis_client() -> None:
    """Test helper."""
    global _client
    _client = None
