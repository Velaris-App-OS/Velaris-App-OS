"""HxReplay — baseline trace: the recorded reality of a case.

Loads a case's ordered ``case_event_log`` events (the process shape that actually
happened) and derives baseline metrics from them. This is the ground truth every
counterfactual is compared against — it is never recomputed, only read.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import CaseEventLogModel

# actor_type values counted as automated work (vs human) for the auto/manual ratio
_AUTO_ACTOR_TYPES = ("system", "rule", "ai", "service")


async def load_baseline_trace(session: AsyncSession, case_id: uuid.UUID) -> list[dict[str, Any]]:
    """The case's recorded events, oldest first. Empty list = no recorded history."""
    rows = (await session.execute(
        select(CaseEventLogModel)
        .where(CaseEventLogModel.case_id == case_id)
        .order_by(CaseEventLogModel.timestamp, CaseEventLogModel.id)
    )).scalars().all()
    return [_event_view(e) for e in rows]


def _event_view(e: CaseEventLogModel) -> dict[str, Any]:
    return {
        "id": str(e.id),
        "activity": e.activity,
        "activity_type": e.activity_type,
        "stage_id": e.stage_id,
        "step_id": e.step_id,
        "actor_id": e.actor_id,
        "actor_type": e.actor_type,
        "timestamp": e.timestamp.isoformat() if e.timestamp else None,
        "duration_seconds": e.duration_seconds,
        "outcome": e.outcome,
        "metadata": e.extra_metadata or {},
    }


def baseline_metrics(trace: list[dict[str, Any]]) -> dict[str, Any]:
    """Metrics of the recorded trace: cycle time, auto/manual split, stage durations.

    Cycle time = first→last recorded event. For an unresolved case this is
    "history so far", which is still the correct baseline for a like-for-like
    comparison (the counterfactual replays the same window).
    """
    if not trace:
        return {"event_count": 0, "cycle_time_seconds": None, "auto_ratio": None,
                "auto_count": 0, "manual_count": 0, "manual_seconds": 0,
                "stage_durations": {}, "outcomes": {}, "resolved": False}

    from datetime import datetime

    def _ts(ev: dict) -> datetime | None:
        return datetime.fromisoformat(ev["timestamp"]) if ev.get("timestamp") else None

    first, last = _ts(trace[0]), _ts(trace[-1])
    cycle = (last - first).total_seconds() if first and last else None

    auto = sum(1 for e in trace if (e.get("actor_type") or "") in _AUTO_ACTOR_TYPES)
    manual = len(trace) - auto
    # recorded human effort (case costing basis): duration of non-automated events
    manual_seconds = sum(int(e["duration_seconds"]) for e in trace
                         if e.get("duration_seconds")
                         and (e.get("actor_type") or "") not in _AUTO_ACTOR_TYPES)

    stage_durations: dict[str, int] = {}
    outcomes: dict[str, int] = {}
    for e in trace:
        if e.get("stage_id") and e.get("duration_seconds"):
            sid = e["stage_id"]
            stage_durations[sid] = stage_durations.get(sid, 0) + int(e["duration_seconds"])
        if e.get("outcome"):
            outcomes[e["outcome"]] = outcomes.get(e["outcome"], 0) + 1

    return {
        "event_count": len(trace),
        "cycle_time_seconds": cycle,
        "auto_count": auto,
        "manual_count": manual,
        "manual_seconds": manual_seconds,
        "auto_ratio": round(auto / len(trace), 4),
        "stage_durations": stage_durations,
        "outcomes": outcomes,
        "resolved": any(e.get("activity_type") == "case_end" for e in trace),
    }
