"""HxCheckout — service-token auth + order intake (Phases 2–4).

Exercises the marketplace-install gate, service-token auth, and the full
basket → Order case + checkout_orders + line items + payment path. Like HxTest,
HxCheckout is dark until its marketplace package is installed, so these tests
seed a MarketplaceInstallModel for the test tenant ("default" — the default admin
token carries no tenant).

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select

from case_service.checkout.tokens import generate_token
from case_service.db.models import (
    CaseInstanceModel, CheckoutOrderItemModel, CheckoutOrderModel,
    MarketplaceInstallModel,
)

PKG = "velaris/hxcheckout"


@pytest_asyncio.fixture
async def checkout_installed(session):
    """Seed the marketplace install that enables HxCheckout for tenant 'default'."""
    session.add(MarketplaceInstallModel(
        tenant_id="default", package_id=PKG,
        package_version="1.0.0", package_type="module", approved_by="test-admin"))
    await session.commit()
    yield


async def _mint(session, tenant="default", mode="live") -> str:
    """Mint a service token directly (bypassing the gated admin endpoint)."""
    plaintext, _ = generate_token(session, tenant_id=tenant, label="test", mode=mode)
    await session.commit()
    return plaintext


def _basket(qty=2, unit=1999):
    return {"basket": [{"sku": "TS-BLU-L", "name": "Tee Blue L", "quantity": qty, "unit_price": unit}],
            "customer": {"name": "Jane", "email": "jane@example.com"},
            "shipping": {"country": "GB", "method": "standard"}}


# ── Gate + auth ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_orders_401_bad_token(client):
    r = await client.post("/api/v1/checkout/orders",
                          headers={"X-Velaris-Token": "vsk_live_deadbeefcafe_nope"},
                          json=_basket())
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_orders_401_missing_token(client):
    r = await client.post("/api/v1/checkout/orders", json=_basket())
    assert r.status_code == 401, r.text


@pytest.mark.asyncio
async def test_orders_404_when_not_installed(session, client):
    """Valid token but no install row → HxCheckout is dark (404)."""
    tok = await _mint(session)
    r = await client.post("/api/v1/checkout/orders",
                          headers={"X-Velaris-Token": tok}, json=_basket())
    assert r.status_code == 404, r.text


# ── Order intake ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_order_full_path(session, client, checkout_installed):
    tok = await _mint(session)
    r = await client.post("/api/v1/checkout/orders",
                          headers={"X-Velaris-Token": tok}, json=_basket(qty=2, unit=1999))
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["tracking_token"].startswith("TRK-")
    # No Stripe connector in tests → invoice/COD mode.
    assert data["status"] == "awaiting_fulfilment"

    order = (await session.execute(
        select(CheckoutOrderModel).where(CheckoutOrderModel.id == uuid.UUID(data["order_id"]))
    )).scalar_one()
    assert order.total_cents == 3998
    assert order.tenant_id == "default"
    assert order.case_id is not None

    items = (await session.execute(
        select(CheckoutOrderItemModel).where(CheckoutOrderItemModel.order_id == order.id)
    )).scalars().all()
    assert len(items) == 1 and items[0].quantity == 2 and items[0].unit_price_cents == 1999

    # The linked Order case exists and runs the seeded Order case type.
    case = await session.get(CaseInstanceModel, order.case_id)
    assert case is not None
    assert case.data.get("tracking_token") == order.tracking_token


@pytest.mark.asyncio
async def test_stripe_payment_path(session, client, checkout_installed, monkeypatch):
    """Primary path: Stripe connector present → payment_url issued, status
    pending_payment, payment_request_id persisted."""
    import types
    from case_service.checkout import service as svc

    pr_id = uuid.uuid4()

    async def _fake_create_payment_request(session, **kw):
        return types.SimpleNamespace(checkout_url="https://checkout.stripe.com/c/pay/cs_test_123", id=pr_id)

    monkeypatch.setattr(svc.payment_svc, "create_payment_request", _fake_create_payment_request)

    tok = await _mint(session)
    r = await client.post("/api/v1/checkout/orders",
                          headers={"X-Velaris-Token": tok}, json=_basket(qty=1, unit=4999))
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["status"] == "pending_payment"
    assert data["payment_url"] == "https://checkout.stripe.com/c/pay/cs_test_123"

    order = (await session.execute(
        select(CheckoutOrderModel).where(CheckoutOrderModel.id == uuid.UUID(data["order_id"]))
    )).scalar_one()
    assert order.status == "pending_payment"
    assert order.payment_request_id == pr_id


@pytest.mark.asyncio
async def test_empty_basket_422(session, client, checkout_installed):
    tok = await _mint(session)
    r = await client.post("/api/v1/checkout/orders",
                          headers={"X-Velaris-Token": tok},
                          json={"basket": [], "customer": {}, "shipping": {}})
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_idempotency_key_dedups(session, client, checkout_installed):
    tok = await _mint(session)
    h = {"X-Velaris-Token": tok, "Idempotency-Key": "order-abc-123"}
    r1 = await client.post("/api/v1/checkout/orders", headers=h, json=_basket(qty=1, unit=500))
    r2 = await client.post("/api/v1/checkout/orders", headers=h, json=_basket(qty=1, unit=500))
    assert r1.status_code == 201 and r2.status_code == 201, (r1.text, r2.text)
    assert r1.json()["order_id"] == r2.json()["order_id"]   # same order, no duplicate case
    # Exactly one order row carries the key.
    rows = (await session.execute(
        select(CheckoutOrderModel).where(CheckoutOrderModel.idempotency_key == "order-abc-123")
    )).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_idempotent_replay_returns_payment_url(session, client, checkout_installed, monkeypatch):
    """A replay of a pending-payment order must still return a usable payment_url
    (the retry is exactly when the client needs it)."""
    from case_service.checkout import service as svc
    from case_service.db.models import PaymentRequestModel

    async def _fake(session, *, case_id, step_id, amount_cents, currency, tenant_id, **kw):
        # Mirror the real create_payment_request: persist a PaymentRequestModel row
        # so the idempotent replay can re-fetch its checkout_url.
        pr = PaymentRequestModel(
            tenant_id=tenant_id, case_id=case_id, step_id=step_id, provider="stripe",
            amount_cents=amount_cents, currency=currency, status="pending",
            checkout_url="https://checkout.stripe.com/c/pay/cs_replay")
        session.add(pr)
        await session.flush()
        return pr
    monkeypatch.setattr(svc.payment_svc, "create_payment_request", _fake)

    tok = await _mint(session)
    h = {"X-Velaris-Token": tok, "Idempotency-Key": "pay-replay-1"}
    r1 = await client.post("/api/v1/checkout/orders", headers=h, json=_basket(qty=1, unit=999))
    r2 = await client.post("/api/v1/checkout/orders", headers=h, json=_basket(qty=1, unit=999))
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["order_id"] == r2.json()["order_id"]
    assert r2.json()["payment_url"] == "https://checkout.stripe.com/c/pay/cs_replay"   # not dropped on replay


@pytest.mark.asyncio
async def test_get_and_cancel_order(session, client, checkout_installed):
    tok = await _mint(session)
    created = (await client.post("/api/v1/checkout/orders",
                                 headers={"X-Velaris-Token": tok}, json=_basket())).json()
    oid = created["order_id"]
    h = {"X-Velaris-Token": tok}

    got = await client.get(f"/api/v1/checkout/orders/{oid}", headers=h)
    assert got.status_code == 200 and got.json()["tracking_token"] == created["tracking_token"]

    cancelled = await client.post(f"/api/v1/checkout/orders/{oid}/cancel", headers=h)
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"

    # Second cancel is a conflict (already cancelled).
    again = await client.post(f"/api/v1/checkout/orders/{oid}/cancel", headers=h)
    assert again.status_code == 409, again.text


# ── Admin token management (gated, require_admin) ─────────────────────────────

@pytest.mark.asyncio
async def test_admin_token_crud(client, checkout_installed):
    created = await client.post("/api/v1/checkout/tokens",
                                json={"label": "My Shopify Store", "mode": "live"})
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["token"].startswith("vsk_live_")
    assert body["token_prefix"] in body["token"]

    listed = await client.get("/api/v1/checkout/tokens")
    assert listed.status_code == 200
    assert any(t["label"] == "My Shopify Store" for t in listed.json()["tokens"])

    rev = await client.delete(f"/api/v1/checkout/tokens/{body['id']}")
    assert rev.status_code == 200
