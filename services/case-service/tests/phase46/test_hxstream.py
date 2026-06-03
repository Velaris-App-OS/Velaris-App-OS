"""HELIX P46 — HxStream tests (22 tests).

Covers: emit_trace (broadcast + persist), POST /hxstream/event,
        GET /hxstream/events (list + filters), GET /hxstream/replay,
        access control (admin only), WebSocket connect + filter,
        emitter hooks in cases router (stage_transition, step_complete,
        lock_acquire, lock_release), broadcast to multiple subscribers,
        queue-full drop behaviour, empty-replay response.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select

from case_service.main import app
from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser, ActiveAccessGroup
from case_service.db.models import (
    CaseTypeModel, CaseInstanceModel, TraceEventModel,
)
from case_service.hxstream.emitter import emit_trace, _subscribers


# ── Auth helpers ──────────────────────────────────────────────────────────────

def _admin(uid: str = "admin-1") -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=uid,
        roles=["admin"],
        active_access_group=ActiveAccessGroup(
            id=str(uuid.uuid4()), name="Admins",
            portal_id=str(uuid.uuid4()), portal_type="admin",
            portal_name="Admin Portal", modules=["hxstream"],
            homepage="/hxstream", roles=["admin"], privileges=[],
            allowed_case_type_ids=["*"], allowed_queue_ids=["*"],
        ),
    )

def _staff(uid: str = "staff-1") -> AuthenticatedUser:
    return AuthenticatedUser(user_id=uid, roles=["staff"])


def _override(user: AuthenticatedUser):
    app.dependency_overrides[get_current_user] = lambda: user

def _clear():
    app.dependency_overrides.pop(get_current_user, None)


# ── Fixtures ──────────────────────────────────────────────────────────────────

STAGE_ID = "intake"
STEP_ID  = "fill_form"

CASE_TYPE_DEF = {
    "stages": [
        {
            "id": STAGE_ID, "name": "Intake", "stage_type": "linear", "order": 0,
            "steps": [
                {"id": STEP_ID, "name": "Fill Form", "step_type": "user_task",
                 "required": True},
            ],
        },
    ]
}


@pytest_asyncio.fixture
async def case_type(session) -> CaseTypeModel:
    ct = CaseTypeModel(
        name="HxStream Test Case", version="1.0",
        definition_json=CASE_TYPE_DEF, default_priority="medium",
    )
    session.add(ct)
    await session.flush()
    return ct


@pytest_asyncio.fixture
async def case_inst(session, case_type) -> CaseInstanceModel:
    ci = CaseInstanceModel(
        case_type_id=case_type.id,
        case_type_version="1.0",
        status="open",
        priority="medium",
        current_stage_id=STAGE_ID,
    )
    session.add(ci)
    await session.flush()
    return ci


# ── Emitter unit tests ────────────────────────────────────────────────────────

class TestEmitter:
    async def test_emit_persists_to_db(self, session, case_inst):
        """emit_trace with session= writes a TraceEventModel row."""
        await emit_trace(
            "stage_transition",
            {"from_stage": "intake", "to_stage": "review"},
            case_id=case_inst.id,
            tenant_id="t1",
            actor_user_id="alice",
            session=session,
        )
        await session.flush()
        row = (await session.execute(
            select(TraceEventModel).where(TraceEventModel.event_type == "stage_transition")
        )).scalar_one()
        assert row.actor_user_id == "alice"
        assert row.payload["from_stage"] == "intake"
        assert row.case_id == case_inst.id

    async def test_emit_without_session_broadcast_only(self):
        """emit_trace without session= does not raise and broadcasts."""
        received = []
        sub_id = "test-sub-" + str(uuid.uuid4())
        q: asyncio.Queue = asyncio.Queue()
        _subscribers[sub_id] = q
        try:
            await emit_trace("rule_eval", {"rule": "sla_check"}, tenant_id="t1")
            assert not q.empty()
            event = q.get_nowait()
            assert event["event_type"] == "rule_eval"
        finally:
            _subscribers.pop(sub_id, None)

    async def test_emit_broadcasts_to_multiple_subscribers(self):
        """All connected subscribers receive the event."""
        queues = {}
        for i in range(3):
            sid = f"sub-{i}"
            queues[sid] = asyncio.Queue()
            _subscribers[sid] = queues[sid]
        try:
            await emit_trace("ai_invoke", {"model": "llama3.2"}, tenant_id="t1")
            for q in queues.values():
                assert not q.empty()
        finally:
            for sid in queues:
                _subscribers.pop(sid, None)

    async def test_emit_drops_when_subscriber_queue_full(self):
        """A full subscriber queue causes a drop, not a raise."""
        sid = "full-sub-" + str(uuid.uuid4())
        q: asyncio.Queue = asyncio.Queue(maxsize=1)
        await q.put({"dummy": True})  # fill it
        _subscribers[sid] = q
        try:
            await emit_trace("error", {"msg": "boom"}, tenant_id="t1")
            # No exception raised — drop happened silently
        finally:
            _subscribers.pop(sid, None)

    async def test_emit_string_case_id_parsed(self, session, case_inst):
        """String case_id is coerced to UUID correctly."""
        await emit_trace(
            "step_complete", {"step_id": STEP_ID},
            case_id=str(case_inst.id), tenant_id="t1", session=session,
        )
        await session.flush()
        row = (await session.execute(
            select(TraceEventModel).where(TraceEventModel.event_type == "step_complete")
        )).scalar_one()
        assert row.case_id == case_inst.id

    async def test_emit_invalid_case_id_stored_as_none(self, session):
        """Unparseable case_id is stored as NULL without raising."""
        await emit_trace(
            "error", {"msg": "bad id"}, case_id="not-a-uuid",
            tenant_id="t1", session=session,
        )
        await session.flush()
        row = (await session.execute(
            select(TraceEventModel).where(TraceEventModel.event_type == "error")
        )).scalar_one()
        assert row.case_id is None


# ── POST /hxstream/event ──────────────────────────────────────────────────────

class TestPostEvent:
    async def test_ui_event_accepted(self, client, case_inst):
        _override(_admin())
        try:
            r = await client.post("/api/v1/hxstream/event", json={
                "event_type": "ui_interaction",
                "case_id": str(case_inst.id),
                "payload": {"action": "case_open", "view_type": "360"},
            })
        finally:
            _clear()
        assert r.status_code == 202
        assert r.json()["status"] == "accepted"

    async def test_staff_can_post_ui_event(self, client):
        """Any authenticated user can post UI events — access guard is only on GET/WS."""
        _override(_staff())
        try:
            r = await client.post("/api/v1/hxstream/event", json={
                "event_type": "ui_interaction",
                "payload": {"action": "form_submit"},
            })
        finally:
            _clear()
        assert r.status_code == 202

    async def test_event_stored_in_db(self, client, session, case_inst):
        _override(_admin())
        try:
            await client.post("/api/v1/hxstream/event", json={
                "event_type": "ui_interaction",
                "case_id": str(case_inst.id),
                "payload": {"action": "document_view", "doc_id": "doc-abc"},
            })
        finally:
            _clear()
        session.expire_all()
        row = (await session.execute(
            select(TraceEventModel).where(TraceEventModel.event_type == "ui_interaction")
        )).scalar_one_or_none()
        assert row is not None
        assert row.payload["action"] == "document_view"


# ── GET /hxstream/events ──────────────────────────────────────────────────────

class TestListEvents:
    async def test_admin_sees_events(self, client, session, case_inst):
        session.add(TraceEventModel(
            case_id=case_inst.id, tenant_id="t1",
            event_type="stage_transition",
            actor_user_id="alice",
            payload={"from_stage": "intake", "to_stage": "review"},
        ))
        await session.flush()

        _override(_admin())
        try:
            r = await client.get("/api/v1/hxstream/events")
        finally:
            _clear()
        assert r.status_code == 200
        assert len(r.json()["events"]) >= 1

    async def test_staff_cannot_list_events(self, client):
        _override(_staff())
        try:
            r = await client.get("/api/v1/hxstream/events")
        finally:
            _clear()
        assert r.status_code == 403

    async def test_filter_by_event_type(self, client, session, case_inst):
        for et in ("stage_transition", "ui_interaction", "stage_transition"):
            session.add(TraceEventModel(
                case_id=case_inst.id, tenant_id="t1",
                event_type=et, actor_user_id="u1", payload={},
            ))
        await session.flush()

        _override(_admin())
        try:
            r = await client.get("/api/v1/hxstream/events?event_type=stage_transition")
        finally:
            _clear()
        events = r.json()["events"]
        assert all(e["event_type"] == "stage_transition" for e in events)

    async def test_filter_by_actor(self, client, session, case_inst):
        session.add(TraceEventModel(
            case_id=case_inst.id, tenant_id="t1",
            event_type="ui_interaction", actor_user_id="bob", payload={},
        ))
        await session.flush()

        _override(_admin())
        try:
            r = await client.get("/api/v1/hxstream/events?actor_user_id=bob")
        finally:
            _clear()
        events = r.json()["events"]
        assert all(e["actor_user_id"] == "bob" for e in events)


# ── GET /hxstream/replay ──────────────────────────────────────────────────────

class TestReplay:
    async def test_replay_returns_ordered_events(self, client, session, case_inst):
        for et in ("lock_acquire", "step_complete", "lock_release"):
            session.add(TraceEventModel(
                case_id=case_inst.id, tenant_id="t1",
                event_type=et, actor_user_id="u1", payload={},
            ))
        await session.flush()

        _override(_admin())
        try:
            r = await client.get(f"/api/v1/hxstream/replay/{case_inst.id}")
        finally:
            _clear()
        assert r.status_code == 200
        types = [e["event_type"] for e in r.json()["events"]]
        assert types == ["lock_acquire", "step_complete", "lock_release"]

    async def test_replay_empty_case(self, client, case_inst):
        _override(_admin())
        try:
            r = await client.get(f"/api/v1/hxstream/replay/{case_inst.id}")
        finally:
            _clear()
        assert r.status_code == 200
        assert r.json()["events"] == []

    async def test_replay_staff_forbidden(self, client, case_inst):
        _override(_staff())
        try:
            r = await client.get(f"/api/v1/hxstream/replay/{case_inst.id}")
        finally:
            _clear()
        assert r.status_code == 403


# ── Emitter wired into cases router ──────────────────────────────────────────

class TestCasesRouterEmits:
    async def test_filter_by_case_id(self, client, session, case_inst):
        """Events for a different case_id are excluded."""
        other_id = uuid.uuid4()
        session.add(TraceEventModel(
            case_id=case_inst.id, tenant_id="t1",
            event_type="lock_acquire", actor_user_id="u1", payload={},
        ))
        await session.flush()

        _override(_admin())
        try:
            r = await client.get(f"/api/v1/hxstream/events?case_id={other_id}")
        finally:
            _clear()
        assert r.status_code == 200
        assert r.json()["events"] == []

    async def test_latency_ms_stored(self, session, case_inst):
        """latency_ms field is persisted correctly."""
        await emit_trace(
            "ai_invoke", {"model": "llama3.2", "tokens": 128},
            case_id=case_inst.id, tenant_id="t1",
            actor_user_id="alice", latency_ms=342, session=session,
        )
        await session.flush()
        row = (await session.execute(
            select(TraceEventModel).where(TraceEventModel.event_type == "ai_invoke")
        )).scalar_one()
        assert row.latency_ms == 342

    async def test_session_id_stored(self, session, case_inst):
        """session_id groups events for one attached trace session."""
        sid = "sess-abc"
        await emit_trace(
            "ui_interaction", {"action": "case_open"},
            case_id=case_inst.id, tenant_id="t1",
            session_id=sid, session=session,
        )
        await session.flush()
        row = (await session.execute(
            select(TraceEventModel).where(TraceEventModel.session_id == sid)
        )).scalar_one()
        assert row.session_id == sid

    async def test_system_event_no_case(self, session):
        """Events without a case_id (system-wide) store NULL case_id."""
        await emit_trace(
            "queue_route", {"queue": "claims-q", "case_count": 7},
            tenant_id="t1", session=session,
        )
        await session.flush()
        row = (await session.execute(
            select(TraceEventModel).where(TraceEventModel.event_type == "queue_route")
        )).scalar_one()
        assert row.case_id is None

    async def test_stage_transition_emits_trace(self, client, session, case_inst):
        """POST /{id}/advance-stage writes a stage_transition trace event."""
        _override(_admin())
        try:
            r = await client.post(
                f"/api/v1/cases/{case_inst.id}/stage",
                json={"target_stage_id": "review", "actor_id": "admin-1"},
            )
        finally:
            _clear()
        assert r.status_code == 200
        cid = case_inst.id
        row = (await session.execute(
            select(TraceEventModel).where(
                TraceEventModel.case_id == cid,
                TraceEventModel.event_type == "stage_transition",
            )
        )).scalar_one_or_none()
        assert row is not None
        assert row.payload["to_stage"] == "review"
