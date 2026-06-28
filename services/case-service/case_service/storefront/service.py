"""HxStorefront shared business logic — stock movement + promotion validation.

Reused by admin (inventory adjust, promotion CRUD), the public storefront (validate
a code at the basket), and the order bridge (decrement stock + re-validate at order
creation — key invariants 2 and 3).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import (
    StorefrontInventoryLogModel, StorefrontProductVariantModel,
    StorefrontPromotionModel, StorefrontPromotionUseModel,
)


async def adjust_stock(
    session: AsyncSession, variant: StorefrontProductVariantModel,
    change: int, reason: str = "adjustment", actor: str | None = None,
) -> int | None:
    """Apply a signed stock delta to a variant and log the movement. Returns the new
    quantity (None if the variant tracks unlimited stock). Caller commits."""
    if variant.stock_quantity is None:
        # Unlimited — log the intent but don't track a number.
        session.add(StorefrontInventoryLogModel(
            variant_id=variant.id, change=change, new_quantity=None, reason=reason, actor=actor))
        return None
    variant.stock_quantity = variant.stock_quantity + change
    session.add(StorefrontInventoryLogModel(
        variant_id=variant.id, change=change, new_quantity=variant.stock_quantity,
        reason=reason, actor=actor))
    return variant.stock_quantity


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def validate_promotion(
    session: AsyncSession, *, store_id, code: str, subtotal_cents: int,
    customer_email: str | None = None,
) -> dict:
    """Validate a discount code for a store and compute the discount in minor units.

    Returns {valid, discount_cents, free_shipping, promotion_id, reason}. Covers the
    common code-based types (percentage / fixed / free_shipping); automatic discounts
    (bxgy/spend/quantity/bundle) are evaluated at basket build, not here."""
    fail = lambda why: {"valid": False, "discount_cents": 0, "free_shipping": False,
                        "promotion_id": None, "reason": why}
    if not code:
        return fail("no code")

    promo = (await session.execute(
        select(StorefrontPromotionModel).where(
            StorefrontPromotionModel.store_id == store_id,
            StorefrontPromotionModel.code == code,
            StorefrontPromotionModel.active.is_(True),
        ).limit(1))).scalar_one_or_none()
    if promo is None:
        return fail("invalid code")

    now = _now()
    if promo.valid_from and now < promo.valid_from:
        return fail("not yet active")
    if promo.valid_until and now > promo.valid_until:
        return fail("expired")
    if promo.min_order_cents and subtotal_cents < promo.min_order_cents:
        return fail("minimum order not met")

    # Usage limits.
    if promo.usage_limit is not None:
        used = (await session.execute(
            select(func.count()).select_from(StorefrontPromotionUseModel)
            .where(StorefrontPromotionUseModel.promotion_id == promo.id))).scalar_one()
        if used >= promo.usage_limit:
            return fail("usage limit reached")
    if promo.per_customer_limit is not None and customer_email:
        used_by = (await session.execute(
            select(func.count()).select_from(StorefrontPromotionUseModel).where(
                StorefrontPromotionUseModel.promotion_id == promo.id,
                StorefrontPromotionUseModel.customer_email == customer_email))).scalar_one()
        if used_by >= promo.per_customer_limit:
            return fail("per-customer limit reached")

    cfg = promo.config or {}
    discount_cents = 0
    free_shipping = False
    if promo.discount_type == "percentage":
        pct = float(cfg.get("percent", 0))
        discount_cents = int(round(subtotal_cents * pct / 100.0))
    elif promo.discount_type == "fixed":
        discount_cents = min(int(cfg.get("amount_cents", 0)), subtotal_cents)
    elif promo.discount_type == "free_shipping":
        free_shipping = True
    else:
        return fail(f"discount type '{promo.discount_type}' not applied at checkout")

    return {"valid": True, "discount_cents": discount_cents, "free_shipping": free_shipping,
            "promotion_id": str(promo.id), "reason": "ok"}
