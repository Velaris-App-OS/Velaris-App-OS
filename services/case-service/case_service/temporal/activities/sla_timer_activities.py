"""Temporal activities for durable SLA timers — direct repository access.

These are SYSTEM actions: the platform enforcing its own SLA policies.
There is no acting user, so they bypass the HTTP layer entirely and use
the database directly; every write is audited with actor_type="system".

Workflow state holds only IDs — case payloads never enter Temporal's
database (Option A constraint 3, docs/Future/temporal-decision-record.md).

Firing is verify-before-fire: every activity re-reads the SLA row and
only applies transitions that are actually due NOW per DB truth. A timer
sleeping toward a stale deadline therefore no-ops harmlessly if the SLA
was paused, resumed (deadlines shifted), or cancelled while it slept.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from temporalio import activity

from case_service.db import repository as repo
from case_service.db.models import CaseSLAInstanceModel
from case_service.db.session import get_session_factory

logger = logging.getLogger(__name__)


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; treat them as UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _pending_events(
    row: CaseSLAInstanceModel,
) -> list[tuple[datetime, str, dict[str, Any] | None]]:
    """All future-or-due events for this SLA, sorted by fire time.

    Event kinds: "at_risk" (goal passed), "breach" (deadline passed),
    "escalation" (one precomputed schedule level from the v2 snapshot).
    """
    events: list[tuple[datetime, str, dict[str, Any] | None]] = []
    if row.status == "on_track":
        events.append((_aware(row.goal_at), "at_risk", None))
    if row.status in ("on_track", "at_risk"):
        events.append((_aware(row.deadline_at), "breach", None))
    if row.status in ("on_track", "at_risk", "breached"):
        fired = {h.get("level") for h in (row.escalation_history or [])}
        schedule = (row.escalation_tree_snapshot or {}).get("schedule", [])
        for entry in schedule:
            if entry.get("level") in fired:
                continue
            try:
                fires_at = datetime.fromisoformat(entry["fires_at"])
            except (KeyError, ValueError):
                continue
            events.append((_aware(fires_at), "escalation", entry))
    events.sort(key=lambda e: e[0])
    return events


def _state_dict(row: CaseSLAInstanceModel | None) -> dict[str, Any]:
    if row is None:
        return {"exists": False, "status": "missing", "terminal": True,
                "paused": False, "next_event_at": None}
    events = _pending_events(row)
    terminal = row.status == "cancelled" or (
        row.status == "breached" and not events
    )
    return {
        "exists": True,
        "status": row.status,
        "case_id": str(row.case_id),
        "paused": row.status == "paused",
        "terminal": terminal,
        "next_event_at": events[0][0].isoformat() if events else None,
    }


@activity.defn
async def list_case_sla_timers(case_id: str) -> list[dict[str, Any]]:
    """Return id + status of every SLA instance on a case."""
    factory = get_session_factory()
    async with factory() as session:
        rows = await repo.get_sla_instances(session, uuid.UUID(case_id))
        return [{"sla_id": str(r.id), "status": r.status} for r in rows]


@activity.defn
async def get_sla_timer_state(sla_id: str) -> dict[str, Any]:
    """Read one SLA instance and report what the timer should do next."""
    factory = get_session_factory()
    async with factory() as session:
        row = await session.get(CaseSLAInstanceModel, uuid.UUID(sla_id))
        return _state_dict(row)


@activity.defn
async def fire_sla_event(sla_id: str) -> dict[str, Any]:
    """Apply every transition that is due NOW; idempotent.

    Mirrors check_sla semantics for status (goal_at -> at_risk,
    deadline_at -> breached) and applies due, unfired escalation levels
    from the precomputed v2 snapshot schedule via sla_escalation.apply_level.
    """
    from case_service.core.sla_escalation import apply_level

    factory = get_session_factory()
    async with factory() as session:
        row = await session.get(CaseSLAInstanceModel, uuid.UUID(sla_id))
        if row is None:
            return _state_dict(None)

        now = datetime.now(timezone.utc)
        fired = 0

        if row.status not in ("paused", "cancelled"):
            if row.status in ("on_track", "at_risk") and now >= _aware(row.deadline_at):
                row.status = "breached"
                row.breached_at = now
                await session.flush()
                await repo.append_audit_entry(
                    session,
                    data={
                        "case_id": row.case_id,
                        "action": "sla_breached",
                        "actor_type": "system",
                        "details": {"sla_policy_id": row.sla_policy_id,
                                    "target_id": row.target_id},
                    },
                )
                fired += 1
            elif row.status == "on_track" and now >= _aware(row.goal_at):
                row.status = "at_risk"
                await session.flush()
                await repo.append_audit_entry(
                    session,
                    data={
                        "case_id": row.case_id,
                        "action": "sla_at_risk",
                        "actor_type": "system",
                        "details": {"sla_policy_id": row.sla_policy_id,
                                    "target_id": row.target_id},
                    },
                )
                fired += 1

            fired_levels = {h.get("level") for h in (row.escalation_history or [])}
            schedule = (row.escalation_tree_snapshot or {}).get("schedule", [])
            for entry in schedule:
                if entry.get("level") in fired_levels:
                    continue
                try:
                    fires_at = _aware(datetime.fromisoformat(entry["fires_at"]))
                except (KeyError, ValueError):
                    continue
                if fires_at <= now:
                    await apply_level(session, row.case_id, row, entry)
                    fired += 1

        await session.commit()
        logger.info(
            "SLA timer fired %d event(s): sla=%s status=%s", fired, sla_id, row.status,
        )
        return {**_state_dict(row), "events_fired": fired}
