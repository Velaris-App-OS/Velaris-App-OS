"""HxBridge — Generic HTTP connector.

Covers REST/JSON APIs with Bearer token, API key header, or Basic auth.
The foundation for all future cloud service connectors (Stripe, Salesforce, etc.)
— they inherit from this and override config_schema + execute().
"""
from __future__ import annotations

import httpx

from case_service.hxbridge.protocol import register_connector


@register_connector("http")
class HttpConnector:
    display_name = "HTTP / REST"
    description  = "Generic HTTP connector for any REST/JSON API."

    config_schema = {
        "type": "object",
        "required": ["base_url", "method"],
        "properties": {
            "base_url":       {"type": "string", "description": "Base URL of the API"},
            "method":         {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
            "path":           {"type": "string", "description": "Path appended to base_url"},
            "headers":        {"type": "object", "description": "Static headers"},
            "auth_type":      {"type": "string", "enum": ["none", "bearer", "api_key", "basic"], "default": "none"},
            "timeout_seconds":{"type": "integer", "default": 30},
        },
    }

    credential_schema = {
        "type": "object",
        "properties": {
            "token":    {"type": "string", "description": "Bearer token or API key value"},
            "username": {"type": "string", "description": "Basic auth username"},
            "password": {"type": "string", "description": "Basic auth password"},
            "header_name": {"type": "string", "description": "Header name for api_key auth", "default": "X-API-Key"},
        },
    }

    def __init__(self, config: dict, credentials: dict) -> None:
        self._config  = config
        self._creds   = credentials

    def _build_headers(self) -> dict:
        headers = dict(self._config.get("headers") or {})
        auth_type = self._config.get("auth_type", "none")
        token = self._creds.get("token", "")

        if auth_type == "bearer" and token:
            headers["Authorization"] = f"Bearer {token}"
        elif auth_type == "api_key" and token:
            header_name = self._creds.get("header_name", "X-API-Key")
            headers[header_name] = token
        elif auth_type == "basic":
            import base64
            user = self._creds.get("username", "")
            pwd  = self._creds.get("password", "")
            encoded = base64.b64encode(f"{user}:{pwd}".encode()).decode()
            headers["Authorization"] = f"Basic {encoded}"

        headers.setdefault("Content-Type", "application/json")
        return headers

    async def execute(self, input_data: dict) -> dict:
        base   = self._config.get("base_url", "").rstrip("/")
        path   = self._config.get("path", "").lstrip("/")
        method = self._config.get("method", "POST").upper()
        timeout = self._config.get("timeout_seconds", 30)
        url    = f"{base}/{path}" if path else base

        async with httpx.AsyncClient(timeout=timeout) as client:
            if method in ("GET", "DELETE"):
                r = await client.request(method, url, headers=self._build_headers(), params=input_data)
            else:
                r = await client.request(method, url, headers=self._build_headers(), json=input_data)

            r.raise_for_status()
            try:
                return r.json()
            except Exception:
                return {"raw": r.text, "status_code": r.status_code}

    async def test(self) -> bool:
        try:
            base = self._config.get("base_url", "").rstrip("/")
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(base, headers=self._build_headers())
                return r.status_code < 500
        except Exception:
            return False
