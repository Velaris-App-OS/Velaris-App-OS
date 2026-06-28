"""HxStorefront — PUBLIC storefront API (no auth; served to /store/:slug).

Read-only endpoints the public storefront renders from. Key invariant 1: these
endpoints only read — they can never modify catalogue data. Tenant + enablement are
resolved from the store slug (get_public_store gates on the owning tenant's install),
so a store whose tenant has uninstalled HxStorefront goes dark (404).

The one write here is a newsletter sign-up (storefront_subscribers) — collected
emails live only in that table and are never merged into user accounts (invariant 4).

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.middleware.endpoint_rate_limit import SlidingWindowLimiter

from case_service.db.models import (
    StorefrontAnalyticsEventModel, StorefrontCategoryModel, StorefrontNavigationModel,
    StorefrontPageModel, StorefrontProductImageModel, StorefrontProductModel,
    StorefrontProductVariantModel, StorefrontSubscriberModel, StorefrontThemeModel,
    StorefrontVariantOptionModel,
)
from case_service.db.session import get_session
from case_service.storefront import service as sf_service
from case_service.storefront.checkout import place_storefront_order
from case_service.storefront.common import get_public_store

router = APIRouter(prefix="/storefront/public", tags=["storefront-public"])

# These are UNAUTHENTICATED public writes (checkout / event ingest / newsletter) —
# rate-limit per IP to blunt order-spam, analytics-flooding, and subscriber-spam.
# Per-process/in-memory (same trade-off as the rest of the codebase); skipped under
# pytest so tests can drive freely.
_checkout_rl = SlidingWindowLimiter(max_calls=20, window_seconds=60.0, name="sf_checkout")
_event_rl = SlidingWindowLimiter(max_calls=120, window_seconds=60.0, name="sf_event")
_subscribe_rl = SlidingWindowLimiter(max_calls=10, window_seconds=60.0, name="sf_subscribe")


def _enforce(limiter: SlidingWindowLimiter, request: Request) -> None:
    if "PYTEST_CURRENT_TEST" in os.environ:
        return
    ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or (
        request.client.host if request.client else "?")
    allowed, retry_after = limiter.allow(ip)
    if not allowed:
        raise HTTPException(429, "Too many requests; please slow down.",
                            headers={"Retry-After": str(retry_after)})


def _public_product(p: StorefrontProductModel) -> dict:
    return {
        "name": p.name, "slug": p.slug, "short_description": p.short_description,
        "price_cents": p.price_cents, "compare_at_cents": p.compare_at_cents,
        "is_featured": p.is_featured, "is_digital": p.is_digital,
        "in_stock": p.stock_quantity is None or p.stock_quantity > 0,
    }


@router.get("/{slug}/config")
async def public_config(slug: str, session: AsyncSession = Depends(get_session)):
    """Store config + active theme + navigation — the storefront's bootstrap payload."""
    store = await get_public_store(session, slug)
    theme = (await session.execute(
        select(StorefrontThemeModel).where(
            StorefrontThemeModel.store_id == store.id, StorefrontThemeModel.is_active.is_(True))
        .order_by(StorefrontThemeModel.version.desc()).limit(1))).scalar_one_or_none()
    navs = (await session.execute(select(StorefrontNavigationModel)
            .where(StorefrontNavigationModel.store_id == store.id))).scalars().all()
    return {
        "store": {"slug": store.slug, "name": store.name, "currency": store.currency,
                  "locale": store.locale},
        "theme": theme.config if theme else {},
        "navigation": {n.location: n.items for n in navs},
    }


@router.get("/{slug}/products")
async def public_products(
    slug: str, limit: int = 24, offset: int = 0,
    session: AsyncSession = Depends(get_session),
):
    store = await get_public_store(session, slug)
    rows = (await session.execute(
        select(StorefrontProductModel).where(
            StorefrontProductModel.store_id == store.id,
            StorefrontProductModel.status == "active")
        .order_by(StorefrontProductModel.is_featured.desc(), StorefrontProductModel.created_at.desc())
        .limit(min(limit, 100)).offset(offset))).scalars().all()
    return {"products": [_public_product(p) for p in rows]}


@router.get("/{slug}/products/{product_slug}")
async def public_product_detail(
    slug: str, product_slug: str, session: AsyncSession = Depends(get_session),
):
    store = await get_public_store(session, slug)
    p = (await session.execute(select(StorefrontProductModel).where(
        StorefrontProductModel.store_id == store.id,
        StorefrontProductModel.slug == product_slug,
        StorefrontProductModel.status == "active").limit(1))).scalar_one_or_none()
    if p is None:
        raise HTTPException(404, "Product not found")
    options = (await session.execute(select(StorefrontVariantOptionModel)
               .where(StorefrontVariantOptionModel.product_id == p.id)
               .order_by(StorefrontVariantOptionModel.display_order))).scalars().all()
    variants = (await session.execute(select(StorefrontProductVariantModel)
                .where(StorefrontProductVariantModel.product_id == p.id)
                .order_by(StorefrontProductVariantModel.display_order))).scalars().all()
    images = (await session.execute(select(StorefrontProductImageModel)
              .where(StorefrontProductImageModel.product_id == p.id)
              .order_by(StorefrontProductImageModel.display_order))).scalars().all()
    detail = _public_product(p)
    detail.update({
        "description": p.description, "tags": p.tags,
        "options": [{"name": o.name, "values": o.values} for o in options],
        "variants": [{"sku": v.sku, "option_values": v.option_values,
                      "price_cents": v.price_cents if v.price_cents is not None else p.price_cents,
                      "in_stock": v.stock_quantity is None or v.stock_quantity > 0} for v in variants],
        "images": [{"media_path": i.media_path, "alt_text": i.alt_text} for i in images],
    })
    return detail


@router.get("/{slug}/categories")
async def public_categories(slug: str, session: AsyncSession = Depends(get_session)):
    store = await get_public_store(session, slug)
    rows = (await session.execute(select(StorefrontCategoryModel)
            .where(StorefrontCategoryModel.store_id == store.id)
            .order_by(StorefrontCategoryModel.display_order))).scalars().all()
    return {"categories": [{"name": c.name, "slug": c.slug,
                            "parent_slug": None, "id": str(c.id),
                            "parent_id": str(c.parent_id) if c.parent_id else None} for c in rows]}


@router.get("/{slug}/categories/{cat_slug}")
async def public_category_products(
    slug: str, cat_slug: str, session: AsyncSession = Depends(get_session),
):
    store = await get_public_store(session, slug)
    cat = (await session.execute(select(StorefrontCategoryModel).where(
        StorefrontCategoryModel.store_id == store.id,
        StorefrontCategoryModel.slug == cat_slug).limit(1))).scalar_one_or_none()
    if cat is None:
        raise HTTPException(404, "Category not found")
    from case_service.db.models import StorefrontProductCategoryModel
    rows = (await session.execute(
        select(StorefrontProductModel)
        .join(StorefrontProductCategoryModel, StorefrontProductCategoryModel.product_id == StorefrontProductModel.id)
        .where(StorefrontProductCategoryModel.category_id == cat.id,
               StorefrontProductModel.status == "active"))).scalars().all()
    return {"category": {"name": cat.name, "slug": cat.slug},
            "products": [_public_product(p) for p in rows]}


@router.get("/{slug}/search")
async def public_search(slug: str, q: str = "", session: AsyncSession = Depends(get_session)):
    store = await get_public_store(session, slug)
    if not q.strip():
        return {"query": q, "products": []}
    like = f"%{q.strip()}%"
    rows = (await session.execute(
        select(StorefrontProductModel).where(
            StorefrontProductModel.store_id == store.id,
            StorefrontProductModel.status == "active",
            or_(StorefrontProductModel.name.ilike(like),
                StorefrontProductModel.short_description.ilike(like)))
        .limit(50))).scalars().all()
    return {"query": q, "products": [_public_product(p) for p in rows]}


@router.post("/{slug}/promotions/validate")
async def public_validate_promotion(
    slug: str, body: dict, session: AsyncSession = Depends(get_session),
):
    store = await get_public_store(session, slug)
    return await sf_service.validate_promotion(
        session, store_id=store.id, code=body.get("code", ""),
        subtotal_cents=int(body.get("subtotal_cents", 0)),
        customer_email=body.get("customer_email"))


class SubscribeReq(BaseModel):
    email: str


@router.post("/{slug}/subscribers", status_code=201)
async def public_subscribe(
    slug: str, body: SubscribeReq, request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Newsletter sign-up. Stored only in storefront_subscribers (invariant 4)."""
    _enforce(_subscribe_rl, request)
    store = await get_public_store(session, slug)
    email = (body.email or "").strip().lower()
    if "@" not in email:
        raise HTTPException(422, "Invalid email")
    existing = (await session.execute(select(StorefrontSubscriberModel.id).where(
        StorefrontSubscriberModel.store_id == store.id,
        StorefrontSubscriberModel.email == email).limit(1))).first()
    if not existing:
        session.add(StorefrontSubscriberModel(store_id=store.id, email=email))
        await session.commit()
    return {"subscribed": True}


@router.get("/{slug}/sitemap.xml")
async def public_sitemap(slug: str, session: AsyncSession = Depends(get_session)):
    store = await get_public_store(session, slug)
    base = f"/store/{store.slug}"
    urls = [base, f"{base}/products"]
    for p in (await session.execute(select(StorefrontProductModel.slug).where(
            StorefrontProductModel.store_id == store.id,
            StorefrontProductModel.status == "active"))).scalars().all():
        urls.append(f"{base}/products/{p}")
    for c in (await session.execute(select(StorefrontCategoryModel.slug)
              .where(StorefrontCategoryModel.store_id == store.id))).scalars().all():
        urls.append(f"{base}/categories/{c}")
    page_rows = (await session.execute(select(StorefrontPageModel.page_slug).where(
        StorefrontPageModel.store_id == store.id,
        StorefrontPageModel.is_published.is_(True)))).scalars().all()
    for pg in page_rows:
        urls.append(f"{base}/pages/{pg}")
    body = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
    xml = f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>'
    return Response(content=xml, media_type="application/xml")


# ── Checkout (basket → HxCheckout Order) ──────────────────────────────────────

class CheckoutItem(BaseModel):
    product_slug: str
    variant_sku: str | None = None
    quantity: int = 1


class CheckoutReq(BaseModel):
    items: list[CheckoutItem]
    customer: dict = {}
    shipping: dict = {}
    discount_code: str = ""


@router.post("/{slug}/checkout", status_code=201)
async def public_checkout(
    slug: str, body: CheckoutReq, request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    session: AsyncSession = Depends(get_session),
):
    """Place an order from the storefront basket. Prices come from the catalogue,
    stock is reserved atomically, the promo is re-validated, and the order flows to
    HxCheckout. Requires HxCheckout to also be installed for the tenant. Rate-limited
    per IP; idempotent via Idempotency-Key (double-submit safe)."""
    _enforce(_checkout_rl, request)
    store = await get_public_store(session, slug)
    # HxCheckout is a hard dependency for accepting orders.
    from case_service.api.routers.checkout import require_installed as checkout_installed
    try:
        await checkout_installed(session, store.tenant_id)
    except HTTPException:
        raise HTTPException(409, "Checkout is not configured for this store")
    return await place_storefront_order(
        session, store=store,
        items=[i.model_dump() for i in body.items],
        customer=body.customer, shipping=body.shipping, discount_code=body.discount_code,
        idempotency_key=idempotency_key)


class EventReq(BaseModel):
    event: str
    data: dict = {}
    session_id: str | None = None


@router.post("/{slug}/events", status_code=201)
async def public_event(
    slug: str, body: EventReq, request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Ingest a client-side commerce analytics event (page_view, add_to_basket, …)."""
    _enforce(_event_rl, request)
    store = await get_public_store(session, slug)
    session.add(StorefrontAnalyticsEventModel(
        store_id=store.id, event=body.event[:50], data=body.data or {}, session=body.session_id))
    await session.commit()
    return {"recorded": True}
