"""SLA tracker: default implementation of the SLAEngine protocol.

Manages the lifecycle of SLA instances — starting, pausing, resuming,
checking, and handling breaches.  Temporal workflows call these
functions as activities.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db import repository as repo
from case_service.core.business_calendar import BusinessCalendar
from case_service.db.models import CaseSLAInstanceModel

logger = logging.getLogger(__name__)


# ─── Timezone helper ─────────────────────────────────────────────

def _ensure_aware(dt: datetime | None) -> datetime | None:
    """Ensure a datetime is timezone-aware (SQLite returns naive)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ─── ISO 8601 duration parser ────────────────────────────────────

_DURATION_RE = re.compile(
    r"^P"
    r"(?:(\d+)Y)?"
    r"(?:(\d+)M)?"
    r"(?:(\d+)D)?"
    r"(?:T"
    r"(?:(\d+)H)?"
    r"(?:(\d+)M)?"
    r"(?:(\d+(?:\.\d+)?)S)?"
    r")?$"
)


def parse_iso8601_duration(duration: str) -> timedelta:
    """Parse an ISO 8601 duration string into a timedelta.

    Supports: ``P1D``, ``PT4H``, ``P2DT6H30M``, ``PT30S``, etc.
    Years and months are approximated (365d and 30d respectively).

    Raises ``ValueError`` if the string is not valid.
    """
    m = _DURATION_RE.match(duration)
    if not m:
        raise ValueError(f"Invalid ISO 8601 duration: {duration!r}")

    years = int(m.group(1) or 0)
    months = int(m.group(2) or 0)
    days = int(m.group(3) or 0)
    hours = int(m.group(4) or 0)
    minutes = int(m.group(5) or 0)
    seconds = float(m.group(6) or 0)

    total_days = years * 365 + months * 30 + days
    return timedelta(
        days=total_days,
        hours=hours,
        minutes=minutes,
        seconds=seconds,
    )


def duration_to_seconds(duration: str) -> int:
    """Convert an ISO 8601 duration to total seconds."""
    return int(parse_iso8601_duration(duration).total_seconds())


# ─── SLA operations ──────────────────────────────────────────────


async def start_sla(
    session: AsyncSession,
    *,
    case_id: uuid.UUID,
    sla_policy: dict[str, Any],
    target_id: str,
    now: datetime | None = None,
) -> CaseSLAInstanceModel:
    """Start tracking an SLA for a case or stage.

    Creates the SLA instance record with computed goal and deadline
    timestamps.

    Parameters
    ----------
    case_id : UUID
        The case this SLA belongs to.
    sla_policy : dict
        The SLAPolicy IR dict (must have ``id``, ``goal_duration``,
        ``deadline_duration``).
    target_id : str
        What the SLA targets — either the case ID (for case-level SLAs)
        or a stage ID.
    now : datetime, optional
        Override start time (for testing).
    """
    now = now or datetime.now(timezone.utc)

    goal_td = parse_iso8601_duration(sla_policy["goal_duration"])
    deadline_td = parse_iso8601_duration(sla_policy["deadline_duration"])

    sla = await repo.create_sla_instance(
        session,
        data={
            "case_id": case_id,
            "sla_policy_id": sla_policy["id"],
            "target_id": target_id,
            "status": "on_track",
            "started_at": now,
            "goal_at": now + goal_td,
            "deadline_at": now + deadline_td,
        },
    )

    await repo.append_audit_entry(
        session,
        data={
            "case_id": case_id,
            "action": "sla_started",
            "actor_type": "system",
            "details": {
                "sla_policy_id": sla_policy["id"],
                "target_id": target_id,
                "goal_at": str(now + goal_td),
                "deadline_at": str(now + deadline_td),
            },
        },
    )

    logger.info(
        "SLA started: case=%s policy=%s goal=%s deadline=%s",
        case_id,
        sla_policy["id"],
        now + goal_td,
        now + deadline_td,
    )
    return sla


async def pause_sla(
    session: AsyncSession,
    *,
    case_id: uuid.UUID,
    sla_policy_id: str,
    now: datetime | None = None,
) -> bool:
    """Pause an active SLA clock.

    Returns True if the SLA was paused, False if not found or already paused.
    """
    now = now or datetime.now(timezone.utc)

    stmt = select(CaseSLAInstanceModel).where(
        CaseSLAInstanceModel.case_id == case_id,
        CaseSLAInstanceModel.sla_policy_id == sla_policy_id,
        CaseSLAInstanceModel.status.in_(["on_track", "at_risk"]),
    )
    result = await session.execute(stmt)
    sla = result.scalar_one_or_none()
    if sla is None:
        return False

    await repo.update_sla_instance(
        session,
        sla.id,
        values={"status": "paused", "paused_at": now},
    )

    await repo.append_audit_entry(
        session,
        data={
            "case_id": case_id,
            "action": "sla_paused",
            "actor_type": "system",
            "details": {"sla_policy_id": sla_policy_id},
        },
    )
    return True


async def resume_sla(
    session: AsyncSession,
    *,
    case_id: uuid.UUID,
    sla_policy_id: str,
    now: datetime | None = None,
) -> bool:
    """Resume a paused SLA clock.

    Adjusts goal_at and deadline_at forward by the paused duration.
    Returns True if resumed, False if not found or not paused.
    """
    now = now or datetime.now(timezone.utc)

    stmt = select(CaseSLAInstanceModel).where(
        CaseSLAInstanceModel.case_id == case_id,
        CaseSLAInstanceModel.sla_policy_id == sla_policy_id,
        CaseSLAInstanceModel.status == "paused",
    )
    result = await session.execute(stmt)
    sla = result.scalar_one_or_none()
    if sla is None:
        return False

    paused_secs = 0
    if sla.paused_at:
        paused_at = _ensure_aware(sla.paused_at)
        paused_secs = int((now - paused_at).total_seconds())

    total_paused = sla.paused_duration_seconds + paused_secs
    shift = timedelta(seconds=paused_secs)

    goal_at = _ensure_aware(sla.goal_at)
    deadline_at = _ensure_aware(sla.deadline_at)

    await repo.update_sla_instance(
        session,
        sla.id,
        values={
            "status": "on_track",
            "paused_at": None,
            "paused_duration_seconds": total_paused,
            "goal_at": goal_at + shift,
            "deadline_at": deadline_at + shift,
        },
    )

    await repo.append_audit_entry(
        session,
        data={
            "case_id": case_id,
            "action": "sla_resumed",
            "actor_type": "system",
            "details": {
                "sla_policy_id": sla_policy_id,
                "paused_seconds": paused_secs,
                "total_paused_seconds": total_paused,
            },
        },
    )
    return True


async def check_sla(
    session: AsyncSession,
    *,
    case_id: uuid.UUID,
    sla_policy_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Check current SLA status, updating if thresholds crossed.

    Returns a dict with ``status``, ``remaining_seconds``, ``elapsed_seconds``.
    """
    now = now or datetime.now(timezone.utc)

    stmt = select(CaseSLAInstanceModel).where(
        CaseSLAInstanceModel.case_id == case_id,
        CaseSLAInstanceModel.sla_policy_id == sla_policy_id,
    )
    result = await session.execute(stmt)
    sla = result.scalar_one_or_none()
    if sla is None:
        return {"error": "SLA not found"}

    if sla.status in ("paused", "breached"):
        return {
            "status": sla.status,
            "sla_policy_id": sla_policy_id,
            "remaining_seconds": None,
            "elapsed_seconds": None,
        }

    total = (_ensure_aware(sla.deadline_at) - _ensure_aware(sla.started_at)).total_seconds()
    elapsed = (now - _ensure_aware(sla.started_at)).total_seconds() - sla.paused_duration_seconds
    remaining = max(0, total - elapsed - sla.paused_duration_seconds)

    # Check for breach
    new_status = sla.status
    if now >= _ensure_aware(sla.deadline_at):
        new_status = "breached"
    elif now >= _ensure_aware(sla.goal_at):
        new_status = "at_risk"

    if new_status != sla.status:
        await repo.update_sla_instance(
            session,
            sla.id,
            values={
                "status": new_status,
                "breached_at": now if new_status == "breached" else None,
            },
        )
        await repo.append_audit_entry(
            session,
            data={
                "case_id": case_id,
                "action": f"sla_{new_status}",
                "actor_type": "system",
                "details": {"sla_policy_id": sla_policy_id},
            },
        )

    return {
        "status": new_status,
        "sla_policy_id": sla_policy_id,
        "remaining_seconds": int(remaining),
        "elapsed_seconds": int(elapsed),
        "goal_at": str(sla.goal_at),
        "deadline_at": str(sla.deadline_at),
    }


async def evaluate_all_slas(
    session: AsyncSession,
    case_id: uuid.UUID,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Check all SLA instances for a case and return their statuses."""
    stmt = select(CaseSLAInstanceModel).where(
        CaseSLAInstanceModel.case_id == case_id
    )
    result = await session.execute(stmt)
    sla_rows = result.scalars().all()

    results = []
    for sla in sla_rows:
        status = await check_sla(
            session,
            case_id=case_id,
            sla_policy_id=sla.sla_policy_id,
            now=now,
        )
        results.append(status)
    return results


async def cancel_slas_for_case(
    session: AsyncSession,
    case_id: uuid.UUID,
) -> int:
    """Cancel all active SLAs for a case (on resolve/close/cancel).

    Returns the number of SLAs cancelled.
    """
    stmt = select(CaseSLAInstanceModel).where(
        CaseSLAInstanceModel.case_id == case_id,
        CaseSLAInstanceModel.status.in_(["on_track", "at_risk", "paused"]),
    )
    result = await session.execute(stmt)
    rows = result.scalars().all()

    for sla in rows:
        await repo.update_sla_instance(
            session,
            sla.id,
            values={"status": "cancelled"},
        )

    if rows:
        logger.info("Cancelled %d SLAs for case %s", len(rows), case_id)
    return len(rows)


async def start_sla_with_calendar(
    session: AsyncSession,
    *,
    case_id: uuid.UUID,
    sla_policy: dict[str, Any],
    target_id: str,
    calendar: BusinessCalendar | None = None,
    now: datetime | None = None,
) -> CaseSLAInstanceModel:
    """Start SLA using business calendar for deadline computation.

    If calendar is None, falls back to wall-clock (24/7).
    """
    now = now or datetime.now(timezone.utc)

    goal_td = parse_iso8601_duration(sla_policy["goal_duration"])
    deadline_td = parse_iso8601_duration(sla_policy["deadline_duration"])

    if calendar:
        goal_at = calendar.add_business_duration(now, goal_td)
        deadline_at = calendar.add_business_duration(now, deadline_td)
    else:
        goal_at = now + goal_td
        deadline_at = now + deadline_td

    sla = await repo.create_sla_instance(
        session,
        data={
            "case_id": case_id,
            "sla_policy_id": sla_policy["id"],
            "target_id": target_id,
            "status": "on_track",
            "started_at": now,
            "goal_at": goal_at,
            "deadline_at": deadline_at,
        },
    )

    await repo.append_audit_entry(
        session,
        data={
            "case_id": case_id,
            "action": "sla_started",
            "actor_type": "system",
            "details": {
                "sla_policy_id": sla_policy["id"],
                "target_id": target_id,
                "goal_at": str(goal_at),
                "deadline_at": str(deadline_at),
                "calendar": calendar.name if calendar else "24x7",
            },
        },
    )
    return sla


async def check_sla_with_calendar(
    session: AsyncSession,
    *,
    case_id: uuid.UUID,
    sla_policy_id: str,
    calendar: BusinessCalendar | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Check SLA status using business calendar for elapsed computation."""
    now = now or datetime.now(timezone.utc)

    stmt = select(CaseSLAInstanceModel).where(
        CaseSLAInstanceModel.case_id == case_id,
        CaseSLAInstanceModel.sla_policy_id == sla_policy_id,
    )
    result = await session.execute(stmt)
    sla = result.scalar_one_or_none()
    if sla is None:
        return {"error": "SLA not found"}

    if sla.status in ("paused", "breached"):
        return {"status": sla.status, "sla_policy_id": sla_policy_id}

    started_at = _ensure_aware(sla.started_at)
    goal_at = _ensure_aware(sla.goal_at)
    deadline_at = _ensure_aware(sla.deadline_at)

    if calendar:
        elapsed = calendar.business_seconds_between(started_at, now) - sla.paused_duration_seconds
    else:
        elapsed = (now - started_at).total_seconds() - sla.paused_duration_seconds

    new_status = sla.status
    if now >= deadline_at:
        new_status = "breached"
    elif now >= goal_at:
        new_status = "at_risk"

    if new_status != sla.status:
        await repo.update_sla_instance(
            session, sla.id,
            values={
                "status": new_status,
                "breached_at": now if new_status == "breached" else None,
            },
        )

    return {
        "status": new_status,
        "sla_policy_id": sla_policy_id,
        "elapsed_seconds": int(elapsed),
        "goal_at": str(goal_at),
        "deadline_at": str(deadline_at),
    }



# ═══ P34: SLA start with escalation tree snapshot ══════════════════

async def start_sla_v2(
    session: AsyncSession,
    *,
    case_id: uuid.UUID,
    case_type_id: uuid.UUID | None,
    sla_policy: dict,
    target_id: str,
    calendar: BusinessCalendar | None = None,
    tenant_id: str | None = None,
    now: datetime | None = None,
) -> CaseSLAInstanceModel:
    """Start SLA with business-hour math + escalation tree snapshot.

    Resolves the escalation tree (case-type-specific preferred; global fallback)
    and freezes a snapshot onto the SLA instance so later tree edits don't
    affect this in-flight SLA.
    """
    from case_service.core.sla_escalation import (
        resolve_escalation_tree_for_policy, snapshot_tree, precompute_level_schedule,
    )

    now = now or datetime.now(timezone.utc)
    goal_td = parse_iso8601_duration(sla_policy["goal_duration"])
    deadline_td = parse_iso8601_duration(sla_policy["deadline_duration"])

    if calendar:
        goal_at = calendar.add_business_duration(now, goal_td)
        deadline_at = calendar.add_business_duration(now, deadline_td)
    else:
        goal_at = now + goal_td
        deadline_at = now + deadline_td

    tree = await resolve_escalation_tree_for_policy(
        session, case_type_id, sla_policy, tenant_id,
    )
    snapshot = snapshot_tree(tree) if tree else {"tree_id": None, "levels": []}
    schedule = precompute_level_schedule(snapshot, now, goal_at, deadline_at)
    snapshot["schedule"] = schedule

    sla = await repo.create_sla_instance(
        session,
        data={
            "case_id": case_id,
            "sla_policy_id": sla_policy["id"],
            "target_id": target_id,
            "status": "on_track",
            "started_at": now,
            "goal_at": goal_at,
            "deadline_at": deadline_at,
            "escalation_tree_snapshot": snapshot,
            "escalation_history": [],
            "pause_reasons_log": [],
            "escalation_level": 0,
        },
    )

    await repo.append_audit_entry(
        session,
        data={
            "case_id": case_id,
            "action": "sla_started_v2",
            "actor_type": "system",
            "details": {
                "sla_policy_id": sla_policy["id"],
                "goal_at": str(goal_at),
                "deadline_at": str(deadline_at),
                "calendar": calendar.name if calendar else "24x7",
                "escalation_tree_id": snapshot.get("tree_id"),
                "escalation_levels": len(schedule),
            },
        },
    )
    return sla
