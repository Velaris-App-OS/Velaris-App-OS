"""HxStorefront — public storefront API (HS-6).

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


async def _seed_store(client):
    await client.post("/api/v1/storefront/stores", json={"name": "Acme", "slug": "acme"})
    # active + draft product — only active should be public
    await client.post("/api/v1/storefront/stores/acme/products",
                      json={"name": "Visible Tee", "price_cents": 1999, "status": "active",
                            "short_description": "A nice tee"})
    await client.post("/api/v1/storefront/stores/acme/products",
                      json={"name": "Hidden Draft", "price_cents": 999, "status": "draft"})


@pytest.mark.asyncio
async def test_public_config_and_listing(client, storefront_installed):
    await _seed_store(client)
    cfg = await client.get("/api/v1/storefront/public/acme/config")
    assert cfg.status_code == 200 and cfg.json()["store"]["name"] == "Acme"

    prods = await client.get("/api/v1/storefront/public/acme/products")
    assert prods.status_code == 200
    names = [p["name"] for p in prods.json()["products"]]
    assert "Visible Tee" in names and "Hidden Draft" not in names  # draft hidden

    detail = await client.get("/api/v1/storefront/public/acme/products/visible-tee")
    assert detail.status_code == 200 and detail.json()["price_cents"] == 1999


@pytest.mark.asyncio
async def test_public_search_and_sitemap(client, storefront_installed):
    await _seed_store(client)
    s = await client.get("/api/v1/storefront/public/acme/search?q=visible")
    assert s.status_code == 200 and len(s.json()["products"]) == 1

    sm = await client.get("/api/v1/storefront/public/acme/sitemap.xml")
    assert sm.status_code == 200 and "visible-tee" in sm.text
    assert sm.headers["content-type"].startswith("application/xml")


@pytest.mark.asyncio
async def test_public_subscribe(client, storefront_installed):
    await _seed_store(client)
    r = await client.post("/api/v1/storefront/public/acme/subscribers",
                          json={"email": "Fan@Example.com"})
    assert r.status_code == 201 and r.json()["subscribed"] is True
    # idempotent (dedupe)
    again = await client.post("/api/v1/storefront/public/acme/subscribers",
                              json={"email": "fan@example.com"})
    assert again.status_code == 201


@pytest.mark.asyncio
async def test_public_404_when_not_installed(client):
    """No install row → public storefront is dark even with a valid-looking slug."""
    r = await client.get("/api/v1/storefront/public/whatever/config")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_public_dark_after_uninstall(session, client, storefront_installed):
    """Store exists, but if the tenant's install is revoked the public store 404s."""
    await _seed_store(client)
    ok = await client.get("/api/v1/storefront/public/acme/config")
    assert ok.status_code == 200
    # Revoke the install.
    from sqlalchemy import update
    from case_service.db.models import MarketplaceInstallModel as MI
    from datetime import datetime, timezone
    await session.execute(update(MI).where(MI.package_id == PKG)
                          .values(revoked_at=datetime.now(timezone.utc)))
    await session.commit()
    dark = await client.get("/api/v1/storefront/public/acme/config")
    assert dark.status_code == 404