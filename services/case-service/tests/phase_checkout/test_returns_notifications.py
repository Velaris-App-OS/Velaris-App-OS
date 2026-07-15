"""HxCheckout — returns/complaints sub-cases + notification logging (Phase 6).

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
    CaseInstanceModel, CheckoutNotificationLogModel, CheckoutOrderModel,
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


async def _mint(session) -> str:
    plaintext, _ = generate_token(session, tenant_id="default", label="t", mode="live")
    await session.commit()
    return plaintext


async def _place_order(session, client) -> dict:
    tok = await _mint(session)
    r = await client.post("/api/v1/checkout/orders", headers={"X-Velaris-Token": tok},
                          json={"basket": [{"sku": "A", "name": "A", "quantity": 1, "unit_price": 1000}],
                                "customer": {"email": "c@d.com"}, "shipping": {}})
    assert r.status_code == 201, r.text
    return r.json()


@pytest.mark.asyncio
async def test_order_received_notifications_logged(session, client, checkout_installed):
    created = await _place_order(session, client)
    order = (await session.execute(
        select(CheckoutOrderModel).where(CheckoutOrderModel.id == uuid.UUID(created["order_id"]))
    )).scalar_one()
    notes = (await session.execute(
        select(CheckoutNotificationLogModel).where(CheckoutNotificationLogModel.order_id == order.id)
    )).scalars().all()
    # order_received → email + sms + push
    assert {n.channel for n in notes} == {"email", "sms", "push"}
    assert all(n.event == "order_received" for n in notes)


@pytest.mark.asyncio
async def test_return_subcase_linked_to_order(session, client, checkout_installed):
    created = await _place_order(session, client)
    r = await client.post(f"/api/v1/checkout/orders/{created['order_id']}/returns",
                          json={"tracking_token": created["tracking_token"], "reason": "Too small"})
    assert r.status_code == 201, r.text
    sub_id = r.json()["sub_case_id"]

    order = await session.get(CheckoutOrderModel, uuid.UUID(created["order_id"]))
    sub = await session.get(CaseInstanceModel, uuid.UUID(sub_id))
    assert sub is not None
    assert sub.parent_case_id == order.case_id      # linked to the parent Order case
    assert sub.data.get("kind") == "return"


@pytest.mark.asyncio
async def test_complaint_subcase(session, client, checkout_installed):
    created = await _place_order(session, client)
    r = await client.post(f"/api/v1/checkout/orders/{created['order_id']}/complaints",
                          json={"tracking_token": created["tracking_token"], "reason": "Damaged"})
    assert r.status_code == 201, r.text
    sub = await session.get(CaseInstanceModel, uuid.UUID(r.json()["sub_case_id"]))
    assert sub.data.get("kind") == "complaint"


@pytest.mark.asyncio
async def test_return_wrong_tracking_token_404(session, client, checkout_installed):
    created = await _place_order(session, client)
    r = await client.post(f"/api/v1/checkout/orders/{created['order_id']}/returns",
                          json={"tracking_token": "TRK-WRONG", "reason": "x"})
    assert r.status_code == 404, r.text
