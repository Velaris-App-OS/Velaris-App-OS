"""8g (2026-09-29) — outbound case-event webhooks.

Found live: every `/webhooks` route needed a login and nothing else. A VIEWER
created a subscription with no case type — which the relay delivers every
tenant's case events to — and `/test` posted to any URL, internal ones included.

Pinned: a privilege per route; a platform-wide subscription is the platform
operator's alone (create, clear the case type, read, edit, delete, test); a
tenant admin can still manage their own case type's subscriptions; and every
URL passes the SSRF check the relay already applied.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from velaris_shared.db.models import (
    CaseTypeModel, TenantModel, UserDirectoryModel, WebhookSubscriptionModel,
)

from case_service.main import app
from case_service.tenancy import identity as tenancy
from tests.conftest import LIFECYCLE_PROCESS_ID, TestSessionFactory  # type: ignore[attr-defined]
from tests.conftest import _headers  # type: ignore[attr-defined]

pytestmark = pytest.mark.asyncio

OPERATOR = _headers("op", ["admin"])           # default tenant: the install's operator
URL = "https://hooks.example.com/in"


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    """The SSRF check resolves hostnames; tests must not depend on real DNS."""
    import ipaddress

    from case_service.hxbridge import security

    def fake(hostname):
        if hostname.endswith("example.com"):
            return
        ip = ipaddress.ip_address(hostname) if hostname[0].isdigit() else None
        if ip is None or ip.is_private or ip.is_loopback or ip.is_link_local:
            raise ValueError(f"blocked: {hostname}")
    monkeypatch.setattr(security, "_resolve_and_check", fake)


@pytest_asyncio.fixture
async def api():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def world():
    tenancy.clear_cache()
    async with TestSessionFactory() as db:
        t = TenantModel(id=uuid.uuid4(), slug="default", name="Default")
        d = TenantModel(id=uuid.uuid4(), slug="dev", name="Dev")
        db.add_all([t, d])
        await db.flush()
        db.add(UserDirectoryModel(id=uuid.uuid4(), user_id="devadmin", tenant_id="dev"))
        mine = CaseTypeModel(id=uuid.uuid4(), name="Dev claims", version="1",
                             lifecycle_process_id=LIFECYCLE_PROCESS_ID,
                             definition_json={}, tenant_id=d.id)
        db.add(mine)
        wide = WebhookSubscriptionModel(id=uuid.uuid4(), name="platform", url=URL,
                                        events=["*"], case_type_id=None)
        db.add(wide)
        await db.commit()
    yield {"mine": mine, "wide": wide}
    tenancy.clear_cache()


DEV = _headers("devadmin", ["admin"])


async def test_a_viewer_can_do_nothing(api, world):
    v = _headers("v", ["viewer"])
    assert (await api.post("/api/v1/webhooks", headers=v,
                           json={"name": "x", "url": URL, "events": ["*"]})).status_code == 403
    assert (await api.get("/api/v1/webhooks", headers=v)).status_code == 403


async def test_only_the_operator_may_create_a_platform_wide_subscription(api, world):
    r = await api.post("/api/v1/webhooks", headers=DEV,
                       json={"name": "all", "url": URL, "events": ["*"]})
    assert r.status_code == 403 and "every tenant" in r.text
    r = await api.post("/api/v1/webhooks", headers=OPERATOR,
                       json={"name": "all", "url": URL, "events": ["*"]})
    assert r.status_code == 201, r.text


async def test_a_tenant_admin_manages_their_own_and_cannot_widen_it(api, world):
    r = await api.post("/api/v1/webhooks", headers=DEV, json={
        "name": "mine", "url": URL, "events": ["case.created"],
        "case_type_id": str(world["mine"].id)})
    assert r.status_code == 201, r.text
    wid = r.json()["id"]
    r = await api.patch(f"/api/v1/webhooks/{wid}", headers=DEV, json={"case_type_id": None})
    assert r.status_code == 403
    assert (await api.patch(f"/api/v1/webhooks/{wid}", headers=DEV,
                            json={"name": "renamed"})).status_code == 200


async def test_the_platform_wide_subscription_is_invisible_and_untouchable(api, world):
    wid = world["wide"].id
    names = [w["name"] for w in (await api.get("/api/v1/webhooks", headers=DEV)).json()]
    assert "platform" not in names
    for method, path, body in (
            ("GET", f"/api/v1/webhooks/{wid}", None),
            ("PATCH", f"/api/v1/webhooks/{wid}", {"url": "https://evil.example.com/x"}),
            ("DELETE", f"/api/v1/webhooks/{wid}", None),
            ("GET", f"/api/v1/webhooks/{wid}/deliveries", None),
            ("POST", f"/api/v1/webhooks/{wid}/test", None)):
        r = await api.request(method, path, headers=DEV, json=body)
        assert r.status_code == 404, (method, path, r.status_code)
    assert "platform" in [w["name"] for w in
                          (await api.get("/api/v1/webhooks", headers=OPERATOR)).json()]


@pytest.mark.parametrize("bad", ["http://127.0.0.1:8201/admin", "http://169.254.169.254/latest",
                                 "http://10.0.0.5/x", "file:///etc/passwd"])
async def test_internal_urls_are_refused(api, world, bad):
    r = await api.post("/api/v1/webhooks", headers=OPERATOR,
                       json={"name": "x", "url": bad, "events": ["*"]})
    assert r.status_code == 422, r.text


async def test_a_stored_internal_url_is_refused_at_test_time(api, world):
    async with TestSessionFactory() as db:
        old = WebhookSubscriptionModel(id=uuid.uuid4(), name="pre-8g", url="http://127.0.0.1:9/x",
                                       events=["*"], case_type_id=None)
        db.add(old)
        await db.commit()
    r = await api.post(f"/api/v1/webhooks/{old.id}/test", headers=OPERATOR)
    assert r.status_code == 422
