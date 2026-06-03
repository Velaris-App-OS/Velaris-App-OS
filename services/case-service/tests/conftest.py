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

from case_service.db.models import Base
from case_service.db.session import get_session
from case_service.main import app

# Disable rate limiting in tests
from case_service.middleware.rate_limit import RateLimitMiddleware
app.middleware_stack = None  # force rebuild
app.user_middleware = [m for m in app.user_middleware if m.cls is not RateLimitMiddleware]

# ── Single shared engine ──────────────────────────────────────────

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
engine = create_async_engine(TEST_DB_URL, echo=False)
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


# ── Shared fixtures ───────────────────────────────────────────────

LIFECYCLE_PROCESS_ID = str(uuid.uuid4())


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    """Create tables before each test, drop after."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def client():
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
