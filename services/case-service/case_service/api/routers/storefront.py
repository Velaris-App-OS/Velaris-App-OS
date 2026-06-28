"""HxStorefront — Studio management API (staff JWT).

Marketplace `module` package `velaris/hxstorefront`. Enablement is the standard
marketplace-install gate (like HxTest/HxCheckout); until installed, every endpoint
404s. The Python ships in-image; install only flips these routes + the Studio module.

This module holds store + catalogue management. Public storefront read endpoints are
in storefront_public.py.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.auth.dependencies import get_current_user, require_admin
from case_service.auth.models import AuthenticatedUser
from case_service.config import get_settings
from case_service.db.models import (
    StorefrontCategoryModel, StorefrontInventoryLogModel, StorefrontNavigationModel,
    StorefrontPageModel, StorefrontProductCategoryModel, StorefrontProductImageModel,
    StorefrontMediaModel, StorefrontProductModel, StorefrontProductVariantModel,
    StorefrontPromotionModel, StorefrontSeoOverrideModel, StorefrontStoreModel,
    StorefrontThemeModel, StorefrontVariantOptionModel,
)
from case_service.db.session import get_session
from case_service.storefront import content as sf_content
from case_service.storefront import service as sf_service
from case_service.storefront.common import get_store_for_user, require_enabled_user

router = APIRouter(prefix="/storefront", tags=["storefront"])

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _slugify(value: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return s or "store"


def _store_dict(s: StorefrontStoreModel) -> dict:
    return {
        "id": str(s.id), "slug": s.slug, "name": s.name, "currency": s.currency,
        "locale": s.locale, "status": s.status, "settings": s.settings,
        "created_at": s.created_at.isoformat(),
    }


# ══════════════════════════════════════════════════════════════════════════════
#  STORES
# ══════════════════════════════════════════════════════════════════════════════

class StoreCreate(BaseModel):
    name: str
    slug: str | None = None
    currency: str | None = None
    locale: str = "en-GB"
    settings: dict = {}


class StoreUpdate(BaseModel):
    name: str | None = None
    currency: str | None = None
    locale: str | None = None
    status: str | None = None
    settings: dict | None = None


@router.get("/stores")
async def list_stores(
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await require_enabled_user(session, user)
    tenant = user.tenant_id or "default"
    rows = (await session.execute(
        select(StorefrontStoreModel).where(StorefrontStoreModel.tenant_id == tenant)
        .order_by(StorefrontStoreModel.created_at.desc())
    )).scalars().all()
    return {"stores": [_store_dict(s) for s in rows]}


@router.post("/stores", status_code=201)
async def create_store(
    body: StoreCreate,
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await require_enabled_user(session, user)
    tenant = user.tenant_id or "default"

    # Enforce the per-tenant store cap.
    count = (await session.execute(
        select(func.count()).select_from(StorefrontStoreModel)
        .where(StorefrontStoreModel.tenant_id == tenant))).scalar_one()
    if count >= get_settings().storefront_max_stores_per_tenant:
        raise HTTPException(409, "Store limit reached for this tenant")

    slug = _slugify(body.slug or body.name)
    if not _SLUG_RE.match(slug):
        raise HTTPException(422, "Invalid slug")
    # Slugs are globally unique (public route). Reject collisions explicitly.
    if (await session.execute(select(StorefrontStoreModel.id)
                              .where(StorefrontStoreModel.slug == slug).limit(1))).first():
        raise HTTPException(409, f"Store slug '{slug}' is taken")

    store = StorefrontStoreModel(
        tenant_id=tenant, slug=slug, name=body.name,
        currency=(body.currency or get_settings().storefront_default_currency).upper(),
        locale=body.locale, settings=body.settings or {})
    session.add(store)
    await session.commit()
    return _store_dict(store)


@router.get("/stores/{slug}")
async def get_store(
    slug: str,
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await require_enabled_user(session, user)
    return _store_dict(await get_store_for_user(session, slug, user))


@router.put("/stores/{slug}")
async def update_store(
    slug: str,
    body: StoreUpdate,
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await require_enabled_user(session, user)
    store = await get_store_for_user(session, slug, user)
    if body.name is not None:
        store.name = body.name
    if body.currency is not None:
        store.currency = body.currency.upper()
    if body.locale is not None:
        store.locale = body.locale
    if body.status is not None:
        store.status = body.status
    if body.settings is not None:
        store.settings = body.settings
    await session.commit()
    return _store_dict(store)


@router.delete("/stores/{slug}")
async def archive_store(
    slug: str,
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Archive a store (not deleted — order history is preserved)."""
    await require_enabled_user(session, user)
    store = await get_store_for_user(session, slug, user)
    store.status = "archived"
    await session.commit()
    return {"archived": slug}


@router.post("/stores/{slug}/clone", status_code=201)
async def clone_store(
    slug: str,
    body: StoreCreate,
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Clone a store's CONFIG (settings, active theme, pages, navigation) to a new
    slug — NOT products or orders. Useful for agencies spinning up similar stores."""
    await require_enabled_user(session, user)
    src = await get_store_for_user(session, slug, user)
    tenant = user.tenant_id or "default"

    new_slug = _slugify(body.slug or body.name)
    if (await session.execute(select(StorefrontStoreModel.id)
                              .where(StorefrontStoreModel.slug == new_slug).limit(1))).first():
        raise HTTPException(409, f"Store slug '{new_slug}' is taken")

    clone = StorefrontStoreModel(
        tenant_id=tenant, slug=new_slug, name=body.name,
        currency=src.currency, locale=src.locale, settings=dict(src.settings or {}))
    session.add(clone)
    await session.flush()

    # Copy active theme.
    theme = (await session.execute(
        select(StorefrontThemeModel).where(
            StorefrontThemeModel.store_id == src.id, StorefrontThemeModel.is_active.is_(True))
        .order_by(StorefrontThemeModel.version.desc()).limit(1))).scalar_one_or_none()
    if theme:
        session.add(StorefrontThemeModel(store_id=clone.id, config=dict(theme.config or {}), version=1))
    # Copy pages + navigation.
    for pg in (await session.execute(
            select(StorefrontPageModel).where(StorefrontPageModel.store_id == src.id))).scalars().all():
        session.add(StorefrontPageModel(store_id=clone.id, page_slug=pg.page_slug,
                                        title=pg.title, sections=list(pg.sections or []),
                                        is_published=pg.is_published))
    for nav in (await session.execute(
            select(StorefrontNavigationModel).where(StorefrontNavigationModel.store_id == src.id))).scalars().all():
        session.add(StorefrontNavigationModel(store_id=clone.id, location=nav.location,
                                              items=list(nav.items or [])))
    await session.commit()
    return _store_dict(clone)


# ══════════════════════════════════════════════════════════════════════════════
#  PRODUCTS
# ══════════════════════════════════════════════════════════════════════════════

def _product_dict(p: StorefrontProductModel, *, detail: bool = False,
                  options=None, variants=None, images=None) -> dict:
    d = {
        "id": str(p.id), "name": p.name, "slug": p.slug, "sku": p.sku,
        "short_description": p.short_description, "price_cents": p.price_cents,
        "compare_at_cents": p.compare_at_cents, "tax_class": p.tax_class,
        "status": p.status, "stock_quantity": p.stock_quantity,
        "is_featured": p.is_featured, "is_digital": p.is_digital,
    }
    if detail:
        d.update({
            "description": p.description, "tags": p.tags, "weight_grams": p.weight_grams,
            "low_stock_threshold": p.low_stock_threshold, "metadata": p.product_meta,
            "options": [{"id": str(o.id), "name": o.name, "values": o.values} for o in (options or [])],
            "variants": [{"id": str(v.id), "sku": v.sku, "option_values": v.option_values,
                          "price_cents": v.price_cents, "stock_quantity": v.stock_quantity}
                         for v in (variants or [])],
            "images": [{"id": str(i.id), "media_path": i.media_path, "alt_text": i.alt_text,
                        "display_order": i.display_order} for i in (images or [])],
        })
    return d


class ProductCreate(BaseModel):
    name: str
    slug: str | None = None
    sku: str | None = None
    description: str = ""
    short_description: str = ""
    tags: list = []
    price_cents: int = 0
    compare_at_cents: int | None = None
    tax_class: str = "standard"
    weight_grams: int = 0
    status: str = "draft"
    stock_quantity: int | None = None
    low_stock_threshold: int | None = None
    is_featured: bool = False
    is_digital: bool = False
    metadata: dict = {}


class ProductUpdate(BaseModel):
    name: str | None = None
    sku: str | None = None
    description: str | None = None
    short_description: str | None = None
    tags: list | None = None
    price_cents: int | None = None
    compare_at_cents: int | None = None
    tax_class: str | None = None
    weight_grams: int | None = None
    status: str | None = None
    stock_quantity: int | None = None
    low_stock_threshold: int | None = None
    is_featured: bool | None = None
    is_digital: bool | None = None
    metadata: dict | None = None


async def _get_product(session: AsyncSession, store: StorefrontStoreModel, product_id: str) -> StorefrontProductModel:
    try:
        pid = uuid.UUID(product_id)
    except ValueError:
        raise HTTPException(404, "Product not found")
    p = await session.get(StorefrontProductModel, pid)
    if p is None or p.store_id != store.id:
        raise HTTPException(404, "Product not found")
    return p


@router.get("/stores/{slug}/products")
async def list_products(
    slug: str, status: str | None = None, limit: int = 50, offset: int = 0,
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await require_enabled_user(session, user)
    store = await get_store_for_user(session, slug, user)
    stmt = select(StorefrontProductModel).where(StorefrontProductModel.store_id == store.id)
    if status:
        stmt = stmt.where(StorefrontProductModel.status == status)
    stmt = stmt.order_by(StorefrontProductModel.created_at.desc()).limit(min(limit, 200)).offset(offset)
    rows = (await session.execute(stmt)).scalars().all()
    return {"products": [_product_dict(p) for p in rows]}


@router.post("/stores/{slug}/products", status_code=201)
async def create_product(
    slug: str, body: ProductCreate,
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await require_enabled_user(session, user)
    store = await get_store_for_user(session, slug, user)

    count = (await session.execute(
        select(func.count()).select_from(StorefrontProductModel)
        .where(StorefrontProductModel.store_id == store.id))).scalar_one()
    if count >= get_settings().storefront_max_products_per_store:
        raise HTTPException(409, "Product limit reached for this store")

    pslug = _slugify(body.slug or body.name)
    if (await session.execute(select(StorefrontProductModel.id).where(
            StorefrontProductModel.store_id == store.id,
            StorefrontProductModel.slug == pslug).limit(1))).first():
        raise HTTPException(409, f"Product slug '{pslug}' is taken in this store")

    p = StorefrontProductModel(
        store_id=store.id, name=body.name, slug=pslug, sku=body.sku,
        description=body.description, short_description=body.short_description,
        tags=body.tags or [], price_cents=body.price_cents, compare_at_cents=body.compare_at_cents,
        tax_class=body.tax_class, weight_grams=body.weight_grams, status=body.status,
        stock_quantity=body.stock_quantity, low_stock_threshold=body.low_stock_threshold,
        is_featured=body.is_featured, is_digital=body.is_digital, product_meta=body.metadata or {})
    session.add(p)
    await session.commit()
    return _product_dict(p, detail=True)


@router.get("/stores/{slug}/products/{product_id}")
async def get_product(
    slug: str, product_id: str,
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await require_enabled_user(session, user)
    store = await get_store_for_user(session, slug, user)
    p = await _get_product(session, store, product_id)
    options = (await session.execute(select(StorefrontVariantOptionModel)
               .where(StorefrontVariantOptionModel.product_id == p.id)
               .order_by(StorefrontVariantOptionModel.display_order))).scalars().all()
    variants = (await session.execute(select(StorefrontProductVariantModel)
                .where(StorefrontProductVariantModel.product_id == p.id)
                .order_by(StorefrontProductVariantModel.display_order))).scalars().all()
    images = (await session.execute(select(StorefrontProductImageModel)
              .where(StorefrontProductImageModel.product_id == p.id)
              .order_by(StorefrontProductImageModel.display_order))).scalars().all()
    return _product_dict(p, detail=True, options=options, variants=variants, images=images)


@router.put("/stores/{slug}/products/{product_id}")
async def update_product(
    slug: str, product_id: str, body: ProductUpdate,
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await require_enabled_user(session, user)
    store = await get_store_for_user(session, slug, user)
    p = await _get_product(session, store, product_id)
    for field in ("name", "sku", "description", "short_description", "tags", "price_cents",
                  "compare_at_cents", "tax_class", "weight_grams", "status", "stock_quantity",
                  "low_stock_threshold", "is_featured", "is_digital"):
        val = getattr(body, field)
        if val is not None:
            setattr(p, field, val)
    if body.metadata is not None:
        p.product_meta = body.metadata
    await session.commit()
    return _product_dict(p, detail=True)


@router.delete("/stores/{slug}/products/{product_id}")
async def archive_product(
    slug: str, product_id: str,
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await require_enabled_user(session, user)
    store = await get_store_for_user(session, slug, user)
    p = await _get_product(session, store, product_id)
    p.status = "archived"
    await session.commit()
    return {"archived": product_id}


# ── Variant options + variants ────────────────────────────────────────────────

class OptionCreate(BaseModel):
    name: str
    values: list = []


class VariantCreate(BaseModel):
    sku: str
    option_values: dict = {}
    price_cents: int | None = None
    stock_quantity: int | None = None


@router.post("/stores/{slug}/products/{product_id}/options", status_code=201)
async def add_option(
    slug: str, product_id: str, body: OptionCreate,
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await require_enabled_user(session, user)
    store = await get_store_for_user(session, slug, user)
    p = await _get_product(session, store, product_id)
    opt = StorefrontVariantOptionModel(product_id=p.id, name=body.name, values=body.values or [])
    session.add(opt)
    await session.commit()
    return {"id": str(opt.id), "name": opt.name, "values": opt.values}


@router.post("/stores/{slug}/products/{product_id}/variants", status_code=201)
async def add_variant(
    slug: str, product_id: str, body: VariantCreate,
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await require_enabled_user(session, user)
    store = await get_store_for_user(session, slug, user)
    p = await _get_product(session, store, product_id)
    if (await session.execute(select(StorefrontProductVariantModel.id).where(
            StorefrontProductVariantModel.product_id == p.id,
            StorefrontProductVariantModel.sku == body.sku).limit(1))).first():
        raise HTTPException(409, f"Variant SKU '{body.sku}' already exists for this product")
    v = StorefrontProductVariantModel(
        product_id=p.id, sku=body.sku, option_values=body.option_values or {},
        price_cents=body.price_cents, stock_quantity=body.stock_quantity)
    session.add(v)
    await session.commit()
    return {"id": str(v.id), "sku": v.sku, "option_values": v.option_values,
            "price_cents": v.price_cents, "stock_quantity": v.stock_quantity}


@router.delete("/stores/{slug}/products/{product_id}/variants/{variant_id}")
async def delete_variant(
    slug: str, product_id: str, variant_id: str,
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await require_enabled_user(session, user)
    store = await get_store_for_user(session, slug, user)
    p = await _get_product(session, store, product_id)
    try:
        v = await session.get(StorefrontProductVariantModel, uuid.UUID(variant_id))
    except ValueError:
        v = None
    if v is None or v.product_id != p.id:
        raise HTTPException(404, "Variant not found")
    await session.delete(v)
    await session.commit()
    return {"deleted": variant_id}


# ══════════════════════════════════════════════════════════════════════════════
#  CATEGORIES
# ══════════════════════════════════════════════════════════════════════════════

class CategoryCreate(BaseModel):
    name: str
    slug: str | None = None
    parent_id: str | None = None
    description: str = ""
    display_order: int = 0


def _category_dict(c: StorefrontCategoryModel) -> dict:
    return {"id": str(c.id), "name": c.name, "slug": c.slug,
            "parent_id": str(c.parent_id) if c.parent_id else None,
            "description": c.description, "display_order": c.display_order}


@router.get("/stores/{slug}/categories")
async def list_categories(
    slug: str,
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Full category tree (flat list with parent_id; client builds the tree)."""
    await require_enabled_user(session, user)
    store = await get_store_for_user(session, slug, user)
    rows = (await session.execute(select(StorefrontCategoryModel)
            .where(StorefrontCategoryModel.store_id == store.id)
            .order_by(StorefrontCategoryModel.display_order))).scalars().all()
    return {"categories": [_category_dict(c) for c in rows]}


@router.post("/stores/{slug}/categories", status_code=201)
async def create_category(
    slug: str, body: CategoryCreate,
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await require_enabled_user(session, user)
    store = await get_store_for_user(session, slug, user)
    cslug = _slugify(body.slug or body.name)
    if (await session.execute(select(StorefrontCategoryModel.id).where(
            StorefrontCategoryModel.store_id == store.id,
            StorefrontCategoryModel.slug == cslug).limit(1))).first():
        raise HTTPException(409, f"Category slug '{cslug}' is taken in this store")
    parent_id = None
    if body.parent_id:
        try:
            parent_id = uuid.UUID(body.parent_id)
        except ValueError:
            raise HTTPException(422, "Invalid parent_id")
    c = StorefrontCategoryModel(
        store_id=store.id, parent_id=parent_id, name=body.name, slug=cslug,
        description=body.description, display_order=body.display_order)
    session.add(c)
    await session.commit()
    return _category_dict(c)


@router.delete("/stores/{slug}/categories/{category_id}")
async def delete_category(
    slug: str, category_id: str,
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Delete a category (must be empty of products)."""
    await require_enabled_user(session, user)
    store = await get_store_for_user(session, slug, user)
    try:
        cid = uuid.UUID(category_id)
    except ValueError:
        raise HTTPException(404, "Category not found")
    c = await session.get(StorefrontCategoryModel, cid)
    if c is None or c.store_id != store.id:
        raise HTTPException(404, "Category not found")
    has_products = (await session.execute(select(StorefrontProductCategoryModel.id)
                    .where(StorefrontProductCategoryModel.category_id == cid).limit(1))).first()
    if has_products:
        raise HTTPException(409, "Category is not empty")
    await session.delete(c)
    await session.commit()
    return {"deleted": category_id}


@router.put("/stores/{slug}/products/{product_id}/categories")
async def set_product_categories(
    slug: str, product_id: str, body: dict,
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Replace a product's category assignments. body = {category_ids: [...]}."""
    await require_enabled_user(session, user)
    store = await get_store_for_user(session, slug, user)
    p = await _get_product(session, store, product_id)
    category_ids = body.get("category_ids") or []
    # Clear existing.
    for link in (await session.execute(select(StorefrontProductCategoryModel)
                 .where(StorefrontProductCategoryModel.product_id == p.id))).scalars().all():
        await session.delete(link)
    await session.flush()
    # Re-add (validating each category belongs to the store).
    added = []
    for cid_s in category_ids:
        try:
            cid = uuid.UUID(cid_s)
        except (ValueError, TypeError):
            continue
        cat = await session.get(StorefrontCategoryModel, cid)
        if cat is None or cat.store_id != store.id:
            continue
        session.add(StorefrontProductCategoryModel(product_id=p.id, category_id=cid))
        added.append(str(cid))
    await session.commit()
    return {"product_id": product_id, "category_ids": added}


# ══════════════════════════════════════════════════════════════════════════════
#  INVENTORY
# ══════════════════════════════════════════════════════════════════════════════

async def _get_store_variant(session, store, variant_id) -> StorefrontProductVariantModel:
    """Fetch a variant and verify it belongs to a product in `store`."""
    try:
        vid = uuid.UUID(variant_id)
    except ValueError:
        raise HTTPException(404, "Variant not found")
    v = await session.get(StorefrontProductVariantModel, vid)
    if v is None:
        raise HTTPException(404, "Variant not found")
    prod = await session.get(StorefrontProductModel, v.product_id)
    if prod is None or prod.store_id != store.id:
        raise HTTPException(404, "Variant not found")
    return v


@router.get("/stores/{slug}/inventory")
async def list_inventory(
    slug: str,
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """All variants in the store with their stock levels + low-stock flag."""
    await require_enabled_user(session, user)
    store = await get_store_for_user(session, slug, user)
    rows = (await session.execute(
        select(StorefrontProductVariantModel, StorefrontProductModel.name, StorefrontProductModel.low_stock_threshold)
        .join(StorefrontProductModel, StorefrontProductVariantModel.product_id == StorefrontProductModel.id)
        .where(StorefrontProductModel.store_id == store.id))).all()
    out = []
    for v, pname, threshold in rows:
        low = v.stock_quantity is not None and threshold is not None and v.stock_quantity <= threshold
        out.append({"variant_id": str(v.id), "product_name": pname, "sku": v.sku,
                    "stock_quantity": v.stock_quantity, "low_stock": low})
    return {"inventory": out}


@router.patch("/stores/{slug}/inventory/{variant_id}")
async def adjust_inventory(
    slug: str, variant_id: str, body: dict,
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Manual stock adjustment. body = {change: int, reason?: str}."""
    await require_enabled_user(session, user)
    store = await get_store_for_user(session, slug, user)
    v = await _get_store_variant(session, store, variant_id)
    try:
        change = int(body.get("change"))
    except (TypeError, ValueError):
        raise HTTPException(422, "change must be an integer")
    new_qty = await sf_service.adjust_stock(
        session, v, change, reason=body.get("reason") or "adjustment", actor=user.user_id)
    await session.commit()
    return {"variant_id": variant_id, "stock_quantity": new_qty}


@router.get("/stores/{slug}/inventory/{variant_id}/history")
async def inventory_history(
    slug: str, variant_id: str,
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await require_enabled_user(session, user)
    store = await get_store_for_user(session, slug, user)
    v = await _get_store_variant(session, store, variant_id)
    rows = (await session.execute(
        select(StorefrontInventoryLogModel).where(StorefrontInventoryLogModel.variant_id == v.id)
        .order_by(StorefrontInventoryLogModel.created_at.desc()).limit(200))).scalars().all()
    return {"history": [{"change": r.change, "new_quantity": r.new_quantity, "reason": r.reason,
                         "actor": r.actor, "created_at": r.created_at.isoformat()} for r in rows]}


# ══════════════════════════════════════════════════════════════════════════════
#  PROMOTIONS
# ══════════════════════════════════════════════════════════════════════════════

class PromotionCreate(BaseModel):
    code: str | None = None
    discount_type: str               # percentage|fixed|free_shipping|bxgy|spend|quantity|bundle|flash
    config: dict = {}
    applies_to: dict = {}
    min_order_cents: int | None = None
    usage_limit: int | None = None
    per_customer_limit: int | None = None
    valid_from: str | None = None
    valid_until: str | None = None
    stackable: bool = False


def _promo_dict(p: StorefrontPromotionModel) -> dict:
    return {"id": str(p.id), "code": p.code, "discount_type": p.discount_type,
            "config": p.config, "applies_to": p.applies_to, "min_order_cents": p.min_order_cents,
            "usage_limit": p.usage_limit, "per_customer_limit": p.per_customer_limit,
            "stackable": p.stackable, "active": p.active}


@router.get("/stores/{slug}/promotions")
async def list_promotions(
    slug: str,
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await require_enabled_user(session, user)
    store = await get_store_for_user(session, slug, user)
    rows = (await session.execute(select(StorefrontPromotionModel)
            .where(StorefrontPromotionModel.store_id == store.id)
            .order_by(StorefrontPromotionModel.created_at.desc()))).scalars().all()
    return {"promotions": [_promo_dict(p) for p in rows]}


@router.post("/stores/{slug}/promotions", status_code=201)
async def create_promotion(
    slug: str, body: PromotionCreate,
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await require_enabled_user(session, user)
    store = await get_store_for_user(session, slug, user)
    from datetime import datetime
    def _parse(dt):
        return datetime.fromisoformat(dt) if dt else None
    p = StorefrontPromotionModel(
        store_id=store.id, code=body.code, discount_type=body.discount_type,
        config=body.config or {}, applies_to=body.applies_to or {},
        min_order_cents=body.min_order_cents, usage_limit=body.usage_limit,
        per_customer_limit=body.per_customer_limit, stackable=body.stackable,
        valid_from=_parse(body.valid_from), valid_until=_parse(body.valid_until))
    session.add(p)
    await session.commit()
    return _promo_dict(p)


async def _get_promotion(session, store, promo_id) -> StorefrontPromotionModel:
    try:
        pid = uuid.UUID(promo_id)
    except ValueError:
        raise HTTPException(404, "Promotion not found")
    p = await session.get(StorefrontPromotionModel, pid)
    if p is None or p.store_id != store.id:
        raise HTTPException(404, "Promotion not found")
    return p


@router.put("/stores/{slug}/promotions/{promo_id}")
async def update_promotion(
    slug: str, promo_id: str, body: dict,
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await require_enabled_user(session, user)
    store = await get_store_for_user(session, slug, user)
    p = await _get_promotion(session, store, promo_id)
    for field in ("code", "config", "applies_to", "min_order_cents", "usage_limit",
                  "per_customer_limit", "stackable", "active", "discount_type"):
        if field in body:
            setattr(p, field, body[field])
    await session.commit()
    return _promo_dict(p)


@router.delete("/stores/{slug}/promotions/{promo_id}")
async def deactivate_promotion(
    slug: str, promo_id: str,
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await require_enabled_user(session, user)
    store = await get_store_for_user(session, slug, user)
    p = await _get_promotion(session, store, promo_id)
    p.active = False
    await session.commit()
    return {"deactivated": promo_id}


@router.post("/stores/{slug}/promotions/validate")
async def validate_promotion_admin(
    slug: str, body: dict,
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Validate a code against a subtotal. body = {code, subtotal_cents, customer_email?}."""
    await require_enabled_user(session, user)
    store = await get_store_for_user(session, slug, user)
    return await sf_service.validate_promotion(
        session, store_id=store.id, code=body.get("code", ""),
        subtotal_cents=int(body.get("subtotal_cents", 0)),
        customer_email=body.get("customer_email"))


# ══════════════════════════════════════════════════════════════════════════════
#  THEME  (versioned — keep last 10)
# ══════════════════════════════════════════════════════════════════════════════

_THEME_KEEP = 10


async def _active_theme(session, store_id):
    return (await session.execute(
        select(StorefrontThemeModel).where(
            StorefrontThemeModel.store_id == store_id, StorefrontThemeModel.is_active.is_(True))
        .order_by(StorefrontThemeModel.version.desc()).limit(1))).scalar_one_or_none()


@router.get("/stores/{slug}/theme")
async def get_theme(
    slug: str,
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await require_enabled_user(session, user)
    store = await get_store_for_user(session, slug, user)
    t = await _active_theme(session, store.id)
    return {"config": t.config if t else {}, "version": t.version if t else 0}


@router.put("/stores/{slug}/theme")
async def save_theme(
    slug: str, body: dict,
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Save a new theme version (non-destructive). Keeps the last 10 versions; older
    ones are pruned. body = {config: {...}}."""
    await require_enabled_user(session, user)
    store = await get_store_for_user(session, slug, user)
    config = body.get("config") or {}

    versions = (await session.execute(
        select(StorefrontThemeModel).where(StorefrontThemeModel.store_id == store.id)
        .order_by(StorefrontThemeModel.version.desc()))).scalars().all()
    next_version = (versions[0].version + 1) if versions else 1
    for v in versions:
        v.is_active = False
    new = StorefrontThemeModel(store_id=store.id, config=config, version=next_version, is_active=True)
    session.add(new)
    # Prune to last _THEME_KEEP.
    for old in versions[_THEME_KEEP - 1:]:
        await session.delete(old)
    await session.commit()
    return {"config": new.config, "version": new.version}


@router.post("/stores/{slug}/theme/preview")
async def preview_theme(
    slug: str, body: dict,
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Render a theme config to CSS without saving it."""
    await require_enabled_user(session, user)
    await get_store_for_user(session, slug, user)
    return {"css": sf_content.render_theme_css(body.get("config") or {})}


# ══════════════════════════════════════════════════════════════════════════════
#  PAGES  (Page Builder)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/stores/{slug}/pages")
async def list_pages(
    slug: str,
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await require_enabled_user(session, user)
    store = await get_store_for_user(session, slug, user)
    rows = (await session.execute(select(StorefrontPageModel)
            .where(StorefrontPageModel.store_id == store.id))).scalars().all()
    return {"pages": [{"page_slug": p.page_slug, "title": p.title,
                       "is_published": p.is_published} for p in rows]}


@router.get("/stores/{slug}/pages/{page_slug}")
async def get_page(
    slug: str, page_slug: str,
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await require_enabled_user(session, user)
    store = await get_store_for_user(session, slug, user)
    p = (await session.execute(select(StorefrontPageModel).where(
        StorefrontPageModel.store_id == store.id,
        StorefrontPageModel.page_slug == page_slug).limit(1))).scalar_one_or_none()
    if p is None:
        return {"page_slug": page_slug, "title": "", "sections": [], "is_published": False}
    return {"page_slug": p.page_slug, "title": p.title, "sections": p.sections,
            "is_published": p.is_published}


@router.put("/stores/{slug}/pages/{page_slug}")
async def save_page(
    slug: str, page_slug: str, body: dict,
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Save a page's section layout (upsert). Custom HTML sections are sanitised."""
    await require_enabled_user(session, user)
    store = await get_store_for_user(session, slug, user)
    sections = sf_content.sanitize_sections(body.get("sections") or [])
    p = (await session.execute(select(StorefrontPageModel).where(
        StorefrontPageModel.store_id == store.id,
        StorefrontPageModel.page_slug == page_slug).limit(1))).scalar_one_or_none()
    if p is None:
        p = StorefrontPageModel(store_id=store.id, page_slug=page_slug)
        session.add(p)
    p.title = body.get("title", p.title or "")
    p.sections = sections
    if "is_published" in body:
        p.is_published = bool(body["is_published"])
    await session.commit()
    return {"page_slug": p.page_slug, "title": p.title, "sections": p.sections,
            "is_published": p.is_published}


# ══════════════════════════════════════════════════════════════════════════════
#  NAVIGATION
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/stores/{slug}/navigation")
async def get_navigation(
    slug: str,
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await require_enabled_user(session, user)
    store = await get_store_for_user(session, slug, user)
    rows = (await session.execute(select(StorefrontNavigationModel)
            .where(StorefrontNavigationModel.store_id == store.id))).scalars().all()
    return {"navigation": {n.location: n.items for n in rows}}


@router.put("/stores/{slug}/navigation")
async def save_navigation(
    slug: str, body: dict,
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Save header/footer menus. body = {header: [...], footer: [...]}."""
    await require_enabled_user(session, user)
    store = await get_store_for_user(session, slug, user)
    for location in ("header", "footer"):
        if location not in body:
            continue
        nav = (await session.execute(select(StorefrontNavigationModel).where(
            StorefrontNavigationModel.store_id == store.id,
            StorefrontNavigationModel.location == location).limit(1))).scalar_one_or_none()
        if nav is None:
            nav = StorefrontNavigationModel(store_id=store.id, location=location)
            session.add(nav)
        nav.items = body[location] or []
    await session.commit()
    return {"saved": True}


# ══════════════════════════════════════════════════════════════════════════════
#  SEO
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/stores/{slug}/seo")
async def get_seo(
    slug: str, target_type: str = "store", target_id: str = "",
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await require_enabled_user(session, user)
    store = await get_store_for_user(session, slug, user)
    o = (await session.execute(select(StorefrontSeoOverrideModel).where(
        StorefrontSeoOverrideModel.store_id == store.id,
        StorefrontSeoOverrideModel.target_type == target_type,
        StorefrontSeoOverrideModel.target_id == target_id).limit(1))).scalar_one_or_none()
    if o is None:
        return {"target_type": target_type, "target_id": target_id, "meta_title": "",
                "meta_description": "", "og_title": "", "og_description": "",
                "og_image": None, "canonical_url": None}
    return {"target_type": o.target_type, "target_id": o.target_id, "meta_title": o.meta_title,
            "meta_description": o.meta_description, "og_title": o.og_title,
            "og_description": o.og_description, "og_image": o.og_image, "canonical_url": o.canonical_url}


@router.put("/stores/{slug}/seo")
async def save_seo(
    slug: str, body: dict,
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Upsert SEO fields for a target. body includes target_type, target_id, and any
    of meta_title/meta_description/og_*/canonical_url."""
    await require_enabled_user(session, user)
    store = await get_store_for_user(session, slug, user)
    target_type = body.get("target_type", "store")
    target_id = body.get("target_id", "")
    o = (await session.execute(select(StorefrontSeoOverrideModel).where(
        StorefrontSeoOverrideModel.store_id == store.id,
        StorefrontSeoOverrideModel.target_type == target_type,
        StorefrontSeoOverrideModel.target_id == target_id).limit(1))).scalar_one_or_none()
    if o is None:
        o = StorefrontSeoOverrideModel(store_id=store.id, target_type=target_type, target_id=target_id)
        session.add(o)
    for field in ("meta_title", "meta_description", "og_title", "og_description",
                  "og_image", "canonical_url"):
        if field in body:
            setattr(o, field, body[field])
    await session.commit()
    return {"saved": True, "target_type": target_type, "target_id": target_id}


# ══════════════════════════════════════════════════════════════════════════════
#  MEDIA LIBRARY  (MinIO-backed via the shared storage backend)
# ══════════════════════════════════════════════════════════════════════════════

# Accepted formats: images + video + digital-product files. SVG is intentionally
# EXCLUDED — an SVG can carry inline <script>/onload and executes when served from
# our origin (stored XSS), and validation here is filename-extension only (no content
# sniff). Raster formats don't have that problem.
_MEDIA_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif",
               ".mp4", ".webm", ".pdf", ".zip"}


def _media_dict(m: StorefrontMediaModel) -> dict:
    return {"id": str(m.id), "media_path": m.media_path, "media_type": m.media_type,
            "size_bytes": m.size_bytes, "alt_text": m.alt_text, "folder": m.folder,
            "created_at": m.created_at.isoformat()}


@router.get("/stores/{slug}/media")
async def list_media(
    slug: str, folder: str | None = None,
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await require_enabled_user(session, user)
    store = await get_store_for_user(session, slug, user)
    stmt = select(StorefrontMediaModel).where(StorefrontMediaModel.store_id == store.id)
    if folder is not None:
        stmt = stmt.where(StorefrontMediaModel.folder == folder)
    rows = (await session.execute(stmt.order_by(StorefrontMediaModel.created_at.desc()))).scalars().all()
    return {"media": [_media_dict(m) for m in rows]}


@router.post("/stores/{slug}/media", status_code=201)
async def upload_media(
    slug: str,
    file: UploadFile = File(...),
    alt_text: str = Form(""),
    folder: str = Form(""),
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Upload a media asset to the store's library (stored via the MinIO/local backend)."""
    await require_enabled_user(session, user)
    store = await get_store_for_user(session, slug, user)

    from case_service.middleware.file_security import safe_filename, validate_upload_filename
    raw = file.filename or "unnamed"
    ok, reason = validate_upload_filename(raw, allowed_extensions=_MEDIA_EXTS)
    if not ok:
        raise HTTPException(400, f"File rejected: {reason}")
    data = await file.read()
    max_bytes = get_settings().storefront_image_max_mb * 1024 * 1024
    if len(data) > max_bytes:
        raise HTTPException(413, f"File exceeds {get_settings().storefront_image_max_mb} MB limit")

    name = safe_filename(raw)
    ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    key = f"storefront/{store.id}/{uuid.uuid4().hex}{ext}"
    media_type = ("video" if ext in (".mp4", ".webm")
                  else "file" if ext in (".pdf", ".zip") else "image")

    from case_service.storage.factory import get_storage_backend
    await get_storage_backend().put(key, data, content_type=file.content_type or "application/octet-stream")

    m = StorefrontMediaModel(
        store_id=store.id, media_path=key, media_type=media_type,
        size_bytes=len(data), alt_text=alt_text, folder=folder)
    session.add(m)
    await session.commit()
    return _media_dict(m)


@router.delete("/stores/{slug}/media/{media_id}")
async def delete_media(
    slug: str, media_id: str,
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await require_enabled_user(session, user)
    store = await get_store_for_user(session, slug, user)
    try:
        mid = uuid.UUID(media_id)
    except ValueError:
        raise HTTPException(404, "Media not found")
    m = await session.get(StorefrontMediaModel, mid)
    if m is None or m.store_id != store.id:
        raise HTTPException(404, "Media not found")
    try:
        from case_service.storage.factory import get_storage_backend
        await get_storage_backend().delete(m.media_path)
    except Exception:
        pass  # object may already be gone; remove the row regardless
    await session.delete(m)
    await session.commit()
    return {"deleted": media_id}
