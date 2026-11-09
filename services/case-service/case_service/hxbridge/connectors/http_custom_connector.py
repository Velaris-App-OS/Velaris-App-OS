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


def _host_is_templated(template: str) -> bool:
    """Does a {placeholder} fall in the scheme or host part of the url?"""
    rest = template.split("://", 1)[1] if "://" in template else template
    authority = rest.split("/", 1)[0]
    return "{" in authority or "{" in template.split("://", 1)[0]


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
        # Marketplace Layer-1 (mig 122): a marketplace-installed connector
        # carries its grant anchor; every call is allowlist-checked against
        # the ADMIN-GRANTED domains (absent until approval = block all),
        # SSRF-guarded, and logged. User-built connectors have no block.
        self._marketplace = config.get("_marketplace")
        # Set only by the executor (velaris_shared.hxbridge.security.runtime_config):
        # the operator's own connector may call its LAN; any other may not.
        self._platform_owned = config.get("_platform_owned") is True
        # A named operation (velaris_shared.hxbridge.operations): its path is
        # joined to this connector's own url, placeholders URL-encoded.
        self._op_path = config.get("_op_path") if isinstance(config.get("_op_path"), str) else None

    async def _marketplace_gate(self, url: str) -> None:
        from urllib.parse import urlparse

        from velaris_shared.apps import grant_checks as mkt_grants

        mkt = self._marketplace or {}
        granted = mkt.get("granted_domains")
        host = urlparse(url).hostname or ""
        if not granted or not mkt_grants.host_allowed(host, granted):
            await mkt_grants.log_marketplace_call(
                grant_id=mkt.get("grant_id"), package_id=mkt.get("package_id", "?"),
                url=url, method=self._method, status="blocked",
                is_declared=bool(granted) and mkt_grants.host_allowed(host, granted))
            if not granted:
                raise RuntimeError(
                    "Marketplace connector is not activated — no capability grant "
                    "has been approved for it.")
            raise RuntimeError(
                f"Marketplace connector blocked: '{host}' is not in the granted "
                "outbound domains.")
        from case_service.hxbridge.security import validate_outbound_url
        await validate_outbound_url(url)   # SSRF guard — raises on internal targets

    async def execute(self, input_data: dict) -> dict:
        url     = _substitute(self._url_tmpl, input_data)
        if self._op_path:
            from velaris_shared.hxbridge.operations import fill_path
            url = url.rstrip("/") + fill_path(self._op_path, input_data)
        if self._marketplace is not None:
            await self._marketplace_gate(url)
        # SSRF guard after the placeholders are filled (2026-11-04). It ran for
        # marketplace connectors only: any tenant's admin built one pointing at
        # 127.0.0.1 and read the reply — and a {placeholder} in the host let a
        # form's input choose the target. The operator's own keep their LAN.
        # A placeholder in the HOST part lets whoever fills the input choose the
        # target, so even the operator's connector is then held to public ones.
        extra_headers: dict = {}
        extensions: dict = {}
        shown_url = url                      # what the call log records (the name, not the pinned address)
        if not self._platform_owned or self._marketplace is not None or _host_is_templated(self._url_tmpl):
            # Checked AND pinned: sent to the address that was checked.
            from case_service.hxbridge.security import pinned_request_args
            url, extra_headers, extensions = await pinned_request_args(url)
        headers = {k: _substitute(v, input_data) for k, v in self._headers.items()}
        headers.update(extra_headers)
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
            resp = await client.request(self._method, url, headers=headers, content=body,
                                        extensions=extensions)

        if self._marketplace is not None:
            from velaris_shared.apps import grant_checks as mkt_grants
            await mkt_grants.log_marketplace_call(
                grant_id=self._marketplace.get("grant_id"),
                package_id=self._marketplace.get("package_id", "?"),
                url=shown_url, method=self._method, status="allowed",
                http_status_code=resp.status_code)

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
            if self._marketplace is not None:
                await self._marketplace_gate(url)   # inert/blocked = test fails
            if not self._platform_owned:
                from case_service.hxbridge.security import validate_outbound_url
                await validate_outbound_url(url)    # an internal target = test fails
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.request("GET", url)
            return resp.status_code < 500
        except Exception:
            return False
