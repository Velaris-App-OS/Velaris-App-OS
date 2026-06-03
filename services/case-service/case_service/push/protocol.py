"""Push notification channel protocol — P27.

Every channel (FCM, APNs, Web Push) implements PushChannel.
Channels must degrade gracefully when credentials are absent.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class PushPayload:
    title: str
    body: str
    data: dict[str, Any] = field(default_factory=dict)
    badge: int | None = None
    sound: str = "default"
    icon: str | None = None
    click_action: str | None = None


@dataclass
class DeliveryResult:
    success: bool
    channel: str
    token_prefix: str          # first 8 chars of token — never log full token
    error: str | None = None
    should_deactivate_token: bool = False   # True on InvalidRegistration / BadDeviceToken


class PushChannel(Protocol):
    """Pluggable push delivery channel."""

    channel_name: str          # "fcm" | "apns" | "webpush"

    @property
    def available(self) -> bool:
        """Return False when credentials are absent — allows graceful skip."""
        ...

    async def send(self, token: str, payload: PushPayload) -> DeliveryResult:
        """Deliver one notification. Must not raise; encode errors in DeliveryResult."""
        ...
