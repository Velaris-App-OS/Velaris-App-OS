"""FCM v1 push channel — P27.

Uses raw httpx to call the FCM v1 HTTP API via a Google service-account
OAuth2 token.  No firebase-admin dependency (avoids protobuf bloat).

Required env vars:
  FCM_SERVICE_ACCOUNT_JSON  — path to service-account JSON file, OR
  FCM_SERVICE_ACCOUNT_JSON_CONTENT — JSON content directly (for secrets managers)
  FCM_PROJECT_ID            — Firebase project ID
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import httpx

from .protocol import DeliveryResult, PushChannel, PushPayload

log = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/firebase.messaging"]
_TOKEN_URL = "https://oauth2.googleapis.com/token"


def _load_sa_json() -> dict | None:
    content = os.getenv("FCM_SERVICE_ACCOUNT_JSON_CONTENT")
    if content:
        try:
            return json.loads(content)
        except Exception:
            return None
    path = os.getenv("FCM_SERVICE_ACCOUNT_JSON")
    if path and os.path.isfile(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return None
    return None


class FCMChannel:
    """FCM v1 channel implementation."""

    channel_name = "fcm"

    def __init__(self) -> None:
        self._sa = _load_sa_json()
        self._project_id = os.getenv("FCM_PROJECT_ID") or (
            self._sa.get("project_id") if self._sa else None
        )
        self._access_token: str | None = None
        self._token_expiry: float = 0.0

    @property
    def available(self) -> bool:
        return self._sa is not None and self._project_id is not None

    async def _get_access_token(self) -> str:
        if self._access_token and time.time() < self._token_expiry - 60:
            return self._access_token

        try:
            import jwt as pyjwt  # PyJWT
        except ImportError:
            raise RuntimeError("PyJWT is required for FCM: pip install PyJWT cryptography")

        sa = self._sa
        now = int(time.time())
        claim = {
            "iss": sa["client_email"],
            "scope": " ".join(_SCOPES),
            "aud": _TOKEN_URL,
            "iat": now,
            "exp": now + 3600,
        }
        signed = pyjwt.encode(claim, sa["private_key"], algorithm="RS256")
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(_TOKEN_URL, data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": signed,
            })
            resp.raise_for_status()
            body = resp.json()
        self._access_token = body["access_token"]
        self._token_expiry = time.time() + body.get("expires_in", 3600)
        return self._access_token

    async def send(self, token: str, payload: PushPayload) -> DeliveryResult:
        prefix = token[:8]
        if not self.available:
            return DeliveryResult(success=False, channel=self.channel_name,
                                  token_prefix=prefix, error="FCM credentials not configured")
        try:
            access_token = await self._get_access_token()
            message: dict[str, Any] = {
                "token": token,
                "notification": {"title": payload.title, "body": payload.body},
                "data": {k: str(v) for k, v in payload.data.items()},
            }
            if payload.icon:
                message["android"] = {"notification": {"icon": payload.icon}}
            url = f"https://fcm.googleapis.com/v1/projects/{self._project_id}/messages:send"
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(url,
                    headers={"Authorization": f"Bearer {access_token}"},
                    json={"message": message},
                )
            if resp.status_code == 200:
                return DeliveryResult(success=True, channel=self.channel_name, token_prefix=prefix)
            body = resp.json()
            error = body.get("error", {})
            err_msg = error.get("message", str(resp.status_code))
            deactivate = "INVALID_ARGUMENT" in err_msg or "UNREGISTERED" in err_msg
            return DeliveryResult(success=False, channel=self.channel_name,
                                  token_prefix=prefix, error=err_msg,
                                  should_deactivate_token=deactivate)
        except Exception as exc:
            log.warning("FCM send error for %s...: %s", prefix, exc)
            return DeliveryResult(success=False, channel=self.channel_name,
                                  token_prefix=prefix, error=str(exc))
