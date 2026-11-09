"""The five actions §7 unblocked, and the automatic `in_progress`.

Design: `docs/Future/Done/CASE-ACTIONS-STATUS-DESIGN.md` §7, answered 2026-09-08.
Migration **199** (soft delete) is the only schema change; the rest is registry,
handlers and one rule.

What these pin beyond "it works":

* **Archive seals, and the seal has exactly one door.** Every action is refused
  from `archived` except `reopen`. An action with literally no reverse is one an
  operator who archived the wrong case can never undo, and "no way back" is a
  worse property for a control than "the way back is privileged and audited".
* **Delete is soft, and soft still means gone.** The row and its hash-linked
  history stay; the case leaves every list, every count and every read. A soft
  delete any query can forget is not a delete.
* **Transfer knows what it will not do.** `tenant` is refused **by name**, with
  the reason, rather than falling through to a generic "unknown target" — the
  refusal has to be legible or somebody will assume it is unimplemented rather
  than deliberately excluded.
* **The automatic `in_progress` never overrides a designer.** A stage carrying
  its own `on_enter_status` stands the rule down entirely — not "the stage wins
  the race", but "the rule does not run" — because a configuration a designer
  can see must not be undone by one nobody can.
* **And the gate that would have broken silently.** Auto-advance was gated on a
  literal `["open", "new", "reopened"]`. The moment `in_progress` became
  automatic, the *second* step completed on any case would have stopped
  advancing. It now asks the status set.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from velaris_shared.db.models import (
    AccessGroupModel,
    CaseInstanceModel,
    CaseRelationshipModel,
    CaseStatusModel,
    CaseTypeModel,
    HxGuardTupleModel,
    TenantModel,
    UserDirectoryModel,
)

from case_service.case_actions import registry, stages, statuses
from case_service.main import app
from case_service.tenancy import identity as tenancy
from tests.conftest import LIFECYCLE_PROCESS_ID, TestSessionFactory

pytestmark = pytest.mark.asyncio


def _token(username: str, roles=("admin",)) -> str:
    from velaris_shared.auth.jwt_handler import create_dev_token
    from velaris_shared.config import get_settings

    s = get_settings()
    return create_dev_token(
        user_id=str(uuid.uuid4()), username=username, roles=list(roles),
        secret=s.auth_secret, private_key=s.auth_rsa_private_key or "")


def _headers(username: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(username)}"}


@pytest_asyncio.fixture(autouse=True)
async def _case_creators(setup_db):
    """Creating a case needs an access group granting it (2026-10-18)."""
    from tests.conftest import grant_case_create
    await grant_case_create('ren')


@pytest_asyncio.fixture
async def api():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def cased():
    tenancy.clear_cache()
    async with TestSessionFactory() as db:
        tenant = TenantModel(id=uuid.uuid4(), slug="rem", name="Remaining Co")
        db.add(tenant)
        db.add(UserDirectoryModel(id=uuid.uuid4(), user_id="ren", tenant_id="rem"))
        ct = CaseTypeModel(
            id=uuid.uuid4(), name="Remaining CT", version="1.0.0",
            lifecycle_process_id=LIFECYCLE_PROCESS_ID,
            definition_json={"stages": [
                {"id": "intake", "name": "Intake", "stage_type": "linear",
                 "order": 0, "steps": []}]})
        db.add(ct)
        await db.flush()
        case = CaseInstanceModel(
            id=uuid.uuid4(), case_number="REM-1", case_type_id=ct.id,
            case_type_version="1.0.0", status="open", priority="medium",
            current_stage_id="intake", tenant_id=tenant.id,
            created_by="ren", data={})
        db.add(case)
        await db.commit()
        yield {"tenant": tenant, "case_type": ct.id, "case": case.id}
    tenancy.clear_cache()


# ── the vocabulary is still closed, and still complete ───────────────────────

async def test_every_new_action_has_a_handler():
    from case_service.case_actions import dispatcher

    assert set(registry.ACTIONS) == set(dispatcher._HANDLERS)
    assert {"archive", "delete", "restore", "escalate", "split",
            "transfer"} <= set(registry.ACTIONS)


async def test_merge_and_tags_are_absent_and_stay_absent():
    """Dropped 2026-09-08, and deleted from the design rather than deferred.
    An item that stays listed gets raised again."""
    for gone in ("merge", "add_tag", "remove_tag", "tag"):
        assert gone not in registry.ACTIONS


async def test_every_new_privilege_is_a_real_privilege():
    from velaris_shared.hxguard.actions import ACTIONS as GUARD

    for name in ("archive", "delete", "restore", "escalate", "split", "transfer"):
        assert registry.ACTIONS[name].privilege in GUARD, name


# ── archive ──────────────────────────────────────────────────────────────────

async def test_archive_seals_the_case(cased, api):
    r = await api.post(f"/api/v1/cases/{cased['case']}/actions/archive",
                       json={"params": {}}, headers=_headers("ren"))
    assert r.status_code == 200, r.text
    assert r.json()["case"]["status"] == "archived"


async def test_nothing_may_act_on_an_archived_case(cased, api):
    await api.post(f"/api/v1/cases/{cased['case']}/actions/archive",
                   json={"params": {}}, headers=_headers("ren"))
    for action, params in (("add_note", {"body": "hi"}),
                           ("close", {}),
                           ("change_priority", {"priority": "high"}),
                           ("escalate", {}),
                           ("delete", {})):
        r = await api.post(f"/api/v1/cases/{cased['case']}/actions/{action}",
                           json={"params": params}, headers=_headers("ren"))
        assert r.status_code == 409, f"{action} -> {r.status_code}"
        assert "VEL-CASE-ACTION-SEALED-409" in str(r.json()), action


async def test_reopen_is_the_one_door_out_of_archived(cased, api):
    """An action with no reverse is one nobody can recover from."""
    await api.post(f"/api/v1/cases/{cased['case']}/actions/archive",
                   json={"params": {}}, headers=_headers("ren"))
    r = await api.post(f"/api/v1/cases/{cased['case']}/actions/reopen",
                       json={"params": {}}, headers=_headers("ren"))
    assert r.status_code == 200, r.text
    assert r.json()["case"]["status"] == "reopened"


# ── delete / restore ─────────────────────────────────────────────────────────

async def test_delete_is_soft_and_keeps_the_row(cased, api):
    r = await api.post(f"/api/v1/cases/{cased['case']}/actions/delete",
                       json={"params": {}}, headers=_headers("ren"))
    assert r.status_code == 200, r.text
    async with TestSessionFactory() as db:
        row = await db.get(CaseInstanceModel, cased["case"])
        assert row is not None, "a soft delete must not remove the row"
        assert row.is_deleted is True
        assert row.deleted_at is not None and row.deleted_by


async def test_a_deleted_case_is_a_404_not_a_403(cased, api):
    """403 would confirm the case exists to somebody who may not see it, and a
    200 with a `deleted` flag is a case the platform is still showing."""
    await api.post(f"/api/v1/cases/{cased['case']}/actions/delete",
                   json={"params": {}}, headers=_headers("ren"))
    r = await api.get(f"/api/v1/cases/{cased['case']}", headers=_headers("ren"))
    assert r.status_code == 404


async def test_a_deleted_case_leaves_the_list_and_the_count(cased, api):
    before = (await api.get("/api/v1/cases", headers=_headers("ren"))).json()
    await api.post(f"/api/v1/cases/{cased['case']}/actions/delete",
                   json={"params": {}}, headers=_headers("ren"))
    after = (await api.get("/api/v1/cases", headers=_headers("ren"))).json()

    ids = [c["id"] for c in after["items"]]
    assert str(cased["case"]) not in ids
    # The COUNT too — a total the caller cannot page to makes pagination lie
    # and tells them how many hidden cases there are.
    assert after["total"] == before["total"] - 1


async def test_nothing_but_restore_may_act_on_a_deleted_case(cased, api):
    await api.post(f"/api/v1/cases/{cased['case']}/actions/delete",
                   json={"params": {}}, headers=_headers("ren"))
    r = await api.post(f"/api/v1/cases/{cased['case']}/actions/add_note",
                       json={"params": {"body": "hi"}}, headers=_headers("ren"))
    assert r.status_code in (404, 409)


async def test_restore_brings_it_back(cased, api):
    await api.post(f"/api/v1/cases/{cased['case']}/actions/delete",
                   json={"params": {}}, headers=_headers("ren"))
    r = await api.post(f"/api/v1/cases/{cased['case']}/actions/restore",
                       json={"params": {}}, headers=_headers("ren"))
    assert r.status_code == 200, r.text
    got = await api.get(f"/api/v1/cases/{cased['case']}", headers=_headers("ren"))
    assert got.status_code == 200


async def test_deleting_twice_answers_404_because_it_is_already_invisible(cased, api):
    """The invisibility rule has exactly ONE exception, and it is `restore`.

    A second `delete` is a 404 — not a cheerful 200 — because the case it names
    cannot be seen, and that is the same answer every other route gives for a
    case you cannot see. In a bulk run those come back per case as
    `VEL-CASE-NOTFOUND-404`, reported and not fatal, which is the honest report:
    the sweep did not delete them, because they were already gone.
    """
    first = await api.post(f"/api/v1/cases/{cased['case']}/actions/delete",
                           json={"params": {}}, headers=_headers("ren"))
    assert first.status_code == 200, first.text
    second = await api.post(f"/api/v1/cases/{cased['case']}/actions/delete",
                            json={"params": {}}, headers=_headers("ren"))
    assert second.status_code == 404


async def test_restoring_a_case_that_was_never_deleted_is_a_no_op(cased, api):
    """The other side of the same branch: `restore` may see any case, so it is
    the one that has to answer for "already in the requested state"."""
    r = await api.post(f"/api/v1/cases/{cased['case']}/actions/restore",
                       json={"params": {}}, headers=_headers("ren"))
    assert r.status_code == 200, r.text
    assert r.json()["detail"]["changed"] is False


async def test_delete_is_marked_destructive(cased):
    """So a bulk run never applies it without an explicit confirmation."""
    assert registry.ACTIONS["delete"].destructive is True
    assert registry.ACTIONS["archive"].destructive is False


async def test_bulk_delete_needs_confirmation(cased, api):
    r = await api.post("/api/v1/cases/bulk", json={
        "action": "delete", "case_ids": [str(cased["case"]), str(uuid.uuid4())],
        "params": {}}, headers=_headers("ren"))
    assert r.status_code == 400
    assert "VEL-CASE-BULK-DESTRUCTIVE-400" in str(r.json())


# ── split ────────────────────────────────────────────────────────────────────

async def test_split_creates_a_linked_child(cased, api):
    r = await api.post(f"/api/v1/cases/{cased['case']}/actions/split",
                       json={"params": {"case_type_id": str(cased["case_type"])}},
                       headers=_headers("ren"))
    assert r.status_code == 200, r.text
    child_id = uuid.UUID(r.json()["detail"]["child_case_id"])

    async with TestSessionFactory() as db:
        child = await db.get(CaseInstanceModel, child_id)
        assert child.parent_case_id == cased["case"]
        # The child inherits the tenant — a split that quietly produced a case
        # in another tenant would cross the boundary without anybody calling it
        # a transfer.
        assert child.tenant_id == cased["tenant"].id
        rel = (await db.execute(select(CaseRelationshipModel).where(
            CaseRelationshipModel.source_case_id == cased["case"],
            CaseRelationshipModel.target_case_id == child_id,
        ))).scalars().first()
        assert rel is not None and rel.relationship_type == "child"
        # So the parent learns when the child finishes.
        assert rel.propagate_status is True


async def test_split_refuses_another_tenants_case_type(cased, api):
    async with TestSessionFactory() as db:
        other = TenantModel(id=uuid.uuid4(), slug="remother", name="Other")
        db.add(other)
        await db.flush()
        foreign = CaseTypeModel(
            id=uuid.uuid4(), name="Foreign CT", version="1.0.0",
            lifecycle_process_id=LIFECYCLE_PROCESS_ID, tenant_id=other.id,
            definition_json={"stages": []})
        db.add(foreign)
        await db.commit()
        fid = foreign.id

    r = await api.post(f"/api/v1/cases/{cased['case']}/actions/split",
                       json={"params": {"case_type_id": str(fid)}},
                       headers=_headers("ren"))
    assert r.status_code == 400
    assert "another tenant" in str(r.json())


async def test_split_needs_a_case_type(cased, api):
    r = await api.post(f"/api/v1/cases/{cased['case']}/actions/split",
                       json={"params": {}}, headers=_headers("ren"))
    assert r.status_code == 400
    assert "VEL-CASE-ACTION-PARAM-400" in str(r.json())


# ── transfer ─────────────────────────────────────────────────────────────────

async def test_transfer_to_a_tenant_is_refused_by_name(cased, api):
    """Legible, not a generic 'unknown target' — otherwise somebody reads the
    refusal as "not implemented yet" and tries again next release."""
    r = await api.post(f"/api/v1/cases/{cased['case']}/actions/transfer",
                       json={"params": {"target_type": "tenant",
                                        "target_id": str(uuid.uuid4())}},
                       headers=_headers("ren"))
    assert r.status_code == 400
    body = str(r.json())
    assert "cannot be transferred to another tenant" in body
    assert "tenant-transfers" in body


async def test_transfer_to_an_unknown_target_is_refused(cased, api):
    r = await api.post(f"/api/v1/cases/{cased['case']}/actions/transfer",
                       json={"params": {"target_type": "planet",
                                        "target_id": "mars"}},
                       headers=_headers("ren"))
    assert r.status_code == 400
    assert "not a transfer target" in str(r.json())


async def test_transfer_to_an_access_group_writes_a_group_tuple(cased, api):
    async with TestSessionFactory() as db:
        group = AccessGroupModel(
            id=uuid.uuid4(), name="Complaints", is_active=True,
            portal_id=uuid.uuid4(),
            tenant_id=cased["tenant"].slug, allowed_case_type_ids=[])
        db.add(group)
        await db.commit()
        gid = group.id

    r = await api.post(f"/api/v1/cases/{cased['case']}/actions/transfer",
                       json={"params": {"target_type": "access_group",
                                        "target_id": str(gid)}},
                       headers=_headers("ren"))
    assert r.status_code == 200, r.text

    async with TestSessionFactory() as db:
        tup = (await db.execute(select(HxGuardTupleModel).where(
            HxGuardTupleModel.object_type == "case",
            HxGuardTupleModel.object_id == cased["case"],
            HxGuardTupleModel.subject_type == "access_group",
        ))).scalars().first()
        assert tup is not None and str(tup.subject_id) == str(gid)
        assert tup.relation == "editor"


async def test_transfer_refuses_another_tenants_access_group(cased, api):
    """The back door for the thing `target_type: tenant` is refused at the
    front for."""
    async with TestSessionFactory() as db:
        other = TenantModel(id=uuid.uuid4(), slug="remgrp", name="Other")
        db.add(other)
        await db.flush()
        group = AccessGroupModel(
            id=uuid.uuid4(), name="Theirs", is_active=True,
            portal_id=uuid.uuid4(),
            tenant_id=other.slug, allowed_case_type_ids=[])
        db.add(group)
        await db.commit()
        gid = group.id

    r = await api.post(f"/api/v1/cases/{cased['case']}/actions/transfer",
                       json={"params": {"target_type": "access_group",
                                        "target_id": str(gid)}},
                       headers=_headers("ren"))
    assert r.status_code == 400
    assert "another tenant" in str(r.json())


async def test_transfer_refuses_an_inactive_group(cased, api):
    async with TestSessionFactory() as db:
        group = AccessGroupModel(
            id=uuid.uuid4(), name="Retired", is_active=False,
            portal_id=uuid.uuid4(),
            tenant_id=cased["tenant"].slug, allowed_case_type_ids=[])
        db.add(group)
        await db.commit()
        gid = group.id
    r = await api.post(f"/api/v1/cases/{cased['case']}/actions/transfer",
                       json={"params": {"target_type": "access_group",
                                        "target_id": str(gid)}},
                       headers=_headers("ren"))
    assert r.status_code == 400


# ── the automatic in_progress ────────────────────────────────────────────────

async def test_the_rule_stands_down_when_the_stage_has_an_opinion(cased):
    """Not "the stage wins the race" — the rule does not run at all. A
    configuration a designer can see must not be undone by one nobody can."""
    out = await _auto(cased["case"], {"id": "intake", "on_enter_status": "on_hold"})
    assert out is None
    async with TestSessionFactory() as db:
        case = await db.get(CaseInstanceModel, cased["case"])
        assert case.status == "open"


async def test_the_rule_fires_when_the_stage_says_nothing(cased):
    out = await _auto(cased["case"], {"id": "intake"})
    assert out == "in_progress"
    async with TestSessionFactory() as db:
        case = await db.get(CaseInstanceModel, cased["case"])
        assert case.status == "in_progress"


async def test_the_rule_never_drags_a_case_out_of_a_hold(cased):
    async with TestSessionFactory() as db:
        case = await db.get(CaseInstanceModel, cased["case"])
        case.status = "on_hold"
        await db.commit()
    assert await _auto(cased["case"], {"id": "intake"}) is None
    async with TestSessionFactory() as db:
        case = await db.get(CaseInstanceModel, cased["case"])
        assert case.status == "on_hold"


async def test_the_rule_never_reopens_a_finished_case(cased):
    async with TestSessionFactory() as db:
        case = await db.get(CaseInstanceModel, cased["case"])
        case.status = "closed"
        await db.commit()
    assert await _auto(cased["case"], {"id": "intake"}) is None


async def _auto(case_id, stage):
    async with TestSessionFactory() as db:
        out = await stages.apply_auto_in_progress(
            db, case_id=case_id, stage=stage, actor="ren")
        await db.commit()
        return out


# ── the gate that would have broken silently ─────────────────────────────────

async def test_in_progress_is_workable_so_auto_advance_keeps_running(cased):
    """The defect this replaced: auto-advance was gated on a literal
    `["open", "new", "reopened"]`, so the moment `in_progress` became automatic
    the SECOND step completed on any case would have stopped advancing."""
    async with TestSessionFactory() as db:
        case = await db.get(CaseInstanceModel, cased["case"])
        for status in ("new", "open", "reopened", "in_progress", "escalated"):
            case.status = status
            assert await statuses.is_workable_for_case(db, case) is True, status


async def test_a_held_or_finished_case_is_not_workable(cased):
    """Advancing through a hold would make the hold a label, not a control."""
    async with TestSessionFactory() as db:
        case = await db.get(CaseInstanceModel, cased["case"])
        for status in ("on_hold", "pending_external", "closed", "cancelled",
                       "archived"):
            case.status = status
            assert await statuses.is_workable_for_case(db, case) is False, status


async def test_a_tenants_own_active_status_is_workable(cased):
    """The other half of the literal's problem: a tenant's own status was not
    in it either."""
    async with TestSessionFactory() as db:
        db.add(CaseStatusModel(
            id=uuid.uuid4(), tenant_id=cased["tenant"].id, case_type_id=None,
            scope_key=statuses.scope_key(cased["tenant"].id, None),
            name="with_underwriter", label="With Underwriter", category="open",
            is_terminal=False, is_default=False, allowed_from=[], sort_order=50,
            enabled=True))
        case = await db.get(CaseInstanceModel, cased["case"])
        case.status = "with_underwriter"
        await db.commit()
    async with TestSessionFactory() as db:
        case = await db.get(CaseInstanceModel, cased["case"])
        assert await statuses.is_workable_for_case(db, case) is True


# ── the group tuple has to actually GRANT something ──────────────────────────
#
# Writing a row is not the feature. Until 2026-09-08 the ReBAC backend read
# `subject_type="user"` tuples and nothing else, so a tuple against a group
# would have been a row that decided nothing — the feature would have "worked"
# in every test that checked the write and in none that checked the access.


async def test_a_group_member_gains_access_through_the_tuple(cased):
    from velaris_shared.db.models import OperatorAccessGroupModel
    from velaris_shared.hxguard import tuples as _tuples
    from velaris_shared.hxguard.service import RebacBackend

    member = str(uuid.uuid4())
    async with TestSessionFactory() as db:
        group = AccessGroupModel(
            id=uuid.uuid4(), name="Grantees", is_active=True,
            portal_id=uuid.uuid4(), tenant_id=cased["tenant"].slug,
            allowed_case_type_ids=[])
        db.add(group)
        await db.flush()
        db.add(OperatorAccessGroupModel(
            id=uuid.uuid4(), operator_id=member, access_group_id=group.id))
        await _tuples.write_tuple(
            db, object_type="case", object_id=cased["case"],
            relation="editor", subject_type="access_group",
            subject_id=str(group.id), created_by="ren")
        await db.commit()
        gid = group.id

    class _S:
        id = member
        kind = "user"
        roles: list = []

    async with TestSessionFactory() as db:
        d = await RebacBackend().evaluate(db, _S(), "case.update", {"case_id": str(cased["case"])})
        assert d.allow, d.reason
        assert "access-group" in d.reason

    # And a stranger — same case, no membership — still gets nothing.
    class _T:
        id = str(uuid.uuid4())
        kind = "user"
        roles: list = []

    async with TestSessionFactory() as db:
        d2 = await RebacBackend().evaluate(db, _T(), "case.update", {"case_id": str(cased["case"])})
        assert not d2.allow


async def test_deactivating_the_group_withdraws_the_access(cased):
    """Membership is resolved at DECISION time, not expanded at write time —
    so deactivating a group withdraws what it was granting with nothing else
    touched, and no stale per-user rows outlive it."""
    from velaris_shared.db.models import OperatorAccessGroupModel
    from velaris_shared.hxguard import tuples as _tuples
    from velaris_shared.hxguard.service import RebacBackend

    member = str(uuid.uuid4())
    async with TestSessionFactory() as db:
        group = AccessGroupModel(
            id=uuid.uuid4(), name="Temporary", is_active=True,
            portal_id=uuid.uuid4(), tenant_id=cased["tenant"].slug,
            allowed_case_type_ids=[])
        db.add(group)
        await db.flush()
        db.add(OperatorAccessGroupModel(
            id=uuid.uuid4(), operator_id=member, access_group_id=group.id))
        await _tuples.write_tuple(
            db, object_type="case", object_id=cased["case"],
            relation="editor", subject_type="access_group",
            subject_id=str(group.id), created_by="ren")
        await db.commit()
        gid = group.id

    class _S:
        id = member
        kind = "user"
        roles: list = []

    async with TestSessionFactory() as db:
        assert (await RebacBackend().evaluate(db, _S(), "case.update", {"case_id": str(cased["case"])})).allow

    async with TestSessionFactory() as db:
        g = await db.get(AccessGroupModel, gid)
        g.is_active = False
        await db.commit()

    async with TestSessionFactory() as db:
        assert not (await RebacBackend().evaluate(db, _S(), "case.update", {"case_id": str(cased["case"])})).allow


async def test_a_group_tuple_does_not_leak_to_another_case(cased):
    """The tuple is per case. A group holding one case must not thereby hold
    every case its members can see."""
    from velaris_shared.db.models import OperatorAccessGroupModel
    from velaris_shared.hxguard import tuples as _tuples
    from velaris_shared.hxguard.service import RebacBackend

    member = str(uuid.uuid4())
    async with TestSessionFactory() as db:
        other_case = CaseInstanceModel(
            id=uuid.uuid4(), case_number="REM-2",
            case_type_id=cased["case_type"], case_type_version="1.0.0",
            status="open", priority="low", tenant_id=cased["tenant"].id,
            created_by="someone-else", data={})
        db.add(other_case)
        group = AccessGroupModel(
            id=uuid.uuid4(), name="Narrow", is_active=True,
            portal_id=uuid.uuid4(), tenant_id=cased["tenant"].slug,
            allowed_case_type_ids=[])
        db.add(group)
        await db.flush()
        db.add(OperatorAccessGroupModel(
            id=uuid.uuid4(), operator_id=member, access_group_id=group.id))
        await _tuples.write_tuple(
            db, object_type="case", object_id=cased["case"],
            relation="editor", subject_type="access_group",
            subject_id=str(group.id), created_by="ren")
        await db.commit()
        other_id = other_case.id

    class _S:
        id = member
        kind = "user"
        roles: list = []

    async with TestSessionFactory() as db:
        assert not (await RebacBackend().evaluate(db, _S(), "case.update", {"case_id": str(other_id)})).allow


async def test_a_group_member_also_SEES_the_case_in_the_list(cased):
    """Readable by id and absent from the list is the worst of both.

    `RebacBackend.evaluate` and `search_case_instances` are two copies of the
    same rule — the repository's own comment says it "mirrors
    RebacBackend.evaluate" — and teaching one about group tuples without the
    other would tell the operator the case does not exist right up until
    somebody sent them a link to it.
    """
    from velaris_shared.db import repository as repo
    from velaris_shared.db.models import OperatorAccessGroupModel
    from velaris_shared.hxguard import tuples as _tuples

    member = str(uuid.uuid4())
    async with TestSessionFactory() as db:
        group = AccessGroupModel(
            id=uuid.uuid4(), name="Listers", is_active=True,
            portal_id=uuid.uuid4(), tenant_id=cased["tenant"].slug,
            allowed_case_type_ids=[])
        db.add(group)
        await db.flush()
        db.add(OperatorAccessGroupModel(
            id=uuid.uuid4(), operator_id=member, access_group_id=group.id))
        await _tuples.write_tuple(
            db, object_type="case", object_id=cased["case"],
            relation="editor", subject_type="access_group",
            subject_id=str(group.id), created_by="ren")
        await db.commit()
        gid = group.id

    async with TestSessionFactory() as db:
        items, total = await repo.search_case_instances(
            db, filters={}, accessible_to_user=(member, []))
        assert [str(c.id) for c in items] == [str(cased["case"])], \
            "a group member cannot see the case they were given"
        assert total == 1

    # And a stranger sees nothing, so the clause did not simply open the list.
    async with TestSessionFactory() as db:
        items, total = await repo.search_case_instances(
            db, filters={}, accessible_to_user=(str(uuid.uuid4()), []))
        assert items == [] and total == 0


async def test_deactivating_the_group_removes_it_from_the_list_too(cased):
    from velaris_shared.db import repository as repo
    from velaris_shared.db.models import OperatorAccessGroupModel
    from velaris_shared.hxguard import tuples as _tuples

    member = str(uuid.uuid4())
    async with TestSessionFactory() as db:
        group = AccessGroupModel(
            id=uuid.uuid4(), name="Fading", is_active=True,
            portal_id=uuid.uuid4(), tenant_id=cased["tenant"].slug,
            allowed_case_type_ids=[])
        db.add(group)
        await db.flush()
        db.add(OperatorAccessGroupModel(
            id=uuid.uuid4(), operator_id=member, access_group_id=group.id))
        await _tuples.write_tuple(
            db, object_type="case", object_id=cased["case"],
            relation="editor", subject_type="access_group",
            subject_id=str(group.id), created_by="ren")
        await db.commit()
        gid = group.id

    async with TestSessionFactory() as db:
        g = await db.get(AccessGroupModel, gid)
        g.is_active = False
        await db.commit()

    async with TestSessionFactory() as db:
        items, total = await repo.search_case_instances(
            db, filters={}, accessible_to_user=(member, []))
        assert items == [] and total == 0


# ── the seal has to hold on the WHOLE surface, not just the dispatcher ───────
#
# Written after a live probe on 2026-09-08 archived a case and then moved it to
# another stage (200), created an assignment on it (201) and shared it with
# another user (201). Every test passed and the feature looked finished: the
# rule lived inside the action dispatcher, and those three routes do not go
# through it. "Wrapped up without any further changes" was true of eleven
# actions and false of every other write on the case surface.
#
# This walks the routes rather than the rule, because walking the rule is what
# missed it.


@pytest_asyncio.fixture
async def sealed(cased, api):
    await api.post(f"/api/v1/cases/{cased['case']}/actions/archive",
                   json={"params": {}}, headers=_headers("ren"))
    return cased


async def test_an_archived_case_refuses_a_stage_move(sealed, api):
    r = await api.post(f"/api/v1/cases/{sealed['case']}/stage",
                       json={"target_stage_id": "intake"},
                       headers=_headers("ren"))
    assert r.status_code == 409, r.text
    assert "VEL-CASE-ACTION-SEALED-409" in str(r.json())


async def test_an_archived_case_refuses_an_assignment(sealed, api):
    r = await api.post(f"/api/v1/cases/{sealed['case']}/assignments",
                       json={"step_id": "s1", "assignee_type": "queue",
                             "assignee_id": "q"},
                       headers=_headers("ren"))
    assert r.status_code == 409, r.text


async def test_an_archived_case_refuses_a_share(sealed, api):
    """Sharing changes who may reach the case, which is exactly the kind of
    further change archiving exists to stop."""
    r = await api.post(f"/api/v1/cases/{sealed['case']}/shares",
                       json={"user_id": "ren", "relation": "viewer"},
                       headers=_headers("ren"))
    assert r.status_code == 409, r.text


async def test_an_archived_case_refuses_a_data_write(sealed, api):
    # `PATCH /cases/{id}` — the data route is the case route, which is why a
    # probe aimed at `/cases/{id}/data` answered 404 and looked, briefly, like
    # a seal that was holding.
    r = await api.patch(f"/api/v1/cases/{sealed['case']}",
                        json={"data": {"snuck": "in"}, "updated_by": "ren"},
                        headers=_headers("ren"))
    assert r.status_code == 409, r.text


async def test_an_archived_case_refuses_a_child(sealed, api):
    r = await api.post(f"/api/v1/cases/{sealed['case']}/children",
                       json={"case_type_id": str(sealed["case_type"]),
                             "data": {}},
                       headers=_headers("ren"))
    assert r.status_code == 409, r.text


async def test_an_archived_case_refuses_a_relationship(sealed, api):
    r = await api.post(f"/api/v1/cases/{sealed['case']}/relationships",
                       json={"target_case_id": str(sealed["case"]),
                             "relationship_type": "related"},
                       headers=_headers("ren"))
    assert r.status_code == 409, r.text


async def test_reopening_puts_the_whole_surface_back(sealed, api):
    """The seal is not a one-way door, and that has to be true of the routes
    too — not only of the action that reverses it."""
    r = await api.post(f"/api/v1/cases/{sealed['case']}/actions/reopen",
                       json={"params": {}}, headers=_headers("ren"))
    assert r.status_code == 200, r.text
    r2 = await api.post(f"/api/v1/cases/{sealed['case']}/assignments",
                        json={"step_id": "s1", "assignee_type": "queue",
                              "assignee_id": "q"},
                        headers=_headers("ren"))
    assert r2.status_code in (200, 201), r2.text


async def test_a_deleted_case_refuses_the_same_routes(cased, api):
    """404 here rather than 409, because a deleted case is invisible before it
    is inert — and that is the stronger answer, not a weaker one."""
    await api.post(f"/api/v1/cases/{cased['case']}/actions/delete",
                   json={"params": {}}, headers=_headers("ren"))
    for method, path, body in (
        ("post", f"/api/v1/cases/{cased['case']}/stage", {"target_stage_id": "intake"}),
        ("post", f"/api/v1/cases/{cased['case']}/assignments",
         {"step_id": "s1", "assignee_type": "queue", "assignee_id": "q"}),
        ("post", f"/api/v1/cases/{cased['case']}/shares",
         {"user_id": "ren", "relation": "viewer"}),
    ):
        r = await getattr(api, method)(path, json=body, headers=_headers("ren"))
        assert r.status_code in (404, 409), f"{path} -> {r.status_code}"


async def test_group_access_never_confers_the_power_to_RE_SHARE(cased):
    """Found by audit on 2026-09-08, before shipping, and it is the sharper
    half of the transfer feature.

    `case.share` requires the `editor` relation — and `transfer` writes exactly
    `editor` for a group by default. So a group tuple that answered `case.share`
    would have given every member of the receiving team the power to re-share
    the case with anyone on the platform: the transfer would have granted more
    than the operator performing it meant to give, and more than a direct
    `POST /cases/{id}/shares` to those same people would have given.

    The rule is not new — the access-group case-type parity check beside it has
    said "never for case.share" since the HxGuard cutover, for the same reason.
    This pins that the group-tuple path obeys it too.
    """
    from velaris_shared.db.models import OperatorAccessGroupModel
    from velaris_shared.hxguard import tuples as _tuples
    from velaris_shared.hxguard.service import RebacBackend

    member = str(uuid.uuid4())
    async with TestSessionFactory() as db:
        group = AccessGroupModel(
            id=uuid.uuid4(), name="Sharers", is_active=True,
            portal_id=uuid.uuid4(), tenant_id=cased["tenant"].slug,
            allowed_case_type_ids=[])
        db.add(group)
        await db.flush()
        db.add(OperatorAccessGroupModel(
            id=uuid.uuid4(), operator_id=member, access_group_id=group.id))
        await _tuples.write_tuple(
            db, object_type="case", object_id=cased["case"],
            relation="editor", subject_type="access_group",
            subject_id=str(group.id), created_by="ren")
        await db.commit()

    class _S:
        id = member
        kind = "user"
        roles: list = []

    async with TestSessionFactory() as db:
        # They may work the case…
        assert (await RebacBackend().evaluate(
            db, _S(), "case.update", {"case_id": str(cased["case"])})).allow
        assert (await RebacBackend().evaluate(
            db, _S(), "case.read", {"case_id": str(cased["case"])})).allow
        # …and may NOT hand it on.
        share = await RebacBackend().evaluate(
            db, _S(), "case.share", {"case_id": str(cased["case"])})
        assert not share.allow, share.reason


async def test_a_deleted_case_is_hidden_from_the_CUSTOMER_too(cased, api):
    """The gap this closes was the wrong way round.

    "Hidden from every list and read" was true of the staff surface and false
    of the customer portal — the least-trusted surface on the platform — where
    the tracking-token lookup and the account-linked lookup both read the case
    row directly. A customer could still see the status, timeline and documents
    of a case staff had deleted.
    """
    from velaris_shared.db.models import TenantModel

    async with TestSessionFactory() as db:
        case = await db.get(CaseInstanceModel, cased["case"])
        case.portal_tracking_token = uuid.uuid4()
        await db.commit()
        token = str(case.portal_tracking_token)
        slug = (await db.get(TenantModel, cased["tenant"].id)).slug

    ok = await api.get(f"/api/v1/portal/{slug}/track/{token}")
    assert ok.status_code in (200, 404)          # route shape varies by build
    seen_before = ok.status_code == 200

    await api.post(f"/api/v1/cases/{cased['case']}/actions/delete",
                   json={"params": {}}, headers=_headers("ren"))

    after = await api.get(f"/api/v1/portal/{slug}/track/{token}")
    assert after.status_code == 404, (
        "a deleted case is still visible to the customer" if seen_before
        else after.text)

    # And the write path a customer has: uploading to the tracked case.
    up = await api.post(
        f"/api/v1/portal/{slug}/track/{token}/documents",
        files={"file": ("x.txt", b"hello", "text/plain")})
    assert up.status_code == 404
