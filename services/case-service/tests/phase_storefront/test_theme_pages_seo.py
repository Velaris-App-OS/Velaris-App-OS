"""HxStorefront — theme (versioned) + pages (sanitised) + navigation + SEO (HS-4).

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


async def _store(client):
    await client.post("/api/v1/storefront/stores", json={"name": "Shop", "slug": "shop"})


@pytest.mark.asyncio
async def test_theme_versioning(client, storefront_installed):
    await _store(client)
    v1 = await client.put("/api/v1/storefront/stores/shop/theme",
                          json={"config": {"colors": {"brand_primary": "#111"}}})
    assert v1.status_code == 200 and v1.json()["version"] == 1
    v2 = await client.put("/api/v1/storefront/stores/shop/theme",
                          json={"config": {"colors": {"brand_primary": "#222"}}})
    assert v2.json()["version"] == 2

    cur = await client.get("/api/v1/storefront/stores/shop/theme")
    assert cur.json()["version"] == 2 and cur.json()["config"]["colors"]["brand_primary"] == "#222"

    prev = await client.post("/api/v1/storefront/stores/shop/theme/preview",
                             json={"config": {"colors": {"brand_primary": "#333"}}})
    assert "--color-brand-primary: #333" in prev.json()["css"]


@pytest.mark.asyncio
async def test_page_custom_html_sanitised(client, storefront_installed):
    """Allowlist sanitiser: safe tags survive; every executable/dangerous construct
    is dropped (deny-by-default)."""
    await _store(client)
    payload = ('<p>Hi</p><script>alert(1)</script>'
               '<iframe src="evil"></iframe>'
               '<img src="x" onerror="alert(1)">'
               '<a href="javascript:alert(1)">x</a>'
               '<a href="https://ok.com" target="_blank">good</a>'
               '<svg onload="alert(1)"></svg><object data="evil"></object>'
               '<form action="evil"><input></form>')
    r = await client.put("/api/v1/storefront/stores/shop/pages/about",
                         json={"title": "About", "is_published": True,
                               "sections": [{"type": "custom_html", "html": payload}]})
    assert r.status_code == 200, r.text
    html = r.json()["sections"][0]["html"]
    # Safe content kept.
    assert "<p>Hi</p>" in html
    assert '<a href="https://ok.com"' in html and 'rel="noopener noreferrer"' in html
    # No executable tags or handlers survive (no <script/iframe/svg/object/form, no on*, no javascript:).
    for bad in ("<script", "<iframe", "<svg", "<object", "<form", "onerror", "onload", "javascript:"):
        assert bad not in html.lower(), f"{bad} leaked through sanitiser"


@pytest.mark.asyncio
async def test_navigation_and_seo(client, storefront_installed):
    await _store(client)
    nav = await client.put("/api/v1/storefront/stores/shop/navigation",
                           json={"header": [{"label": "Home", "url": "/store/shop"}]})
    assert nav.status_code == 200
    got = await client.get("/api/v1/storefront/stores/shop/navigation")
    assert got.json()["navigation"]["header"][0]["label"] == "Home"

    seo = await client.put("/api/v1/storefront/stores/shop/seo",
                           json={"target_type": "store", "target_id": "", "meta_title": "Acme"})
    assert seo.status_code == 200
    gs = await client.get("/api/v1/storefront/stores/shop/seo")
    assert gs.json()["meta_title"] == "Acme"
