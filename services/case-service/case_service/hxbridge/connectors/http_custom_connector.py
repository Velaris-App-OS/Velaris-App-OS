"""HttpCustomConnector — user-defined outbound HTTP connector (no code required).

Config shape stored in connector_registry.config:
{
    "method":       "POST",
    "url":          "https://api.example.com/endpoint",
    "headers":      {"X-Api-Key": "{api_key}"},   -- {var} substituted from input_data
    "auth_type":    "none" | "bearer" | "basic",
    "body_template": "{\"field\": \"{case_ref}\"}",  -- {var} substituted
    "response_mapping": {"case_field": "response.json.path"}
}
Credentials: {"token": "...", "username": "...", "password": "..."}
"""
from __future__ import annotations

import json
import re
from typing import Any

import httpx

from case_service.hxbridge.protocol import ConnectorProtocol, register_connector


def _substitute(template: str, data: dict) -> str:
    """Replace {key} placeholders with values from data."""
    def replace(m: re.Match) -> str:
        return str(data.get(m.group(1), m.group(0)))
    return re.sub(r"\{(\w+)\}", replace, template)


def _get_nested(obj: Any, path: str) -> Any:
    """Traverse a dot-path like 'data.id' into a nested dict."""
    for key in path.split("."):
        if isinstance(obj, dict):
            obj = obj.get(key)
        else:
            return None
    return obj


@register_connector("http_custom")
class HttpCustomConnector(ConnectorProtocol):
    name = "Custom HTTP Connector"
    connector_type = "http_custom"
    schema = {
        "config": {
            "method": "GET | POST | PUT | PATCH | DELETE",
            "url": "str (supports {var} placeholders)",
            "headers": "dict (supports {var} placeholders in values)",
            "auth_type": "none | bearer | basic",
            "body_template": "str JSON (supports {var} placeholders)",
            "response_mapping": "dict {case_field: response.json.path}",
        },
        "credentials": {
            "token": "str (for bearer auth)",
            "username": "str (for basic auth)",
            "password": "str (for basic auth)",
        },
    }

    def __init__(self, config: dict, credentials: dict):
        self._method   = config.get("method", "POST").upper()
        self._url_tmpl = config.get("url", "")
        self._headers  = config.get("headers", {})
        self._auth     = config.get("auth_type", "none")
        self._body_tmpl = config.get("body_template", "")
        self._response_mapping = config.get("response_mapping", {})
        self._credentials = credentials

    async def execute(self, input_data: dict) -> dict:
        url     = _substitute(self._url_tmpl, input_data)
        headers = {k: _substitute(v, input_data) for k, v in self._headers.items()}
        headers.setdefault("Content-Type", "application/json")

        if self._auth == "bearer":
            headers["Authorization"] = f"Bearer {self._credentials.get('token', '')}"
        elif self._auth == "basic":
            import base64
            pair = f"{self._credentials.get('username','')}:{self._credentials.get('password','')}"
            headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode()

        body: bytes | None = None
        if self._body_tmpl:
            body = _substitute(self._body_tmpl, input_data).encode()

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.request(self._method, url, headers=headers, content=body)

        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")

        try:
            resp_json = resp.json()
        except Exception:
            resp_json = {"raw": resp.text}

        result: dict = {"status_code": resp.status_code, "response": resp_json}
        for case_field, json_path in self._response_mapping.items():
            result[case_field] = _get_nested(resp_json, json_path)

        return result

    async def test(self) -> bool:
        try:
            url = _substitute(self._url_tmpl, {})
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.request("GET", url)
            return resp.status_code < 500
        except Exception:
            return False
