"""Webhook dispatcher — fires HTTP callbacks on case events.

Matches events against active subscriptions and dispatches
asynchronous HTTP POST requests with HMAC signatures.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Supported event types
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


async def get_matching_subscriptions(
    session: AsyncSession,
    event_type: str,
    case_type_id: uuid.UUID | None = None,
) -> list:
    """Find active webhook subscriptions matching an event."""
    from case_service.db.models import WebhookSubscriptionModel

    stmt = select(WebhookSubscriptionModel).where(
        WebhookSubscriptionModel.is_active == True,  # noqa: E712
    )
    result = await session.execute(stmt)
    subs = list(result.scalars().all())

    matched = []
    for sub in subs:
        # Check event filter
        events = sub.events or []
        if events and event_type not in events and "*" not in events:
            continue
        # Check case type filter
        if sub.case_type_id and case_type_id and sub.case_type_id != case_type_id:
            continue
        matched.append(sub)

    return matched


async def dispatch_event(
    session: AsyncSession,
    event_type: str,
    payload: dict[str, Any],
    case_type_id: uuid.UUID | None = None,
) -> int:
    """Dispatch a webhook event to all matching subscriptions.

    Creates delivery records for each subscription. Actual HTTP
    delivery happens asynchronously (fire-and-forget for now,
    with retry support in delivery records).

    Returns the number of subscriptions notified.
    """
    from case_service.db.models import WebhookDeliveryModel

    subs = await get_matching_subscriptions(session, event_type, case_type_id)
    if not subs:
        return 0

    count = 0
    for sub in subs:
        delivery = WebhookDeliveryModel(
            subscription_id=sub.id,
            event_type=event_type,
            payload={
                "event": event_type,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                **payload,
            },
            status="pending",
        )
        session.add(delivery)
        count += 1

        # Fire-and-forget HTTP call (best effort, non-blocking)
        try:
            import httpx
            headers = {"Content-Type": "application/json", **(sub.headers or {})}
            body = json.dumps(delivery.payload, default=str)

            if sub.secret:
                headers["X-Helix-Signature"] = compute_signature(body, sub.secret)

            headers["X-Helix-Event"] = event_type
            headers["X-Helix-Delivery"] = str(delivery.id)

            async with httpx.AsyncClient(timeout=sub.timeout_seconds) as client:
                resp = await client.post(sub.url, content=body, headers=headers)
                delivery.response_status = resp.status_code
                delivery.response_body = resp.text[:2000] if resp.text else None
                delivery.status = "delivered" if resp.status_code < 400 else "failed"
                delivery.delivered_at = datetime.now(timezone.utc)

                if resp.status_code >= 400:
                    delivery.error_message = f"HTTP {resp.status_code}"
                    if delivery.attempt < sub.retry_count:
                        delivery.next_retry_at = datetime.now(timezone.utc) + timedelta(
                            minutes=2 ** delivery.attempt
                        )

        except Exception as e:
            delivery.status = "failed"
            delivery.error_message = str(e)[:500]
            logger.warning("Webhook delivery failed for %s: %s", sub.url, e)

    await session.flush()
    logger.info("Dispatched %s to %d webhook(s)", event_type, count)
    return count


async def build_case_event_payload(
    session: AsyncSession,
    case_id: uuid.UUID,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a standardized webhook payload for a case event."""
    from case_service.db import repository as repo

    case = await repo.get_case_instance(session, case_id)
    if case is None:
        return {"case_id": str(case_id), **(extra or {})}

    return {
        "case_id": str(case.id),
        "case_type_id": str(case.case_type_id),
        "status": case.status,
        "priority": case.priority,
        "current_stage_id": case.current_stage_id,
        "created_by": case.created_by,
        "data": case.data,
        **(extra or {}),
    }
