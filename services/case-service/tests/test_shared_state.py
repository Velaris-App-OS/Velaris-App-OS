"""Shared state across processes (Nov_TODO R5e step 5).

Two mechanisms, and the property each must keep:

* `velaris_shared.generations` — a change bumped in one process is seen by the
  others' poll and their caches reload; a baseline read is NOT a change; a bump
  never fails the change it rides on.
* `velaris_shared.shared_state` — cache OFF behaves exactly as the old per-process
  dicts did (single-use, TTL, sliding windows); cache ON is one store for every
  copy; cache UNREACHABLE falls back to per-process, never to "no limit" and
  never to "accepted".

The cross-process cases with a real Valkey are proven separately with two OS
processes (see the Nov_TODO entry); here the Valkey path runs against the
dev box's cache when it answers, and is skipped otherwise.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import socket

import pytest

from velaris_shared import generations, shared_state
from velaris_shared.config import get_settings

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clean():
    shared_state.reset_for_tests()
    generations._seen.clear()
    yield
    shared_state.reset_for_tests()
    generations._seen.clear()


@pytest.fixture
def cache_off(monkeypatch):
    monkeypatch.delenv("VELARIS_CASE_REDIS_ENABLED", raising=False)
    monkeypatch.setenv("VELARIS_CASE_REDIS_ENABLED", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _valkey_up() -> bool:
    try:
        socket.create_connection(("127.0.0.1", 6379), timeout=0.5).close()
        return True
    except OSError:
        return False


@pytest.fixture
async def cache_on(monkeypatch):
    if not _valkey_up():
        pytest.skip("no Valkey on 127.0.0.1:6379")
    from velaris_shared import redis_client
    monkeypatch.setenv("VELARIS_CASE_REDIS_ENABLED", "true")
    monkeypatch.setenv("VELARIS_CASE_REDIS_URL", "redis://127.0.0.1:6379/15")
    monkeypatch.setenv("VELARIS_CASE_REDIS_PASSWORD", "")
    get_settings.cache_clear()
    redis_client.reset_redis_client()
    r = await redis_client.get_redis()
    if r is None:
        pytest.skip("Valkey refused the test connection (password set?)")
    yield r
    for k in await r.keys(shared_state.PREFIX + "*"):
        await r.delete(k)
    await redis_client.close_redis()
    get_settings.cache_clear()


async def _publish_as_other_process(r, channel: str, message: dict) -> None:
    """What another process's `publish` puts on the wire (its own origin)."""
    import json
    await r.publish(shared_state.PREFIX + "ch:" + channel,
                    json.dumps({"origin": "other-process", "message": message}))


async def _until_subscribed(r, channel: str) -> None:
    """Valkey keeps nothing for late subscribers: publish only once one exists."""
    for _ in range(100):
        n = dict(await r.pubsub_numsub(shared_state.PREFIX + "ch:" + channel))
        if n.get(shared_state.PREFIX + "ch:" + channel, 0):
            return
        await asyncio.sleep(0.05)
    raise AssertionError("listener never subscribed")


# ── generations ───────────────────────────────────────────────────────────────

class TestGenerations:
    async def test_bump_seen_by_another_process(self, session):
        seen: list[str] = []

        async def reload(_s):
            seen.append("reloaded")

        generations.on_change("flags", reload)
        try:
            assert await generations.poll_once(session) == []      # baseline, not a change
            await generations.bump(session, "flags", reason="test")  # row missing → inserted
            await session.commit()
            assert await generations.poll_once(session) == ["flags"]
            assert seen == ["reloaded"]
            assert await generations.poll_once(session) == []      # nothing moved since
            await generations.bump(session, "flags")
            await session.commit()
            assert await generations.poll_once(session) == ["flags"]
            assert seen == ["reloaded", "reloaded"]
        finally:
            generations._handlers["flags"].remove(reload)

    async def test_one_failing_reload_does_not_stop_the_others(self, session):
        ran: list[str] = []

        async def bad(_s):
            raise RuntimeError("boom")

        async def good(_s):
            ran.append("good")

        generations.on_change("tenants", bad)
        generations.on_change("tenants", good)
        try:
            await generations.poll_once(session)
            await generations.bump(session, "tenants")
            await session.commit()
            await generations.poll_once(session)
            assert ran == ["good"]
        finally:
            generations._handlers["tenants"].remove(bad)
            generations._handlers["tenants"].remove(good)

    async def test_unknown_topic_refused(self, session):
        with pytest.raises(ValueError):
            await generations.bump(session, "nope")

    async def test_every_cache_is_registered(self):
        import case_service.api.routers.releases  # noqa: F401
        import case_service.hxvault.keyring  # noqa: F401
        import velaris_shared.auth.dependencies  # noqa: F401
        import velaris_shared.tenancy.identity  # noqa: F401
        assert {t for t, h in generations._handlers.items() if h} >= generations.TOPICS

    async def test_logout_is_refused_at_once_in_this_process(self):
        import hashlib

        from velaris_shared.auth import dependencies as d
        tok = "header.payload.sig"
        d.note_revoked(hashlib.sha256(tok.encode()).hexdigest())
        assert d._is_token_revoked_cached(tok)

    async def test_tenant_change_clears_lookups(self, session):
        from velaris_shared.tenancy import identity
        identity._cache["acme"] = (1e18, None)        # "no such tenant", cached
        identity._actor_cache["bob"] = (1e18, None)
        await identity.tenants_changed(session, reason="test")
        assert identity._cache == {} and identity._actor_cache == {}


# ── shared state, cache off (= the old per-process behaviour) ──────────────────

class TestCacheOff:
    async def test_single_use_and_ttl(self, cache_off):
        await shared_state.put("t", "k", "v", 60)
        assert await shared_state.get("t", "k") == "v"
        assert await shared_state.take("t", "k") == "v"
        assert await shared_state.take("t", "k") is None
        await shared_state.put("t", "short", "v", 0.05)
        await asyncio.sleep(0.1)
        assert await shared_state.get("t", "short") is None

    async def test_window_limit(self, cache_off):
        assert all([await shared_state.allow("t", "ip", 3, 60) for _ in range(3)])
        assert not await shared_state.allow("t", "ip", 3, 60)
        assert await shared_state.allow("t", "other-ip", 3, 60)

    async def test_hits_count_without_recording(self, cache_off):
        assert await shared_state.hits("t", "u", 60) == 1
        assert await shared_state.hits("t", "u", 60, record=False) == 1
        assert await shared_state.hits("t", "u", 60) == 2

    async def test_allow_shared_says_none_when_off(self, cache_off):
        assert await shared_state.allow_shared("t", "ip", 3, 60) is None

    async def test_publish_is_a_no_op_when_off(self, cache_off):
        assert await shared_state.publish("c", {"a": 1}) is False


# ── shared state, cache on (dev box Valkey, db 15) ───────────────────────────────

class TestCacheOn:
    async def test_values_live_in_valkey_hashed(self, cache_on):
        await shared_state.put("portal-otp", "case-123", "deadbeef", 60)
        keys = await cache_on.keys(shared_state.PREFIX + "*")
        assert keys and all("case-123" not in k for k in keys)      # key hashed
        shared_state._kv.clear()                                     # not served locally
        assert await shared_state.take("portal-otp", "case-123") == "deadbeef"
        assert await shared_state.take("portal-otp", "case-123") is None

    async def test_window_shared(self, cache_on):
        for _ in range(3):
            assert await shared_state.allow("t", "ip", 3, 60)
        shared_state._windows.clear()                                # a "different process"
        assert not await shared_state.allow("t", "ip", 3, 60)
        assert await shared_state.allow_shared("t", "ip", 3, 60) is False

    async def test_pubsub_reaches_other_listeners_not_self(self, cache_on):
        got: list[dict] = []

        async def consume():
            async for m in shared_state.listen("chan"):
                got.append(m)
                return

        task = asyncio.create_task(consume())
        await _until_subscribed(cache_on, "chan")
        await shared_state.publish("chan", {"own": True})              # own: skipped
        await _publish_as_other_process(cache_on, "chan", {"x": 1})
        await asyncio.wait_for(task, 5)
        assert got == [{"x": 1}]


class TestCacheUnreachable:
    async def test_falls_back_to_this_process_never_to_no_limit(self, monkeypatch):
        from velaris_shared import redis_client
        monkeypatch.setenv("VELARIS_CASE_REDIS_ENABLED", "true")
        monkeypatch.setenv("VELARIS_CASE_REDIS_URL", "redis://127.0.0.1:1/0")   # nothing listens
        get_settings.cache_clear()
        redis_client.reset_redis_client()
        try:
            assert all([await shared_state.allow("t", "ip", 2, 60) for _ in range(2)])
            assert not await shared_state.allow("t", "ip", 2, 60)            # still limited
            assert await shared_state.take("portal-otp", "never-issued") is None
            assert await shared_state.allow_shared("t", "ip", 2, 60) is None  # caller falls back
        finally:
            redis_client.reset_redis_client()
            get_settings.cache_clear()


# ── the callers ─────────────────────────────────────────────────────────────────

class TestCallers:
    async def test_portal_otp_single_use_and_wrong_code(self, cache_off):
        from portal_service.api.routers.portal import _store_portal_otp, _verify_portal_otp
        await _store_portal_otp("c1", "123456")
        assert not await _verify_portal_otp("c1", "000000")
        assert await _verify_portal_otp("c1", "123456")
        assert not await _verify_portal_otp("c1", "123456")       # used up

    async def test_endpoint_limiter_off_is_exactly_the_old_limiter(self, cache_off):
        from velaris_shared.middleware.endpoint_rate_limit import SlidingWindowLimiter
        lim = SlidingWindowLimiter(2, 60, "t")
        assert (await lim.allow_async("ip"))[0] and (await lim.allow_async("ip"))[0]
        allowed, retry = await lim.allow_async("ip")
        assert not allowed and 1 <= retry <= 61
        assert len(lim._hits["ip"]) == 2                          # its own table was used

    async def test_hxdbmanager_elevation_round_trip(self, cache_off):
        import time

        from case_service.api.routers import hxdbmanager as m
        assert not await m._has_active_elevation("u1")
        await shared_state.put("dbm-elevation", "u1", str(time.time() + 60), 60)
        assert await m._has_active_elevation("u1")


class TestQuietChannel:
    async def test_a_quiet_subscription_does_not_switch_the_store_off(self, cache_on):
        """Regression (2026-10-17): with listen(), 3 s of silence raised the client's
        socket timeout and marked the cache down for 30 s — every shared limit
        silently became per-process. Silence must be just silence."""
        got: list[dict] = []

        async def consume():
            async for m in shared_state.listen("quiet"):
                got.append(m)
                return

        task = asyncio.create_task(consume())
        await _until_subscribed(cache_on, "quiet")
        await asyncio.sleep(4.5)                         # longer than the 3 s socket timeout
        assert shared_state._down_until == 0.0, "a quiet channel marked the cache down"
        await _publish_as_other_process(cache_on, "quiet", {"after": "silence"})
        await asyncio.wait_for(task, 5)
        assert got == [{"after": "silence"}]
