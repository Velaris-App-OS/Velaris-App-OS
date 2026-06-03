"""TwilioConnector — outbound SMS via Twilio REST API."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from typing import Any

import httpx

from case_service.hxbridge.protocol import ConnectorProtocol, register_connector


@register_connector("twilio")
class TwilioConnector(ConnectorProtocol):
    name = "Twilio SMS"
    connector_type = "twilio"
    schema = {
        "credentials": {"account_sid": "str", "auth_token": "str"},
        "config":      {"from_number": "str (optional default sender)"},
    }

    def __init__(self, config: dict, credentials: dict):
        self._account_sid  = credentials["account_sid"]
        self._auth_token   = credentials["auth_token"]
        self._from_default = config.get("from_number", "")
        self._base_url     = f"https://api.twilio.com/2010-04-01/Accounts/{self._account_sid}/Messages.json"

    def _auth_header(self) -> str:
        token = base64.b64encode(f"{self._account_sid}:{self._auth_token}".encode()).decode()
        return f"Basic {token}"

    async def execute(self, input_data: dict) -> dict:
        to   = input_data["to_number"]
        body = input_data["body"]
        frm  = input_data.get("from_number") or self._from_default

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                self._base_url,
                headers={"Authorization": self._auth_header()},
                data={"To": to, "From": frm, "Body": body},
            )

        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Twilio error {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        return {
            "message_sid": data.get("sid"),
            "status":      data.get("status", "queued"),
            "from_number": data.get("from"),
            "sent_at":     datetime.now(timezone.utc).isoformat(),
        }

    async def test(self) -> bool:
        url = f"https://api.twilio.com/2010-04-01/Accounts/{self._account_sid}.json"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers={"Authorization": self._auth_header()})
        return resp.status_code == 200
