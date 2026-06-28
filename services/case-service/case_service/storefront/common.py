"""HxStorefront shared gate + store-resolution helpers.

The marketplace-install gate mirrors HxCheckout's: a plain SELECT (no SAVEPOINT —
that pattern misreads committed rows once the request has already done a read), and
we 404 immediately on a missing table so an aborted statement can't poison anything.

Two entry points need the gate:
  * admin (Studio JWT)      → tenant from the user
  * public (/public/{slug}) → tenant from the store slug
"""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.auth.models import AuthenticatedUser
from case_service.db.models import MarketplaceInstallModel, StorefrontStoreModel

# 4-place id contract: must match official_registry.json, manifest.json, velaris.json.
HXSTOREFRONT_PACKAGE_ID = "velaris/hxstorefront"


async def require_installed(session: AsyncSession, tenant: str) -> None:
    """HxStorefront is available iff its package is installed (non-revoked) for `tenant`.
    Plain SELECT + immediate 404 (see module docstring for why no SAVEPOINT)."""
    try:
        row = (await session.execute(
            select(MarketplaceInstallModel.id).where(
                MarketplaceInstallModel.tenant_id == tenant,
                MarketplaceInstallModel.package_id == HXSTOREFRONT_PACKAGE_ID,
                MarketplaceInstallModel.revoked_at.is_(None),
            ).limit(1))).first()
    except (ProgrammingError, OperationalError):
        row = None                       # marketplace not yet activated → table absent
    if row is None:
        raise HTTPException(404, "HxStorefront is not installed on this instance")


async def require_enabled_user(session: AsyncSession, user: AuthenticatedUser) -> None:
    """Gate for staff-JWT (Studio) endpoints — tenant from the user."""
    await require_installed(session, user.tenant_id or "default")


async def get_store_for_user(session: AsyncSession, store_slug: str, user: AuthenticatedUser) -> StorefrontStoreModel:
    """Resolve a store by slug, scoped to the acting user's tenant (admin path)."""
    tenant = user.tenant_id or "default"
    store = (await session.execute(
        select(StorefrontStoreModel).where(
            StorefrontStoreModel.slug == store_slug,
            StorefrontStoreModel.tenant_id == tenant,
        ).limit(1))).scalar_one_or_none()
    if store is None:
        raise HTTPException(404, "Store not found")
    return store


async def get_public_store(session: AsyncSession, store_slug: str) -> StorefrontStoreModel:
    """Resolve an active store by slug for the public storefront, and gate on the
    owning tenant's install. No auth — the slug is public."""
    store = (await session.execute(
        select(StorefrontStoreModel).where(
            StorefrontStoreModel.slug == store_slug,
            StorefrontStoreModel.status == "active",
        ).limit(1))).scalar_one_or_none()
    if store is None:
        raise HTTPException(404, "Store not found")
    await require_installed(session, store.tenant_id)
    return store
