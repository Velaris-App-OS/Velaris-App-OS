"""HxStorefront — install gate + store CRUD (HS-1).

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


@pytest.mark.asyncio
async def test_stores_404_when_not_installed(client):
    r = await client.get("/api/v1/storefront/stores")
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_store_crud_and_clone(client, storefront_installed):
    # Create
    c = await client.post("/api/v1/storefront/stores",
                          json={"name": "Acme Shop", "currency": "GBP"})
    assert c.status_code == 201, c.text
    assert c.json()["slug"] == "acme-shop"

    # List
    listed = await client.get("/api/v1/storefront/stores")
    assert listed.status_code == 200 and len(listed.json()["stores"]) == 1

    # Update
    u = await client.put("/api/v1/storefront/stores/acme-shop", json={"name": "Acme Store"})
    assert u.status_code == 200 and u.json()["name"] == "Acme Store"

    # Clone (config only)
    cl = await client.post("/api/v1/storefront/stores/acme-shop/clone", json={"name": "Acme EU"})
    assert cl.status_code == 201 and cl.json()["slug"] == "acme-eu"

    # Archive
    a = await client.delete("/api/v1/storefront/stores/acme-shop")
    assert a.status_code == 200


@pytest.mark.asyncio
async def test_duplicate_slug_rejected(client, storefront_installed):
    await client.post("/api/v1/storefront/stores", json={"name": "Dup", "slug": "dup"})
    r = await client.post("/api/v1/storefront/stores", json={"name": "Dup 2", "slug": "dup"})
    assert r.status_code == 409, r.text


# ── HS-2: catalogue ───────────────────────────────────────────────────────────

async def _store(client, slug="shop"):
    await client.post("/api/v1/storefront/stores", json={"name": "Shop", "slug": slug})
    return slug


@pytest.mark.asyncio
async def test_product_crud_with_variants_and_options(client, storefront_installed):
    slug = await _store(client)
    p = await client.post(f"/api/v1/storefront/stores/{slug}/products",
                          json={"name": "Classic Tee", "price_cents": 1999, "status": "active"})
    assert p.status_code == 201, p.text
    pid = p.json()["id"]
    assert p.json()["slug"] == "classic-tee"

    opt = await client.post(f"/api/v1/storefront/stores/{slug}/products/{pid}/options",
                            json={"name": "Size", "values": ["S", "M", "L"]})
    assert opt.status_code == 201

    v = await client.post(f"/api/v1/storefront/stores/{slug}/products/{pid}/variants",
                          json={"sku": "TEE-M", "option_values": {"Size": "M"}, "stock_quantity": 5})
    assert v.status_code == 201, v.text
    vid = v.json()["id"]

    # Duplicate variant SKU rejected.
    dup = await client.post(f"/api/v1/storefront/stores/{slug}/products/{pid}/variants",
                            json={"sku": "TEE-M"})
    assert dup.status_code == 409

    detail = await client.get(f"/api/v1/storefront/stores/{slug}/products/{pid}")
    assert detail.status_code == 200
    body = detail.json()
    assert len(body["options"]) == 1 and len(body["variants"]) == 1

    d = await client.delete(f"/api/v1/storefront/stores/{slug}/products/{pid}/variants/{vid}")
    assert d.status_code == 200


@pytest.mark.asyncio
async def test_categories_and_assignment(client, storefront_installed):
    slug = await _store(client)
    c = await client.post(f"/api/v1/storefront/stores/{slug}/categories", json={"name": "Men"})
    assert c.status_code == 201
    cid = c.json()["id"]
    sub = await client.post(f"/api/v1/storefront/stores/{slug}/categories",
                            json={"name": "Tees", "parent_id": cid})
    assert sub.status_code == 201 and sub.json()["parent_id"] == cid

    p = await client.post(f"/api/v1/storefront/stores/{slug}/products",
                          json={"name": "Tee", "price_cents": 999})
    pid = p.json()["id"]
    assign = await client.put(f"/api/v1/storefront/stores/{slug}/products/{pid}/categories",
                              json={"category_ids": [cid]})
    assert assign.status_code == 200 and assign.json()["category_ids"] == [cid]

    # Non-empty category cannot be deleted.
    nodel = await client.delete(f"/api/v1/storefront/stores/{slug}/categories/{cid}")
    assert nodel.status_code == 409


@pytest.mark.asyncio
async def test_product_isolated_to_store(client, storefront_installed):
    await _store(client, "store-a")
    await _store(client, "store-b")
    p = await client.post("/api/v1/storefront/stores/store-a/products",
                          json={"name": "A-only", "price_cents": 100})
    pid = p.json()["id"]
    # Same product id is not reachable under store-b.
    r = await client.get(f"/api/v1/storefront/stores/store-b/products/{pid}")
    assert r.status_code == 404
