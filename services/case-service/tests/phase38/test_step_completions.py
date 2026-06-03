"""HELIX P38 — Step Completion tests (25 tests).

Covers: complete step (user_task, approval, document_request),
        list completions, auto-advance logic, upsert idempotency,
        rejection, required-step gating, 404 guards.
"""
from __future__ import annotations

import uuid
import pytest
import pytest_asyncio
from httpx import AsyncClient

from case_service.main import app
from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser
from case_service.db.models import CaseTypeModel, CaseInstanceModel


# ── Helpers ───────────────────────────────────────────────────────────────────

def _user(uid: str = "worker-1") -> AuthenticatedUser:
    return AuthenticatedUser(user_id=uid, roles=["staff"])

def _admin() -> AuthenticatedUser:
    return AuthenticatedUser(user_id="admin-1", roles=["admin"])

STAGE_ID   = "triage"
STEP_A_ID  = "collect_info"
STEP_B_ID  = "verify_docs"
STAGE2_ID  = "review"

CASE_TYPE_DEF = {
    "stages": [
        {
            "id": STAGE_ID, "name": "Triage", "stage_type": "linear", "order": 0,
            "steps": [
                {"id": STEP_A_ID, "name": "Collect Info",  "step_type": "user_task",  "required": True,
                 "fields": [{"name": "summary", "label": "Summary", "type": "text", "required": True}]},
                {"id": STEP_B_ID, "name": "Verify Docs",   "step_type": "document_request", "required": True},
            ],
        },
        {
            "id": STAGE2_ID, "name": "Review", "stage_type": "linear", "order": 1,
            "steps": [
                {"id": "approve", "name": "Approve", "step_type": "approval", "required": True},
            ],
        },
    ]
}


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def case_type(session) -> CaseTypeModel:
    ct = CaseTypeModel(
        name="Step Test Case", version="1.0.0",
        definition_json=CASE_TYPE_DEF, default_priority="medium",
    )
    session.add(ct); await session.flush(); return ct


@pytest_asyncio.fixture
async def case_(session, case_type) -> CaseInstanceModel:
    c = CaseInstanceModel(
        case_type_id=case_type.id, case_type_version="1.0.0",
        status="open", priority="medium",
        data={"subject": "Test case"},
        current_stage_id=STAGE_ID,
        created_by="worker-1",
    )
    session.add(c); await session.flush(); return c


@pytest_asyncio.fixture
async def case_no_stages(session) -> CaseInstanceModel:
    ct = CaseTypeModel(
        name="No Stages", version="1.0.0",
        definition_json={"stages": []}, default_priority="medium",
    )
    session.add(ct); await session.flush()
    c = CaseInstanceModel(
        case_type_id=ct.id, case_type_version="1.0.0",
        status="open", priority="medium", data={}, created_by="w",
    )
    session.add(c); await session.flush(); return c


# ── Complete step ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_complete_user_task(client: AsyncClient, case_):
    app.dependency_overrides[get_current_user] = lambda: _user()
    resp = await client.post(f"/api/v1/cases/{case_.id}/steps/{STEP_A_ID}/complete", json={
        "stage_id": STAGE_ID, "step_type": "user_task", "status": "completed",
        "data": {"summary": "Customer called, confirmed issue"},
    })
    assert resp.status_code == 200
    d = resp.json()
    assert d["step_id"] == STEP_A_ID
    assert d["status"] == "completed"
    assert d["data"]["summary"] == "Customer called, confirmed issue"
    assert d["completed_by"] == "worker-1"


@pytest.mark.asyncio
async def test_complete_document_request(client: AsyncClient, case_):
    app.dependency_overrides[get_current_user] = lambda: _user()
    resp = await client.post(f"/api/v1/cases/{case_.id}/steps/{STEP_B_ID}/complete", json={
        "stage_id": STAGE_ID, "step_type": "document_request", "status": "completed",
        "data": {"document_id": "doc-uuid-123", "filename": "invoice.pdf"},
    })
    assert resp.status_code == 200
    assert resp.json()["step_type"] == "document_request"
    assert resp.json()["data"]["filename"] == "invoice.pdf"


@pytest.mark.asyncio
async def test_reject_approval_step(client: AsyncClient, case_):
    app.dependency_overrides[get_current_user] = lambda: _user()
    resp = await client.post(f"/api/v1/cases/{case_.id}/steps/approve/complete", json={
        "stage_id": STAGE2_ID, "step_type": "approval", "status": "rejected",
        "data": {"reason": "Missing documentation"},
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"
    assert resp.json()["data"]["reason"] == "Missing documentation"


@pytest.mark.asyncio
async def test_complete_step_404_case(client: AsyncClient):
    app.dependency_overrides[get_current_user] = lambda: _user()
    resp = await client.post(f"/api/v1/cases/{uuid.uuid4()}/steps/some-step/complete", json={
        "stage_id": "s1", "step_type": "user_task", "status": "completed", "data": {},
    })
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_actor_id_override(client: AsyncClient, case_):
    """actor_id in body overrides authenticated user_id."""
    app.dependency_overrides[get_current_user] = lambda: _user("worker-1")
    resp = await client.post(f"/api/v1/cases/{case_.id}/steps/{STEP_A_ID}/complete", json={
        "stage_id": STAGE_ID, "step_type": "user_task", "status": "completed",
        "data": {}, "actor_id": "supervisor-99",
    })
    assert resp.status_code == 200
    assert resp.json()["completed_by"] == "supervisor-99"


# ── Upsert idempotency ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_upsert_on_resubmit(client: AsyncClient, case_):
    """Second complete call updates data, does not create duplicate."""
    app.dependency_overrides[get_current_user] = lambda: _user()
    await client.post(f"/api/v1/cases/{case_.id}/steps/{STEP_A_ID}/complete", json={
        "stage_id": STAGE_ID, "step_type": "user_task", "status": "completed",
        "data": {"summary": "First attempt"},
    })
    resp2 = await client.post(f"/api/v1/cases/{case_.id}/steps/{STEP_A_ID}/complete", json={
        "stage_id": STAGE_ID, "step_type": "user_task", "status": "completed",
        "data": {"summary": "Updated attempt"},
    })
    assert resp2.status_code == 200
    assert resp2.json()["data"]["summary"] == "Updated attempt"

    # Verify only one record in list
    list_resp = await client.get(f"/api/v1/cases/{case_.id}/step-completions")
    matching = [r for r in list_resp.json() if r["step_id"] == STEP_A_ID]
    assert len(matching) == 1


# ── List completions ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_completions_empty(client: AsyncClient, case_):
    resp = await client.get(f"/api/v1/cases/{case_.id}/step-completions")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_list_completions_after_completing(client: AsyncClient, case_):
    app.dependency_overrides[get_current_user] = lambda: _user()
    await client.post(f"/api/v1/cases/{case_.id}/steps/{STEP_A_ID}/complete", json={
        "stage_id": STAGE_ID, "step_type": "user_task", "status": "completed", "data": {},
    })
    await client.post(f"/api/v1/cases/{case_.id}/steps/{STEP_B_ID}/complete", json={
        "stage_id": STAGE_ID, "step_type": "document_request", "status": "completed", "data": {},
    })
    resp = await client.get(f"/api/v1/cases/{case_.id}/step-completions")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


@pytest.mark.asyncio
async def test_list_completions_stage_filter(client: AsyncClient, case_):
    app.dependency_overrides[get_current_user] = lambda: _user()
    await client.post(f"/api/v1/cases/{case_.id}/steps/{STEP_A_ID}/complete", json={
        "stage_id": STAGE_ID, "step_type": "user_task", "status": "completed", "data": {},
    })
    await client.post(f"/api/v1/cases/{case_.id}/steps/approve/complete", json={
        "stage_id": STAGE2_ID, "step_type": "approval", "status": "completed", "data": {},
    })
    resp = await client.get(f"/api/v1/cases/{case_.id}/step-completions?stage_id={STAGE_ID}")
    assert resp.status_code == 200
    assert all(r["stage_id"] == STAGE_ID for r in resp.json())
    assert len(resp.json()) == 1


@pytest.mark.asyncio
async def test_list_completions_404_case(client: AsyncClient):
    resp = await client.get(f"/api/v1/cases/{uuid.uuid4()}/step-completions")
    assert resp.status_code == 404


# ── Auto-advance ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_auto_advance_when_all_required_done(client: AsyncClient, case_):
    """Completing both required steps in stage 1 should auto-advance to stage 2."""
    app.dependency_overrides[get_current_user] = lambda: _user()

    r1 = await client.post(f"/api/v1/cases/{case_.id}/steps/{STEP_A_ID}/complete", json={
        "stage_id": STAGE_ID, "step_type": "user_task", "status": "completed", "data": {},
    })
    assert r1.json()["auto_advanced"] is False  # only one of two done

    r2 = await client.post(f"/api/v1/cases/{case_.id}/steps/{STEP_B_ID}/complete", json={
        "stage_id": STAGE_ID, "step_type": "document_request", "status": "completed", "data": {},
    })
    assert r2.json()["auto_advanced"] is True  # both done → advance

    # Verify case is now in stage 2
    case_resp = await client.get(f"/api/v1/cases/{case_.id}")
    assert case_resp.json()["current_stage_id"] == STAGE2_ID


@pytest.mark.asyncio
async def test_no_auto_advance_when_partial(client: AsyncClient, case_):
    """Only one required step done → no auto-advance."""
    app.dependency_overrides[get_current_user] = lambda: _user()
    r = await client.post(f"/api/v1/cases/{case_.id}/steps/{STEP_A_ID}/complete", json={
        "stage_id": STAGE_ID, "step_type": "user_task", "status": "completed", "data": {},
    })
    assert r.json()["auto_advanced"] is False
    case_resp = await client.get(f"/api/v1/cases/{case_.id}")
    assert case_resp.json()["current_stage_id"] == STAGE_ID


@pytest.mark.asyncio
async def test_no_auto_advance_on_rejection(client: AsyncClient, case_):
    """Rejection does not trigger auto-advance even if it's the last step."""
    app.dependency_overrides[get_current_user] = lambda: _user()
    await client.post(f"/api/v1/cases/{case_.id}/steps/{STEP_A_ID}/complete", json={
        "stage_id": STAGE_ID, "step_type": "user_task", "status": "completed", "data": {},
    })
    r = await client.post(f"/api/v1/cases/{case_.id}/steps/{STEP_B_ID}/complete", json={
        "stage_id": STAGE_ID, "step_type": "document_request", "status": "rejected",
        "data": {"reason": "Wrong format"},
    })
    assert r.json()["auto_advanced"] is False


@pytest.mark.asyncio
async def test_no_auto_advance_for_case_no_stages(client: AsyncClient, case_no_stages):
    """Case with no stages definition: step completion records fine, no advance."""
    app.dependency_overrides[get_current_user] = lambda: _user()
    r = await client.post(f"/api/v1/cases/{case_no_stages.id}/steps/any-step/complete", json={
        "stage_id": "s1", "step_type": "user_task", "status": "completed", "data": {},
    })
    assert r.status_code == 200
    assert r.json()["auto_advanced"] is False


@pytest.mark.asyncio
async def test_auto_advance_single_required_step(client: AsyncClient, session):
    """Stage with a single required step auto-advances on first completion."""
    ct = CaseTypeModel(
        name="Single Step", version="1.0.0",
        definition_json={"stages": [
            {"id": "s1", "name": "S1", "stage_type": "linear", "order": 0,
             "steps": [{"id": "only", "name": "Only Step", "step_type": "user_task", "required": True}]},
            {"id": "s2", "name": "S2", "stage_type": "linear", "order": 1, "steps": []},
        ]},
        default_priority="medium",
    )
    session.add(ct); await session.flush()
    c = CaseInstanceModel(
        case_type_id=ct.id, case_type_version="1.0.0", status="open",
        priority="medium", data={}, current_stage_id="s1", created_by="w",
    )
    session.add(c); await session.flush()

    app.dependency_overrides[get_current_user] = lambda: _user()
    r = await client.post(f"/api/v1/cases/{c.id}/steps/only/complete", json={
        "stage_id": "s1", "step_type": "user_task", "status": "completed", "data": {},
    })
    assert r.json()["auto_advanced"] is True


@pytest.mark.asyncio
async def test_no_auto_advance_optional_steps_not_counted(client: AsyncClient, session):
    """Optional steps are not required for auto-advance."""
    ct = CaseTypeModel(
        name="Optional Steps", version="1.0.0",
        definition_json={"stages": [
            {"id": "s1", "name": "S1", "stage_type": "linear", "order": 0,
             "steps": [
                 {"id": "req",  "name": "Required", "step_type": "user_task", "required": True},
                 {"id": "opt",  "name": "Optional", "step_type": "user_task", "required": False},
             ]},
            {"id": "s2", "name": "S2", "stage_type": "linear", "order": 1, "steps": []},
        ]},
        default_priority="medium",
    )
    session.add(ct); await session.flush()
    c = CaseInstanceModel(
        case_type_id=ct.id, case_type_version="1.0.0", status="open",
        priority="medium", data={}, current_stage_id="s1", created_by="w",
    )
    session.add(c); await session.flush()

    app.dependency_overrides[get_current_user] = lambda: _user()
    # Only complete required step — should still auto-advance
    r = await client.post(f"/api/v1/cases/{c.id}/steps/req/complete", json={
        "stage_id": "s1", "step_type": "user_task", "status": "completed", "data": {},
    })
    assert r.json()["auto_advanced"] is True


# ── Response shape ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_completion_response_shape(client: AsyncClient, case_):
    app.dependency_overrides[get_current_user] = lambda: _user()
    r = await client.post(f"/api/v1/cases/{case_.id}/steps/{STEP_A_ID}/complete", json={
        "stage_id": STAGE_ID, "step_type": "user_task", "status": "completed",
        "data": {"key": "val"},
    })
    d = r.json()
    for field in ["id", "case_id", "stage_id", "step_id", "step_type",
                  "status", "data", "completed_by", "completed_at", "auto_advanced"]:
        assert field in d, f"Missing field: {field}"


@pytest.mark.asyncio
async def test_empty_data_allowed(client: AsyncClient, case_):
    """Steps can be completed with empty data dict."""
    app.dependency_overrides[get_current_user] = lambda: _user()
    r = await client.post(f"/api/v1/cases/{case_.id}/steps/{STEP_A_ID}/complete", json={
        "stage_id": STAGE_ID, "step_type": "user_task", "status": "completed", "data": {},
    })
    assert r.status_code == 200
    assert r.json()["data"] == {}


@pytest.mark.asyncio
async def test_automated_step_not_counted_for_advance(client: AsyncClient, session):
    """Automated steps are excluded from required-step count."""
    ct = CaseTypeModel(
        name="Automated", version="1.0.0",
        definition_json={"stages": [
            {"id": "s1", "name": "S1", "stage_type": "linear", "order": 0,
             "steps": [
                 {"id": "manual", "name": "Manual", "step_type": "user_task", "required": True},
                 {"id": "auto",   "name": "Auto",   "step_type": "automated", "required": True},
             ]},
            {"id": "s2", "name": "S2", "stage_type": "linear", "order": 1, "steps": []},
        ]},
        default_priority="medium",
    )
    session.add(ct); await session.flush()
    c = CaseInstanceModel(
        case_type_id=ct.id, case_type_version="1.0.0", status="open",
        priority="medium", data={}, current_stage_id="s1", created_by="w",
    )
    session.add(c); await session.flush()

    app.dependency_overrides[get_current_user] = lambda: _user()
    # Completing only the manual step should auto-advance (automated excluded from count)
    r = await client.post(f"/api/v1/cases/{c.id}/steps/manual/complete", json={
        "stage_id": "s1", "step_type": "user_task", "status": "completed", "data": {},
    })
    assert r.json()["auto_advanced"] is True
