"""Event logger — writes events to the case_event_log table.

Called by case lifecycle hooks to build up the process mining
dataset over time.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def log_event(
    session: AsyncSession,
    *,
    case_id: uuid.UUID,
    case_type_id: uuid.UUID,
    activity: str,
    activity_type: str,
    stage_id: str | None = None,
    step_id: str | None = None,
    actor_id: str | None = None,
    actor_type: str = "user",
    duration_seconds: int | None = None,
    resource_id: str | None = None,
    outcome: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Record a single event in the process mining log."""
    from case_service.db.models import CaseEventLogModel

    event = CaseEventLogModel(
        case_id=case_id,
        case_type_id=case_type_id,
        activity=activity,
        activity_type=activity_type,
        stage_id=stage_id,
        step_id=step_id,
        actor_id=actor_id,
        actor_type=actor_type,
        duration_seconds=duration_seconds,
        resource_id=resource_id,
        outcome=outcome,
        extra_metadata=metadata or {},
    )
    session.add(event)
    await session.flush()


async def log_case_created(
    session: AsyncSession, case_id: uuid.UUID, case_type_id: uuid.UUID,
    actor_id: str | None = None,
) -> None:
    await log_event(
        session,
        case_id=case_id, case_type_id=case_type_id,
        activity="case_created", activity_type="case_start",
        actor_id=actor_id, outcome="success",
    )


async def log_case_resolved(
    session: AsyncSession, case_id: uuid.UUID, case_type_id: uuid.UUID,
    actor_id: str | None = None, duration_seconds: int | None = None,
) -> None:
    await log_event(
        session,
        case_id=case_id, case_type_id=case_type_id,
        activity="case_resolved", activity_type="case_end",
        actor_id=actor_id, duration_seconds=duration_seconds, outcome="success",
    )


async def log_stage_transition(
    session: AsyncSession, case_id: uuid.UUID, case_type_id: uuid.UUID,
    stage_id: str, activity_type: str = "stage_enter",
    actor_id: str | None = None, duration_seconds: int | None = None,
) -> None:
    await log_event(
        session,
        case_id=case_id, case_type_id=case_type_id,
        activity=stage_id, activity_type=activity_type,
        stage_id=stage_id, actor_id=actor_id, duration_seconds=duration_seconds,
    )


async def log_step_completed(
    session: AsyncSession, case_id: uuid.UUID, case_type_id: uuid.UUID,
    step_id: str, stage_id: str | None = None,
    actor_id: str | None = None, duration_seconds: int | None = None,
    outcome: str = "success",
) -> None:
    await log_event(
        session,
        case_id=case_id, case_type_id=case_type_id,
        activity=step_id, activity_type="step_complete",
        step_id=step_id, stage_id=stage_id, actor_id=actor_id,
        duration_seconds=duration_seconds, outcome=outcome,
    )
