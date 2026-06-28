"""HxCheckout order notifications.

v1 scope (decision: "log + emit hook"): every order-event notification is recorded
in checkout_notifications_log per channel, and a thin best-effort emit hook is
invoked. Actual email/SMS/push delivery + customisable templates are wired into the
notification-service later — `_emit` is the single integration point for that.

Channel matrix mirrors docs/Future/HxCheckout.md.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import CheckoutNotificationLogModel, CheckoutOrderModel

logger = logging.getLogger(__name__)

# event → channels (per the doc's notification matrix).
EVENT_CHANNELS: dict[str, list[str]] = {
    "order_received":    ["email", "sms", "push"],
    "payment_confirmed": ["email", "push"],
    "order_dispatched":  ["email", "sms", "push"],
    "out_for_delivery":  ["email", "sms", "push"],
    "delivered":         ["email", "sms", "push"],
    "return_approved":   ["email", "sms"],
    "refund_issued":     ["email", "sms"],
    "return_requested":  ["email"],
    "complaint_raised":  ["email"],
}


def _emit(order: CheckoutOrderModel, event: str, channel: str) -> None:
    """Best-effort delivery hook. v1: log only. Wire notification-service here
    (email/SMS/push with brand + order + customer template variables)."""
    logger.info("hxcheckout.notify order=%s event=%s channel=%s → (delivery deferred)",
                order.tracking_token, event, channel)


async def notify(session: AsyncSession, order: CheckoutOrderModel, event: str) -> int:
    """Record + emit notifications for an order event. Returns channels notified.
    Non-fatal: never raises into the order path."""
    channels = EVENT_CHANNELS.get(event, ["email"])
    n = 0
    for channel in channels:
        try:
            session.add(CheckoutNotificationLogModel(
                order_id=order.id, event=event, channel=channel, status="logged"))
            _emit(order, event, channel)
            n += 1
        except Exception as e:  # notifications must never break order processing
            logger.warning("notify failed order=%s event=%s channel=%s: %s",
                           order.id, event, channel, e)
    return n
