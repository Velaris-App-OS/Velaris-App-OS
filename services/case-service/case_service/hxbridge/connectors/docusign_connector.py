"""DocuSign e-sign connector — envelope creation + hosted signing URL.

Credentials: integration_key, user_id, account_id, private_key (RSA PEM for JWT auth)
Config:      base_url (sandbox or production)
"""
from __future__ import annotations

import hashlib
import hmac
import logging

import httpx

from case_service.hxbridge.protocol import register_connector

logger = logging.getLogger(__name__)
_TIMEOUT = 15.0


@register_connector("docusign")
class DocuSignConnector:
    display_name = "DocuSign"
    description  = "E-signature via DocuSign — send envelopes, track signing, retrieve signed documents"

    config_schema = {
        "type": "object",
        "properties": {
            "base_url": {"type": "string", "description": "DocuSign base URL (demo or production)"},
        },
    }
    credential_schema = {
        "type": "object",
        "required": ["access_token", "account_id"],
        "properties": {
            "access_token":  {"type": "string", "description": "DocuSign OAuth access token"},
            "account_id":    {"type": "string", "description": "DocuSign account ID"},
            "hmac_key":      {"type": "string", "description": "DocuSign Connect HMAC key for webhook verification"},
        },
    }

    def __init__(self, config: dict, credentials: dict) -> None:
        self._config       = config
        self._creds        = credentials
        self._access_token = credentials.get("access_token", "")
        self._account_id   = credentials.get("account_id", "")
        self._base_url     = config.get("base_url", "https://demo.docusign.net/restapi")

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type":  "application/json",
        }

    async def execute(self, input_data: dict) -> dict:
        op = input_data.get("operation", "create_envelope")
        if op == "create_envelope":
            return await self._create_envelope(input_data)
        if op == "get_status":
            return await self._get_envelope_status(input_data)
        if op == "void_envelope":
            return await self._void_envelope(input_data)
        raise ValueError(f"Unknown DocuSign operation: {op!r}")

    async def test(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
                r = await c.get(
                    f"{self._base_url}/v2.1/accounts/{self._account_id}",
                    headers=self._headers(),
                )
            return r.status_code == 200
        except Exception as exc:
            logger.warning("DocuSign test failed: %s", exc)
            return False

    async def _create_envelope(self, data: dict) -> dict:
        """Create an envelope with a document and return a hosted signing URL."""
        signer_email = data["signer_email"]
        signer_name  = data.get("signer_name", "Customer")
        doc_name     = data.get("document_name", "Document for Signature")
        doc_b64      = data.get("document_base64", "")   # caller passes base64 PDF
        return_url   = data.get("return_url", "https://example.com/signed")

        envelope_def = {
            "emailSubject": f"Please sign: {doc_name}",
            "status":       "sent",
            "documents": [{
                "documentId": "1",
                "name":       doc_name,
                "fileExtension": "pdf",
                "documentBase64": doc_b64 or _PLACEHOLDER_PDF,
            }],
            "recipients": {
                "signers": [{
                    "email":       signer_email,
                    "name":        signer_name,
                    "recipientId": "1",
                    "tabs": {"signHereTabs": [{"documentId": "1", "pageNumber": "1", "xPosition": "100", "yPosition": "100"}]},
                }]
            },
        }

        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(
                f"{self._base_url}/v2.1/accounts/{self._account_id}/envelopes",
                headers=self._headers(),
                json=envelope_def,
            )
            if r.status_code not in (200, 201):
                raise RuntimeError(f"DocuSign envelope error {r.status_code}: {r.text}")
            envelope_id = r.json()["envelopeId"]

            # Get recipient view (hosted signing URL)
            r2 = await c.post(
                f"{self._base_url}/v2.1/accounts/{self._account_id}/envelopes/{envelope_id}/views/recipient",
                headers=self._headers(),
                json={
                    "authenticationMethod": "none",
                    "email":       signer_email,
                    "userName":    signer_name,
                    "recipientId": "1",
                    "returnUrl":   return_url,
                },
            )
            signing_url = r2.json().get("url", "") if r2.status_code == 200 else ""

        return {"envelope_id": envelope_id, "signing_url": signing_url, "status": "sent"}

    async def _get_envelope_status(self, data: dict) -> dict:
        env_id = data["envelope_id"]
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(
                f"{self._base_url}/v2.1/accounts/{self._account_id}/envelopes/{env_id}",
                headers=self._headers(),
            )
        body = r.json()
        return {"envelope_id": env_id, "status": body.get("status"), "completed_date_time": body.get("completedDateTime")}

    async def _void_envelope(self, data: dict) -> dict:
        env_id = data["envelope_id"]
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.put(
                f"{self._base_url}/v2.1/accounts/{self._account_id}/envelopes/{env_id}",
                headers=self._headers(),
                json={"status": "voided", "voidedReason": data.get("reason", "Voided by staff")},
            )
        return {"envelope_id": env_id, "status": "voided", "ok": r.status_code in (200, 201)}

    def verify_webhook(self, payload: bytes, sig_header: str) -> bool:
        """Verify DocuSign Connect HMAC signature (X-DocuSign-Signature-1)."""
        key = self._creds.get("hmac_key", "")
        if not key:
            return False
        try:
            import base64
            expected = base64.b64encode(
                hmac.new(key.encode(), payload, hashlib.sha256).digest()
            ).decode()
            return hmac.compare_digest(expected, sig_header.strip())
        except Exception:
            return False


# Minimal 1-page blank PDF in base64 (used as placeholder when no doc provided)
_PLACEHOLDER_PDF = (
    "JVBERi0xLjQKJcOkw7zDtsOfCjIgMCBvYmoKPDwvTGVuZ3RoIDMgMCBSL0ZpbHRlci9GbGF0ZURlY29kZT4+"
    "CnN0cmVhbQp4nCvkMlAwUDC1NNUzMVcoL04tykvMTQUA"
)
