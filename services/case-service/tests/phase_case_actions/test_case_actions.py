"""Case actions and the configured status set (migration 198).

Design: docs/Future/Done/CASE-ACTIONS-STATUS-DESIGN.md

What these tests are actually pinning, beyond "the happy path works":

* **The free-text status hole is closed.** `POST /cases/{id}/status` accepted any
  string a caller sent — that is how six undesigned statuses came to exist. The
  dispatcher validates against the resolved set, and the three refusals are kept
  distinct because "that did not work" is not something an operator can act on.
* **The vocabulary is closed.** An unknown action is a 404, never a guess at what
  the word might mean.
* **Bulk is the same code path**, so a per-case failure is reported per case and
  a batch is never permitted on one check.
* **Compatibility.** A status set that cannot be read falls back to the pre-198
  behaviour, and an undefined status a case is already sitting on can still be
  moved out of.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from velaris_shared.db.models import (
    CaseActionConfigModel,
    CaseInstanceModel,
    CaseStatusModel,
    CaseTypeModel,
    TenantModel,
    UserDirectoryModel,
)

from case_service.case_actions import registry, statuses
from case_service.main import app
from case_service.tenancy import identity as tenancy
from tests.conftest import LIFECYCLE_PROCESS_ID, TestSessionFactory

pytestmark = pytest.mark.asyncio


#: The platform set is seeded by `conftest._seed_case_statuses`, exactly as
#: migration 198 seeds a real install — so these tests exercise the same thirteen
#: statuses an operator gets, not a fixture's idea of them.
#:
#: Thirteen, not eleven: `pending_external` and `pending_subcase` are live
#: platform statuses that no case on any box happens to be sitting on, so they
#: never appeared in a `SELECT DISTINCT status`. `core/case_lifecycle.py` pauses
#: SLAs on both and `relationship_manager.py` moves a parent off
#: `pending_subcase` when its child resolves.
_SEED_COUNT = 13
_TERMINAL = {"resolved", "closed", "cancelled", "archived"}


def _token(username: str, roles=("admin",)) -> str:
    from velaris_shared.auth.jwt_handler import create_dev_token
    from velaris_shared.config import get_settings

    s = get_settings()
    return create_dev_token(
        user_id=str(uuid.uuid4()), username=username, roles=list(roles),
        secret=s.auth_secret, private_key=s.auth_rsa_private_key or "")


def _headers(username: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(username)}"}


@pytest_asyncio.fixture
async def seeded():
    """The platform status set, a tenant, and three cases to act on."""
    tenancy.clear_cache()
    async with TestSessionFactory() as db:
        acme = TenantModel(id=uuid.uuid4(), slug="acme", name="Acme")
        db.add(acme)
        db.add(UserDirectoryModel(id=uuid.uuid4(), user_id="ann", tenant_id="acme"))
        ct = CaseTypeModel(
            id=uuid.uuid4(), name="Action CT", version="1.0.0",
            lifecycle_process_id=LIFECYCLE_PROCESS_ID, definition_json={"stages": []})
        db.add(ct)
        await db.flush()

        cases = []
        for n in range(3):
            case = CaseInstanceModel(
                id=uuid.uuid4(), case_number=f"ACME-{n}", case_type_id=ct.id,
                case_type_version="1.0.0", status="new", priority="medium",
                tenant_id=acme.id, created_by="ann", data={})
            db.add(case)
            cases.append(case)
        # A case sitting on a status nobody defined — the compatibility case.
        stray = CaseInstanceModel(
            id=uuid.uuid4(), case_number="ACME-STRAY", case_type_id=ct.id,
            case_type_version="1.0.0", status="bespoke_legacy", priority="low",
            tenant_id=acme.id, created_by="ann", data={})
        db.add(stray)
        await db.commit()
        yield {"tenant": acme, "case_type": ct.id,
               "cases": [c.id for c in cases], "stray": stray.id}
    tenancy.clear_cache()


@pytest_asyncio.fixture
async def api():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ── the registry is closed ───────────────────────────────────────────────────

async def test_every_registered_action_has_a_handler():
    """A registered action with no handler would 500 at call time rather than
    404 — the one failure a closed vocabulary exists to prevent. The dispatcher
    asserts this at import; this states it as a test so the reason survives."""
    from case_service.case_actions import dispatcher

    assert set(registry.ACTIONS) == set(dispatcher._HANDLERS)


async def test_every_action_declares_only_known_effects():
    for action in registry.ACTIONS.values():
        assert set(action.effects) <= registry.EFFECTS, action.name


async def test_every_action_privilege_is_a_real_privilege():
    """An unknown privilege denies everything, so it would silently disable the
    action for everybody rather than failing loudly."""
    from velaris_shared.hxguard.actions import ACTIONS as GUARD

    for action in registry.ACTIONS.values():
        assert action.privilege in GUARD, action.name


async def test_unknown_action_is_refused_not_guessed(seeded, api):
    """The HxArbiter lesson: behaviour is never inferred from a name."""
    case_id = seeded["cases"][0]
    r = await api.post(f"/api/v1/cases/{case_id}/actions/obliterate",
                       json={"params": {}}, headers=_headers("ann"))
    assert r.status_code == 404
    assert "VEL-CASE-ACTION-UNKNOWN-404" in str(r.json())


# ── statuses are data ────────────────────────────────────────────────────────

async def test_resolved_set_is_the_seeded_eleven(session, seeded):
    resolved = await statuses.resolve_set(session)
    assert len(resolved) == _SEED_COUNT
    assert {s.name for s in resolved.values() if s.is_terminal} == _TERMINAL


async def test_terminal_comes_from_data_not_a_tuple(session, seeded):
    """The whole point of 198: `archived` is terminal, and no tuple in any
    module names it."""
    assert await statuses.is_terminal(session, "archived") is True
    assert await statuses.is_terminal(session, "in_progress") is False


async def test_unknown_status_is_not_terminal(session, seeded):
    """A case on a status nobody defined is still an OPEN case. Treating it as
    finished would silently stop its workflow."""
    assert await statuses.is_terminal(session, "bespoke_legacy") is False


async def test_tenant_layer_merges_with_platform_rather_than_replacing(session, seeded):
    """A tenant adding one status must still get the platform's eleven —
    otherwise "add one" means "restate all", and the twelfth goes missing the
    first time the platform set grows."""
    tenant = seeded["tenant"]
    async with TestSessionFactory() as db:
        db.add(CaseStatusModel(
            id=uuid.uuid4(), tenant_id=tenant.id, case_type_id=None,
            scope_key=statuses.scope_key(tenant.id, None),
            name="awaiting_survey", label="Awaiting Survey", category="waiting",
            is_terminal=False, is_default=False, allowed_from=[],
            sort_order=45, enabled=True))
        await db.commit()

    resolved = await statuses.resolve_set(session, tenant_id=tenant.id)
    assert len(resolved) == _SEED_COUNT + 1
    assert "awaiting_survey" in resolved
    assert "archived" in resolved


async def test_more_specific_scope_overrides_by_name(session, seeded):
    tenant = seeded["tenant"]
    async with TestSessionFactory() as db:
        db.add(CaseStatusModel(
            id=uuid.uuid4(), tenant_id=tenant.id, case_type_id=None,
            scope_key=statuses.scope_key(tenant.id, None),
            name="archived", label="Filed Away", category="terminal",
            is_terminal=False, is_default=False, allowed_from=[],
            sort_order=110, enabled=True))
        await db.commit()

    resolved = await statuses.resolve_set(session, tenant_id=tenant.id)
    assert resolved["archived"].label == "Filed Away"
    # And the tenant's own opinion about terminality wins.
    assert resolved["archived"].is_terminal is False


# ── the three refusals, kept distinct ────────────────────────────────────────

async def test_status_not_in_the_set_is_refused(session, seeded):
    """This is the defect 198 exists to close: before it, any string was
    accepted, which is how six undesigned statuses came to exist."""
    with pytest.raises(statuses.StatusError) as exc:
        await statuses.validate_transition(
            session, current="new", target="frobnicated")
    assert exc.value.code == "VEL-CASE-STATUS-UNKNOWN-400"


async def test_disabled_status_is_refused_with_its_own_code(session, seeded):
    async with TestSessionFactory() as db:
        row = (await db.execute(
            __import__("sqlalchemy").select(CaseStatusModel).where(
                CaseStatusModel.name == "archived")
        )).scalars().first()
        row.enabled = False
        await db.commit()

    with pytest.raises(statuses.StatusError) as exc:
        await statuses.validate_transition(session, current="new", target="archived")
    assert exc.value.code == "VEL-CASE-STATUS-DISABLED-409"


async def test_transition_graph_is_refused_with_its_own_code(session, seeded):
    async with TestSessionFactory() as db:
        row = (await db.execute(
            __import__("sqlalchemy").select(CaseStatusModel).where(
                CaseStatusModel.name == "closed")
        )).scalars().first()
        row.allowed_from = ["resolved"]
        await db.commit()

    with pytest.raises(statuses.StatusError) as exc:
        await statuses.validate_transition(session, current="new", target="closed")
    assert exc.value.code == "VEL-CASE-STATUS-TRANSITION-409"
    # The error must name what it WOULD accept, or an operator cannot act on it.
    assert "resolved" in str(exc.value)


async def test_empty_allowed_from_permits_any_transition(session, seeded):
    """Every seeded row ships with `allowed_from = []`, which is what makes
    migration 198 break no transition that works today."""
    for target in ("closed", "cancelled", "in_progress", "archived"):
        await statuses.validate_transition(session, current="new", target=target)


async def test_a_case_can_leave_an_undefined_status(session, seeded):
    """Refusing to let a case leave a status nobody defined would strand it
    there permanently — the opposite of the fix."""
    await statuses.validate_transition(
        session, current="bespoke_legacy", target="closed")


# ── single action, end to end ────────────────────────────────────────────────

async def test_close_moves_the_case_and_reports_terminality(seeded, api):
    case_id = seeded["cases"][0]
    r = await api.post(f"/api/v1/cases/{case_id}/actions/close",
                       json={"params": {}}, headers=_headers("ann"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["detail"]["from"] == "new"
    assert body["detail"]["to"] == "closed"
    assert body["detail"]["terminal"] is True


async def test_change_status_validates_the_target(seeded, api):
    case_id = seeded["cases"][0]
    r = await api.post(f"/api/v1/cases/{case_id}/actions/change_status",
                       json={"params": {"status": "not_a_status"}},
                       headers=_headers("ann"))
    # 400, not 409: the caller named a status that does not exist, which is a
    # malformed request. A 409 is for a transition the set refuses — a real
    # conflict with the case's current state. The HTTP status is derived from
    # the code's own suffix so the two can never disagree.
    assert r.status_code == 400
    assert "VEL-CASE-STATUS-UNKNOWN-400" in str(r.json())


async def test_missing_required_param_is_refused_before_any_case_is_touched(seeded, api):
    case_id = seeded["cases"][0]
    r = await api.post(f"/api/v1/cases/{case_id}/actions/change_priority",
                       json={"params": {}}, headers=_headers("ann"))
    assert r.status_code == 400
    assert "VEL-CASE-ACTION-PARAM-400" in str(r.json())


async def test_add_note_never_reaches_the_customer(seeded, api, session):
    """`portal_visible` is not settable from an action. A sweep that published
    400 internal notes to 400 customers is not recoverable by any means this
    platform has."""
    from sqlalchemy import select
    from velaris_shared.db.models import CaseMessageModel

    case_id = seeded["cases"][0]
    r = await api.post(f"/api/v1/cases/{case_id}/actions/add_note",
                       json={"params": {"body": "internal only",
                                        "portal_visible": True}},
                       headers=_headers("ann"))
    assert r.status_code == 200, r.text

    async with TestSessionFactory() as db:
        msg = (await db.execute(
            select(CaseMessageModel).where(CaseMessageModel.case_id == case_id)
        )).scalars().first()
    assert msg is not None
    assert msg.portal_visible is False


# ── bulk is the same code path ───────────────────────────────────────────────

async def test_bulk_reports_per_case_and_does_not_hide_failure(seeded, api):
    """Partial success is the NORMAL outcome. A 200 that says nothing about the
    cases that refused is how a bulk feature loses trust in its first week."""
    good = seeded["cases"]
    missing = uuid.uuid4()
    r = await api.post("/api/v1/cases/bulk", json={
        "case_ids": [str(c) for c in good] + [str(missing)],
        "action": "change_priority",
        "params": {"priority": "high"},
    }, headers=_headers("ann"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["requested"] == 4
    assert body["succeeded"] == 3
    assert body["failed"] == 1
    bad = [x for x in body["results"] if not x["ok"]]
    assert len(bad) == 1
    assert bad[0]["case_id"] == str(missing)
    assert bad[0]["code"].endswith("-404")


async def test_bulk_deduplicates_ids(seeded, api):
    """A caller repeating an id must not get two audit rows on one case."""
    one = str(seeded["cases"][0])
    r = await api.post("/api/v1/cases/bulk", json={
        "case_ids": [one, one, one], "action": "change_priority",
        "params": {"priority": "low"},
    }, headers=_headers("ann"))
    assert r.json()["requested"] == 1


async def test_bulk_refuses_over_the_ceiling_rather_than_truncating(seeded, api):
    """Acting on the first N of a larger selection and reporting success is the
    worse answer — the posture the scheduled bulk action already takes."""
    from case_service.case_actions.dispatcher import BULK_CEILING

    ids = [str(uuid.uuid4()) for _ in range(BULK_CEILING + 1)]
    r = await api.post("/api/v1/cases/bulk", json={
        "case_ids": ids, "action": "change_priority",
        "params": {"priority": "low"},
    }, headers=_headers("ann"))
    assert r.status_code == 400
    assert "VEL-CASE-BULK-CEILING-400" in str(r.json())


async def test_bulk_refuses_an_empty_selection(seeded, api):
    r = await api.post("/api/v1/cases/bulk", json={
        "case_ids": [], "action": "change_priority", "params": {"priority": "low"},
    }, headers=_headers("ann"))
    assert r.status_code == 400
    assert "VEL-CASE-BULK-EMPTY-400" in str(r.json())


async def test_bulk_validates_params_once_before_touching_anything(seeded, api):
    """A bulk run must not half-apply and then discover id 200 was missing a
    field."""
    r = await api.post("/api/v1/cases/bulk", json={
        "case_ids": [str(c) for c in seeded["cases"]],
        "action": "change_priority", "params": {},
    }, headers=_headers("ann"))
    assert r.status_code == 400
    assert "VEL-CASE-ACTION-PARAM-400" in str(r.json())


# ── the tenant boundary, per case ────────────────────────────────────────────

async def test_bulk_checks_permission_per_case_not_once_for_the_batch(seeded, api):
    """A batch containing one case the caller may not reach must refuse THAT
    case and act on the rest — which is only possible if the check is inside
    the loop."""
    async with TestSessionFactory() as db:
        beta = TenantModel(id=uuid.uuid4(), slug="beta", name="Beta")
        db.add(beta)
        db.add(UserDirectoryModel(id=uuid.uuid4(), user_id="ben", tenant_id="beta"))
        await db.flush()
        theirs = CaseInstanceModel(
            id=uuid.uuid4(), case_number="BETA-1", case_type_id=seeded["case_type"],
            case_type_version="1.0.0", status="new", priority="low",
            tenant_id=beta.id, created_by="ben", data={})
        db.add(theirs)
        await db.commit()
        theirs_id = theirs.id
    tenancy.clear_cache()

    r = await api.post("/api/v1/cases/bulk", json={
        "case_ids": [str(seeded["cases"][0]), str(theirs_id)],
        "action": "change_priority", "params": {"priority": "high"},
    }, headers=_headers("ann"))
    body = r.json()
    assert body["succeeded"] == 1
    refused = [x for x in body["results"] if not x["ok"]]
    assert len(refused) == 1
    assert refused[0]["case_id"] == str(theirs_id)


# ── configuration ────────────────────────────────────────────────────────────

async def test_disabled_action_is_unreachable_through_the_dispatcher(seeded, api):
    """A switch that only hides the button in the UI is decoration."""
    tenant = seeded["tenant"]
    async with TestSessionFactory() as db:
        db.add(CaseActionConfigModel(
            id=uuid.uuid4(), tenant_id=tenant.id, case_type_id=None,
            scope_key=statuses.scope_key(tenant.id, None),
            action="cancel", label=None, enabled=False, sort_order=0,
            privilege=None, allowed_from=[]))
        await db.commit()

    r = await api.get("/api/v1/cases/actions/catalogue", headers=_headers("ann"))
    assert r.status_code == 200, r.text
    names = [a["name"] for a in r.json()["actions"]]
    # Absent, not present and greyed out.
    assert "cancel" not in names
    assert "close" in names


async def test_allowed_from_narrows_when_an_action_is_offered(seeded, api):
    tenant = seeded["tenant"]
    async with TestSessionFactory() as db:
        db.add(CaseActionConfigModel(
            id=uuid.uuid4(), tenant_id=tenant.id, case_type_id=None,
            scope_key=statuses.scope_key(tenant.id, None),
            action="close", label=None, enabled=True, sort_order=0,
            privilege=None, allowed_from=["resolved"]))
        await db.commit()

    case_id = seeded["cases"][0]  # sits on `new`
    r = await api.post(f"/api/v1/cases/{case_id}/actions/close",
                       json={"params": {}}, headers=_headers("ann"))
    assert r.status_code == 409
    assert "VEL-CASE-ACTION-NOTALLOWED-409" in str(r.json())


async def test_config_refuses_an_action_outside_the_registry(seeded, api):
    """Refused at WRITE time. A config row that silently does nothing is worse
    than a refusal, because the operator believes they configured something."""
    r = await api.put("/api/v1/case-config/actions", json={
        "action": "teleport", "enabled": True,
    }, headers=_headers("ann"))
    assert r.status_code == 400
    assert "VEL-CASE-ACTION-UNKNOWN-404" in str(r.json())


async def test_config_refuses_an_unknown_privilege(seeded, api):
    r = await api.put("/api/v1/case-config/actions", json={
        "action": "close", "privilege": "case.actions.invented",
    }, headers=_headers("ann"))
    assert r.status_code == 400
    assert "VEL-CASE-ACTION-PRIV-400" in str(r.json())


async def test_a_status_a_case_sits_on_cannot_be_deleted(seeded, api):
    """Deleting it strands the case with no label and no transition graph.
    `enabled = false` is the verb for 'stop offering this'."""
    async with TestSessionFactory() as db:
        tenant = seeded["tenant"]
        db.add(CaseStatusModel(
            id=uuid.uuid4(), tenant_id=tenant.id, case_type_id=None,
            scope_key=statuses.scope_key(tenant.id, None),
            name="new", label="New (tenant)", category="open",
            is_terminal=False, is_default=True, allowed_from=[],
            sort_order=10, enabled=True))
        await db.commit()

    r = await api.delete("/api/v1/case-config/statuses/new", headers=_headers("ann"))
    assert r.status_code == 409
    assert "VEL-CASE-STATUS-INUSE-409" in str(r.json())


async def test_catalogue_reports_the_status_set_and_the_ceiling(seeded, api):
    r = await api.get("/api/v1/cases/actions/catalogue", headers=_headers("ann"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["statuses"]) == _SEED_COUNT
    assert body["bulk_ceiling"] == 500
    assert {a["name"] for a in body["actions"]} == set(registry.names())


# ── the three defects found by reviewing this code, not by a test failing ────

async def test_a_status_action_writes_exactly_one_audit_row(seeded, api, session):
    """`apply_status_change` writes its own audit row. A dispatcher that wrote a
    second on top would double every status change in the log an auditor reads —
    and `change_status` would have produced two IDENTICAL entries."""
    from sqlalchemy import select
    from velaris_shared.db.models import CaseAuditLogModel

    case_id = seeded["cases"][0]
    r = await api.post(f"/api/v1/cases/{case_id}/actions/close",
                       json={"params": {}}, headers=_headers("ann"))
    assert r.status_code == 200, r.text

    async with TestSessionFactory() as db:
        rows = (await db.execute(
            select(CaseAuditLogModel).where(CaseAuditLogModel.case_id == case_id)
        )).scalars().all()
    actions = [x.action for x in rows]
    # The pre-198 verb, recorded once. Not `close`, and not twice.
    assert actions.count("closed") == 1, actions
    assert "status_changed" not in actions, actions


async def test_priority_action_writes_exactly_one_audit_row(seeded, api):
    from sqlalchemy import select
    from velaris_shared.db.models import CaseAuditLogModel

    case_id = seeded["cases"][0]
    await api.post(f"/api/v1/cases/{case_id}/actions/change_priority",
                   json={"params": {"priority": "high"}}, headers=_headers("ann"))
    async with TestSessionFactory() as db:
        rows = (await db.execute(
            select(CaseAuditLogModel).where(CaseAuditLogModel.case_id == case_id)
        )).scalars().all()
    assert [x.action for x in rows].count("priority_changed") == 1


async def test_reassign_revokes_the_previous_holders_access(seeded, api):
    """Moving the work must move the authorization with it.

    `repo.create_assignment` writes a `case#assignee@user` tuple for the NEW
    holder. Without an explicit removal the OLD holder keeps theirs — so someone
    reassigned away from a case retains access to it with no assignment left to
    justify it.
    """
    from sqlalchemy import select
    from velaris_shared.db.models import CaseAssignmentModel, HxGuardTupleModel

    case_id = seeded["cases"][0]
    async with TestSessionFactory() as db:
        db.add(CaseAssignmentModel(
            id=uuid.uuid4(), case_id=case_id, step_id="step-1",
            assignee_type="user", assignee_id="ann", status="active"))
        await db.commit()
    # The tuple the original assignment would have written.
    from velaris_shared.hxguard import tuples as hxg_tuples
    async with TestSessionFactory() as db:
        await hxg_tuples.write_tuple(
            db, object_type="case", object_id=case_id, relation="assignee",
            subject_type="user", subject_id="ann", created_by="test")
        await db.commit()

    r = await api.post(f"/api/v1/cases/{case_id}/actions/reassign",
                       json={"params": {"assignee_type": "user",
                                        "assignee_id": "ben"}},
                       headers=_headers("ann"))
    assert r.status_code == 200, r.text
    assert r.json()["detail"]["moved"] == 1

    async with TestSessionFactory() as db:
        holders = (await db.execute(
            select(HxGuardTupleModel.subject_id).where(
                HxGuardTupleModel.object_id == case_id,
                HxGuardTupleModel.relation == "assignee")
        )).scalars().all()
    assert "ben" in holders
    assert "ann" not in holders, "the previous holder kept case access"


async def test_reassign_reports_a_case_with_no_active_assignment(seeded, api):
    """Reported, not failed: a sweep over 400 cases where 30 are unassigned has
    not gone wrong."""
    case_id = seeded["cases"][1]
    r = await api.post(f"/api/v1/cases/{case_id}/actions/reassign",
                       json={"params": {"assignee_type": "user",
                                        "assignee_id": "ben"}},
                       headers=_headers("ann"))
    assert r.status_code == 200, r.text
    assert r.json()["detail"]["moved"] == 0
    assert "no active assignment" in r.json()["detail"]["note"]


async def test_bulk_honours_per_case_type_action_config(seeded, api):
    """Resolving config once at tenant scope would have let a selection bypass a
    per-case-type restriction that the single-case route enforces — worse than
    not offering bulk at all."""
    tenant = seeded["tenant"]
    async with TestSessionFactory() as db:
        db.add(CaseActionConfigModel(
            id=uuid.uuid4(), tenant_id=tenant.id, case_type_id=seeded["case_type"],
            scope_key=statuses.scope_key(tenant.id, seeded["case_type"]),
            action="close", label=None, enabled=True, sort_order=0,
            privilege=None, allowed_from=["resolved"]))
        await db.commit()

    r = await api.post("/api/v1/cases/bulk", json={
        "case_ids": [str(c) for c in seeded["cases"]],
        "action": "close", "params": {},
    }, headers=_headers("ann"))
    body = r.json()
    # Every case sits on `new`, so the per-case-type rule must refuse all three.
    assert body["succeeded"] == 0, body
    assert all(x["code"] == "VEL-CASE-ACTION-NOTALLOWED-409"
               for x in body["results"]), body


async def test_bulk_reports_a_cross_tenant_case_as_404_not_403(seeded, api):
    """A caller who may not reach a case must not be able to confirm the id
    exists — the posture the 2026-09-06 audit established.

    The SINGLE-case route is already covered: it calls `_get_case_or_404` before
    the dispatcher, which 404s first. **Bulk does not**, and cannot — it must
    report each case rather than raise on the first — so the distinction has to
    survive inside `perform()`. Collapsing every refusal to 403 there would tell
    a caller, per case, which of another tenant's ids are real.
    """
    async with TestSessionFactory() as db:
        beta = TenantModel(id=uuid.uuid4(), slug="beta2", name="Beta2")
        db.add(beta)
        db.add(UserDirectoryModel(id=uuid.uuid4(), user_id="bo", tenant_id="beta2"))
        await db.flush()
        theirs = CaseInstanceModel(
            id=uuid.uuid4(), case_number="BETA2-1", case_type_id=seeded["case_type"],
            case_type_version="1.0.0", status="new", priority="low",
            tenant_id=beta.id, created_by="bo", data={})
        db.add(theirs)
        await db.commit()
        theirs_id = theirs.id
    tenancy.clear_cache()

    r = await api.post("/api/v1/cases/bulk", json={
        "case_ids": [str(theirs_id)], "action": "change_priority",
        "params": {"priority": "high"},
    }, headers=_headers("ann"))
    assert r.status_code == 200, r.text
    refused = r.json()["results"][0]
    assert refused["ok"] is False
    # 404, not 403 — a real case in another tenant reads exactly like one that
    # does not exist.
    assert refused["code"] == "VEL-CASE-NOTFOUND-404", refused


async def test_bulk_reports_a_nonexistent_case_the_same_way(seeded, api):
    """The companion to the test above: an id that does not exist must be
    indistinguishable from one the caller may not reach."""
    r = await api.post("/api/v1/cases/bulk", json={
        "case_ids": [str(uuid.uuid4())], "action": "change_priority",
        "params": {"priority": "high"},
    }, headers=_headers("ann"))
    assert r.json()["results"][0]["code"] == "VEL-CASE-NOTFOUND-404"


# ── security posture ────────────────────────────────────────────────────────

async def test_the_audit_actor_is_the_caller_never_the_body(seeded, api):
    """**The six removed routes trusted `body.actor_id`.**

    `await _audit(session, case_id, "closed", actor_id=body.actor_id)` — an
    unverified, caller-supplied string. So any authenticated user could close a
    case and record somebody else as having done it, in the log an auditor
    reads. That is repudiation, and it was live on every one of the six.

    The dispatcher takes the actor from the authenticated session and nowhere
    else. A caller may still SEND `actor_id`; it lands in `details.params`,
    plainly labelled as what the caller asked for, and never as who acted.
    """
    from sqlalchemy import select
    from velaris_shared.db.models import CaseAuditLogModel

    case_id = seeded["cases"][0]
    r = await api.post(f"/api/v1/cases/{case_id}/actions/close",
                       json={"params": {"actor_id": "somebody.else",
                                        "updated_by": "somebody.else"}},
                       headers=_headers("ann"))
    assert r.status_code == 200, r.text

    async with TestSessionFactory() as db:
        row = (await db.execute(
            select(CaseAuditLogModel).where(
                CaseAuditLogModel.case_id == case_id,
                CaseAuditLogModel.action == "closed")
        )).scalars().first()
    assert row is not None
    assert row.actor_id != "somebody.else", "the caller forged the audit actor"


async def test_an_action_config_privilege_can_only_narrow(seeded, api):
    """A configuration that REPLACED the registry's privilege would be silent
    privilege escalation: rebinding `close` to a privilege every viewer holds
    would let viewers close cases, with nothing in the role store to show for
    it. The override is an ADDITIONAL requirement, so widening is impossible by
    construction rather than by a check that has to stay correct.
    """
    from case_service.case_actions.dispatcher import required_privileges
    from velaris_shared.db.models import CaseActionConfigModel as Cfg

    action = registry.ACTIONS["close"]
    # BOTH gates by default: the ReBAC relationship question the six removed
    # routes asked, and the platform privilege question.
    assert required_privileges(action, None) == (
        "case.update", "case.actions.close")

    weak = Cfg(action="close", privilege="calendar.events.read",
               enabled=True, sort_order=0, allowed_from=[], scope_key="x:y")
    required = required_privileges(action, weak)
    # Neither of the originals is displaced — the override is a THIRD check.
    assert "case.update" in required
    assert "case.actions.close" in required
    assert "calendar.events.read" in required
    assert len(required) == 3


async def test_assignee_type_is_a_closed_vocabulary(seeded, api):
    """An assignment stored with an unknown `assignee_type` gets **no HxGuard
    tuple** (the tuple is written only for `user`), is **invisible to
    `assignment_router`** (which selects on `user`), and still shows on the
    case — work assigned to nobody, silently. Every consumer branches on the
    literal string, so the vocabulary has to be closed at the door."""
    case_id = seeded["cases"][0]
    r = await api.post(f"/api/v1/cases/{case_id}/actions/assign",
                       json={"params": {"step_id": "s1", "assignee_type": "usr",
                                        "assignee_id": "ann"}},
                       headers=_headers("ann"))
    assert r.status_code == 400
    assert "VEL-CASE-ACTION-PARAM-400" in str(r.json())


async def test_oversized_params_are_refused_not_truncated(seeded, api):
    """A silently shortened `step_id` points at a step that does not exist."""
    case_id = seeded["cases"][0]
    r = await api.post(f"/api/v1/cases/{case_id}/actions/assign",
                       json={"params": {"step_id": "s" * 300,
                                        "assignee_type": "user",
                                        "assignee_id": "ann"}},
                       headers=_headers("ann"))
    assert r.status_code == 400
    assert "limit is 255" in str(r.json())


async def test_a_note_cannot_be_used_to_flood_the_model_context(seeded, api):
    """Notes are read back as HxNexus case context (`hxnexus/case_qa.py` reads
    `case_messages`), so an unbounded body is a way to push arbitrary text at
    the model — and a bulk run is 500 of them. The cap does not make the text
    trustworthy; it stops one call filling the context window."""
    case_id = seeded["cases"][0]
    r = await api.post(f"/api/v1/cases/{case_id}/actions/add_note",
                       json={"params": {"body": "x" * 25_000}},
                       headers=_headers("ann"))
    assert r.status_code == 400
    assert "VEL-CASE-ACTION-PARAM-400" in str(r.json())


async def test_config_refuses_another_tenants_case_type(seeded, api):
    """The row would be inert — its `scope_key` carries the WRITER's tenant, so
    it can only resolve for their own callers. It is refused anyway: a 200 on an
    id the caller cannot see is a confirmation oracle, and 404 is the posture
    the platform takes everywhere else."""
    async with TestSessionFactory() as db:
        other = TenantModel(id=uuid.uuid4(), slug="rival", name="Rival")
        db.add(other)
        await db.flush()
        theirs = CaseTypeModel(
            id=uuid.uuid4(), name="Rival CT", version="1.0.0",
            lifecycle_process_id=LIFECYCLE_PROCESS_ID,
            definition_json={"stages": []}, tenant_id=other.id)
        db.add(theirs)
        await db.commit()
        theirs_id = theirs.id
    tenancy.clear_cache()

    r = await api.put("/api/v1/case-config/actions", json={
        "action": "close", "enabled": False, "case_type_id": str(theirs_id),
    }, headers=_headers("ann"))
    assert r.status_code == 404, r.text


async def test_config_still_accepts_a_shared_case_type(seeded, api):
    """A NULL-tenant case type stays visible to everybody — what every release
    so far does, and what single-tenant installs depend on."""
    async with TestSessionFactory() as db:
        shared = CaseTypeModel(
            id=uuid.uuid4(), name="Shared CT", version="1.0.0",
            lifecycle_process_id=LIFECYCLE_PROCESS_ID,
            definition_json={"stages": []}, tenant_id=None)
        db.add(shared)
        await db.commit()
        shared_id = shared.id

    r = await api.put("/api/v1/case-config/actions", json={
        "action": "close", "enabled": True, "case_type_id": str(shared_id),
    }, headers=_headers("ann"))
    assert r.status_code == 200, r.text


async def test_the_rebac_relationship_gate_is_not_bypassed(seeded, api, monkeypatch):
    """**The regression this test exists to make impossible.**

    `hxguard.check()` routes an action to exactly ONE backend by name:
    `CASE_ACTIONS` → ReBAC (relationship to THIS case), `PLATFORM_ACTIONS` →
    privilege (role only). `case.update` is the former; `case.actions.*` are the
    latter.

    So a dispatcher that checked only its own new privilege would have silently
    replaced a per-case relationship check with a role check — every
    `case_worker` on the platform able to close any case they can see, with no
    assignment to it. That is looser than the six routes it replaced, and it
    would have looked entirely correct.
    """
    from velaris_shared.hxguard.service import CASE_ACTIONS, PLATFORM_ACTIONS
    from case_service.case_actions.dispatcher import required_privileges

    for name, action in registry.ACTIONS.items():
        required = required_privileges(action, None)
        # At least one name must reach the ReBAC backend...
        assert any(r in CASE_ACTIONS for r in required), (
            f"{name}: no case-level check — the relationship gate is bypassed")
        # ...and at least one must reach the privilege backend, or the new
        # privileges are decorative.
        assert any(r in PLATFORM_ACTIONS for r in required), (
            f"{name}: no platform privilege check")


async def test_every_required_check_is_actually_evaluated(seeded, api):
    """A name in neither routing set falls to the default backend, where an
    action absent from `ACTIONS` is a fail-closed deny — so a typo in a
    privilege would 403 every caller rather than fail loudly at import."""
    from velaris_shared.hxguard.service import CASE_ACTIONS, PLATFORM_ACTIONS
    from case_service.case_actions.dispatcher import required_privileges

    for name, action in registry.ACTIONS.items():
        for check in required_privileges(action, None):
            assert check in CASE_ACTIONS or check in PLATFORM_ACTIONS, (
                f"{name}: '{check}' is in neither routing set")


async def test_the_catalogue_still_offers_actions_to_a_permitted_caller(seeded, api):
    """`permitted` is the PLATFORM half of the gate only.

    `user_allows` is a synchronous privilege test — it can answer a platform
    action and not a ReBAC one. Feeding it `case.update` returns False for
    everybody, **including admin**, and the Studio action bar renders empty. The
    relationship half is checked at action time, per case, where it means
    something.
    """
    r = await api.get("/api/v1/cases/actions/catalogue", headers=_headers("ann"))
    assert r.status_code == 200, r.text
    actions = r.json()["actions"]
    permitted = [a for a in actions if a["permitted"]]
    assert len(permitted) == len(actions), (
        "an admin caller sees no permitted actions — the action bar would be empty")
    # And the ReBAC half is still declared, so nobody mistakes this for the
    # whole gate.
    assert all("case.update" in a["required_privileges"] for a in actions)


async def test_a_viewer_is_offered_nothing(seeded, api):
    """The other direction: a role holding none of the action privileges must
    not be shown buttons that will 403."""
    token = _token("vic", roles=("viewer",))
    r = await api.get("/api/v1/cases/actions/catalogue",
                      headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert [a for a in r.json()["actions"] if a["permitted"]] == []


async def test_the_role_gate_survives_case_enforcement_being_off(seeded, api, monkeypatch):
    """`require_case` returns early when `VELARIS_CASE_HXGUARD_CASE_ENFORCEMENT`
    is `off`. Routing the privilege through it would let that switch disable the
    ROLE gate as well — and an operator setting it to `off` is choosing how hard
    the relationship rules bite, not choosing to let a viewer close cases. Same
    reasoning that put the tenant clause outside the mode switch.
    """
    from velaris_shared.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "hxguard_case_enforcement", "off",
                        raising=False)
    from velaris_shared import hxguard
    hxguard.invalidate_cache()

    case_id = seeded["cases"][0]
    token = _token("vic2", roles=("viewer",))
    r = await api.post(f"/api/v1/cases/{case_id}/actions/close",
                       json={"params": {}},
                       headers={"Authorization": f"Bearer {token}"})
    # A viewer is still refused, with enforcement off.
    assert r.status_code in (403, 404), r.text
    hxguard.invalidate_cache()
