"""The install's audit chain routes are the platform operator's (2026-11-06).

Found securing the sealer fix: `/compliance/audit/status|verify|seal|anchors|anchor`
passed any `admin` — every tenant admin, demo sign-ups included. One chain
covers the whole install: its counts, its breaks (other tenants' audit-row ids),
a full verification (61 s at 1.6M links, vx-load) and an outside timestamp call
are not a tenant's. A tenant admin is refused here; the operator (default
tenant) passes; a tenant's evidence pack still reports whether the chain verifies,
from one shared run at most 5 min old.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient

from case_service.compliance import audit_chain as ac
from case_service.main import app
from case_service.tenancy import identity as tenancy
from tests.conftest import TestSessionFactory  # type: ignore[attr-defined]
from velaris_shared.auth.jwt_handler import create_dev_token
from velaris_shared.config import get_settings
from velaris_shared.db.models import TenantModel, UserDirectoryModel

pytestmark = pytest.mark.asyncio

ACME = uuid.UUID("00000000-0000-0000-0000-0000000ac0de")
ROUTES = [("GET", "/api/v1/compliance/audit/status"),
          ("GET", "/api/v1/compliance/audit/verify"),
          ("POST", "/api/v1/compliance/audit/seal"),
          ("GET", "/api/v1/compliance/audit/anchors"),
          ("POST", "/api/v1/compliance/audit/anchor")]


def _h(user_id: str, roles=("admin",)) -> dict[str, str]:
    s = get_settings()
    token = create_dev_token(user_id=user_id, username=user_id, roles=list(roles),
                             secret=s.auth_secret, private_key=s.auth_rsa_private_key or "")
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def api():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def people():
    tenancy.clear_cache()
    ids = {"tenant_admin": f"ann-{uuid.uuid4().hex[:6]}", "operator": f"op-{uuid.uuid4().hex[:6]}"}
    async with TestSessionFactory() as db:
        await db.execute(sa.text("DELETE FROM tenants WHERE slug = 'acmechain'"))
        if not (await db.execute(sa.text("SELECT 1 FROM tenants WHERE slug = 'default'"))).first():
            db.add(TenantModel(id=uuid.uuid4(), slug="default", name="Default"))
        db.add(TenantModel(id=ACME, slug="acmechain", name="Acme chain"))
        db.add(UserDirectoryModel(id=uuid.uuid4(), user_id=ids["tenant_admin"], tenant_id="acmechain"))
        db.add(UserDirectoryModel(id=uuid.uuid4(), user_id=ids["operator"], tenant_id="default"))
        await db.commit()
    return ids


@pytest.mark.parametrize("method,path", ROUTES)
async def test_a_tenant_admin_is_refused(api, people, method, path):
    r = await api.request(method, path, headers=_h(people["tenant_admin"]))
    assert r.status_code == 403, r.text


@pytest.mark.parametrize("method,path", [r for r in ROUTES if r[1] != "/api/v1/compliance/audit/anchor"])
async def test_the_operator_passes(api, people, method, path):
    r = await api.request(method, path, headers=_h(people["operator"]))
    assert r.status_code == 200, r.text


async def test_a_viewer_is_refused_before_the_tenant_check(api, people):
    r = await api.get("/api/v1/compliance/audit/verify",
                      headers=_h(people["operator"], roles=("viewer",)))
    assert r.status_code == 403


async def test_tenant_packs_share_one_recent_verification(session, monkeypatch):
    ac._reset_cache_for_tests()
    runs = 0
    real = ac.verify_chain

    async def counting(s, limit=None):
        nonlocal runs
        runs += 1
        return await real(s, limit=limit)

    monkeypatch.setattr(ac, "verify_chain", counting)
    a = await ac.verify_chain_recent(session)
    b = await ac.verify_chain_recent(session)
    assert runs == 1 and a is b and a["verified_at"]
    c = await ac.verify_chain_recent(session, max_age_s=0)
    assert runs == 2 and c is not a
    ac._reset_cache_for_tests()
