"""One place where a case action happens — single or bulk.

Design: docs/Future/Done/CASE-ACTIONS-STATUS-DESIGN.md

**Why one dispatcher rather than seventeen routes.** Every named action route
has to remember the same six things: the tenant clause, the per-case ReBAC
check, the privilege, the audit row, the lifecycle hook and the realtime
broadcast. Seventeen routes is seventeen chances to forget one — and that is not
hypothetical here. Nineteen handlers gated on `require_case` alone were readable
across the tenant boundary until 2026-09-06, and the fix was to put the clause
*inside* `require_case` so the next feature inherits it. Same lesson, one level
up: put the action machinery in one function so the next action inherits it.

**And bulk stops being a feature.** `POST /cases/bulk` is this same function in a
loop. There is no second implementation to keep in step, which is the whole
reason B3 in `market-gaps-secondary.md` closes here rather than being built.

Three things this refuses to do, each for a recorded reason:

* **It never checks permission once for a batch.** `require_case` is per case,
  because the answer genuinely differs per case.
* **It never truncates.** A request naming more ids than the ceiling is refused,
  not silently applied to the first N and reported as success — the posture
  `scheduler/actions.py::CaseBulkAction` already takes.
* **It never hides partial failure behind a 200 body with no detail.** Partial
  success is the normal outcome of a bulk run; the caller gets a per-case result.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db import repository as repo
from velaris_shared.db.models import (
    CaseActionConfigModel,
    CaseAssignmentModel,
    CaseInstanceModel,
    CaseMessageModel,
    CaseSLAInstanceModel,
)

from case_service.case_actions import registry, stages, statuses
from case_service.case_actions.registry import ActionDef

log = logging.getLogger(__name__)


#: The most cases one request may act on. Deliberately the same ceiling the
#: scheduled bulk action uses, so an operator does not have to learn two
#: numbers, and deliberately refused rather than truncated when exceeded.
BULK_CEILING = 500


class _Unset:
    """Distinguishes "the caller resolved the stage and it has none" from "the
    caller did not resolve it". `None` is a real answer here — a case with no
    stage — so it cannot double as "not supplied"."""


_UNSET = _Unset()


class ActionError(Exception):
    """An action that cannot be performed, with a code the caller can act on."""

    def __init__(self, message: str, *, code: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass
class CaseResult:
    case_id: uuid.UUID
    ok: bool
    detail: dict[str, Any] | None = None
    code: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"case_id": str(self.case_id), "ok": self.ok}
        if self.ok:
            out["detail"] = self.detail or {}
        else:
            out["code"] = self.code
            out["error"] = self.error
        return out


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── configuration ────────────────────────────────────────────────────────────

async def resolve_config(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID | None = None,
    case_type_id: uuid.UUID | None = None,
) -> dict[str, CaseActionConfigModel]:
    """Per-tenant overrides for this scope, keyed by action name.

    Same most-specific-wins precedence as the status set, and the same merge
    rather than replace: a tenant that renames one action still gets the other
    ten.
    """
    keys = [statuses.scope_key(None, None)]
    if tenant_id is not None:
        keys.append(statuses.scope_key(tenant_id, None))
        if case_type_id is not None:
            keys.append(statuses.scope_key(tenant_id, case_type_id))
    elif case_type_id is not None:
        keys.append(statuses.scope_key(None, case_type_id))

    try:
        rows = (await session.execute(
            select(CaseActionConfigModel).where(CaseActionConfigModel.scope_key.in_(keys))
        )).scalars().all()
    except Exception as exc:                                   # noqa: BLE001
        # No configuration means the registry's own defaults, which is exactly
        # how the platform behaves before anyone configures anything. It must
        # never mean "no action is permitted".
        log.warning("case action config unreadable, using registry defaults: %s", exc)
        return {}

    order = {k: i for i, k in enumerate(keys)}
    out: dict[str, CaseActionConfigModel] = {}
    for row in sorted(rows, key=lambda r: order.get(r.scope_key, 0)):
        out[row.action] = row
    return out


def required_privileges(
    action: ActionDef, cfg: CaseActionConfigModel | None,
) -> tuple[str, ...]:
    """Every check the caller must pass, in order. **Overrides ADD, never replace.**

    Two DIFFERENT questions, and both have to be asked, because
    `hxguard.check()` routes an action to exactly one backend by name:

    * **`rebac_action`** (`case.update`) is in `CASE_ACTIONS`, so it goes to the
      ReBAC backend: *does this operator have a relationship to THIS case* —
      assignee, editor, or a manager bypass. This is the gate all six removed
      routes actually had.
    * **`privilege`** (`case.actions.close`) is in `PLATFORM_ACTIONS`, so it
      goes to the privilege backend: *may this kind of operator ever do this*.

    Checking only the second would have silently replaced a per-case
    relationship check with a role check — a `case_worker` with no connection to
    a case could close it. Checking only the first would make the new privileges
    decorative. So both, always, and the relationship one first because it is
    the pre-existing behaviour and the parity baseline.

    A tenant's configured privilege is a THIRD requirement, never a
    substitution. A configuration that replaced the gate would be silent
    privilege escalation: rebinding `close` to a privilege every viewer holds
    would let viewers close cases, with nothing in the role store to show for
    it. Widening is impossible by construction rather than by a check that has
    to be kept correct — the same shape as F-12's matcher, where the wider
    ceiling is never produced and so can never win.
    """
    out = [action.rebac_action, action.privilege]
    if cfg is not None and cfg.privilege and cfg.privilege not in out:
        out.append(cfg.privilege)
    return tuple(out)


# ── parameter hygiene ────────────────────────────────────────────────────────
#
# Handlers read only named keys, and nothing here reaches raw SQL — every write
# goes through SQLAlchemy, so injection is not the risk. The risk is the quieter
# one: a value that is *accepted*, stored, and then means nothing to the code
# that has to act on it.

#: Closed, because every consumer in the platform branches on `== "user"`:
#: `assignment_router` selects on it, `_sync_assignee_tuple` writes the HxGuard
#: tuple only for it, and `assignments.py` gates lock ownership on it. An
#: assignment stored as `usr` therefore gets **no authorization tuple, is
#: invisible to routing, and still shows on the case** — work assigned to
#: nobody, silently. Live data has only ever held `user` and `queue`.
ASSIGNEE_TYPES = frozenset({"user", "queue"})

#: A note is fed to HxNexus as case context (`hxnexus/case_qa.py` reads
#: `case_messages`), so an unbounded body is a way to push arbitrary text at the
#: model — and a bulk run is 500 of them. The cap does not make the text
#: trustworthy; it stops one call from filling the context window.
MAX_NOTE_CHARS = 20_000


def _bounded(params: dict, key: str, *, limit: int, required: bool = True) -> str:
    value = str(params.get(key) or "").strip()
    if not value:
        if required:
            raise ActionError(f"'{key}' must not be empty.",
                              code="VEL-CASE-ACTION-PARAM-400")
        return ""
    if len(value) > limit:
        # Refused, not truncated. A silently shortened `step_id` points at a
        # step that does not exist, which is worse than a 400.
        raise ActionError(
            f"'{key}' is {len(value)} characters; the limit is {limit}.",
            code="VEL-CASE-ACTION-PARAM-400")
    return value


# ── handlers ─────────────────────────────────────────────────────────────────
#
# One per action. Each returns the detail dict that lands in the per-case
# result. None of them commit: the caller owns the transaction, so a bulk run
# commits once and a single action commits once, by the same code path.

async def _h_status(
    session: AsyncSession, case: CaseInstanceModel, action: ActionDef,
    params: dict, actor: str, tenant_uuid: uuid.UUID | None,
) -> dict[str, Any]:
    target = params["status"] if action.status_effect == "$param" else action.status_effect
    target = str(target)

    try:
        resolved = await statuses.validate_transition(
            session, current=case.status, target=target,
            tenant_id=tenant_uuid, case_type_id=case.case_type_id,
        )
    except statuses.StatusError as exc:
        # The HTTP status comes from the CODE's own suffix, in one place, rather
        # than being asserted here as well. `…-UNKNOWN-400` is a bad request —
        # the caller named a status that does not exist — while `…-TRANSITION-409`
        # is a conflict with the case's current state. Two sources of truth for
        # one answer is how they drift.
        raise ActionError(str(exc), code=exc.code) from exc

    prev = case.status
    now = _utcnow()

    # Routed through the SAME `apply_status_change` the named routes used, not
    # around it. That function is the full machinery — audit row, SLA lifecycle
    # hook, HxSentinel signal extraction on close, realtime broadcast and the
    # outbox webhook — and a dispatcher that wrote the column directly would
    # silently drop all five. Its own docstring says why: "state never changes
    # silently".
    from case_service.api.routers.cases import apply_status_change

    await apply_status_change(session, case.id, target, actor,
                              params.get("reason"),
                              audit_action=action.audit_name)

    # The two timestamps the named routes stamped, driven off the RESOLVED
    # status rather than off the action name — so a tenant that adds its own
    # terminal status gets the same stamp with no code change.
    values: dict[str, Any] = {}
    if target == "resolved" and getattr(case, "resolved_at", None) is None:
        values["resolved_at"] = now
    if target == "closed" and getattr(case, "closed_at", None) is None:
        values["closed_at"] = now
    # `resolution` is deliberately NOT written to the case: there is no such
    # column, and the named `/resolve` route recorded it in the audit details.
    # The dispatcher passes it through to the audit entry the same way.
    if values:
        await repo.update_case_instance(session, case.id, values=values)

    return {
        "from": prev, "to": target,
        "terminal": resolved.is_terminal, "category": resolved.category,
        # `apply_status_change` has already written the audit row, with this
        # action's own verb. Marked so `perform` does not write a second one —
        # two entries per status change would double the log an auditor reads.
        "_audited": True,
    }


async def _h_priority(
    session: AsyncSession, case: CaseInstanceModel, action: ActionDef,
    params: dict, actor: str, tenant_uuid: uuid.UUID | None,
) -> dict[str, Any]:
    # Deliberately NOT a closed set: a tenant may have its own priority ladder,
    # and unlike `assignee_type` nothing in the platform branches on a specific
    # value — an unknown priority sorts oddly, it does not lose work.
    priority = _bounded(params, "priority", limit=20).lower()
    prev = case.priority
    # Same reason as the status handler: `apply_priority_change` carries the
    # audit row and the outbox webhook, and writing the column directly would
    # drop both.
    from case_service.api.routers.cases import apply_priority_change

    await apply_priority_change(session, case.id, priority, actor,
                                audit_action=action.audit_name)
    return {"from": prev, "to": priority, "_audited": True}


async def _h_assign(
    session: AsyncSession, case: CaseInstanceModel, action: ActionDef,
    params: dict, actor: str, tenant_uuid: uuid.UUID | None,
) -> dict[str, Any]:
    assignee_type = _bounded(params, "assignee_type", limit=20)
    if assignee_type not in ASSIGNEE_TYPES:
        raise ActionError(
            f"'{assignee_type}' is not an assignee type. Known: "
            f"{', '.join(sorted(ASSIGNEE_TYPES))}.",
            code="VEL-CASE-ACTION-PARAM-400")
    row = await repo.create_assignment(session, data={
        "case_id": case.id,
        "step_id": _bounded(params, "step_id", limit=255),
        "assignee_type": assignee_type,
        "assignee_id": _bounded(params, "assignee_id", limit=255),
        "status": "active",
    })
    return {"assignment_id": str(row.id), "step_id": row.step_id}


async def _h_reassign(
    session: AsyncSession, case: CaseInstanceModel, action: ActionDef,
    params: dict, actor: str, tenant_uuid: uuid.UUID | None,
) -> dict[str, Any]:
    """Move every ACTIVE assignment on this case.

    The scheduled bulk action refuses assignment because an assignment needs a
    `step_id` and an unattended run has no way to name one. That objection does
    not apply here: these rows already carry their steps, so nothing is guessed.

    A case with no active assignment is **reported, not failed** — a sweep over
    400 cases where 30 happen to be unassigned has not gone wrong.
    """
    rows = (await session.execute(
        select(CaseAssignmentModel).where(
            CaseAssignmentModel.case_id == case.id,
            CaseAssignmentModel.status == "active",
        )
    )).scalars().all()

    if not rows:
        return {"moved": 0, "note": "no active assignment"}

    new_type = _bounded(params, "assignee_type", limit=20)
    if new_type not in ASSIGNEE_TYPES:
        raise ActionError(
            f"'{new_type}' is not an assignee type. Known: "
            f"{', '.join(sorted(ASSIGNEE_TYPES))}.",
            code="VEL-CASE-ACTION-PARAM-400")
    new_id = _bounded(params, "assignee_id", limit=255)
    moved = []
    for row in rows:
        if row.assignee_type == new_type and row.assignee_id == new_id:
            continue
        previous = f"{row.assignee_type}:{row.assignee_id}"
        prev_type, prev_id = row.assignee_type, row.assignee_id
        # Closed and recreated rather than mutated in place, so
        # `repo.create_assignment` writes the HxGuard assignee tuple for the new
        # holder in the same transaction.
        row.status = "reassigned"
        fresh = await repo.create_assignment(session, data={
            "case_id": case.id, "step_id": row.step_id,
            "assignee_type": new_type, "assignee_id": new_id,
            "status": "active",
        })
        # **And the OLD holder's tuple has to go**, or reassigning work away
        # from someone leaves them with case access they no longer have any
        # assignment to justify — the work moves and the authorization does not.
        # `_sync_assignee_tuple` is reused rather than calling `remove_tuple`
        # directly because it already carries the rule that matters: the tuple
        # is removed only when that user holds NO other active assignment on
        # this case, so moving one step must not revoke access granted by a
        # still-active one.
        if prev_type == "user":
            from case_service.api.routers.assignments import _sync_assignee_tuple
            await session.flush()
            await _sync_assignee_tuple(session, case.id, prev_id, add=False)
        moved.append({"step_id": row.step_id, "from": previous,
                      "assignment_id": str(fresh.id)})
    return {"moved": len(moved), "assignments": moved}


async def _h_note(
    session: AsyncSession, case: CaseInstanceModel, action: ActionDef,
    params: dict, actor: str, tenant_uuid: uuid.UUID | None,
) -> dict[str, Any]:
    body = _bounded(params, "body", limit=MAX_NOTE_CHARS)
    # `portal_visible` defaults FALSE and is not settable from a bulk run: a
    # sweep that accidentally published 400 internal notes to 400 customers is
    # not recoverable by any means this platform has.
    msg = CaseMessageModel(
        case_id=case.id, author=f"user:{actor}", author_name=actor,
        body=body, portal_visible=False,
    )
    session.add(msg)
    await session.flush()
    return {"message_id": str(msg.id)}


async def _h_hold(
    session: AsyncSession, case: CaseInstanceModel, action: ActionDef,
    params: dict, actor: str, tenant_uuid: uuid.UUID | None,
) -> dict[str, Any]:
    """On hold, and the clock stops — with the reason recorded.

    A hold that leaves the SLA running is a label on a case that is still going
    late, which is worse than not offering the action because it looks like it
    worked.

    **The pause itself is not done here.** `on_status_changed` pauses the SLAs
    of any case entering a `waiting` status, and it does so for every waiting
    status a tenant defines rather than for two hardcoded names. Pausing again
    here would be a second implementation of the same rule, and the two would
    drift. What this adds is the REASON — `on_status_changed` has no reason to
    record, and "why is this case on hold" is the question an operator actually
    asks a week later.
    """
    detail = await _h_status(session, case, action, params, actor, tenant_uuid)

    reason = _bounded(params, "reason", limit=1000, required=False)
    annotated = 0
    slas = (await session.execute(
        select(CaseSLAInstanceModel).where(
            CaseSLAInstanceModel.case_id == case.id,
            CaseSLAInstanceModel.status == "paused",
        )
    )).scalars().all()
    for sla in slas:
        try:
            from case_service.core.sla_escalation import record_pause_with_reason
            await record_pause_with_reason(session, sla, reason, actor)
            annotated += 1
        except Exception as exc:                               # noqa: BLE001
            # The status change is the operator's instruction and it stands; a
            # missing reason on the pause log is reported beside it rather than
            # rolling the whole action back.
            log.warning("hold: reason not recorded on case %s: %s", case.id, exc)
    detail["slas_paused"] = len(slas)
    if annotated < len(slas):
        detail["reason_not_recorded"] = len(slas) - annotated
    return detail


# ── §7 answered 2026-09-08 ───────────────────────────────────────────────────

async def _h_delete(
    session: AsyncSession, case: CaseInstanceModel, action: ActionDef,
    params: dict, actor: str, tenant_uuid: uuid.UUID | None,
) -> dict[str, Any]:
    """Hide the case, or bring it back. Never removes a row.

    `restore` shares this handler because they are one operation with a sign:
    writing the same three columns the other way. Two handlers would be two
    chances for the read filter and the write to disagree about what "deleted"
    means.
    """
    deleting = action.name == "delete"
    if bool(getattr(case, "is_deleted", False)) == deleting:
        # Already in the requested state. Not an error — a bulk sweep over 400
        # cases where 30 are already hidden has not gone wrong — and not a
        # second audit row for a change that did not happen.
        return {"is_deleted": deleting, "changed": False}

    await repo.update_case_instance(session, case.id, values={
        "is_deleted": deleting,
        "deleted_at": _utcnow() if deleting else None,
        "deleted_by": (actor if deleting else None),
    })
    return {"is_deleted": deleting, "changed": True,
            "reason": params.get("reason")}


async def _h_escalate(
    session: AsyncSession, case: CaseInstanceModel, action: ActionDef,
    params: dict, actor: str, tenant_uuid: uuid.UUID | None,
) -> dict[str, Any]:
    """Mark it escalated, then fire the tree — in that order, and for a reason.

    The status change is the operator's instruction and it stands whatever the
    escalation machinery does. The tree reaches OUTSIDE the case — it notifies
    people who are not working it — so it is the part most likely to fail on a
    given install (no tree configured, no SLA running, a notification channel
    down), and none of those is a reason to refuse the operator's escalation.

    What is NOT done here: inventing an escalation for a case that has no SLA to
    escalate. `apply_level` is defined over an SLA instance, and a case with no
    running SLA has no level schedule to advance. That case is escalated in
    status, the fact that no tree fired is REPORTED in the result, and nothing
    is fabricated to make the answer look complete.
    """
    detail = await _h_status(session, case, action, params, actor, tenant_uuid)
    detail["levels_fired"] = 0

    try:
        from case_service.core import sla_escalation as _esc

        slas = (await session.execute(
            select(CaseSLAInstanceModel).where(
                CaseSLAInstanceModel.case_id == case.id,
                CaseSLAInstanceModel.status.in_(("on_track", "at_risk", "breached")),
            )
        )).scalars().all()
        if not slas:
            detail["escalation"] = "no running SLA on this case — status only"
            return detail

        fired = []
        for sla in slas:
            tree = await _esc.resolve_escalation_tree_for_policy(
                session, sla.sla_policy_id)
            if not tree:
                continue
            snap = _esc.snapshot_tree(tree)
            levels = snap.get("levels") or []
            if not levels:
                continue
            nxt = int(getattr(sla, "escalation_level", 0) or 0)
            if nxt >= len(levels):
                continue
            out = await _esc.apply_level(session, case.id, sla, levels[nxt])
            fired.append({"sla_policy_id": str(sla.sla_policy_id),
                          "level": levels[nxt].get("level"),
                          "result": out.get("status") if isinstance(out, dict) else None})
        detail["levels_fired"] = len(fired)
        if fired:
            detail["escalated"] = fired
        else:
            detail["escalation"] = "no escalation tree with a next level — status only"
    except Exception as exc:                                    # noqa: BLE001
        log.warning("escalate: tree not fired on case %s: %s", case.id, exc)
        detail["escalation_error"] = str(exc)[:200]
    return detail


async def _h_split(
    session: AsyncSession, case: CaseInstanceModel, action: ActionDef,
    params: dict, actor: str, tenant_uuid: uuid.UUID | None,
) -> dict[str, Any]:
    """Create a linked child, carrying the parent's tenant.

    Deliberately the same shape `POST /{id}/children` already writes — a child
    row plus a `child` relationship with `propagate_status` — rather than a
    second notion of "related case". The parent therefore learns when the child
    finishes, through the propagation `relationship_manager` already performs.

    **The child inherits the parent's tenant, and is refused if the case type
    belongs to someone else.** A split that quietly produced a case in another
    tenant would be a tenant boundary crossed by an action nobody thinks of as
    crossing one.
    """
    raw = _bounded(params, "case_type_id", limit=64)
    try:
        ct_id = uuid.UUID(raw)
    except ValueError:
        raise ActionError(f"'{raw}' is not a case type id.",
                          code="VEL-CASE-ACTION-PARAM-400") from None

    ct = await repo.get_case_type(session, ct_id)
    if ct is None:
        raise ActionError("That case type does not exist.",
                          code="VEL-CASE-ACTION-PARAM-400")
    # A tenant-owned case type is usable only by its own tenant. A global one
    # (tenant_id NULL) is usable by everybody, as everywhere else.
    if ct.tenant_id is not None and str(ct.tenant_id) != str(case.tenant_id or ""):
        raise ActionError("That case type belongs to another tenant.",
                          code="VEL-CASE-ACTION-PARAM-400")

    from case_service.rollouts import service as _rollouts
    child_data = params.get("data") if isinstance(params.get("data"), dict) else {}
    ct, _rollout = await _rollouts.resolve_case_type(
        session, case_type=ct, tenant=case.tenant_id,
        subject=_rollouts.subject_of(
            child_data.get("customer_id"),
            (case.data or {}).get("customer_id") if isinstance(case.data, dict) else None,
            case.created_by, actor),
        data=child_data)
    child = await repo.create_case_instance(session, data={
        "case_type_id": ct.id,
        "case_type_version": ct.version,
        "status": "new",
        "priority": params.get("priority") or case.priority,
        "data": child_data,
        "parent_case_id": case.id,
        "tenant_id": case.tenant_id,
        "created_by": actor,
    })
    await _rollouts.record(session, case_id=child.id, res=_rollout,
                           tenant=case.tenant_id, via="case_action")
    await repo.create_relationship(session, data={
        "source_case_id": case.id,
        "target_case_id": child.id,
        "relationship_type": "child",
        "propagate_status": True,
    })
    return {"child_case_id": str(child.id), "case_type_id": str(ct.id)}


#: What `transfer` may hand a case to. Closed, and the omission is the point:
#: `tenant` is not here, and adding it is a design rather than an entry.
TRANSFER_TARGETS = frozenset({"queue", "access_group"})


async def _h_transfer(
    session: AsyncSession, case: CaseInstanceModel, action: ActionDef,
    params: dict, actor: str, tenant_uuid: uuid.UUID | None,
) -> dict[str, Any]:
    """Hand the case to another queue, or to an access group.

    Two forms, one action, because from an operator's chair they are the same
    sentence — "this belongs to them now" — and the difference is only who
    "them" is.

    * **queue** — the case's ACTIVE assignments move, which is exactly what
      `reassign` does with `assignee_type: "queue"`. It is routed through the
      same handler rather than reimplemented, so the assignee tuple bookkeeping
      that reassign gets right (the old holder's access is withdrawn only when
      they hold no other active assignment) is not written a second time.
    * **access_group** — a tuple against the GROUP, resolved through membership
      at decision time. Somebody joining the group next week inherits the case;
      somebody leaving loses it. Expanding it into per-user tuples now would
      produce rows that outlive the membership that justified them.

    **A tenant is not a transfer target.** Moving a case across the tenant
    boundary has to say what moves with it and who may authorise it; it is
    refused here by name so the refusal is legible rather than a generic
    "unknown target".
    """
    target_type = _bounded(params, "target_type", limit=32)
    if target_type == "tenant":
        raise ActionError(
            "A case cannot be transferred to another tenant by this action. "
            "Moving a case across the tenant boundary has to say what moves "
            "with it — documents, messages, audit history, variables, "
            "assignments, SLAs, portal links — and who may authorise it. It "
            "has its own two-person flow: POST /cases/{id}/tenant-transfers.",
            code="VEL-CASE-ACTION-PARAM-400")
    if target_type not in TRANSFER_TARGETS:
        raise ActionError(
            f"'{target_type}' is not a transfer target. Known: "
            f"{', '.join(sorted(TRANSFER_TARGETS))}.",
            code="VEL-CASE-ACTION-PARAM-400")

    target_id = _bounded(params, "target_id", limit=255)

    if target_type == "queue":
        moved = await _h_reassign(
            session, case, action,
            {**params, "assignee_type": "queue", "assignee_id": target_id},
            actor, tenant_uuid)
        return {"target_type": "queue", "target_id": target_id, **moved}

    # ── access group ────────────────────────────────────────────────────────
    from velaris_shared.db.models import AccessGroupModel

    relation = str(params.get("relation") or "editor")
    if relation not in ("viewer", "editor"):
        raise ActionError("relation must be viewer or editor.",
                          code="VEL-CASE-ACTION-PARAM-400")
    try:
        gid = uuid.UUID(target_id)
    except ValueError:
        raise ActionError(f"'{target_id}' is not an access group id.",
                          code="VEL-CASE-ACTION-PARAM-400") from None

    group = await session.get(AccessGroupModel, gid)
    if group is None or not group.is_active:
        raise ActionError("That access group does not exist, or is not active.",
                          code="VEL-CASE-ACTION-PARAM-400")
    # The same tenant rule every other by-id lookup on this surface applies. A
    # transfer to a neighbouring tenant's group would hand them the case through
    # the back door that `target_type: tenant` is refused at the front for.
    #
    # **Through `same_tenant`, and that is not a stylistic choice.**
    # `access_groups.tenant_id` is a `String(255)` holding a SLUG, while
    # `case_instances.tenant_id` is a `GUID`. A direct `str(a) != str(b)` — which
    # is what this line was for its first ten minutes — never matches, so every
    # transfer to a legitimate group would have been refused as "another
    # tenant": the exact failure `costing._authorized_case` had, where a GUID was
    # compared to a slug and every tenant-owned case answered "not found" to its
    # own tenant. `same_tenant` resolves both forms, which is what it exists for.
    from velaris_shared.tenancy.identity import same_tenant

    if not await same_tenant(session, case.tenant_id, getattr(group, "tenant_id", None)):
        raise ActionError("That access group belongs to another tenant.",
                          code="VEL-CASE-ACTION-PARAM-400")

    from velaris_shared.hxguard import tuples as _tuples
    await _tuples.write_tuple(
        session, object_type="case", object_id=case.id,
        relation=relation, subject_type="access_group",
        subject_id=str(gid), created_by=actor,
    )
    return {"target_type": "access_group", "target_id": str(gid),
            "relation": relation, "group": group.name}


_HANDLERS = {
    "open":            _h_status,
    "close":           _h_status,
    "resolve":         _h_status,
    "reopen":          _h_status,
    "cancel":          _h_status,
    "change_status":   _h_status,
    "put_on_hold":     _h_hold,
    "change_priority": _h_priority,
    "assign":          _h_assign,
    "reassign":        _h_reassign,
    "add_note":        _h_note,
    "archive":         _h_status,
    "delete":          _h_delete,
    "restore":         _h_delete,
    "escalate":        _h_escalate,
    "split":           _h_split,
    "transfer":        _h_transfer,
}


def _missing_handlers() -> set[str]:
    return set(registry.ACTIONS) - set(_HANDLERS)


# An action in the registry with no handler would 500 at call time rather than
# 404, which is the one failure mode a closed vocabulary is supposed to prevent.
if _missing_handlers():                                        # pragma: no cover
    raise RuntimeError(f"case actions without handlers: {sorted(_missing_handlers())}")


# ── the dispatcher ───────────────────────────────────────────────────────────

async def perform(
    session: AsyncSession,
    *,
    case_id: uuid.UUID,
    action_name: str,
    params: dict[str, Any],
    user: Any,
    tenant_uuid: uuid.UUID | None,
    config: dict[str, CaseActionConfigModel] | None = None,
    stage_def: Any = _UNSET,
    audit=None,
) -> CaseResult:
    """Perform one action on one case. Never raises for a per-case failure.

    Everything that can be wrong with the *request* — an unknown action, a
    missing parameter — is checked by `precheck` before any case is touched, so
    a bulk run cannot half-apply because id 200 was missing a field. What is
    left here is genuinely per-case: does this operator have a relationship to
    THIS case, does the tenant boundary allow it, is this transition legal from
    THIS case's current status, and does THIS case's stage offer the action.

    `stage_def` lets a caller that has already resolved the stage hand it in —
    a bulk run resolves one per case type and stage rather than one per case.
    It is `_UNSET` rather than `None` by default because `None` is a real
    answer here: a case with no stage, which is most cases on most installs.
    """
    from velaris_shared import hxguard

    action = registry.get(action_name)
    if action is None:                                          # pragma: no cover
        return CaseResult(case_id, False, code="VEL-CASE-ACTION-UNKNOWN-404",
                          error=f"Unknown action '{action_name}'.")

    cfg = (config or {}).get(action_name)

    try:
        # Per case, never once for the batch. `require_case` now carries the
        # tenant clause itself, so the boundary is inherited — but only if it is
        # actually called for each id.
        #
        # EVERY privilege, not the last one written: a tenant's override is an
        # extra requirement on top of the registry's, so the loop can only ever
        # make the gate harder to pass.
        from velaris_shared.hxguard.service import PLATFORM_ACTIONS

        for check_name in required_privileges(action, cfg):
            if check_name in PLATFORM_ACTIONS:
                # The ROLE question, asked unconditionally.
                #
                # `require_case` returns early when
                # `VELARIS_CASE_HXGUARD_CASE_ENFORCEMENT` is `off`, and routing
                # the privilege through it would let that switch disable the
                # role gate as well. An operator who sets that to `off` is
                # choosing how hard the RELATIONSHIP rules bite — the same
                # reasoning that put the tenant clause outside the mode switch
                # in `require_case` itself. It is not a choice to let a viewer
                # close cases.
                await hxguard.require_user(session, user, check_name)
            else:
                # The RELATIONSHIP question, which honours the switch exactly as
                # the six removed routes did.
                await hxguard.require_case(session, user, check_name, case_id)
        # `split` creates a case: the access-group rule for creating one
        # applies to the child's type, on top of the parent's privileges.
        if action_name == "split" and user is not None:
            from velaris_shared.hxguard.case_create import require_case_create
            raw_ct = str((params or {}).get("case_type_id") or "")
            if raw_ct:
                await require_case_create(session, user, raw_ct)
    except Exception as exc:                                    # noqa: BLE001
        # **404 stays 404.** `require_case` answers 404 rather than 403 for a
        # case in another tenant, deliberately: a caller who may not reach a
        # case must not be able to confirm that the id exists. Collapsing every
        # refusal to 403 here would have undone that on every action — the
        # posture the whole 2026-09-06 audit was about. Only a genuine
        # privilege denial is reported as 403.
        status = getattr(exc, "status_code", None)
        code = ("VEL-CASE-NOTFOUND-404" if status == 404
                else "VEL-CASE-ACTION-DENIED-403")
        detail = getattr(exc, "detail", None)
        return CaseResult(case_id, False, code=code, error=str(detail or exc))

    case = (await session.execute(
        select(CaseInstanceModel).where(CaseInstanceModel.id == case_id)
    )).scalars().first()
    if case is None:
        return CaseResult(case_id, False, code="VEL-CASE-NOTFOUND-404",
                          error="Case not found.")

    # The customer's half of the state machine: the platform says what `close`
    # does, the tenant says which statuses it may be done from.
    allowed_from = list(cfg.allowed_from or []) if cfg else []
    if allowed_from and (case.status or "") not in allowed_from:
        return CaseResult(
            case_id, False, code="VEL-CASE-ACTION-NOTALLOWED-409",
            error=f"'{action.label}' is not available from status "
                  f"'{case.status}'. Allowed from: {', '.join(allowed_from)}.")

    # ── archived, and deleted, are the two states nothing else may act on ────
    #
    # `archive` means the case is sealed: the user's words were "wrapped up
    # without any further changes". So every action is refused from it — with
    # ONE exception, `reopen`, and the exception is deliberate. An action with
    # literally no reverse is one an operator who archives the wrong case can
    # never undo, and "no way back" is a worse property for a control than "the
    # way back is privileged and audited", which is what reopen is.
    #
    # A soft-deleted case is likewise inert, and there the single exception is
    # `restore`. Both are checked here rather than in each handler, so an action
    # added next year inherits the rule instead of having to remember it.
    # One definition of "sealed", in `case_actions.seal`, shared with the write
    # routes that do NOT come through here — `POST /{id}/stage`,
    # `/{id}/assignments` and `/{id}/shares` all wrote happily to an archived
    # case until 2026-09-08, because the rule lived only in this function.
    from case_service.case_actions import seal as _seal

    sealed = _seal.sealed_reason(case, action=action_name)
    if sealed:
        return CaseResult(case_id, False, code=_seal.CODE, error=sealed)

    # A legal hold (Oct item 13) freezes concealment as well as destruction:
    # `delete` hides the case from every list and read, which is not preserving
    # it. Keyed on the action's declared EFFECT, not its name, so a destructive
    # action added next year inherits the rule. `restore` shares the effect and
    # is the one way back, so it stays open — the same shape as `reopen` above.
    if "delete" in action.effects and action_name != "restore":
        from case_service.legal_hold import service as _legal_hold

        if await _legal_hold.is_held(session, case_id):
            return CaseResult(
                case_id, False, code=_legal_hold.CODE_HELD,
                error=f"This case is under a legal hold; '{action.label}' is refused "
                      "until the hold is released.")

    # The designer's half of the same question. Two stages can share a status,
    # so this can never stand in for the check above — it is asked AS WELL,
    # never INSTEAD, and it can only ever narrow. A stage that says nothing
    # offers everything, which is every stage on every install today.
    stage = stage_def if stage_def is not _UNSET else await stages.stage_def(
        session, case_type_id=case.case_type_id, stage_id=case.current_stage_id)
    if not stages.action_offered(stage, action_name):
        offered = stages.allowed_actions(stage) or ()
        return CaseResult(
            case_id, False, code="VEL-CASE-ACTION-STAGE-409",
            error=(f"'{action.label}' is not offered in stage "
                   f"'{(stage or {}).get('name') or case.current_stage_id}'. "
                   + (f"Offered here: {', '.join(sorted(offered))}."
                      if offered else "This stage offers no actions.")))

    actor = getattr(user, "user_id", None) or "unknown"

    # ── HxArbiter PEP ────────────────────────────────────────────────────────
    #
    # HxGuard has already decided whether this CALLER may act on this case. The
    # engine answers a different question — whether this TRANSITION is permitted
    # for this case in this tenant — which a policy can express and a role
    # cannot: "an EU case may not be closed while a retention hold stands".
    #
    # Until the dispatcher existed there were six routes that changed a status
    # and therefore nowhere sensible to put this, so status changes had **no
    # policy control at all** while stage transitions had one. A sovereignty
    # rule that can refuse a stage move and not a close was only ever half a
    # control.
    #
    # NG1 holds: asked AFTER the HxGuard gate and never instead of it, so it can
    # only further restrict. The `case` namespace defaults to ALLOW and is
    # clamped to shadow, so an install taking this update sees no change.
    if action.status_effect is not None:
        try:
            from case_service.hxarbiter import pep as _arb_pep

            target = (params.get("status") if action.status_effect == "$param"
                      else action.status_effect)
            statuses_now = await statuses.resolve_set(
                session, tenant_id=tenant_uuid, case_type_id=case.case_type_id)
            target_def = statuses_now.get(str(target))

            if not await _arb_pep.check(
                session,
                tenant_id=str(getattr(case, "tenant_id", None) or "default"),
                action=_arb_pep.ACTION_STATUS_TRANSITION,
                subject_id=str(actor),
                subject_kind=_arb_pep.SubjectKind.USER,
                resource_kind="case",
                resource_id=str(case_id),
                resource_attrs={"case_type": str(case.case_type_id)},
                context={
                    "from_status": str(case.status or ""),
                    "to_status": str(target or ""),
                    # Category and `terminal` are given so a policy can say "no
                    # case may be ENDED here" without having to enumerate a
                    # tenant's own status names — the same reason the category
                    # exists at all.
                    "to_status_category": (target_def.category if target_def else ""),
                    "terminal": bool(target_def and target_def.is_terminal),
                    "action": action_name,
                    "purpose": "status_transition",
                },
                legacy_allowed=True,
                claimed_by=str(getattr(user, "username", None) or actor)[:255],
            ):
                return CaseResult(
                    case_id, False, code="VEL-CASE-ACTION-POLICY-403",
                    error="Policy refuses this status transition for this case.")
        except Exception as _arb_exc:                          # noqa: BLE001
            # Fails OPEN, exactly as the stage PEP beside it does. An engine
            # that is unreachable must not stop a case worker closing a case;
            # the decision log is what records that it could not be consulted.
            log.warning("case action: HxArbiter status PEP unavailable: %s", _arb_exc)

    handler = _HANDLERS[action_name]
    try:
        detail = await handler(session, case, action, params, actor, tenant_uuid)
    except ActionError as exc:
        return CaseResult(case_id, False, code=exc.code, error=str(exc))
    except KeyError as exc:                                     # pragma: no cover
        return CaseResult(case_id, False, code="VEL-CASE-ACTION-PARAM-400",
                          error=f"Missing parameter {exc}.")
    except Exception as exc:                                    # noqa: BLE001
        log.exception("case action %s failed on %s", action_name, case_id)
        return CaseResult(case_id, False, code="VEL-CASE-ACTION-FAILED-500",
                          error=str(exc))

    already_audited = bool(detail.pop("_audited", False))
    if audit is not None and not already_audited:
        try:
            # The pre-198 verb, not the action name — see `audit_action`.
            await audit(session, case_id, action.audit_name, actor_id=actor,
                        details={"action": action_name,
                                 "params": _safe_params(params), **detail})
        except Exception as exc:                                # noqa: BLE001
            log.warning("audit failed for %s on %s: %s", action_name, case_id, exc)

    return CaseResult(case_id, True, detail=detail)


def _safe_params(params: dict[str, Any]) -> dict[str, Any]:
    """Audit records what was asked for, truncated — never a whole note body."""
    out = {}
    for k, v in (params or {}).items():
        s = str(v)
        out[k] = s if len(s) <= 200 else s[:200] + "…"
    return out


def precheck(action_name: str, params: dict[str, Any], *, case_count: int = 1) -> ActionDef:
    """Everything wrong with the REQUEST, before any case is touched.

    Raises rather than returning a result, because none of these are per-case
    outcomes: they are reasons the whole request is malformed, and a bulk run
    that half-applied before discovering one would be the worse answer.
    """
    action = registry.get(action_name)
    if action is None:
        raise ActionError(
            f"Unknown action '{action_name}'. Known: {', '.join(registry.names())}.",
            code="VEL-CASE-ACTION-UNKNOWN-404", status_code=404)

    if case_count > BULK_CEILING:
        raise ActionError(
            f"This request names {case_count} cases; the ceiling is {BULK_CEILING}. "
            f"Narrow it or split it — acting on the first {BULK_CEILING} and "
            f"reporting success is the worse answer.",
            code="VEL-CASE-BULK-CEILING-400")
    if case_count < 1:
        raise ActionError("No cases named.", code="VEL-CASE-BULK-EMPTY-400")

    missing = [p for p in action.required_params if params.get(p) in (None, "")]
    if missing:
        raise ActionError(
            f"'{action.name}' needs: {', '.join(missing)}.",
            code="VEL-CASE-ACTION-PARAM-400")

    if action.destructive and case_count > 1 and not params.get("confirm_destructive"):
        raise ActionError(
            f"'{action.label}' is destructive and this request names {case_count} "
            f"cases. Pass confirm_destructive=true to proceed.",
            code="VEL-CASE-BULK-DESTRUCTIVE-400")

    return action
