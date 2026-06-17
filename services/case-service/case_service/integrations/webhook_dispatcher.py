"""Webhook dispatcher — writes outbox rows for reliable HTTP delivery.

All HTTP firing is done by OutboxRelay (outbox_relay.py), not here.
dispatch_event() is called inline with the business transaction so the
outbox row and the triggering case mutation commit atomically.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

CASE_EVENTS = [
    "case.created", "case.status_changed", "case.priority_changed",
    "case.resolved", "case.closed", "case.cancelled", "case.reopened",
    "case.data_updated", "case.stage_transitioned",
    "assignment.created", "assignment.claimed", "assignment.completed",
    "sla.started", "sla.at_risk", "sla.breached", "sla.paused", "sla.resumed",
    "form.submitted",
]


def compute_signature(payload: str, secret: str) -> str:
    """HMAC-SHA256 signature for webhook payload verification."""
    return hmac.new(
        secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


async def dispatch_event(
    session: AsyncSession,
    event_type: str,
    payload: dict[str, Any],
    case_type_id: uuid.UUID | None = None,
) -> None:
    """Write one outbox row for this event.

    Runs inside the caller's transaction — the outbox row and the triggering
    business mutation commit together, guaranteeing at-least-once delivery
    even if the process crashes before OutboxRelay picks the row up.

    HTTP delivery is handled asynchronously by OutboxRelay; callers do not
    wait for the remote endpoint.
    """
    from case_service.db.models import OutboxEventModel

    row = OutboxEventModel(
        event_type=event_type,
        payload={
            "event": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **payload,
        },
        case_type_id=case_type_id,
    )
    session.add(row)
    logger.debug("outbox: queued %s (case_type=%s)", event_type, case_type_id)


async def get_matching_subscriptions(
    session: AsyncSession,
    event_type: str,
    case_type_id: uuid.UUID | None = None,
) -> list:
    """Return active webhook subscriptions that match this event."""
    from case_service.db.models import WebhookSubscriptionModel

    stmt = select(WebhookSubscriptionModel).where(
        WebhookSubscriptionModel.is_active == True,  # noqa: E712
    )
    result = await session.execute(stmt)
    subs = list(result.scalars().all())

    matched = []
    for sub in subs:
        events = sub.events or []
        if events and event_type not in events and "*" not in events:
            continue
        if sub.case_type_id and case_type_id and sub.case_type_id != case_type_id:
            continue
        matched.append(sub)

    return matched


async def build_case_event_payload(
    session: AsyncSession,
    case_id: uuid.UUID,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a standardized webhook payload for a case event.

    Thin by default: IDs + status only, never case variables. The webhook is
    a notification; consumers fetch fresh state via GET /cases/{id}, which
    enforces auth and field redaction — inline variables would bypass both
    and persist in the outbox table. webhook_full_payloads=true restores the
    legacy embedded-variables shape for consumers that cannot call back.
    """
    from case_service.config import get_settings
    from case_service.db import repository as repo

    case = await repo.get_case_instance(session, case_id)
    if case is None:
        return {"case_id": str(case_id), **(extra or {})}

    payload = {
        "case_id": str(case.id),
        "case_type_id": str(case.case_type_id),
        "status": case.status,
        "priority": case.priority,
        "current_stage_id": case.current_stage_id,
        "created_by": case.created_by,
        **(extra or {}),
    }
    if get_settings().webhook_full_payloads:
        # Read through the case_vars façade (blob fallback included): typed
        # pii/secret variables leave the platform masked — webhook targets
        # are not privileged readers.
        from case_service.case_vars import service as case_vars
        ctx = case_vars.CallerContext(kind="platform", actor_id="webhook-dispatcher")
        payload["data"] = await case_vars.get_all(session, ctx, case.id)
    return payload
