"""HxStorefront — inventory + promotions (HS-3).

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import pytest
import pytest_asyncio

from case_service.db.models import MarketplaceInstallModel

PKG = "velaris/hxstorefront"


@pytest_asyncio.fixture
async def storefront_installed(session):
    session.add(MarketplaceInstallModel(
        tenant_id="default", package_id=PKG,
        package_version="1.0.0", package_type="module", approved_by="test-admin"))
    await session.commit()
    yield


async def _store_with_variant(client):
    await client.post("/api/v1/storefront/stores", json={"name": "Shop", "slug": "shop"})
    p = await client.post("/api/v1/storefront/stores/shop/products",
                          json={"name": "Tee", "price_cents": 1000, "low_stock_threshold": 3})
    pid = p.json()["id"]
    v = await client.post(f"/api/v1/storefront/stores/shop/products/{pid}/variants",
                          json={"sku": "TEE-1", "stock_quantity": 10})
    return pid, v.json()["id"]


@pytest.mark.asyncio
async def test_inventory_adjust_and_history(client, storefront_installed):
    _, vid = await _store_with_variant(client)
    r = await client.patch(f"/api/v1/storefront/stores/shop/inventory/{vid}",
                           json={"change": -4, "reason": "manual"})
    assert r.status_code == 200 and r.json()["stock_quantity"] == 6

    inv = await client.get("/api/v1/storefront/stores/shop/inventory")
    assert inv.status_code == 200
    row = next(i for i in inv.json()["inventory"] if i["variant_id"] == vid)
    assert row["stock_quantity"] == 6 and row["low_stock"] is False

    # Drop below threshold (3) → low_stock flips.
    await client.patch(f"/api/v1/storefront/stores/shop/inventory/{vid}", json={"change": -4})
    inv2 = await client.get("/api/v1/storefront/stores/shop/inventory")
    row2 = next(i for i in inv2.json()["inventory"] if i["variant_id"] == vid)
    assert row2["stock_quantity"] == 2 and row2["low_stock"] is True

    hist = await client.get(f"/api/v1/storefront/stores/shop/inventory/{vid}/history")
    assert hist.status_code == 200 and len(hist.json()["history"]) == 2


@pytest.mark.asyncio
async def test_promotion_create_and_validate(client, storefront_installed):
    await client.post("/api/v1/storefront/stores", json={"name": "Shop", "slug": "shop"})
    p = await client.post("/api/v1/storefront/stores/shop/promotions",
                          json={"code": "SUMMER10", "discount_type": "percentage",
                                "config": {"percent": 10}, "min_order_cents": 5000})
    assert p.status_code == 201, p.text

    # Below minimum → invalid.
    low = await client.post("/api/v1/storefront/stores/shop/promotions/validate",
                            json={"code": "SUMMER10", "subtotal_cents": 4000})
    assert low.json()["valid"] is False

    # Above minimum → 10% of 10000 = 1000.
    ok = await client.post("/api/v1/storefront/stores/shop/promotions/validate",
                           json={"code": "SUMMER10", "subtotal_cents": 10000})
    assert ok.json()["valid"] is True and ok.json()["discount_cents"] == 1000

    # Unknown code → invalid.
    bad = await client.post("/api/v1/storefront/stores/shop/promotions/validate",
                            json={"code": "NOPE", "subtotal_cents": 10000})
    assert bad.json()["valid"] is False


@pytest.mark.asyncio
async def test_deactivated_promotion_rejected(client, storefront_installed):
    await client.post("/api/v1/storefront/stores", json={"name": "Shop", "slug": "shop"})
    p = await client.post("/api/v1/storefront/stores/shop/promotions",
                          json={"code": "OFF", "discount_type": "fixed", "config": {"amount_cents": 500}})
    pid = p.json()["id"]
    await client.delete(f"/api/v1/storefront/stores/shop/promotions/{pid}")
    v = await client.post("/api/v1/storefront/stores/shop/promotions/validate",
                          json={"code": "OFF", "subtotal_cents": 10000})
    assert v.json()["valid"] is False
