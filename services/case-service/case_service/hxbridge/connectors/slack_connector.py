"""SlackConnector — outbound messages via Slack Incoming Webhook or Web API."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from case_service.hxbridge.protocol import ConnectorProtocol, register_connector


@register_connector("slack")
class SlackConnector(ConnectorProtocol):
    name = "Slack"
    connector_type = "slack"
    schema = {
        "credentials": {"webhook_url": "str (incoming webhook URL)"},
        "config":      {"default_channel": "str (optional)"},
    }

    def __init__(self, config: dict, credentials: dict):
        self._webhook_url      = credentials["webhook_url"]
        self._default_channel  = config.get("default_channel", "")

    async def execute(self, input_data: dict) -> dict:
        message = input_data["message"]
        blocks  = input_data.get("blocks", [])
        channel = input_data.get("channel") or self._default_channel

        payload: dict[str, Any] = {"text": message}
        if channel:
            payload["channel"] = channel
        if blocks:
            payload["blocks"] = blocks

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(self._webhook_url, json=payload)

        if resp.status_code != 200 or resp.text != "ok":
            raise RuntimeError(f"Slack error {resp.status_code}: {resp.text[:300]}")

        return {
            "status":  "sent",
            "sent_at": datetime.now(timezone.utc).isoformat(),
        }

    async def test(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(self._webhook_url, json={"text": "Helix connector test"})
            return resp.status_code == 200 and resp.text == "ok"
        except Exception:
            return False
