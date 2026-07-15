"""TeamsConnector — create Microsoft Teams meetings via the Graph API (HxMeet P1).

App-only (client-credentials) flow: the app must hold the Graph
`OnlineMeetings.ReadWrite.All` application permission plus an application
access policy assigned to the organizer account (Teams admin requirement
for app-created meetings).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from case_service.hxbridge.protocol import ConnectorProtocol, register_connector

_GRAPH = "https://graph.microsoft.com/v1.0"


@register_connector("teams")
class TeamsConnector(ConnectorProtocol):
    name = "Microsoft Teams Meetings"
    connector_type = "teams"
    schema = {
        "credentials": {
            "tenant_id":     "str (Entra ID directory/tenant id)",
            "client_id":     "str (app registration client id)",
            "client_secret": "str (app registration client secret)",
        },
        "config": {"organizer_user_id": "str (user id or UPN the meetings are created under)"},
    }

    def __init__(self, config: dict, credentials: dict):
        self._tenant_id     = credentials["tenant_id"]
        self._client_id     = credentials["client_id"]
        self._client_secret = credentials["client_secret"]
        self._organizer     = config["organizer_user_id"]

    async def _app_token(self, client: httpx.AsyncClient) -> str:
        resp = await client.post(
            f"https://login.microsoftonline.com/{self._tenant_id}/oauth2/v2.0/token",
            data={
                "grant_type":    "client_credentials",
                "client_id":     self._client_id,
                "client_secret": self._client_secret,
                "scope":         "https://graph.microsoft.com/.default",
            },
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Teams auth error {resp.status_code}: {resp.text[:300]}")
        return resp.json()["access_token"]

    async def execute(self, input_data: dict) -> dict:
        title = input_data.get("title") or "Velaris case session"
        start = input_data.get("start_time") or datetime.now(timezone.utc).isoformat()
        duration = int(input_data.get("duration_minutes") or 60)
        end = (datetime.fromisoformat(start.replace("Z", "+00:00")) + timedelta(minutes=duration)).isoformat()

        payload: dict[str, Any] = {
            "subject":       title,
            "startDateTime": start,
            "endDateTime":   end,
        }
        async with httpx.AsyncClient(timeout=20) as client:
            token = await self._app_token(client)
            resp = await client.post(
                f"{_GRAPH}/users/{self._organizer}/onlineMeetings",
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )

        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Teams error {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        return {
            "external_meeting_id": data.get("id"),
            "join_url":            data.get("joinWebUrl") or data.get("joinUrl"),
            "provider":            "teams",
            "created_at":          datetime.now(timezone.utc).isoformat(),
        }

    async def test(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await self._app_token(client)
            return True
        except Exception:
            return False
