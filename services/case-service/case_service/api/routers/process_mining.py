"""Process Mining API.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.auth.dependencies import require_role
from case_service.db.session import get_session
from case_service.process_mining import analyzer, event_logger

router = APIRouter(prefix="/process-mining", tags=["process-mining"], dependencies=[Depends(require_role("designer", "admin"))])


class EventLogEntry(BaseModel):
    case_id: str
    activity: str
    activity_type: str
    timestamp: str
    actor_id: str | None = None
    duration_seconds: int | None = None
    outcome: str | None = None


@router.get("/activity-stats")
async def get_activity_stats(
    case_type_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    days: int = Query(30, ge=1, le=365),
    session: AsyncSession = Depends(get_session),
):
    """Activity frequency and duration statistics."""
    return await analyzer.activity_stats(session, case_type_id, days, tenant_id=tenant_id)


@router.get("/bottlenecks")
async def get_bottlenecks(
    case_type_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(10, ge=1, le=50),
    session: AsyncSession = Depends(get_session),
):
    """Activities with longest average duration."""
    return await analyzer.find_bottlenecks(session, case_type_id, days, limit, tenant_id=tenant_id)


@router.get("/variants")
async def get_variants(
    case_type_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
):
    """Unique paths through the process."""
    return await analyzer.discover_variants(session, case_type_id, days, limit, tenant_id=tenant_id)


@router.get("/flow-graph")
async def get_flow_graph(
    case_type_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    days: int = Query(30, ge=1, le=365),
    session: AsyncSession = Depends(get_session),
):
    """Directly-follows graph — shows activity transitions."""
    return await analyzer.build_dfg(session, case_type_id, days, tenant_id=tenant_id)


@router.get("/duration-stats")
async def get_duration_stats(
    case_type_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    days: int = Query(30, ge=1, le=365),
    session: AsyncSession = Depends(get_session),
):
    """Case duration statistics (mean, median, percentiles)."""
    return await analyzer.case_duration_stats(session, case_type_id, days, tenant_id=tenant_id)


@router.get("/conformance/{case_type_id}")
async def check_conformance(
    case_type_id: uuid.UUID,
    days: int = Query(30, ge=1, le=365),
    session: AsyncSession = Depends(get_session),
):
    """Compare actual flow vs planned case type definition."""
    result = await analyzer.check_conformance(session, case_type_id, days)
    if "error" in result:
        raise HTTPException(404, result["error"])
    return result


@router.get("/events")
async def list_events(
    case_id: uuid.UUID | None = None,
    case_type_id: uuid.UUID | None = None,
    limit: int = Query(100, ge=1, le=1000),
    session: AsyncSession = Depends(get_session),
):
    """Raw event log."""
    from case_service.db.models import CaseEventLogModel
    stmt = select(CaseEventLogModel).order_by(CaseEventLogModel.timestamp.desc()).limit(limit)
    if case_id:
        stmt = stmt.where(CaseEventLogModel.case_id == case_id)
    if case_type_id:
        stmt = stmt.where(CaseEventLogModel.case_type_id == case_type_id)

    result = await session.execute(stmt)
    return [
        {
            "id": str(e.id),
            "case_id": str(e.case_id),
            "case_type_id": str(e.case_type_id),
            "activity": e.activity,
            "activity_type": e.activity_type,
            "stage_id": e.stage_id,
            "step_id": e.step_id,
            "actor_id": e.actor_id,
            "timestamp": e.timestamp.isoformat() if e.timestamp else None,
            "duration_seconds": e.duration_seconds,
            "outcome": e.outcome,
        }
        for e in result.scalars().all()
    ]


@router.get("/summary")
async def get_summary(
    case_type_id: uuid.UUID | None = None,
    days: int = Query(30, ge=1, le=365),
    session: AsyncSession = Depends(get_session),
):
    """High-level process mining summary."""
    from case_service.db.models import CaseEventLogModel
    from datetime import datetime, timedelta, timezone
    since = datetime.now(timezone.utc) - timedelta(days=days)

    # Total events
    stmt = select(func.count()).select_from(CaseEventLogModel).where(
        CaseEventLogModel.timestamp >= since,
    )
    if case_type_id:
        stmt = stmt.where(CaseEventLogModel.case_type_id == case_type_id)
    total_events = (await session.execute(stmt)).scalar_one()

    # Distinct cases
    cases_stmt = select(func.count(func.distinct(CaseEventLogModel.case_id))).where(
        CaseEventLogModel.timestamp >= since,
    )
    if case_type_id:
        cases_stmt = cases_stmt.where(CaseEventLogModel.case_type_id == case_type_id)
    distinct_cases = (await session.execute(cases_stmt)).scalar_one()

    # Distinct activities
    act_stmt = select(func.count(func.distinct(CaseEventLogModel.activity))).where(
        CaseEventLogModel.timestamp >= since,
    )
    if case_type_id:
        act_stmt = act_stmt.where(CaseEventLogModel.case_type_id == case_type_id)
    distinct_activities = (await session.execute(act_stmt)).scalar_one()

    # Duration stats
    duration = await analyzer.case_duration_stats(session, case_type_id, days)

    return {
        "total_events": total_events,
        "distinct_cases": distinct_cases,
        "distinct_activities": distinct_activities,
        "duration_stats": duration,
        "period_days": days,
    }
