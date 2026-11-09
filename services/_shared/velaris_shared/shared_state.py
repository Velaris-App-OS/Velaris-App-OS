"""Short-lived state every copy of a service must see the same way (R5e step 5).

One-time codes, re-auth windows, previews, attempt counters and live fan-out
lived in module dicts: correct while ONE process serves the platform, wrong as
soon as there are two — a code issued by one copy is unknown to the next, and N
copies allow N times every limit.

    put / get / take / delete     a value with a time-to-live (take = single use)
    allow(ns, key, limit, window) a sliding-window limit: record + decide, atomic
    hits(ns, key, window)         record an event, return how many in the window
    publish / listen              fan-out to every process

**The cache off (VELARIS_CASE_REDIS_ENABLED unset — every existing install):**
the in-process store below, which is exactly what each caller did before.
**The cache on:** one store in Valkey for every copy. If Valkey stops answering
mid-run, calls fall back to the in-process store (a per-process limit, never
no limit; an unknown code, never an accepted one) and Valkey is retried after
RETRY_SECONDS; each transition is logged once.

Keys are namespaced and hashed: no address, user id or code is stored in clear
in Valkey. Values are whatever the caller passes — callers store hashes, never
secrets.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Callable
from typing import Any

log = logging.getLogger(__name__)

PREFIX = "velaris:ss:"
RETRY_SECONDS = 30.0
INSTANCE = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"

# ── the in-process store (the cache off, or unreachable) ───────────────────────

_kv: dict[str, tuple[str, float]] = {}
_windows: dict[str, deque] = {}
# No lock: nothing below awaits between reading and writing these dicts, so
# under asyncio each operation is already atomic within the process.


def _now() -> float:
    return time.monotonic()


def _sweep() -> None:
    if len(_kv) > 10_000:
        now = _now()
        for k in [k for k, (_, exp) in _kv.items() if exp <= now]:
            _kv.pop(k, None)


# ── Valkey, with a backoff when it stops answering ─────────────────────────────

_down_until = 0.0
_down_logged = False


async def _valkey():
    global _down_until
    if _now() < _down_until:
        return None
    try:
        from velaris_shared.config import get_settings
        if not getattr(get_settings(), "redis_enabled", False):
            return None              # the cache is off: the normal case, said nowhere
        from velaris_shared.redis_client import get_redis
        r = await get_redis()
    except Exception:  # noqa: BLE001
        r = None
    if r is None:
        _mark_down("not reachable")
    return r


def _mark_down(why: Any) -> None:
    global _down_until, _down_logged
    _down_until = _now() + RETRY_SECONDS
    if not _down_logged:
        log.warning("shared state: cache unavailable (%s) — using this process's store "
                    "until it answers again", why)
        _down_logged = True


def _mark_up() -> None:
    global _down_logged
    if _down_logged:
        log.info("shared state: cache answering again")
        _down_logged = False


def _k(ns: str, key: str) -> str:
    return f"{PREFIX}{ns}:{hashlib.sha256(key.encode()).hexdigest()[:40]}"


async def _call(op: Callable[[Any], Any]):
    """(True, result) from Valkey, or (False, None) to use the in-process store."""
    r = await _valkey()
    if r is None:
        return False, None
    try:
        out = await op(r)
    except Exception as exc:  # noqa: BLE001
        _mark_down(type(exc).__name__)
        return False, None
    _mark_up()
    return True, out


# ── key / value ────────────────────────────────────────────────────────────────

async def put(ns: str, key: str, value: str, ttl: float) -> None:
    k = _k(ns, key)
    ok, _ = await _call(lambda r: r.set(k, value, px=max(1, int(ttl * 1000))))
    if not ok:
        _sweep()
        _kv[k] = (value, _now() + ttl)


async def get(ns: str, key: str) -> str | None:
    k = _k(ns, key)
    ok, out = await _call(lambda r: r.get(k))
    if ok:
        return out
    hit = _kv.get(k)
    if hit and hit[1] > _now():
        return hit[0]
    _kv.pop(k, None)
    return None


async def take(ns: str, key: str) -> str | None:
    """Read and delete in one step: a value can be used once, by one copy."""
    k = _k(ns, key)
    ok, out = await _call(lambda r: r.getdel(k))
    if ok:
        return out
    hit = _kv.pop(k, None)
    return hit[0] if hit and hit[1] > _now() else None


async def delete(ns: str, key: str) -> None:
    k = _k(ns, key)
    ok, _ = await _call(lambda r: r.delete(k))
    if not ok:
        _kv.pop(k, None)


# ── sliding windows ────────────────────────────────────────────────────────────

# KEYS[1] window; ARGV now_ms, window_ms, limit (0 = record always, -1 = never:
# just count), member.
# Returns the count AFTER this call, and 1/0 for whether it was recorded.
_LUA_WINDOW = """
local k = KEYS[1]
local now = tonumber(ARGV[1]); local win = tonumber(ARGV[2]); local lim = tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', k, '-inf', now - win)
local n = redis.call('ZCARD', k)
if lim < 0 or (lim > 0 and n >= lim) then return {n, 0} end
redis.call('ZADD', k, now, ARGV[4])
redis.call('PEXPIRE', k, win)
return {n + 1, 1}
"""
_sha: str | None = None


async def _window_shared(ns: str, key: str, window: float, limit: int) -> tuple[int, bool] | None:
    """The window in Valkey, or None when the cache is off or not answering."""
    global _sha
    k = _k(ns, key)
    now_ms = int(time.time() * 1000)
    member = f"{now_ms}-{uuid.uuid4().hex[:8]}"

    async def op(r):
        global _sha
        if _sha is None:
            _sha = await r.script_load(_LUA_WINDOW)
        try:
            return await r.evalsha(_sha, 1, k, now_ms, int(window * 1000), limit, member)
        except Exception as exc:  # noqa: BLE001
            if "NOSCRIPT" in str(exc):   # the cache restarted and lost its scripts
                _sha = await r.script_load(_LUA_WINDOW)
                return await r.evalsha(_sha, 1, k, now_ms, int(window * 1000), limit, member)
            raise

    ok, out = await _call(op)
    return (int(out[0]), bool(int(out[1]))) if ok else None


async def _window(ns: str, key: str, window: float, limit: int) -> tuple[int, bool]:
    shared = await _window_shared(ns, key, window, limit)
    if shared is not None:
        return shared
    k = _k(ns, key)
    now = _now()
    q = _windows.setdefault(k, deque())
    while q and now - q[0] >= window:
        q.popleft()
    if limit < 0 or (limit > 0 and len(q) >= limit):
        return len(q), False
    q.append(now)
    if len(_windows) > 10_000:
        for kk in [kk for kk, d in _windows.items() if not d]:
            _windows.pop(kk, None)
    return len(q), True


async def allow(ns: str, key: str, limit: int, window: float) -> bool:
    """True and counted if under `limit` calls in the last `window` seconds."""
    return (await _window(ns, key, window, limit))[1]


async def allow_shared(ns: str, key: str, limit: int, window: float) -> bool | None:
    """Like `allow`, but None when the cache is off or not answering — for a
    caller that keeps its own in-process limiter for that case."""
    out = await _window_shared(ns, key, window, limit)
    return None if out is None else out[1]


async def hits(ns: str, key: str, window: float, *, record: bool = True) -> int:
    """Record one event (unless record=False); return how many happened in the
    last `window` seconds."""
    return (await _window(ns, key, window, 0 if record else -1))[0]


# ── fan-out ────────────────────────────────────────────────────────────────────

async def publish(channel: str, message: dict) -> bool:
    """Send to every process listening on `channel`. False when the cache is off
    or unreachable — the caller has already delivered locally, as before."""
    payload = json.dumps({"origin": INSTANCE, "message": message}, default=str)
    ok, _ = await _call(lambda r: r.publish(PREFIX + "ch:" + channel, payload))
    return ok


async def listen(channel: str) -> AsyncIterator[dict]:
    """Messages published by OTHER processes on `channel` (own ones are skipped —
    the publisher delivers those locally). Re-subscribes after a cache outage;
    yields nothing while the cache is off."""
    while True:
        r = await _valkey()
        if r is None:
            await asyncio.sleep(RETRY_SECONDS)
            continue
        pubsub = r.pubsub()
        try:
            await pubsub.subscribe(PREFIX + "ch:" + channel)
            _mark_up()
            while True:
                # A quiet channel is normal. `pubsub.listen()` would read with the
                # client's 3 s socket timeout and raise on every quiet spell — and
                # treating that as an outage switched the WHOLE shared store to
                # per-process for 30 s at a time (found under load, 2026-10-17).
                # get_message waits with its own timeout and returns None instead.
                item = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if item is None or item.get("type") != "message":
                    continue
                try:
                    body = json.loads(item["data"])
                except Exception:  # noqa: BLE001
                    continue
                if body.get("origin") != INSTANCE:
                    yield body.get("message") or {}
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — the subscription itself broke
            log.warning("shared state: subscription to %s lost (%s) — re-subscribing",
                        channel, type(exc).__name__)
        finally:
            try:
                await pubsub.aclose()
            except Exception:  # noqa: BLE001
                pass
        await asyncio.sleep(1)


def reset_for_tests() -> None:
    global _down_until, _down_logged, _sha
    _kv.clear()
    _windows.clear()
    _down_until = 0.0
    _down_logged = False
    _sha = None
