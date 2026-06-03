"""Urgency calculator: computes work-item ordering scores.

The urgency score is a dimensionless float that determines how
high an item appears in work queues.  Higher = more urgent.

Default formula::

    urgency = (priority_weight × priority_value)
            + (sla_weight × sla_proximity_factor)
            + (age_weight × age_factor)
            + (relationship_weight × blocking_factor)

All weights and the formula itself can be overridden per work queue
via ``WorkQueueDefinition.urgency_formula``.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import (
    CaseAssignmentModel,
    CaseInstanceModel,
    CaseRelationshipModel,
    CaseSLAInstanceModel,
)

logger = logging.getLogger(__name__)


# ─── Priority mapping ─────────────────────────────────────────────

PRIORITY_VALUES: dict[str, int] = {
    "low": 10,
    "medium": 20,
    "high": 30,
    "critical": 40,
    "blocker": 50,
}

# ─── Default weights ──────────────────────────────────────────────

DEFAULT_WEIGHTS: dict[str, float] = {
    "priority": 1.0,
    "sla": 2.0,
    "age": 0.1,
    "relationship": 1.5,
}


# ─── Factor computations ─────────────────────────────────────────


def compute_priority_factor(priority: str) -> float:
    """Map a priority string to its numeric weight."""
    return float(PRIORITY_VALUES.get(priority, 20))


def compute_sla_proximity_factor(
    sla_instances: list[dict[str, Any]],
    now: datetime | None = None,
) -> float:
    """Compute how close the most urgent SLA is to breach.

    Returns a value between 0.0 (just started) and 1.0+ (at or past deadline).
    If no SLAs exist, returns 0.0.
    """
    if not sla_instances:
        return 0.0

    now = now or datetime.now(timezone.utc)
    worst = 0.0

    for sla in sla_instances:
        status = sla.get("status", "on_track")
        if status == "paused":
            continue
        if status == "breached":
            worst = max(worst, 1.5)  # breached items get max urgency
            continue

        started_at = sla.get("started_at")
        deadline_at = sla.get("deadline_at")
        if not started_at or not deadline_at:
            continue

        # Parse if strings
        if isinstance(started_at, str):
            started_at = datetime.fromisoformat(started_at)
        if isinstance(deadline_at, str):
            deadline_at = datetime.fromisoformat(deadline_at)

        # Ensure timezone-aware
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        if deadline_at.tzinfo is None:
            deadline_at = deadline_at.replace(tzinfo=timezone.utc)

        total = (deadline_at - started_at).total_seconds()
        if total <= 0:
            worst = max(worst, 1.0)
            continue

        elapsed = (now - started_at).total_seconds()
        # Subtract paused time
        paused_secs = sla.get("paused_duration_seconds", 0)
        elapsed = max(0, elapsed - paused_secs)

        proximity = elapsed / total
        worst = max(worst, proximity)

    return worst


def compute_age_factor(
    created_at: datetime | str | None,
    now: datetime | None = None,
) -> float:
    """Compute age factor: hours since creation / 24."""
    if created_at is None:
        return 0.0

    now = now or datetime.now(timezone.utc)
    if isinstance(created_at, str):
        created_at = datetime.fromisoformat(created_at)

    # Ensure both are timezone-aware for subtraction
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    hours = (now - created_at).total_seconds() / 3600.0
    return max(0.0, hours / 24.0)


def compute_blocking_factor(blocking_count: int) -> float:
    """Each case blocked by this one adds 10 to the factor."""
    return float(blocking_count * 10)


# ─── Main computation ─────────────────────────────────────────────


def compute_urgency(
    *,
    priority: str = "medium",
    sla_instances: list[dict[str, Any]] | None = None,
    created_at: datetime | str | None = None,
    blocking_count: int = 0,
    weights: dict[str, float] | None = None,
    now: datetime | None = None,
) -> float:
    """Compute the urgency score for a case.

    Parameters
    ----------
    priority : str
        Case priority (low/medium/high/critical/blocker).
    sla_instances : list
        SLA instance dicts with status, started_at, deadline_at, etc.
    created_at : datetime or str
        When the case was created.
    blocking_count : int
        How many other cases this case is blocking.
    weights : dict, optional
        Override default weights.  Keys: priority, sla, age, relationship.
    now : datetime, optional
        Override current time (for testing).

    Returns
    -------
    float
        Urgency score (higher = more urgent).
    """
    w = {**DEFAULT_WEIGHTS, **(weights or {})}

    pf = compute_priority_factor(priority)
    sf = compute_sla_proximity_factor(sla_instances or [], now=now)
    af = compute_age_factor(created_at, now=now)
    bf = compute_blocking_factor(blocking_count)

    score = (
        w["priority"] * pf
        + w["sla"] * sf
        + w["age"] * af
        + w["relationship"] * bf
    )

    return round(score, 4)


# ─── Database integration ─────────────────────────────────────────


async def refresh_urgency_for_case(
    session: AsyncSession, case_id: uuid.UUID
) -> float:
    """Recompute and persist the urgency score for a single case."""
    # Load case
    stmt = select(CaseInstanceModel).where(
        CaseInstanceModel.id == case_id
    )
    result = await session.execute(stmt)
    case = result.scalar_one_or_none()
    if case is None:
        return 0.0

    # Load SLAs
    sla_stmt = select(CaseSLAInstanceModel).where(
        CaseSLAInstanceModel.case_id == case_id
    )
    sla_result = await session.execute(sla_stmt)
    sla_rows = sla_result.scalars().all()
    sla_dicts = [
        {
            "status": s.status,
            "started_at": s.started_at,
            "deadline_at": s.deadline_at,
            "paused_duration_seconds": s.paused_duration_seconds,
        }
        for s in sla_rows
    ]

    # Count blocking relationships
    block_stmt = select(func.count()).select_from(
        CaseRelationshipModel
    ).where(
        CaseRelationshipModel.source_case_id == case_id,
        CaseRelationshipModel.relationship_type == "blocking",
    )
    blocking_count = (await session.execute(block_stmt)).scalar_one()

    score = compute_urgency(
        priority=case.priority,
        sla_instances=sla_dicts,
        created_at=case.created_at,
        blocking_count=blocking_count,
    )

    # Persist
    upd = (
        update(CaseInstanceModel)
        .where(CaseInstanceModel.id == case_id)
        .values(urgency_score=score)
    )
    await session.execute(upd)

    logger.debug("Urgency for case %s = %.4f", case_id, score)
    return score


async def refresh_urgency_batch(
    session: AsyncSession,
    case_ids: list[uuid.UUID] | None = None,
) -> int:
    """Recompute urgency for multiple cases (or all open cases).

    Returns the number of cases updated.
    """
    if case_ids is None:
        stmt = select(CaseInstanceModel.id).where(
            CaseInstanceModel.status.in_(["new", "open", "reopened"])
        )
        result = await session.execute(stmt)
        case_ids = [row[0] for row in result.all()]

    count = 0
    for cid in case_ids:
        await refresh_urgency_for_case(session, cid)
        count += 1

    logger.info("Refreshed urgency for %d cases", count)
    return count
