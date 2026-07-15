"""Case Costing — rate cards, time rollup, and the replay cost block.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import RateCardModel
from case_service.hxreplay import trace as trace_mod

COST_BASIS = ("manual recorded activity durations × tenant default hourly rate; "
              "automated work costs 0 in this model")


# ── rate cards ──────────────────────────────────────────────────────────────────

async def get_default_rate(session: AsyncSession, tenant_id: str | None) -> RateCardModel | None:
    return (await session.execute(
        select(RateCardModel).where(RateCardModel.tenant_id == tenant_id,
                                    RateCardModel.role == "*")
    )).scalar_one_or_none()


async def upsert_default_rate(session: AsyncSession, tenant_id: str | None,
                              hourly_rate: float, currency: str,
                              actor_id: str | None) -> RateCardModel:
    if hourly_rate < 0:
        raise ValueError("hourly_rate must be >= 0")
    row = await get_default_rate(session, tenant_id)
    if row is None:
        row = RateCardModel(tenant_id=tenant_id, role="*", hourly_rate=float(hourly_rate),
                            currency=currency or "USD", created_by=actor_id)
        session.add(row)
    else:
        row.hourly_rate = float(hourly_rate)
        row.currency = currency or row.currency
        row.updated_at = datetime.now(timezone.utc)
    await session.flush()
    return row


# ── automatic time rollup (zero new capture) ────────────────────────────────────

async def case_time_rollup(session: AsyncSession, case_id: uuid.UUID) -> dict[str, Any]:
    """Recorded time for one case from its event log: total / manual / auto and
    per-stage / per-activity breakdowns."""
    trace = await trace_mod.load_baseline_trace(session, case_id)
    total = manual = auto = 0
    by_stage: dict[str, int] = {}
    by_activity: dict[str, int] = {}
    for e in trace:
        d = int(e.get("duration_seconds") or 0)
        if not d:
            continue
        total += d
        if (e.get("actor_type") or "") in trace_mod._AUTO_ACTOR_TYPES:
            auto += d
        else:
            manual += d
        if e.get("stage_id"):
            by_stage[e["stage_id"]] = by_stage.get(e["stage_id"], 0) + d
        by_activity[e["activity"]] = by_activity.get(e["activity"], 0) + d
    return {"case_id": str(case_id), "event_count": len(trace),
            "total_recorded_seconds": total, "manual_seconds": manual,
            "auto_seconds": auto, "by_stage": by_stage, "by_activity": by_activity}


# ── replay cost block (HxReplay P4) ─────────────────────────────────────────────

def cost_block(outcomes: list[dict[str, Any]], hourly_rate: float | None,
               currency: str = "USD") -> dict[str, Any] | None:
    """Counterfactual cost delta over the DETERMINATE subset only.

    Estimated/indeterminate cases never contribute — a cost figure inherits the
    trust class of the metrics it is derived from.
    """
    if hourly_rate is None:
        return None
    det = [o for o in outcomes if o.get("determinacy") == "determinate"]
    if not det:
        return None

    def _manual(m: dict[str, Any] | None) -> float:
        return float((m or {}).get("manual_seconds") or 0)

    base_s = sum(_manual(o["baseline_metrics"]) for o in det)
    cf_s = sum(_manual(o["counterfactual_metrics"] or o["baseline_metrics"]) for o in det)
    rate = float(hourly_rate)
    return {
        "cases": len(det),
        "hourly_rate": rate,
        "currency": currency,
        "baseline_manual_seconds": base_s,
        "counterfactual_manual_seconds": cf_s,
        "baseline_cost": round(base_s / 3600.0 * rate, 2),
        "counterfactual_cost": round(cf_s / 3600.0 * rate, 2),
        "savings": round((base_s - cf_s) / 3600.0 * rate, 2),
        "basis": COST_BASIS,
    }


# ── §11 P2: manual timers / timesheets (billable effort) ────────────────────────

async def _rate_for(session: AsyncSession, tenant_id: str | None, role: str | None) -> RateCardModel | None:
    """role rate if configured, else the tenant default '*'."""
    if role:
        row = (await session.execute(
            select(RateCardModel).where(RateCardModel.tenant_id == tenant_id,
                                        RateCardModel.role == role)
        )).scalar_one_or_none()
        if row is not None:
            return row
    return await get_default_rate(session, tenant_id)


async def running_timer(session: AsyncSession, case_id: uuid.UUID, user_id: str):
    from case_service.db.models import CaseTimeEntryModel
    return (await session.execute(
        select(CaseTimeEntryModel).where(
            CaseTimeEntryModel.case_id == case_id,
            CaseTimeEntryModel.user_id == user_id,
            CaseTimeEntryModel.source == "timer",
            CaseTimeEntryModel.ended_at.is_(None),
        )
    )).scalars().first()


async def start_timer(session: AsyncSession, *, tenant_id: str | None,
                      case_id: uuid.UUID, user_id: str, role: str | None,
                      note: str | None):
    """One running timer per user per case; returns the new entry."""
    from case_service.db.models import CaseTimeEntryModel
    entry = CaseTimeEntryModel(
        tenant_id=tenant_id, case_id=case_id, user_id=user_id, role=role,
        source="timer", started_at=datetime.now(timezone.utc), note=note,
    )
    session.add(entry)
    await session.flush()
    return entry


def _utc(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:      # SQLite returns tz-naive
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def stop_timer(session: AsyncSession, entry) -> None:
    now = datetime.now(timezone.utc)
    entry.ended_at = now
    started = _utc(entry.started_at) or now
    entry.duration_seconds = max(0, int((now - started).total_seconds()))


async def list_entries(session: AsyncSession, case_id: uuid.UUID) -> list:
    from case_service.db.models import CaseTimeEntryModel
    return list((await session.execute(
        select(CaseTimeEntryModel)
        .where(CaseTimeEntryModel.case_id == case_id)
        .order_by(CaseTimeEntryModel.created_at)
    )).scalars().all())


async def entries_rollup(session: AsyncSession, tenant_id: str | None,
                         case_id: uuid.UUID) -> dict[str, Any]:
    """Billable-effort summary + cost for one case (completed entries only)."""
    entries = await list_entries(session, case_id)
    billable = tracked = 0
    cost = 0.0
    priced = True
    currency = None
    for e in entries:
        if e.source == "timer" and e.ended_at is None:
            continue                              # running timers don't count yet
        d = int(e.duration_seconds or 0)
        tracked += d
        if not e.billable:
            continue
        billable += d
        rate = await _rate_for(session, tenant_id, e.role)
        if rate is None:
            priced = False
            continue
        currency = currency or rate.currency
        cost += d / 3600.0 * float(rate.hourly_rate)
    return {
        "case_id": str(case_id),
        "entry_count": len(entries),
        "tracked_seconds": tracked,
        "billable_seconds": billable,
        "billable_cost": round(cost, 2) if priced else None,
        "currency": currency,
        "fully_priced": priced,
        "basis": "billable time entries × role rate (falls back to tenant default '*'); "
                 "unpriced roles leave billable_cost null",
    }


# ── §11 P3: billing export ──────────────────────────────────────────────────────

async def billing_export(session: AsyncSession, tenant_id: str | None, *,
                         case_type_id: uuid.UUID | None = None,
                         since: datetime | None = None,
                         until: datetime | None = None) -> list[dict[str, Any]]:
    """One line per case with billable entries in the window — invoice input.

    Commercially sensitive (per-case cost): the router gates this behind the
    same HxGuard ``costing.rates`` capability as the rate card.
    """
    from case_service.db.models import CaseInstanceModel, CaseTimeEntryModel
    stmt = select(CaseTimeEntryModel).where(
        CaseTimeEntryModel.tenant_id == tenant_id,
        CaseTimeEntryModel.ended_at.isnot(None) | (CaseTimeEntryModel.source == "timesheet"),
    )
    if since is not None:
        stmt = stmt.where(CaseTimeEntryModel.created_at >= since)
    if until is not None:
        stmt = stmt.where(CaseTimeEntryModel.created_at < until)
    entries = (await session.execute(stmt)).scalars().all()

    by_case: dict[uuid.UUID, list] = {}
    for e in entries:
        if e.source == "timer" and e.ended_at is None:
            continue
        by_case.setdefault(e.case_id, []).append(e)

    lines: list[dict[str, Any]] = []
    for cid, case_entries in by_case.items():
        case = await session.get(CaseInstanceModel, cid)
        if case is None:
            continue
        if case_type_id is not None and case.case_type_id != case_type_id:
            continue
        billable = sum(int(e.duration_seconds or 0) for e in case_entries if e.billable)
        cost = 0.0
        priced = True
        currency = None
        for e in case_entries:
            if not e.billable:
                continue
            rate = await _rate_for(session, tenant_id, e.role)
            if rate is None:
                priced = False
                continue
            currency = currency or rate.currency
            cost += int(e.duration_seconds or 0) / 3600.0 * float(rate.hourly_rate)
        lines.append({
            "case_id": str(cid),
            "case_number": getattr(case, "case_number", None),
            "case_type_id": str(case.case_type_id),
            "entries": len(case_entries),
            "billable_seconds": billable,
            "billable_cost": round(cost, 2) if priced else None,
            "currency": currency,
            "fully_priced": priced,
        })
    lines.sort(key=lambda x: x["case_id"])
    return lines
