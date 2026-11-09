"""Who may submit a step's form or complete a step (2026-10-28, user's decision).

The assignee; anyone in an access group the assignee is also in; the assignee's
N+1; for a queue, a group covering the queue; for an unassigned step, any group
covering the case type. Everyone else is refused — a caller in no group too.
Every test runs as the least-privileged user who should pass, and as one who
must be refused (velaris_shared/hxguard/step_submit.py).

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from case_service.main import app
from tests.conftest import TestSessionFactory
from velaris_shared.auth.dependencies import get_current_user
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.db import repository as repo
from velaris_shared.db.models import (
    AccessGroupModel, OperatorAccessGroupModel, PortalModel, UserDirectoryModel,
)

FORM = {"sections": [{"id": "s", "title": "S", "order": 0, "fields": [
    {"id": "f1", "type": "text", "label": "Name", "field_key": "name", "required": True}]}]}


async def _group(members: list[str], case_types=("*",), queues=("*",), tenant="default") -> None:
    async with TestSessionFactory() as db:
        portal = PortalModel(name=f"P-{uuid.uuid4().hex[:6]}", portal_type="staff")
        db.add(portal)
        await db.flush()
        g = AccessGroupModel(name=f"G-{uuid.uuid4().hex[:6]}", tenant_id=tenant, portal_id=portal.id,
                             allowed_case_type_ids=list(case_types), allowed_queue_ids=list(queues))
        db.add(g)
        await db.flush()
        for m in members:
            db.add(OperatorAccessGroupModel(operator_id=m, access_group_id=g.id))
        await db.commit()


async def _manager(user: str, manager: str) -> None:
    async with TestSessionFactory() as db:
        db.add(UserDirectoryModel(user_id=user, manager_user_id=manager))
        await db.commit()


def _as(uid: str):
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(user_id=uid, roles=["case_worker"])


async def _case(client: AsyncClient, with_form: bool = True) -> tuple[str, str, str]:
    """(case_type_id, case_id, form_id) — made by the superadmin test client."""
    form_id = ""
    step: dict = {"id": "step-1", "name": "Do", "step_type": "user_task", "required": True}
    if with_form:
        f = await client.post("/api/v1/forms", json={
            "name": f"F-{uuid.uuid4().hex[:6]}", "version": "1.0.0", "definition_json": FORM})
        form_id = f.json()["id"]
        step["form_id"] = form_id
    ct = await client.post("/api/v1/case-types", json={
        "name": f"SS-{uuid.uuid4().hex[:6]}", "version": "1.0.0", "lifecycle_process_id": "p",
        "definition_json": {"stages": [{"id": "st", "name": "S", "stage_type": "linear", "order": 0,
                                        "steps": [step]}], "sla_policies": []}})
    assert ct.status_code == 201, ct.text
    case = await client.post("/api/v1/cases", json={"case_type_id": ct.json()["id"], "data": {}})
    assert case.status_code == 201, case.text
    return ct.json()["id"], case.json()["id"], form_id


async def _assign(session, case_id: str, assignee: str, kind: str = "user"):
    a = await repo.create_assignment(session, data={
        "case_id": uuid.UUID(case_id), "step_id": "step-1", "assignee_type": kind,
        "assignee_id": assignee, "status": "active"})
    await session.commit()
    return a


async def _submit(client, a, form_id):
    return await client.post(f"/api/v1/form-submissions/{a.id}",
                             json={"form_id": form_id, "values": {"name": "Jane"}})


async def _complete(client, case_id):
    return await client.post(f"/api/v1/cases/{case_id}/steps/step-1/complete", json={
        "stage_id": "st", "step_type": "user_task", "status": "completed", "data": {"name": "Jane"}})


@pytest.fixture(autouse=True)
def _restore():
    yield
    app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_the_assignee_submits_without_any_group(client: AsyncClient, session):
    _, case_id, form_id = await _case(client)
    a = await _assign(session, case_id, "ss-assignee-1")
    _as("ss-assignee-1")
    r = await _submit(client, a, form_id)
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_a_colleague_in_the_assignees_group_may_and_an_outsider_may_not(client: AsyncClient, session):
    _, case_id, form_id = await _case(client)
    await _group(["ss-a2", "ss-colleague-2"])
    await _group(["ss-outsider-2"])
    a = await _assign(session, case_id, "ss-a2")
    _as("ss-outsider-2")
    r = await _submit(client, a, form_id)
    assert r.status_code == 403, r.text
    r = await _complete(client, case_id)
    assert r.status_code == 403, r.text
    _as("ss-colleague-2")
    r = await _submit(client, a, form_id)
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_a_caller_in_no_group_is_refused(client: AsyncClient, session):
    _, case_id, form_id = await _case(client)
    a = await _assign(session, case_id, "ss-a3")
    _as("ss-nobody-3")
    r = await _submit(client, a, form_id)
    assert r.status_code == 403 and "not in an access group" in r.text


@pytest.mark.asyncio
async def test_the_n_plus_1_may_in_a_group_covering_the_case_type(client: AsyncClient, session):
    ct_id, case_id, form_id = await _case(client)
    await _group(["ss-a4"])
    await _group(["ss-boss-4"], case_types=(ct_id,))
    await _manager("ss-a4", "ss-boss-4")
    a = await _assign(session, case_id, "ss-a4")
    _as("ss-boss-4")
    r = await _submit(client, a, form_id)
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_the_n_plus_1_without_a_covering_group_is_refused(client: AsyncClient, session):
    _, case_id, form_id = await _case(client)
    await _group(["ss-boss-5"], case_types=(str(uuid.uuid4()),))
    await _manager("ss-a5", "ss-boss-5")
    a = await _assign(session, case_id, "ss-a5")
    _as("ss-boss-5")
    r = await _submit(client, a, form_id)
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_a_shared_group_must_cover_the_case_type(client: AsyncClient, session):
    """ABAC: sharing a group is not enough when that group does not cover this case type."""
    _, case_id, form_id = await _case(client)
    await _group(["ss-a6", "ss-colleague-6"], case_types=(str(uuid.uuid4()),))
    a = await _assign(session, case_id, "ss-a6")
    _as("ss-colleague-6")
    r = await _submit(client, a, form_id)
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_a_shared_group_of_another_tenant_does_not_count(client: AsyncClient, session):
    """ABAC: the admitting group must belong to the case's tenant."""
    from velaris_shared.db.models import TenantModel
    _, case_id, form_id = await _case(client)
    mine, other = (TenantModel(id=uuid.uuid4(), slug=f"t-{uuid.uuid4().hex[:8]}", name="T")
                   for _ in range(2))
    session.add_all([mine, other])
    case = await repo.get_case_instance(session, uuid.UUID(case_id))
    case.tenant_id = mine.id
    await session.commit()
    await _group(["ss-a7", "ss-colleague-7"], tenant=other.slug)
    a = await _assign(session, case_id, "ss-a7")
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        user_id="ss-colleague-7", roles=["case_worker"], tenant_id=str(mine.id))
    r = await _submit(client, a, form_id)
    assert r.status_code == 403, r.text
    await _group(["ss-a7", "ss-colleague-7"], tenant=mine.slug)
    r = await _submit(client, a, form_id)
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_a_queue_item_needs_a_group_covering_the_queue(client: AsyncClient, session):
    _, case_id, form_id = await _case(client)
    queue = str(uuid.uuid4())
    await _group(["ss-q-in-8"], queues=(queue,))
    await _group(["ss-q-out-8"], queues=(str(uuid.uuid4()),))
    a = await _assign(session, case_id, queue, kind="queue")
    _as("ss-q-out-8")
    r = await _submit(client, a, form_id)
    assert r.status_code == 403, r.text
    _as("ss-q-in-8")
    r = await _submit(client, a, form_id)
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_an_unassigned_step_needs_a_group_covering_the_case_type(client: AsyncClient, session):
    ct_id, case_id, _ = await _case(client)
    await _group(["ss-wrong-9"], case_types=(str(uuid.uuid4()),))
    await _group(["ss-right-9"], case_types=(ct_id,))
    _as("ss-wrong-9")
    assert (await _complete(client, case_id)).status_code == 403
    _as("ss-right-9")
    r = await _complete(client, case_id)
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_assignment_complete_and_activate_take_the_same_rule(client: AsyncClient, session):
    _, case_id, _ = await _case(client, with_form=False)
    await _group(["ss-outsider-10"])
    a = await _assign(session, case_id, "ss-a10")
    _as("ss-outsider-10")
    r = await client.post(f"/api/v1/assignments/{a.id}/complete", json={"completed_by": "ss-outsider-10"})
    assert r.status_code == 403, r.text
    r = await client.post(f"/api/v1/cases/{case_id}/steps/step-1/activate")
    assert r.status_code == 403, r.text
    _as("ss-a10")
    r = await client.post(f"/api/v1/assignments/{a.id}/complete", json={"completed_by": "ss-a10"})
    assert r.status_code == 200, r.text


# ── the ways around the rule, closed (2026-10-28 second pass) ─────────────────

@pytest.mark.asyncio
async def test_an_outsider_cannot_take_the_work_by_claim_reassign_or_delete(client: AsyncClient, session):
    _, case_id, _ = await _case(client, with_form=False)
    await _group(["ss-a11", "ss-colleague-11"])
    await _group(["ss-outsider-11"])
    a = await _assign(session, case_id, "ss-a11")
    _as("ss-outsider-11")
    r = await client.post(f"/api/v1/assignments/{a.id}/claim", json={"user_id": "ss-outsider-11"})
    assert r.status_code == 403, r.text
    r = await client.post(f"/api/v1/assignments/{a.id}/reassign", json={"new_assignee_id": "ss-outsider-11"})
    assert r.status_code == 403, r.text
    r = await client.delete(f"/api/v1/assignments/{a.id}")
    assert r.status_code == 403, r.text
    still = await repo.get_assignment(session, a.id)
    assert still is not None and still.assignee_id == "ss-a11"


@pytest.mark.asyncio
async def test_work_cannot_be_reassigned_to_someone_outside_the_groups(client: AsyncClient, session):
    _, case_id, _ = await _case(client, with_form=False)
    await _group(["ss-a12", "ss-colleague-12"])
    a = await _assign(session, case_id, "ss-a12")
    _as("ss-colleague-12")
    r = await client.post(f"/api/v1/assignments/{a.id}/reassign", json={"new_assignee_id": "ss-stranger-12"})
    assert r.status_code == 422, r.text
    r = await client.post(f"/api/v1/assignments/{a.id}/reassign", json={"new_assignee_id": "ss-colleague-12"})
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_no_one_acts_in_someone_elses_name(client: AsyncClient, session):
    _, case_id, form_id = await _case(client)
    queue = str(uuid.uuid4())
    await _group(["ss-q13", "ss-other-13"], queues=(queue,))
    a = await _assign(session, case_id, queue, kind="queue")
    _as("ss-q13")
    r = await client.post(f"/api/v1/assignments/{a.id}/claim", json={"user_id": "ss-other-13"})
    assert r.status_code == 403, r.text
    r = await client.post(f"/api/v1/form-submissions/{a.id}",
                          json={"form_id": form_id, "values": {"name": "J"}, "completed_by": "ss-other-13"})
    assert r.status_code == 403, r.text
    r = await client.post(f"/api/v1/assignments/{a.id}/claim", json={"user_id": "ss-q13"})
    assert r.status_code == 200, r.text
    # Release was checked against the body's user_id, which named whoever it liked.
    _as("ss-other-13")
    r = await client.post(f"/api/v1/assignments/{a.id}/release", json={"user_id": "ss-q13"})
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_the_audit_names_the_signed_in_user(client: AsyncClient, session):
    _, case_id, form_id = await _case(client)
    a = await _assign(session, case_id, "ss-a14")
    _as("ss-a14")
    r = await client.post(f"/api/v1/form-submissions/{a.id}",
                          json={"form_id": form_id, "values": {"name": "J"}})
    assert r.status_code == 200, r.text
    from sqlalchemy import select
    from velaris_shared.db.models import CaseAuditLogModel
    rows = (await session.execute(select(CaseAuditLogModel).where(
        CaseAuditLogModel.case_id == uuid.UUID(case_id),
        CaseAuditLogModel.action == "form_submitted"))).scalars().all()
    assert rows and all(r.actor_id == "ss-a14" for r in rows)


@pytest.mark.asyncio
async def test_a_role_assignment_needs_a_group_holding_the_role(client: AsyncClient, session):
    from velaris_shared.db.models import AccessRoleModel
    _, case_id, form_id = await _case(client)
    async with TestSessionFactory() as db:
        role = AccessRoleModel(name=f"adjuster-{uuid.uuid4().hex[:6]}", privileges=[])
        portal = PortalModel(name=f"P-{uuid.uuid4().hex[:6]}", portal_type="staff")
        db.add_all([role, portal])
        await db.flush()
        g = AccessGroupModel(name=f"G-{uuid.uuid4().hex[:6]}", tenant_id="default",
                             portal_id=portal.id, role_ids=[str(role.id)])
        db.add(g)
        await db.flush()
        db.add(OperatorAccessGroupModel(operator_id="ss-holder-15", access_group_id=g.id))
        await db.commit()
        role_name = role.name
    await _group(["ss-without-15"])
    a = await _assign(session, case_id, role_name, kind="role")
    _as("ss-without-15")
    assert (await _submit(client, a, form_id)).status_code == 403
    _as("ss-holder-15")
    r = await _submit(client, a, form_id)
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_an_unknown_assignee_type_is_refused(client: AsyncClient, session):
    _, case_id, form_id = await _case(client)
    await _group(["ss-any-16"])
    a = await _assign(session, case_id, "x", kind="mystery")
    _as("ss-any-16")
    assert (await _submit(client, a, form_id)).status_code == 403


@pytest.mark.asyncio
async def test_self_assign_only_reaches_a_visible_case(client: AsyncClient, session):
    from velaris_shared.db.models import TenantModel
    _, case_id, _ = await _case(client, with_form=False)
    other = TenantModel(id=uuid.uuid4(), slug=f"t-{uuid.uuid4().hex[:8]}", name="Other")
    session.add(other)
    case = await repo.get_case_instance(session, uuid.UUID(case_id))
    case.tenant_id = other.id
    await session.commit()
    _as("ss-self-17")
    r = await client.post(f"/api/v1/my/self-assign?case_id={case_id}")
    assert r.status_code == 404, r.text
