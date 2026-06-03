"""HxAnalytics — Core platform metrics computed from live case data.

All queries use SQLAlchemy ORM expressions so they run on SQLite (tests)
and PostgreSQL (production) without modification.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, case as sa_case, select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import (
    CaseInstanceModel, CaseTypeModel, CaseSLAInstanceModel,
)


async def platform_snapshot(session: AsyncSession) -> dict[str, Any]:
    """Key platform metrics — the numbers shown on the HxAnalytics dashboard."""

    # Case counts by status
    status_rows = (await session.execute(
        select(CaseInstanceModel.status, func.count().label("n"))
        .group_by(CaseInstanceModel.status)
    )).all()
    by_status = {r.status: r.n for r in status_rows}
    total = sum(by_status.values())
    open_count = sum(v for k, v in by_status.items() if k not in ("resolved", "closed", "cancelled"))

    # Case counts by priority
    prio_rows = (await session.execute(
        select(CaseInstanceModel.priority, func.count().label("n"))
        .group_by(CaseInstanceModel.priority)
    )).all()
    by_priority = {r.priority: r.n for r in prio_rows}

    # Cases created in last 7 / 30 days
    now = datetime.now(timezone.utc)
    last_7  = now - timedelta(days=7)
    last_30 = now - timedelta(days=30)

    new_7 = (await session.execute(
        select(func.count()).select_from(CaseInstanceModel)
        .where(CaseInstanceModel.created_at >= last_7)
    )).scalar_one()

    new_30 = (await session.execute(
        select(func.count()).select_from(CaseInstanceModel)
        .where(CaseInstanceModel.created_at >= last_30)
    )).scalar_one()

    # Average resolution time (seconds) for resolved cases in last 30 days
    avg_resolution = None
    resolved_rows = (await session.execute(
        select(CaseInstanceModel.created_at, CaseInstanceModel.resolved_at)
        .where(
            and_(
                CaseInstanceModel.resolved_at.is_not(None),
                CaseInstanceModel.resolved_at >= last_30,
            )
        )
    )).all()
    if resolved_rows:
        durations = [
            (r.resolved_at - r.created_at).total_seconds()
            for r in resolved_rows
            if r.resolved_at and r.created_at
        ]
        avg_resolution = int(sum(durations) / len(durations)) if durations else None

    # SLA metrics
    sla_rows = (await session.execute(
        select(CaseSLAInstanceModel.status, func.count().label("n"))
        .group_by(CaseSLAInstanceModel.status)
    )).all()
    sla_by_status = {r.status: r.n for r in sla_rows}
    sla_total  = sum(sla_by_status.values())
    sla_breach = sla_by_status.get("breached", 0)
    sla_breach_pct = round(100 * sla_breach / sla_total, 1) if sla_total else 0.0

    # Cases by case type
    type_rows = (await session.execute(
        select(CaseTypeModel.name, func.count(CaseInstanceModel.id).label("n"))
        .join(CaseInstanceModel, CaseInstanceModel.case_type_id == CaseTypeModel.id)
        .group_by(CaseTypeModel.name)
        .order_by(func.count(CaseInstanceModel.id).desc())
        .limit(10)
    )).all()
    by_type = [{"name": r.name, "count": r.n} for r in type_rows]

    return {
        "total_cases":          total,
        "open_cases":           open_count,
        "by_status":            by_status,
        "by_priority":          by_priority,
        "by_type":              by_type,
        "new_last_7_days":      new_7,
        "new_last_30_days":     new_30,
        "avg_resolution_secs":  avg_resolution,
        "avg_resolution_hours": round(avg_resolution / 3600, 1) if avg_resolution else None,
        "sla_total":            sla_total,
        "sla_breached":         sla_breach,
        "sla_breach_pct":       sla_breach_pct,
        "snapshot_at":          now.isoformat(),
    }


async def cases_over_time(
    session: AsyncSession,
    days: int = 30,
    group_by: str = "day",
) -> list[dict]:
    """Cases created per day/week over the last N days."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (await session.execute(
        select(
            func.date(CaseInstanceModel.created_at).label("date"),
            func.count().label("count"),
        )
        .where(CaseInstanceModel.created_at >= since)
        .group_by(func.date(CaseInstanceModel.created_at))
        .order_by(func.date(CaseInstanceModel.created_at))
    )).all()
    return [{"date": str(r.date), "count": r.count} for r in rows]


async def sla_performance(session: AsyncSession, days: int = 30) -> dict:
    """SLA breach rate over time and by case type."""
    since = datetime.now(timezone.utc) - timedelta(days=days)

    rows = (await session.execute(
        select(
            func.date(CaseSLAInstanceModel.started_at).label("date"),
            func.count().label("total"),
            func.sum(
                sa_case((CaseSLAInstanceModel.status == "breached", 1), else_=0)
            ).label("breached"),
        )
        .where(CaseSLAInstanceModel.started_at >= since)
        .group_by(func.date(CaseSLAInstanceModel.started_at))
        .order_by(func.date(CaseSLAInstanceModel.started_at))
    )).all()

    return {
        "series": [
            {
                "date": str(r.date),
                "total": r.total,
                "breached": int(r.breached or 0),
                "breach_pct": round(100 * (r.breached or 0) / r.total, 1) if r.total else 0.0,
            }
            for r in rows
        ],
        "period_days": days,
    }


async def funnel_by_case_type(session: AsyncSession, case_type_id: str) -> dict:
    """Stage funnel: how many cases are at/have passed each stage."""
    rows = (await session.execute(
        select(
            CaseInstanceModel.current_stage_id,
            func.count().label("count"),
        )
        .where(CaseInstanceModel.case_type_id == case_type_id)
        .group_by(CaseInstanceModel.current_stage_id)
        .order_by(func.count().desc())
    )).all()

    return {
        "case_type_id": case_type_id,
        "stages": [
            {"stage_id": r.current_stage_id or "none", "count": r.count}
            for r in rows
        ],
    }
