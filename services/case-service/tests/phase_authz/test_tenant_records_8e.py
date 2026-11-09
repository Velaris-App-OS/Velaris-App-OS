"""8e part b (2026-10-01) — tenant records get an owner.

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

from case_service.main import app
from case_service.tenancy import identity as tenancy
from tests.conftest import TestSessionFactory  # type: ignore[attr-defined]
from velaris_shared.auth.jwt_handler import create_dev_token
from velaris_shared.config import get_settings

pytestmark = pytest.mark.asyncio


# (was imported from test_payments_authz.py, which moved to commerce-service)
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


async def test_a_branch_is_its_creators_tenants(api, world):
    r = await api.post("/api/v1/branches", headers=DEV,
                       json={"name": "b1", "branch_type": "app"})
    assert r.status_code == 201, r.text
    assert (await _get(ArtifactBranchModel, r.json()["id"])).tenant_id == world["dev"].id

    # …and the operator (default tenant) does not see it in its list.
    listed = (await api.get("/api/v1/branches", headers=OPS)).json()["branches"]
    assert r.json()["id"] not in [b["id"] for b in listed]


async def test_a_commit_is_its_creators_tenants(api, world):
    r = await api.post("/api/v1/commits", headers=DEV, json={
        "component_type": "form", "component_id": str(uuid.uuid4()),
        "component_name": "f", "commit_message": "a commit message"})
    assert r.status_code == 201, r.text
    assert (await _get(ComponentCommitModel, r.json()["id"])).tenant_id == world["dev"].id


async def test_a_gdpr_request_is_its_creators_tenants(api, world):
    r = await api.post("/api/v1/enterprise/gdpr/requests", headers=DEV, json={
        "subject_id": "someone", "request_type": "export"})
    assert r.status_code == 201, r.text
    rid = r.json().get("id") or r.json().get("request_id")
    assert (await _get(GDPRRequestModel, rid)).tenant_id == world["dev"].id


async def test_a_whole_platform_package_is_the_operators(api, world):
    r = await api.post("/api/v1/apps/package", headers=DEV,
                       json={"name": "snap", "version": "1.0.0"})
    assert r.status_code == 403, r.text
    r = await api.post("/api/v1/apps/package", headers=OPS,
                       json={"name": "snap", "version": "1.0.0"})
    assert r.status_code == 201, r.text
