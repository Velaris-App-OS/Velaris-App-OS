"""Velaris P32 — Redis bridge, rate limiter, realtime multi-instance tests."""
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
async def test_07_redis_rate_limiter_falls_back_without_redis(monkeypatch):
    """Cache unreachable → the in-process limiter, never no limit.

    It used to fail OPEN here: every request allowed while the cache was down
    (changed 2026-10-16). A real client address is used because loopback is
    exempt from the in-process limiter.
    """
    from velaris_shared.middleware.rate_limit_redis import RedisRateLimitMiddleware

    async def _no_redis():
        return None

    app = FastAPI()
    app.add_middleware(RedisRateLimitMiddleware, requests_per_minute=60, burst=5)
    @app.get("/x")
    async def x(): return {"ok": True}

    monkeypatch.setattr("velaris_shared.redis_client.get_redis", _no_redis)

    transport = ASGITransport(app=app, client=("203.0.113.9", 4321))
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        codes = [(await c.get("/x")).status_code for _ in range(12)]
    assert codes[:5] == [200] * 5
    assert 429 in codes[5:]


@pytest.mark.asyncio
async def test_07b_redis_rate_limiter_reloads_its_script_after_a_cache_restart(monkeypatch):
    """NOSCRIPT (cache restarted, script cache gone) must not disable limiting
    for good: the limiter forgets the sha, falls back for that request, and
    loads the script again on the next one."""
    from velaris_shared.middleware import rate_limit_redis as rlr

    class RestartedRedis:
        def __init__(self):
            self.loads = 0
            self.fail_next = True
        async def script_load(self, _lua):
            self.loads += 1
            return f"sha{self.loads}"
        async def evalsha(self, sha, n, *args):
            if self.fail_next:
                self.fail_next = False
                raise RuntimeError("NOSCRIPT No matching script")
            return [1, 2, 0]

    rr = RestartedRedis()

    async def _get_redis():
        return rr
    monkeypatch.setattr("velaris_shared.redis_client.get_redis", _get_redis)

    app = FastAPI()
    app.add_middleware(rlr.RedisRateLimitMiddleware, requests_per_minute=60, burst=3)
    @app.get("/x")
    async def x(): return {"ok": True}

    transport = ASGITransport(app=app, client=("203.0.113.9", 4321))
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        first = await c.get("/x")
        second = await c.get("/x")
    assert first.status_code == 200 and "X-RateLimit-Backend" not in first.headers
    assert second.headers.get("X-RateLimit-Backend") == "redis"
    assert rr.loads == 2, "the script must be loaded again after NOSCRIPT"


@pytest.mark.asyncio
async def test_07c_forwarded_for_cannot_switch_the_limiter_off():
    """`X-Forwarded-For: 127.0.0.1` used to hit the loopback exemption: the
    limiter read the header's left-most entry, which the client writes. It now
    keys on uvicorn's resolved address only."""
    from velaris_shared.middleware.rate_limit import RateLimitMiddleware

    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, requests_per_minute=60, burst=3)
    @app.get("/x")
    async def x(): return {"ok": True}

    transport = ASGITransport(app=app, client=("203.0.113.9", 4321))
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        codes = [
            (await c.get("/x", headers={"X-Forwarded-For": "127.0.0.1"})).status_code
            for _ in range(8)
        ]
    assert 429 in codes, "a spoofed loopback X-Forwarded-For must not bypass the limit"


def test_07d_cache_password_never_reaches_a_log():
    from velaris_shared.redis_client import _safe_url
    from velaris_shared.config import Settings

    assert _safe_url("redis://:hunter2@127.0.0.1:6379/0") == "redis://:***@127.0.0.1:6379/0"
    assert _safe_url("redis://localhost:6379/0") == "redis://localhost:6379/0"
    s = Settings(redis_password="hunter2")
    assert "hunter2" not in repr(s)


@pytest.mark.asyncio
async def test_08_redis_rate_limiter_enforces_when_redis_available(monkeypatch):
    """Simulate Redis: allow first burst requests, then deny."""
    from velaris_shared.middleware import rate_limit_redis as rlr

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
    monkeypatch.setattr("velaris_shared.redis_client.get_redis", _get_redis)

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
    for cls in ("VelarisUser", "CaseWorkerUser", "ManagerUser", "DevOpsUser", "SpikeUser"):  # current persona set
        assert f"class {cls}" in src, f"{cls} missing from locustfile.py"
    # Also verify it declares required Locust decorators
    assert "@task" in src
    assert "HttpUser" in src


# ── Helm chart sanity ────────────────────────────────────────────────


def test_10_helm_chart_files_exist():
    from pathlib import Path
    base = Path(__file__).parents[4] / "deploy" / "helm" / "velaris"
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


def test_10b_every_http_deployment_probes_correctly():
    """The probe shape, pinned (2026-09-17).

    Three things, each of which has been wrong here before:

    * **Readiness checks the DATABASE (`/ready`), liveness only the process
      (`/health`).** case-service probed `/health` for both until the August
      Kubernetes pass and reported Ready with no database behind it; the engine
      kept that shape until 2026-09-17, where it is worse — without a database
      the engine keeps deployed processes in one pod's memory.
    * **Liveness must never check the database.** Restarting every pod because
      the database blipped turns a database outage into a platform outage.
    * **A startup probe owns the boot budget**, so a slow first boot is waited
      out rather than restarted in a loop.
    """
    from pathlib import Path
    base = Path(__file__).parents[4] / "deploy" / "helm" / "velaris" / "templates"
    for name in ("case-service-deployment.yaml", "analytics-service-deployment.yaml",
                 "hxgraph-service-deployment.yaml", "hxmigrate-service-deployment.yaml",
                 "commerce-service-deployment.yaml", "engine-deployment.yaml"):
        src = (base / name).read_text()
        for probe in ("startupProbe:", "readinessProbe:", "livenessProbe:"):
            assert probe in src, f"{name} has no {probe}"
        after = lambda p: src.split(p, 1)[1].split("Probe:", 1)[0]
        assert "path: /health" in after("startupProbe:"), name
        assert "path: /ready" in after("readinessProbe:"), f"{name} readiness must check the database"
        assert "path: /health" in after("livenessProbe:"), f"{name} liveness must NOT check the database"
        assert "failureThreshold" in after("startupProbe:"), name


def test_10c_workloads_are_hardened_and_carry_no_plaintext_password():
    """What a cluster deploy found on 2026-09-17, pinned.

    * **No securityContext at all** on our six deployments (the bundled
      PostgreSQL and Valkey subcharts had theirs from day one), so containers
      could escalate privileges and kept every capability.
    * **The default ServiceAccount token was mounted** into pods that never
      call the Kubernetes API — a credential sitting there for anything that
      gets in.
    * **The database URL, password and all, was an inline env value** in every
      deployment: readable by anyone who can `get deployments`, which is a much
      wider group than `get secrets`.
    """
    from pathlib import Path
    base = Path(__file__).parents[4] / "deploy" / "helm" / "velaris"
    assert (base / "templates" / "db-secret.yaml").exists()
    for name in ("case-service", "analytics-service", "hxgraph-service",
                 "hxmigrate-service", "commerce-service", "engine", "worker"):
        src = (base / "templates" / f"{name}-deployment.yaml").read_text()
        assert "automountServiceAccountToken: false" in src, name
        assert 'include "helix.podSecurityContext"' in src, name
        assert 'include "helix.containerSecurityContext"' in src, name
        assert 'include "helix.databaseUrl"' not in src, (
            f"{name}: the database URL must come from the Secret, not inline")
        if "DATABASE_URL" in src:
            assert "secretKeyRef" in src, name
    helpers = (base / "templates" / "_helpers.tpl").read_text()
    for want in ("runAsNonRoot", "allowPrivilegeEscalation: false",
                 'drop: ["ALL"]', "seccompProfile"):
        assert want in helpers, want


def test_10d_images_declare_a_numeric_non_root_user():
    """`runAsNonRoot` fails a pod with "image has non-numeric user" before the
    container starts, so a named USER is not enough. The engine ran as ROOT
    until 2026-09-17 — the one image that did — and every image now pins uid
    10001. Found by deploying the chart to a cluster, not by reading it."""
    from pathlib import Path
    root = Path(__file__).parents[4]
    for df in ("services/case-service/Dockerfile", "services/analytics-service/Dockerfile",
               "services/hxgraph-service/Dockerfile", "services/hxmigrate-service/Dockerfile",
               "services/commerce-service/Dockerfile", "engine/Dockerfile"):
        src = (root / df).read_text()
        assert "USER 10001:10001" in src, f"{df} must run as a numeric non-root uid"


def test_10e_the_case_service_image_carries_hxarbiter_core():
    """25 modules import `hxarbiter_core`, one of them at import time through
    `api/routers/hxnexus.py`. It is a workspace library, not a PyPI package, and
    the Dockerfile never copied it — so every case-service image built since
    HxArbiter shipped died at startup with ModuleNotFoundError. The host install
    hid it: there the library is an editable install in the workspace venv."""
    from pathlib import Path
    src = (Path(__file__).parents[4] / "services" / "case-service" / "Dockerfile").read_text()
    assert "libs/hxarbiter-core" in src
    assert "pip install --break-system-packages -e /app/libs/hxarbiter-core" in src


def test_11_values_yaml_has_autoscaling_keys():
    import yaml
    from pathlib import Path
    values = yaml.safe_load((Path(__file__).parents[4] / "deploy" / "helm" / "velaris" / "values.yaml").read_text())
    assert values["caseService"]["autoscaling"]["enabled"] is True
    assert values["engine"]["autoscaling"]["enabled"] is True
    assert "keda" in values["worker"]


@pytest.mark.asyncio
async def test_12_redis_client_falls_back_gracefully(monkeypatch):
    """get_redis returns None if redis_enabled=False."""
    from velaris_shared import redis_client as rc
    rc.reset_redis_client()

    class FakeSettings:
        redis_enabled = False
        redis_url = ""

    monkeypatch.setattr("velaris_shared.config.get_settings", lambda: FakeSettings())
    out = await rc.get_redis()
    assert out is None


# ── Bundled images are pinned, not floating (found 2026-08-26) ──────
#
# The chart pinned the *chart* version and left the *image* floating. Three of
# the four database/cache subcharts default to `bitnami/<name>:latest`, so two
# clusters installing the same chart version a month apart run different
# database builds — and a rescheduled pod silently upgrades a live database.
# Bitnami's 2025 policy leaves only `latest` in the free namespace, so the pin
# has to be a DIGEST rather than a version tag.


def _bundled_images() -> dict:
    import yaml
    from pathlib import Path
    values = yaml.safe_load(
        (Path(__file__).parents[4] / "deploy" / "helm" / "velaris" / "values.yaml").read_text())
    return {name: values[name].get("image") or {}
            for name in ("postgresql", "mysql", "mariadb", "valkey")}


def test_13_no_bundled_database_image_floats():
    """`latest` is only allowed when a digest nails it down."""
    floating = []
    for name, image in _bundled_images().items():
        if not image.get("digest"):
            floating.append(f"{name} (tag={image.get('tag')!r}, no digest)")
    assert not floating, (
        "bundled image(s) are not pinned: " + ", ".join(floating) +
        ". An unpinned image is a silent database upgrade on a customer's "
        "cluster, and Bitnami's free namespace keeps only `latest`, so the pin "
        "must be a digest."
    )


def test_14_the_digests_are_real_sha256_references():
    for name, image in _bundled_images().items():
        digest = image.get("digest", "")
        assert digest.startswith("sha256:") and len(digest) == 71, (
            f"{name}: {digest!r} is not a sha256 digest")


def test_15_mysql_points_at_an_image_that_exists():
    """`docker.io/bitnami/mysql` is EMPTY — no `latest`, no version tags at all
    — so the tag this chart pinned could not be pulled and `mysql.enabled=true`
    would ImagePullBackOff on a fresh cluster. The image lives on under
    `bitnamilegacy/`, which is frozen: no updates, no security fixes. Pointing
    there is what makes the path work; the values file says out loud that an
    operator running MySQL for real should bring their own database."""
    mysql = _bundled_images()["mysql"]
    assert mysql.get("repository") != "bitnami/mysql", (
        "bitnami/mysql has no tags at all — a bundled MySQL cannot start from it")


def test_07e_endpoint_limiters_key_on_the_resolved_client_not_the_header():
    """The per-endpoint limiters (storefront subscribe, checkout order lookup,
    every `rate_limit(...)` dependency) read X-Forwarded-For's left-most entry,
    which the client writes: a new value per request was a new "address", so
    tracking-token guessing was unlimited. They now share `_client_ip`, which
    returns uvicorn's resolved client only."""
    from types import SimpleNamespace
    from velaris_shared.middleware.endpoint_rate_limit import _client_ip

    req = SimpleNamespace(
        headers={"X-Forwarded-For": "198.51.100.77, 203.0.113.9"},
        client=SimpleNamespace(host="203.0.113.9"),
    )
    assert _client_ip(req) == "203.0.113.9"
    assert _client_ip(SimpleNamespace(headers={}, client=None)) == "unknown"
