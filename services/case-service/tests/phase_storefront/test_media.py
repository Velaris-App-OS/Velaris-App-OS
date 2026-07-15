"""HxStorefront — media library (HS-5).

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
async def test_media_upload_list_delete(client, storefront_installed):
    await client.post("/api/v1/storefront/stores", json={"name": "Shop", "slug": "shop"})

    # 1x1 PNG bytes.
    png = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082")
    up = await client.post("/api/v1/storefront/stores/shop/media",
                           files={"file": ("logo.png", png, "image/png")},
                           data={"alt_text": "Logo", "folder": "brand"})
    assert up.status_code == 201, up.text
    mid = up.json()["id"]
    assert up.json()["media_type"] == "image" and up.json()["alt_text"] == "Logo"

    listed = await client.get("/api/v1/storefront/stores/shop/media")
    assert listed.status_code == 200 and len(listed.json()["media"]) == 1

    by_folder = await client.get("/api/v1/storefront/stores/shop/media?folder=brand")
    assert len(by_folder.json()["media"]) == 1

    d = await client.delete(f"/api/v1/storefront/stores/shop/media/{mid}")
    assert d.status_code == 200


@pytest.mark.asyncio
async def test_media_rejects_bad_extension(client, storefront_installed):
    await client.post("/api/v1/storefront/stores", json={"name": "Shop", "slug": "shop"})
    r = await client.post("/api/v1/storefront/stores/shop/media",
                          files={"file": ("evil.exe", b"MZ", "application/octet-stream")})
    assert r.status_code == 400, r.text


@pytest.mark.asyncio
async def test_media_rejects_svg_xss_vector(client, storefront_installed):
    """SVG is excluded from the allowlist — it can carry inline script (stored XSS)."""
    await client.post("/api/v1/storefront/stores", json={"name": "Shop", "slug": "shop"})
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    r = await client.post("/api/v1/storefront/stores/shop/media",
                          files={"file": ("logo.svg", svg, "image/svg+xml")})
    assert r.status_code == 400, r.text
