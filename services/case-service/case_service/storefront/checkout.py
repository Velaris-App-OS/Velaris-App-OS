"""HxStorefront → HxCheckout order bridge.

A storefront basket becomes a Velaris Order via HxCheckout (in-process). Two key
invariants are enforced here:
  * #2 stock is decremented ATOMICALLY at order creation — a guarded UPDATE
    (`SET stock = stock - qty WHERE stock >= qty`) means two simultaneous orders for
    the last item cannot both succeed.
  * #3 the discount code is re-validated at order creation, not just at the basket.

Prices are taken from the catalogue, never trusted from the client.
"""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.checkout import service as checkout_svc
from case_service.db.models import (
    CheckoutOrderModel, StorefrontAnalyticsEventModel, StorefrontInventoryLogModel,
    StorefrontProductModel, StorefrontProductVariantModel, StorefrontPromotionUseModel,
    StorefrontStoreModel,
)
from case_service.storefront import service as sf_service


async def _reserve_variant(session: AsyncSession, variant_id, qty: int) -> bool:
    """Atomically decrement a variant's stock if enough is available. Returns True on
    success. NULL stock = unlimited (always succeeds, no decrement)."""
    v = await session.get(StorefrontProductVariantModel, variant_id)
    if v is None:
        return False
    if v.stock_quantity is None:
        return True
    res = await session.execute(
        update(StorefrontProductVariantModel)
        .where(StorefrontProductVariantModel.id == variant_id,
               StorefrontProductVariantModel.stock_quantity >= qty)
        .values(stock_quantity=StorefrontProductVariantModel.stock_quantity - qty))
    if (res.rowcount or 0) != 1:
        return False
    session.add(StorefrontInventoryLogModel(
        variant_id=variant_id, change=-qty, reason="sale", actor="hxstorefront"))
    return True


async def _reserve_product(session: AsyncSession, product: StorefrontProductModel, qty: int) -> bool:
    """Atomically decrement a product-level stock count (products with no variants)."""
    if product.stock_quantity is None:
        return True
    res = await session.execute(
        update(StorefrontProductModel)
        .where(StorefrontProductModel.id == product.id,
               StorefrontProductModel.stock_quantity >= qty)
        .values(stock_quantity=StorefrontProductModel.stock_quantity - qty))
    return (res.rowcount or 0) == 1


async def place_storefront_order(
    session: AsyncSession, *, store: StorefrontStoreModel, items: list[dict],
    customer: dict, shipping: dict, discount_code: str = "",
    idempotency_key: str | None = None,
) -> dict:
    """Build a server-priced basket, reserve stock atomically, re-validate the promo,
    open the order via HxCheckout, record the promo use, and emit analytics."""
    if not items:
        raise HTTPException(422, "Basket is empty")

    # Idempotency must be checked BEFORE reserving stock — otherwise a double-submit
    # (e.g. a double-clicked "Pay") would decrement stock twice before the duplicate
    # order is caught. A replay returns the original order with no new reservation.
    if idempotency_key:
        existing = (await session.execute(
            select(CheckoutOrderModel).where(
                CheckoutOrderModel.tenant_id == store.tenant_id,
                CheckoutOrderModel.idempotency_key == idempotency_key).limit(1))).scalar_one_or_none()
        if existing is not None:
            return {"order_id": str(existing.id), "tracking_token": existing.tracking_token,
                    "status": existing.status, "payment_url": None,
                    "discount_cents": 0, "idempotent_replay": True}

    basket: list[dict] = []
    subtotal = 0
    for it in items:
        qty = int(it.get("quantity") or 0)
        if qty <= 0:
            raise HTTPException(422, "Invalid quantity")
        product = (await session.execute(select(StorefrontProductModel).where(
            StorefrontProductModel.store_id == store.id,
            StorefrontProductModel.slug == it.get("product_slug"),
            StorefrontProductModel.status == "active").limit(1))).scalar_one_or_none()
        if product is None:
            raise HTTPException(404, f"Product '{it.get('product_slug')}' not available")

        variant = None
        if it.get("variant_sku"):
            variant = (await session.execute(select(StorefrontProductVariantModel).where(
                StorefrontProductVariantModel.product_id == product.id,
                StorefrontProductVariantModel.sku == it["variant_sku"]).limit(1))).scalar_one_or_none()
            if variant is None:
                raise HTTPException(404, f"Variant '{it['variant_sku']}' not found")

        # Reserve stock atomically (invariant 2).
        ok = (await _reserve_variant(session, variant.id, qty) if variant
              else await _reserve_product(session, product, qty))
        if not ok:
            raise HTTPException(409, f"'{product.name}' is out of stock")

        unit = (variant.price_cents if (variant and variant.price_cents is not None)
                else product.price_cents)
        subtotal += unit * qty
        basket.append({
            "sku": (variant.sku if variant else product.sku) or product.slug,
            "name": product.name,
            "quantity": qty,
            "unit_price": unit,
            "metadata": {"product_slug": product.slug,
                         "variant_sku": variant.sku if variant else None},
        })

    # Re-validate the promo at order time (invariant 3).
    discount_cents = 0
    promo_id = None
    if discount_code:
        v = await sf_service.validate_promotion(
            session, store_id=store.id, code=discount_code, subtotal_cents=subtotal,
            customer_email=(customer or {}).get("email"))
        if v["valid"]:
            discount_cents = v["discount_cents"]
            promo_id = v["promotion_id"]

    result = await checkout_svc.create_order(
        session,
        tenant=store.tenant_id,
        basket=basket,
        customer=customer,
        shipping=shipping,
        currency=store.currency,
        metadata={"store_slug": store.slug, "discount_code": discount_code or None},
        source="storefront",
        created_by=f"hxstorefront:{store.slug}",
        discount_cents=discount_cents,
        idempotency_key=idempotency_key,
    )

    if promo_id:
        session.add(StorefrontPromotionUseModel(
            promotion_id=promo_id, order_ref=result["order_id"],
            customer_email=(customer or {}).get("email")))

    session.add(StorefrontAnalyticsEventModel(
        store_id=store.id, event="store.checkout_completed",
        data={"order_id": result["order_id"], "total_cents": max(0, subtotal - discount_cents),
              "items": len(basket)}))

    result["discount_cents"] = discount_cents
    return result
