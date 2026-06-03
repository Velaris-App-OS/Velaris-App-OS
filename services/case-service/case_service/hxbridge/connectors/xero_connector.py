"""Xero accounting connector — outbound invoice generation (v1).

Credentials: client_id, client_secret, refresh_token, tenant_id (Xero org)
Config:      (none required)

Uses OAuth2 PKCE refresh flow. Xero access tokens expire after 30 minutes.
"""
from __future__ import annotations

import logging
import time

import httpx

from case_service.hxbridge.protocol import register_connector

logger = logging.getLogger(__name__)
_TIMEOUT = 15.0
_TOKEN_URL = "https://identity.xero.com/connect/token"
_API_BASE  = "https://api.xero.com/api.xro/2.0"


@register_connector("xero")
class XeroConnector:
    display_name = "Xero"
    description  = "Create invoice drafts in Xero when a case step completes"

    config_schema = {"type": "object", "properties": {}}
    credential_schema = {
        "type": "object",
        "required": ["client_id", "client_secret", "refresh_token", "xero_tenant_id"],
        "properties": {
            "client_id":      {"type": "string"},
            "client_secret":  {"type": "string"},
            "refresh_token":  {"type": "string"},
            "xero_tenant_id": {"type": "string", "description": "Xero organisation tenant ID"},
        },
    }

    def __init__(self, config: dict, credentials: dict) -> None:
        self._config        = config
        self._creds         = credentials
        self._access_token: str | None = None
        self._token_expiry: float = 0

    async def _get_token(self) -> str:
        if self._access_token and time.time() < self._token_expiry - 30:
            return self._access_token
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(_TOKEN_URL, data={
                "grant_type":    "refresh_token",
                "client_id":     self._creds.get("client_id", ""),
                "client_secret": self._creds.get("client_secret", ""),
                "refresh_token": self._creds.get("refresh_token", ""),
            }, headers={"Content-Type": "application/x-www-form-urlencoded"})
        if r.status_code != 200:
            raise RuntimeError(f"Xero token refresh failed: {r.text}")
        body = r.json()
        self._access_token = body["access_token"]
        self._token_expiry = time.time() + body.get("expires_in", 1800)
        # SD-7: Xero rotates refresh_token on every use — capture the new one
        if "refresh_token" in body:
            self._creds["refresh_token"] = body["refresh_token"]
            self._new_refresh_token: str | None = body["refresh_token"]
        return self._access_token

    def _headers(self, token: str) -> dict:
        return {
            "Authorization":  f"Bearer {token}",
            "Xero-Tenant-Id": self._creds.get("xero_tenant_id", ""),
            "Content-Type":   "application/json",
            "Accept":         "application/json",
        }

    async def execute(self, input_data: dict) -> dict:
        op = input_data.get("operation", "create_invoice")
        token = await self._get_token()
        if op == "create_invoice":
            return await self._create_invoice(token, input_data)
        if op == "get_invoice":
            return await self._get_invoice(token, input_data)
        raise ValueError(f"Unknown Xero operation: {op!r}")

    async def test(self) -> bool:
        try:
            token = await self._get_token()
            async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                r = await c.get(f"{_API_BASE}/Organisation", headers=self._headers(token))
            return r.status_code == 200
        except Exception as exc:
            logger.warning("Xero test failed: %s", exc)
            return False

    async def _create_invoice(self, token: str, data: dict) -> dict:
        contact_name = data.get("contact_name", "Customer")
        line_items   = data.get("line_items", [])
        currency     = data.get("currency", "USD").upper()
        reference    = data.get("reference", "")

        if not line_items:
            line_items = [{
                "Description": data.get("description", "Service"),
                "Quantity":    1,
                "UnitAmount":  round(data.get("amount_cents", 0) / 100, 2),
                "AccountCode": "200",
            }]

        invoice_body = {
            "Type":     "ACCREC",     # accounts receivable (customer owes us)
            "Status":   "DRAFT",
            "CurrencyCode": currency,
            "Reference":    reference,
            "Contact":  {"Name": contact_name},
            "LineItems": [
                {
                    "Description": li.get("description", li.get("Description", "")),
                    "Quantity":    li.get("quantity",    li.get("Quantity", 1)),
                    "UnitAmount":  li.get("unit_amount", li.get("UnitAmount", 0)),
                    "AccountCode": li.get("account_code", li.get("AccountCode", "200")),
                }
                for li in line_items
            ],
        }

        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(f"{_API_BASE}/Invoices", headers=self._headers(token), json={"Invoices": [invoice_body]})

        if r.status_code not in (200, 201):
            raise RuntimeError(f"Xero invoice error {r.status_code}: {r.text}")

        inv = r.json().get("Invoices", [{}])[0]
        inv_id  = inv.get("InvoiceID", "")
        inv_num = inv.get("InvoiceNumber", "")
        total   = int(round(inv.get("Total", 0) * 100))
        return {
            "invoice_id":     inv_id,
            "invoice_number": inv_num,
            "invoice_url":    f"https://go.xero.com/AccountsReceivable/View.aspx?InvoiceID={inv_id}",
            "amount_cents":   total,
            "status":         inv.get("Status", "DRAFT").lower(),
        }

    async def _get_invoice(self, token: str, data: dict) -> dict:
        inv_id = data["invoice_id"]
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(f"{_API_BASE}/Invoices/{inv_id}", headers=self._headers(token))
        if r.status_code != 200:
            raise RuntimeError(f"Xero get_invoice error {r.status_code}: {r.text}")
        inv = r.json().get("Invoices", [{}])[0]
        return {"invoice_id": inv_id, "status": inv.get("Status", "").lower(), "amount_cents": int(round(inv.get("Total", 0) * 100))}
