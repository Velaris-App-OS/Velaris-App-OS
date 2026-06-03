"""Process mining analyzer.

Core algorithms:
- Activity frequency & duration stats
- Bottleneck detection (highest avg waiting time)
- Variant discovery (unique paths through the process)
- Conformance checking (actual vs planned flow)
- Directly-follows graph (DFG) construction

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import CaseEventLogModel, CaseInstanceModel, CaseTypeModel


def _tenant_case_type_ids(tenant_id: uuid.UUID):
    """Subquery returning case_type IDs belonging to a tenant."""
    return select(CaseTypeModel.id).where(CaseTypeModel.tenant_id == tenant_id).scalar_subquery()


# ─── Activity statistics ────────────────────────────────────────

async def activity_stats(
    session: AsyncSession,
    case_type_id: uuid.UUID | None = None,
    days: int = 30,
    *,
    tenant_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    """Compute frequency and average duration per activity."""
    since = datetime.now(timezone.utc) - timedelta(days=days)

    stmt = select(
        CaseEventLogModel.activity,
        CaseEventLogModel.activity_type,
        func.count().label("count"),
        func.avg(CaseEventLogModel.duration_seconds).label("avg_duration"),
        func.min(CaseEventLogModel.duration_seconds).label("min_duration"),
        func.max(CaseEventLogModel.duration_seconds).label("max_duration"),
    ).where(
        CaseEventLogModel.timestamp >= since,
    ).group_by(
        CaseEventLogModel.activity, CaseEventLogModel.activity_type,
    ).order_by(desc("count"))

    if tenant_id:
        stmt = stmt.where(CaseEventLogModel.case_type_id.in_(_tenant_case_type_ids(tenant_id)))
    if case_type_id:
        stmt = stmt.where(CaseEventLogModel.case_type_id == case_type_id)

    result = await session.execute(stmt)
    return [
        {
            "activity": r.activity,
            "activity_type": r.activity_type,
            "count": r.count,
            "avg_duration_seconds": float(r.avg_duration) if r.avg_duration else 0.0,
            "min_duration_seconds": int(r.min_duration) if r.min_duration else 0,
            "max_duration_seconds": int(r.max_duration) if r.max_duration else 0,
        }
        for r in result.all()
    ]


# ─── Bottleneck detection ───────────────────────────────────────

async def find_bottlenecks(
    session: AsyncSession,
    case_type_id: uuid.UUID | None = None,
    days: int = 30,
    limit: int = 10,
    *,
    tenant_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    """Identify activities with longest average duration."""
    since = datetime.now(timezone.utc) - timedelta(days=days)

    stmt = select(
        CaseEventLogModel.activity,
        func.count().label("occurrences"),
        func.avg(CaseEventLogModel.duration_seconds).label("avg_duration"),
        func.max(CaseEventLogModel.duration_seconds).label("max_duration"),
    ).where(
        CaseEventLogModel.timestamp >= since,
        CaseEventLogModel.duration_seconds.isnot(None),
    ).group_by(
        CaseEventLogModel.activity
    ).order_by(desc("avg_duration")).limit(limit)

    if tenant_id:
        stmt = stmt.where(CaseEventLogModel.case_type_id.in_(_tenant_case_type_ids(tenant_id)))
    if case_type_id:
        stmt = stmt.where(CaseEventLogModel.case_type_id == case_type_id)

    result = await session.execute(stmt)
    return [
        {
            "activity": r.activity,
            "occurrences": r.occurrences,
            "avg_duration_seconds": float(r.avg_duration) if r.avg_duration else 0.0,
            "max_duration_seconds": int(r.max_duration) if r.max_duration else 0,
            "severity": _classify_severity(float(r.avg_duration or 0)),
        }
        for r in result.all()
    ]


def _classify_severity(avg_seconds: float) -> str:
    if avg_seconds > 86400:  # > 1 day
        return "critical"
    if avg_seconds > 14400:  # > 4 hours
        return "high"
    if avg_seconds > 3600:  # > 1 hour
        return "medium"
    return "low"


# ─── Variant discovery (unique flows) ───────────────────────────

async def discover_variants(
    session: AsyncSession,
    case_type_id: uuid.UUID | None = None,
    days: int = 30,
    limit: int = 20,
    *,
    tenant_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    """Find unique paths through the process.

    A variant is the sequence of activities for a case.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)

    stmt = select(
        CaseEventLogModel.case_id,
        CaseEventLogModel.activity,
        CaseEventLogModel.timestamp,
    ).where(
        CaseEventLogModel.timestamp >= since,
    ).order_by(
        CaseEventLogModel.case_id, CaseEventLogModel.timestamp,
    )

    if tenant_id:
        stmt = stmt.where(CaseEventLogModel.case_type_id.in_(_tenant_case_type_ids(tenant_id)))
    if case_type_id:
        stmt = stmt.where(CaseEventLogModel.case_type_id == case_type_id)

    result = await session.execute(stmt)

    # Group events by case and build traces
    traces: dict[str, list[str]] = defaultdict(list)
    for row in result.all():
        traces[str(row.case_id)].append(row.activity)

    # Count variants
    variant_counter: Counter = Counter()
    variant_cases: dict[tuple, list[str]] = defaultdict(list)
    for case_id, trace in traces.items():
        key = tuple(trace)
        variant_counter[key] += 1
        variant_cases[key].append(case_id)

    # Top variants
    variants = []
    for i, (variant, count) in enumerate(variant_counter.most_common(limit)):
        variants.append({
            "variant_id": i + 1,
            "sequence": list(variant),
            "case_count": count,
            "percentage": round(count / len(traces) * 100, 1) if traces else 0,
            "example_cases": variant_cases[variant][:3],  # First 3 case IDs
        })

    return variants


# ─── Directly-follows graph ─────────────────────────────────────

async def build_dfg(
    session: AsyncSession,
    case_type_id: uuid.UUID | None = None,
    days: int = 30,
    *,
    tenant_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Build directly-follows graph showing activity transitions."""
    since = datetime.now(timezone.utc) - timedelta(days=days)

    stmt = select(
        CaseEventLogModel.case_id,
        CaseEventLogModel.activity,
        CaseEventLogModel.timestamp,
    ).where(
        CaseEventLogModel.timestamp >= since,
    ).order_by(
        CaseEventLogModel.case_id, CaseEventLogModel.timestamp,
    )

    if tenant_id:
        stmt = stmt.where(CaseEventLogModel.case_type_id.in_(_tenant_case_type_ids(tenant_id)))
    if case_type_id:
        stmt = stmt.where(CaseEventLogModel.case_type_id == case_type_id)

    result = await session.execute(stmt)

    # Build transitions
    transitions: Counter = Counter()
    case_traces: dict[str, list[str]] = defaultdict(list)
    for row in result.all():
        case_traces[str(row.case_id)].append(row.activity)

    for trace in case_traces.values():
        for i in range(len(trace) - 1):
            transitions[(trace[i], trace[i + 1])] += 1

    # Build nodes + edges
    nodes = set()
    edges = []
    for (source, target), count in transitions.items():
        nodes.add(source)
        nodes.add(target)
        edges.append({"source": source, "target": target, "count": count})

    edges.sort(key=lambda e: e["count"], reverse=True)

    return {
        "nodes": sorted(nodes),
        "edges": edges,
        "total_cases": len(case_traces),
    }


# ─── Case duration analysis ─────────────────────────────────────

async def case_duration_stats(
    session: AsyncSession,
    case_type_id: uuid.UUID | None = None,
    days: int = 30,
    *,
    tenant_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Compute statistics on total case duration (creation to resolution)."""
    since = datetime.now(timezone.utc) - timedelta(days=days)

    stmt = select(
        CaseInstanceModel.id,
        CaseInstanceModel.created_at,
        CaseInstanceModel.resolved_at,
    ).where(
        CaseInstanceModel.created_at >= since,
        CaseInstanceModel.resolved_at.isnot(None),
    )
    if tenant_id:
        stmt = stmt.where(CaseInstanceModel.tenant_id == tenant_id)
    if case_type_id:
        stmt = stmt.where(CaseInstanceModel.case_type_id == case_type_id)

    result = await session.execute(stmt)
    durations = []
    for row in result.all():
        if row.resolved_at and row.created_at:
            # Ensure timezone-aware for subtraction
            r = row.resolved_at if row.resolved_at.tzinfo else row.resolved_at.replace(tzinfo=timezone.utc)
            c = row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=timezone.utc)
            durations.append((r - c).total_seconds())

    if not durations:
        return {
            "cases_analyzed": 0,
            "avg_duration_hours": 0, "median_duration_hours": 0,
            "min_duration_hours": 0, "max_duration_hours": 0,
        }

    durations.sort()
    n = len(durations)
    median = durations[n // 2]
    avg = sum(durations) / n

    return {
        "cases_analyzed": n,
        "avg_duration_hours": round(avg / 3600, 2),
        "median_duration_hours": round(median / 3600, 2),
        "min_duration_hours": round(durations[0] / 3600, 2),
        "max_duration_hours": round(durations[-1] / 3600, 2),
        "p95_duration_hours": round(durations[int(n * 0.95)] / 3600, 2) if n > 20 else None,
    }


# ─── Conformance checking ───────────────────────────────────────

async def check_conformance(
    session: AsyncSession,
    case_type_id: uuid.UUID,
    days: int = 30,
) -> dict[str, Any]:
    """Compare actual flow against the planned case type definition.

    Returns conformance metrics:
    - deviating_cases: cases that don't follow expected path
    - skipped_activities: planned activities that never happened
    - unexpected_activities: activities not in the plan
    """
    from case_service.db import repository as repo

    # Get planned activities from case type definition
    case_type = await repo.get_case_type(session, case_type_id)
    if case_type is None:
        return {"error": "Case type not found"}

    planned_activities = set()
    for stage in case_type.stages or []:
        planned_activities.add(stage.stage_id)
        for step in stage.steps or []:
            planned_activities.add(step.step_id)

    # Get actual activities
    since = datetime.now(timezone.utc) - timedelta(days=days)
    stmt = select(CaseEventLogModel.activity).where(
        CaseEventLogModel.case_type_id == case_type_id,
        CaseEventLogModel.timestamp >= since,
    ).distinct()
    result = await session.execute(stmt)
    actual_activities = set(r[0] for r in result.all())

    # Skipped vs unexpected
    skipped = planned_activities - actual_activities
    unexpected = actual_activities - planned_activities

    # Count deviating cases
    traces_stmt = select(
        CaseEventLogModel.case_id, CaseEventLogModel.activity,
    ).where(
        CaseEventLogModel.case_type_id == case_type_id,
        CaseEventLogModel.timestamp >= since,
    ).order_by(CaseEventLogModel.case_id, CaseEventLogModel.timestamp)
    result = await session.execute(traces_stmt)

    case_activities: dict[str, set] = defaultdict(set)
    for row in result.all():
        case_activities[str(row.case_id)].add(row.activity)

    deviating = 0
    total = len(case_activities)
    for activities in case_activities.values():
        if activities & unexpected or not activities.issubset(planned_activities):
            deviating += 1

    return {
        "total_cases_analyzed": total,
        "conforming_cases": total - deviating,
        "deviating_cases": deviating,
        "conformance_rate": round((total - deviating) / total * 100, 1) if total else 100.0,
        "planned_activities": sorted(planned_activities),
        "actual_activities": sorted(actual_activities),
        "skipped_activities": sorted(skipped),
        "unexpected_activities": sorted(unexpected),
    }
