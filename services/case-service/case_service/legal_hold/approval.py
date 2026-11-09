"""Legal hold release — the optional second person (mig 218, 2026-10-14).

A tenant may name an access group — its compliance team — in
`legal_hold_settings`. Then releasing a hold is two steps:

  1. a holder of `legal.holds.release` asks, with the reason;
  2. an active member of that group who is NOT the person who asked approves,
     and only then is the hold released.

With no group named (every tenant on upgrade, and every hold with no tenant)
nothing changes: one holder of `legal.holds.release` releases at once.

**The rule must not be switch-off-able by the person it binds.** Naming a group
where none is set needs `legal.holds.configure`. Changing or clearing a group
already set ALSO needs membership of that group, or the platform superadmin —
otherwise an admin facing the rule would clear it and release alone, which is
the two-call defeat the scheduler's two-person rule had (2026-08-20).

**Fail closed.** A named group that is deleted or inactive is not "no rule": a
release waits, and the settings view says the group is unusable. Only a member
of it (none, if deleted) or the superadmin can re-point it.

Residual, stated plainly: an operator who holds `identity.groups.write` can add
themselves, or a second account, to the group. Both are audited group writes;
nothing in this module can tell a colleague from a second account.

**Missing tables** (code ahead of migration 218) read as "no group named" —
exactly the behaviour before 218 — never as an error.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import (
    AccessGroupModel, LegalHoldModel, LegalHoldReleaseRequestModel,
    LegalHoldSettingModel, OperatorAccessGroupModel,
)

from case_service.legal_hold import service as lh

log = logging.getLogger(__name__)

PRIV_CONFIGURE = "legal.holds.configure"

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
WITHDRAWN = "withdrawn"
SUPERSEDED = "superseded"

CODE_SELF = "VEL-LEGALHOLD-SELF-APPROVAL-403"
CODE_NOT_APPROVER = "VEL-LEGALHOLD-NOT-APPROVER-403"
CODE_NO_REQUEST = "VEL-LEGALHOLD-NO-REQUEST-404"
CODE_GROUP = "VEL-LEGALHOLD-GROUP-400"
CODE_LOCKED = "VEL-LEGALHOLD-SETTING-LOCKED-403"

NOTE_MAX = 4000

_tables_seen = False
_missing_logged = False


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _same_actor(a: Any, b: Any) -> bool:
    return str(a or "").strip().lower() == str(b or "").strip().lower() != ""


async def available(session: AsyncSession) -> bool:
    """False only when migration 218's tables do not exist yet."""
    global _tables_seen, _missing_logged  # noqa: PLW0603
    if _tables_seen:
        return True
    try:
        async with session.begin_nested():
            await session.execute(select(LegalHoldSettingModel.tenant_id).limit(1))
            await session.execute(select(LegalHoldReleaseRequestModel.id).limit(1))
    except DBAPIError as exc:
        text = str(exc).lower()
        if "legal_hold_" in text and any(
                s in text for s in ("does not exist", "doesn't exist", "no such table")):
            if not _missing_logged:
                _missing_logged = True
                log.warning("legal hold approval tables missing (migration 218 not applied): "
                            "releases need no second person")
            return False
        raise
    _tables_seen = True
    return True


def _reset_cache_for_tests() -> None:
    global _tables_seen  # noqa: PLW0603
    _tables_seen = False


# ── the setting ──────────────────────────────────────────────────────────────

async def approver_group_id(session: AsyncSession, tenant_id: Any) -> uuid.UUID | None:
    """The group whose member must approve a release in this tenant, or None."""
    tid = lh._as_uuid(tenant_id)
    if tid is None or not await available(session):
        return None
    row = await session.get(LegalHoldSettingModel, tid)
    return row.approver_group_id if row else None


async def group_usable(session: AsyncSession, group_id: uuid.UUID | None) -> bool:
    if group_id is None:
        return False
    g = await session.get(AccessGroupModel, group_id)
    return bool(g and g.is_active)


async def is_member(session: AsyncSession, group_id: uuid.UUID | None, user_id: Any) -> bool:
    """An ACTIVE member of an ACTIVE group. Membership, not the active context:
    an approver in several groups need not switch into this one first."""
    if group_id is None or not user_id:
        return False
    row = (await session.execute(
        select(OperatorAccessGroupModel.id)
        .join(AccessGroupModel, AccessGroupModel.id == OperatorAccessGroupModel.access_group_id)
        .where(OperatorAccessGroupModel.access_group_id == group_id,
               OperatorAccessGroupModel.operator_id == str(user_id),
               AccessGroupModel.is_active == True)  # noqa: E712
        .limit(1))).first()
    return row is not None


async def set_approver_group(
    session: AsyncSession, *, tenant_id: uuid.UUID, group_id: uuid.UUID | None,
    actor: str, actor_is_superadmin: bool,
) -> LegalHoldSettingModel | None:
    """Name (or clear) the approving group. The caller has checked
    `legal.holds.configure` and that `group_id` belongs to this tenant."""
    if not await available(session):
        raise lh.LegalHoldError(
            "Release approval needs migration 218, which is not applied on this server.",
            code=lh.CODE_STATE, status_code=409)
    row = await session.get(LegalHoldSettingModel, tenant_id)
    current = row.approver_group_id if row else None
    if current is not None and current != group_id:
        if not (actor_is_superadmin or await is_member(session, current, actor)):
            raise lh.LegalHoldError(
                "Only a member of the current approving group, or the platform superadmin, "
                "can change or remove it.", code=CODE_LOCKED, status_code=403)
    if group_id is not None and not await group_usable(session, group_id):
        raise lh.LegalHoldError("That access group does not exist or is not active.",
                                code=CODE_GROUP, status_code=400)
    if row is None:
        row = LegalHoldSettingModel(tenant_id=tenant_id, approver_group_id=group_id,
                                    updated_by=str(actor), updated_at=_utcnow())
        try:
            async with session.begin_nested():
                session.add(row)
                await session.flush()
        except IntegrityError:
            # Two first-time saves at once: the other one won. Re-read and decide
            # again rather than overwrite a group somebody just named.
            raise lh.LegalHoldError("The setting was changed at the same moment. Reload and "
                                    "try again.", code=lh.CODE_STATE) from None
    else:
        row.approver_group_id = group_id
        row.updated_by = str(actor)
        row.updated_at = _utcnow()
        session.add(row)
        await session.flush()

    from velaris_shared.db import security_events
    await security_events.log_security_event(
        session, event_type="legal_hold.approval_setting_changed", severity="warning",
        user_id=str(actor), resource_type="tenant", resource_id=str(tenant_id),
        action="configure", outcome="success",
        details={"before": str(current) if current else None,
                 "after": str(group_id) if group_id else None,
                 "superadmin_override": bool(actor_is_superadmin and current is not None
                                             and not await is_member(session, current, actor))})
    return row


# ── requests ─────────────────────────────────────────────────────────────────

async def pending_for(session: AsyncSession, hold_ids: Iterable[Any]
                      ) -> dict[uuid.UUID, LegalHoldReleaseRequestModel]:
    ids = [i for i in {lh._as_uuid(h) for h in hold_ids} if i is not None]
    if not ids or not await available(session):
        return {}
    rows = (await session.execute(select(LegalHoldReleaseRequestModel).where(
        LegalHoldReleaseRequestModel.hold_id.in_(ids),
        LegalHoldReleaseRequestModel.status == PENDING))).scalars().all()
    return {r.hold_id: r for r in rows}


async def _audit(session: AsyncSession, hold: LegalHoldModel, *, action: str, actor: str,
                 req: LegalHoldReleaseRequestModel) -> None:
    """Case timeline + security log, like placing and releasing. No reason text."""
    from velaris_shared.db import repository as repo
    from velaris_shared.db import security_events
    from velaris_shared.db.models import CaseInstanceModel

    details = {"hold_id": str(hold.id), "matter_ref": hold.matter_ref,
               "request_id": str(req.id), "requested_by": req.requested_by}
    exists = (await session.execute(select(CaseInstanceModel.id).where(
        CaseInstanceModel.id == hold.case_id))).first() is not None
    if exists:
        await repo.append_audit_entry(session, data={
            "case_id": hold.case_id, "action": f"legal_hold_release_{action}",
            "actor_id": actor, "actor_type": "user", "details": details})
    await security_events.log_security_event(
        session, event_type=f"legal_hold.release_{action}", severity="warning",
        user_id=actor, resource_type="case", resource_id=str(hold.case_id),
        action=f"release_{action}", outcome="success", details=details)


async def request_release(session: AsyncSession, *, hold: LegalHoldModel, reason: str,
                          actor: str, group_id: uuid.UUID) -> LegalHoldReleaseRequestModel:
    _, why = lh._clean(hold.matter_ref, reason)
    if hold.status != lh.ACTIVE:
        raise lh.LegalHoldError("This hold has already been released.", code=lh.CODE_STATE)
    if (await pending_for(session, [hold.id])):
        raise lh.LegalHoldError("A release of this hold is already waiting for approval.",
                                code=lh.CODE_STATE)
    req = LegalHoldReleaseRequestModel(
        id=uuid.uuid4(), hold_id=hold.id, tenant_id=hold.tenant_id, status=PENDING,
        pending_key=str(hold.id), reason=why, requested_by=str(actor),
        requested_at=_utcnow(), approver_group_id=group_id)
    try:
        async with session.begin_nested():
            session.add(req)
            await session.flush()
    except IntegrityError:
        raise lh.LegalHoldError("A release of this hold is already waiting for approval.",
                                code=lh.CODE_STATE) from None
    await _audit(session, hold, action="requested", actor=str(actor), req=req)
    return req


async def _close(session: AsyncSession, req: LegalHoldReleaseRequestModel, status: str,
                 actor: str, note: str | None) -> None:
    """Decide the request — ATOMICALLY. Only one caller can move it out of
    `pending`: two approvers clicking at once would otherwise both pass the
    checks above and release the hold twice, or an approval and a refusal
    would both be recorded. The conditional UPDATE is the lock."""
    values = {"status": status, "pending_key": None, "decided_by": str(actor),
              "decided_at": _utcnow(),
              "decision_note": (note or "").strip()[:NOTE_MAX] or None}
    result = await session.execute(
        update(LegalHoldReleaseRequestModel)
        .where(LegalHoldReleaseRequestModel.id == req.id,
               LegalHoldReleaseRequestModel.status == PENDING)
        .values(**values)
        .execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise lh.LegalHoldError("Somebody else has already decided this release request.",
                                code=lh.CODE_STATE)
    for k, v in values.items():
        setattr(req, k, v)


async def approve(session: AsyncSession, *, hold: LegalHoldModel, actor: str,
                  note: str | None = None) -> LegalHoldReleaseRequestModel:
    """Approve the open request and release the hold, in the caller's transaction."""
    req = (await pending_for(session, [hold.id])).get(hold.id)
    if req is None:
        raise lh.LegalHoldError("There is no release waiting for approval on this hold.",
                                code=CODE_NO_REQUEST, status_code=404)
    if _same_actor(actor, req.requested_by):
        raise lh.LegalHoldError("The person who asked for a release cannot approve it.",
                                code=CODE_SELF, status_code=403)
    # The group named NOW decides. A request made under a group since replaced
    # is approved by the new one — never by a group the tenant has dropped.
    group = await approver_group_id(session, hold.tenant_id)
    if not await is_member(session, group, actor):
        raise lh.LegalHoldError(
            "Only an active member of this tenant's approving group can approve a release.",
            code=CODE_NOT_APPROVER, status_code=403)
    await _close(session, req, APPROVED, actor, note)
    await _audit(session, hold, action="approved", actor=str(actor), req=req)
    # Released in the REQUESTER's name, with the reason they gave; the approval
    # row records who agreed.
    await lh.release(session, hold=hold, reason=req.reason, actor=req.requested_by)
    return req


async def reject(session: AsyncSession, *, hold: LegalHoldModel, actor: str,
                 note: str) -> LegalHoldReleaseRequestModel:
    """Refuse (an approver) or withdraw (the requester) the open request."""
    req = (await pending_for(session, [hold.id])).get(hold.id)
    if req is None:
        raise lh.LegalHoldError("There is no release waiting for approval on this hold.",
                                code=CODE_NO_REQUEST, status_code=404)
    why = str(note or "").strip()
    if not lh.REASON_MIN <= len(why) <= NOTE_MAX:
        raise lh.LegalHoldError(
            f"Give a reason of {lh.REASON_MIN}–{NOTE_MAX} characters.",
            code=lh.CODE_INVALID, status_code=400)
    if _same_actor(actor, req.requested_by):
        status = WITHDRAWN
    elif await is_member(session, await approver_group_id(session, hold.tenant_id), actor):
        status = REJECTED
    else:
        raise lh.LegalHoldError(
            "Only the person who asked, or a member of the approving group, can turn a "
            "release request down.", code=CODE_NOT_APPROVER, status_code=403)
    await _close(session, req, status, actor, why)
    await _audit(session, hold, action=status, actor=str(actor), req=req)
    return req


async def supersede_pending(session: AsyncSession, hold_id: Any, actor: str) -> None:
    """A hold released directly (no group named any more) closes its open request."""
    req = (await pending_for(session, [hold_id])).get(lh._as_uuid(hold_id))
    if req is not None:
        await _close(session, req, SUPERSEDED, actor,
                     "Released directly: no approving group is named.")


def request_dict(req: LegalHoldReleaseRequestModel | None) -> dict[str, Any] | None:
    if req is None:
        return None
    return {
        "id": str(req.id), "status": req.status, "reason": req.reason,
        "requested_by": req.requested_by,
        "requested_at": req.requested_at.isoformat() if req.requested_at else None,
    }
