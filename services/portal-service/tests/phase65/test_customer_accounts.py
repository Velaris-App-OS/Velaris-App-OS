"""P65 — Customer Accounts: registration, OTP login, profile, admin management.

Pins the 2026-07-10 un-hide fixes: routes mounted under /portal/{slug} (the
prefix the Studio portal calls), portal activation read from
tenant.settings["portal"]["enabled"] (no portal_enabled column on tenants),
tenant-scoped historical case auto-link, slug-bound customer tokens, and
admin-role-gated customer management.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select, update

from velaris_shared.db.models import CaseInstanceModel, CaseTypeModel, TenantModel

PORTAL = "/api/v1/portal"


def _plant_known_otp(customer, code: str = "424242") -> str:
    """Give a customer a code this test knows.

    The stored value is a SHA-256 hash and the plaintext is only ever emailed,
    so a test that needs to *use* a code has to plant one.
    """
    import hashlib
    customer.otp_code = hashlib.sha256(code.encode()).hexdigest()
    customer.otp_expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
    customer.otp_attempts = 0
    return code


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
        "portal_service.api.routers.portal_customers._send_otp_email",
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
        from velaris_shared.db.models import PortalCustomerModel
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
        session.add(ct); await session.commit()
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
        from velaris_shared.auth.jwt_handler import create_dev_token
        from velaris_shared.config import get_settings
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


class TestSelfRegistrationToggle:
    """A portal may decide the public cannot create its own accounts.

    Added 2026-08-19 with the mobile app: the sign-in screen has to know whether
    to offer registration, and until this there was **no way for staff to create
    a customer at all** — the public register route was the only code path that
    made a `portal_customers` row, so switching self-registration off would have
    meant nobody could have an account.
    """

    async def test_it_is_open_unless_a_portal_says_otherwise(self, client, tenant):
        # The setting arrived after self-registration was already live. A
        # missing key must not silently close a door that was open.
        r = await client.get(f"{PORTAL}/acme")
        assert r.status_code == 200
        assert r.json()["self_registration"] is True

    async def test_the_portal_publishes_the_setting(self, client, session, tenant):
        tenant.settings = {"portal": {"enabled": True, "self_registration": False}}
        await session.commit()
        assert (await client.get(f"{PORTAL}/acme")).json()["self_registration"] is False

    async def test_registration_is_refused_when_it_is_off(self, client, session, tenant):
        tenant.settings = {"portal": {"enabled": True, "self_registration": False}}
        await session.commit()
        r = await client.post(f"{PORTAL}/acme/auth/register", json={
            "email": "nope@example.com", "display_name": "Nope",
        })
        # Hiding the control in the app is presentation; this is the control.
        assert r.status_code == 403

    async def test_staff_can_still_create_one_when_it_is_off(self, client, session, tenant):
        tenant.settings = {"portal": {"enabled": True, "self_registration": False}}
        await session.commit()
        r = await client.post(f"{PORTAL}/acme/customers", json={
            "email": "Made.By.Staff@Example.com", "display_name": "Made By Staff",
        })
        assert r.status_code == 201, r.text
        assert r.json()["primary_email"] == "made.by.staff@example.com"
        assert r.json()["invited"] is False
        listed = (await client.get(f"{PORTAL}/acme/customers")).json()
        assert listed["total"] == 1

    async def test_staff_creation_adopts_the_cases_that_person_already_sent(
        self, client, session, tenant,
    ):
        # The public route links historical cases by email; a staff-created
        # account that skipped it would show an empty history to someone who
        # has been submitting for months.
        ct = CaseTypeModel(name="Support", version="1.0", definition_json={"stages": []},
                           portal_enabled=True)
        session.add(ct)
        await session.commit()
        session.add(CaseInstanceModel(
            tenant_id=tenant.id, case_type_id=ct.id, case_type_version="1.0",
            status="open", priority="medium", data={},
            portal_submitter_email="early@example.com",
        ))
        await session.commit()

        r = await client.post(f"{PORTAL}/acme/customers", json={
            "email": "early@example.com", "display_name": "Early Bird",
        })
        assert r.status_code == 201
        assert r.json()["linked_cases"] == 1

    async def test_staff_creation_refuses_a_duplicate(self, client, tenant):
        body = {"email": "dupe@example.com", "display_name": "Dupe"}
        assert (await client.post(f"{PORTAL}/acme/customers", json=body)).status_code == 201
        assert (await client.post(f"{PORTAL}/acme/customers", json=body)).status_code == 409

    async def test_staff_creation_is_gated(self, client, tenant):
        from velaris_shared.auth.jwt_handler import create_dev_token
        from velaris_shared.config import get_settings
        s = get_settings()
        viewer = create_dev_token(
            user_id=str(uuid.uuid4()), username="viewer", roles=["viewer"],
            secret=s.auth_secret, private_key=s.auth_rsa_private_key or "",
        )
        r = await client.post(
            f"{PORTAL}/acme/customers",
            json={"email": "x@example.com", "display_name": "X"},
            headers=_cust_headers(viewer),
        )
        assert r.status_code == 403

        # A viewer is refused by the read action too, so the 403 above cannot
        # tell a write gate from a read gate. Read the gate off the BUILT route
        # rather than off a hand-kept map, which does not change when the
        # decorator does.
        from portal_service.main import app as fastapi_app   # R5e step 8
        # `Depends(...)` in the signature lands in `r.dependant.dependencies`;
        # `r.dependencies` only holds router- and decorator-level ones.
        gates = [
            getattr(d.call, "hxguard_action", None)
            for r in fastapi_app.routes
            if getattr(r, "path", "") == "/api/v1/portal/{slug}/customers"
            and "POST" in getattr(r, "methods", set())
            for d in r.dependant.dependencies
        ]
        assert "portal.customers.write" in gates, gates


class TestTheCodeCannotBeGuessed:
    """A six-digit code is 1,000,000 possibilities and `verify-otp` is anonymous.

    Measured against the live service on 2026-08-19, before this existed:
    **30 wrong codes in 0.1 seconds, every one a plain 401, no throttle and no
    lockout.** At that rate the whole space falls inside the code's own
    ten-minute life and the account is taken over silently. The password path
    already had this control; the code path did not.
    """

    async def test_five_wrong_guesses_retire_the_code(self, client, session, tenant):
        from velaris_shared.db.models import PortalCustomerModel
        await _register_and_login(client, "acme", "jane@example.com")
        # a fresh code, so the count starts clean
        await client.post(f"{PORTAL}/acme/auth/request-otp", json={"email": "jane@example.com"})

        for _ in range(5):
            r = await client.post(f"{PORTAL}/acme/auth/verify-otp",
                                  json={"email": "jane@example.com", "otp": "000000"})
            assert r.status_code == 401

        cust = (await session.execute(
            select(PortalCustomerModel).where(
                PortalCustomerModel.primary_email == "jane@example.com")
        )).scalar_one()
        await session.refresh(cust)
        assert cust.otp_code is None, "the code must be retired, not merely counted"

    async def test_a_wrong_guess_answers_the_same_whether_or_not_it_burned_the_code(
        self, client, tenant,
    ):
        # Telling a guesser they exhausted the code tells them the code was live.
        await _register_and_login(client, "acme", "jane@example.com")
        await client.post(f"{PORTAL}/acme/auth/request-otp", json={"email": "jane@example.com"})
        seen = set()
        for _ in range(6):
            r = await client.post(f"{PORTAL}/acme/auth/verify-otp",
                                  json={"email": "jane@example.com", "otp": "000000"})
            seen.add((r.status_code, r.json().get("detail")))
        assert len(seen) == 1, f"the answer changes and leaks state: {seen}"

    async def test_the_counter_is_not_a_read_modify_write(self, client, session, tenant):
        """Parallel guesses must not each read the same starting count.

        Measured before the fix: **eight concurrent wrong guesses left
        `otp_attempts` at 1 and the code alive** — each request read 0 and wrote
        1, so a concurrent attacker walked straight past the control while the
        sequential test still passed. Asserted on the statements rather than by
        racing the DB, because a timing test that passes once proves nothing.
        """
        import inspect
        from portal_service.api.routers import portal_customers as pc
        src = inspect.getsource(pc.portal_verify_otp)
        assert "update(PortalCustomerModel)" in src, \
            "the increment must be a statement the database evaluates"
        assert "PortalCustomerModel.otp_attempts + 1" in src, \
            "the new value must be computed from the column, not from Python"
        assert "PortalCustomerModel.otp_attempts >= _OTP_MAX_ATTEMPTS" in src, \
            "retirement must be conditional on the value AFTER the increment"
        assert "customer.otp_attempts = (customer.otp_attempts" not in src, \
            "a read-modify-write is exactly what the race exploited"

    async def test_a_typo_does_not_cost_the_code(self, client, session, tenant):
        # The control must bound an attacker without punishing the owner.
        from velaris_shared.db.models import PortalCustomerModel
        _, _ = await _register_and_login(client, "acme", "jane@example.com")
        cust = (await session.execute(
            select(PortalCustomerModel).where(
                PortalCustomerModel.primary_email == "jane@example.com")
        )).scalar_one()

        await client.post(f"{PORTAL}/acme/auth/request-otp", json={"email": "jane@example.com"})
        await session.refresh(cust)
        code = _plant_known_otp(cust)
        await session.commit()

        assert (await client.post(f"{PORTAL}/acme/auth/verify-otp",
                                  json={"email": "jane@example.com", "otp": "111111"})).status_code == 401
        r = await client.post(f"{PORTAL}/acme/auth/verify-otp",
                              json={"email": "jane@example.com", "otp": code})
        assert r.status_code == 200, "one wrong guess must not lock the owner out"

    async def test_a_new_code_starts_with_its_full_allowance(self, client, session, tenant):
        from velaris_shared.db.models import PortalCustomerModel
        await _register_and_login(client, "acme", "jane@example.com")
        cust = (await session.execute(
            select(PortalCustomerModel).where(
                PortalCustomerModel.primary_email == "jane@example.com")
        )).scalar_one()
        cust.otp_attempts = 4
        await session.commit()

        await client.post(f"{PORTAL}/acme/auth/request-otp", json={"email": "jane@example.com"})
        await session.refresh(cust)
        assert cust.otp_attempts == 0, "a reissued code must not inherit the old count"


class TestThePortalShowsOnlyItsOwnTenant:
    """`portal_enabled` says a case type is customer-facing, not *whose*.

    Found 2026-08-19: the public, unauthenticated portal config listed another
    tenant's case type by name, and `submit` accepted it — a tenant boundary
    crossed from an endpoint needing no credentials at all.
    """

    async def test_the_config_does_not_list_another_tenants_case_type(
        self, client, session, tenant, other_tenant,
    ):
        theirs = CaseTypeModel(name="Their Private Flow", version="1.0",
                               definition_json={"stages": []}, portal_enabled=True,
                               tenant_id=other_tenant.id)
        shared = CaseTypeModel(name="Platform Shared", version="1.0",
                               definition_json={"stages": []}, portal_enabled=True)
        session.add_all([theirs, shared])
        await session.commit()

        body = (await client.get(f"{PORTAL}/acme")).json()
        names = {ct["name"] for ct in body["case_types"]}
        assert "Their Private Flow" not in names, "another tenant's case type is on a public page"
        # A NULL tenant_id is platform-shared and stays visible — same_tenant's
        # documented rule, not an exception invented here.
        assert "Platform Shared" in names

    async def test_submitting_another_tenants_case_type_is_refused(
        self, client, session, tenant, other_tenant,
    ):
        theirs = CaseTypeModel(name="Their Private Flow", version="1.0",
                               definition_json={"stages": []}, portal_enabled=True,
                               tenant_id=other_tenant.id)
        session.add(theirs)
        await session.commit()

        r = await client.post(f"{PORTAL}/acme/submit", json={
            "case_type_id": str(theirs.id), "submitter_name": "X",
            "submitter_email": "x@example.com", "subject": "s", "description": "d",
        })
        assert r.status_code == 400, r.text

    async def test_the_form_of_another_tenants_case_type_is_refused(
        self, client, session, tenant, other_tenant,
    ):
        theirs = CaseTypeModel(name="Their Private Flow", version="1.0",
                               definition_json={"stages": []}, portal_enabled=True,
                               tenant_id=other_tenant.id)
        session.add(theirs)
        await session.commit()
        r = await client.get(f"{PORTAL}/acme/case-types/{theirs.id}/form")
        assert r.status_code in (400, 404), r.text


class TestFeatureFlagGate:
    async def test_flag_off_404(self, client, tenant):
        from case_service.api.routers.releases import _ENABLED_VERSIONS
        _ENABLED_VERSIONS.pop("customer_accounts", None)
        r = await client.post(f"{PORTAL}/acme/auth/register",
                              json={"email": "a@x.com", "display_name": "A"})
        assert r.status_code == 404
