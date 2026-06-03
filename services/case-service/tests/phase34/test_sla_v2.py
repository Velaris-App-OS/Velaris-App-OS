"""HELIX P34 — SLA v2 & Escalation tests."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import AsyncClient

from case_service.core.sla_escalation import (
    compute_level_trigger_at, precompute_level_schedule,
    resolve_escalation_tree, snapshot_tree, apply_level,
    record_pause_with_reason, record_resume,
)
from case_service.core.sla_tracker import start_sla_v2
from case_service.db.models import (
    CaseTypeModel, CaseInstanceModel, CaseSLAInstanceModel,
    EscalationTreeModel, CaseAssignmentModel,
)


# ── Fixtures ─────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def seeded_case_type(session):
    ct = CaseTypeModel(
        name="P34TestType", version="1.0.0",
        lifecycle_process_id="lp-p34",
        definition_json={"stages": []},
    )
    session.add(ct)
    await session.flush()
    return ct


@pytest_asyncio.fixture
async def seeded_case(session, seeded_case_type):
    case = CaseInstanceModel(
        case_type_id=seeded_case_type.id, case_type_version="1.0.0",
        status="new", priority="medium", data={},
    )
    session.add(case)
    await session.flush()
    return case


@pytest_asyncio.fixture
async def active_assignment(session, seeded_case):
    a = CaseAssignmentModel(
        case_id=seeded_case.id,
        step_id="step-1",
        assignee_type="user", assignee_id="alice",
        status="active",
    )
    session.add(a)
    await session.flush()
    return a


# ── Trigger computation (pure-function tests, no DB) ────────────────

def test_01_goal_pct_trigger():
    started = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
    goal = started + timedelta(hours=10)
    deadline = started + timedelta(hours=20)
    fires = compute_level_trigger_at(
        {"trigger": {"type": "goal_pct", "value": 50}}, started, goal, deadline,
    )
    assert fires == started + timedelta(hours=5)


def test_02_deadline_pct_trigger():
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    goal = started + timedelta(hours=4)
    deadline = started + timedelta(hours=10)
    fires = compute_level_trigger_at(
        {"trigger": {"type": "deadline_pct", "value": 80}}, started, goal, deadline,
    )
    assert fires == started + timedelta(hours=8)


def test_03_fixed_duration_trigger():
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    goal = started + timedelta(hours=4)
    deadline = started + timedelta(hours=10)
    fires = compute_level_trigger_at(
        {"trigger": {"type": "fixed_duration", "value": "PT90M"}}, started, goal, deadline,
    )
    assert fires == started + timedelta(minutes=90)


def test_04_at_breach_trigger_equals_deadline():
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    goal = started + timedelta(hours=4)
    deadline = started + timedelta(hours=10)
    fires = compute_level_trigger_at(
        {"trigger": {"type": "at_breach"}}, started, goal, deadline,
    )
    assert fires == deadline


def test_05_unknown_trigger_returns_none():
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert compute_level_trigger_at(
        {"trigger": {"type": "xyz"}}, started, started, started,
    ) is None


def test_06_precompute_schedule_sorted_by_fire_time():
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    goal = started + timedelta(hours=10)
    deadline = started + timedelta(hours=20)
    snapshot = {"levels": [
        {"level": 2, "name": "late", "trigger": {"type": "goal_pct", "value": 80}, "actions": []},
        {"level": 1, "name": "early", "trigger": {"type": "goal_pct", "value": 30}, "actions": []},
        {"level": 3, "name": "latest", "trigger": {"type": "at_breach"}, "actions": []},
    ]}
    sched = precompute_level_schedule(snapshot, started, goal, deadline)
    assert [s["level"] for s in sched] == [1, 2, 3]


# ── Tree resolution (DB) ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_07_resolve_case_type_tree_wins_over_global(session, seeded_case_type):
    global_tree = EscalationTreeModel(
        name="Global", scope="global", case_type_id=None,
        tree_json={"levels": []}, is_active=True,
    )
    ct_tree = EscalationTreeModel(
        name="CaseType", scope="case_type", case_type_id=seeded_case_type.id,
        tree_json={"levels": [{"level": 1, "name": "x", "trigger": {"type": "goal_pct", "value": 50}, "actions": []}]},
        is_active=True,
    )
    session.add(global_tree); session.add(ct_tree)
    await session.flush()

    resolved = await resolve_escalation_tree(session, seeded_case_type.id)
    assert resolved is not None
    assert resolved.name == "CaseType"


@pytest.mark.asyncio
async def test_08_resolve_falls_back_to_global(session, seeded_case_type):
    global_tree = EscalationTreeModel(
        name="Only-Global", scope="global", case_type_id=None,
        tree_json={"levels": []}, is_active=True,
    )
    session.add(global_tree)
    await session.flush()

    resolved = await resolve_escalation_tree(session, seeded_case_type.id)
    assert resolved is not None
    assert resolved.name == "Only-Global"


@pytest.mark.asyncio
async def test_09_inactive_trees_ignored(session, seeded_case_type):
    inactive = EscalationTreeModel(
        name="Dead", scope="global", case_type_id=None,
        tree_json={"levels": []}, is_active=False,
    )
    session.add(inactive)
    await session.flush()

    resolved = await resolve_escalation_tree(session, seeded_case_type.id)
    assert resolved is None


# ── start_sla_v2: snapshot + schedule ───────────────────────────────

@pytest.mark.asyncio
async def test_10_start_sla_v2_snapshots_tree(session, seeded_case, seeded_case_type):
    tree = EscalationTreeModel(
        name="T1", scope="global", case_type_id=None,
        tree_json={"levels": [
            {"level": 1, "name": "remind", "trigger": {"type": "goal_pct", "value": 50}, "actions": []},
        ]}, is_active=True,
    )
    session.add(tree)
    await session.flush()

    policy = {"id": "policy-1", "goal_duration": "PT4H", "deadline_duration": "PT24H"}
    sla = await start_sla_v2(
        session, case_id=seeded_case.id, case_type_id=seeded_case_type.id,
        sla_policy=policy, target_id=str(seeded_case.id),
    )
    assert sla.escalation_tree_snapshot["tree_id"] == str(tree.id)
    assert len(sla.escalation_tree_snapshot["schedule"]) == 1
    assert sla.escalation_level == 0


@pytest.mark.asyncio
async def test_11_start_sla_v2_without_tree_is_ok(session, seeded_case, seeded_case_type):
    policy = {"id": "policy-x", "goal_duration": "PT1H", "deadline_duration": "PT2H"}
    sla = await start_sla_v2(
        session, case_id=seeded_case.id, case_type_id=seeded_case_type.id,
        sla_policy=policy, target_id=str(seeded_case.id),
    )
    assert sla.escalation_tree_snapshot.get("tree_id") in (None, "")
    assert sla.escalation_tree_snapshot.get("levels", []) == []


# ── apply_level: reassign + priority actions ────────────────────────

@pytest.mark.asyncio
async def test_12_apply_level_reassigns_active_assignments(
    session, seeded_case, active_assignment,
):
    sla = CaseSLAInstanceModel(
        case_id=seeded_case.id, sla_policy_id="p1", target_id="x",
        status="on_track", started_at=datetime.now(timezone.utc),
        goal_at=datetime.now(timezone.utc) + timedelta(hours=1),
        deadline_at=datetime.now(timezone.utc) + timedelta(hours=2),
        escalation_history=[], pause_reasons_log=[], escalation_level=0,
        escalation_tree_snapshot={},
    )
    session.add(sla); await session.flush()

    level_entry = {
        "level": 1, "name": "escalate-to-managers",
        "actions": [
            {"type": "reassign", "target_type": "queue", "target_id": "managers"},
            {"type": "priority", "set": "high"},
        ],
    }
    result = await apply_level(session, seeded_case.id, sla, level_entry)
    await session.refresh(active_assignment)
    await session.refresh(seeded_case)
    assert active_assignment.assignee_id == "managers"
    assert active_assignment.assignee_type == "queue"
    assert seeded_case.priority == "high"
    assert sla.escalation_level == 1
    assert len(sla.escalation_history) == 1


@pytest.mark.asyncio
async def test_13_apply_level_records_failures(session, seeded_case):
    sla = CaseSLAInstanceModel(
        case_id=seeded_case.id, sla_policy_id="p2", target_id="x",
        status="on_track", started_at=datetime.now(timezone.utc),
        goal_at=datetime.now(timezone.utc), deadline_at=datetime.now(timezone.utc),
        escalation_history=[], pause_reasons_log=[], escalation_level=0,
        escalation_tree_snapshot={},
    )
    session.add(sla); await session.flush()

    level_entry = {
        "level": 1,
        "actions": [
            {"type": "reassign"},  # missing target_id — should fail gracefully
            {"type": "unknown"},
        ],
    }
    result = await apply_level(session, seeded_case.id, sla, level_entry)
    actions = result["actions"]
    assert len(actions) == 2
    assert actions[0]["ok"] is False
    assert actions[1]["ok"] is False


# ── Pause reason tracking ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_14_pause_reason_is_logged(session, seeded_case):
    sla = CaseSLAInstanceModel(
        case_id=seeded_case.id, sla_policy_id="p3", target_id="x",
        status="on_track", started_at=datetime.now(timezone.utc),
        goal_at=datetime.now(timezone.utc), deadline_at=datetime.now(timezone.utc),
        pause_reasons_log=[], escalation_history=[], escalation_level=0,
        escalation_tree_snapshot={},
    )
    session.add(sla); await session.flush()

    await record_pause_with_reason(session, sla, "waiting on customer", actor_id="alice")
    assert sla.pause_reason == "waiting on customer"
    assert len(sla.pause_reasons_log) == 1
    assert sla.pause_reasons_log[0]["reason"] == "waiting on customer"
    assert sla.pause_reasons_log[0]["actor_id"] == "alice"


@pytest.mark.asyncio
async def test_15_resume_clears_reason_and_logs(session, seeded_case):
    sla = CaseSLAInstanceModel(
        case_id=seeded_case.id, sla_policy_id="p4", target_id="x",
        status="paused", started_at=datetime.now(timezone.utc),
        goal_at=datetime.now(timezone.utc), deadline_at=datetime.now(timezone.utc),
        pause_reason="waiting", pause_reasons_log=[{"type": "pause", "reason": "waiting"}],
        escalation_history=[], escalation_level=0, escalation_tree_snapshot={},
    )
    session.add(sla); await session.flush()

    await record_resume(session, sla, actor_id="bob")
    assert sla.pause_reason is None
    assert len(sla.pause_reasons_log) == 2
    assert sla.pause_reasons_log[1]["type"] == "resume"
    assert sla.pause_reasons_log[1]["actor_id"] == "bob"


# ── API endpoints ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_16_api_create_and_list_tree(client: AsyncClient, seeded_case_type):
    body = {
        "name": "API Tree", "scope": "global", "case_type_id": None,
        "description": "t",
        "tree_json": {"levels": [
            {"level": 1, "name": "x", "trigger": {"type": "goal_pct", "value": 50}, "actions": []},
        ]}, "is_active": True,
    }
    r = await client.post("/api/v1/escalation-trees", json=body)
    assert r.status_code == 201, r.text
    tree_id = r.json()["id"]

    r2 = await client.get("/api/v1/escalation-trees")
    assert r2.status_code == 200
    ids = [t["id"] for t in r2.json()]
    assert tree_id in ids


@pytest.mark.asyncio
async def test_17_api_preview_returns_schedule(client: AsyncClient):
    body = {
        "tree_json": {"levels": [
            {"level": 1, "name": "a", "trigger": {"type": "goal_pct", "value": 50}, "actions": []},
            {"level": 2, "name": "b", "trigger": {"type": "at_breach"}, "actions": []},
        ]},
        "goal_duration": "PT4H",
        "deadline_duration": "PT8H",
    }
    r = await client.post("/api/v1/escalation-trees/preview", json=body)
    assert r.status_code == 200, r.text
    body_resp = r.json()
    assert len(body_resp["schedule"]) == 2
    # First level fires before deadline
    assert body_resp["schedule"][0]["fires_at"] < body_resp["deadline_at"]


@pytest.mark.asyncio
async def test_18_api_preview_rejects_bad_duration(client: AsyncClient):
    body = {
        "tree_json": {"levels": []},
        "goal_duration": "not-a-duration",
        "deadline_duration": "PT8H",
    }
    r = await client.post("/api/v1/escalation-trees/preview", json=body)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_19_api_update_tree(client: AsyncClient):
    body = {
        "name": "ToUpdate", "scope": "global", "case_type_id": None, "description": "",
        "tree_json": {"levels": []}, "is_active": True,
    }
    r = await client.post("/api/v1/escalation-trees", json=body)
    tree_id = r.json()["id"]

    r2 = await client.patch(
        f"/api/v1/escalation-trees/{tree_id}",
        json={"name": "Updated", "is_active": False},
    )
    assert r2.status_code == 200
    assert r2.json()["name"] == "Updated"
    assert r2.json()["is_active"] is False


@pytest.mark.asyncio
async def test_20_api_case_type_scope_requires_case_type_id(client: AsyncClient):
    body = {
        "name": "Bad", "scope": "case_type", "case_type_id": None, "description": "",
        "tree_json": {"levels": []}, "is_active": True,
    }
    r = await client.post("/api/v1/escalation-trees", json=body)
    assert r.status_code == 400
