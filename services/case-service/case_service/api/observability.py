"""HELIX P23 — /health and /api/v1/observability/* endpoints."""
from __future__ import annotations
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.observability import render_metrics, recent_spans, get_logger

log = get_logger("helix.observability.api")
router = APIRouter(tags=["observability"])


from case_service.db.session import get_session


@router.get("/metrics", include_in_schema=False)
async def prometheus_metrics() -> Response:
    body, ct = render_metrics()
    return Response(content=body, media_type=ct)


@router.get("/health")
async def health_shallow() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "case-service",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/health/deep")
async def health_deep(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    components: list[dict[str, Any]] = []

    db_ok, db_latency, db_err = True, None, None
    if session is not None:
        start = time.perf_counter()
        try:
            await session.execute(select(1))
            db_latency = round((time.perf_counter() - start) * 1000.0, 2)
        except Exception as e:
            db_ok = False
            db_err = str(e)[:200]
    else:
        db_ok = False
        db_err = "no session dependency available"
    components.append({
        "component": "database",
        "status": "ok" if db_ok else "down",
        "latency_ms": db_latency,
        "detail": {"error": db_err} if db_err else {},
    })

    components.append({
        "component": "telemetry",
        "status": "ok",
        "latency_ms": 0.0,
        "detail": {"recent_spans": len(recent_spans(limit=500))},
    })

    try:
        from case_service.db.models import HealthCheckResultModel
        if session is not None:
            for c in components:
                session.add(HealthCheckResultModel(
                    id=uuid.uuid4(),
                    component=c["component"],
                    status=c["status"],
                    latency_ms=c["latency_ms"],
                    detail=c["detail"],
                ))
            await session.commit()
    except Exception as e:
        log.warning("health_deep_persist_failed", error=str(e))

    overall = "ok" if all(c["status"] == "ok" for c in components) else "degraded"
    return {
        "status": overall,
        "components": components,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/api/v1/observability/metrics")
async def metrics_summary() -> dict[str, Any]:
    from case_service.observability.metrics import (
        http_requests_total, http_request_duration_seconds,
    )

    total_requests = 0
    error_requests = 0
    by_path: dict[str, dict[str, Any]] = {}

    for sample in http_requests_total.collect():
        for s in sample.samples:
            if s.name.endswith("_total"):
                total_requests += int(s.value)
                status = s.labels.get("status", "200")
                path = s.labels.get("path", "?")
                slot = by_path.setdefault(path, {"count": 0, "errors": 0})
                slot["count"] += int(s.value)
                if status.startswith(("4", "5")):
                    error_requests += int(s.value)
                    slot["errors"] += int(s.value)

    latency: dict[str, dict[str, float]] = {}
    for sample in http_request_duration_seconds.collect():
        path_buckets: dict[str, list[tuple[float, float]]] = {}
        path_sum: dict[str, float] = {}
        path_count: dict[str, float] = {}
        for s in sample.samples:
            path = s.labels.get("path", "?")
            if s.name.endswith("_bucket"):
                le = s.labels.get("le", "+Inf")
                le_val = float("inf") if le == "+Inf" else float(le)
                path_buckets.setdefault(path, []).append((le_val, s.value))
            elif s.name.endswith("_sum"):
                path_sum[path] = s.value
            elif s.name.endswith("_count"):
                path_count[path] = s.value
        for path, buckets in path_buckets.items():
            buckets.sort(key=lambda x: x[0])
            count = path_count.get(path, 0)
            if count <= 0:
                continue
            p50 = _quantile_from_buckets(buckets, count, 0.50)
            p95 = _quantile_from_buckets(buckets, count, 0.95)
            avg = (path_sum.get(path, 0) / count) if count else 0
            latency[path] = {
                "avg_ms": round(avg * 1000, 2),
                "p50_ms": round(p50 * 1000, 2),
                "p95_ms": round(p95 * 1000, 2),
                "count": int(count),
            }

    slowest = sorted(
        [{"path": p, **v} for p, v in latency.items()],
        key=lambda x: x["avg_ms"], reverse=True,
    )[:10]

    error_rate = (error_requests / total_requests) if total_requests else 0.0
    return {
        "total_requests": total_requests,
        "error_requests": error_requests,
        "error_rate": round(error_rate, 4),
        "by_path": by_path,
        "latency": latency,
        "slowest_endpoints": slowest,
    }


def _quantile_from_buckets(buckets, count, q):
    if not buckets or count <= 0:
        return 0.0
    target = q * count
    prev_le, prev_c = 0.0, 0.0
    for le, c in buckets:
        if c >= target:
            if le == float("inf"):
                return prev_le
            if c == prev_c:
                return le
            frac = (target - prev_c) / (c - prev_c)
            return prev_le + (le - prev_le) * frac
        prev_le, prev_c = le, c
    return buckets[-1][0]


@router.get("/api/v1/observability/traces/recent")
async def recent_traces(limit: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
    spans = recent_spans(limit=limit)
    return {"count": len(spans), "spans": spans}


@router.get("/api/v1/observability/events")
async def list_events(
    session: AsyncSession = Depends(get_session),
    limit: int = Query(100, ge=1, le=500),
    severity: str | None = None,
    event_type: str | None = None,
) -> dict[str, Any]:
    from case_service.db.models import TelemetryEventModel
    q = select(TelemetryEventModel).order_by(TelemetryEventModel.created_at.desc()).limit(limit)
    if severity:
        q = q.where(TelemetryEventModel.severity == severity)
    if event_type:
        q = q.where(TelemetryEventModel.event_type == event_type)
    res = await session.execute(q)
    rows = res.scalars().all()
    return {
        "count": len(rows),
        "events": [
            {
                "id": str(r.id),
                "event_type": r.event_type,
                "severity": r.severity,
                "payload": r.payload,
                "request_id": r.request_id,
                "trace_id": r.trace_id,
                "tenant_id": r.tenant_id,
                "user_id": r.user_id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }


@router.get("/api/v1/observability/health-history")
async def health_history(
    session: AsyncSession = Depends(get_session),
    hours: int = Query(24, ge=1, le=168),
    component: str | None = None,
) -> dict[str, Any]:
    from case_service.db.models import HealthCheckResultModel
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    q = (
        select(HealthCheckResultModel)
        .where(HealthCheckResultModel.checked_at >= since)
        .order_by(HealthCheckResultModel.checked_at.desc())
        .limit(1000)
    )
    if component:
        q = q.where(HealthCheckResultModel.component == component)
    res = await session.execute(q)
    rows = res.scalars().all()
    return {
        "count": len(rows),
        "results": [
            {
                "component": r.component,
                "status": r.status,
                "latency_ms": r.latency_ms,
                "detail": r.detail,
                "checked_at": r.checked_at.isoformat() if r.checked_at else None,
            }
            for r in rows
        ],
    }
