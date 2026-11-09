"""Every route with a `{case_id}` refuses another tenant's case — 2026-09-29.

`test_ungated_routes.py` finds routes with NO identity. It cannot see a route
that authenticates the caller and then never asks whose case it was handed,
and on 2026-09-29 that shape was found on 31 routes: all of `/payments`, the
SLA start/pause/resume routes, KYC and e-sign, CRM and invoice sync, document
extraction, lineage, the HxStream replay and case variables. Proven by the
first test below before the fix — an admin in another tenant got a 2xx from 15
of them, including `/variables/cases/{id}` returning the case's data.

Two checks, because each is blind where the other sees:

* **the probe** calls every `{case_id}` route as another tenant's admin. It
  cannot get past a 422, so a write whose body it cannot guess proves nothing;
* **the static check** reads every `{case_id}` handler for a gate. It cannot
  tell a gate that works from one that does not — the probe can.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import inspect
import re
import uuid

from fastapi.routing import APIRoute

from case_service.main import app
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from case_service.tenancy import identity as tenancy
from velaris_shared.auth.jwt_handler import create_dev_token
from velaris_shared.config import get_settings
from velaris_shared.db.models import (
    CaseInstanceModel, CaseTypeModel, PaymentDisbursementModel, PaymentRequestModel,
    TenantModel, UserDirectoryModel,
)
from tests.conftest import LIFECYCLE_PROCESS_ID, TestSessionFactory  # type: ignore[attr-defined]


# The fixtures this file used from test_payments_authz.py, which moved to
# commerce-service with the payments routes (R5e step 7). The world still has a
# payment and a disbursement: case-service's own case_id routes read them.
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

#: Answer 2xx to another tenant, and are right to.
CROSS_TENANT_2XX_OK = {
    # Releases a step lock only where `locked_by` is the CALLER — there is
    # nothing of the other tenant's to touch.
    ("POST", "/api/v1/cases/{case_id}/steps/{step_id}/unlock"),
    # Filtered to the caller's tenant in the query: the answer is empty.
    ("GET", "/api/v1/costing/cases/{case_id}/cost"),
    ("GET", "/api/v1/channels/cases/{case_id}/conversations"),
}

#: A gate, by name. Helpers that wrap `require_case` are listed so a rename of
#: one shows up here rather than silently passing.
GATE = re.compile(
    r"require_case|_authorized_case|_assert_case_access|_case_gate|_reach_case|"
    r"_stage_move_gate|same_tenant|case_visible_to|_scoped\(|tenant_id\s*==|"
    r"_get_run\(|_manifest_allows_cases|locked_by == user|"
    r"_get_case_or_404\([^)]*user|_tenant_uuid_for_costing|"
    # Form drafts (2026-10-29): case_visible_to + require_step_submit, in one helper
    r"_draft_target\(")

#: Portal routes authenticate a CUSTOMER and match the case to them — a
#: different model, pinned by the portal's own tests. The broker authenticates
#: a package grant.
OTHER_MODEL = ("/api/v1/portal/", "/api/v1/broker/")


def _case_routes():
    for r in app.routes:
        if isinstance(r, APIRoute) and "{case_id}" in r.path:
            yield r


async def test_no_case_id_route_answers_another_tenant(api, world):
    cid = str(world["case"].id)
    h = _headers("mallory", ["admin"])
    leaks = []
    for r in _case_routes():
        if r.path.startswith(OTHER_MODEL):
            continue
        path = r.path.replace("{case_id}", cid)
        path = re.sub(r"\{[^}:]+(:path)?\}", str(uuid.uuid4()), path)
        for method in sorted(r.methods - {"HEAD", "OPTIONS"}):
            if (method, r.path) in CROSS_TENANT_2XX_OK:
                continue
            resp = await api.request(method, path, headers=h, json={})
            if resp.status_code < 300:
                leaks.append((method, r.path, resp.status_code))
    assert leaks == [], leaks


def test_every_case_id_handler_has_a_gate():
    ungated = []
    for r in _case_routes():
        if r.path.startswith(OTHER_MODEL):
            continue
        if not GATE.search(inspect.getsource(r.endpoint)):
            ungated.append((sorted(r.methods), r.path))
    assert ungated == [], ungated


# ── the global lists beside those routes ─────────────────────────────────────

_LISTS = {
    "/api/v1/identity/verifications": "IdentityVerificationModel",
    "/api/v1/esign/requests": "ESignRequestModel",
    "/api/v1/crm/records": "CrmSyncRecordModel",
    "/api/v1/invoices/records": "InvoiceRecordModel",
    "/api/v1/docintel/extractions": "DocExtractionJobModel",
    "/api/v1/docintel/storage": "DocStorageRouteModel",
    # /api/v1/payments/{requests,disbursements}: commerce-service now —
    # its copy of this guard covers them (R5e step 7).
}


async def test_the_global_lists_hold_only_the_callers_tenant(api, world):
    """Each of these returned every tenant's rows until 2026-09-29."""
    from velaris_shared.db import models
    from tests.conftest import TestSessionFactory  # type: ignore[attr-defined]

    async with TestSessionFactory() as db:
        for name in filter(None, _LISTS.values()):
            extra = {"document_name": "a.pdf"} if name == "DocStorageRouteModel" else {}
            db.add(getattr(models, name)(tenant_id="acme", case_id=world["case"].id,
                                         step_id="pay", **extra))
        await db.commit()
    for path in _LISTS:
        mine = await api.get(path, headers=_headers("ann", ["admin"]))
        theirs = await api.get(path, headers=_headers("mallory", ["admin"]))
        assert mine.status_code == 200, (path, mine.text)
        assert len(mine.json()) >= 1, path
        assert theirs.status_code == 200 and theirs.json() == [], (path, theirs.text)
