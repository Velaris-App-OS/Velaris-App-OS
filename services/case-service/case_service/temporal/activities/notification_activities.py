"""Temporal activities for case notifications.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from typing import Any

from temporalio import activity


@activity.defn
async def send_case_notification(
    case_id: str,
    notification_type: str,
    recipients: list[str] | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Send a notification about a case event.

    In production this would dispatch via email, Slack, webhook, etc.
    through the NotificationChannel protocol.  For now: log only.
    """
    activity.logger.info(
        "Case notification: case=%s type=%s recipients=%s",
        case_id,
        notification_type,
        recipients or ["system"],
    )
    # TODO: Wire to NotificationChannel protocol
    # - email via SMTP plugin
    # - Slack via Slack plugin
    # - webhook via HTTP plugin
