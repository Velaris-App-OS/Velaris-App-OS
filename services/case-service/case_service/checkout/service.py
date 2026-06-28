"""HxCheckout order intake service.

Turns a validated basket into (a) a Velaris Order `case` running the resolved
Order case type, and (b) a `checkout_orders` row + line items, then initiates
payment via the existing HxConnect Stripe connector. The two are linked by
checkout_orders.case_id. Staff own the case from here; the external API is
write-once (orders are immutable from outside after creation, key invariant 2).

This is the user-less creation path (no JWT): the order's tenant comes from the
service token, mirroring HxFusion's tenant-derivation rule in cases.create_case.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import secrets

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.checkout import case_type as ct_svc
from case_service.checkout import notifications as notify_svc
from case_service.config import get_settings
from case_service.core import case_lifecycle
from case_service.db import repository as repo
from case_service.db.models import CheckoutOrderItemModel, CheckoutOrderModel, PaymentRequestModel
from case_service.payments import service as payment_svc

logger = logging.getLogger(__name__)

# Order statuses surfaced to the external caller.
STATUS_PENDING_PAYMENT     = "pending_payment"      # Stripe checkout session issued
STATUS_AWAITING_FULFILMENT = "awaiting_fulfilment"  # no payment needed (invoice / COD / £0)
STATUS_CANCELLED           = "cancelled"


def _new_tracking_token() -> str:
    """Cryptographically random 128-bit tracking token (key invariant 3 — never
    derived from the order id or any predictable value)."""
    return "TRK-" + secrets.token_hex(16).upper()


def _validate_basket(basket: list[dict]) -> int:
    """Validate line items and return the computed total in minor units."""
    if not basket:
        raise HTTPException(422, "Basket is empty")
    total = 0
    for i, item in enumerate(basket):
        sku = (item.get("sku") or "").strip()
        name = (item.get("name") or "").strip()
        if not sku or not name:
            raise HTTPException(422, f"Basket item {i} missing sku or name")
        try:
            qty = int(item.get("quantity", 0))
            unit = int(item.get("unit_price", 0))
        except (TypeError, ValueError):
            raise HTTPException(422, f"Basket item {i} has non-integer quantity/unit_price")
        if qty <= 0 or unit < 0:
            raise HTTPException(422, f"Basket item {i} has invalid quantity/unit_price")
        total += qty * unit
    return total


def _order_response(order: CheckoutOrderModel, payment_url: str | None) -> dict:
    return {
        "order_id":       str(order.id),
        "tracking_token": order.tracking_token,
        "payment_url":    payment_url,
        "status":         order.status,
        "portal_url":     f"/portal/track/{order.tracking_token}",
    }


async def create_order(
    session: AsyncSession,
    *,
    tenant: str,
    basket: list[dict],
    customer: dict,
    shipping: dict,
    metadata: dict | None = None,
    currency: str | None = None,
    idempotency_key: str | None = None,
    source: str = "api",
    is_test: bool = False,
    created_by: str = "hxcheckout",
    integration_id=None,
    discount_cents: int = 0,
) -> dict:
    """Create an order + its Order case. Idempotent on (tenant, idempotency_key).

    Shared by all intake paths — service-token (POST /orders), webhook, and the
    HxStorefront bridge — which supply `tenant`, `is_test`, `created_by` (and
    `discount_cents` for an applied promotion) from their own context."""
    metadata = metadata or {}
    currency = (currency or get_settings().checkout_default_currency).upper()

    # ── Idempotency: a retried request returns the original order, no duplicate case.
    # Covers the common sequential-retry case. Two *simultaneous* requests with the
    # same key can both pass this read and one then hits uq_checkout_orders_idem at
    # commit (500) — a known v1 edge; clients retry sequentially in practice.
    if idempotency_key:
        existing = (await session.execute(
            select(CheckoutOrderModel).where(
                CheckoutOrderModel.tenant_id == tenant,
                CheckoutOrderModel.idempotency_key == idempotency_key,
            ).limit(1)
        )).scalar_one_or_none()
        if existing is not None:
            # Replay (e.g. a client that timed out and retried) must still get a
            # usable payment_url for a pending-payment order — re-fetch it from the
            # linked Stripe payment request rather than dropping it.
            pay_url = None
            if existing.payment_request_id:
                pr = await session.get(PaymentRequestModel, existing.payment_request_id)
                pay_url = pr.checkout_url if pr else None
            return _order_response(existing, pay_url)

    total_cents = _validate_basket(basket)
    # Apply an externally-validated discount (e.g. an HxStorefront promotion),
    # floored at zero. The charged total + the payment request both use this.
    if discount_cents:
        total_cents = max(0, total_cents - int(discount_cents))

    # ── Open the Velaris Order case (user-less; tenant from the token).
    case_type = await ct_svc.resolve_order_case_type(session, tenant)
    case_tenant = ct_svc.tenant_uuid(tenant)   # UUID or None — never a bare string
    tracking_token = _new_tracking_token()
    case = await repo.create_case_instance(session, data={
        "case_type_id": case_type.id,
        "case_type_version": case_type.version,
        "status": "new",
        "priority": case_type.default_priority,
        "data": {
            "tracking_token": tracking_token,
            "source": source,
            "is_test": is_test,
            "currency": currency,
            "total_cents": total_cents,
            "customer": customer,
            "shipping": shipping,
            "items": basket,
            "metadata": metadata,
        },
        "created_by": created_by,
        "tenant_id": case_tenant,
    })

    # Start case-level SLAs (the delivery-estimate ring depends on these). Best-effort.
    try:
        await case_lifecycle.on_case_created(
            session, case_id=case.id, case_type_def=case_type.definition_json or {},
            case_type_id=case.case_type_id, tenant_id=case_tenant,
        )
    except Exception as e:
        logger.warning("SLA start failed for order case %s: %s", case.id, e)

    # ── Persist the order + line items.
    order = CheckoutOrderModel(
        tenant_id=tenant,
        case_id=case.id,
        tracking_token=tracking_token,
        status=STATUS_AWAITING_FULFILMENT,
        currency=currency,
        total_cents=total_cents,
        customer=customer,
        shipping=shipping,
        order_meta=metadata,
        source=source,
        idempotency_key=idempotency_key,
        integration_id=integration_id,
        is_test=is_test,
    )
    session.add(order)
    await session.flush()
    for item in basket:
        session.add(CheckoutOrderItemModel(
            order_id=order.id,
            sku=str(item["sku"]).strip(),
            name=str(item["name"]).strip(),
            quantity=int(item["quantity"]),
            unit_price_cents=int(item["unit_price"]),
            item_meta=item.get("metadata") or {},
        ))

    # ── Payment: inline Stripe when configured + payable, else invoice/COD mode.
    payment_url = None
    provider = get_settings().checkout_payment_provider
    if provider == "stripe" and total_cents > 0:
        try:
            pr = await payment_svc.create_payment_request(
                session,
                case_id=case.id,
                step_id="await_payment",
                amount_cents=total_cents,
                currency=currency,
                description=f"Order {tracking_token}",
                tenant_id=tenant,
                customer_email=(customer or {}).get("email"),
            )
            payment_url = pr.checkout_url
            order.payment_request_id = pr.id
            order.status = STATUS_PENDING_PAYMENT
        except ValueError as e:
            # No Stripe connector configured for this tenant → degrade to invoice/COD
            # (the doc's "Pay on collection / Invoice" fallback). Order still proceeds.
            logger.info("Order %s: no Stripe payment (%s) — invoice/COD mode", tracking_token, e)
            order.order_meta = {**metadata, "payment_mode": "invoice"}

    await session.flush()
    await notify_svc.notify(session, order, "order_received")
    return _order_response(order, payment_url)


async def create_subcase(
    session: AsyncSession, *, order: CheckoutOrderModel, kind: str,
    reason: str = "", items: list | None = None,
) -> dict:
    """Open a Return or Complaint sub-case linked to the order's Order case
    (parent_case_id). Customer-initiated (authenticated by tracking token)."""
    if kind == "return":
        case_type = await ct_svc.resolve_return_case_type(session)
        event = "return_requested"
    elif kind == "complaint":
        case_type = await ct_svc.resolve_complaint_case_type(session)
        event = "complaint_raised"
    else:
        raise HTTPException(422, "kind must be 'return' or 'complaint'")

    case_tenant = ct_svc.tenant_uuid(order.tenant_id)
    sub = await repo.create_case_instance(session, data={
        "case_type_id": case_type.id,
        "case_type_version": case_type.version,
        "status": "new",
        "priority": case_type.default_priority,
        "parent_case_id": order.case_id,
        "data": {
            "kind": kind, "reason": reason, "items": items or [],
            "order_id": str(order.id), "tracking_token": order.tracking_token,
        },
        "created_by": "hxcheckout:customer",
        "tenant_id": case_tenant,
    })
    try:
        await case_lifecycle.on_case_created(
            session, case_id=sub.id, case_type_def=case_type.definition_json or {},
            case_type_id=sub.case_type_id, tenant_id=case_tenant)
    except Exception as e:
        logger.warning("SLA start failed for %s sub-case %s: %s", kind, sub.id, e)
    await notify_svc.notify(session, order, event)
    return {"sub_case_id": str(sub.id), "kind": kind, "parent_case_id": str(order.case_id) if order.case_id else None}
