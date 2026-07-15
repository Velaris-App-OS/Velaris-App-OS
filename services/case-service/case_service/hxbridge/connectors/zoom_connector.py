"""ZoomConnector — create Zoom meetings via the Meetings API (HxMeet P1).

Server-to-Server OAuth app: account-level token via account_credentials
grant, meeting created under the configured host user (default "me" = the
app owner's account).
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from typing import Any

import httpx

from case_service.hxbridge.protocol import ConnectorProtocol, register_connector

_API = "https://api.zoom.us/v2"


@register_connector("zoom")
class ZoomConnector(ConnectorProtocol):
    name = "Zoom Meetings"
    connector_type = "zoom"
    schema = {
        "credentials": {
            "account_id":    "str (S2S OAuth account id)",
            "client_id":     "str (S2S OAuth client id)",
            "client_secret": "str (S2S OAuth client secret)",
        },
        "config": {"host_user_id": "str (optional; user id/email meetings are hosted by, default 'me')"},
    }

    def __init__(self, config: dict, credentials: dict):
        self._account_id    = credentials["account_id"]
        self._client_id     = credentials["client_id"]
        self._client_secret = credentials["client_secret"]
        self._host          = config.get("host_user_id") or "me"

    async def _token(self, client: httpx.AsyncClient) -> str:
        basic = base64.b64encode(f"{self._client_id}:{self._client_secret}".encode()).decode()
        resp = await client.post(
            "https://zoom.us/oauth/token",
            params={"grant_type": "account_credentials", "account_id": self._account_id},
            headers={"Authorization": f"Basic {basic}"},
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Zoom auth error {resp.status_code}: {resp.text[:300]}")
        return resp.json()["access_token"]

    async def execute(self, input_data: dict) -> dict:
        title = input_data.get("title") or "Velaris case session"
        duration = int(input_data.get("duration_minutes") or 60)

        payload: dict[str, Any] = {
            "topic":    title,
            "type":     2,          # scheduled meeting
            "duration": duration,
            "settings": {"join_before_host": True, "waiting_room": False},
        }
        if input_data.get("start_time"):
            payload["start_time"] = input_data["start_time"]

        async with httpx.AsyncClient(timeout=20) as client:
            token = await self._token(client)
            resp = await client.post(
                f"{_API}/users/{self._host}/meetings",
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )

        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Zoom error {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        return {
            "external_meeting_id": str(data.get("id")),
            "join_url":            data.get("join_url"),
            "provider":            "zoom",
            "created_at":          datetime.now(timezone.utc).isoformat(),
        }

    async def test(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await self._token(client)
            return True
        except Exception:
            return False
