"""Web Push (VAPID) channel — P27.

Uses pywebpush (open-source, Apache 2.0).

Required env vars:
  VAPID_PRIVATE_KEY   — VAPID private key (base64url DER)
  VAPID_PUBLIC_KEY    — VAPID public key  (base64url DER) — served to browser
  VAPID_SUBJECT       — mailto: or https: identifying the sender

Generate a keypair once:
  python -c "from py_vapid import Vapid; v = Vapid(); v.generate_keys(); print(v.private_key, v.public_key)"
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from .protocol import DeliveryResult, PushPayload

log = logging.getLogger(__name__)


def _get_vapid_public_key() -> str | None:
    return os.getenv("VAPID_PUBLIC_KEY")


class WebPushChannel:
    """Web Push / VAPID channel."""

    channel_name = "webpush"

    def __init__(self) -> None:
        self._private_key = os.getenv("VAPID_PRIVATE_KEY")
        self._public_key = os.getenv("VAPID_PUBLIC_KEY")
        self._subject = os.getenv("VAPID_SUBJECT", "mailto:admin@helix.local")

    @property
    def available(self) -> bool:
        return bool(self._private_key and self._public_key)

    def get_public_key(self) -> str | None:
        return self._public_key

    async def send(self, token: str, payload: PushPayload) -> DeliveryResult:
        """token is a JSON-encoded PushSubscription dict from the browser."""
        prefix = token[:8]
        if not self.available:
            return DeliveryResult(success=False, channel=self.channel_name,
                                  token_prefix=prefix, error="VAPID credentials not configured")
        try:
            from pywebpush import webpush, WebPushException
        except ImportError:
            return DeliveryResult(success=False, channel=self.channel_name,
                                  token_prefix=prefix, error="pywebpush not installed")
        try:
            subscription_info = json.loads(token)
            data: dict[str, Any] = {
                "title": payload.title,
                "body": payload.body,
                "data": payload.data,
            }
            if payload.icon:
                data["icon"] = payload.icon
            if payload.click_action:
                data["url"] = payload.click_action

            webpush(
                subscription_info=subscription_info,
                data=json.dumps(data),
                vapid_private_key=self._private_key,
                vapid_claims={"sub": self._subject},
            )
            return DeliveryResult(success=True, channel=self.channel_name, token_prefix=prefix)
        except Exception as exc:
            err_str = str(exc)
            deactivate = "410" in err_str or "404" in err_str
            log.warning("WebPush send error for %s...: %s", prefix, exc)
            return DeliveryResult(success=False, channel=self.channel_name,
                                  token_prefix=prefix, error=err_str,
                                  should_deactivate_token=deactivate)
