"""APNs HTTP/2 push channel — P27.

Uses httpx with HTTP/2 support.  APNs requires HTTP/2; install httpx[http2].

Required env vars:
  APNS_KEY_FILE   — path to .p8 private key file
  APNS_KEY_ID     — 10-char key ID from Apple Developer Portal
  APNS_TEAM_ID    — 10-char team ID
  APNS_BUNDLE_ID  — app bundle identifier, e.g. com.example.helix
  APNS_SANDBOX    — "true" for development/sandbox (default: false = production)
"""
from __future__ import annotations

import logging
import os
import time

import httpx

from .protocol import DeliveryResult, PushChannel, PushPayload

log = logging.getLogger(__name__)

_PROD_HOST = "api.push.apple.com"
_SANDBOX_HOST = "api.sandbox.push.apple.com"


def _load_key() -> str | None:
    path = os.getenv("APNS_KEY_FILE")
    if path and os.path.isfile(path):
        with open(path) as f:
            return f.read().strip()
    content = os.getenv("APNS_KEY_CONTENT")
    return content or None


class APNsChannel:
    """APNs push channel implementation."""

    channel_name = "apns"

    def __init__(self) -> None:
        self._key_pem = _load_key()
        self._key_id = os.getenv("APNS_KEY_ID")
        self._team_id = os.getenv("APNS_TEAM_ID")
        self._bundle_id = os.getenv("APNS_BUNDLE_ID")
        sandbox = os.getenv("APNS_SANDBOX", "false").lower() == "true"
        self._host = _SANDBOX_HOST if sandbox else _PROD_HOST
        self._token: str | None = None
        self._token_expiry: float = 0.0

    @property
    def available(self) -> bool:
        return all([self._key_pem, self._key_id, self._team_id, self._bundle_id])

    def _make_jwt(self) -> str:
        try:
            import jwt as pyjwt
        except ImportError:
            raise RuntimeError("PyJWT is required: pip install PyJWT cryptography")

        now = int(time.time())
        if self._token and now < self._token_expiry - 60:
            return self._token

        claim = {"iss": self._team_id, "iat": now}
        self._token = pyjwt.encode(claim, self._key_pem,
                                   algorithm="ES256",
                                   headers={"kid": self._key_id})
        self._token_expiry = float(now + 3000)   # APNs tokens valid for 60 min, refresh at 50
        return self._token

    async def send(self, token: str, payload: PushPayload) -> DeliveryResult:
        prefix = token[:8]
        if not self.available:
            return DeliveryResult(success=False, channel=self.channel_name,
                                  token_prefix=prefix, error="APNs credentials not configured")
        try:
            jwt_token = self._make_jwt()
            url = f"https://{self._host}/3/device/{token}"
            apns_payload = {
                "aps": {
                    "alert": {"title": payload.title, "body": payload.body},
                    "sound": payload.sound,
                    **({"badge": payload.badge} if payload.badge is not None else {}),
                },
                **payload.data,
            }
            headers = {
                "authorization": f"bearer {jwt_token}",
                "apns-topic": self._bundle_id,
                "apns-push-type": "alert",
            }
            # HTTP/2 is required — httpx[http2]
            async with httpx.AsyncClient(http2=True, timeout=15) as client:
                resp = await client.post(url, headers=headers, json=apns_payload)
            if resp.status_code == 200:
                return DeliveryResult(success=True, channel=self.channel_name, token_prefix=prefix)
            try:
                err_body = resp.json()
                reason = err_body.get("reason", str(resp.status_code))
            except Exception:
                reason = str(resp.status_code)
            deactivate = reason in ("BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic")
            return DeliveryResult(success=False, channel=self.channel_name,
                                  token_prefix=prefix, error=reason,
                                  should_deactivate_token=deactivate)
        except Exception as exc:
            log.warning("APNs send error for %s...: %s", prefix, exc)
            return DeliveryResult(success=False, channel=self.channel_name,
                                  token_prefix=prefix, error=str(exc))
