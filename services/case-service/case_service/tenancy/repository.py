"""Tenant CRUD operations.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select, func, update, delete
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import TenantModel, TenantMembershipModel


async def create_tenant(
    session: AsyncSession, *, data: dict[str, Any]
) -> TenantModel:
    model = TenantModel(**data)
    session.add(model)
    await session.flush()
    return model


async def get_tenant(
    session: AsyncSession, tenant_id: uuid.UUID
) -> TenantModel | None:
    stmt = select(TenantModel).where(TenantModel.id == tenant_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_tenant_by_slug(
    session: AsyncSession, slug: str
) -> TenantModel | None:
    stmt = select(TenantModel).where(TenantModel.slug == slug)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_tenants(
    session: AsyncSession,
    *,
    status: str | None = None,
) -> list[TenantModel]:
    stmt = select(TenantModel).order_by(TenantModel.name)
    if status:
        stmt = stmt.where(TenantModel.status == status)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def update_tenant(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    values: dict[str, Any],
) -> bool:
    stmt = update(TenantModel).where(TenantModel.id == tenant_id).values(**values)
    result = await session.execute(stmt)
    return result.rowcount > 0


async def delete_tenant(
    session: AsyncSession, tenant_id: uuid.UUID
) -> bool:
    """Soft delete by setting status=archived."""
    stmt = update(TenantModel).where(TenantModel.id == tenant_id).values(status="archived")
    result = await session.execute(stmt)
    return result.rowcount > 0


# ─── Memberships ────────────────────────────────────────────────

async def add_membership(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: str,
    role: str = "member",
) -> TenantMembershipModel:
    model = TenantMembershipModel(
        tenant_id=tenant_id, user_id=user_id, role=role,
    )
    session.add(model)
    await session.flush()
    return model


async def list_memberships_for_user(
    session: AsyncSession, user_id: str
) -> list[TenantMembershipModel]:
    stmt = select(TenantMembershipModel).where(
        TenantMembershipModel.user_id == user_id
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def list_members_of_tenant(
    session: AsyncSession, tenant_id: uuid.UUID
) -> list[TenantMembershipModel]:
    stmt = select(TenantMembershipModel).where(
        TenantMembershipModel.tenant_id == tenant_id
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def remove_membership(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: str,
) -> bool:
    stmt = delete(TenantMembershipModel).where(
        TenantMembershipModel.tenant_id == tenant_id,
        TenantMembershipModel.user_id == user_id,
    )
    result = await session.execute(stmt)
    return result.rowcount > 0


async def get_user_role_in_tenant(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: str,
) -> str | None:
    stmt = select(TenantMembershipModel.role).where(
        TenantMembershipModel.tenant_id == tenant_id,
        TenantMembershipModel.user_id == user_id,
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()
