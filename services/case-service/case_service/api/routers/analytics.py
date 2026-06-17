"""Analytics API router.

Aggregate queries for case metrics, SLA compliance, resolution
times, and bottleneck identification.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select, case as sa_case, extract, and_, desc
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import (
    CaseAssignmentModel,
    CaseAuditLogModel,
    CaseInstanceModel,
    CaseSLAInstanceModel,
    CaseTypeModel,
    SavedReportModel,
)
from case_service.db.session import get_analytics_session as get_session
# Group I: heavy read-only endpoints run on the replica dependency (falls
# back to the analytics pool when no replica is configured). Saved-report
# CRUD stays on get_session — it writes, and must always hit the primary.
from case_service.db.session import get_replica_session as get_ro_session
from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser

router = APIRouter(prefix="/analytics", tags=["analytics"])


# ─── Response schemas ─────────────────────────────────────────────


class OverviewMetrics(BaseModel):
    total_cases: int = 0
    open_cases: int = 0
    resolved_cases: int = 0
    closed_cases: int = 0
    cancelled_cases: int = 0
    avg_resolution_hours: float | None = None
    cases_created_today: int = 0
    cases_resolved_today: int = 0


class StatusBreakdown(BaseModel):
    status: str
    count: int


class PriorityBreakdown(BaseModel):
    priority: str
    count: int


class CaseTypeBreakdown(BaseModel):
    case_type_id: str
    case_type_name: str
    count: int


class SLAComplianceMetrics(BaseModel):
    total_sla_instances: int = 0
    on_track: int = 0
    at_risk: int = 0
    breached: int = 0
    paused: int = 0
    compliance_rate: float = 0.0  # percentage on_track / total


class TimeSeriesPoint(BaseModel):
    date: str
    count: int


class StageBottleneck(BaseModel):
    stage_id: str
    avg_duration_hours: float
    case_count: int


class AssignmentMetrics(BaseModel):
    total_assignments: int = 0
    active: int = 0
    completed: int = 0
    avg_completion_hours: float | None = None
    unassigned: int = 0


class AnalyticsDashboard(BaseModel):
    overview: OverviewMetrics
    status_breakdown: list[StatusBreakdown]
    priority_breakdown: list[PriorityBreakdown]
    case_type_breakdown: list[CaseTypeBreakdown]
    sla_compliance: SLAComplianceMetrics
    assignments: AssignmentMetrics
    cases_over_time: list[TimeSeriesPoint]
    resolved_over_time: list[TimeSeriesPoint] = []


# ─── Endpoints ────────────────────────────────────────────────────


@router.get("/dashboard", response_model=AnalyticsDashboard)
async def get_dashboard(
    days: int = Query(30, ge=1, le=365),
    case_type_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_ro_session),
):
    """Full analytics dashboard — all metrics in one call."""
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    base_filter = []
    if tenant_id:
        base_filter.append(CaseInstanceModel.tenant_id == tenant_id)
    if case_type_id:
        base_filter.append(CaseInstanceModel.case_type_id == case_type_id)

    overview = await _get_overview(session, base_filter, today_start)
    status = await _get_status_breakdown(session, base_filter)
    priority = await _get_priority_breakdown(session, base_filter)
    ct_breakdown = await _get_case_type_breakdown(session, base_filter)
    sla = await _get_sla_compliance(session, case_type_id)
    assignments = await _get_assignment_metrics(session, case_type_id)
    time_series = await _get_cases_over_time(session, since, base_filter)
    resolved_series = await _get_resolved_over_time(session, since, base_filter)

    return AnalyticsDashboard(
        overview=overview,
        status_breakdown=status,
        priority_breakdown=priority,
        case_type_breakdown=ct_breakdown,
        sla_compliance=sla,
        assignments=assignments,
        cases_over_time=time_series,
        resolved_over_time=resolved_series,
    )


@router.get("/overview", response_model=OverviewMetrics)
async def get_overview_metrics(
    case_type_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_ro_session),
):
    base_filter = []
    if case_type_id:
        base_filter.append(CaseInstanceModel.case_type_id == case_type_id)
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return await _get_overview(session, base_filter, today_start)


@router.get("/sla-compliance", response_model=SLAComplianceMetrics)
async def get_sla_compliance(
    case_type_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_ro_session),
):
    return await _get_sla_compliance(session, case_type_id)


@router.get("/cases-over-time", response_model=list[TimeSeriesPoint])
async def get_cases_over_time(
    days: int = Query(30, ge=1, le=365),
    case_type_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_ro_session),
):
    since = datetime.now(timezone.utc) - timedelta(days=days)
    base_filter = []
    if case_type_id:
        base_filter.append(CaseInstanceModel.case_type_id == case_type_id)
    return await _get_cases_over_time(session, since, base_filter)


@router.get("/bottlenecks", response_model=list[StageBottleneck])
async def get_bottlenecks(
    case_type_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_ro_session),
):
    """Identify stages where cases spend the most time."""
    # Look at audit log stage_transitioned events to compute avg duration per stage
    stmt = (
        select(
            CaseAuditLogModel.details["stage_id"].as_string().label("stage_id"),
            func.count().label("case_count"),
        )
        .where(CaseAuditLogModel.action == "stage_transitioned")
        .group_by("stage_id")
        .order_by(func.count().desc())
        .limit(20)
    )

    result = await session.execute(stmt)
    rows = result.all()

    bottlenecks = []
    for row in rows:
        stage_id = row.stage_id if row.stage_id else "unknown"
        bottlenecks.append(StageBottleneck(
            stage_id=stage_id,
            avg_duration_hours=0.0,  # Would need stage enter/exit timestamps for real computation
            case_count=row.case_count,
        ))

    return bottlenecks


# ─── Internal query functions ─────────────────────────────────────


async def _get_overview(
    session: AsyncSession,
    base_filter: list,
    today_start: datetime,
) -> OverviewMetrics:
    # Total cases
    total_q = select(func.count()).select_from(CaseInstanceModel)
    for f in base_filter:
        total_q = total_q.where(f)
    total = (await session.execute(total_q)).scalar_one()

    # By status
    status_counts = {}
    for s in ["new", "open", "resolved", "closed", "cancelled", "reopened",
              "pending_external", "pending_subcase"]:
        q = select(func.count()).select_from(CaseInstanceModel).where(
            CaseInstanceModel.status == s
        )
        for f in base_filter:
            q = q.where(f)
        status_counts[s] = (await session.execute(q)).scalar_one()

    open_cases = sum(status_counts.get(s, 0) for s in ["new", "open", "reopened",
                     "pending_external", "pending_subcase"])

    # Average resolution time
    avg_q = select(
        func.avg(
            extract("epoch", CaseInstanceModel.resolved_at) -
            extract("epoch", CaseInstanceModel.created_at)
        )
    ).where(CaseInstanceModel.resolved_at.isnot(None))
    for f in base_filter:
        avg_q = avg_q.where(f)
    avg_seconds = (await session.execute(avg_q)).scalar_one()
    avg_hours = round(avg_seconds / 3600, 1) if avg_seconds else None

    # Today's counts
    created_today_q = select(func.count()).select_from(CaseInstanceModel).where(
        CaseInstanceModel.created_at >= today_start
    )
    for f in base_filter:
        created_today_q = created_today_q.where(f)
    created_today = (await session.execute(created_today_q)).scalar_one()

    resolved_today_q = select(func.count()).select_from(CaseInstanceModel).where(
        CaseInstanceModel.resolved_at >= today_start
    )
    for f in base_filter:
        resolved_today_q = resolved_today_q.where(f)
    resolved_today = (await session.execute(resolved_today_q)).scalar_one()

    return OverviewMetrics(
        total_cases=total,
        open_cases=open_cases,
        resolved_cases=status_counts.get("resolved", 0),
        closed_cases=status_counts.get("closed", 0),
        cancelled_cases=status_counts.get("cancelled", 0),
        avg_resolution_hours=avg_hours,
        cases_created_today=created_today,
        cases_resolved_today=resolved_today,
    )


async def _get_status_breakdown(
    session: AsyncSession, base_filter: list
) -> list[StatusBreakdown]:
    q = (
        select(
            CaseInstanceModel.status,
            func.count().label("cnt"),
        )
        .group_by(CaseInstanceModel.status)
        .order_by(func.count().desc())
    )
    for f in base_filter:
        q = q.where(f)
    result = await session.execute(q)
    return [StatusBreakdown(status=r.status, count=r.cnt) for r in result.all()]


async def _get_priority_breakdown(
    session: AsyncSession, base_filter: list
) -> list[PriorityBreakdown]:
    q = (
        select(
            CaseInstanceModel.priority,
            func.count().label("cnt"),
        )
        .group_by(CaseInstanceModel.priority)
        .order_by(func.count().desc())
    )
    for f in base_filter:
        q = q.where(f)
    result = await session.execute(q)
    return [PriorityBreakdown(priority=r.priority, count=r.cnt) for r in result.all()]


async def _get_case_type_breakdown(
    session: AsyncSession, base_filter: list
) -> list[CaseTypeBreakdown]:
    q = (
        select(
            CaseInstanceModel.case_type_id,
            CaseTypeModel.name.label("ct_name"),
            func.count().label("cnt"),
        )
        .join(CaseTypeModel, CaseInstanceModel.case_type_id == CaseTypeModel.id)
        .group_by(CaseInstanceModel.case_type_id, CaseTypeModel.name)
        .order_by(func.count().desc())
    )
    for f in base_filter:
        q = q.where(f)
    result = await session.execute(q)
    return [
        CaseTypeBreakdown(
            case_type_id=str(r.case_type_id),
            case_type_name=r.ct_name,
            count=r.cnt,
        )
        for r in result.all()
    ]


async def _get_sla_compliance(
    session: AsyncSession, case_type_id: uuid.UUID | None
) -> SLAComplianceMetrics:
    q = select(
        CaseSLAInstanceModel.status,
        func.count().label("cnt"),
    ).group_by(CaseSLAInstanceModel.status)

    if case_type_id:
        q = q.join(
            CaseInstanceModel,
            CaseSLAInstanceModel.case_id == CaseInstanceModel.id,
        ).where(CaseInstanceModel.case_type_id == case_type_id)

    result = await session.execute(q)
    counts = {r.status: r.cnt for r in result.all()}

    total = sum(counts.values())
    on_track = counts.get("on_track", 0)
    compliance = round((on_track / total) * 100, 1) if total > 0 else 0.0

    return SLAComplianceMetrics(
        total_sla_instances=total,
        on_track=on_track,
        at_risk=counts.get("at_risk", 0),
        breached=counts.get("breached", 0),
        paused=counts.get("paused", 0),
        compliance_rate=compliance,
    )


async def _get_assignment_metrics(
    session: AsyncSession, case_type_id: uuid.UUID | None
) -> AssignmentMetrics:
    base = select(
        CaseAssignmentModel.status,
        func.count().label("cnt"),
    ).group_by(CaseAssignmentModel.status)

    if case_type_id:
        base = base.join(
            CaseInstanceModel,
            CaseAssignmentModel.case_id == CaseInstanceModel.id,
        ).where(CaseInstanceModel.case_type_id == case_type_id)

    result = await session.execute(base)
    counts = {r.status: r.cnt for r in result.all()}

    total = sum(counts.values())
    active = counts.get("active", 0)
    completed = counts.get("completed", 0)

    # Avg completion time
    avg_q = select(
        func.avg(
            extract("epoch", CaseAssignmentModel.completed_at) -
            extract("epoch", CaseAssignmentModel.assigned_at)
        )
    ).where(CaseAssignmentModel.completed_at.isnot(None))
    if case_type_id:
        avg_q = avg_q.join(
            CaseInstanceModel,
            CaseAssignmentModel.case_id == CaseInstanceModel.id,
        ).where(CaseInstanceModel.case_type_id == case_type_id)
    avg_secs = (await session.execute(avg_q)).scalar_one()
    avg_hours = round(avg_secs / 3600, 1) if avg_secs else None

    # Unassigned (queue-type, not claimed)
    unassigned_q = select(func.count()).select_from(CaseAssignmentModel).where(
        CaseAssignmentModel.status == "active",
        CaseAssignmentModel.claimed_at.is_(None),
    )
    unassigned = (await session.execute(unassigned_q)).scalar_one()

    return AssignmentMetrics(
        total_assignments=total,
        active=active,
        completed=completed,
        avg_completion_hours=avg_hours,
        unassigned=unassigned,
    )


async def _get_cases_over_time(
    session: AsyncSession,
    since: datetime,
    base_filter: list,
) -> list[TimeSeriesPoint]:
    """Daily case creation counts."""
    q = (
        select(
            func.date(CaseInstanceModel.created_at).label("day"),
            func.count().label("cnt"),
        )
        .where(CaseInstanceModel.created_at >= since)
        .group_by("day")
        .order_by("day")
    )
    for f in base_filter:
        q = q.where(f)

    result = await session.execute(q)
    return [
        TimeSeriesPoint(date=str(r.day), count=r.cnt)
        for r in result.all()
    ]


async def _get_resolved_over_time(
    session: AsyncSession,
    since: datetime,
    base_filter: list,
) -> list[TimeSeriesPoint]:
    """Daily case resolution counts."""
    q = (
        select(
            func.date(CaseInstanceModel.resolved_at).label("day"),
            func.count().label("cnt"),
        )
        .where(
            CaseInstanceModel.resolved_at.isnot(None),
            CaseInstanceModel.resolved_at >= since,
        )
        .group_by("day")
        .order_by("day")
    )
    for f in base_filter:
        q = q.where(f)

    result = await session.execute(q)
    return [
        TimeSeriesPoint(date=str(r.day), count=r.cnt)
        for r in result.all()
    ]


# ── P26 HxAnalytics additions ─────────────────────────────────────────────────

from case_service.analytics.metrics import platform_snapshot, cases_over_time, sla_performance, funnel_by_case_type
from case_service.analytics.query_engine import nl_query, run_structured
from case_service.analytics.exporter import to_csv, to_json, odata_response


class NLQueryRequest(BaseModel):
    question: Optional[str] = None
    query_def: Optional[dict] = None
    chart_type: Optional[str] = None


class SaveReportRequest(BaseModel):
    name: str
    description: Optional[str] = None
    query_type: str = "structured"
    query_def: dict = {}
    chart_type: str = "bar"
    is_public: bool = False


@router.get("/metrics/snapshot")
async def metrics_snapshot(
    session: AsyncSession = Depends(get_ro_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    """Full platform snapshot: case counts, SLA breach %, avg resolution, by-type."""
    return await platform_snapshot(session)


@router.get("/metrics/time-series")
async def metrics_time_series(
    days: int = Query(30, ge=1, le=365),
    session: AsyncSession = Depends(get_ro_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    return {"series": await cases_over_time(session, days=days), "period_days": days}


@router.get("/metrics/sla-performance")
async def metrics_sla(
    days: int = Query(30, ge=1, le=365),
    session: AsyncSession = Depends(get_ro_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    return await sla_performance(session, days=days)


@router.get("/metrics/funnel/{case_type_id}")
async def metrics_funnel(
    case_type_id: str,
    session: AsyncSession = Depends(get_ro_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    ct = (await session.execute(
        select(CaseTypeModel).where(CaseTypeModel.id == case_type_id)
    )).scalar_one_or_none()
    if not ct:
        raise HTTPException(404, "Case type not found")
    result = await funnel_by_case_type(session, case_type_id)
    result["case_type_name"] = ct.name
    return result


@router.post("/query")
async def analytics_query(
    body: NLQueryRequest,
    session: AsyncSession = Depends(get_ro_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    """NL or structured query → chart-ready data."""
    if body.question:
        return await nl_query(body.question, session)
    if body.query_def:
        qd = {**body.query_def, **({"chart_type": body.chart_type} if body.chart_type else {})}
        return await run_structured(qd, session)
    raise HTTPException(400, "Provide 'question' (NL) or 'query_def' (structured)")


@router.get("/reports")
async def list_saved_reports(
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    rows = (await session.execute(
        select(SavedReportModel).order_by(desc(SavedReportModel.created_at))
    )).scalars().all()
    return {"reports": [_rpt(r) for r in rows], "total": len(rows)}


@router.post("/reports", status_code=201)
async def save_report(
    body: SaveReportRequest,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    r = SavedReportModel(
        name=body.name, description=body.description,
        query_type=body.query_type, query_def=body.query_def,
        chart_type=body.chart_type, is_public=body.is_public,
        created_by=user.user_id,
    )
    session.add(r)
    await session.commit()
    await session.refresh(r)
    return _rpt(r)


@router.get("/reports/{report_id}")
async def get_saved_report(
    report_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    return _rpt(await _rpt_or_404(session, report_id))


@router.delete("/reports/{report_id}", status_code=204)
async def delete_saved_report(
    report_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    r = await _rpt_or_404(session, report_id)
    await session.delete(r)
    await session.commit()


@router.get("/reports/{report_id}/run")
async def run_saved_report(
    report_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    r = await _rpt_or_404(session, report_id)
    result = (await nl_query(r.query_def.get("question", ""), session)
              if r.query_type == "nl"
              else await run_structured(r.query_def, session))
    result["report_id"]   = str(r.id)
    result["report_name"] = r.name
    return result


@router.get("/reports/{report_id}/export")
async def export_saved_report(
    report_id: uuid.UUID,
    format: str = Query("csv", pattern="^(csv|json)$"),
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    r = await _rpt_or_404(session, report_id)
    result = (await nl_query(r.query_def.get("question", ""), session)
              if r.query_type == "nl"
              else await run_structured(r.query_def, session))
    fname = r.name.replace(" ", "_")
    if format == "csv":
        return Response(to_csv(result.get("series", [])), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{fname}.csv"'})
    return Response(to_json(result), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{fname}.json"'})


@router.get("/odata")
async def odata_feed(
    top:  int = Query(1000, alias="$top",  le=10000),
    skip: int = Query(0,    alias="$skip", ge=0),
    session: AsyncSession = Depends(get_ro_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    """OData v4 compatible endpoint — connect PowerBI or Tableau directly to this URL."""
    rows = (await session.execute(
        select(CaseInstanceModel).order_by(desc(CaseInstanceModel.created_at))
        .offset(skip).limit(top)
    )).scalars().all()
    records = [{
        "id": str(r.id), "case_number": r.case_number,
        "case_type_id": str(r.case_type_id), "status": r.status,
        "priority": r.priority, "current_stage": r.current_stage_id,
        "created_by": r.created_by,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "resolved_at": r.resolved_at.isoformat() if r.resolved_at else None,
    } for r in rows]
    return odata_response(records)


async def _rpt_or_404(session, report_id):
    r = await session.get(SavedReportModel, report_id)
    if not r:
        raise HTTPException(404, f"Report {report_id} not found")
    return r


def _rpt(r: SavedReportModel) -> dict:
    return {
        "id": str(r.id), "name": r.name, "description": r.description,
        "query_type": r.query_type, "query_def": r.query_def,
        "chart_type": r.chart_type, "is_public": r.is_public,
        "created_by": r.created_by,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }
