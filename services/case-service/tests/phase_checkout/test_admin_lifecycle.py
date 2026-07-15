"""HxCheckout — admin order views + marketplace data-teardown (Phase 7).

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from case_service.checkout.tokens import generate_token
from case_service.marketplace import official_registry
from case_service.marketplace.app_lifecycle import teardown_package_data
from case_service.db.models import (
    CaseInstanceModel, CheckoutOrderModel, CheckoutServiceTokenModel,
    MarketplaceInstallModel,
)

PKG = "velaris/hxcheckout"


@pytest_asyncio.fixture
async def checkout_installed(session):
    session.add(MarketplaceInstallModel(
        tenant_id="default", package_id=PKG,
        package_version="1.0.0", package_type="module", approved_by="test-admin"))
    await session.commit()
    yield


async def _place(session, client):
    plaintext, _ = generate_token(session, tenant_id="default", label="t", mode="live")
    await session.commit()
    r = await client.post("/api/v1/checkout/orders", headers={"X-Velaris-Token": plaintext},
                          json={"basket": [{"sku": "A", "name": "A", "quantity": 1, "unit_price": 2500}],
                                "customer": {"email": "c@d.com"}, "shipping": {}})
    assert r.status_code == 201, r.text
    return r.json()


def test_registry_lists_hxcheckout():
    """Official-tier trust anchor includes the package id (4-place id contract)."""
    pkgs = official_registry.load_official_packages() if hasattr(official_registry, "load_official_packages") else None
    # Fall back to reading the raw set if the loader name differs.
    import json, pathlib
    raw = json.loads((pathlib.Path(official_registry.__file__).parent / "official_registry.json").read_text())
    assert PKG in raw["official_packages"]


@pytest.mark.asyncio
async def test_admin_list_orders_and_analytics(session, client, checkout_installed):
    await _place(session, client)
    await _place(session, client)

    listed = await client.get("/api/v1/checkout/orders")
    assert listed.status_code == 200, listed.text
    assert len(listed.json()["orders"]) == 2

    summary = await client.get("/api/v1/checkout/analytics/summary")
    assert summary.status_code == 200, summary.text
    body = summary.json()
    assert body["total_orders"] == 2
    assert body["total_revenue_cents"] == 5000


@pytest.mark.asyncio
async def test_teardown_deletes_checkout_data_keeps_cases(session, client, checkout_installed):
    created = await _place(session, client)
    order = await session.get(CheckoutOrderModel, uuid.UUID(created["order_id"]))
    case_id = order.case_id
    assert case_id is not None

    result = await teardown_package_data(session, PKG, "default")
    await session.commit()
    assert result["deleted"] is True and result["orders"] >= 1

    # checkout_* rows gone …
    assert (await session.execute(
        select(func.count()).select_from(CheckoutOrderModel))).scalar_one() == 0
    assert (await session.execute(
        select(func.count()).select_from(CheckoutServiceTokenModel))).scalar_one() == 0
    # … but the Order CASE remains (core data; FK was SET NULL not CASCADE).
    case = await session.get(CaseInstanceModel, case_id)
    assert case is not None
