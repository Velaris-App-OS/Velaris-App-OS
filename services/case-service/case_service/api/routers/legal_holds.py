"""Legal holds — the routes. Rules and the list of what a hold freezes:
`case_service/legal_hold/service.py`.

    GET  /legal-holds                      the caller's tenant's holds
    POST /legal-holds                      place one matter's hold on 1–500 cases
    GET  /legal-holds/cases/{case_id}      is this case held, and what that freezes
    POST /legal-holds/matters/release      release every active hold of one matter
    POST /legal-holds/{hold_id}/release    release one hold (or ask, see below)
    POST /legal-holds/{hold_id}/release/approve   approve an asked-for release
    POST /legal-holds/{hold_id}/release/reject    refuse, or withdraw, one
    GET  /legal-holds/settings             the approving group, and the choices
    PUT  /legal-holds/settings             name or clear the approving group

**Release approval (mig 218).** When the hold's tenant names an approving
access group, a release is ASKED for (202, `pending: true`) and happens only
when a member of that group other than the asker approves. No group named =
released at once, as before. Rules: `case_service/legal_hold/approval.py`.

**Three privileges, not one.** Reading names a dispute; placing protects a
record; releasing is what lets it be destroyed again and is the narrowest.

**Anyone who can read the case learns that it is held** — a case worker whose
Delete is refused is owed the reason. The matter, the stated reason and who
placed it are shown only to holders of `legal.holds.read`. The customer portal
never mentions a hold at all.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared import hxguard
from velaris_shared.auth.dependencies import get_current_user
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.core.error_codes import coded
from velaris_shared.db.models import CaseInstanceModel, LegalHoldModel
from velaris_shared.db.session import get_session

from case_service.legal_hold import approval as la
from case_service.legal_hold import service as lh
from velaris_shared.tenancy.identity import (
    caller_tenant_uuid, is_platform_operator, may_reach_tenant, scope_to_caller,
)

router = APIRouter(prefix="/legal-holds", tags=["legal-holds"])

#: One request, one matter, at most this many cases — the bulk ceiling the case
#: action dispatcher uses.
MAX_CASES = 500


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlaceIn(_Strict):
    case_ids: list[uuid.UUID] = Field(min_length=1, max_length=MAX_CASES)
    matter_ref: str = Field(min_length=1, max_length=lh.MATTER_MAX)
    reason: str = Field(min_length=lh.REASON_MIN, max_length=lh.REASON_MAX)


class ReleaseIn(_Strict):
    reason: str = Field(min_length=lh.REASON_MIN, max_length=lh.REASON_MAX)


class ReleaseMatterIn(ReleaseIn):
    matter_ref: str = Field(min_length=1, max_length=lh.MATTER_MAX)


class ApproveIn(_Strict):
    note: str | None = Field(None, max_length=la.NOTE_MAX)


class RejectIn(_Strict):
    note: str = Field(min_length=lh.REASON_MIN, max_length=la.NOTE_MAX)


class SettingsIn(_Strict):
    approver_group_id: uuid.UUID | None = None


def _raise(exc: lh.LegalHoldError):
    raise coded(exc.status_code, exc.code, str(exc))


@router.get("")
async def list_holds(
    status: Literal["active", "released"] | None = None,
    matter_ref: str | None = Query(None, max_length=lh.MATTER_MAX),
    case_id: uuid.UUID | None = None,
    limit: int = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard(lh.PRIV_READ)),
) -> dict[str, Any]:
    q = select(LegalHoldModel).order_by(LegalHoldModel.placed_at.desc()).limit(limit)
    if status:
        q = q.where(LegalHoldModel.status == status)
    if matter_ref:
        q = q.where(LegalHoldModel.matter_ref == lh.normalise_matter(matter_ref))
    if case_id:
        q = q.where(LegalHoldModel.case_id == case_id)
    q = await scope_to_caller(q, LegalHoldModel, session, user)
    rows = (await session.execute(q)).scalars().all()
    return {"holds": await _with_approval(session, user, rows), "count": len(rows),
            "frozen": list(lh.FROZEN),
            # So the screen offers Release only to whoever may — asked of the
            # server, never inferred from a role name.
            "can_release": hxguard.user_allows(user, lh.PRIV_RELEASE)}


async def _with_approval(session: AsyncSession, user: AuthenticatedUser,
                         rows) -> list[dict[str, Any]]:
    """Each hold, plus whether releasing it needs approval, the open request,
    and whether THIS caller may approve it — decided here, not by the screen."""
    pending = await la.pending_for(session, [r.id for r in rows])
    groups: dict[Any, Any] = {}
    out = []
    for r in rows:
        if r.tenant_id not in groups:
            groups[r.tenant_id] = await la.approver_group_id(session, r.tenant_id)
        group = groups[r.tenant_id]
        req = pending.get(r.id)
        d = lh.as_dict(r)
        d["approval_required"] = group is not None
        d["pending_release"] = la.request_dict(req)
        d["can_approve"] = bool(
            req and not la._same_actor(user.user_id, req.requested_by)
            and await la.is_member(session, group, user.user_id))
        d["can_withdraw"] = bool(req and la._same_actor(user.user_id, req.requested_by))
        out.append(d)
    return out


@router.post("", status_code=201)
async def place_holds(
    body: PlaceIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard(lh.PRIV_PLACE)),
) -> dict[str, Any]:
    """Place one matter's hold on each named case. Reported case by case.

    A case outside the caller's tenant is refused as not found — never a reason
    to learn which of the others exist. A case with no tenant belongs to the
    platform, so only the platform operator may freeze it.
    """
    tenant = await caller_tenant_uuid(session, user)
    operator = await is_platform_operator(session, user)
    results: list[dict[str, Any]] = []
    placed = 0
    for cid in dict.fromkeys(body.case_ids):          # de-duplicated, order kept
        item: dict[str, Any] = {"case_id": str(cid)}
        try:
            # The TENANT boundary, not a relationship to the case: a compliance
            # officer freezes cases they have never worked, so `case.read`
            # (assignee, editor, manager) is the wrong question here. What must
            # hold is that the case is in the caller's own tenant.
            case = await session.get(CaseInstanceModel, cid)
            if (case is None or (case.tenant_id is None and not operator)
                    or not await may_reach_tenant(session, user, case.tenant_id)):
                raise coded(404, lh.CODE_NOTFOUND, "Case not found")
            row = await lh.place(session, case=case, matter_ref=body.matter_ref,
                                 reason=body.reason, actor=str(user.user_id),
                                 tenant_uuid=tenant)
            item.update(ok=True, hold=lh.as_dict(row))
            placed += 1
        except lh.LegalHoldError as exc:
            if exc.status_code == 400:
                _raise(exc)                            # the request itself is wrong
            item.update(ok=False, code=exc.code, error=str(exc))
        except Exception as exc:                       # noqa: BLE001
            code = getattr(exc, "status_code", None)
            if code not in (403, 404):
                raise
            # 403 and 404 both read as "not found": a case in another tenant
            # must be indistinguishable from one that does not exist.
            item.update(ok=False, code=lh.CODE_NOTFOUND, error="Case not found")
        results.append(item)
    await session.commit()
    return {"placed": placed, "requested": len(results), "results": results}


@router.get("/cases/{case_id}")
async def case_hold_status(
    case_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Whether the case is held. Details only for `legal.holds.read`."""
    await hxguard.require_case(session, user, "case.read", case_id)
    held = await lh.is_held(session, case_id)
    out: dict[str, Any] = {"case_id": str(case_id), "held": held,
                           "frozen": list(lh.FROZEN) if held else []}
    may_read = hxguard.user_allows(user, lh.PRIV_READ)
    out["can_read"] = may_read
    out["can_place"] = hxguard.user_allows(user, lh.PRIV_PLACE)
    out["can_release"] = hxguard.user_allows(user, lh.PRIV_RELEASE)
    if may_read and await lh.available(session):
        rows = (await session.execute(
            select(LegalHoldModel).where(LegalHoldModel.case_id == case_id)
            .order_by(LegalHoldModel.placed_at.desc()))).scalars().all()
        mine = [r for r in rows if await may_reach_tenant(session, user, r.tenant_id)]
        out["holds"] = await _with_approval(session, user, mine)
        # A shared case may be held by a tenant this caller cannot see into.
        out["other_active_holds"] = sum(
            1 for r in rows if r.status == lh.ACTIVE and r not in mine)
    return out


@router.post("/matters/release")
async def release_matter(
    body: ReleaseMatterIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard(lh.PRIV_RELEASE)),
) -> dict[str, Any]:
    """Release every active hold of one matter in the caller's tenant."""
    q = select(LegalHoldModel).where(
        LegalHoldModel.status == lh.ACTIVE,
        LegalHoldModel.matter_ref == lh.normalise_matter(body.matter_ref))
    q = await scope_to_caller(q, LegalHoldModel, session, user)
    rows = (await session.execute(q)).scalars().all()
    if not rows:
        raise coded(404, lh.CODE_NOTFOUND, "No active hold for that matter.")
    released, requested = [], []
    try:
        for row in rows:
            if await _release_or_ask(session, user, row, body.reason, skip_pending=True):
                released.append(row)
            else:
                requested.append(row)
    except lh.LegalHoldError as exc:
        _raise(exc)
    await session.commit()
    return {"released": len(released), "pending_approval": len(requested),
            "matter_ref": rows[0].matter_ref,
            "case_ids": [str(r.case_id) for r in released],
            "pending_case_ids": [str(r.case_id) for r in requested]}


async def _release_or_ask(session: AsyncSession, user: AuthenticatedUser,
                          row: LegalHoldModel, reason: str, *,
                          skip_pending: bool = False) -> bool:
    """Release now (True), or open a request for approval (False).

    With `skip_pending` (a whole matter), a hold already waiting for approval
    counts as asked-for: one hold asked about earlier must not fail the rest.
    One hold asked twice is still a 409 — the person should know."""
    group = await la.approver_group_id(session, row.tenant_id)
    if skip_pending and group is not None and (await la.pending_for(session, [row.id])):
        return False
    if group is None:
        await lh.release(session, hold=row, reason=reason, actor=str(user.user_id))
        await la.supersede_pending(session, row.id, str(user.user_id))
        return True
    await la.request_release(session, hold=row, reason=reason, actor=str(user.user_id),
                             group_id=group)
    return False


async def _reachable_hold(session: AsyncSession, user: AuthenticatedUser,
                          hold_id: uuid.UUID) -> LegalHoldModel:
    row = await session.get(LegalHoldModel, hold_id)
    if row is None or not await may_reach_tenant(session, user, row.tenant_id):
        raise coded(404, lh.CODE_NOTFOUND, "No such legal hold.")
    return row


@router.post("/{hold_id}/release")
async def release_hold(
    hold_id: uuid.UUID,
    body: ReleaseIn,
    response: Response,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard(lh.PRIV_RELEASE)),
) -> dict[str, Any]:
    row = await session.get(LegalHoldModel, hold_id)
    # The path guard refuses another tenant's hold already; checked here as
    # well, because MCP tools call handlers directly.
    if row is None or not await may_reach_tenant(session, user, row.tenant_id):
        raise coded(404, lh.CODE_NOTFOUND, "No such legal hold.")
    try:
        released = await _release_or_ask(session, user, row, body.reason)
    except lh.LegalHoldError as exc:
        _raise(exc)
    await session.commit()
    if not released:
        # Asked for, not done: the hold stands until the group approves.
        response.status_code = 202
    return {"hold": lh.as_dict(row), "pending": not released,
            "pending_release": la.request_dict(
                (await la.pending_for(session, [row.id])).get(row.id)),
            "case_still_held": await lh.is_held(session, row.case_id)}


@router.post("/{hold_id}/release/approve")
async def approve_release(
    hold_id: uuid.UUID,
    body: ApproveIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Approve a release someone else asked for, and release the hold.

    Not a privilege gate: the approver is whoever the tenant's setting names —
    an active member of the approving group — and never the person who asked.
    """
    row = await _reachable_hold(session, user, hold_id)
    try:
        req = await la.approve(session, hold=row, actor=str(user.user_id), note=body.note)
    except lh.LegalHoldError as exc:
        _raise(exc)
    await session.commit()
    return {"hold": lh.as_dict(row), "request": la.request_dict(req),
            "case_still_held": await lh.is_held(session, row.case_id)}


@router.post("/{hold_id}/release/reject")
async def reject_release(
    hold_id: uuid.UUID,
    body: RejectIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Refuse a release (an approver) or withdraw it (the person who asked)."""
    row = await _reachable_hold(session, user, hold_id)
    try:
        req = await la.reject(session, hold=row, actor=str(user.user_id), note=body.note)
    except lh.LegalHoldError as exc:
        _raise(exc)
    await session.commit()
    return {"hold": lh.as_dict(row), "request": la.request_dict(req)}


# ── settings: which group approves ────────────────────────────────────────────

async def _settings_tenant(session: AsyncSession, user: AuthenticatedUser,
                           tenant_id: uuid.UUID | None = None) -> uuid.UUID:
    """The caller's own tenant. Only the superadmin may name another — the
    break-glass for a group that is gone, which no member is left to re-point."""
    from velaris_shared.tenancy.identity import is_superadmin
    if tenant_id is not None:
        from velaris_shared.db.models import TenantModel
        if not is_superadmin(user) or await session.get(TenantModel, tenant_id) is None:
            raise coded(404, lh.CODE_NOTFOUND, "No such tenant.")
        return tenant_id
    tenant = await caller_tenant_uuid(session, user)
    if tenant is None:
        raise coded(400, la.CODE_GROUP, "Your account has no tenant to configure.")
    return tenant


async def _tenant_groups(session: AsyncSession, tenant: uuid.UUID) -> list[dict[str, Any]]:
    from sqlalchemy import func
    from velaris_shared.db.models import AccessGroupModel, OperatorAccessGroupModel
    from velaris_shared.tenancy.identity import same_tenant

    groups = (await session.execute(
        select(AccessGroupModel).order_by(AccessGroupModel.name))).scalars().all()
    out = []
    for g in groups:
        if not await same_tenant(session, g.tenant_id, tenant):
            continue
        members = (await session.execute(
            select(func.count()).select_from(OperatorAccessGroupModel)
            .where(OperatorAccessGroupModel.access_group_id == g.id))).scalar() or 0
        out.append({"id": str(g.id), "name": g.name, "is_active": bool(g.is_active),
                    "members": int(members)})
    return out


@router.get("/settings")
async def get_settings(
    tenant_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard(lh.PRIV_READ)),
) -> dict[str, Any]:
    tenant = await _settings_tenant(session, user, tenant_id)
    group_id = await la.approver_group_id(session, tenant)
    can_configure = hxguard.user_allows(user, la.PRIV_CONFIGURE)
    groups = await _tenant_groups(session, tenant)
    named = next((g for g in groups if g["id"] == str(group_id)), None) if group_id else None
    from velaris_shared.tenancy.identity import is_superadmin
    return {
        "available": await la.available(session),
        "approver_group_id": str(group_id) if group_id else None,
        # Named but gone or inactive: releases wait. Said, not hidden.
        "approver_group": named,
        "approver_group_unusable": bool(group_id and not (named and named["is_active"])),
        "can_configure": can_configure,
        # Changing a group already named needs membership of it (or superadmin).
        "can_change": can_configure and (
            group_id is None or is_superadmin(user)
            or await la.is_member(session, group_id, user.user_id)),
        "groups": groups if can_configure else [],
    }


@router.put("/settings")
async def put_settings(
    body: SettingsIn,
    tenant_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard(la.PRIV_CONFIGURE)),
) -> dict[str, Any]:
    from velaris_shared.tenancy.identity import is_superadmin, same_tenant
    from velaris_shared.db.models import AccessGroupModel

    tenant = await _settings_tenant(session, user, tenant_id)
    if body.approver_group_id is not None:
        g = await session.get(AccessGroupModel, body.approver_group_id)
        # Another tenant's group reads as missing, never as "exists elsewhere".
        if g is None or not await same_tenant(session, g.tenant_id, tenant):
            raise coded(400, la.CODE_GROUP, "No such access group in your tenant.")
    try:
        await la.set_approver_group(
            session, tenant_id=tenant, group_id=body.approver_group_id,
            actor=str(user.user_id), actor_is_superadmin=is_superadmin(user))
    except lh.LegalHoldError as exc:
        _raise(exc)
    await session.commit()
    return await get_settings(tenant_id=tenant_id, session=session, user=user)
