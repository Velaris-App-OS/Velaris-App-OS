"""R5d 11b — the read/write split: which reads run where, and why.

Agreed 2026-10-22 (Nov_TODO R5d "11b design"). The rules under test: no replica
= exactly as before; read your own writes; behind is not down; down → the
capped main pool, back only after a healthy spell; READ ONLY always; a cutoff
barrier for point-in-time outputs.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from starlette.requests import Request

from velaris_shared import shared_state
from velaris_shared.db import read_routing as rr
from velaris_shared.db import session as dbs
from velaris_shared.middleware.read_routing import ReadRoutingMiddleware


class FakeSession:
    bind = None

    def __init__(self, name, fail_first=False, log=None):
        from sqlalchemy.orm import Session
        self.name, self.fail_first, self.log = name, fail_first, log if log is not None else []
        self.info = {}
        self.sync_session = Session()      # carries the READ ONLY hook

    async def execute(self, stmt, *a, **k):
        self.log.append((self.name, str(stmt)))
        if self.fail_first:
            self.fail_first = False
            raise ConnectionError("replica gone")

    async def rollback(self):
        self.log.append((self.name, "rollback"))

    async def close(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


@pytest.fixture
def routing(monkeypatch):
    rr.reset_for_tests()
    shared_state.reset_for_tests()
    log = []
    fac = {"replica": lambda: FakeSession("replica", log=log),
           "main_capped": lambda: FakeSession("main_capped", log=log)}
    monkeypatch.setattr(dbs, "replica_url", lambda: "postgresql+asyncpg://replica/x")
    monkeypatch.setattr(rr.get_settings(), "database_read_url", "postgresql+asyncpg://replica/x")
    monkeypatch.setattr(dbs, "get_replica_session_factory", lambda: fac["replica"])
    monkeypatch.setattr(rr, "fallback_factory", lambda: fac["main_capped"])
    monkeypatch.setattr(rr, "_main_connect", lambda: _ScalarConn("0/9000000"))

    async def healthy(force=False):
        rr.health._mark_up(datetime(2026, 10, 22, 9, 44, tzinfo=timezone.utc))
    monkeypatch.setattr(rr.health, "refresh", healthy)
    return {"log": log, "fac": fac}


def _req(headers=None, user=None):
    scope = {"type": "http", "method": "GET", "headers": [(k.lower().encode(), v.encode())
                                                          for k, v in (headers or {}).items()],
             "state": {}}
    r = Request(scope)
    if user:
        r.state.velaris_user_id = user
    return r


async def _read(request):
    """One request: the session, one query in the route body; an exception
    there is thrown into the dependency, as FastAPI does."""
    gen = rr.get_read_session(request)
    s = await gen.__anext__()
    try:
        await s.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        await gen.athrow(exc)
        raise
    await gen.aclose()
    return s.name


async def test_no_replica_is_exactly_as_before(monkeypatch):
    """Each path keeps the pool it always had: the newly routed ones the
    operations pool, the old get_replica_session ones the analytics pool."""
    rr.reset_for_tests()
    monkeypatch.setattr(dbs, "replica_url", lambda: "")
    factory, route, as_of = await rr.choose(_req(user="u1"))
    assert route == "main_no_replica" and factory is dbs.get_session_factory()
    factory, route, as_of = await rr.choose(_req(user="u1"), analytics_pool=True)
    assert factory is dbs.get_replica_session_factory()
    await rr.mark_wrote("u1")                       # no replica: nothing to remember
    assert await rr.wrote_recently("u1") is False


async def test_no_replica_shares_the_requests_own_session(monkeypatch):
    """The 11b audit (2026-10-23): a read session of its own beside the guard's
    took a second connection from the same pool — 8 requests on a 2-connection
    pool all failed after 30 s on real PostgreSQL. With no replica the routed
    paths get the request's own session (the guard's), as before 11b."""
    rr.reset_for_tests()
    monkeypatch.setattr(dbs, "replica_url", lambda: "")
    own = FakeSession("request")
    req = _req(user="u1")
    dep = rr.get_read_session(req, own)
    assert await dep.__anext__() is own
    await dep.aclose()
    async with rr.read_session_at_cutoff(req, main=own) as s:
        assert s is own
    assert "velaris_read_only_hook" not in own.info
    import inspect
    for fn in (rr.get_read_session, rr.get_cutoff_session):
        assert inspect.signature(fn).parameters["main"].default.dependency is dbs.get_session


async def test_a_healthy_replica_takes_the_read_and_says_as_of_when(routing):
    req = _req(user="u1")
    assert await _read(req) == "replica"
    assert req.state.velaris_read_route == "replica"
    assert req.state.velaris_read_as_of.isoformat().startswith("2026-10-22T09:44")


async def test_a_user_reads_their_own_write_from_the_main(routing, monkeypatch):
    """The user's point: write on the main, then read the old value from the
    replica. The read runs on the request's own session (the path's own pool),
    not the 3-connection capped pool — at a peak most active users have just
    written (11b audit, 2026-10-23)."""
    await rr.mark_wrote("u1")
    own = FakeSession("request")
    dep = rr.get_read_session(_req(user="u1"), own)
    assert await dep.__anext__() is own
    await dep.aclose()
    factory, route, _ = await rr.choose(_req(user="u1"), analytics_pool=True)
    assert route == "main_own_writes" and factory is dbs.get_analytics_session_factory()
    assert await _read(_req(user="u2")) == "replica"            # only the writer


async def test_an_existing_analytics_replica_does_not_take_the_newly_routed_paths(monkeypatch):
    """An install with DB_ANALYTICS_URL keeps it for the paths that always used
    it; the paths 11b routed move only when the operator sets DATABASE_READ_URL."""
    rr.reset_for_tests()
    monkeypatch.setattr(dbs, "replica_url", lambda: "postgresql+asyncpg://analytics-replica/x")
    monkeypatch.setattr(rr.get_settings(), "database_read_url", "")
    _, route, _ = await rr.choose(_req(user="u1"))
    assert route == "main_no_replica"
    async with rr.read_session_at_cutoff(_req()) as s:
        pass
    assert rr._replica_for(True) and not rr._replica_for(False)


def test_a_router_on_the_analytics_pool_keeps_it_on_its_read_paths():
    """compliance.py names get_analytics_session `get_session`: its routed paths
    were on the analytics pool, so they take the analytics variants (11b audit)."""
    import importlib
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2] / "case_service" / "api" / "routers"
    for f in root.glob("*.py"):
        mod = importlib.import_module(f"case_service.api.routers.{f.stem}")
        if getattr(mod, "get_session", None) is dbs.get_analytics_session:
            for name in ("get_read_session", "get_cutoff_session"):
                dep = getattr(mod, name, None)
                assert dep in (None, rr.get_analytics_read_session, rr.get_analytics_cutoff_session), \
                    (f.name, name)


async def test_the_own_writes_window_ends(routing, monkeypatch):
    monkeypatch.setattr(rr.get_settings(), "read_own_writes_seconds", 0.05)
    await rr.mark_wrote("u1")
    import asyncio
    await asyncio.sleep(0.1)
    assert await _read(_req(user="u1")) == "replica"


def test_the_user_comes_from_a_valid_token_when_auth_has_not_run_yet():
    from velaris_shared.auth.jwt_handler import create_dev_token
    s = rr.get_settings()
    tok = create_dev_token("u9", secret=s.auth_secret, private_key=s.auth_rsa_private_key or "")
    assert rr.user_of(_req({"Authorization": f"Bearer {tok}"})) == "u9"


def test_a_forged_token_names_nobody():
    """Found in the 11b audit: an unverified subject let anyone ask "did user X
    just write?" by naming X in a token nobody signed."""
    import jwt
    forged = jwt.encode({"sub": "victim"}, "attacker-key-" * 4, algorithm="HS256")
    assert rr.user_of(_req({"Authorization": f"Bearer {forged}"})) is None
    assert rr.user_of(_req({"Authorization": "Bearer not-a-jwt"})) is None


async def test_a_replica_behind_keeps_its_reads(routing, monkeypatch):
    """The user's point: switching at peak would move the heavy load onto the
    main exactly when it is busiest. Behind is not down."""
    async def behind(force=False):
        rr.health._mark_up(datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc))   # 44 min behind
    monkeypatch.setattr(rr.health, "refresh", behind)
    req = _req(user="u1")
    assert await _read(req) == "replica" and req.state.velaris_read_as_of.minute == 0


async def test_a_replica_that_is_down_sends_reads_to_the_capped_main_pool(routing, monkeypatch):
    async def down(force=False):
        rr.health.mark_down("connection refused")
    monkeypatch.setattr(rr.health, "refresh", down)
    assert await _read(_req(user="u1")) == "main_capped"


def test_after_an_outage_the_replica_comes_back_only_after_a_healthy_spell(monkeypatch):
    h = rr.ReplicaHealth()
    monkeypatch.setattr(rr.get_settings(), "read_recovery_seconds", 60.0)
    h._mark_up(None)
    assert h.usable()                                  # never failed: straight away
    h.mark_down("x")
    h._mark_up(None)
    t0 = h.healthy_since
    assert not h.usable(now=t0 + 59) and h.usable(now=t0 + 60)


async def test_a_replica_lost_mid_request_is_a_retry_and_the_next_read_goes_to_the_main(
        routing, monkeypatch):
    """No connection is taken while dependencies resolve (the audit's deadlock),
    so a replica lost since the last health look fails at the route's first
    query: that request gets a 503 to retry, the replica is looked at again at
    once, and the next request reads from the main."""
    log = routing["log"]
    routing["fac"]["replica"] = lambda: FakeSession("replica", fail_first=True, log=log)
    forced = []

    async def down(force=False):
        forced.append(force)
        rr.health.mark_down("gone")
    monkeypatch.setattr(rr.health, "refresh", lambda force=False: down(force) if force else _noop())
    rr.health._mark_up(None)
    with pytest.raises(Exception) as e:
        await _read(_req(user="u1"))
    assert getattr(e.value, "status_code", None) == 503 and forced == [True]
    assert await _read(_req(user="u1")) == "main_capped"


async def _noop():
    return None


async def _noop_refresh(force=False):
    return None


async def test_every_read_session_is_read_only_and_never_commits(routing):
    req = _req(user="u1")
    gen = rr.get_read_session(req)
    s = await gen.__anext__()
    from sqlalchemy import event
    assert event.contains(s.sync_session, "after_begin", s.info["velaris_read_only_hook"])
    await gen.aclose()
    assert "velaris_read_only_hook" not in s.info          # removed on release
    assert [x for n, x in routing["log"] if n == "replica"] == ["rollback"]   # no commit


async def test_the_barrier_reads_the_replica_once_it_passed_the_cutoff(routing, monkeypatch):
    monkeypatch.setattr(rr, "_postgres", lambda: True)

    class Conn:
        def __init__(self, answers):
            self.answers = answers

        async def execute(self, stmt, params=None):
            v = self.answers.pop(0)

            class R:
                def scalar(self_inner):
                    return v
            return R()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class Eng:
        def __init__(self, answers):
            self.answers = answers

        def connect(self):
            return Conn(self.answers)

    monkeypatch.setattr(dbs, "get_replica_engine", lambda: Eng([False, False, True]))
    req = _req()
    async with rr.read_session_at_cutoff(req) as s:
        assert s.name == "replica"
    assert req.state.velaris_read_route == "replica"


async def test_the_barrier_runs_on_the_main_when_the_replica_does_not_catch_up(routing, monkeypatch):
    """The user's point: a case updated at 9:43:59, the report at 9:44:00 — the
    report must include it, so a replica that cannot reach the cutoff in time
    is not read at all."""
    monkeypatch.setattr(rr, "_postgres", lambda: True)
    monkeypatch.setattr(rr.get_settings(), "read_barrier_max_seconds", 0.2)

    class Never:
        def connect(self):
            class C:
                async def execute(self, *a, **k):
                    class R:
                        def scalar(self):
                            return False
                    return R()

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *a):
                    return False
            return C()
    monkeypatch.setattr(dbs, "get_replica_engine", lambda: Never())
    async with rr.read_session_at_cutoff(_req()) as s:
        assert s.name == "main_capped"


class _ScalarConn:
    def __init__(self, v):
        self.v = v

    async def execute(self, *a, **k):
        v = self.v

        class R:
            def scalar(self):
                return v
        return R()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


async def test_a_writable_server_that_is_not_this_database_is_refused(monkeypatch):
    rr.reset_for_tests()
    monkeypatch.setattr(rr, "_postgres", lambda: True)

    class One:
        def __init__(self, row):
            self.row = row

        def one(self):
            return self.row

        def scalar(self):
            return self.row

    class C:
        def __init__(self, rows):
            self.rows = rows

        async def execute(self, *a, **k):
            return One(self.rows.pop(0))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(dbs, "get_replica_engine",
                        lambda: type("E", (), {"connect": lambda self: C([(False, None, False, "111")])})())
    monkeypatch.setattr(rr, "_main_connect", lambda: C(["222"]))
    with pytest.raises(RuntimeError, match="not this install's database"):
        await rr._probe()
    # …and a REPLICA of another cluster (in recovery) — it carries that cluster's id.
    rr.reset_for_tests()
    monkeypatch.setattr(rr, "_postgres", lambda: True)
    monkeypatch.setattr(dbs, "get_replica_engine",
                        lambda: type("E", (), {"connect": lambda self: C([(True, None, True, "111")])})())
    with pytest.raises(RuntimeError, match="not this install's database"):
        await rr._probe()


class _Rows:
    def __init__(self, v):
        self.v = v

    def scalar(self):
        return self.v

    def mappings(self):
        v = self.v

        class M:
            def all(self_inner):
                return v
        return M()


class _MyConn:
    def __init__(self, answers):
        self.answers = answers

    async def execute(self, stmt, *a, **k):
        v = self.answers.pop(0)
        if isinstance(v, Exception):
            raise v
        return _Rows(v)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _eng(answers):
    return type("E", (), {"connect": lambda self: _MyConn(answers)})()


@pytest.mark.parametrize("replica_answers, main_answers, ok", [
    (["U-MAIN"], ["U-MAIN"], True),                                           # the main itself
    (["U-R", [{"Source_UUID": "U-MAIN", "Seconds_Behind_Source": 3}]], ["U-MAIN"], True),
    (["U-R", [{"Source_UUID": "U-OTHER", "Seconds_Behind_Source": 0}]], ["U-MAIN", "9"], False),
    (["U-R", PermissionError("denied"), PermissionError("denied")], ["U-MAIN"], False),
    (["U-R", [{"Source_UUID": "U-MAIN", "Seconds_Behind_Source": None}]], ["U-MAIN"], False),
])
async def test_mysql_and_mariadb_replicas_must_be_shown_to_be_this_database(
        monkeypatch, replica_answers, main_answers, ok):
    rr.reset_for_tests()
    monkeypatch.setattr(rr, "_postgres", lambda: False)
    monkeypatch.setattr(dbs, "get_replica_engine", lambda: _eng(replica_answers))
    main = list(main_answers)
    monkeypatch.setattr(rr, "_main_connect", lambda: _eng(main).connect())
    if ok:
        assert await rr._probe() is not None
    else:
        with pytest.raises(RuntimeError):
            await rr._probe()


async def test_a_full_capped_pool_is_a_quick_busy_answer_not_a_hang(routing):
    from sqlalchemy.exc import TimeoutError as SATimeout

    class Full(FakeSession):
        async def execute(self, stmt, *a, **k):
            raise SATimeout("QueuePool limit reached")
    routing["fac"]["main_capped"] = lambda: Full("main_capped")
    rr.health.refresh = _noop_refresh          # the replica is down: the capped pool
    rr.health.mark_down("down")
    with pytest.raises(Exception) as e:
        await _read(_req(user="u1"))
    assert getattr(e.value, "status_code", None) == 503 and e.value.headers["Retry-After"] == "5"


async def test_a_replica_with_the_cache_off_says_own_writes_stay_in_one_process(routing, monkeypatch, caplog):
    monkeypatch.setattr(rr.get_settings(), "redis_enabled", False)
    await rr.choose(_req(user="u1"))
    await rr.choose(_req(user="u1"))
    assert sum("only inside one process" in m for m in caplog.messages) == 1


# ── the middleware ──────────────────────────────────────────────────────────

def _app(monkeypatch):
    rr.reset_for_tests()
    shared_state.reset_for_tests()
    monkeypatch.setattr(dbs, "replica_url", lambda: "postgresql+asyncpg://replica/x")
    app = FastAPI()
    app.add_middleware(ReadRoutingMiddleware)

    def who(request: Request):
        request.state.velaris_user_id = request.headers.get("x-test-user")

    @app.post("/write", dependencies=[Depends(who)])
    async def write(ok: bool = True):
        from fastapi import HTTPException
        if not ok:
            raise HTTPException(400, "no")
        return {"ok": True}

    @app.get("/read", dependencies=[Depends(who)])
    async def read(request: Request):
        request.state.velaris_read_route = "replica"
        request.state.velaris_read_as_of = datetime(2026, 10, 22, 9, 44, tzinfo=timezone.utc)
        return {"own": await rr.wrote_recently(request.headers.get("x-test-user"))}
    return TestClient(app)


def test_a_successful_write_marks_the_user_before_the_response_leaves(monkeypatch):
    c = _app(monkeypatch)
    assert c.get("/read", headers={"x-test-user": "u1"}).json() == {"own": False}
    c.post("/write", headers={"x-test-user": "u1"})
    assert c.get("/read", headers={"x-test-user": "u1"}).json() == {"own": True}
    assert c.get("/read", headers={"x-test-user": "u2"}).json() == {"own": False}


def test_a_refused_write_and_a_read_mark_nobody(monkeypatch):
    c = _app(monkeypatch)
    c.post("/write?ok=false", headers={"x-test-user": "u1"})
    c.get("/read", headers={"x-test-user": "u1"})
    assert c.get("/read", headers={"x-test-user": "u1"}).json() == {"own": False}


def test_an_opted_in_read_says_as_of_when_and_nothing_about_where(monkeypatch):
    r = _app(monkeypatch).get("/read", headers={"x-test-user": "u1"})
    assert r.headers["x-velaris-data-as-of"].startswith("2026-10-22T09:44")
    assert "x-velaris-read-from" not in r.headers          # infrastructure, not for clients


def test_a_refused_request_carries_no_read_header(monkeypatch):
    c = _app(monkeypatch)

    @c.app.get("/denied")
    async def denied(request: Request):
        from fastapi import HTTPException
        request.state.velaris_read_route = "main_own_writes"
        request.state.velaris_read_as_of = datetime(2026, 10, 22, tzinfo=timezone.utc)
        raise HTTPException(401, "no")
    r = c.get("/denied")
    assert r.status_code == 401 and "x-velaris-data-as-of" not in r.headers


def test_unset_settings_keep_every_install_as_before():
    from velaris_shared.config import Settings
    s = Settings(_env_file=None)
    assert s.database_read_url == "" and s.db_analytics_url == ""


def test_the_platform_stacks_carry_the_middleware():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[4]
    for f in ("services/_shared/velaris_shared/app.py", "services/case-service/case_service/main.py"):
        assert "app.add_middleware(ReadRoutingMiddleware)" in (root / f).read_text(), f


async def test_the_default_sqlite_test_database_refuses_a_write_on_a_read_path(tmp_path, monkeypatch):
    """So the ordinary test suite — not only a PostgreSQL run — catches a write
    that slipped onto an opted-in read path; and the connection is writable
    again afterwards."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db", poolclass=NullPool)
    async with eng.begin() as c:
        await c.execute(text("create table t (x int)"))
    rr.reset_for_tests()
    monkeypatch.setattr(dbs, "replica_url", lambda: "")
    monkeypatch.setattr(dbs, "get_session_factory", lambda: async_sessionmaker(eng))
    async for s in rr.get_read_session(None):
        with pytest.raises(Exception, match="readonly|read-only|attempt to write"):
            await s.execute(text("insert into t values (1)"))
    async with eng.begin() as c:
        await c.execute(text("insert into t values (2)"))         # writable again
    await eng.dispose()


def test_analytics_routes_use_only_sessions_its_tenant_scope_marks():
    """Found in 11b: analytics-service scopes by MARKING the session objects of
    `get_replica_session` / `get_analytics_session`; a route on any other session
    dependency read every tenant (another tenant's saved report ran, 200)."""
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parents[4] / "services" / "analytics-service" / "analytics_service"
    allowed = {"get_ro_session", "get_session", "_scoped_cutoff_session", "get_replica_session",
               "get_analytics_session"}
    for f in root.rglob("*.py"):
        src = f.read_text()
        for dep in re.findall(r"Depends\((get_[a-z_]*session[a-z_]*|_scoped_[a-z_]+)\)", src):
            if dep == "get_analytics_cutoff_session":
                # only inside the wrapper that scopes it
                wrapper = src.split("async def _scoped_cutoff_session", 1)
                assert len(wrapper) == 2 and src.count("Depends(get_analytics_cutoff_session)") == 1 \
                    and "Depends(get_analytics_cutoff_session)" in wrapper[1].split("\n\n\n", 1)[0], f.name
                continue
            assert dep in allowed, f"{f.name}: Depends({dep}) is not tenant-scoped"


@pytest.mark.skipif(not (os.environ.get("VELARIS_TEST_DATABASE_URL") or "").startswith("postgresql"),
                    reason="needs a real PostgreSQL (VELARIS_TEST_DATABASE_URL)")
async def test_the_barrier_statement_runs_on_real_postgresql_through_asyncpg():
    """The barrier's SQL as asyncpg sends it. The mocked tests above never ran
    it: `CAST(:l AS pg_lsn)` failed on every call against a real server (clean
    box 2026-10-23) and took the replica out after each report."""
    from sqlalchemy.ext.asyncio import create_async_engine
    eng = create_async_engine(os.environ["VELARIS_TEST_DATABASE_URL"])
    try:
        async with eng.connect() as c:
            lsn = (await c.execute(text("SELECT pg_current_wal_lsn()::text"))).scalar()
            assert (await c.execute(text(rr._BARRIER_SQL), {"l": lsn})).scalar() is True
    finally:
        await eng.dispose()
