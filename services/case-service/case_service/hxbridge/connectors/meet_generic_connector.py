"""MeetGenericConnector — create meetings on ANY provider with a "create meeting"
HTTP API (HxMeet P1).

Config-driven endpoint + response field mapping (the templated-connector
pattern). Because the URL is operator-configured, every call goes through
the SSRF guard — unlike the named providers whose hosts are hard-coded.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from case_service.hxbridge.protocol import ConnectorProtocol, register_connector
from case_service.hxbridge.security import validate_outbound_url


def _pluck(data: Any, path: str) -> Any:
    """Resolve a dotted path ("meeting.join_url") in a JSON response."""
    cur = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


@register_connector("meet_generic")
class MeetGenericConnector(ConnectorProtocol):
    name = "Generic Meeting Provider"
    connector_type = "meet_generic"
    schema = {
        "credentials": {"api_token": "str (sent as Bearer unless auth_header set)"},
        "config": {
            "create_url":        "str (POST endpoint that creates a meeting)",
            "auth_header":       "str (optional header name, default 'Authorization: Bearer <token>')",
            "title_field":       "str (optional request field for the title, default 'title')",
            "join_url_path":     "str (dotted path to the join URL in the response, default 'join_url')",
            "meeting_id_path":   "str (dotted path to the meeting id, default 'id')",
            "extra_payload":     "dict (optional static fields merged into the request)",
        },
    }

    def __init__(self, config: dict, credentials: dict):
        self._api_token       = credentials.get("api_token", "")
        self._create_url      = config["create_url"]
        self._auth_header     = config.get("auth_header") or ""
        self._title_field     = config.get("title_field") or "title"
        self._join_url_path   = config.get("join_url_path") or "join_url"
        self._meeting_id_path = config.get("meeting_id_path") or "id"
        self._extra_payload   = config.get("extra_payload") or {}

    def _headers(self) -> dict:
        if self._auth_header:
            return {self._auth_header: self._api_token}
        return {"Authorization": f"Bearer {self._api_token}"}

    async def execute(self, input_data: dict) -> dict:
        await validate_outbound_url(self._create_url)

        payload: dict[str, Any] = {**self._extra_payload}
        payload[self._title_field] = input_data.get("title") or "Velaris case session"

        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(self._create_url, headers=self._headers(), json=payload)

        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Meeting provider error {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        join_url = _pluck(data, self._join_url_path)
        if not join_url:
            raise RuntimeError(f"Meeting provider response has no '{self._join_url_path}'")

        return {
            "external_meeting_id": str(_pluck(data, self._meeting_id_path) or ""),
            "join_url":            join_url,
            "provider":            "generic",
            "created_at":          datetime.now(timezone.utc).isoformat(),
        }

    async def test(self) -> bool:
        try:
            await validate_outbound_url(self._create_url)
            return True
        except Exception:
            return False
