"""Stripe payment connector — implements ConnectorProtocol via httpx (no SDK).

Credentials schema:
    secret_key:       Stripe secret API key (sk_live_... / sk_test_...)
    webhook_secret:   Stripe webhook signing secret (whsec_...)

Config schema:
    currency:         Default currency code (default: usd)
    success_url:      Redirect URL after successful payment
    cancel_url:       Redirect URL if payment is cancelled
"""
from __future__ import annotations

import hashlib
import hmac
import logging

import httpx

from case_service.hxbridge.protocol import register_connector, ConnectorProtocol

logger = logging.getLogger(__name__)

_STRIPE_API = "https://api.stripe.com/v1"
_TIMEOUT = 15.0


@register_connector("stripe")
class StripeConnector:
    display_name = "Stripe"
    description  = "Payment processing via Stripe — charge, refund, payment intent"

    config_schema = {
        "type": "object",
        "properties": {
            "currency":    {"type": "string", "description": "Default currency (e.g. usd, gbp)"},
            "success_url": {"type": "string", "description": "Redirect URL after payment success"},
            "cancel_url":  {"type": "string", "description": "Redirect URL if payment is cancelled"},
        },
    }

    credential_schema = {
        "type": "object",
        "required": ["secret_key"],
        "properties": {
            "secret_key":     {"type": "string", "description": "Stripe secret key (sk_live_... / sk_test_...)"},
            "webhook_secret": {"type": "string", "description": "Stripe webhook signing secret (whsec_...)"},
        },
    }

    def __init__(self, config: dict, credentials: dict) -> None:
        self._config = config
        self._creds  = credentials
        self._secret_key = credentials.get("secret_key", "")

    # ── ConnectorProtocol ──────────────────────────────────────────────────────

    async def execute(self, input_data: dict) -> dict:
        """Dispatch to the correct Stripe operation."""
        operation = input_data.get("operation", "checkout_session")
        if operation == "checkout_session":
            return await self._create_checkout_session(input_data)
        if operation == "get_status":
            return await self._get_payment_intent_status(input_data)
        if operation == "refund":
            return await self._refund(input_data)
        raise ValueError(f"Unknown Stripe operation: {operation!r}")

    async def test(self) -> bool:
        """Validate API key by calling GET /v1/balance."""
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                r = await client.get(
                    f"{_STRIPE_API}/balance",
                    auth=(self._secret_key, ""),
                )
            return r.status_code == 200
        except Exception as exc:
            logger.warning("Stripe test failed: %s", exc)
            return False

    # ── Operations ─────────────────────────────────────────────────────────────

    async def _create_checkout_session(self, data: dict) -> dict:
        """Create a Stripe Checkout Session and return the hosted URL."""
        amount_cents   = int(data["amount_cents"])
        currency       = data.get("currency") or self._config.get("currency", "usd")
        description    = data.get("description", "Payment")
        customer_email = data.get("customer_email")
        success_url    = data.get("success_url") or self._config.get("success_url", "https://example.com/success")
        cancel_url     = data.get("cancel_url")  or self._config.get("cancel_url",  "https://example.com/cancel")
        idempotency_key = data.get("idempotency_key", "")

        payload = {
            "mode": "payment",
            "line_items[0][price_data][currency]":     currency,
            "line_items[0][price_data][unit_amount]":  amount_cents,
            "line_items[0][price_data][product_data][name]": description,
            "line_items[0][quantity]":                 1,
            "success_url": success_url,
            "cancel_url":  cancel_url,
            "payment_intent_data[description]": description,
        }
        if customer_email:
            payload["customer_email"] = customer_email

        headers: dict[str, str] = {}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.post(
                f"{_STRIPE_API}/checkout/sessions",
                auth=(self._secret_key, ""),
                data=payload,
                headers=headers,
            )

        body = r.json()
        if r.status_code not in (200, 201):
            raise RuntimeError(f"Stripe error {r.status_code}: {body.get('error', {}).get('message', r.text)}")

        return {
            "session_id":        body.get("id"),
            "payment_intent_id": body.get("payment_intent"),
            "checkout_url":      body.get("url"),
            "status":            body.get("status"),
            "amount_cents":      amount_cents,
            "currency":          currency,
        }

    async def _get_payment_intent_status(self, data: dict) -> dict:
        """Retrieve current status of a PaymentIntent."""
        pi_id = data["payment_intent_id"]
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(
                f"{_STRIPE_API}/payment_intents/{pi_id}",
                auth=(self._secret_key, ""),
            )
        body = r.json()
        if r.status_code != 200:
            raise RuntimeError(f"Stripe error {r.status_code}: {body.get('error', {}).get('message', r.text)}")
        return {
            "payment_intent_id": pi_id,
            "status":            body.get("status"),
            "amount_cents":      body.get("amount"),
            "currency":          body.get("currency"),
        }

    async def _refund(self, data: dict) -> dict:
        """Refund a PaymentIntent (full or partial)."""
        pi_id        = data["payment_intent_id"]
        amount_cents = data.get("amount_cents")  # None = full refund
        payload: dict[str, str | int] = {"payment_intent": pi_id}
        if amount_cents:
            payload["amount"] = int(amount_cents)

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.post(
                f"{_STRIPE_API}/refunds",
                auth=(self._secret_key, ""),
                data={k: str(v) for k, v in payload.items()},
            )
        body = r.json()
        if r.status_code not in (200, 201):
            raise RuntimeError(f"Stripe refund error {r.status_code}: {body.get('error', {}).get('message', r.text)}")
        return {
            "refund_id":  body.get("id"),
            "status":     body.get("status"),
            "amount_cents": body.get("amount"),
        }

    # ── Webhook HMAC verification ──────────────────────────────────────────────

    def verify_webhook(self, payload: bytes, sig_header: str) -> bool:
        """Verify a Stripe webhook signature header.

        Header format: t=<timestamp>,v1=<hmac_hex>[,v0=<old_hmac>]
        """
        secret = self._creds.get("webhook_secret", "")
        if not secret:
            logger.warning("Stripe webhook_secret not configured — skipping HMAC verification")
            return False
        try:
            parts = dict(p.split("=", 1) for p in sig_header.split(","))
            timestamp = parts.get("t", "")
            v1        = parts.get("v1", "")
            signed    = f"{timestamp}.{payload.decode()}"
            expected  = hmac.new(secret.encode(), signed.encode(), hashlib.sha256).hexdigest()
            return hmac.compare_digest(expected, v1)
        except Exception as exc:
            logger.warning("Stripe HMAC verification error: %s", exc)
            return False
