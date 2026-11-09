"""Who may submit a step's form or complete a step — decided by access groups.

Until 2026-10-28 `POST /form-submissions/{id}`, `POST /cases/{id}/steps/{step}/
complete` and `POST /assignments/{id}/complete` checked only that the caller was
signed in to the case's tenant, so anyone there (a viewer too) could complete
anyone's work. The rule now (user's decision, 2026-10-28: "anyone in the same
access group or N+1 … as RBAC and ABAC"):

* **RBAC** — the caller is
  - the assignee, or
  - a member of an access group the assignee is also in, or
  - the assignee's N+1 (`user_directory.manager_user_id`);
  for a queue assignment, a member of a group whose `allowed_queue_ids` covers
  the queue; for a role assignment, a member of a group holding that access role;
  for an unassigned (or agent) step, a member of any group. Any other assignee
  type is refused.
  Claim, release, reassign and delete use the same rule (`assignments.py::_guard`),
  so taking the work is no way around it.
* **ABAC** — the group that admits the caller belongs to the case's tenant, is
  active, and its `allowed_case_type_ids` covers the case type (`["*"]` = all).
  The assignee needs no group: the assignment itself is the grant.
* **A caller in no access group is refused**, as for creating a case (2026-10-18).
* `superadmin` passes — the break-glass every other gate keeps.

Identities are matched as `auth.dependencies` matches them: the token's user
id, then username, then email, by precedence.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from sqlalchemy import select

NOT_YOURS = (
    "You cannot complete this work: it is assigned to someone outside your access "
    "groups. Ask the assignee, someone in their access group, or their manager."
)
NO_GROUP = (
    "You are not in an access group, so you cannot complete case work. An "
    "administrator assigns one in Admin Studio > Access Groups."
)


def _identities(user: Any) -> list[str]:
    return [str(i) for i in (getattr(user, "user_id", None), getattr(user, "username", None),
                             getattr(user, "email", None)) if i]


async def _groups_of(session: Any, idents: list[str]) -> list[Any]:
    """Active access groups of the first identity that has any (precedence, not IN)."""
    from velaris_shared.db.models import AccessGroupModel, OperatorAccessGroupModel
    for ident in idents:
        rows = (await session.execute(
            select(AccessGroupModel).join(
                OperatorAccessGroupModel,
                OperatorAccessGroupModel.access_group_id == AccessGroupModel.id,
            ).where(OperatorAccessGroupModel.operator_id == ident)
        )).scalars().all()
        if rows:
            return [g for g in rows if g.is_active is not False]
    return []


def _covers(allowed: Any, value: Any) -> bool:
    items = [str(a) for a in (allowed or ["*"])]
    return "*" in items or str(value) in items


async def _usable(session: Any, groups: list[Any], case: Any) -> list[Any]:
    """The caller's groups that may act on this case (ABAC: tenant + case type)."""
    from velaris_shared.tenancy.identity import same_tenant
    out = []
    for g in groups:
        if not _covers(g.allowed_case_type_ids, case.case_type_id):
            continue
        if await same_tenant(session, case.tenant_id, g.tenant_id):
            out.append(g)
    return out


async def step_submit_decision(
    session: Any, user: Any, case: Any, assignment: Any | None,
) -> tuple[bool, str]:
    """`(allowed, reason)` for completing `assignment` (or an unassigned step) on `case`."""
    from velaris_shared.tenancy.identity import is_superadmin, may_reach_tenant
    if is_superadmin(user):
        return True, "superadmin break-glass"
    if not await may_reach_tenant(session, user, case.tenant_id):
        return False, NOT_YOURS
    me = _identities(user)
    a_type = getattr(assignment, "assignee_type", None)
    assignee = str(getattr(assignment, "assignee_id", "") or "")

    if a_type == "user" and assignee and assignee in me:
        return True, "assignee"

    mine = await _usable(session, await _groups_of(session, me), case)
    if not mine:
        return False, NO_GROUP

    if a_type == "user" and assignee:
        theirs = {str(g.id) for g in await _groups_of(session, [assignee])}
        shared = next((g for g in mine if str(g.id) in theirs), None)
        if shared is not None:
            return True, f"shares access group '{shared.name}' with the assignee"
        from velaris_shared.db.models import UserDirectoryModel
        manager = (await session.execute(
            select(UserDirectoryModel.manager_user_id).where(UserDirectoryModel.user_id == assignee)
        )).scalar_one_or_none()
        if manager and str(manager) in me:
            return True, "the assignee's manager (N+1)"
        return False, NOT_YOURS

    if a_type == "queue" and assignee:
        g = next((g for g in mine if _covers(g.allowed_queue_ids, assignee)), None)
        if g is not None:
            return True, f"access group '{g.name}' covers the queue"
        return False, NOT_YOURS

    if a_type == "role" and assignee:
        g = await _group_with_role(session, mine, assignee)
        if g is not None:
            return True, f"access group '{g.name}' holds the role"
        return False, NOT_YOURS

    # Unassigned, or assigned to an agent: any group covering the case type.
    # Any other assignee type is unknown here, so it is refused (fail closed).
    if assignment is None or a_type == "agent" or not assignee:
        return True, f"access group '{mine[0].name}' covers the case type"
    return False, NOT_YOURS


async def _group_with_role(session: Any, groups: list[Any], role: str) -> Any | None:
    """The first group holding the access role `role` (by id or by name)."""
    from velaris_shared.db.models import AccessRoleModel
    for g in groups:
        ids = [str(r) for r in (g.role_ids or [])]
        if role in ids:
            return g
        if ids:
            import uuid as _uuid
            uuids = []
            for r in ids:
                try:
                    uuids.append(_uuid.UUID(r))
                except ValueError:
                    continue
            names = (await session.execute(
                select(AccessRoleModel.name).where(AccessRoleModel.id.in_(uuids))
            )).scalars().all() if uuids else []
            if role in names:
                return g
    return None


def acts_as(user: Any, claimed: str | None) -> bool:
    """Is `claimed` (a user id sent in a body) the caller? Superadmin may name anyone."""
    from velaris_shared.tenancy.identity import is_superadmin
    return not claimed or is_superadmin(user) or str(claimed) in _identities(user)


async def may_receive_work(session: Any, assignee: str, case: Any) -> bool:
    """May `assignee` be given work on `case`? (in an access group covering it)"""
    return bool(await _usable(session, await _groups_of(session, [assignee]), case))


async def require_step_submit(session: Any, user: Any, case: Any, assignment: Any | None) -> None:
    """403 with the reason when `step_submit_decision` refuses, logged like every HxGuard gate."""
    ok, reason = await step_submit_decision(session, user, case, assignment)
    if ok:
        return
    try:
        from velaris_shared.hxguard.service import Decision, _maybe_log_deny, subject_from_user
        await _maybe_log_deny(session, subject_from_user(user), "case.step.complete",
                              {"case_id": str(case.id),
                               "assignment_id": str(getattr(assignment, "id", "") or "")},
                              Decision(False, reason, "access_group"), "authz_denied")
    except Exception:                                   # the audit never decides
        pass
    raise HTTPException(status_code=403, detail=reason)
