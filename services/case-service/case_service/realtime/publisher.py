"""Publisher helpers — anything in the codebase can call these
to broadcast real-time events without importing the manager directly.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from case_service.realtime.manager import get_manager

logger = logging.getLogger(__name__)


async def publish_case_event(
    case_id: uuid.UUID | str,
    event_type: str,
    data: dict[str, Any] | None = None,
    actor_id: str | None = None,
) -> None:
    """Broadcast a case event on cases.{id} and cases.* channels."""
    try:
        manager = get_manager()
        await manager.broadcast(f"cases.{case_id}", {
            "type": event_type,
            "case_id": str(case_id),
            "actor_id": actor_id,
            "data": data or {},
        })
    except Exception as e:
        logger.debug("Publish case event failed: %s", e)


async def publish_assignment_event(
    user_id: str,
    event_type: str,
    data: dict[str, Any] | None = None,
) -> None:
    """Broadcast an assignment event to a user's worklist channel."""
    try:
        manager = get_manager()
        await manager.broadcast(f"assignments.{user_id}", {
            "type": event_type,
            "user_id": user_id,
            "data": data or {},
        })
    except Exception as e:
        logger.debug("Publish assignment event failed: %s", e)


async def publish_system_event(
    event_type: str,
    data: dict[str, Any] | None = None,
) -> None:
    """Broadcast a system-wide event on events.global."""
    try:
        manager = get_manager()
        await manager.broadcast("events.global", {
            "type": event_type,
            "data": data or {},
        })
    except Exception as e:
        logger.debug("Publish system event failed: %s", e)
