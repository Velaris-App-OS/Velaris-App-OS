"""Onfido KYC connector — hosted SDK flow (no raw PII stored).

Credentials: api_token (Onfido API key)
Config:      region (EU|US|CA, default EU), webhook_token
"""
from __future__ import annotations

import hashlib
import hmac
import logging

import httpx

from case_service.hxbridge.protocol import register_connector

logger = logging.getLogger(__name__)

_REGION_BASE = {
    "EU": "https://api.eu.onfido.com/v3.6",
    "US": "https://api.us.onfido.com/v3.6",
    "CA": "https://api.ca.onfido.com/v3.6",
}
_TIMEOUT = 15.0


@register_connector("onfido")
class OnfidoConnector:
    display_name = "Onfido"
    description  = "Identity verification via Onfido hosted SDK — document + biometric check"

    config_schema = {
        "type": "object",
        "properties": {
            "region": {"type": "string", "enum": ["EU", "US", "CA"], "description": "API region"},
        },
    }
    credential_schema = {
        "type": "object",
        "required": ["api_token"],
        "properties": {
            "api_token":     {"type": "string", "description": "Onfido API token"},
            "webhook_token": {"type": "string", "description": "Onfido webhook token for HMAC verification"},
        },
    }

    def __init__(self, config: dict, credentials: dict) -> None:
        self._config = config
        self._creds  = credentials
        self._token  = credentials.get("api_token", "")
        region = config.get("region", "EU")
        self._base = _REGION_BASE.get(region, _REGION_BASE["EU"])

    def _headers(self) -> dict:
        return {"Authorization": f"Token token={self._token}", "Content-Type": "application/json"}

    async def execute(self, input_data: dict) -> dict:
        op = input_data.get("operation", "create_check")
        if op == "create_applicant_and_token":
            return await self._create_applicant_and_token(input_data)
        if op == "get_check":
            return await self._get_check(input_data)
        raise ValueError(f"Unknown Onfido operation: {op!r}")

    async def test(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                r = await c.get(f"{self._base}/applicants?per_page=1", headers=self._headers())
            return r.status_code == 200
        except Exception as exc:
            logger.warning("Onfido test failed: %s", exc)
            return False

    async def _create_applicant_and_token(self, data: dict) -> dict:
        """Create an Onfido applicant and return an SDK token for the hosted flow."""
        first_name = data.get("first_name", "Customer")
        last_name  = data.get("last_name", "")

        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            # 1. Create applicant
            r = await c.post(
                f"{self._base}/applicants",
                headers=self._headers(),
                json={"first_name": first_name, "last_name": last_name},
            )
            if r.status_code not in (200, 201):
                raise RuntimeError(f"Onfido applicant error {r.status_code}: {r.text}")
            applicant = r.json()
            applicant_id = applicant["id"]

            # 2. Generate SDK token for hosted flow
            r2 = await c.post(
                f"{self._base}/sdk_token",
                headers=self._headers(),
                json={"applicant_id": applicant_id, "referrer": data.get("referrer", "*")},
            )
            if r2.status_code not in (200, 201):
                raise RuntimeError(f"Onfido SDK token error {r2.status_code}: {r2.text}")
            token_data = r2.json()

            # 3. Create check (triggers the verification workflow)
            r3 = await c.post(
                f"{self._base}/checks",
                headers=self._headers(),
                json={"applicant_id": applicant_id, "report_names": ["document", "facial_similarity_photo"]},
            )
            if r3.status_code not in (200, 201):
                raise RuntimeError(f"Onfido check error {r3.status_code}: {r3.text}")
            check = r3.json()

        return {
            "applicant_id": applicant_id,
            "check_id":     check["id"],
            "sdk_token":    token_data.get("token"),
            # Hosted URL: customer opens this to complete document + selfie capture
            "verification_url": f"https://id.onfido.com/?token={token_data.get('token')}",
        }

    async def _get_check(self, data: dict) -> dict:
        check_id = data["check_id"]
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(f"{self._base}/checks/{check_id}", headers=self._headers())
        if r.status_code != 200:
            raise RuntimeError(f"Onfido get_check error {r.status_code}: {r.text}")
        body = r.json()
        return {"check_id": check_id, "status": body.get("status"), "result": body.get("result")}

    def verify_webhook(self, payload: bytes, sig_header: str) -> bool:
        """Verify Onfido webhook using X-SHA2-Signature header."""
        secret = self._creds.get("webhook_token", "")
        if not secret:
            return False
        try:
            expected = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
            return hmac.compare_digest(expected, sig_header.strip())
        except Exception:
            return False
