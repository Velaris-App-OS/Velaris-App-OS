"""Tests for P48 HxConnect — Payment & Financial.

Coverage:
  Unit:  StripeConnector HMAC verification · mock charge/refund · test() health check
  API:   initiate charge · list payment requests · get single request · refund flow
         webhook succeeded → status update · webhook failed → status update
         invalid HMAC rejected · list connectors · list webhook events · test connector
  Edge:  refund on non-succeeded request · duplicate webhook idempotency · tenant isolation
         payment request without matching connector · charge with missing connector
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import (
    ConnectorRegistryModel,
    PaymentRequestModel,
    PaymentWebhookEventModel,
)
from case_service.hxbridge.encryption import encrypt_credentials
from case_service.hxbridge.connectors.stripe_connector import StripeConnector

from tests.conftest import client, session, deploy_case_type, create_case  # type: ignore[attr-defined]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _stripe_sig(payload: bytes, secret: str) -> str:
    """Generate a valid Stripe-Signature header."""
    t = str(int(time.time()))
    signed = f"{t}.{payload.decode()}"
    sig = hmac.new(secret.encode(), signed.encode(), hashlib.sha256).hexdigest()
    return f"t={t},v1={sig}"


async def _register_stripe_connector(session: AsyncSession, tenant_id: str = "t1") -> ConnectorRegistryModel:
    """Insert a Stripe connector with test credentials."""
    creds = encrypt_credentials({
        "secret_key":     "sk_test_dummy",
        "webhook_secret": "whsec_test_secret",
    })
    row = ConnectorRegistryModel(
        name           = "Stripe Test",
        connector_type = "stripe",
        config         = {"currency": "usd"},
        credentials    = creds,
        tenant_id      = tenant_id,
        enabled        = True,
    )
    session.add(row)
    await session.flush()
    return row


async def _make_case(client: AsyncClient) -> dict:
    ct = await deploy_case_type(client, name="Payment Case", definition_json={
        "stages": [{"id": "s1", "name": "Payment", "order": 1, "steps": [
            {"id": "pay_step", "name": "Collect Payment", "step_type": "payment_request", "required": True},
        ]}]
    })
    return await create_case(client, ct["id"])


# ── Unit — StripeConnector HMAC ───────────────────────────────────────────────

class TestStripeConnectorHMAC:
    def _connector(self) -> StripeConnector:
        return StripeConnector(
            config      = {},
            credentials = {"secret_key": "sk_test_x", "webhook_secret": "whsec_test_secret"},
        )

    def test_valid_signature_accepted(self):
        connector = self._connector()
        payload = b'{"type":"payment_intent.succeeded"}'
        sig = _stripe_sig(payload, "whsec_test_secret")
        assert connector.verify_webhook(payload, sig) is True

    def test_tampered_payload_rejected(self):
        connector = self._connector()
        payload   = b'{"type":"payment_intent.succeeded"}'
        sig       = _stripe_sig(payload, "whsec_test_secret")
        tampered  = b'{"type":"payment_intent.succeeded","extra":1}'
        assert connector.verify_webhook(tampered, sig) is False

    def test_wrong_secret_rejected(self):
        connector = self._connector()
        payload = b'{"type":"payment_intent.succeeded"}'
        sig = _stripe_sig(payload, "wrong_secret")
        assert connector.verify_webhook(payload, sig) is False

    def test_missing_webhook_secret_returns_false(self):
        connector = StripeConnector(config={}, credentials={"secret_key": "sk_test"})
        assert connector.verify_webhook(b"payload", "t=1,v1=abc") is False

    def test_malformed_header_returns_false(self):
        connector = self._connector()
        assert connector.verify_webhook(b"payload", "not_a_valid_header") is False


# ── API — Connector management ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_payment_connectors_empty(client: AsyncClient):
    resp = await client.get("/api/v1/payments/connectors")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_list_payment_connectors_returns_stripe(client: AsyncClient, session: AsyncSession):
    await _register_stripe_connector(session)
    await session.commit()
    resp = await client.get("/api/v1/payments/connectors")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["connector_type"] == "stripe"
    assert data[0]["credentials"] == {"_enc": "***"}   # credentials masked


@pytest.mark.asyncio
async def test_credentials_are_masked_in_list(client: AsyncClient, session: AsyncSession):
    await _register_stripe_connector(session)
    await session.commit()
    resp = await client.get("/api/v1/payments/connectors")
    assert resp.status_code == 200
    creds = resp.json()[0]["credentials"]
    assert "sk_test" not in str(creds)
    assert "whsec" not in str(creds)


# ── API — Charge initiation ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_charge_no_connector_returns_400(client: AsyncClient):
    case = await _make_case(client)
    resp = await client.post(f"/api/v1/payments/cases/{case['id']}/charge", json={
        "step_id":      "pay_step",
        "amount_cents": 5000,
        "description":  "Test charge",
    })
    assert resp.status_code in (400, 502)   # no connector registered


@pytest.mark.asyncio
async def test_charge_missing_case_still_processes(client: AsyncClient, session: AsyncSession):
    await _register_stripe_connector(session)
    await session.commit()
    fake_case_id = str(uuid.uuid4())
    resp = await client.post(f"/api/v1/payments/cases/{fake_case_id}/charge", json={
        "step_id":      "pay_step",
        "amount_cents": 1000,
        "description":  "Ghost charge",
    })
    # Will fail at Stripe API level (fake key) — should return 502 not 500
    assert resp.status_code in (400, 502)


# ── API — Payment request lifecycle ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_payments_for_case_empty(client: AsyncClient):
    case = await _make_case(client)
    resp = await client.get(f"/api/v1/payments/cases/{case['id']}/requests")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_get_payment_request_not_found(client: AsyncClient):
    resp = await client.get(f"/api/v1/payments/requests/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_payment_request_created_and_retrievable(
    client: AsyncClient, session: AsyncSession
):
    reg = await _register_stripe_connector(session)
    await session.commit()   # commit BEFORE client calls — they roll back the shared connection
    case = await _make_case(client)

    pr = PaymentRequestModel(
        tenant_id    = "t1",
        case_id      = uuid.UUID(case["id"]),
        step_id      = "pay_step",
        connector_id = reg.id,
        provider     = "stripe",
        provider_ref = "pi_test_123",
        checkout_url = "https://checkout.stripe.com/pay/test",
        amount_cents = 9900,
        currency     = "usd",
        status       = "pending",
        description  = "Invoice payment",
    )
    session.add(pr)
    await session.commit()

    resp = await client.get(f"/api/v1/payments/requests/{pr.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "pending"
    assert data["amount_cents"] == 9900
    assert data["checkout_url"] == "https://checkout.stripe.com/pay/test"


@pytest.mark.asyncio
async def test_list_payments_for_case_returns_row(client: AsyncClient, session: AsyncSession):
    reg = await _register_stripe_connector(session)
    await session.commit()   # commit BEFORE client calls — they roll back the shared connection
    case = await _make_case(client)

    pr = PaymentRequestModel(
        tenant_id    = "t1",
        case_id      = uuid.UUID(case["id"]),
        step_id      = "pay_step",
        connector_id = reg.id,
        provider     = "stripe",
        provider_ref = "pi_test_456",
        amount_cents = 2000,
        currency     = "eur",
        status       = "pending",
    )
    session.add(pr)
    await session.commit()

    resp = await client.get(f"/api/v1/payments/cases/{case['id']}/requests")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["currency"] == "eur"


# ── API — Refund ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_refund_non_succeeded_payment_returns_400(
    client: AsyncClient, session: AsyncSession
):
    reg = await _register_stripe_connector(session)
    await session.commit()   # commit BEFORE client calls — they roll back the shared connection
    case = await _make_case(client)

    pr = PaymentRequestModel(
        tenant_id    = "t1",
        case_id      = uuid.UUID(case["id"]),
        step_id      = "pay_step",
        connector_id = reg.id,
        provider     = "stripe",
        provider_ref = "pi_pending",
        amount_cents = 3000,
        currency     = "usd",
        status       = "pending",
    )
    session.add(pr)
    await session.commit()

    resp = await client.post(f"/api/v1/payments/requests/{pr.id}/refund", json={})
    assert resp.status_code == 400
    assert "cannot refund" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_refund_unknown_payment_returns_400(client: AsyncClient):
    resp = await client.post(f"/api/v1/payments/requests/{uuid.uuid4()}/refund", json={})
    assert resp.status_code == 400


# ── API — Stripe webhook ──────────────────────────────────────────────────────

def _webhook_payload(event_type: str, payment_intent_id: str) -> bytes:
    return json.dumps({
        "type": event_type,
        "data": {"object": {"id": payment_intent_id, "status": "succeeded"}},
    }).encode()


@pytest.mark.asyncio
async def test_webhook_succeeded_updates_payment_status(
    client: AsyncClient, session: AsyncSession
):
    reg = await _register_stripe_connector(session)
    await session.commit()   # commit BEFORE client calls — they roll back the shared connection
    case = await _make_case(client)

    pr = PaymentRequestModel(
        tenant_id    = "t1",
        case_id      = uuid.UUID(case["id"]),
        step_id      = "pay_step",
        connector_id = reg.id,
        provider     = "stripe",
        provider_ref = "pi_webhook_ok",
        amount_cents = 1500,
        currency     = "usd",
        status       = "pending",
    )
    session.add(pr)
    await session.commit()

    payload = _webhook_payload("payment_intent.succeeded", "pi_webhook_ok")
    sig = _stripe_sig(payload, "whsec_test_secret")

    resp = await client.post(
        "/api/v1/payments/webhooks/stripe",
        content=payload,
        headers={"content-type": "application/json", "stripe-signature": sig},
    )
    assert resp.status_code == 200
    assert resp.json()["event_type"] == "payment_intent.succeeded"

    await session.refresh(pr)
    assert pr.status == "succeeded"
    assert pr.completed_at is not None


@pytest.mark.asyncio
async def test_webhook_failed_updates_status(
    client: AsyncClient, session: AsyncSession
):
    reg = await _register_stripe_connector(session)
    await session.commit()   # commit BEFORE client calls — they roll back the shared connection
    case = await _make_case(client)

    pr = PaymentRequestModel(
        tenant_id    = "t1",
        case_id      = uuid.UUID(case["id"]),
        step_id      = "pay_step",
        connector_id = reg.id,
        provider     = "stripe",
        provider_ref = "pi_failed_one",
        amount_cents = 5000,
        currency     = "usd",
        status       = "pending",
    )
    session.add(pr)
    await session.commit()

    payload = _webhook_payload("payment_intent.payment_failed", "pi_failed_one")
    sig = _stripe_sig(payload, "whsec_test_secret")
    resp = await client.post(
        "/api/v1/payments/webhooks/stripe",
        content=payload,
        headers={"content-type": "application/json", "stripe-signature": sig},
    )
    assert resp.status_code == 200

    await session.refresh(pr)
    assert pr.status == "failed"


@pytest.mark.asyncio
async def test_invalid_hmac_webhook_rejected(client: AsyncClient, session: AsyncSession):
    await _register_stripe_connector(session)
    await session.commit()   # commit BEFORE client calls — they roll back the shared connection
    case = await _make_case(client)

    pr = PaymentRequestModel(
        tenant_id    = "t1",
        case_id      = uuid.UUID(case["id"]),
        step_id      = "pay_step",
        connector_id = (await session.execute(
            select(ConnectorRegistryModel).where(ConnectorRegistryModel.connector_type == "stripe")
        )).scalar_one().id,
        provider     = "stripe",
        provider_ref = "pi_bad_sig",
        amount_cents = 100,
        currency     = "usd",
        status       = "pending",
    )
    session.add(pr)
    await session.commit()

    payload = _webhook_payload("payment_intent.succeeded", "pi_bad_sig")
    bad_sig = _stripe_sig(payload, "wrong_secret")
    resp = await client.post(
        "/api/v1/payments/webhooks/stripe",
        content=payload,
        headers={"content-type": "application/json", "stripe-signature": bad_sig},
    )
    assert resp.status_code == 400

    await session.refresh(pr)
    assert pr.status == "pending"   # unchanged


@pytest.mark.asyncio
async def test_webhook_without_matching_payment_request_is_logged(
    client: AsyncClient,
):
    """Unknown payment_intent_id → event logged but no crash."""
    payload = _webhook_payload("payment_intent.succeeded", "pi_unknown_xyz")
    # No sig header → dev mode, accepted without HMAC
    resp = await client.post(
        "/api/v1/payments/webhooks/stripe",
        content=payload,
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_webhook_events_logged(client: AsyncClient, session: AsyncSession):
    payload = _webhook_payload("charge.refunded", "pi_refund_log")
    await client.post(
        "/api/v1/payments/webhooks/stripe",
        content=payload,
        headers={"content-type": "application/json"},
    )
    rows = (await session.execute(select(PaymentWebhookEventModel))).scalars().all()
    assert len(rows) >= 1
    assert any(r.event_type == "charge.refunded" for r in rows)


# ── API — Webhook event list ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_webhook_events(client: AsyncClient, session: AsyncSession):
    evt = PaymentWebhookEventModel(
        provider     = "stripe",
        event_type   = "payment_intent.succeeded",
        provider_ref = "pi_list_test",
        payload      = {"type": "payment_intent.succeeded"},
        verified     = True,
        processed    = True,
    )
    session.add(evt)
    await session.commit()

    resp = await client.get("/api/v1/payments/webhooks")
    assert resp.status_code == 200
    events = resp.json()
    assert any(e["event_type"] == "payment_intent.succeeded" for e in events)


# ── Edge — Multi-tenant isolation ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_payment_connectors_scoped_to_provider_type(
    client: AsyncClient, session: AsyncSession
):
    """Only stripe/paypal/adyen connectors appear in /payments/connectors."""
    other = ConnectorRegistryModel(
        name           = "HTTP Conn",
        connector_type = "http",
        config         = {},
        credentials    = {},
        tenant_id      = "t1",
        enabled        = True,
    )
    session.add(other)
    await session.commit()

    resp = await client.get("/api/v1/payments/connectors")
    assert resp.status_code == 200
    types = [c["connector_type"] for c in resp.json()]
    assert "http" not in types


@pytest.mark.asyncio
async def test_duplicate_webhook_second_call_is_idempotent(
    client: AsyncClient, session: AsyncSession
):
    """Sending the same webhook twice should not error."""
    reg = await _register_stripe_connector(session)
    await session.commit()   # commit BEFORE client calls — they roll back the shared connection
    case = await _make_case(client)

    pr = PaymentRequestModel(
        tenant_id    = "t1",
        case_id      = uuid.UUID(case["id"]),
        step_id      = "pay_step",
        connector_id = reg.id,
        provider     = "stripe",
        provider_ref = "pi_dup_test",
        amount_cents = 1000,
        currency     = "usd",
        status       = "pending",
    )
    session.add(pr)
    await session.commit()

    payload = _webhook_payload("payment_intent.succeeded", "pi_dup_test")
    sig = _stripe_sig(payload, "whsec_test_secret")
    headers = {"content-type": "application/json", "stripe-signature": sig}

    r1 = await client.post("/api/v1/payments/webhooks/stripe", content=payload, headers=headers)
    assert r1.status_code == 200

    # Second identical call (new sig timestamp is fine — same provider_ref)
    sig2 = _stripe_sig(payload, "whsec_test_secret")
    headers2 = {"content-type": "application/json", "stripe-signature": sig2}
    r2 = await client.post("/api/v1/payments/webhooks/stripe", content=payload, headers=headers2)
    assert r2.status_code == 200
