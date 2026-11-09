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


def _safe_url(url: str) -> str:
    """The URL without any password it carries, for logs."""
    from urllib.parse import urlsplit, urlunsplit
    try:
        p = urlsplit(url)
        if p.password is None:
            return url
        host = p.hostname or ""
        netloc = f"{p.username or ''}:***@{host}" + (f":{p.port}" if p.port else "")
        return urlunsplit((p.scheme, netloc, p.path, p.query, p.fragment))
    except Exception:
        return "<unparseable redis url>"


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
        from velaris_shared.config import get_settings
        s = get_settings()
        if not getattr(s, "redis_enabled", False):
            return None
        try:
            kw = {}
            if getattr(s, "redis_password", ""):
                kw["password"] = s.redis_password
            _client = from_url(
                s.redis_url,
                encoding="utf-8", decode_responses=True,
                socket_timeout=3.0, socket_connect_timeout=3.0, **kw,
            )
            await _client.ping()
            log.info("Redis connected: %s", _safe_url(s.redis_url))
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
