"""Salesforce CRM connector — outbound only (v1).

Credentials: client_id, client_secret, refresh_token, instance_url
Config:      api_version (default 59.0)

Uses OAuth2 refresh_token flow to obtain short-lived access tokens.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from case_service.hxbridge.protocol import register_connector

logger = logging.getLogger(__name__)
_TIMEOUT = 15.0


@register_connector("salesforce")
class SalesforceConnector:
    display_name = "Salesforce"
    description  = "Sync case data to Salesforce — creates/updates Contacts and Cases"

    config_schema = {
        "type": "object",
        "properties": {
            "api_version":   {"type": "string", "description": "Salesforce API version (e.g. 59.0)"},
            "instance_url":  {"type": "string", "description": "Your Salesforce instance URL"},
        },
    }
    credential_schema = {
        "type": "object",
        "required": ["client_id", "client_secret", "refresh_token"],
        "properties": {
            "client_id":     {"type": "string"},
            "client_secret": {"type": "string"},
            "refresh_token": {"type": "string"},
        },
    }

    def __init__(self, config: dict, credentials: dict) -> None:
        self._config       = config
        self._creds        = credentials
        self._instance_url = config.get("instance_url", "https://login.salesforce.com")
        self._version      = config.get("api_version", "59.0")
        self._access_token: str | None = None
        self._token_expiry: float = 0

    async def _access_token_valid(self) -> str:
        """Return a valid access token, refreshing if expired."""
        if self._access_token and time.time() < self._token_expiry - 60:
            return self._access_token
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post("https://login.salesforce.com/services/oauth2/token", data={
                "grant_type":    "refresh_token",
                "client_id":     self._creds.get("client_id", ""),
                "client_secret": self._creds.get("client_secret", ""),
                "refresh_token": self._creds.get("refresh_token", ""),
            })
        if r.status_code != 200:
            raise RuntimeError(f"Salesforce token refresh failed: {r.text}")
        body = r.json()
        self._access_token = body["access_token"]
        self._instance_url = body.get("instance_url", self._instance_url)
        self._token_expiry = time.time() + 7200   # SF access tokens last ~2h
        # SD-7: flag that credentials were refreshed so executor can update credentials_updated_at
        self._token_refreshed = True
        return self._access_token

    def _headers(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def _base(self) -> str:
        return f"{self._instance_url}/services/data/v{self._version}"

    async def execute(self, input_data: dict) -> dict:
        op = input_data.get("operation", "upsert_contact")
        token = await self._access_token_valid()
        if op == "upsert_contact":
            return await self._upsert_contact(token, input_data)
        if op == "create_case":
            return await self._create_case(token, input_data)
        if op == "upsert_contact_and_case":
            contact = await self._upsert_contact(token, input_data)
            case    = await self._create_case(token, {**input_data, "contact_id": contact.get("id")})
            return {**contact, "case_id": case.get("id"), "case_url": case.get("url")}
        raise ValueError(f"Unknown Salesforce operation: {op!r}")

    async def test(self) -> bool:
        try:
            token = await self._access_token_valid()
            async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                r = await c.get(f"{self._base()}/limits", headers=self._headers(token))
            return r.status_code == 200
        except Exception as exc:
            logger.warning("Salesforce test failed: %s", exc)
            return False

    async def _upsert_contact(self, token: str, data: dict) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "FirstName": data.get("first_name", ""),
            "LastName":  data.get("last_name", "Customer"),
            "Email":     data.get("email", ""),
            "Phone":     data.get("phone", ""),
            "Description": data.get("description", ""),
        }
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(f"{self._base()}/sobjects/Contact", headers=self._headers(token), json=payload)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"Salesforce Contact error {r.status_code}: {r.text}")
        rec_id = r.json().get("id", "")
        return {"id": rec_id, "type": "Contact", "url": f"{self._instance_url}/{rec_id}"}

    async def _create_case(self, token: str, data: dict) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "Subject":     data.get("subject", "Case from Helix"),
            "Description": data.get("description", ""),
            "Status":      "New",
            "Origin":      "Helix BPM",
        }
        if data.get("contact_id"):
            payload["ContactId"] = data["contact_id"]
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(f"{self._base()}/sobjects/Case", headers=self._headers(token), json=payload)
        if r.status_code not in (200, 201):
            raise RuntimeError(f"Salesforce Case error {r.status_code}: {r.text}")
        rec_id = r.json().get("id", "")
        return {"id": rec_id, "type": "Case", "url": f"{self._instance_url}/{rec_id}"}
