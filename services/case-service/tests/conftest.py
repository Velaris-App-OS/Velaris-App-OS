"""Shared test fixtures for case-service.

Single SQLite engine, session factory, and app override used by
all test modules.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from typing import AsyncGenerator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

import case_service.db.session as _db_session
from case_service.db.models import Base
from case_service.db.session import get_session
from case_service.main import app

# Disable rate limiting in tests
from case_service.middleware.rate_limit import RateLimitMiddleware
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

PG_TEST_URL = os.environ.get("VELARIS_TEST_DATABASE_URL")
PG_MODE = bool(PG_TEST_URL)

if PG_MODE:
    assert "helix_test" in PG_TEST_URL or os.environ.get("VELARIS_TEST_DB_CONFIRM"), (
        "VELARIS_TEST_DATABASE_URL does not target a *_test database. Refusing to run "
        "the (truncating) suite against it. Set VELARIS_TEST_DB_CONFIRM=1 to override."
    )
    # NullPool: pytest-asyncio uses a fresh event loop per test, but a pooled asyncpg
    # connection stays bound to the loop that created it → "attached to a different
    # loop" on the next test. NullPool opens a fresh connection per operation (bound
    # to the current loop) and closes it after, sidestepping the issue.
    from sqlalchemy.pool import NullPool
    engine = create_async_engine(PG_TEST_URL, echo=False, poolclass=NullPool)
else:
    TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
    engine = create_async_engine(
        TEST_DB_URL,
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
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

# In Postgres mode also point the lazily-created engine globals at the test engine,
# so direct get_engine() callers (e.g. the regen background tasks) hit helix_test —
# not dev's real DB. (In SQLite mode those callers fail to connect and no-op.)
if PG_MODE:
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
    from case_service.auth.jwt_handler import create_dev_token
    from case_service.config import get_settings

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


_pg_schema_ready = False


async def _pg_truncate_all() -> None:
    """Fast per-test isolation on Postgres — empty every table (schema + the
    standalone helix_case_seq persist)."""
    from sqlalchemy import text
    names = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    """Isolate each test's data.

    SQLite: create_all before / drop_all after each test (fast on in-memory).
    Postgres: build the schema ONCE (create_all + helix_case_seq), then TRUNCATE
    every table before each subsequent test (drop/create per test is far too slow
    against a real DB). Postgres-only objects (sequences, information_schema) work
    natively, which is the whole point of the opt-in PG harness."""
    global _pg_schema_ready
    if PG_MODE:
        from sqlalchemy import text
        if not _pg_schema_ready:
            async with engine.begin() as conn:
                # Wipe whatever is there (incl. any migration-applied schema with FKs
                # the ORM metadata doesn't know) and rebuild cleanly from the models.
                await conn.execute(text("DROP SCHEMA public CASCADE"))
                await conn.execute(text("CREATE SCHEMA public"))
                await conn.run_sync(Base.metadata.create_all)
                await conn.execute(text("CREATE SEQUENCE IF NOT EXISTS helix_case_seq"))
            _pg_schema_ready = True
        else:
            await _pg_truncate_all()
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
