"""HELIX P34b — UserDirectory, dynamic targets, lifecycle wiring tests."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from httpx import AsyncClient

from case_service.core.sla_escalation import (
    resolve_dynamic_target, execute_action,
    resolve_escalation_tree_for_policy,
)
from case_service.core.user_directory import (
    get_user, get_manager, users_in_access_group, users_with_role,
    get_current_assignee_for_case,
)
from case_service.db.models import (
    CaseTypeModel, CaseInstanceModel, CaseSLAInstanceModel, CaseAssignmentModel,
    EscalationTreeModel, UserDirectoryModel,
)


# ── Fixtures ─────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def alice(session):
    u = UserDirectoryModel(
        user_id="alice", email="alice@helix.test", display_name="Alice",
        manager_user_id="boss", access_group_ids=["g-sales"], roles=["worker"],
    )
    session.add(u); await session.flush(); return u


@pytest_asyncio.fixture
async def boss(session):
    u = UserDirectoryModel(
        user_id="boss", email="boss@helix.test", display_name="Boss",
        manager_user_id="ceo", access_group_ids=["g-managers"], roles=["manager", "admin"],
    )
    session.add(u); await session.flush(); return u


@pytest_asyncio.fixture
async def ct(session):
    x = CaseTypeModel(
        name="P34b-Type", version="1.0.0",
        lifecycle_process_id="lp-p34b",
        definition_json={"stages": [], "sla_policies": []},
    )
    session.add(x); await session.flush(); return x


@pytest_asyncio.fixture
async def case(session, ct):
    c = CaseInstanceModel(
        case_type_id=ct.id, case_type_version="1.0.0",
        status="new", priority="medium", data={},
    )
    session.add(c); await session.flush(); return c


@pytest_asyncio.fixture
async def assignment_alice(session, case):
    a = CaseAssignmentModel(
        case_id=case.id, step_id="s1", assignee_type="user", assignee_id="alice",
        status="active",
    )
    session.add(a); await session.flush(); return a


def _sla(case_id) -> CaseSLAInstanceModel:
    now = datetime.now(timezone.utc)
    return CaseSLAInstanceModel(
        case_id=case_id, sla_policy_id="p", target_id="x",
        status="on_track", started_at=now, goal_at=now, deadline_at=now,
        escalation_history=[], pause_reasons_log=[], escalation_level=0,
        escalation_tree_snapshot={},
    )


# ── UserDirectory basics ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_01_get_user_by_id(session, alice):
    u = await get_user(session, "alice")
    assert u is not None
    assert u.display_name == "Alice"


@pytest.mark.asyncio
async def test_02_get_user_missing_returns_none(session):
    u = await get_user(session, "nobody")
    assert u is None


@pytest.mark.asyncio
async def test_03_get_manager(session, alice, boss):
    mgr = await get_manager(session, "alice")
    assert mgr == "boss"


@pytest.mark.asyncio
async def test_04_get_manager_missing_returns_none(session):
    mgr = await get_manager(session, "ghost")
    assert mgr is None


@pytest.mark.asyncio
async def test_05_users_in_access_group(session, alice, boss):
    sales = await users_in_access_group(session, "g-sales")
    assert sales == ["alice"]


@pytest.mark.asyncio
async def test_06_users_with_role(session, alice, boss):
    mgrs = await users_with_role(session, "manager")
    assert mgrs == ["boss"]


@pytest.mark.asyncio
async def test_07_inactive_users_filtered(session):
    u = UserDirectoryModel(user_id="retired", is_active=False, roles=["worker"])
    session.add(u); await session.flush()
    assert await get_user(session, "retired") is None


# ── get_current_assignee_for_case ────────────────────────────────────

@pytest.mark.asyncio
async def test_08_current_assignee_resolves(session, case, assignment_alice):
    u = await get_current_assignee_for_case(session, case.id)
    assert u == "alice"


@pytest.mark.asyncio
async def test_09_current_assignee_returns_none_for_queue(session, case):
    a = CaseAssignmentModel(
        case_id=case.id, step_id="s2", assignee_type="queue", assignee_id="q1",
        status="active",
    )
    session.add(a); await session.flush()
    u = await get_current_assignee_for_case(session, case.id)
    assert u is None


# ── resolve_dynamic_target ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_10_resolve_current_assignee(session, case, assignment_alice):
    t, i = await resolve_dynamic_target(session, case.id, "current_assignee", None)
    assert t == "user" and i == "alice"


@pytest.mark.asyncio
async def test_11_resolve_manager_of_current_assignee(session, case, alice, boss, assignment_alice):
    t, i = await resolve_dynamic_target(session, case.id, "manager_of_current_assignee", None)
    assert t == "user" and i == "boss"


@pytest.mark.asyncio
async def test_12_resolve_manager_without_manager_returns_empty(session, case, assignment_alice):
    # Alice exists but no manager row
    u = UserDirectoryModel(user_id="alice", manager_user_id=None)
    # replace existing alice
    await session.execute(UserDirectoryModel.__table__.delete().where(UserDirectoryModel.user_id == "alice"))
    session.add(u); await session.flush()
    t, i = await resolve_dynamic_target(session, case.id, "manager_of_current_assignee", None)
    assert t == "" and i == ""


@pytest.mark.asyncio
async def test_13_resolve_access_group(session, case):
    t, i = await resolve_dynamic_target(session, case.id, "access_group", "managers")
    assert t == "queue" and i == "access_group:managers"


@pytest.mark.asyncio
async def test_14_resolve_role(session, case):
    t, i = await resolve_dynamic_target(session, case.id, "role", "senior-manager")
    assert t == "queue" and i == "role:senior-manager"


@pytest.mark.asyncio
async def test_15_resolve_unknown_passthrough(session, case):
    t, i = await resolve_dynamic_target(session, case.id, "user", "frank")
    assert t == "user" and i == "frank"


# ── execute_action with dynamic resolution ───────────────────────────

@pytest.mark.asyncio
async def test_16_reassign_to_manager_resolves(session, case, alice, boss, assignment_alice):
    sla = _sla(case.id); session.add(sla); await session.flush()
    action = {"type": "reassign", "target_type": "manager_of_current_assignee"}
    res = await execute_action(session, case.id, action, sla)
    assert res["ok"] is True
    await session.refresh(assignment_alice)
    assert assignment_alice.assignee_id == "boss"
    assert assignment_alice.assignee_type == "user"


@pytest.mark.asyncio
async def test_17_reassign_access_group(session, case, assignment_alice):
    sla = _sla(case.id); session.add(sla); await session.flush()
    action = {"type": "reassign", "target_type": "access_group", "target_id": "managers"}
    res = await execute_action(session, case.id, action, sla)
    assert res["ok"] is True
    await session.refresh(assignment_alice)
    assert assignment_alice.assignee_type == "queue"
    assert assignment_alice.assignee_id == "access_group:managers"


@pytest.mark.asyncio
async def test_18_reassign_fails_when_target_unresolvable(session, case, assignment_alice):
    # Alice exists in assignment but not in user-directory, no manager
    sla = _sla(case.id); session.add(sla); await session.flush()
    action = {"type": "reassign", "target_type": "manager_of_current_assignee"}
    res = await execute_action(session, case.id, action, sla)
    assert res["ok"] is False
    assert "could not resolve" in (res.get("detail") or "")


# ── resolve_escalation_tree_for_policy ───────────────────────────────

@pytest.mark.asyncio
async def test_19_policy_tree_override_wins(session, ct):
    default_tree = EscalationTreeModel(
        name="Default", scope="case_type", case_type_id=ct.id,
        tree_json={"levels": []}, is_active=True,
    )
    override = EscalationTreeModel(
        name="Override", scope="global", tree_json={"levels": []}, is_active=True,
    )
    session.add(default_tree); session.add(override)
    await session.flush()

    policy = {"id": "p", "escalation_tree_id": str(override.id)}
    resolved = await resolve_escalation_tree_for_policy(session, ct.id, policy)
    assert resolved is not None
    assert resolved.name == "Override"


@pytest.mark.asyncio
async def test_20_policy_override_missing_falls_back(session, ct):
    default_tree = EscalationTreeModel(
        name="CT-Default", scope="case_type", case_type_id=ct.id,
        tree_json={"levels": []}, is_active=True,
    )
    session.add(default_tree); await session.flush()

    policy = {"id": "p", "escalation_tree_id": str(uuid.uuid4())}  # missing
    resolved = await resolve_escalation_tree_for_policy(session, ct.id, policy)
    assert resolved is not None
    assert resolved.name == "CT-Default"


@pytest.mark.asyncio
async def test_21_policy_override_inactive_falls_back(session, ct):
    default_tree = EscalationTreeModel(
        name="CT-Default", scope="case_type", case_type_id=ct.id,
        tree_json={"levels": []}, is_active=True,
    )
    inactive = EscalationTreeModel(
        name="Dead", scope="global", tree_json={"levels": []}, is_active=False,
    )
    session.add(default_tree); session.add(inactive)
    await session.flush()

    policy = {"id": "p", "escalation_tree_id": str(inactive.id)}
    resolved = await resolve_escalation_tree_for_policy(session, ct.id, policy)
    assert resolved.name == "CT-Default"


@pytest.mark.asyncio
async def test_22_no_override_uses_case_type_default(session, ct):
    default_tree = EscalationTreeModel(
        name="CT-Default", scope="case_type", case_type_id=ct.id,
        tree_json={"levels": []}, is_active=True,
    )
    session.add(default_tree); await session.flush()

    policy = {"id": "p"}  # no escalation_tree_id
    resolved = await resolve_escalation_tree_for_policy(session, ct.id, policy)
    assert resolved.name == "CT-Default"


# ── User directory API ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_23_api_create_and_get_user(client: AsyncClient):
    body = {
        "user_id": "carol", "display_name": "Carol", "email": "carol@x.com",
        "manager_user_id": "boss", "access_group_ids": ["g1"], "roles": ["worker"],
        "timezone": "UTC", "is_active": True, "metadata_json": {},
    }
    r = await client.post("/api/v1/user-directory", json=body)
    assert r.status_code == 201, r.text

    r2 = await client.get("/api/v1/user-directory/carol")
    assert r2.status_code == 200
    assert r2.json()["display_name"] == "Carol"


@pytest.mark.asyncio
async def test_24_api_bulk_sync(client: AsyncClient):
    entries = [
        {"user_id": "u1", "roles": ["worker"], "access_group_ids": [], "timezone": "UTC", "metadata_json": {}, "is_active": True},
        {"user_id": "u2", "roles": ["manager"], "access_group_ids": [], "timezone": "UTC", "metadata_json": {}, "is_active": True},
    ]
    r = await client.post("/api/v1/user-directory/bulk-sync", json=entries)
    assert r.status_code == 200
    body = r.json()
    assert body["created"] == 2
    assert body["total"] == 2

    # Re-sync should update, not re-create
    entries[0]["display_name"] = "Updated"
    r2 = await client.post("/api/v1/user-directory/bulk-sync", json=entries)
    body2 = r2.json()
    assert body2["updated"] == 2
    assert body2["created"] == 0


@pytest.mark.asyncio
async def test_25_api_list_with_filter(client: AsyncClient):
    await client.post("/api/v1/user-directory", json={"user_id": "xray", "display_name": "X", "access_group_ids": [], "roles": [], "timezone": "UTC", "metadata_json": {}, "is_active": True})
    r = await client.get("/api/v1/user-directory?q=xray")
    assert r.status_code == 200
    users = r.json()
    assert any(u["user_id"] == "xray" for u in users)
