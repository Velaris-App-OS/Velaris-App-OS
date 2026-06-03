"""Security event logger — structured audit for SOC2 compliance.

Records every security-relevant event: authentication, authorization,
data access, data changes, configuration changes.

Copyright (c) 2024-2025 HELIX Contributors
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
) -> None:
    """Log a security event."""
    from case_service.db.models import SecurityEventModel

    event = SecurityEventModel(
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
    )
    session.add(event)
    await session.flush()


async def query_events(
    session: AsyncSession,
    *,
    event_type: str | None = None,
    user_id: str | None = None,
    resource_type: str | None = None,
    severity: str | None = None,
    days: int = 30,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Query security events with filters."""
    from case_service.db.models import SecurityEventModel

    since = datetime.now(timezone.utc) - timedelta(days=days)
    stmt = select(SecurityEventModel).where(
        SecurityEventModel.timestamp >= since,
    ).order_by(SecurityEventModel.timestamp.desc()).limit(limit)

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
        }
        for e in result.scalars().all()
    ]


async def event_stats(
    session: AsyncSession, days: int = 30,
) -> dict[str, Any]:
    """Aggregate security event statistics."""
    from case_service.db.models import SecurityEventModel

    since = datetime.now(timezone.utc) - timedelta(days=days)

    # Count by type
    type_stmt = select(
        SecurityEventModel.event_type, func.count().label("count"),
    ).where(
        SecurityEventModel.timestamp >= since,
    ).group_by(SecurityEventModel.event_type)
    type_result = await session.execute(type_stmt)
    by_type = {r.event_type: r.count for r in type_result.all()}

    # Count by severity
    sev_stmt = select(
        SecurityEventModel.severity, func.count().label("count"),
    ).where(
        SecurityEventModel.timestamp >= since,
    ).group_by(SecurityEventModel.severity)
    sev_result = await session.execute(sev_stmt)
    by_severity = {r.severity: r.count for r in sev_result.all()}

    # Failed events (denied/error outcomes)
    failed_stmt = select(func.count()).select_from(SecurityEventModel).where(
        SecurityEventModel.timestamp >= since,
        SecurityEventModel.outcome.in_(["denied", "error"]),
    )
    failed = (await session.execute(failed_stmt)).scalar_one()

    return {
        "period_days": days,
        "by_type": by_type,
        "by_severity": by_severity,
        "failed_count": failed,
        "total": sum(by_type.values()),
    }
