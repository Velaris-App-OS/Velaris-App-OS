"""(commerce-service copy, R5e step 7: the marketplace part of case-service's
test_tenant_records_8e.py.)

8e part b (2026-10-01) — tenant records get an owner.

Migration 213 gave eight tables a `tenant_id`. The isolation probe
(`test_tenant_isolation_probe.py`) proves another tenant cannot reach them by
id or in a list; this file pins the other half — that what a tenant CREATES is
recorded as its own, so the probe's protection applies to new rows at all —
and the one route that had to change shape: `POST /apps/package` snapshots
every tenant's configuration and is now the platform operator's.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from velaris_shared.db.models import (
    ArtifactBranchModel,
    ComponentCommitModel,
    GDPRRequestModel,
    TenantModel,
    UserDirectoryModel,
)

from tests.conftest import app  # the in-process gateway (both apps)
from case_service.tenancy import identity as tenancy
from tests.conftest import TestSessionFactory  # type: ignore[attr-defined]
from velaris_shared.auth.jwt_handler import create_dev_token
from velaris_shared.config import get_settings

pytestmark = pytest.mark.asyncio


# (was imported from test_payments_authz.py)
def _headers(user_id: str, roles: list[str]) -> dict[str, str]:
    s = get_settings()
    token = create_dev_token(user_id=user_id, username=user_id, roles=roles,
                             secret=s.auth_secret,
                             private_key=s.auth_rsa_private_key or "")
    return {"Authorization": f"Bearer {token}"}


DEV = _headers("devadmin", ["admin"])
OPS = _headers("defadmin", ["admin"])




@pytest_asyncio.fixture
async def api():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def world():
    tenancy.clear_cache()
    async with TestSessionFactory() as db:
        d = TenantModel(id=uuid.uuid4(), slug="default", name="Default")
        v = TenantModel(id=uuid.uuid4(), slug="dev", name="Dev")
        db.add_all([d, v])
        await db.flush()
        db.add(UserDirectoryModel(id=uuid.uuid4(), user_id="devadmin", tenant_id=str(v.id)))
        await db.commit()
    yield {"default": d, "dev": v}
    tenancy.clear_cache()


async def _get(model, rid):
    async with TestSessionFactory() as db:
        return await db.get(model, uuid.UUID(str(rid)))


async def test_marketplace_sources_are_the_operators(api, world):
    """8e (c): sources feed every tenant's catalogue — install-wide."""
    r = await api.post("/api/v1/marketplace/sources", headers=DEV,
                       json={"name": "evil", "url": "https://example.invalid/index.json",
                             "tier": "community"})
    assert r.status_code == 403, r.text
    assert "platform operator" in r.text
