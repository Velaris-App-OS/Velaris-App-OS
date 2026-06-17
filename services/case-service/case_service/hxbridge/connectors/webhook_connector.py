"""HxBridge — Webhook outbound connector.

Sends a JSON payload to a configured webhook URL (Slack, Teams, Zapier, n8n, etc.)
with optional HMAC-SHA256 signature for receiver verification.
"""
from __future__ import annotations

import hashlib
import hmac as hmac_mod
import json
import time

import httpx

from case_service.hxbridge.protocol import register_connector
from case_service.hxbridge.security import validate_outbound_url


@register_connector("webhook")
class WebhookConnector:
    display_name = "Webhook (Outbound)"
    description  = "Send a JSON payload to any webhook URL with optional HMAC signature."

    config_schema = {
        "type": "object",
        "required": ["url"],
        "properties": {
            "url":             {"type": "string", "description": "Webhook destination URL"},
            "method":          {"type": "string", "enum": ["POST", "PUT"], "default": "POST"},
            "sign_payload":    {"type": "boolean", "default": False, "description": "Add HMAC-SHA256 signature header"},
            "signature_header":{"type": "string", "default": "X-Helix-Signature"},
            "timeout_seconds": {"type": "integer", "default": 15},
        },
    }

    credential_schema = {
        "type": "object",
        "properties": {
            "secret": {"type": "string", "description": "HMAC signing secret (used when sign_payload=true)"},
        },
    }

    def __init__(self, config: dict, credentials: dict) -> None:
        self._config = config
        self._creds  = credentials

    async def execute(self, input_data: dict) -> dict:
        url     = self._config["url"]
        await validate_outbound_url(url)
        method  = self._config.get("method", "POST").upper()
        timeout = self._config.get("timeout_seconds", 15)
        headers = {"Content-Type": "application/json"}

        if self._config.get("sign_payload") and self._creds.get("secret"):
            body = json.dumps(input_data, sort_keys=True).encode()
            ts   = str(int(time.time()))
            sig  = hmac_mod.new(
                self._creds["secret"].encode(),
                f"{ts}.".encode() + body,
                hashlib.sha256,
            ).hexdigest()
            header_name = self._config.get("signature_header", "X-Helix-Signature")
            headers[header_name] = f"t={ts},v1={sig}"

        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            r = await client.request(method, url, headers=headers, json=input_data)
            r.raise_for_status()
            return {"status_code": r.status_code, "delivered": True}

    async def test(self) -> bool:
        try:
            await validate_outbound_url(self._config.get("url", ""))
            async with httpx.AsyncClient(timeout=5, follow_redirects=False) as client:
                r = await client.head(self._config.get("url", ""))
                return r.status_code < 500
        except Exception:
            return False
