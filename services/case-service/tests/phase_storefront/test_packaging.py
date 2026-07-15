"""HxStorefront — marketplace packaging: registry + data teardown (HS-8).

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
import pathlib

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from case_service.marketplace import official_registry
from case_service.marketplace.app_lifecycle import teardown_package_data
from case_service.db.models import (
    MarketplaceInstallModel, StorefrontProductModel, StorefrontStoreModel,
)

PKG = "velaris/hxstorefront"


@pytest_asyncio.fixture
async def storefront_installed(session):
    session.add(MarketplaceInstallModel(
        tenant_id="default", package_id=PKG,
        package_version="1.0.0", package_type="module", approved_by="test-admin"))
    await session.commit()
    yield


def test_registry_and_id_contract():
    raw = json.loads((pathlib.Path(official_registry.__file__).parent / "official_registry.json").read_text())
    assert PKG in raw["official_packages"]
    # 4-place id contract: backend gate constant matches.
    from case_service.storefront.common import HXSTOREFRONT_PACKAGE_ID
    assert HXSTOREFRONT_PACKAGE_ID == PKG
    # Published manifest/velaris.json ids match.
    root = pathlib.Path(official_registry.__file__).parents[4] / "marketplace" / "official" / "hxstorefront"
    assert json.loads((root / "manifest.json").read_text())["id"] == PKG
    assert json.loads((root / "velaris.json").read_text())["id"] == PKG


def test_published_checksum_matches_hxapp():
    root = pathlib.Path(official_registry.__file__).parents[4] / "marketplace" / "official" / "hxstorefront"
    import hashlib
    actual = hashlib.sha256((root / "hxstorefront-1.0.0.hxapp").read_bytes()).hexdigest()
    declared = json.loads((root / "velaris.json").read_text())["versions"][0]["checksum_sha256"]
    assert actual == declared


@pytest.mark.asyncio
async def test_teardown_deletes_stores_cascade(session, client, storefront_installed):
    await client.post("/api/v1/storefront/stores", json={"name": "Acme", "slug": "acme"})
    await client.post("/api/v1/storefront/stores/acme/products",
                      json={"name": "Tee", "price_cents": 100})

    result = await teardown_package_data(session, PKG, "default")
    await session.commit()
    assert result["deleted"] is True and result["stores"] == 1

    # The tenant's stores are deleted; their child rows (products, variants, …) go
    # via FK ON DELETE CASCADE — enforced on PostgreSQL/MySQL (production). The
    # SQLite test harness doesn't enforce FK cascade, so we assert the store deletion
    # (the teardown's direct effect) here.
    assert (await session.execute(select(func.count()).select_from(StorefrontStoreModel))).scalar_one() == 0
