"""Analytics API router.

Aggregate queries for case metrics, SLA compliance, resolution
times, and bottleneck identification.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import uuid
from typing import Optional

from velaris_shared import hxguard
from fastapi import Request, APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select, case as sa_case, extract, and_, desc
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.portable_sql import duration_seconds
from velaris_shared.db.models import (
    CaseAssignmentModel,
    CaseAuditLogModel,
    CaseInstanceModel,
    CaseSLAInstanceModel,
    CaseTypeModel,
    SavedReportModel,
)
from velaris_shared.db.session import get_analytics_session as get_session
# Group I: heavy read-only endpoints run on the replica dependency (falls
# back to the analytics pool when no replica is configured). Saved-report
# CRUD stays on get_session — it writes, and must always hit the primary.
from velaris_shared.db.session import get_replica_session as get_ro_session
from velaris_shared.auth.dependencies import get_current_user
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.db.read_routing import get_analytics_cutoff_session

router = APIRouter(prefix="/analytics", tags=["analytics"])


# ─── Short reuse of answers (VELARIS_CASE_ANALYTICS_CACHE_SECONDS) ─

_inflight: dict[str, asyncio.Lock] = {}


async def _cached(session: AsyncSession, name: str, params: dict, compute) -> Any:
    """Reuse an answer for the same tenant scope and parameters for a few
    seconds (R6 soak 2026-11-05: 400 users' dashboards each recomputed the
    same numbers). Off (0, the default): compute every time, as before.

    The key carries the session's tenant scope as `_tenant_scope` set it —
    exactly what the queries below are filtered by — so two tenants (or a
    tenant and the operator) never share an answer. Within one process,
    concurrent misses for one key wait for a single computation.
    """
    from velaris_shared import shared_state
    from velaris_shared.config import get_settings
    from velaris_shared.tenancy.orm_scope import SCOPE_KEY

    ttl = min(max(int(getattr(get_settings(), "analytics_cache_seconds", 0) or 0), 0), 300)
    if ttl <= 0:
        return await compute()
    scope = getattr(session, "sync_session", session).info.get(SCOPE_KEY)
    if scope is not None:
        scope = {"uuids": sorted(str(u) for u in scope["uuids"]),
                 "tokens": sorted(str(t) for t in scope["tokens"])}
    key = json.dumps([name, params, scope], sort_keys=True, default=str)

    hit = await shared_state.get("analytics", key)
    if hit is not None:
        return json.loads(hit)
    lock = _inflight.setdefault(key, asyncio.Lock())
    try:
        async with lock:
            hit = await shared_state.get("analytics", key)
            if hit is not None:
                return json.loads(hit)
            value = await compute()
            data = (value.model_dump(mode="json") if isinstance(value, BaseModel)
                    else [v.model_dump(mode="json") if isinstance(v, BaseModel) else v
                          for v in value] if isinstance(value, list) else value)
            await shared_state.put("analytics", key, json.dumps(data), ttl)
            return data
    finally:
        if not lock.locked() and _inflight.get(key) is lock:
            _inflight.pop(key, None)


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
    _user: AuthenticatedUser = Depends(hxguard.guard("analytics.reports.read")),
):
    """Full analytics dashboard — all metrics in one call."""
    return await _cached(
        session, "dashboard",
        {"days": days, "case_type_id": case_type_id, "tenant_id": tenant_id},
        lambda: _dashboard(session, days, case_type_id, tenant_id),
    )


async def _dashboard(session, days, case_type_id, tenant_id) -> AnalyticsDashboard:
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    base_filter = []
    if tenant_id:
        base_filter.append(CaseInstanceModel.tenant_id == tenant_id)
    if case_type_id:
        base_filter.append(CaseInstanceModel.case_type_id == case_type_id)

    # One pass over the cases gives the overview and all three breakdowns
    # (R6 soak 2026-11-05: 19 sequential queries held a connection from a
    # pool of 5 for the whole request; dashboard p95 8.5 s at 420k cases).
    rollup = await _case_rollup(session, base_filter, today_start)
    overview = _overview_from(rollup)
    status = _status_from(rollup)
    priority = _priority_from(rollup)
    ct_breakdown = await _case_types_from(session, rollup)
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
    _user: AuthenticatedUser = Depends(hxguard.guard("analytics.reports.read")),
):
    async def compute():
        base_filter = []
        if case_type_id:
            base_filter.append(CaseInstanceModel.case_type_id == case_type_id)
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return _overview_from(await _case_rollup(session, base_filter, today_start))
    return await _cached(session, "overview", {"case_type_id": case_type_id}, compute)


@router.get("/sla-compliance", response_model=SLAComplianceMetrics)
async def get_sla_compliance(
    case_type_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_ro_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("analytics.reports.read")),
):
    return await _cached(session, "sla-compliance", {"case_type_id": case_type_id},
                         lambda: _get_sla_compliance(session, case_type_id))


@router.get("/cases-over-time", response_model=list[TimeSeriesPoint])
async def get_cases_over_time(
    days: int = Query(30, ge=1, le=365),
    case_type_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_ro_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("analytics.reports.read")),
):
    async def compute():
        since = datetime.now(timezone.utc) - timedelta(days=days)
        base_filter = []
        if case_type_id:
            base_filter.append(CaseInstanceModel.case_type_id == case_type_id)
        return await _get_cases_over_time(session, since, base_filter)
    return await _cached(session, "cases-over-time",
                         {"days": days, "case_type_id": case_type_id}, compute)


@router.get("/bottlenecks", response_model=list[StageBottleneck])
async def get_bottlenecks(
    case_type_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_ro_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("analytics.reports.read")),
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


_OPEN_STATUSES = ("new", "open", "reopened", "pending_external", "pending_subcase")


async def _case_rollup(
    session: AsyncSession,
    base_filter: list,
    today_start: datetime,
) -> list:
    """One scan of the caller's cases, grouped by (status, priority, case type).

    The group count is small (statuses × priorities × case types), and every
    dashboard number over cases is a sum over these groups. Replaces 15
    separate full-table queries (total, 8 status counts, average resolution,
    created/resolved today, and the status, priority and case-type GROUP BYs).
    """
    resolution = duration_seconds(session, CaseInstanceModel.created_at,
                                  CaseInstanceModel.resolved_at)
    q = (
        select(
            CaseInstanceModel.status,
            CaseInstanceModel.priority,
            CaseInstanceModel.case_type_id,
            func.count().label("cnt"),
            # count/sum of a NULL-able expression skip NULLs, exactly as the
            # old AVG(... WHERE resolved_at IS NOT NULL) did.
            func.count(resolution).label("res_n"),
            func.sum(resolution).label("res_secs"),
            func.sum(sa_case((CaseInstanceModel.created_at >= today_start, 1), else_=0)).label("created_today"),
            func.sum(sa_case((CaseInstanceModel.resolved_at >= today_start, 1), else_=0)).label("resolved_today"),
        )
        .group_by(CaseInstanceModel.status, CaseInstanceModel.priority,
                  CaseInstanceModel.case_type_id)
    )
    for f in base_filter:
        q = q.where(f)
    return list((await session.execute(q)).all())


def _overview_from(rows: list) -> OverviewMetrics:
    by_status: dict[str, int] = {}
    res_n = 0
    res_secs = 0.0
    created_today = resolved_today = 0
    for r in rows:
        by_status[r.status] = by_status.get(r.status, 0) + r.cnt
        res_n += r.res_n or 0
        res_secs += float(r.res_secs or 0)
        created_today += int(r.created_today or 0)
        resolved_today += int(r.resolved_today or 0)
    avg_seconds = res_secs / res_n if res_n else None
    return OverviewMetrics(
        total_cases=sum(by_status.values()),
        open_cases=sum(by_status.get(s, 0) for s in _OPEN_STATUSES),
        resolved_cases=by_status.get("resolved", 0),
        closed_cases=by_status.get("closed", 0),
        cancelled_cases=by_status.get("cancelled", 0),
        avg_resolution_hours=round(avg_seconds / 3600, 1) if avg_seconds else None,
        cases_created_today=created_today,
        cases_resolved_today=resolved_today,
    )


def _sum_by(rows: list, attr: str) -> list[tuple[Any, int]]:
    totals: dict[Any, int] = {}
    for r in rows:
        k = getattr(r, attr)
        totals[k] = totals.get(k, 0) + r.cnt
    return sorted(totals.items(), key=lambda kv: kv[1], reverse=True)


def _status_from(rows: list) -> list[StatusBreakdown]:
    return [StatusBreakdown(status=k, count=n) for k, n in _sum_by(rows, "status")]


def _priority_from(rows: list) -> list[PriorityBreakdown]:
    return [PriorityBreakdown(priority=k, count=n) for k, n in _sum_by(rows, "priority")]


async def _case_types_from(session: AsyncSession, rows: list) -> list[CaseTypeBreakdown]:
    """Counts per case type, named. A case whose type the caller cannot see is
    left out, as the old INNER JOIN on case_types (tenant-scoped) did."""
    totals = _sum_by(rows, "case_type_id")
    ids = [k for k, _ in totals if k is not None]
    if not ids:
        return []
    names = {
        r.id: r.name
        for r in (await session.execute(
            select(CaseTypeModel.id, CaseTypeModel.name).where(CaseTypeModel.id.in_(ids))
        )).all()
    }
    return [
        CaseTypeBreakdown(case_type_id=str(k), case_type_name=names[k], count=n)
        for k, n in totals if k in names
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
    completion = duration_seconds(session, CaseAssignmentModel.assigned_at,
                                  CaseAssignmentModel.completed_at)
    unclaimed = sa_case(
        (and_(CaseAssignmentModel.status == "active",
              CaseAssignmentModel.claimed_at.is_(None)), 1),
        else_=0,
    )
    base = select(
        CaseAssignmentModel.status,
        func.count().label("cnt"),
        func.count(completion).label("done_n"),
        func.sum(completion).label("done_secs"),
        func.sum(unclaimed).label("unclaimed"),
    ).group_by(CaseAssignmentModel.status)

    if case_type_id:
        base = base.join(
            CaseInstanceModel,
            CaseAssignmentModel.case_id == CaseInstanceModel.id,
        ).where(CaseInstanceModel.case_type_id == case_type_id)

    rows = (await session.execute(base)).all()
    counts = {r.status: r.cnt for r in rows}
    done_n = sum(r.done_n or 0 for r in rows)
    done_secs = sum(float(r.done_secs or 0) for r in rows)
    avg_secs = done_secs / done_n if done_n else None
    avg_hours = round(avg_secs / 3600, 1) if avg_secs else None

    if case_type_id:
        # Unassigned has always been counted across every case type, filter
        # or not; keep that answer.
        unassigned_q = select(func.count()).select_from(CaseAssignmentModel).where(
            CaseAssignmentModel.status == "active",
            CaseAssignmentModel.claimed_at.is_(None),
        )
        unassigned = (await session.execute(unassigned_q)).scalar_one()
    else:
        unassigned = sum(int(r.unclaimed or 0) for r in rows)

    return AssignmentMetrics(
        total_assignments=sum(counts.values()),
        active=counts.get("active", 0),
        completed=counts.get("completed", 0),
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

from velaris_shared.analytics.metrics import platform_snapshot, cases_over_time, sla_performance, funnel_by_case_type
from velaris_shared.analytics.query_engine import nl_query, run_structured
from velaris_shared.analytics.exporter import to_csv, to_json, odata_response


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
    rows = [r for r in rows if await _rpt_visible(session, _, r)]
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
        # Unset until 2026-09-29, so every saved report belonged to nobody and
        # was listed to every tenant (8d).
        tenant_id=str(user.tenant_id) if getattr(user, "tenant_id", None) else None,
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
    return _rpt(await _rpt_or_404(session, report_id, _))


@router.delete("/reports/{report_id}", status_code=204)
async def delete_saved_report(
    report_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    r = await _rpt_or_404(session, report_id, _, write=True)
    await session.delete(r)
    await session.commit()


@router.get("/reports/{report_id}/run")
async def run_saved_report(
    report_id: uuid.UUID,
    session: AsyncSession = Depends(get_ro_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    r = await _rpt_or_404(session, report_id, _)
    result = (await nl_query(r.query_def.get("question", ""), session)
              if r.query_type == "nl"
              else await run_structured(r.query_def, session))
    result["report_id"]   = str(r.id)
    result["report_name"] = r.name
    return result


async def _scoped_cutoff_session(request: Request,
                                 session: AsyncSession = Depends(get_analytics_cutoff_session)) -> AsyncSession:
    """The export's cutoff session (R5d 11b), scoped to the caller's tenant like
    every analytics session. analytics-service scopes by MARKING sessions
    (tenancy.orm_scope), and the app-level `_tenant_scope` marks only the
    `get_replica_session` / `get_analytics_session` objects — a session from any
    other dependency reads every tenant (found in 11b: another tenant's saved
    report ran)."""
    from velaris_shared.tenancy.orm_scope import scope_session_to_caller
    await scope_session_to_caller(request, session)
    return session


@router.get("/reports/{report_id}/export")
async def export_saved_report(
    report_id: uuid.UUID,
    format: str = Query("csv", pattern="^(csv|json)$"),
    session: AsyncSession = Depends(_scoped_cutoff_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    r = await _rpt_or_404(session, report_id, _)
    result = (await nl_query(r.query_def.get("question", ""), session)
              if r.query_type == "nl"
              else await run_structured(r.query_def, session))

    # HxPrivacy (§8.10): a saved report is downloaded and shared onward, so
    # confirmed personal data is masked on the way out. The series is rebuilt
    # rather than mutated — `result` may be reused by the caller.
    from velaris_shared.config import get_settings as _gs

    if _gs().pii_redact_exports:
        from velaris_shared.db import pii_redaction as _pii

        _paths = await _pii.confirmed_field_paths(session)
        result = {**result, "series": _pii.redact_rows(result.get("series", []), _paths)}

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


async def _rpt_visible(session, user, r) -> bool:
    """2026-11-04: saved reports had no read rule at all — every tenant's,
    private ones included, to any signed-in user. A report is visible in its
    own tenant (a tenant-less one is the default tenant's) and, there, to its
    creator or to everyone when public. The platform operator sees all."""
    from velaris_shared.tenancy.identity import (
        DEFAULT_TENANT, is_platform_operator, same_tenant,
    )
    if await is_platform_operator(session, user):
        return True
    owner = r.tenant_id or DEFAULT_TENANT
    if not await same_tenant(session, owner, getattr(user, "tenant_id", None) or DEFAULT_TENANT):
        return False
    return bool(r.is_public) or _is_creator(user, r)


def _is_creator(user, r) -> bool:
    return r.created_by is not None and r.created_by in {
        str(getattr(user, "user_id", "")), str(getattr(user, "username", ""))}


async def _rpt_or_404(session, report_id, user=None, *, write: bool = False):
    r = await session.get(SavedReportModel, report_id)
    if not r or (user is not None and not await _rpt_visible(session, user, r)):
        raise HTTPException(404, f"Report {report_id} not found")
    if write and user is not None and not _is_creator(user, r):
        from velaris_shared.tenancy.identity import is_platform_operator
        if not await is_platform_operator(session, user):
            raise HTTPException(403, "Only the report's creator can delete it.")
    return r


def _rpt(r: SavedReportModel) -> dict:
    return {
        "id": str(r.id), "name": r.name, "description": r.description,
        "query_type": r.query_type, "query_def": r.query_def,
        "chart_type": r.chart_type, "is_public": r.is_public,
        "created_by": r.created_by,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }
