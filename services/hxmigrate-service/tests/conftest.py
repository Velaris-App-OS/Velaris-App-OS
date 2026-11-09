"""Shared test fixtures for hxmigrate-service.

A copy of case-service's, bound to this service's app. The tests moved here
with their code at 13e; everything they touch below the app — the SQLite
engine, the session override, the model metadata — comes from velaris_shared
and is identical, which is precisely why the move was possible.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import atexit
import shutil
import tempfile
import uuid
from typing import AsyncGenerator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import velaris_shared.db.session as _db_session
from velaris_shared.db.models import Base
from velaris_shared.db.session import get_session
from hxmigrate_service.main import app

# Disable rate limiting in tests
from velaris_shared.middleware.rate_limit import RateLimitMiddleware
app.middleware_stack = None  # force rebuild
app.user_middleware = [m for m in app.user_middleware if m.cls is not RateLimitMiddleware]

# ── Single shared engine ──────────────────────────────────────────
# Default: in-memory SQLite. StaticPool keeps every connection bound to the SAME
# in-memory database (otherwise sqlite+aiosqlite gives each connection its own
# empty DB → phantom "no such table"/missing-row failures).
#
# OPT-IN Postgres harness: set VELARIS_TEST_DATABASE_URL (e.g.
# postgresql+asyncpg://helix:PW@localhost:5432/helix_test) to run against a real
# Postgres test DB — required for Postgres-only SQL (helix_case_seq nextval,
# information_schema, SET LOCAL statement_timeout, EXPLAIN ANALYZE). Use a
# DEDICATED test DB, never dev's `helix`: the suite truncates every table per test.
import os

# OPT-IN external-DB harness: VELARIS_TEST_DATABASE_URL points at a real Postgres OR
# MySQL test database; the dialect is read from the URL scheme. SQLite stays the
# default (zero external deps). DB SDK Phase 1 added the MySQL path.
EXTERNAL_TEST_URL = os.environ.get("VELARIS_TEST_DATABASE_URL")
PG_TEST_URL = EXTERNAL_TEST_URL  # back-compat alias (REQUIRES_PG markers read the env directly)
EXTERNAL_MODE = bool(EXTERNAL_TEST_URL)
_DIALECT = EXTERNAL_TEST_URL.split("://", 1)[0].split("+", 1)[0] if EXTERNAL_MODE else "sqlite"
PG_MODE = EXTERNAL_MODE and _DIALECT == "postgresql"
MYSQL_MODE = EXTERNAL_MODE and _DIALECT == "mysql"

if EXTERNAL_MODE:
    # The suite TRUNCATEs every table per test — refuse anything but a *_test DB.
    _db_name = EXTERNAL_TEST_URL.rsplit("/", 1)[-1].split("?", 1)[0]
    assert _db_name.endswith("_test") or os.environ.get("VELARIS_TEST_DB_CONFIRM"), (
        f"VELARIS_TEST_DATABASE_URL targets {_db_name!r}, not a *_test database. Refusing "
        "to run the (truncating) suite against it. Set VELARIS_TEST_DB_CONFIRM=1 to override."
    )
    # NullPool: pytest-asyncio uses a fresh event loop per test, but a pooled async
    # connection stays bound to the loop that created it → "attached to a different
    # loop" on the next test. NullPool opens a fresh connection per operation (bound
    # to the current loop) and closes it after, sidestepping the issue.
    from sqlalchemy.pool import NullPool
    # MySQL defaults to REPEATABLE READ; case-service assumes READ COMMITTED — match
    # the production MysqlBackend.engine_options so the harness has the same semantics.
    _engine_kw = {"isolation_level": "READ COMMITTED"} if MYSQL_MODE else {}
    engine = create_async_engine(EXTERNAL_TEST_URL, echo=False, poolclass=NullPool, **_engine_kw)
    if MYSQL_MODE:
        # The case-create path dispatches the case-number sequence on
        # get_settings().database_backend (PG nextval vs MySQL counter row). Align the
        # cached settings singleton with the engine's dialect, else it would call the
        # PG backend against MySQL and silently no-op (best-effort try/except).
        from velaris_shared.config import get_settings
        get_settings().database_backend = "mysql"
else:
    # A FILE-backed SQLite DB with NullPool — every session gets its OWN
    # connection, and therefore its own transaction.
    #
    # The old harness was `:memory:` + StaticPool, i.e. a single shared
    # connection: the test's `session` fixture and the request's session were
    # two AsyncSessions sitting on ONE transaction. A commit or rollback from
    # either moved the other's ground, each kept a stale identity map, and row
    # visibility ended up depending on which session last touched the
    # connection — that is, on test order. That is what produced the years of
    # intermittent "but it passes in isolation" failures.
    #
    # A file DB keeps the data visible across connections while giving each one
    # real isolation — the same semantics the service gets from Postgres in
    # production, which is the point: the harness should not be a special case.
    #
    # The cost of that honesty: a test may no longer `flush()` and expect the
    # API to see the row, and it must not hold a write transaction open across
    # a client call. Both now fail loudly instead of corrupting a later test.
    #
    # NullPool for the same reason the external harness uses it: pytest-asyncio
    # builds a fresh event loop per test and a pooled async connection stays
    # bound to the loop that created it.
    from sqlalchemy.pool import NullPool

    _TEST_DB_DIR = tempfile.mkdtemp(prefix="velaris-test-db-")
    atexit.register(shutil.rmtree, _TEST_DB_DIR, True)
    TEST_DB_URL = f"sqlite+aiosqlite:///{_TEST_DB_DIR}/helix_test.sqlite"
    engine = create_async_engine(
        TEST_DB_URL,
        echo=False,
        poolclass=NullPool,
        # SQLite's busy-wait: with separate connections a writer can briefly
        # hold the lock, and waiting beats erroring.
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_test_pragmas(dbapi_conn, _record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")   # readers don't block the writer
        cur.execute("PRAGMA synchronous=OFF")    # throwaway DB — durability is moot
        cur.close()
TestSessionFactory = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


async def override_get_session() -> AsyncGenerator[AsyncSession, None]:
    async with TestSessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


app.dependency_overrides[get_session] = override_get_session

# ── Route ALL four session pools at the single test factory ───────────────────
# case-service has four independent pools (operations / auth / analytics /
# replica). Overriding only the get_session FastAPI dependency leaves the other
# three — plus get_current_user's *direct* get_session_factory()() call — hitting
# real Postgres (the "split-brain" that produced spurious failures).
_db_session._session_factory = TestSessionFactory            # noqa: SLF001
_db_session._auth_session_factory = TestSessionFactory       # noqa: SLF001
_db_session._analytics_session_factory = TestSessionFactory  # noqa: SLF001
_db_session._replica_session_factory = TestSessionFactory    # noqa: SLF001

# In an external-DB mode also point the lazily-created engine globals at the test
# engine, so direct get_engine() callers (e.g. the regen background tasks) hit the
# test DB — not dev's real DB. (In SQLite mode those callers fail to connect and no-op.)
if EXTERNAL_MODE:
    _db_session._engine = engine            # noqa: SLF001
    _db_session._auth_engine = engine       # noqa: SLF001
    _db_session._analytics_engine = engine  # noqa: SLF001
    _db_session._replica_engine = engine    # noqa: SLF001


# ── Default admin auth ────────────────────────────────────────────────────────
# HxGuard ENFORCE requires a Bearer token on every protected endpoint. Mint one
# RS256/HS256 token from the SAME settings the app decodes with, so it validates
# regardless of whether RSA keys are configured. roles include admin+superadmin
# so require_role()/require_admin() pass. Tests needing the anonymous case use
# the `anon_client` fixture instead.
def _mint_admin_token() -> str:
    from velaris_shared.auth.jwt_handler import create_dev_token
    from velaris_shared.config import get_settings

    s = get_settings()
    return create_dev_token(
        user_id=str(uuid.uuid4()),
        username="test-admin",
        roles=["admin", "superadmin"],
        secret=s.auth_secret,
        private_key=s.auth_rsa_private_key or "",
    )


ADMIN_TOKEN = _mint_admin_token()
ADMIN_HEADERS = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


# ── Shared fixtures ───────────────────────────────────────────────

LIFECYCLE_PROCESS_ID = str(uuid.uuid4())


_external_schema_ready = False


async def _external_truncate_all() -> None:
    """Fast per-test isolation on an external DB — empty every table.

    Postgres: one multi-table TRUNCATE … RESTART IDENTITY CASCADE.
    MySQL: TRUNCATE is single-table and has no CASCADE, so disable FK checks for the
    duration and truncate each table (NullPool → one connection per `begin()` block,
    so the session-scoped SET applies to every statement here)."""
    from sqlalchemy import text
    async with engine.begin() as conn:
        if MYSQL_MODE:
            await conn.execute(text("SET FOREIGN_KEY_CHECKS=0"))
            for t in Base.metadata.sorted_tables:
                await conn.execute(text(f"TRUNCATE TABLE `{t.name}`"))
            await conn.execute(text("SET FOREIGN_KEY_CHECKS=1"))
        else:
            names = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
            await conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    """Isolate each test's data.

    SQLite: create_all before / drop_all after each test (fast on in-memory).
    External DB (Postgres/MySQL): build the schema ONCE, then TRUNCATE every table
    before each subsequent test (drop/create per test is far too slow against a real
    DB). Dialect-only objects (PG sequences/information_schema; MySQL FK-check toggles)
    work natively, which is the whole point of the opt-in external harness."""
    global _external_schema_ready
    if EXTERNAL_MODE:
        from sqlalchemy import text
        if not _external_schema_ready:
            async with engine.begin() as conn:
                if MYSQL_MODE:
                    # No DROP SCHEMA on MySQL; drop all known tables with FK checks off,
                    # then rebuild from the models. velaris_sequences (the portable case-
                    # number counter) is part of the metadata, so create_all makes it.
                    await conn.execute(text("SET FOREIGN_KEY_CHECKS=0"))
                    await conn.run_sync(Base.metadata.drop_all)
                    await conn.run_sync(Base.metadata.create_all)
                    await conn.execute(text("SET FOREIGN_KEY_CHECKS=1"))
                else:
                    # Wipe whatever is there (incl. any migration-applied schema with FKs
                    # the ORM metadata doesn't know) and rebuild cleanly from the models.
                    await conn.execute(text("DROP SCHEMA public CASCADE"))
                    await conn.execute(text("CREATE SCHEMA public"))
                    await conn.run_sync(Base.metadata.create_all)
                    await conn.execute(text("CREATE SEQUENCE IF NOT EXISTS helix_case_seq"))
            _external_schema_ready = True
        else:
            await _external_truncate_all()
        yield
        return
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def client():
    """Authenticated client — carries a default admin Bearer token.

    Per-request `headers=` override these defaults per key (httpx semantics),
    so role-denial tests that pass their own lower-privilege token still work.
    For the no-token case, use `anon_client`.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", headers=ADMIN_HEADERS
    ) as c:
        yield c


@pytest_asyncio.fixture
async def anon_client():
    """Unauthenticated client — no default Authorization header.

    For tests asserting that a missing token is rejected (401).
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test"
    ) as c:
        yield c


@pytest_asyncio.fixture
async def session():
    async with TestSessionFactory() as s:
        yield s


# ── Shared helpers ────────────────────────────────────────────────


async def deploy_case_type(client: AsyncClient, **overrides) -> dict:
    payload = {
        "name": overrides.get("name", "Test Case"),
        "version": overrides.get("version", "1.0.0"),
        "lifecycle_process_id": LIFECYCLE_PROCESS_ID,
        "definition_json": overrides.get("definition_json", {"stages": []}),
        "default_priority": overrides.get("default_priority", "medium"),
    }
    payload.update({k: v for k, v in overrides.items() if k not in payload})
    resp = await client.post("/api/v1/case-types", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def create_case(
    client: AsyncClient, case_type_id: str, **overrides
) -> dict:
    payload = {"case_type_id": case_type_id, "data": overrides.pop("data", {"foo": "bar"})}
    payload.update(overrides)
    resp = await client.post("/api/v1/cases", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ── Cross-test state isolation ────────────────────────────────────────
# Two module-level singletons leak state between tests when a test mutates
# them without cleanup: FastAPI dependency_overrides (a test overriding
# get_current_user and failing before its clear) and the lru_cached Settings
# instance (tests flip flags like hxguard_case_enforcement by attribute).
# Snapshot + restore both around EVERY test so ordering can't change results.

import pytest as _pytest


@_pytest.fixture(autouse=True)
def _isolate_dependency_overrides():
    saved = dict(app.dependency_overrides)
    yield
    app.dependency_overrides.clear()
    app.dependency_overrides.update(saved)


@_pytest.fixture(autouse=True)
def _isolate_settings():
    from velaris_shared.config import get_settings
    s = get_settings()
    saved = dict(s.__dict__)
    yield
    s.__dict__.clear()
    s.__dict__.update(saved)


# ── The account check (velaris_shared.auth.dependencies._account_gate) ────────
# Since 2026-10-23 a token is refused when its account row is missing, renamed
# or inactive. The harness mints tokens for ids and names that have no row
# (`_headers("alice", …)`), so the check is off here unless a test is marked
# `account_gate` — those run it for real, against real rows.

def pytest_configure(config):
    config.addinivalue_line("markers", "account_gate: run the real account check (rows must exist)")


@_pytest.fixture(autouse=True)
def _account_gate_off(request):
    # Set and restored by hand, NOT through `monkeypatch`: an autouse fixture
    # that requests it moves its teardown after the database fixture's, so a
    # test that patches `time.monotonic` left it patched while the async DB
    # teardown ran — a leaked connection per test, every later setup slower.
    from velaris_shared.auth import dependencies as _deps
    real = _deps._account_gate
    if request.node.get_closest_marker("account_gate") is None:
        async def _off(user, session):
            return None
        _deps._account_gate = _off
    yield
    _deps._account_gate = real


# The deletion ledger writes files (velaris_shared.identity.ledger): never into
# this checkout's .deletions/ from a test.
@_pytest.fixture(autouse=True)
def _deletion_ledger_in_tmp(tmp_path):
    # By hand, not `monkeypatch` — see _account_gate_off.
    from velaris_shared.config import get_settings
    from velaris_shared.identity import ledger as _ledger
    s = get_settings()
    real = s.deletion_ledger_dir
    s.deletion_ledger_dir = str(tmp_path / "deletions")
    _ledger.reset_cache_for_tests()
    yield
    s.deletion_ledger_dir = real
    _ledger.reset_cache_for_tests()
