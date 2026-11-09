"""The payments router had authentication and nothing else — found 2026-09-29.

Every route under `/api/v1/payments` depended on `get_current_user` alone: no
HxGuard action and no tenant check on the case or the payment named in the
path. `test_ungated_routes.py` could not see it, because that sweep looks for
routes with NO identity, and these had one. So any signed-in user, of any role,
in any tenant, could:

  * `POST /cases/{id}/disburse` — record money paid out on another tenant's
    case, complete its step and advance its stage;
  * `POST /disbursements/{id}/mark-sent` — post that disbursement to the ledger;
  * `POST /requests/{id}/refund` — refund another tenant's payment;
  * list every tenant's payment requests, disbursements, connectors and
    webhook events.

Found while building the rep-desk action rail (Oct item 2), whose "take a
payment" action rides on this router.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from velaris_shared.auth.jwt_handler import create_dev_token
from velaris_shared.config import get_settings
from velaris_shared.db.models import (
    CaseInstanceModel,
    CaseTypeModel,
    PaymentDisbursementModel,
    PaymentRequestModel,
    TenantModel,
    UserDirectoryModel,
)

from case_service.tenancy import identity as tenancy
from tests.conftest import app  # the in-process gateway: commerce + case-service
from tests.conftest import LIFECYCLE_PROCESS_ID, TestSessionFactory  # type: ignore[attr-defined]


def _headers(user_id: str, roles: list[str]) -> dict[str, str]:
    s = get_settings()
    token = create_dev_token(user_id=user_id, username=user_id, roles=roles,
                             secret=s.auth_secret,
                             private_key=s.auth_rsa_private_key or "")
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def api():
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def world():
    """Tenant acme owns a case, a payment and a disbursement. Ann works for acme
    as a case worker; vera is an acme viewer; mallory is an admin at evil."""
    tenancy.clear_cache()
    async with TestSessionFactory() as db:
        acme = TenantModel(id=uuid.uuid4(), slug="acme", name="Acme")
        evil = TenantModel(id=uuid.uuid4(), slug="evil", name="Evil")
        db.add_all([acme, evil])
        db.add(UserDirectoryModel(id=uuid.uuid4(), user_id="ann", tenant_id="acme"))
        db.add(UserDirectoryModel(id=uuid.uuid4(), user_id="vera", tenant_id="acme"))
        db.add(UserDirectoryModel(id=uuid.uuid4(), user_id="mallory", tenant_id="evil"))
        ct = CaseTypeModel(
            id=uuid.uuid4(), name="Pay CT", version="1.0.0",
            lifecycle_process_id=LIFECYCLE_PROCESS_ID,
            definition_json={"stages": [{"id": "s1", "name": "Settle", "order": 1,
                                         "steps": [{"id": "pay", "name": "Pay",
                                                    "step_type": "payment_disbursement"}]}]})
        db.add(ct)
        await db.flush()
        case = CaseInstanceModel(
            id=uuid.uuid4(), case_number="ACME-P1", case_type_id=ct.id,
            case_type_version="1.0.0", status="new", priority="medium",
            tenant_id=acme.id, created_by="ann", data={})
        db.add(case)
        await db.flush()
        pr = PaymentRequestModel(
            id=uuid.uuid4(), tenant_id="acme", case_id=case.id, step_id="pay",
            provider="stripe", amount_cents=1000, currency="gbp", status="succeeded",
            description="Fee")
        disb = PaymentDisbursementModel(
            id=uuid.uuid4(), tenant_id="acme", case_id=case.id, step_id="pay",
            amount_cents=500, currency="gbp", status="confirmed",
            description="Refund", confirmed_by="ann")
        db.add_all([pr, disb])
        await db.commit()
        return {"case": case, "payment": pr, "disbursement": disb}


# ── another tenant ───────────────────────────────────────────────────────────


async def test_another_tenant_cannot_disburse_on_the_case(api, world):
    r = await api.post(f"/api/v1/payments/cases/{world['case'].id}/disburse",
                       headers=_headers("mallory", ["admin"]),
                       json={"step_id": "pay", "amount_cents": 99999})
    assert r.status_code == 404, r.text


async def test_another_tenant_cannot_mark_a_disbursement_sent(api, world):
    r = await api.post(
        f"/api/v1/payments/disbursements/{world['disbursement'].id}/mark-sent",
        headers=_headers("mallory", ["admin"]))
    assert r.status_code == 404, r.text


async def test_another_tenant_cannot_refund_the_payment(api, world):
    r = await api.post(f"/api/v1/payments/requests/{world['payment'].id}/refund",
                       headers=_headers("mallory", ["admin"]), json={})
    assert r.status_code == 404, r.text


async def test_another_tenant_cannot_read_the_case_payments(api, world):
    h = _headers("mallory", ["admin"])
    cid = world["case"].id
    for path in (f"/api/v1/payments/cases/{cid}/requests",
                 f"/api/v1/payments/cases/{cid}/disbursements",
                 f"/api/v1/payments/requests/{world['payment'].id}"):
        r = await api.get(path, headers=h)
        assert r.status_code == 404, (path, r.text)


async def test_the_global_lists_are_this_tenants_only(api, world):
    h = _headers("mallory", ["admin"])
    for path in ("/api/v1/payments/requests", "/api/v1/payments/disbursements"):
        r = await api.get(path, headers=h)
        assert r.status_code == 200, (path, r.text)
        assert r.json() == [], path
    # And the owning tenant still sees its own.
    r = await api.get("/api/v1/payments/requests", headers=_headers("ann", ["case_worker"]))
    assert r.status_code == 200 and len(r.json()) == 1, r.text


# ── a role that should not move money ────────────────────────────────────────


async def test_a_viewer_cannot_move_money(api, world):
    h = _headers("vera", ["viewer"])
    cid = world["case"].id
    r = await api.post(f"/api/v1/payments/cases/{cid}/disburse", headers=h,
                       json={"step_id": "pay", "amount_cents": 100})
    assert r.status_code == 403, r.text
    r = await api.post(f"/api/v1/payments/requests/{world['payment'].id}/refund",
                       headers=h, json={})
    assert r.status_code == 403, r.text
    r = await api.post(
        f"/api/v1/payments/disbursements/{world['disbursement'].id}/mark-sent", headers=h)
    assert r.status_code == 403, r.text


async def test_the_owning_tenants_case_worker_still_can(api, world):
    """The fix must not take the feature away from the people who use it."""
    r = await api.post(f"/api/v1/payments/cases/{world['case'].id}/disburse",
                       headers=_headers("ann", ["case_worker"]),
                       json={"step_id": "pay", "amount_cents": 100})
    assert r.status_code == 200, r.text
    r = await api.get(f"/api/v1/payments/cases/{world['case'].id}/disbursements",
                      headers=_headers("ann", ["case_worker"]))
    assert r.status_code == 200 and len(r.json()) == 2, r.text
