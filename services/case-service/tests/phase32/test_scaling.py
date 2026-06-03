"""HELIX P32 — Redis bridge, rate limiter, realtime multi-instance tests."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from case_service.realtime.manager import ConnectionManager
from case_service.realtime.redis_bridge import RedisBridge


# ── Helpers ──────────────────────────────────────────────────────────


class FakeConn:
    def __init__(self, cid="c1", user_id="u1"):
        self.id = cid
        self.user_id = user_id
        self.channels = set()
        self.sent: list = []

    async def send(self, msg):
        self.sent.append(msg)
        return True


# ── Manager refactor tests ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_01_local_fanout_reaches_subscriber():
    m = ConnectionManager()
    conn = FakeConn()
    m._connections[conn.id] = conn
    m._subscriptions["cases.abc"].add(conn.id)
    conn.channels.add("cases.abc")
    sent = await m._local_fanout("cases.abc", {"channel": "cases.abc", "data": "x"})
    assert sent == 1
    assert conn.sent[0]["data"] == "x"


@pytest.mark.asyncio
async def test_02_broadcast_delegates_to_local_fanout():
    m = ConnectionManager()
    conn = FakeConn()
    m._connections[conn.id] = conn
    m._subscriptions["cases.*"].add(conn.id)
    conn.channels.add("cases.*")
    sent = await m.broadcast("cases.abc", {"type": "updated"})
    assert sent == 1


@pytest.mark.asyncio
async def test_03_attach_redis_bridge_is_called_on_broadcast():
    m = ConnectionManager()
    bridge = MagicMock()
    bridge.publish = AsyncMock()
    m.attach_redis_bridge(bridge)
    await m.broadcast("cases.x", {"type": "created"})
    bridge.publish.assert_awaited_once()
    args, _ = bridge.publish.call_args
    assert args[0] == "cases.x"
    assert args[1]["data"] == {"type": "created"}


# ── Redis bridge unit tests (with fake redis) ────────────────────────


class FakePubSub:
    def __init__(self):
        self.messages: list = []
        self._idx = 0

    async def psubscribe(self, pattern):
        self.pattern = pattern

    async def get_message(self, ignore_subscribe_messages=True, timeout=1.0):
        if self._idx < len(self.messages):
            msg = self.messages[self._idx]
            self._idx += 1
            return msg
        # Respect timeout so the subscriber loop doesn't spin
        await asyncio.sleep(min(timeout, 0.05))
        return None

    async def close(self):
        pass


class FakeRedis:
    def __init__(self):
        self.published: list = []
        self.pub = FakePubSub()

    def pubsub(self):
        return self.pub

    async def publish(self, channel, data):
        self.published.append((channel, data))


@pytest.mark.asyncio
async def test_04_redis_bridge_publishes_on_broadcast():
    m = ConnectionManager()
    r = FakeRedis()
    b = RedisBridge(m, r, prefix="t:", instance_id="i1")
    await b.start()
    m.attach_redis_bridge(b)
    await m.broadcast("cases.x", {"type": "t"})
    await asyncio.sleep(0.05)
    await b.stop()
    assert len(r.published) == 1
    chan, payload = r.published[0]
    assert chan == "t:cases.x"
    parsed = json.loads(payload)
    assert parsed["_src"] == "i1"
    assert parsed["channel"] == "cases.x"


@pytest.mark.asyncio
async def test_05_redis_bridge_ignores_own_messages():
    m = ConnectionManager()
    r = FakeRedis()
    b = RedisBridge(m, r, prefix="t:", instance_id="me")
    # Pre-queue a message from ourselves
    r.pub.messages.append({
        "type": "pmessage",
        "data": json.dumps({"_src": "me", "channel": "x", "envelope": {"data": 1}}),
    })
    conn = FakeConn()
    m._connections[conn.id] = conn
    m._subscriptions["x"].add(conn.id)
    await b.start()
    await asyncio.sleep(0.3)
    await b.stop()
    # Should NOT have fanned out (self-suppression)
    assert len(conn.sent) == 0


@pytest.mark.asyncio
async def test_06_redis_bridge_fans_out_remote_messages():
    m = ConnectionManager()
    r = FakeRedis()
    b = RedisBridge(m, r, prefix="t:", instance_id="me")
    r.pub.messages.append({
        "type": "pmessage",
        "data": json.dumps({"_src": "other", "channel": "cases.y", "envelope": {"data": "hello"}}),
    })
    conn = FakeConn()
    m._connections[conn.id] = conn
    m._subscriptions["cases.y"].add(conn.id)
    conn.channels.add("cases.y")
    await b.start()
    await asyncio.sleep(0.3)
    await b.stop()
    assert len(conn.sent) == 1
    assert conn.sent[0]["data"] == "hello"


# ── Rate limiter unit tests (Redis-backed) ───────────────────────────


@pytest.mark.asyncio
async def test_07_redis_rate_limiter_fails_open_without_redis(monkeypatch):
    from case_service.middleware.rate_limit_redis import RedisRateLimitMiddleware

    async def _no_redis():
        return None

    app = FastAPI()
    app.add_middleware(RedisRateLimitMiddleware, requests_per_minute=60, burst=5)
    @app.get("/x")
    async def x(): return {"ok": True}

    # Patch get_redis to return None
    monkeypatch.setattr("case_service.redis_client.get_redis", _no_redis)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        for _ in range(20):
            r = await c.get("/x")
            assert r.status_code == 200  # fail-open


@pytest.mark.asyncio
async def test_08_redis_rate_limiter_enforces_when_redis_available(monkeypatch):
    """Simulate Redis: allow first burst requests, then deny."""
    from case_service.middleware import rate_limit_redis as rlr

    class ScriptedRedis:
        def __init__(self):
            self.count = 0
            self.burst = 3
        async def script_load(self, _lua):
            return "sha"
        async def evalsha(self, sha, n, *args):
            self.count += 1
            if self.count <= self.burst:
                return [1, self.burst - self.count, 0]
            return [0, 0, 10_000]

    sr = ScriptedRedis()

    async def _get_redis():
        return sr
    monkeypatch.setattr("case_service.redis_client.get_redis", _get_redis)

    app = FastAPI()
    app.add_middleware(rlr.RedisRateLimitMiddleware, requests_per_minute=60, burst=3)
    @app.get("/x")
    async def x(): return {"ok": True}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        codes = []
        for _ in range(6):
            r = await c.get("/x")
            codes.append(r.status_code)
    assert codes[:3] == [200, 200, 200]
    assert 429 in codes[3:]


# ── Locust file smoke test ───────────────────────────────────────────


def test_09_locustfile_defines_expected_user_classes():
    """Static check — avoid importing locust (its event-loop hangs under pytest)."""
    from pathlib import Path
    src = (Path(__file__).parents[4] / "load-tests" / "locustfile.py").read_text()
    for cls in ("CaseWorkerUser", "CaseCreatorUser", "ObservabilityUser"):
        assert f"class {cls}" in src, f"{cls} missing from locustfile.py"
    # Also verify it declares required Locust decorators
    assert "@task" in src
    assert "HttpUser" in src


# ── Helm chart sanity ────────────────────────────────────────────────


def test_10_helm_chart_files_exist():
    from pathlib import Path
    base = Path(__file__).parents[4] / "deploy" / "helm" / "helix"
    assert (base / "Chart.yaml").exists()
    assert (base / "values.yaml").exists()
    templates = {
        "case-service-deployment.yaml",
        "case-service-hpa.yaml",
        "engine-deployment.yaml",
        "engine-hpa.yaml",
        "worker-deployment.yaml",
        "worker-keda-scaledobject.yaml",
        "_helpers.tpl",
    }
    existing = {f.name for f in (base / "templates").iterdir()}
    missing = templates - existing
    assert not missing, f"missing templates: {missing}"


def test_11_values_yaml_has_autoscaling_keys():
    import yaml
    from pathlib import Path
    values = yaml.safe_load((Path(__file__).parents[4] / "deploy" / "helm" / "helix" / "values.yaml").read_text())
    assert values["caseService"]["autoscaling"]["enabled"] is True
    assert values["engine"]["autoscaling"]["enabled"] is True
    assert "keda" in values["worker"]


@pytest.mark.asyncio
async def test_12_redis_client_falls_back_gracefully(monkeypatch):
    """get_redis returns None if redis_enabled=False."""
    from case_service import redis_client as rc
    rc.reset_redis_client()

    class FakeSettings:
        redis_enabled = False
        redis_url = ""

    monkeypatch.setattr("case_service.config.get_settings", lambda: FakeSettings())
    out = await rc.get_redis()
    assert out is None
