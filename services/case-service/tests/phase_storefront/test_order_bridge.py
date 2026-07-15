"""HxStorefront — storefront → HxCheckout order bridge (HS-7).

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select

from case_service.db.models import (
    CheckoutOrderModel, MarketplaceInstallModel, StorefrontProductVariantModel,
)


@pytest_asyncio.fixture
async def both_installed(session):
    for pkg in ("velaris/hxstorefront", "velaris/hxcheckout"):
        session.add(MarketplaceInstallModel(
            tenant_id="default", package_id=pkg,
            package_version="1.0.0", package_type="module", approved_by="test-admin"))
    await session.commit()
    yield


async def _seed(client):
    await client.post("/api/v1/storefront/stores", json={"name": "Acme", "slug": "acme"})
    p = await client.post("/api/v1/storefront/stores/acme/products",
                          json={"name": "Tee", "price_cents": 2000, "status": "active"})
    pid = p.json()["id"]
    v = await client.post(f"/api/v1/storefront/stores/acme/products/{pid}/variants",
                          json={"sku": "TEE-M", "stock_quantity": 3})
    await client.post("/api/v1/storefront/stores/acme/promotions",
                      json={"code": "TAKE10", "discount_type": "percentage", "config": {"percent": 10}})
    return pid, v.json()["id"]


@pytest.mark.asyncio
async def test_storefront_checkout_creates_order_decrements_stock_applies_promo(
    session, client, both_installed):
    _, vid = await _seed(client)
    r = await client.post("/api/v1/storefront/public/acme/checkout",
                          json={"items": [{"product_slug": "tee", "variant_sku": "TEE-M", "quantity": 2}],
                                "customer": {"email": "b@c.com"}, "discount_code": "TAKE10"})
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["tracking_token"].startswith("TRK-")
    assert out["discount_cents"] == 400          # 10% of (2×2000)

    # Order persisted with the discounted total + store_slug, source=storefront.
    order = (await session.execute(
        select(CheckoutOrderModel).where(CheckoutOrderModel.id == uuid.UUID(out["order_id"]))
    )).scalar_one()
    assert order.source == "storefront"
    assert order.total_cents == 3600             # 4000 − 400
    assert order.order_meta.get("store_slug") == "acme"

    # Stock decremented 3 → 1.
    v = await session.get(StorefrontProductVariantModel, uuid.UUID(vid))
    assert v.stock_quantity == 1


@pytest.mark.asyncio
async def test_checkout_idempotent_no_double_decrement(session, client, both_installed):
    """A double-submit with the same Idempotency-Key → one order, stock decremented ONCE."""
    _, vid = await _seed(client)
    body = {"items": [{"product_slug": "tee", "variant_sku": "TEE-M", "quantity": 1}],
            "customer": {"email": "b@c.com"}}
    h = {"Idempotency-Key": "sf-order-1"}
    r1 = await client.post("/api/v1/storefront/public/acme/checkout", json=body, headers=h)
    r2 = await client.post("/api/v1/storefront/public/acme/checkout", json=body, headers=h)
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["order_id"] == r2.json()["order_id"]
    assert r2.json().get("idempotent_replay") is True
    v = await session.get(StorefrontProductVariantModel, uuid.UUID(vid))
    assert v.stock_quantity == 2   # 3 − 1 (only once), not 1


@pytest.mark.asyncio
async def test_out_of_stock_rejected(session, client, both_installed):
    _, vid = await _seed(client)
    r = await client.post("/api/v1/storefront/public/acme/checkout",
                          json={"items": [{"product_slug": "tee", "variant_sku": "TEE-M", "quantity": 99}],
                                "customer": {"email": "b@c.com"}})
    assert r.status_code == 409, r.text
    # Stock untouched.
    v = await session.get(StorefrontProductVariantModel, uuid.UUID(vid))
    assert v.stock_quantity == 3


@pytest.mark.asyncio
async def test_checkout_requires_hxcheckout_installed(session, client):
    """Storefront installed but HxCheckout NOT → checkout 409 (hard dependency)."""
    session.add(MarketplaceInstallModel(
        tenant_id="default", package_id="velaris/hxstorefront",
        package_version="1.0.0", package_type="module", approved_by="test-admin"))
    await session.commit()
    await _seed(client)
    r = await client.post("/api/v1/storefront/public/acme/checkout",
                          json={"items": [{"product_slug": "tee", "variant_sku": "TEE-M", "quantity": 1}],
                                "customer": {"email": "b@c.com"}})
    assert r.status_code == 409, r.text


@pytest.mark.asyncio
async def test_client_cannot_set_price(session, client, both_installed):
    """Prices come from the catalogue; any client-sent price is ignored."""
    await _seed(client)
    r = await client.post("/api/v1/storefront/public/acme/checkout",
                          json={"items": [{"product_slug": "tee", "variant_sku": "TEE-M",
                                           "quantity": 1, "unit_price": 1}],
                                "customer": {"email": "b@c.com"}})
    assert r.status_code == 201
    order = (await session.execute(
        select(CheckoutOrderModel).where(CheckoutOrderModel.id == uuid.UUID(r.json()["order_id"]))
    )).scalar_one()
    assert order.total_cents == 2000   # catalogue price, not the injected 1
