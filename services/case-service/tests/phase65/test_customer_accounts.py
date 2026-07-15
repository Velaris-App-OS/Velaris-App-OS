"""P65 — Customer Accounts: registration, OTP login, profile, admin management.

Pins the 2026-07-10 un-hide fixes: routes mounted under /portal/{slug} (the
prefix the Studio portal calls), portal activation read from
tenant.settings["portal"]["enabled"] (no portal_enabled column on tenants),
tenant-scoped historical case auto-link, slug-bound customer tokens, and
admin-role-gated customer management.

Copyright (c) 2024-2026 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import update

from case_service.db.models import CaseInstanceModel, CaseTypeModel, TenantModel

PORTAL = "/api/v1/portal"


@pytest.fixture(autouse=True)
def _enable_customer_accounts():
    """The router is gated on the `customer_accounts` release flag cache."""
    from case_service.api.routers.releases import _ENABLED_VERSIONS
    prior = _ENABLED_VERSIONS.get("customer_accounts")
    _ENABLED_VERSIONS["customer_accounts"] = "v1.0.1"
    yield
    if prior is None:
        _ENABLED_VERSIONS.pop("customer_accounts", None)
    else:
        _ENABLED_VERSIONS["customer_accounts"] = prior


@pytest_asyncio.fixture
async def tenant(session) -> TenantModel:
    t = TenantModel(
        slug="acme", name="ACME Corp",
        settings={"portal": {"enabled": True}},
    )
    session.add(t); await session.commit(); return t


@pytest_asyncio.fixture
async def other_tenant(session) -> TenantModel:
    t = TenantModel(
        slug="globex", name="Globex",
        settings={"portal": {"enabled": True}},
    )
    session.add(t); await session.commit(); return t


@pytest_asyncio.fixture
async def disabled_tenant(session) -> TenantModel:
    t = TenantModel(slug="no-portal", name="No Portal", settings={})
    session.add(t); await session.commit(); return t


def _capture_otp():
    """Patch the OTP mailer and capture the code it would have sent."""
    sent: list[str] = []

    async def _fake(session, to_email, otp, purpose="login"):
        sent.append(otp)

    return sent, patch(
        "case_service.api.routers.portal_customers._send_otp_email",
        AsyncMock(side_effect=_fake),
    )


async def _register_and_login(client, slug: str, email: str) -> tuple[str, dict]:
    """Register a customer, complete OTP verification, return (token, customer)."""
    sent, patcher = _capture_otp()
    with patcher:
        r = await client.post(f"{PORTAL}/{slug}/auth/register",
                              json={"email": email, "display_name": "Jane Doe"})
        assert r.status_code == 200, r.text
    r2 = await client.post(f"{PORTAL}/{slug}/auth/verify-otp",
                           json={"email": email, "otp": sent[-1]})
    assert r2.status_code == 200, r2.text
    out = r2.json()
    return out["customer_token"], out["customer"]


def _cust_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestPortalGate:
    async def test_routes_mounted_under_portal_prefix(self, client, tenant):
        sent, patcher = _capture_otp()
        with patcher:
            r = await client.post(f"{PORTAL}/acme/auth/register",
                                  json={"email": "a@x.com", "display_name": "A"})
        assert r.status_code == 200
        assert len(sent) == 1

    async def test_unknown_slug_404(self, client):
        r = await client.post(f"{PORTAL}/nope/auth/register",
                              json={"email": "a@x.com", "display_name": "A"})
        assert r.status_code == 404

    async def test_portal_disabled_tenant_404(self, client, disabled_tenant):
        r = await client.post(f"{PORTAL}/no-portal/auth/register",
                              json={"email": "a@x.com", "display_name": "A"})
        assert r.status_code == 404


class TestOTPFlow:
    async def test_register_verify_and_get_account(self, client, tenant):
        token, cust = await _register_and_login(client, "acme", "jane@example.com")
        r = await client.get(f"{PORTAL}/acme/account", headers=_cust_headers(token))
        assert r.status_code == 200
        body = r.json()
        assert body["primary_email"] == "jane@example.com"
        assert body["verified"] is True

    async def test_wrong_otp_401(self, client, tenant):
        sent, patcher = _capture_otp()
        with patcher:
            await client.post(f"{PORTAL}/acme/auth/register",
                              json={"email": "jane@example.com", "display_name": "Jane"})
        r = await client.post(f"{PORTAL}/acme/auth/verify-otp",
                              json={"email": "jane@example.com", "otp": "000000"})
        assert r.status_code == 401

    async def test_otp_single_use(self, client, tenant):
        sent, patcher = _capture_otp()
        with patcher:
            await client.post(f"{PORTAL}/acme/auth/register",
                              json={"email": "jane@example.com", "display_name": "Jane"})
        otp = sent[-1]
        assert (await client.post(f"{PORTAL}/acme/auth/verify-otp",
                                  json={"email": "jane@example.com", "otp": otp})).status_code == 200
        assert (await client.post(f"{PORTAL}/acme/auth/verify-otp",
                                  json={"email": "jane@example.com", "otp": otp})).status_code == 401

    async def test_expired_otp_401(self, client, session, tenant):
        from case_service.db.models import PortalCustomerModel
        sent, patcher = _capture_otp()
        with patcher:
            await client.post(f"{PORTAL}/acme/auth/register",
                              json={"email": "jane@example.com", "display_name": "Jane"})
        await session.execute(
            update(PortalCustomerModel)
            .where(PortalCustomerModel.primary_email == "jane@example.com")
            .values(otp_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
        )
        await session.commit()
        r = await client.post(f"{PORTAL}/acme/auth/verify-otp",
                              json={"email": "jane@example.com", "otp": sent[-1]})
        assert r.status_code == 401

    async def test_request_otp_unknown_email_no_enumeration(self, client, tenant):
        r = await client.post(f"{PORTAL}/acme/auth/request-otp",
                              json={"email": "ghost@example.com"})
        assert r.status_code == 200
        assert r.json()["ok"] is True


class TestTokenScoping:
    async def test_token_rejected_on_other_portal(self, client, tenant, other_tenant):
        token, _ = await _register_and_login(client, "acme", "jane@example.com")
        r = await client.get(f"{PORTAL}/globex/account", headers=_cust_headers(token))
        assert r.status_code == 401

    async def test_garbage_token_401(self, client, tenant):
        r = await client.get(f"{PORTAL}/acme/account",
                             headers=_cust_headers("not-a-jwt"))
        assert r.status_code == 401


class TestHistoricalCaseLink:
    async def test_auto_link_is_tenant_scoped(self, client, session, tenant, other_tenant):
        ct = CaseTypeModel(name="Support", version="1.0", definition_json={"stages": []},
                           portal_enabled=True)
        session.add(ct); await session.flush()
        mine = CaseInstanceModel(
            case_type_id=ct.id, case_type_version="1.0", status="open", priority="medium",
            tenant_id=tenant.id, portal_submitter_email="jane@example.com",
            data={"subject": "Mine"}, created_by="portal:jane@example.com",
        )
        theirs = CaseInstanceModel(
            case_type_id=ct.id, case_type_version="1.0", status="open", priority="medium",
            tenant_id=other_tenant.id, portal_submitter_email="jane@example.com",
            data={"subject": "Theirs"}, created_by="portal:jane@example.com",
        )
        session.add_all([mine, theirs]); await session.commit()

        token, _ = await _register_and_login(client, "acme", "jane@example.com")
        r = await client.get(f"{PORTAL}/acme/account/cases", headers=_cust_headers(token))
        assert r.status_code == 200
        case_ids = {c["case_id"] for c in r.json()["cases"]}
        assert str(mine.id) in case_ids
        assert str(theirs.id) not in case_ids


class TestAdminCustomerManagement:
    async def test_admin_lists_customers(self, client, tenant):
        await _register_and_login(client, "acme", "jane@example.com")
        r = await client.get(f"{PORTAL}/acme/customers")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 1
        assert body["customers"][0]["primary_email"] == "jane@example.com"

    async def test_non_admin_403(self, client, tenant):
        from case_service.auth.jwt_handler import create_dev_token
        from case_service.config import get_settings
        s = get_settings()
        viewer = create_dev_token(
            user_id=str(uuid.uuid4()), username="viewer", roles=["viewer"],
            secret=s.auth_secret, private_key=s.auth_rsa_private_key or "",
        )
        r = await client.get(f"{PORTAL}/acme/customers", headers=_cust_headers(viewer))
        assert r.status_code == 403

    async def test_admin_delete_anonymises(self, client, session, tenant):
        _, cust = await _register_and_login(client, "acme", "jane@example.com")
        r = await client.delete(f"{PORTAL}/acme/customers/{cust['id']}")
        assert r.status_code == 200
        detail = (await client.get(f"{PORTAL}/acme/customers/{cust['id']}")).json()
        assert detail["display_name"] == "Deleted User"
        assert detail["primary_email"].startswith("deleted_")

    async def test_admin_customer_cross_tenant_404(self, client, tenant, other_tenant):
        _, cust = await _register_and_login(client, "acme", "jane@example.com")
        r = await client.get(f"{PORTAL}/globex/customers/{cust['id']}")
        assert r.status_code == 404


class TestFeatureFlagGate:
    async def test_flag_off_404(self, client, tenant):
        from case_service.api.routers.releases import _ENABLED_VERSIONS
        _ENABLED_VERSIONS.pop("customer_accounts", None)
        r = await client.post(f"{PORTAL}/acme/auth/register",
                              json={"email": "a@x.com", "display_name": "A"})
        assert r.status_code == 404
