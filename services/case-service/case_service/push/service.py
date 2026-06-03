"""Push notification service — P27.

Preference resolution order (highest → lowest):
  case-type override > user default > global default (all channels enabled)
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .protocol import PushPayload, DeliveryResult
from .fcm import FCMChannel
from .apns import APNsChannel
from .webpush import WebPushChannel

log = logging.getLogger(__name__)

# Singleton channel instances (credentials loaded once from env at import time)
_FCM = FCMChannel()
_APNS = APNsChannel()
_WEBPUSH = WebPushChannel()

_CHANNELS: dict[str, Any] = {
    "fcm": _FCM,
    "apns": _APNS,
    "webpush": _WEBPUSH,
}


def get_vapid_public_key() -> str | None:
    return _WEBPUSH.get_public_key()


async def resolve_channels(
    session: AsyncSession,
    user_id: str,
    event_type: str,
    case_type_id: uuid.UUID | None = None,
) -> list[str]:
    """Return ordered list of enabled channel names for this user+event+case_type."""
    from case_service.db.models import NotificationPreferenceModel, CaseTypeNotificationOverrideModel

    # Case-type override
    if case_type_id:
        override = await session.scalar(
            select(CaseTypeNotificationOverrideModel).where(
                CaseTypeNotificationOverrideModel.case_type_id == case_type_id,
                CaseTypeNotificationOverrideModel.event_type == event_type,
                CaseTypeNotificationOverrideModel.enabled == True,  # noqa: E712
            )
        )
        if override is not None:
            return override.channels or []

    # User preference
    pref = await session.scalar(
        select(NotificationPreferenceModel).where(
            NotificationPreferenceModel.user_id == user_id,
            NotificationPreferenceModel.event_type == event_type,
        )
    )
    if pref is not None:
        if not pref.enabled:
            return []
        return pref.channels or list(_CHANNELS.keys())

    # Global default: all channels
    return list(_CHANNELS.keys())


async def send_to_user(
    session: AsyncSession,
    user_id: str,
    event_type: str,
    payload: PushPayload,
    case_type_id: uuid.UUID | None = None,
) -> list[DeliveryResult]:
    """Deliver a notification to all active devices of a user, respecting preferences."""
    from case_service.db.models import DeviceTokenModel, NotificationLogModel

    channels = await resolve_channels(session, user_id, event_type, case_type_id)
    if not channels:
        return []

    devices_q = await session.execute(
        select(DeviceTokenModel).where(
            DeviceTokenModel.user_id == user_id,
            DeviceTokenModel.is_active == True,  # noqa: E712
            DeviceTokenModel.channel.in_(channels),
        )
    )
    devices = list(devices_q.scalars())

    results: list[DeliveryResult] = []
    for device in devices:
        channel = _CHANNELS.get(device.channel)
        if channel is None or not channel.available:
            result = DeliveryResult(
                success=False, channel=device.channel,
                token_prefix=device.token[:8],
                error="channel unavailable",
            )
        else:
            result = await channel.send(device.token, payload)

        results.append(result)

        # Deactivate stale tokens (unregistered device)
        if result.should_deactivate_token:
            device.is_active = False
            log.info("Deactivated stale device token %s... for user %s", device.token[:8], user_id)

        # Write delivery log
        log_entry = NotificationLogModel(
            device_id=device.id,
            user_id=user_id,
            event_type=event_type,
            channel=device.channel,
            status="delivered" if result.success else "failed",
            error=result.error,
            sent_at=datetime.now(timezone.utc),
        )
        session.add(log_entry)

    await session.commit()
    return results
