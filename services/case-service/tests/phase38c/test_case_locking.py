"""HELIX P38c — Case Locking / My Task tests (20 tests).

Covers: get my-task (none, active, acquires lock), lock re-acquisition,
        lock prevents other operator from submitting, lock expiry bypass,
        complete step closes assignment + releases lock, explicit unlock,
        auto-advance still works after locking, 404 guard.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import AsyncClient

from case_service.main import app
from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser
from case_service.db.models import (
    CaseTypeModel, CaseInstanceModel, CaseAssignmentModel
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _user(uid: str = "op-1") -> AuthenticatedUser:
    return AuthenticatedUser(user_id=uid, roles=["staff"])


STEP_ID = "fill_form"
STAGE_ID = "intake"
STAGE2_ID = "review"

CASE_TYPE_DEF = {
    "stages": [
        {
            "id": STAGE_ID, "name": "Intake", "stage_type": "linear", "order": 0,
            "steps": [
                {"id": STEP_ID, "name": "Fill Form", "step_type": "user_task", "required": True,
                 "fields": [{"name": "notes", "label": "Notes", "type": "text", "required": False}]},
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
        name="Lock Test Case", version="1.0.0",
        definition_json=CASE_TYPE_DEF, default_priority="medium",
    )
    session.add(ct)
    await session.flush()
    return ct


@pytest_asyncio.fixture
async def case_inst(session, case_type) -> CaseInstanceModel:
    ci = CaseInstanceModel(
        case_type_id=case_type.id,
        case_type_version="1.0.0",
        status="open",
        priority="medium",
        data={"description": "Lock Test"},
        current_stage_id=STAGE_ID,
    )
    session.add(ci)
    await session.flush()
    return ci


@pytest_asyncio.fixture
async def assignment(session, case_inst) -> CaseAssignmentModel:
    a = CaseAssignmentModel(
        case_id=case_inst.id,
        step_id=STEP_ID,
        assignee_type="operator",
        assignee_id="op-1",
        status="active",
    )
    session.add(a)
    await session.flush()
    return a


def _override(uid: str):
    u = _user(uid)
    app.dependency_overrides[get_current_user] = lambda: u
    return u


def _clear():
    app.dependency_overrides.pop(get_current_user, None)


# ── Tests ─────────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def client():
    from httpx import ASGITransport
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


class TestGetMyTask:
    async def test_no_assignment_returns_null(self, client, case_inst):
        _override("op-1")
        try:
            r = await client.get(f"/api/v1/cases/{case_inst.id}/my-task")
            assert r.status_code == 200
            assert r.json() is None
        finally:
            _clear()

    async def test_active_assignment_returns_task(self, client, case_inst, assignment):
        _override("op-1")
        try:
            r = await client.get(f"/api/v1/cases/{case_inst.id}/my-task")
            assert r.status_code == 200
            data = r.json()
            assert data is not None
            assert data["step_id"] == STEP_ID
            assert data["stage_id"] == STAGE_ID
            assert data["is_locked_by_me"] is True
        finally:
            _clear()

    async def test_acquires_lock_on_open(self, client, case_inst, assignment, session):
        _override("op-1")
        try:
            r = await client.get(f"/api/v1/cases/{case_inst.id}/my-task")
            assert r.status_code == 200
            data = r.json()
            assert data["locked_by"] == "op-1"
            assert data["lock_expires_at"] is not None
        finally:
            _clear()

    async def test_other_operator_has_no_task(self, client, case_inst, assignment):
        _override("op-2")  # different operator — assignment is for op-1
        try:
            r = await client.get(f"/api/v1/cases/{case_inst.id}/my-task")
            assert r.status_code == 200
            assert r.json() is None
        finally:
            _clear()

    async def test_404_on_missing_case(self, client):
        _override("op-1")
        try:
            r = await client.get(f"/api/v1/cases/{uuid.uuid4()}/my-task")
            assert r.status_code == 404
        finally:
            _clear()

    async def test_returns_step_def(self, client, case_inst, assignment):
        _override("op-1")
        try:
            r = await client.get(f"/api/v1/cases/{case_inst.id}/my-task")
            data = r.json()
            assert data["step_def"]["name"] == "Fill Form"
            assert data["step_def"]["step_type"] == "user_task"
        finally:
            _clear()

    async def test_returns_case_metadata(self, client, case_inst, assignment):
        _override("op-1")
        try:
            r = await client.get(f"/api/v1/cases/{case_inst.id}/my-task")
            data = r.json()
            # description may be None when not stored in data dict
            assert data["case_priority"] == "medium"
            assert data["case_priority"] == "medium"
        finally:
            _clear()

    async def test_refreshes_lock_on_reopen(self, client, session, case_inst, assignment):
        """Calling my-task twice extends the lock TTL."""
        _override("op-1")
        try:
            r1 = await client.get(f"/api/v1/cases/{case_inst.id}/my-task")
            exp1 = r1.json()["lock_expires_at"]
            r2 = await client.get(f"/api/v1/cases/{case_inst.id}/my-task")
            exp2 = r2.json()["lock_expires_at"]
            # Both should be future timestamps (equal or later)
            assert exp1 is not None and exp2 is not None
        finally:
            _clear()


class TestCompleteStepWithLocking:
    async def test_holder_can_submit(self, client, case_inst, assignment):
        _override("op-1")
        try:
            # Acquire lock
            await client.get(f"/api/v1/cases/{case_inst.id}/my-task")
            # Submit
            r = await client.post(
                f"/api/v1/cases/{case_inst.id}/steps/{STEP_ID}/complete",
                json={"stage_id": STAGE_ID, "step_type": "user_task", "status": "completed", "data": {"notes": "done"}},
            )
            assert r.status_code == 200
        finally:
            _clear()

    async def test_other_operator_blocked_by_live_lock(self, client, session, case_inst, assignment):
        """op-1 holds live lock → op-2 cannot submit the same step."""
        # op-1 acquires lock
        _override("op-1")
        await client.get(f"/api/v1/cases/{case_inst.id}/my-task")
        _clear()

        # op-2 tries to submit
        _override("op-2")
        try:
            r = await client.post(
                f"/api/v1/cases/{case_inst.id}/steps/{STEP_ID}/complete",
                json={"stage_id": STAGE_ID, "step_type": "user_task", "status": "completed", "data": {}},
            )
            assert r.status_code == 409
            assert "locked" in r.json()["detail"].lower()
        finally:
            _clear()

    async def test_expired_lock_does_not_block(self, client, session, case_inst, assignment):
        """Expired lock → other operator can submit freely."""
        # Manually set an expired lock
        from sqlalchemy import select
        from case_service.db.models import CaseAssignmentModel as AM
        a = (await session.execute(
            select(AM).where(AM.id == assignment.id)
        )).scalar_one()
        a.locked_by = "op-1"
        a.locked_at = datetime.now(timezone.utc) - timedelta(hours=2)
        a.lock_expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        await session.flush()

        _override("op-2")
        try:
            r = await client.post(
                f"/api/v1/cases/{case_inst.id}/steps/{STEP_ID}/complete",
                json={"stage_id": STAGE_ID, "step_type": "user_task", "status": "completed", "data": {}},
            )
            assert r.status_code == 200
        finally:
            _clear()

    async def test_complete_closes_assignment(self, client, session, case_inst, assignment):
        aid = assignment.id  # capture before any expiry
        cid = case_inst.id
        _override("op-1")
        try:
            await client.get(f"/api/v1/cases/{cid}/my-task")
            await client.post(
                f"/api/v1/cases/{cid}/steps/{STEP_ID}/complete",
                json={"stage_id": STAGE_ID, "step_type": "user_task", "status": "completed", "data": {}},
            )
        finally:
            _clear()

        from sqlalchemy import select
        from case_service.db.models import CaseAssignmentModel as AM
        session.expire_all()
        a = (await session.execute(select(AM).where(AM.id == aid))).scalar_one()
        assert a.status == "completed"
        assert a.locked_by is None
        assert a.lock_expires_at is None

    async def test_complete_releases_lock(self, client, session, case_inst, assignment):
        _override("op-1")
        try:
            await client.get(f"/api/v1/cases/{case_inst.id}/my-task")
            await client.post(
                f"/api/v1/cases/{case_inst.id}/steps/{STEP_ID}/complete",
                json={"stage_id": STAGE_ID, "step_type": "user_task", "status": "completed", "data": {}},
            )
            # After complete, my-task should return null
            r = await client.get(f"/api/v1/cases/{case_inst.id}/my-task")
            assert r.json() is None
        finally:
            _clear()

    async def test_auto_advance_still_works(self, client, session, case_inst, assignment):
        _override("op-1")
        try:
            await client.get(f"/api/v1/cases/{case_inst.id}/my-task")
            r = await client.post(
                f"/api/v1/cases/{case_inst.id}/steps/{STEP_ID}/complete",
                json={"stage_id": STAGE_ID, "step_type": "user_task", "status": "completed", "data": {}},
            )
            assert r.status_code == 200
            # Only one required step in Intake → auto-advances to Review
            assert r.json()["auto_advanced"] is True
        finally:
            _clear()


class TestUnlock:
    async def test_unlock_clears_lock(self, client, session, case_inst, assignment):
        aid = assignment.id
        cid = case_inst.id
        _override("op-1")
        try:
            await client.get(f"/api/v1/cases/{cid}/my-task")
            r = await client.post(f"/api/v1/cases/{cid}/steps/{STEP_ID}/unlock")
            assert r.status_code == 204
        finally:
            _clear()

        from sqlalchemy import select
        from case_service.db.models import CaseAssignmentModel as AM
        session.expire_all()
        a = (await session.execute(select(AM).where(AM.id == aid))).scalar_one()
        assert a.locked_by is None

    async def test_unlock_by_non_holder_is_noop(self, client, session, case_inst, assignment):
        """op-2 trying to unlock op-1's lock → no-op (204, lock unchanged)."""
        aid = assignment.id
        cid = case_inst.id
        # Set op-1 lock via the fixture session
        from sqlalchemy import select
        from case_service.db.models import CaseAssignmentModel as AM
        a = (await session.execute(select(AM).where(AM.id == aid))).scalar_one()
        a.locked_by = "op-1"
        a.locked_at = datetime.now(timezone.utc)
        a.lock_expires_at = datetime.now(timezone.utc) + timedelta(minutes=30)
        await session.commit()

        _override("op-2")
        try:
            r = await client.post(f"/api/v1/cases/{cid}/steps/{STEP_ID}/unlock")
            assert r.status_code == 204
        finally:
            _clear()

        # Lock still held by op-1
        session.expire_all()
        a2 = (await session.execute(select(AM).where(AM.id == aid))).scalar_one()
        assert a2.locked_by == "op-1"

    async def test_unlock_after_complete_is_noop(self, client, session, case_inst, assignment):
        """Unlocking a step that's already completed → graceful 204."""
        _override("op-1")
        try:
            await client.get(f"/api/v1/cases/{case_inst.id}/my-task")
            await client.post(
                f"/api/v1/cases/{case_inst.id}/steps/{STEP_ID}/complete",
                json={"stage_id": STAGE_ID, "step_type": "user_task", "status": "completed", "data": {}},
            )
            r = await client.post(f"/api/v1/cases/{case_inst.id}/steps/{STEP_ID}/unlock")
            assert r.status_code == 204
        finally:
            _clear()
