"""8h (2026-09-29) — portal admin routes trusted the tenant in the URL.

Found live: a `dev`-tenant admin listed the `default` tenant's portal customers
by typing its slug; the same routes create, read and ERASE them, and two more
rewrote another tenant's portal settings and logo.

Pinned: portal customers are their tenant's staff's (or a superadmin's) and
nobody else's; portal configuration is its tenant's staff's or the platform
operator's; every refusal is a 404, so a slug says nothing.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from velaris_shared.db.models import PortalCustomerModel, TenantModel, UserDirectoryModel

from case_service.main import app
from case_service.tenancy import identity as tenancy
from tests.conftest import TestSessionFactory  # type: ignore[attr-defined]
from tests.conftest import _headers  # type: ignore[attr-defined]

pytestmark = pytest.mark.asyncio

DEV = _headers("devadmin", ["admin"])
OWN = _headers("defadmin", ["admin"])            # default tenant = platform operator
SUPER = _headers("root", ["admin", "superadmin"])


@pytest.fixture(autouse=True)
def _customer_accounts_on():
    from case_service.api.routers.releases import _ENABLED_VERSIONS
    prior = _ENABLED_VERSIONS.get("customer_accounts")
    _ENABLED_VERSIONS["customer_accounts"] = "v1.0.1"
    yield
    if prior is None:
        _ENABLED_VERSIONS.pop("customer_accounts", None)
    else:
        _ENABLED_VERSIONS["customer_accounts"] = prior


@pytest_asyncio.fixture
async def api():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def world():
    tenancy.clear_cache()
    async with TestSessionFactory() as db:
        d = TenantModel(id=uuid.uuid4(), slug="default", name="Default",
                        settings={"portal": {"enabled": True}})
        v = TenantModel(id=uuid.uuid4(), slug="dev", name="Dev",
                        settings={"portal": {"enabled": True}})
        db.add_all([d, v])
        await db.flush()
        db.add(UserDirectoryModel(id=uuid.uuid4(), user_id="devadmin", tenant_id="dev"))
        pc = PortalCustomerModel(id=uuid.uuid4(), tenant_id=d.id, primary_email="pat@x.test",
                                 display_name="Pat Customer")
        db.add(pc)
        await db.commit()
    yield {"customer": pc}
    tenancy.clear_cache()


async def test_another_tenants_portal_customers_are_unreachable(api, world):
    cid = world["customer"].id
    for method, path, body in (
            ("GET", "/api/v1/portal/default/customers", None),
            ("GET", f"/api/v1/portal/default/customers/{cid}", None),
            ("DELETE", f"/api/v1/portal/default/customers/{cid}", None),
            ("POST", "/api/v1/portal/default/customers",
             {"email": "planted@x.test", "display_name": "Planted"})):
        r = await api.request(method, path, headers=DEV, json=body)
        assert r.status_code == 404, (method, path, r.status_code, r.text)
    async with TestSessionFactory() as db:
        pc = await db.get(PortalCustomerModel, cid)
    assert pc.display_name == "Pat Customer"                      # not erased


async def test_own_staff_and_superadmin_still_manage_customers(api, world):
    r = await api.get("/api/v1/portal/default/customers", headers=OWN)
    assert r.status_code == 200 and "pat@x.test" in r.text
    assert (await api.get("/api/v1/portal/dev/customers", headers=SUPER)).status_code == 200
    assert (await api.get("/api/v1/portal/dev/customers", headers=DEV)).status_code == 200


async def test_portal_configuration_is_its_tenants_or_the_operators(api, world):
    r = await api.patch("/api/v1/portal-admin/tenants/default", headers=DEV,
                        json={"welcome_text": "defaced"})
    assert r.status_code == 404
    assert (await api.patch("/api/v1/portal-admin/tenants/dev", headers=DEV,
                            json={"welcome_text": "ours"})).status_code == 200
    assert (await api.patch("/api/v1/portal-admin/tenants/dev", headers=OWN,
                            json={"welcome_text": "operator"})).status_code == 200
    r = await api.post("/api/v1/portal-admin/tenants/default/logo", headers=DEV,
                       files={"file": ("l.png", b"\x89PNG\r\n\x1a\n", "image/png")})
    assert r.status_code == 404
