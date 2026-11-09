"""The portal's internal case API (R5e step 8 — the portal split).

Every `/internal/v1/portal/*` route behind the same guard as step 7's (secret,
proxy headers, unconfigured install), never in the OpenAPI schema, bounded
where a caller sends bulk (case ids, audio), and the idempotent ones answering
a repeat with the original.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
import base64
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from case_service.internal_api import portal as internal_portal
from case_service.main import app
from velaris_shared.config import get_settings
from velaris_shared.internal import client as internal
from velaris_shared.internal.auth import HEADER

SECRET = "p" * 40
PATHS = sorted(internal_portal.ROUTES)


@pytest.fixture
def secret():
    get_settings().internal_service_secret = SECRET
    yield SECRET


@pytest.fixture
async def raw():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
def http_path():
    internal.set_transport(ASGITransport(app=app))
    internal.force_http = True
    yield
    internal.set_transport(None)
    internal.force_http = False


# ── the guard, on every portal route ──────────────────────────────────────────

def test_ten_routes():
    assert len(PATHS) == 10
    assert all(p.startswith("portal/") for p in PATHS)


@pytest.mark.parametrize("path", PATHS)
async def test_unconfigured_install_refuses(raw, path):
    get_settings().internal_service_secret = ""
    r = await raw.post(f"/internal/v1/{path}", json={}, headers={HEADER: "anything"})
    assert r.status_code == 503


@pytest.mark.parametrize("path", PATHS)
async def test_missing_or_wrong_secret_is_refused(raw, secret, path):
    assert (await raw.post(f"/internal/v1/{path}", json={})).status_code == 403
    r = await raw.post(f"/internal/v1/{path}", json={}, headers={HEADER: "wrong-" + secret})
    assert r.status_code == 403


@pytest.mark.parametrize("path", PATHS)
async def test_a_proxied_call_is_refused_even_with_the_secret(raw, secret, path):
    r = await raw.post(f"/internal/v1/{path}", json={},
                       headers={HEADER: secret, "X-Forwarded-For": "203.0.113.9"})
    assert r.status_code == 404


@pytest.mark.parametrize("path", PATHS)
async def test_the_secret_reaches_validation(raw, secret, path):
    # An empty body is refused by the request model, not by the guard.
    r = await raw.post(f"/internal/v1/{path}", json={}, headers={HEADER: secret})
    assert r.status_code == 422, r.text


def test_not_in_the_openapi_schema():
    paths = app.openapi()["paths"]
    assert not [p for p in paths if p.startswith("/internal")]


@pytest.mark.parametrize("path", PATHS)
def test_registered_for_local_mode(path):
    assert path in internal._LOCAL


# ── bounds ───────────────────────────────────────────────────────────────────

async def test_case_vars_refuses_more_than_500_ids(raw, secret):
    ids = [str(uuid.uuid4()) for _ in range(501)]
    r = await raw.post("/internal/v1/portal/case-vars", json={"case_ids": ids},
                       headers={HEADER: secret})
    assert r.status_code == 422


async def test_case_vars_answers_for_unknown_ids(raw, secret):
    r = await raw.post("/internal/v1/portal/case-vars",
                       json={"case_ids": [str(uuid.uuid4())]}, headers={HEADER: secret})
    assert r.status_code == 200, r.text
    assert isinstance(r.json()["vars"], dict)


async def test_transcribe_refuses_more_than_45_seconds(raw, secret):
    too_long = base64.b64encode(b"\0" * (46 * 16_000 * 2)).decode()
    r = await raw.post("/internal/v1/portal/voice/transcribe", json={
        "case_id": str(uuid.uuid4()), "customer_id": str(uuid.uuid4()),
        "pcm_b64": too_long}, headers={HEADER: secret})
    assert r.status_code == 422


async def test_transcribe_clip_limit_cannot_be_raised(raw, secret):
    r = await raw.post("/internal/v1/portal/voice/transcribe", json={
        "case_id": str(uuid.uuid4()), "customer_id": str(uuid.uuid4()),
        "pcm_b64": "AAAA", "clip_seconds_max": 600}, headers={HEADER: secret})
    assert r.status_code == 422


# ── the mail route sends only what case-service composes ─────────────────────

@pytest.mark.parametrize("payload", [
    {"purpose": "newsletter", "to": "a@b.test", "code": "123456"},   # unknown purpose
    {"purpose": "login", "to": "a@b.test", "code": "hello!"},        # not a code
    {"purpose": "login", "to": "a@b.test", "code": "1234567"},       # too long
])
async def test_otp_email_refuses_anything_but_a_code(raw, secret, payload):
    r = await raw.post("/internal/v1/portal/otp-email", json=payload, headers={HEADER: secret})
    assert r.status_code == 422


def test_otp_email_has_no_subject_or_body_field():
    fields = set(internal_portal.OtpEmailIn.model_fields)
    assert fields == {"purpose", "to", "code", "case_id"}


async def test_otp_email_without_smtp_is_not_an_error(raw, secret):
    r = await raw.post("/internal/v1/portal/otp-email",
                       json={"purpose": "login", "to": "a@b.test", "code": "123456"},
                       headers={HEADER: secret})
    assert r.status_code == 200, r.text
    assert r.json() == {"sent": False}


# ── erase / export refuse a customer of another tenant ───────────────────────

@pytest.mark.parametrize("path,extra", [
    ("portal/customers/export", {}),
    ("portal/customers/erase", {"mode": "self"}),
    ("portal/customers/erase", {"mode": "admin"}),
])
async def test_unknown_customer_is_404(raw, secret, path, extra):
    r = await raw.post(f"/internal/v1/{path}", json={
        "tenant_id": str(uuid.uuid4()), "customer_id": str(uuid.uuid4()), **extra},
        headers={HEADER: secret})
    assert r.status_code == 404


async def test_erase_mode_is_closed(raw, secret):
    r = await raw.post("/internal/v1/portal/customers/erase", json={
        "tenant_id": str(uuid.uuid4()), "customer_id": str(uuid.uuid4()), "mode": "everyone"},
        headers={HEADER: secret})
    assert r.status_code == 422


# ── defence in depth: a secret holder that is not the portal ─────────────────
# Every holder of the internal secret reaches these routes, not only
# portal-service. The handlers re-check what the portal checked, and the actor
# on a step is derived from the customer, never taken from the caller.

from sqlalchemy import select as _select

from tests.conftest import TestSessionFactory
from velaris_shared.db.models import (
    CaseAuditLogModel,
    CaseInstanceModel,
    CaseStepCompletionModel,
    CaseTypeModel,
    PortalCustomerCaseLinkModel,
    PortalCustomerModel,
    TenantModel,
)

_DEF = {"stages": [{"id": "s1", "name": "One", "steps": [
    {"id": "cust", "name": "Confirm", "assignment": {"strategy": "customer"},
     "customer_action": {"type": "approval"}},
    {"id": "staff", "name": "Staff only", "assignment": {"strategy": "queue"}},
]}]}


@pytest.fixture
async def world():
    async with TestSessionFactory() as db:
        t = TenantModel(slug=f"p{uuid.uuid4().hex[:8]}", name="P",
                        settings={"portal": {"enabled": True}})
        other = TenantModel(slug=f"o{uuid.uuid4().hex[:8]}", name="O")
        db.add_all([t, other]); await db.flush()
        ct = CaseTypeModel(name="ct", version="1", definition_json=_DEF,
                           portal_enabled=True, tenant_id=t.id)
        foreign_ct = CaseTypeModel(name="fct", version="1", definition_json=_DEF,
                                   portal_enabled=True, tenant_id=other.id)
        db.add_all([ct, foreign_ct]); await db.flush()
        case = CaseInstanceModel(case_type_id=ct.id, case_type_version="1", tenant_id=t.id,
                                 status="open", current_stage_id="s1", data={})
        closed = CaseInstanceModel(case_type_id=ct.id, case_type_version="1", tenant_id=t.id,
                                   status="resolved", current_stage_id="s1", data={})
        db.add_all([case, closed]); await db.flush()
        jane = PortalCustomerModel(tenant_id=t.id, primary_email="jane@x.test", display_name="J")
        mallory = PortalCustomerModel(tenant_id=t.id, primary_email="m@x.test", display_name="M")
        db.add_all([jane, mallory]); await db.flush()
        db.add_all([PortalCustomerCaseLinkModel(customer_id=jane.id, case_id=case.id),
                    PortalCustomerCaseLinkModel(customer_id=jane.id, case_id=closed.id)])
        await db.commit()
        return {"t": t, "ct": ct, "fct": foreign_ct, "case": case, "closed": closed,
                "jane": jane, "mallory": mallory}


def _step(world, customer="jane", case="case", step="cust", **extra):
    return {"case_id": str(world[case].id), "customer_id": str(world[customer].id),
            "step_id": step, "status": "completed", "data": {}, **extra}


async def test_step_by_an_unlinked_customer_is_404(raw, secret, world):
    r = await raw.post("/internal/v1/portal/steps/complete", json=_step(world, "mallory"),
                       headers={HEADER: secret})
    assert r.status_code == 404


async def test_a_staff_step_cannot_be_completed_as_a_customer(raw, secret, world):
    r = await raw.post("/internal/v1/portal/steps/complete", json=_step(world, step="staff"),
                       headers={HEADER: secret})
    assert r.status_code == 404


async def test_a_closed_case_is_refused(raw, secret, world):
    r = await raw.post("/internal/v1/portal/steps/complete", json=_step(world, case="closed"),
                       headers={HEADER: secret})
    assert r.status_code == 400


async def test_the_actor_is_the_customer_whatever_the_caller_sends(raw, secret, world):
    r = await raw.post("/internal/v1/portal/steps/complete",
                       json=_step(world, actor="system:root", stage_id="elsewhere",
                                  step_type="payment"),
                       headers={HEADER: secret})
    assert r.status_code == 200, r.text
    async with TestSessionFactory() as db:
        row = (await db.execute(_select(CaseStepCompletionModel).where(
            CaseStepCompletionModel.case_id == world["case"].id))).scalar_one()
        audit = (await db.execute(_select(CaseAuditLogModel).where(
            CaseAuditLogModel.case_id == world["case"].id,
            CaseAuditLogModel.action == "step_completed"))).scalar_one()
    who = f"customer:{world['jane'].id}"
    assert row.completed_by == who and audit.actor_id == who
    assert row.stage_id == "s1" and row.step_type == "user_task"


def test_no_route_takes_an_actor_or_a_catalogue():
    for _path, (model, _fn) in internal_portal.ROUTES.items():
        fields = set(model.model_fields)
        assert not fields & {"actor", "actor_id", "step", "catalogue", "stage_id"}, _path


@pytest.mark.parametrize("path,extra", [
    ("portal/voice/fill", {"step_id": "cust", "transcript": "hello"}),
    ("portal/voice/transcribe", {"pcm_b64": base64.b64encode(b"\0" * 16_000).decode()}),
    ("portal/meet/join", {"session_id": str(uuid.uuid4())}),
])
async def test_voice_and_meetings_refuse_an_unlinked_customer(raw, secret, world, path, extra):
    r = await raw.post(f"/internal/v1/{path}", json={
        "case_id": str(world["case"].id), "customer_id": str(world["mallory"].id), **extra},
        headers={HEADER: secret})
    assert r.status_code == 404, r.text


def _case_payload(world, **over):
    p = {"tenant_id": str(world["t"].id), "slug": world["t"].slug,
         "case_type_id": str(world["ct"].id), "submitter_name": "A",
         "submitter_email": "a@x.test", "subject": "s", "description": "d"}
    p.update(over)
    return p


@pytest.mark.parametrize("over", [
    lambda w: {"case_type_id": str(w["fct"].id)},        # another tenant's case type
    lambda w: {"slug": "not-this-tenant"},                 # slug of another portal
    lambda w: {"tenant_id": str(uuid.uuid4())},            # no such tenant
])
async def test_portal_case_refuses_what_the_portal_would(raw, secret, world, over):
    r = await raw.post("/internal/v1/portal/cases", json=_case_payload(world, **over(world)),
                       headers={HEADER: secret})
    assert r.status_code == 400, r.text


async def test_portal_case_refuses_a_switched_off_portal(raw, secret, world):
    async with TestSessionFactory() as db:
        t = await db.get(TenantModel, world["t"].id)
        t.settings = {"portal": {"enabled": False}}
        await db.commit()
    r = await raw.post("/internal/v1/portal/cases", json=_case_payload(world),
                       headers={HEADER: secret})
    assert r.status_code == 400


async def test_portal_case_opens_in_the_portals_tenant(raw, secret, world):
    r = await raw.post("/internal/v1/portal/cases", json=_case_payload(world),
                       headers={HEADER: secret})
    assert r.status_code == 200, r.text
    async with TestSessionFactory() as db:
        case = await db.get(CaseInstanceModel, uuid.UUID(r.json()["case_id"]))
    assert str(case.tenant_id) == str(world["t"].id)
    assert case.created_by == "portal:a@x.test"


# ── what a public caller sees when case-service answers badly ─────────────────

@pytest.mark.parametrize("status,shown", [(422, 500), (500, 500), (404, 404), (409, 409)])
async def test_internal_error_text_is_not_relayed_on_contract_errors(monkeypatch, status, shown):
    from fastapi import HTTPException as _H

    from portal_service.api.routers import portal as portal_router

    async def _fail(path, payload, *, session=None, timeout=30.0):
        raise internal.InternalCallError(path, status, "field tenant_id=1234 secret-ish")

    monkeypatch.setattr(internal, "call", _fail)
    with pytest.raises(_H) as e:
        await portal_router._internal("portal/case-vars", {"case_ids": []}, None)
    assert e.value.status_code == shown
    leaked = "tenant_id=1234" in str(e.value.detail)
    assert leaked is (shown != 500)
