"""Security event logger — structured audit for SOC2 compliance.

Records every security-relevant event: authentication, authorization,
data access, data changes, configuration changes.

Lives in the shared db layer, not in `enterprise/`, since 13b. HxGuard writes
its deny audit here, and HxGuard is shared — that one import was the whole of
the recorded coupling debt that made `velaris_shared` un-liftable. The module
never needed `enterprise`: every function below reads exactly one model.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


# ── Detection hook (roadmap #25) ──────────────────────────────────────────────
#
# HxShield's engine had eight detection patterns and exactly one caller: the
# explicit `POST /hxshield/score`. Nothing on a request path ever asked it, so
# on 2026-08-24 the live platform held **3,905 security events, 2 enabled rules
# and 0 incidents**. The detector was not broken — it was never wired.
#
# This is the wire, and it goes here because this is the chokepoint: 51 call
# sites already funnel through `log_security_event`, so one hook gives all of
# them detection instead of 51 edits that would drift.
#
# It is a REGISTRY rather than an import because `velaris_shared` must never
# import an application — `tests/phase_arch/test_import_boundaries.py` enforces
# that, and HxShield lives in case-service. The application registers itself at
# startup; a service that does not (analytics, hxgraph, hxmigrate) simply has no
# detector, and this stays a no-op.
_DETECTOR: Any = None


def register_detector(fn) -> None:
    """Install the detection callback. Called once, from a service's lifespan."""
    global _DETECTOR  # noqa: PLW0603
    _DETECTOR = fn


def clear_detector() -> None:
    """Uninstall it — for tests, and for a clean shutdown."""
    global _DETECTOR  # noqa: PLW0603
    _DETECTOR = None


def has_detector() -> bool:
    return _DETECTOR is not None


#: Detections started and not yet finished. Kept so a task is not collected
#: mid-flight, and so tests and shutdown can wait for them (drain_detections).
_PENDING: set = set()
#: Beyond this many unfinished detections an event is recorded but not scored,
#: with a warning — a flood must not grow memory without bound.
MAX_PENDING_DETECTIONS = 5000


async def _run_detector(**kw: Any) -> None:
    try:
        await _DETECTOR(**kw)
    except Exception:  # noqa: BLE001
        logger.exception("HxShield detection failed — event was still recorded")


async def drain_detections() -> None:
    """Wait for every detection started so far (tests, shutdown)."""
    import asyncio

    while _PENDING:
        await asyncio.gather(*list(_PENDING), return_exceptions=True)


async def log_security_event(
    session: AsyncSession,
    *,
    event_type: str,
    severity: str = "info",
    user_id: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    action: str | None = None,
    outcome: str = "success",
    ip_address: str | None = None,
    user_agent: str | None = None,
    details: dict[str, Any] | None = None,
    tenant_id: str | None = None,
    repeat_count: int = 1,
) -> None:
    """Log a security event. Its tenant is the signed-in request's trusted one
    unless given (2026-11-04); none = the platform's. `repeat_count` > 1 = one
    row standing for that many identical events (migration 231)."""
    from .models import SecurityEventModel

    if tenant_id is None:
        from velaris_shared.observability.context import tenant_id_var
        tenant_id = tenant_id_var.get()
    event = SecurityEventModel(
        tenant_id=tenant_id,
        event_type=event_type,
        severity=severity,
        user_id=user_id,
        resource_type=resource_type,
        resource_id=resource_id,
        action=action,
        outcome=outcome,
        ip_address=ip_address,
        user_agent=user_agent,
        details=details or {},
        repeat_count=repeat_count,
    )
    session.add(event)
    await session.flush()

    # Detection runs after the event is durable, and never at its expense.
    #
    # Fail-open, deliberately and without apology: this function is called from
    # inside login, authorization denials and case mutations. A detector that
    # can raise turns "we noticed something" into "the request failed", which is
    # a worse outcome than a missed detection and is exactly how a security
    # feature becomes the thing that takes the platform down.
    #
    # Started as its own task, not awaited (2026-10-19). The detector opens its
    # own session; awaited here, the caller sat on a connection in an open
    # transaction while waiting for that second one, and under load every
    # connection was held by a caller waiting for another — the audit writes
    # deadlocked the pool. One difference: awaited, it ran before the caller
    # committed, so its history queries could not see this event; now they may,
    # when the caller commits first.
    if _DETECTOR is not None:
        import asyncio

        if len(_PENDING) >= MAX_PENDING_DETECTIONS:
            logger.warning("HxShield: %d detections pending — %s recorded, not scored",
                           len(_PENDING), event_type)
            return
        task = asyncio.ensure_future(_run_detector(
            event_type=event_type,
            actor_id=user_id,
            resource_type=resource_type,
            resource_id=resource_id,
            action=action,
            outcome=outcome,
            severity=severity,
            ip_address=ip_address,
            details=details or {},
        ))
        _PENDING.add(task)
        task.add_done_callback(_PENDING.discard)


async def query_events(
    session: AsyncSession,
    *,
    event_type: str | None = None,
    user_id: str | None = None,
    resource_type: str | None = None,
    severity: str | None = None,
    days: int = 30,
    limit: int = 100,
    tenants: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Query security events with filters. `tenants` (2026-11-04): only these
    tenant spellings — a tenant's admin; None = every event (the operator)."""
    from .models import SecurityEventModel

    since = datetime.now(timezone.utc) - timedelta(days=days)
    stmt = select(SecurityEventModel).where(
        SecurityEventModel.timestamp >= since,
    ).order_by(SecurityEventModel.timestamp.desc()).limit(limit)
    if tenants is not None:
        stmt = stmt.where(SecurityEventModel.tenant_id.in_(tenants))

    if event_type:
        stmt = stmt.where(SecurityEventModel.event_type == event_type)
    if user_id:
        stmt = stmt.where(SecurityEventModel.user_id == user_id)
    if resource_type:
        stmt = stmt.where(SecurityEventModel.resource_type == resource_type)
    if severity:
        stmt = stmt.where(SecurityEventModel.severity == severity)

    result = await session.execute(stmt)
    return [
        {
            "id": str(e.id),
            "event_type": e.event_type,
            "severity": e.severity,
            "user_id": e.user_id,
            "resource_type": e.resource_type,
            "resource_id": e.resource_id,
            "ip_address": e.ip_address,
            "action": e.action,
            "outcome": e.outcome,
            "details": e.details or {},
            "timestamp": e.timestamp.isoformat() if e.timestamp else None,
            "repeat_count": e.repeat_count or 1,
        }
        for e in result.scalars().all()
    ]


async def event_stats(
    session: AsyncSession, days: int = 30, tenants: list[str] | None = None,
) -> dict[str, Any]:
    """Aggregate security event statistics — `tenants` as in `query_events`."""
    from .models import SecurityEventModel

    since = datetime.now(timezone.utc) - timedelta(days=days)
    scope = (SecurityEventModel.tenant_id.in_(tenants),) if tenants is not None else ()

    # SUM(repeat_count), not COUNT(*): one row can stand for many refused
    # repeats (migration 231). Every older row is 1.
    n = func.coalesce(func.sum(SecurityEventModel.repeat_count), 0)

    # Count by type
    type_stmt = select(
        SecurityEventModel.event_type, n.label("count"),
    ).where(
        SecurityEventModel.timestamp >= since, *scope,
    ).group_by(SecurityEventModel.event_type)
    type_result = await session.execute(type_stmt)
    by_type = {r.event_type: int(r.count) for r in type_result.all()}

    # Count by severity
    sev_stmt = select(
        SecurityEventModel.severity, n.label("count"),
    ).where(
        SecurityEventModel.timestamp >= since, *scope,
    ).group_by(SecurityEventModel.severity)
    sev_result = await session.execute(sev_stmt)
    by_severity = {r.severity: int(r.count) for r in sev_result.all()}

    # Failed events (denied/error outcomes)
    failed_stmt = select(n).select_from(SecurityEventModel).where(
        SecurityEventModel.timestamp >= since,
        SecurityEventModel.outcome.in_(["denied", "error"]), *scope,
    )
    failed = int((await session.execute(failed_stmt)).scalar_one())

    return {
        "period_days": days,
        "by_type": by_type,
        "by_severity": by_severity,
        "failed_count": failed,
        "total": sum(by_type.values()),
    }
