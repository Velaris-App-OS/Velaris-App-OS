"""Case Costing API — rate card + automatic time rollup (HxReplay P4 / §11).

Rates are commercially sensitive: gated behind the dedicated HxGuard capability
``costing.rates`` (admin), per-tenant, never reachable by portal identities.
The time rollup for a case inherits that case's view authorization.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from case_service import hxguard
from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser
from case_service.costing import service as costing
from case_service.db import repository as repo
from case_service.db.session import get_session

router = APIRouter(prefix="/costing", tags=["costing"])


def _tenant(user: AuthenticatedUser) -> str:
    return user.tenant_id or "default"


@router.get("/rate-card")
async def get_rate_card(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    await hxguard.require(session, hxguard.subject_from_user(user), "costing.rates")
    row = await costing.get_default_rate(session, _tenant(user))
    if row is None:
        return {"configured": False, "hourly_rate": None, "currency": "USD"}
    return {"configured": True, "hourly_rate": row.hourly_rate, "currency": row.currency,
            "updated_at": (row.updated_at or row.created_at).isoformat()
            if (row.updated_at or row.created_at) else None}


class RateCardBody(BaseModel):
    hourly_rate: float = Field(ge=0, le=1_000_000)
    currency: str = Field(default="USD", min_length=1, max_length=8)


@router.put("/rate-card")
async def put_rate_card(
    body: RateCardBody,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    await hxguard.require(session, hxguard.subject_from_user(user), "costing.rates")
    row = await costing.upsert_default_rate(session, _tenant(user),
                                            body.hourly_rate, body.currency, user.user_id)
    await session.commit()
    return {"configured": True, "hourly_rate": row.hourly_rate, "currency": row.currency}


@router.get("/cases/{case_id}/time")
async def case_time(
    case_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Automatic time rollup from recorded event durations (zero new capture)."""
    try:
        cid = uuid.UUID(case_id)
    except ValueError:
        raise HTTPException(404, "Case not found")
    case = await repo.get_case_instance(session, cid)
    if case is None or (case.tenant_id is not None and case.tenant_id != _tenant(user)):
        raise HTTPException(404, "Case not found")
    await hxguard.require_case(session, user, "case.read", cid)
    return await costing.case_time_rollup(session, cid)


# ── §11 P2: manual timers / timesheets ────────────────────────────────
# Effort capture is case work: start/stop/add require case.update, reading
# requires case.read — same posture as every case action (404 anti-oracle).

async def _authorized_case(session, user, case_id: str, action: str) -> uuid.UUID:
    try:
        cid = uuid.UUID(case_id)
    except ValueError:
        raise HTTPException(404, "Case not found")
    case = await repo.get_case_instance(session, cid)
    if case is None or (case.tenant_id is not None and case.tenant_id != _tenant(user)):
        raise HTTPException(404, "Case not found")
    await hxguard.require_case(session, user, action, cid)
    return cid


def _entry_view(e) -> dict:
    return {
        "id": str(e.id), "case_id": str(e.case_id), "user_id": e.user_id,
        "role": e.role, "source": e.source, "billable": e.billable,
        "running": e.source == "timer" and e.ended_at is None,
        "started_at": e.started_at.isoformat() if e.started_at else None,
        "ended_at": e.ended_at.isoformat() if e.ended_at else None,
        "duration_seconds": e.duration_seconds, "note": e.note,
        "created_at": e.created_at.isoformat() if e.created_at else None,
    }


class TimerStartBody(BaseModel):
    role: str | None = Field(default=None, max_length=100)
    note: str | None = Field(default=None, max_length=2000)


@router.post("/cases/{case_id}/timer/start", status_code=201)
async def start_timer(
    case_id: str,
    body: TimerStartBody | None = None,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    cid = await _authorized_case(session, user, case_id, "case.update")
    if await costing.running_timer(session, cid, str(user.user_id)) is not None:
        raise HTTPException(409, "A timer is already running for you on this case")
    body = body or TimerStartBody()
    entry = await costing.start_timer(
        session, tenant_id=_tenant(user), case_id=cid,
        user_id=str(user.user_id), role=body.role, note=body.note)
    await session.commit()
    await session.refresh(entry)
    return _entry_view(entry)


@router.post("/cases/{case_id}/timer/stop")
async def stop_timer(
    case_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    cid = await _authorized_case(session, user, case_id, "case.update")
    entry = await costing.running_timer(session, cid, str(user.user_id))
    if entry is None:
        raise HTTPException(404, "No running timer for you on this case")
    await costing.stop_timer(session, entry)
    await session.commit()
    await session.refresh(entry)
    return _entry_view(entry)


class TimeEntryBody(BaseModel):
    duration_seconds: int = Field(ge=1, le=24 * 3600)
    role: str | None = Field(default=None, max_length=100)
    note: str | None = Field(default=None, max_length=2000)
    billable: bool = True


@router.post("/cases/{case_id}/time-entries", status_code=201)
async def add_time_entry(
    case_id: str,
    body: TimeEntryBody,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Direct timesheet entry — effort logged after the fact."""
    from case_service.db.models import CaseTimeEntryModel
    cid = await _authorized_case(session, user, case_id, "case.update")
    entry = CaseTimeEntryModel(
        tenant_id=_tenant(user), case_id=cid, user_id=str(user.user_id),
        role=body.role, source="timesheet", duration_seconds=body.duration_seconds,
        billable=body.billable, note=body.note)
    session.add(entry)
    await session.commit()
    await session.refresh(entry)
    return _entry_view(entry)


@router.get("/cases/{case_id}/time-entries")
async def list_time_entries(
    case_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    cid = await _authorized_case(session, user, case_id, "case.read")
    entries = await costing.list_entries(session, cid)
    rollup = await costing.entries_rollup(session, _tenant(user), cid)
    return {"items": [_entry_view(e) for e in entries], "rollup": rollup}


@router.delete("/time-entries/{entry_id}", status_code=204)
async def delete_time_entry(
    entry_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Own entries only (admins included — effort claims belong to their author)."""
    from case_service.db.models import CaseTimeEntryModel
    entry = await session.get(CaseTimeEntryModel, entry_id)
    if entry is None or entry.user_id != str(user.user_id):
        raise HTTPException(404, "Time entry not found")   # anti-oracle
    await _authorized_case(session, user, str(entry.case_id), "case.update")
    await session.delete(entry)
    await session.commit()


# ── §11 P3: billing export ────────────────────────────────────────────

@router.get("/export")
async def export_billing(
    case_type_id: str | None = None,
    days: int = 30,
    fmt: str = "json",
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Per-case billable cost lines for invoicing (HxCheckout / storefront).

    Per-case cost is commercially sensitive — same HxGuard capability as the
    rate card (``costing.rates``), never reachable by portal identities."""
    from datetime import datetime, timedelta, timezone
    await hxguard.require(session, hxguard.subject_from_user(user), "costing.rates")

    ct_id: uuid.UUID | None = None
    if case_type_id:
        try:
            ct_id = uuid.UUID(case_type_id)
        except ValueError:
            raise HTTPException(404, "Case type not found")
    days = max(1, min(days, 366))
    since = datetime.now(timezone.utc) - timedelta(days=days)
    lines = await costing.billing_export(
        session, _tenant(user), case_type_id=ct_id, since=since)

    if fmt == "csv":
        from fastapi.responses import PlainTextResponse
        cols = ["case_id", "case_number", "case_type_id", "entries",
                "billable_seconds", "billable_cost", "currency", "fully_priced"]
        rows = [",".join(cols)] + [
            ",".join("" if l[c] is None else str(l[c]) for c in cols) for l in lines]
        return PlainTextResponse("\n".join(rows) + "\n", media_type="text/csv")
    return {"window_days": days, "lines": lines, "count": len(lines)}
