"""Stage-level status and actions — the second half of the 198 model.

Design: `docs/Future/Done/CASE-ACTIONS-STATUS-DESIGN.md` (§7).
Implementation: `case_service/case_actions/stages.py`.

What these pin, beyond "the feature works":

* **Absent is not empty.** A stage with no `actions_allowed` key offers every
  action — every stage on every install today. A stage with an EMPTY list offers
  none. Collapsing the two would be the `None`-versus-`[]` mistake #29's
  manifest permissions met in August, in the permissive direction.
* **It can only NARROW.** A stage listing an action the tenant has switched off,
  or the status rule refuses, does not resurrect it. Two rules, deny-overrides,
  and the wider answer is never produced — the same property that makes F-12's
  matcher safe.
* **`on_enter_status` never breaks a stage move.** A configured status that is
  unknown, disabled or unreachable leaves the case in its new stage and writes
  the reason to the audit log. Failing the transition would strand a case on a
  designer's typo, and the blocked operator is not the person who can fix it.
* **Every path into a stage gets it.** Three places write `current_stage_id`;
  an effect wired to one of them holds when a human clicks and quietly does not
  when the interpreter moves the case, which is the harder bug to notice.
* **Refused at authoring.** Both failures are silent at runtime, so a typo is a
  400 on the case type rather than a stage that does less than its author read.

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
    CaseActionConfigModel,
    CaseAuditLogModel,
    CaseStatusModel,
    CaseInstanceModel,
    CaseTypeModel,
    TenantModel,
    UserDirectoryModel,
)

from case_service.case_actions import stages, statuses
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


def _stages(*, triage_extra=None, review_extra=None):
    triage = {"id": "triage", "name": "Triage", "stage_type": "linear",
              "order": 0, "steps": []}
    review = {"id": "review", "name": "Review", "stage_type": "linear",
              "order": 1, "steps": []}
    triage.update(triage_extra or {})
    review.update(review_extra or {})
    return [triage, review]


@pytest_asyncio.fixture
async def api():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def staged():
    """A tenant, a two-stage case type, and a case sitting in the first stage."""
    tenancy.clear_cache()
    async with TestSessionFactory() as db:
        tenant = TenantModel(id=uuid.uuid4(), slug="stagex", name="Stage Co")
        db.add(tenant)
        db.add(UserDirectoryModel(id=uuid.uuid4(), user_id="sam", tenant_id="stagex"))
        ct = CaseTypeModel(
            id=uuid.uuid4(), name="Staged CT", version="1.0.0",
            lifecycle_process_id=LIFECYCLE_PROCESS_ID,
            definition_json={"stages": _stages()})
        db.add(ct)
        await db.flush()
        case = CaseInstanceModel(
            id=uuid.uuid4(), case_number="STG-1", case_type_id=ct.id,
            case_type_version="1.0.0", status="new", priority="medium",
            current_stage_id="triage", tenant_id=tenant.id,
            created_by="sam", data={})
        db.add(case)
        await db.commit()
        yield {"tenant": tenant, "case_type": ct.id, "case": case.id}
    tenancy.clear_cache()


async def _set_stages(case_type_id, stage_list):
    async with TestSessionFactory() as db:
        ct = await db.get(CaseTypeModel, case_type_id)
        ct.definition_json = {"stages": stage_list}
        await db.commit()


# ── absent is not empty ──────────────────────────────────────────────────────

async def test_a_stage_with_no_key_offers_everything():
    """Every install today. If this ever returned `()` the update would take
    every action away from every case on the box."""
    assert stages.allowed_actions({"id": "s"}) is None
    assert stages.action_offered({"id": "s"}, "close") is True


async def test_an_empty_list_offers_nothing():
    """A designer who typed a list and then cleared it meant something."""
    assert stages.allowed_actions({"id": "s", "actions_allowed": []}) == ()
    assert stages.action_offered({"id": "s", "actions_allowed": []}, "close") is False


async def test_an_explicit_null_is_read_as_unconfigured():
    """JSON round trips turn a cleared field into `null`, not a missing key."""
    assert stages.allowed_actions({"id": "s", "actions_allowed": None}) is None


async def test_a_malformed_value_is_not_read_as_permission():
    """A control whose entire purpose is to restrict must not fall open when its
    value is the wrong shape."""
    assert stages.allowed_actions({"id": "s", "actions_allowed": "close"}) == ()


async def test_no_stage_at_all_offers_everything():
    """A case with no `current_stage_id` — most cases, on most installs."""
    assert stages.action_offered(None, "close") is True


# ── the gate ─────────────────────────────────────────────────────────────────

async def test_stage_narrows_the_action(staged, api):
    await _set_stages(staged["case_type"],
                      _stages(triage_extra={"actions_allowed": ["add_note"]}))
    r = await api.post(f"/api/v1/cases/{staged['case']}/actions/close",
                       json={"params": {}}, headers=_headers("sam"))
    assert r.status_code == 409, r.text
    assert "VEL-CASE-ACTION-STAGE-409" in str(r.json())
    assert "Triage" in str(r.json())


async def test_an_action_the_stage_offers_still_runs(staged, api):
    await _set_stages(staged["case_type"],
                      _stages(triage_extra={"actions_allowed": ["close", "add_note"]}))
    r = await api.post(f"/api/v1/cases/{staged['case']}/actions/close",
                       json={"params": {}}, headers=_headers("sam"))
    assert r.status_code == 200, r.text
    assert r.json()["case"]["status"] == "closed"


async def test_an_empty_stage_list_refuses_every_action(staged, api):
    await _set_stages(staged["case_type"],
                      _stages(triage_extra={"actions_allowed": []}))
    r = await api.post(f"/api/v1/cases/{staged['case']}/actions/add_note",
                       json={"params": {"body": "hello"}}, headers=_headers("sam"))
    assert r.status_code == 409
    assert "offers no actions" in str(r.json())


async def test_the_stage_rule_cannot_widen_the_status_rule(staged, api):
    """The whole safety property. The stage offers `close`; the tenant's status
    rule says close only from `resolved`; the case is on `new`. Two rules,
    deny-overrides — and the refusal is the STATUS one, because it is asked
    first and neither answer can be overridden by the other."""
    await _set_stages(staged["case_type"],
                      _stages(triage_extra={"actions_allowed": ["close"]}))
    async with TestSessionFactory() as db:
        db.add(CaseActionConfigModel(
            id=uuid.uuid4(), tenant_id=staged["tenant"].id, case_type_id=None,
            scope_key=statuses.scope_key(staged["tenant"].id, None),
            action="close", label=None, enabled=True, sort_order=0,
            privilege=None, allowed_from=["resolved"]))
        await db.commit()

    r = await api.post(f"/api/v1/cases/{staged['case']}/actions/close",
                       json={"params": {}}, headers=_headers("sam"))
    assert r.status_code == 409
    assert "VEL-CASE-ACTION-NOTALLOWED-409" in str(r.json())


async def test_the_stage_rule_cannot_re_enable_a_disabled_action(staged, api):
    """A tenant switching an action off is not undone by a designer listing it."""
    await _set_stages(staged["case_type"],
                      _stages(triage_extra={"actions_allowed": ["cancel"]}))
    async with TestSessionFactory() as db:
        db.add(CaseActionConfigModel(
            id=uuid.uuid4(), tenant_id=staged["tenant"].id, case_type_id=None,
            scope_key=statuses.scope_key(staged["tenant"].id, None),
            action="cancel", label=None, enabled=False, sort_order=0,
            privilege=None, allowed_from=[]))
        await db.commit()

    r = await api.get(
        f"/api/v1/cases/actions/catalogue?case_id={staged['case']}",
        headers=_headers("sam"))
    assert r.status_code == 200, r.text
    assert [a["name"] for a in r.json()["actions"]] == []


# ── the catalogue reflects it ────────────────────────────────────────────────

async def test_catalogue_without_a_case_id_is_unchanged(staged, api):
    """The bulk bar spans many stages, so it asks the tenant-wide question. If
    the stage narrowing leaked into this answer a bulk bar would silently lose
    actions because one selected case happened to be in a closed stage."""
    await _set_stages(staged["case_type"],
                      _stages(triage_extra={"actions_allowed": ["add_note"]}))
    r = await api.get("/api/v1/cases/actions/catalogue", headers=_headers("sam"))
    assert r.status_code == 200, r.text
    names = [a["name"] for a in r.json()["actions"]]
    assert "close" in names and len(names) > 1
    assert r.json()["stage"] is None


async def test_catalogue_with_a_case_id_drops_what_the_stage_hides(staged, api):
    await _set_stages(staged["case_type"],
                      _stages(triage_extra={"actions_allowed": ["add_note"]}))
    r = await api.get(
        f"/api/v1/cases/actions/catalogue?case_id={staged['case']}",
        headers=_headers("sam"))
    assert r.status_code == 200, r.text
    assert [a["name"] for a in r.json()["actions"]] == ["add_note"]
    # Named, so a screen can say WHY a button it expected is missing.
    assert r.json()["stage"]["id"] == "triage"
    assert r.json()["stage"]["actions_allowed"] == ["add_note"]


async def test_catalogue_needs_a_relationship_to_the_case(staged, api):
    """Found in review of this feature, before it shipped.

    `case_id` made a tenant-wide endpoint answer a CASE-scoped question — which
    stage case X is in — and the first cut gated it on the tenant clause alone.
    That is exactly the shape of the nineteen handlers the 2026-09-06 audit
    found: `require_case` is the gate, and a tenant check is not a substitute
    for it. 404, never 403, so the answer does not confirm the id is real.
    """
    async with TestSessionFactory() as db:
        db.add(UserDirectoryModel(id=uuid.uuid4(), user_id="nel", tenant_id="stagex"))
        await db.commit()
    tenancy.clear_cache()

    # Same tenant, no relationship to the case, no platform-wide case access.
    token = _token("nel", roles=("case_worker",))
    r = await api.get(
        f"/api/v1/cases/actions/catalogue?case_id={staged['case']}",
        headers={"Authorization": f"Bearer {token}"})
    assert r.status_code in (403, 404), r.text

    # And the tenant-wide answer, which carries no case-scoped fact, is still
    # available to them — the endpoint did not become privileged, the parameter
    # did.
    r2 = await api.get("/api/v1/cases/actions/catalogue",
                       headers={"Authorization": f"Bearer {token}"})
    assert r2.status_code == 200, r2.text


async def test_catalogue_refuses_another_tenants_case(staged, api):
    """The tenant clause applies here as everywhere: 404, never 403, because a
    403 confirms the id exists."""
    async with TestSessionFactory() as db:
        other = TenantModel(id=uuid.uuid4(), slug="otherco", name="Other")
        db.add(other)
        db.add(UserDirectoryModel(id=uuid.uuid4(), user_id="oli", tenant_id="otherco"))
        await db.commit()
    tenancy.clear_cache()

    r = await api.get(
        f"/api/v1/cases/actions/catalogue?case_id={staged['case']}",
        headers=_headers("oli"))
    assert r.status_code == 404, r.text


# ── on_enter_status ──────────────────────────────────────────────────────────

async def test_entering_a_stage_sets_the_configured_status(staged, api):
    await _set_stages(staged["case_type"],
                      _stages(review_extra={"on_enter_status": "in_progress"}))
    r = await api.post(f"/api/v1/cases/{staged['case']}/stage",
                       json={"target_stage_id": "review"}, headers=_headers("sam"))
    assert r.status_code == 200, r.text

    async with TestSessionFactory() as db:
        case = await db.get(CaseInstanceModel, staged["case"])
        assert case.current_stage_id == "review"
        assert case.status == "in_progress"


async def test_a_stage_with_no_on_enter_status_changes_nothing(staged, api):
    """Today's behaviour on every install, stated as a test."""
    r = await api.post(f"/api/v1/cases/{staged['case']}/stage",
                       json={"target_stage_id": "review"}, headers=_headers("sam"))
    assert r.status_code == 200, r.text
    async with TestSessionFactory() as db:
        case = await db.get(CaseInstanceModel, staged["case"])
        assert case.current_stage_id == "review"
        assert case.status == "new"


async def test_an_unreachable_status_does_not_block_the_stage_move(staged, api):
    """A designer's typo must not strand a case. The stage move stands, the
    status does not change, and the reason is on the record."""
    await _set_stages(staged["case_type"],
                      _stages(review_extra={"on_enter_status": "closed"}))
    async with TestSessionFactory() as db:
        row = (await db.execute(select(CaseStatusModel).where(
            CaseStatusModel.name == "closed",
            CaseStatusModel.scope_key == statuses.scope_key(None, None),
        ))).scalars().first()
        row.allowed_from = ["resolved"]
        await db.commit()

    r = await api.post(f"/api/v1/cases/{staged['case']}/stage",
                       json={"target_stage_id": "review"}, headers=_headers("sam"))
    assert r.status_code == 200, r.text

    async with TestSessionFactory() as db:
        case = await db.get(CaseInstanceModel, staged["case"])
        assert case.current_stage_id == "review"     # the move stands
        assert case.status == "new"                  # the status did not
        rows = (await db.execute(select(CaseAuditLogModel).where(
            CaseAuditLogModel.case_id == staged["case"],
            CaseAuditLogModel.action == "stage_status_not_applied",
        ))).scalars().all()
        # Silence would be the worst outcome: a designer would see the stage
        # move and assume the status followed it.
        assert len(rows) == 1
        assert rows[0].details["code"] == "VEL-CASE-STATUS-TRANSITION-409"


async def test_re_entering_a_stage_on_the_same_status_writes_no_audit_row(staged, api):
    """A log full of non-events is how a real one gets missed."""
    await _set_stages(staged["case_type"],
                      _stages(review_extra={"on_enter_status": "in_progress"}))
    for _ in range(2):
        await api.post(f"/api/v1/cases/{staged['case']}/stage",
                       json={"target_stage_id": "review"}, headers=_headers("sam"))

    async with TestSessionFactory() as db:
        rows = (await db.execute(select(CaseAuditLogModel).where(
            CaseAuditLogModel.case_id == staged["case"],
            CaseAuditLogModel.action == "status_changed",
        ))).scalars().all()
        assert len(rows) == 1


async def test_the_status_change_goes_through_the_full_machinery(staged, api):
    """Not a column write. `apply_status_change` carries the audit row, the SLA
    lifecycle hook, the realtime broadcast and the outbox webhook; writing the
    column here would silently drop four of them."""
    await _set_stages(staged["case_type"],
                      _stages(review_extra={"on_enter_status": "in_progress"}))
    await api.post(f"/api/v1/cases/{staged['case']}/stage",
                   json={"target_stage_id": "review"}, headers=_headers("sam"))

    async with TestSessionFactory() as db:
        rows = (await db.execute(select(CaseAuditLogModel).where(
            CaseAuditLogModel.case_id == staged["case"],
            CaseAuditLogModel.action == "status_changed",
        ))).scalars().all()
        assert len(rows) == 1
        assert rows[0].new_value["status"] == "in_progress"


async def test_the_workflow_path_applies_it_too(staged):
    """Three places write `current_stage_id`. An effect wired to the HTTP route
    alone would hold when a human clicked and quietly not hold when the
    interpreter moved the case."""
    from case_service.core import case_lifecycle

    stage_list = _stages(review_extra={"on_enter_status": "in_progress"})
    await _set_stages(staged["case_type"], stage_list)

    async with TestSessionFactory() as db:
        await case_lifecycle.on_stage_entered(
            db, case_id=staged["case"], stage=stage_list[1],
            case_type_def={"stages": stage_list})
        await db.commit()

    async with TestSessionFactory() as db:
        case = await db.get(CaseInstanceModel, staged["case"])
        assert case.current_stage_id == "review"
        assert case.status == "in_progress"


async def test_the_auto_advance_path_applies_it_too(staged):
    """The third of the three. `_auto_advance_if_complete` is what the Temporal
    interpreter, the payments service and step completion all call — an
    `on_enter_status` that only fired on the two HTTP paths would hold when a
    human clicked and not when a payment cleared."""
    from case_service.api.routers.cases import _auto_advance_if_complete
    from velaris_shared.db.models import CaseStepCompletionModel

    stage_list = _stages(
        triage_extra={"steps": [{"id": "s1", "name": "S1", "step_type": "manual",
                                 "required": True}]},
        review_extra={"on_enter_status": "in_progress"})
    await _set_stages(staged["case_type"], stage_list)

    async with TestSessionFactory() as db:
        db.add(CaseStepCompletionModel(
            id=uuid.uuid4(), case_id=staged["case"], stage_id="triage",
            step_id="s1", status="completed"))
        await db.commit()

    async with TestSessionFactory() as db:
        new_stage, held = await _auto_advance_if_complete(
            db, staged["case"], "triage", {"stages": stage_list})
        await db.commit()
    assert new_stage == "review" and held is False

    async with TestSessionFactory() as db:
        case = await db.get(CaseInstanceModel, staged["case"])
        assert case.current_stage_id == "review"
        assert case.status == "in_progress"


# ── authoring-time validation ────────────────────────────────────────────────

async def test_an_unknown_action_in_a_stage_is_refused():
    """The vocabulary is closed and lives in code, so a name that is not an
    action today can never become one by an operator's act."""
    with pytest.raises(stages.StageConfigError) as exc:
        stages.validate_actions_allowed(
            _stages(triage_extra={"actions_allowed": ["close", "teleport"]}))
    assert exc.value.code == "VEL-CASE-STAGE-CONFIG-400"
    assert "teleport" in str(exc.value)


async def test_a_stage_with_no_keys_validates():
    stages.validate_actions_allowed(_stages())


async def test_deploying_a_case_type_with_a_bad_action_is_refused(staged, api):
    r = await api.post("/api/v1/case-types", json={
        "name": "Bad CT", "version": "1.0.0",
        "lifecycle_process_id": LIFECYCLE_PROCESS_ID,
        "definition_json": {"stages": _stages(
            triage_extra={"actions_allowed": ["teleport"]})},
    }, headers=_headers("sam"))
    assert r.status_code == 400, r.text
    assert "VEL-CASE-STAGE-CONFIG-400" in str(r.json())


async def test_deploying_a_case_type_with_a_bad_status_is_refused(staged, api):
    r = await api.post("/api/v1/case-types", json={
        "name": "Bad Status CT", "version": "1.0.0",
        "lifecycle_process_id": LIFECYCLE_PROCESS_ID,
        "definition_json": {"stages": _stages(
            review_extra={"on_enter_status": "nonesuch"})},
    }, headers=_headers("sam"))
    assert r.status_code == 400, r.text
    assert "VEL-CASE-STAGE-CONFIG-400" in str(r.json())


async def test_updating_a_case_type_with_a_bad_action_is_refused(staged, api):
    r = await api.patch(f"/api/v1/case-types/{staged['case_type']}", json={
        "definition_json": {"stages": _stages(
            triage_extra={"actions_allowed": ["teleport"]})},
    }, headers=_headers("sam"))
    assert r.status_code == 400, r.text
    assert "VEL-CASE-STAGE-CONFIG-400" in str(r.json())


async def test_a_good_stage_config_deploys(staged, api):
    r = await api.patch(f"/api/v1/case-types/{staged['case_type']}", json={
        "definition_json": {"stages": _stages(
            triage_extra={"actions_allowed": ["add_note", "close"]},
            review_extra={"on_enter_status": "in_progress"})},
    }, headers=_headers("sam"))
    assert r.status_code == 200, r.text


async def test_a_case_type_with_no_stage_keys_is_untouched(staged, api):
    """Every case type that exists today deploys exactly as it does now."""
    r = await api.patch(f"/api/v1/case-types/{staged['case_type']}", json={
        "definition_json": {"stages": _stages()},
    }, headers=_headers("sam"))
    assert r.status_code == 200, r.text


# ── bulk ─────────────────────────────────────────────────────────────────────

async def test_bulk_honours_the_stage_rule_per_case(staged, api):
    """A restriction enforced on the single-case route and bypassed by selecting
    the same cases and using bulk is worse than not offering bulk at all."""
    await _set_stages(staged["case_type"],
                      _stages(triage_extra={"actions_allowed": ["add_note"]}))
    r = await api.post("/api/v1/cases/bulk", json={
        "action": "close", "case_ids": [str(staged["case"])], "params": {},
    }, headers=_headers("sam"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["succeeded"] == 0
    assert body["results"][0]["code"] == "VEL-CASE-ACTION-STAGE-409"


# ── a tenant's OWN terminal status must end the case everywhere ──────────────
#
# Migration 198 replaced "which statuses end a case" with data, and fixed the
# three places that had hardcoded it. A sweep on 2026-09-08 found **eleven
# more** — most of them on the customer portal, where they decided whether a
# customer could still upload a document, complete a step, send a message, or
# be shown the case as finished.
#
# The defect that leaves is precise: a tenant adds its own terminal status,
# which is the whole point of the feature, and gets a case that is closed
# everywhere except in front of the customer, who goes on working on it. These
# pin the predicates rather than each call site, because eleven call sites is
# how the rule got copied eleven times in the first place.


@pytest_asyncio.fixture
async def own_terminal(staged):
    """A tenant-scoped status, terminal, that the platform has never heard of."""
    async with TestSessionFactory() as db:
        db.add(CaseStatusModel(
            id=uuid.uuid4(), tenant_id=staged["tenant"].id, case_type_id=None,
            scope_key=statuses.scope_key(staged["tenant"].id, None),
            name="settled", label="Settled", category="terminal",
            is_terminal=True, is_default=False, allowed_from=[],
            sort_order=99, enabled=True))
        case = await db.get(CaseInstanceModel, staged["case"])
        case.status = "settled"
        await db.commit()
    return staged


async def test_a_tenants_own_terminal_status_ends_the_case(own_terminal):
    async with TestSessionFactory() as db:
        case = await db.get(CaseInstanceModel, own_terminal["case"])
        assert await statuses.is_terminal_for_case(db, case) is True


async def test_a_tenants_own_terminal_status_counts_as_concluded(own_terminal):
    """Terminal and not cancelled — the progress rail and CSAT question."""
    async with TestSessionFactory() as db:
        case = await db.get(CaseInstanceModel, own_terminal["case"])
        assert await statuses.is_concluded_for_case(db, case) is True


async def test_a_tenants_own_terminal_status_closes_the_message_thread(own_terminal):
    """Terminal and not resolved — the one predicate where `resolved` stays open."""
    async with TestSessionFactory() as db:
        case = await db.get(CaseInstanceModel, own_terminal["case"])
        assert await statuses.is_thread_closed_for_case(db, case) is True


async def test_cancelled_is_over_but_not_concluded(staged):
    """The distinction the second predicate exists for: a cancelled case is
    finished, so no more work happens on it — but it was abandoned, so its rail
    must not read as complete and nobody should be asked to rate it."""
    async with TestSessionFactory() as db:
        case = await db.get(CaseInstanceModel, staged["case"])
        case.status = "cancelled"
        await db.commit()
    async with TestSessionFactory() as db:
        case = await db.get(CaseInstanceModel, staged["case"])
        assert await statuses.is_terminal_for_case(db, case) is True
        assert await statuses.is_concluded_for_case(db, case) is False


async def test_resolved_is_over_but_the_thread_stays_open(staged):
    """A customer replying to a resolution is how a case gets reopened; refusing
    that message would silently lose the one signal saying the fix was wrong."""
    async with TestSessionFactory() as db:
        case = await db.get(CaseInstanceModel, staged["case"])
        case.status = "resolved"
        await db.commit()
    async with TestSessionFactory() as db:
        case = await db.get(CaseInstanceModel, staged["case"])
        assert await statuses.is_terminal_for_case(db, case) is True
        assert await statuses.is_thread_closed_for_case(db, case) is False


async def test_an_open_case_is_none_of_the_three(staged):
    async with TestSessionFactory() as db:
        case = await db.get(CaseInstanceModel, staged["case"])   # `new`
        assert await statuses.is_terminal_for_case(db, case) is False
        assert await statuses.is_concluded_for_case(db, case) is False
        assert await statuses.is_thread_closed_for_case(db, case) is False


async def test_a_status_nobody_defined_is_not_terminal(staged):
    """A case sitting on a status no set defines is still an OPEN case —
    treating it as finished would silently stop work on it."""
    async with TestSessionFactory() as db:
        case = await db.get(CaseInstanceModel, staged["case"])
        case.status = "bespoke_legacy"
        await db.commit()
    async with TestSessionFactory() as db:
        case = await db.get(CaseInstanceModel, staged["case"])
        assert await statuses.is_terminal_for_case(db, case) is False


async def test_no_case_at_all_is_not_terminal():
    """Every call site passes a row that may be None."""
    async with TestSessionFactory() as db:
        assert await statuses.is_terminal_for_case(db, None) is False
        assert await statuses.is_concluded_for_case(db, None) is False
        assert await statuses.is_thread_closed_for_case(db, None) is False


# ── Migration 198's fallout, two more call sites (found 2026-09-09) ──────────


async def test_no_production_code_composes_a_route_198_removed():
    """`testsuite/executor.py` still mapped `resolve_case` to
    `POST /cases/{id}/resolve` — a route removed on 2026-09-07 — so every
    generated scenario using that step posted to a 404. A step that cannot pass,
    in the feature whose whole job is telling you whether your process still
    works.

    198's sweep counted Studio call sites, tests and docs. This is production
    code that COMPOSES a URL from a table, which none of those categories
    catches, and the analytics-service test suite was missed for the same
    reason — it is not case-service's.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[4]
    removed = ("resolve", "reopen", "cancel", "close", "priority")
    # ONE segment between `cases/` and the verb, because that is what 198
    # removed: `POST /cases/{id}/resolve`. The first spelling of this allowed
    # any number of segments and so flagged
    # `/cases/{id}/documents/index-jobs/{job_id}/cancel` — a different route
    # family that happens to end in a word this one also uses. A guard that
    # cries wolf gets widened until it catches nothing.
    pattern = re.compile(
        r"cases/[^\"'/ ]+/(%s)\b" % "|".join(removed))

    here = pathlib.Path(__file__).resolve()
    offenders = []
    for path in root.rglob("*.py"):
        text = str(path)
        if "/Velaris/" in text or "/node_modules/" in text or "/.venv/" in text:
            continue
        # This file quotes the removed route in the docstring above, on purpose.
        if path.resolve() == here:
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line in content.splitlines():
            if "/actions/" in line or line.strip().startswith(("#", "*")):
                continue
            if pattern.search(line):
                offenders.append(f"{path.relative_to(root)}: {line.strip()[:80]}")
    assert not offenders, (
        "call site(s) still using a route migration 198 removed:\n  "
        + "\n  ".join(offenders))


def test_the_198_guard_does_not_flag_a_deeper_route_that_ends_in_the_same_word():
    """The guard's own precision, pinned.

    198 removed `POST /cases/{id}/cancel`. It did not remove — and nothing
    should stop anybody adding — `POST /cases/{id}/documents/index-jobs/{job}/cancel`,
    which is a different resource that happens to share a verb. This test exists
    because the first version of the pattern flagged exactly that, on the day
    the route was written.
    """
    import re

    pattern = re.compile(r"cases/[^\"'/ ]+/(resolve|reopen|cancel|close|priority)\b")

    assert pattern.search('await client.post(f"/api/v1/cases/{cid}/resolve")')
    assert pattern.search("MAP = {'resolve_case': 'cases/%s/resolve'}")
    assert not pattern.search(
        '@router.post("/cases/{case_id}/documents/index-jobs/{job_id}/cancel")')
    assert not pattern.search('f"/api/v1/cases/{cid}/actions/cancel"')
