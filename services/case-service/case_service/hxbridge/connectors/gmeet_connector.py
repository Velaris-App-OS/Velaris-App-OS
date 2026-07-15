"""GMeetConnector — create Google Meet meetings via the Calendar API (HxMeet P1).

Service-account JWT grant (domain-wide delegation impersonating the
organizer): inserts a calendar event with conferenceData → Google attaches
a Meet link. The service account must be delegated the
`https://www.googleapis.com/auth/calendar.events` scope.
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from case_service.hxbridge.protocol import ConnectorProtocol, register_connector

_SCOPE = "https://www.googleapis.com/auth/calendar.events"
_TOKEN_URL = "https://oauth2.googleapis.com/token"


@register_connector("gmeet")
class GMeetConnector(ConnectorProtocol):
    name = "Google Meet"
    connector_type = "gmeet"
    schema = {
        "credentials": {
            "client_email": "str (service-account email)",
            "private_key":  "str (service-account PEM private key)",
        },
        "config": {
            "organizer_email": "str (delegated user the event is created as)",
            "calendar_id":     "str (optional, default 'primary')",
        },
    }

    def __init__(self, config: dict, credentials: dict):
        self._client_email = credentials["client_email"]
        self._private_key  = credentials["private_key"]
        self._organizer    = config["organizer_email"]
        self._calendar_id  = config.get("calendar_id") or "primary"

    def _assertion(self) -> str:
        import jwt as _jwt  # PyJWT, RS256
        now = int(time.time())
        return _jwt.encode(
            {
                "iss":   self._client_email,
                "sub":   self._organizer,           # domain-wide delegation
                "scope": _SCOPE,
                "aud":   _TOKEN_URL,
                "iat":   now,
                "exp":   now + 3600,
            },
            self._private_key.replace("\\n", "\n"),
            algorithm="RS256",
        )

    async def _token(self, client: httpx.AsyncClient) -> str:
        resp = await client.post(
            _TOKEN_URL,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion":  self._assertion(),
            },
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Google auth error {resp.status_code}: {resp.text[:300]}")
        return resp.json()["access_token"]

    async def execute(self, input_data: dict) -> dict:
        title = input_data.get("title") or "Velaris case session"
        start = input_data.get("start_time") or datetime.now(timezone.utc).isoformat()
        duration = int(input_data.get("duration_minutes") or 60)
        end = (datetime.fromisoformat(start.replace("Z", "+00:00")) + timedelta(minutes=duration)).isoformat()

        payload: dict[str, Any] = {
            "summary": title,
            "start":   {"dateTime": start},
            "end":     {"dateTime": end},
            "conferenceData": {
                "createRequest": {
                    "requestId": str(uuid.uuid4()),
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            },
        }
        async with httpx.AsyncClient(timeout=20) as client:
            token = await self._token(client)
            resp = await client.post(
                f"https://www.googleapis.com/calendar/v3/calendars/{self._calendar_id}/events",
                params={"conferenceDataVersion": 1},
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )

        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Google Meet error {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        return {
            "external_meeting_id": data.get("id"),
            "join_url":            data.get("hangoutLink"),
            "provider":            "gmeet",
            "created_at":          datetime.now(timezone.utc).isoformat(),
        }

    async def test(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await self._token(client)
            return True
        except Exception:
            return False
