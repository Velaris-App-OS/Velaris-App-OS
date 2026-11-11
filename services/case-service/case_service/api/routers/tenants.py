"""Tenants API router.

CRUD for tenants + membership management.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from velaris_shared.auth.dependencies import get_current_user
from velaris_shared.auth.models import AuthenticatedUser
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.auth.dependencies import require_role
from velaris_shared import hxguard
from velaris_shared.db.session import get_session
from case_service.tenancy import repository as tenant_repo

router = APIRouter(prefix="/tenants", tags=["tenants"], dependencies=[Depends(hxguard.guard("platform.tenants.access"))])


class TenantCreate(BaseModel):
    slug: str = Field(..., min_length=2, max_length=100)
    name: str = Field(..., min_length=1, max_length=255)
    description: str = ""
    max_cases: int | None = None
    max_users: int | None = None


class TenantUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    status: str | None = None
    max_cases: int | None = None
    max_users: int | None = None
    settings: dict[str, Any] | None = None


class TenantResponse(BaseModel):
    id: str
    slug: str
    name: str
    description: str
    status: str
    settings: dict[str, Any]
    max_cases: int | None
    max_users: int | None
    created_at: datetime
    updated_at: datetime


class MembershipCreate(BaseModel):
    user_id: str
    role: str = "member"


class MembershipResponse(BaseModel):
    id: str
    tenant_id: str
    user_id: str
    role: str
    created_at: datetime


SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$")


async def _platform_operator_only(session: AsyncSession, user: AuthenticatedUser) -> None:
    """Creating, changing, archiving and deleting tenants is the install's
    operator's (superadmin, or the default tenant) — 2026-10-30. The router's
    privilege is held by every tenant's `admin`, and nothing else was asked: a
    tenant admin could create tenants, raise their own `max_cases`/`max_users`,
    and archive or permanently delete their own tenant with all its data. A
    tenant admin still reads their own tenant and manages its members."""
    from case_service.tenancy.identity import is_platform_operator
    if not await is_platform_operator(session, user):
        raise HTTPException(403, "Only the platform operator can create, change or delete tenants.")


@router.post("", response_model=TenantResponse, status_code=201)
async def create_tenant(
    body: TenantCreate,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Create a new tenant."""
    await _platform_operator_only(session, user)
    if not SLUG_RE.match(body.slug):
        raise HTTPException(400, "Slug must be lowercase alphanumeric with optional hyphens")

    # Check uniqueness
    existing = await tenant_repo.get_tenant_by_slug(session, body.slug)
    if existing:
        raise HTTPException(409, f"Tenant with slug '{body.slug}' already exists")

    tenant = await tenant_repo.create_tenant(session, data={
        "slug": body.slug,
        "name": body.name,
        "description": body.description,
        "max_cases": body.max_cases,
        "max_users": body.max_users,
    })
    await session.commit()
    return _to_response(tenant)


@router.get("", response_model=list[TenantResponse])
async def list_tenants(
    status: str | None = None,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    tenants = await tenant_repo.list_tenants(session, status=status)
    # 8d (2026-09-29): every admin, of any tenant, listed every tenant. A
    # tenant's admin sees their own; the platform operator sees all.
    from case_service.tenancy.identity import is_platform_operator, same_tenant

    if not await is_platform_operator(session, user):
        tenants = [t for t in tenants
                   if await same_tenant(session, t.id, getattr(user, "tenant_id", None))]
    return [_to_response(t) for t in tenants]


@router.get("/{tenant_id}", response_model=TenantResponse)
async def get_tenant(
    tenant_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    tenant = await tenant_repo.get_tenant(session, tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    return _to_response(tenant)


@router.get("/by-slug/{slug}", response_model=TenantResponse)
async def get_tenant_by_slug(
    slug: str,
    session: AsyncSession = Depends(get_session),
):
    tenant = await tenant_repo.get_tenant_by_slug(session, slug)
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    return _to_response(tenant)


@router.patch("/{tenant_id}", response_model=TenantResponse)
async def update_tenant(
    tenant_id: uuid.UUID,
    body: TenantUpdate,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    await _platform_operator_only(session, user)
    tenant = await tenant_repo.get_tenant(session, tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")

    values = {k: v for k, v in body.model_dump(exclude_unset=True).items()}
    if values:
        await tenant_repo.update_tenant(session, tenant_id, values=values)
        await session.commit()

    tenant = await tenant_repo.get_tenant(session, tenant_id)
    return _to_response(tenant)


@router.delete("/{tenant_id}", status_code=204)
async def delete_tenant(
    tenant_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Soft-delete by archiving."""
    await _platform_operator_only(session, user)
    tenant = await tenant_repo.get_tenant(session, tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    if tenant.slug == "default":
        raise HTTPException(400, "Cannot delete default tenant")

    await tenant_repo.delete_tenant(session, tenant_id)
    await session.commit()


@router.delete("/{tenant_id}/permanent", status_code=204)
async def permanently_delete_tenant(
    tenant_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Hard delete — permanently removes the tenant and all its data."""
    await _platform_operator_only(session, user)
    tenant = await tenant_repo.get_tenant(session, tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    if tenant.slug == "default":
        raise HTTPException(400, "Cannot delete default tenant")
    # Its cases go with it — not while one is under a legal hold (Oct item 13).
    from case_service.legal_hold import service as _legal_hold
    await _legal_hold.require_tenant_free(session, tenant.id, "permanently deleted")
    # its cases, case types, forms… first: their foreign keys to `tenants` do not
    # cascade, so a tenant with cases could not be deleted at all (2026-11-10)
    from case_service.compliance.erasure import erase_tenant_data
    await erase_tenant_data(session, tenant.id, actor=f"user:{getattr(user, 'user_id', '')}")
    await session.delete(tenant)
    await session.commit()


# ─── Memberships ─────────────────────────────────────────────────

@router.post("/{tenant_id}/members", response_model=MembershipResponse, status_code=201)
async def add_member(
    tenant_id: uuid.UUID,
    body: MembershipCreate,
    session: AsyncSession = Depends(get_session),
):
    tenant = await tenant_repo.get_tenant(session, tenant_id)
    if not tenant:
        raise HTTPException(404, "Tenant not found")

    if body.role not in ("owner", "admin", "member", "viewer"):
        raise HTTPException(400, "Invalid role")

    try:
        m = await tenant_repo.add_membership(
            session, tenant_id=tenant_id,
            user_id=body.user_id, role=body.role,
        )
        await session.commit()
    except Exception:
        await session.rollback()
        raise HTTPException(409, "User already a member of this tenant")

    return MembershipResponse(
        id=str(m.id), tenant_id=str(m.tenant_id),
        user_id=m.user_id, role=m.role,
        created_at=m.created_at,
    )


@router.get("/{tenant_id}/members", response_model=list[MembershipResponse])
async def list_members(
    tenant_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    members = await tenant_repo.list_members_of_tenant(session, tenant_id)
    return [
        MembershipResponse(
            id=str(m.id), tenant_id=str(m.tenant_id),
            user_id=m.user_id, role=m.role, created_at=m.created_at,
        )
        for m in members
    ]


@router.patch("/{tenant_id}/members/{user_id}", response_model=MembershipResponse)
async def update_member(
    tenant_id: uuid.UUID,
    user_id: str,
    body: MembershipCreate,
    session: AsyncSession = Depends(get_session),
):
    from sqlalchemy import select
    from velaris_shared.db.models import TenantMembershipModel
    if body.role not in ("owner", "admin", "member", "viewer"):
        raise HTTPException(400, "Invalid role")
    result = await session.execute(
        select(TenantMembershipModel)
        .where(TenantMembershipModel.tenant_id == tenant_id)
        .where(TenantMembershipModel.user_id == user_id)
    )
    m = result.scalar_one_or_none()
    if not m:
        raise HTTPException(404, "Membership not found")
    m.role = body.role
    await session.commit()
    await session.refresh(m)
    return MembershipResponse(
        id=str(m.id), tenant_id=str(m.tenant_id),
        user_id=m.user_id, role=m.role, created_at=m.created_at,
    )


@router.delete("/{tenant_id}/members/{user_id}", status_code=204)
async def remove_member(
    tenant_id: uuid.UUID,
    user_id: str,
    session: AsyncSession = Depends(get_session),
):
    removed = await tenant_repo.remove_membership(
        session, tenant_id=tenant_id, user_id=user_id,
    )
    if not removed:
        raise HTTPException(404, "Membership not found")
    await session.commit()


# ─── Current user's tenants ─────────────────────────────────────

@router.get("/user/{user_id}/tenants", response_model=list[TenantResponse])
async def list_user_tenants(
    user_id: str,
    session: AsyncSession = Depends(get_session),
):
    """List all tenants a user is a member of."""
    memberships = await tenant_repo.list_memberships_for_user(session, user_id)
    tenants = []
    for m in memberships:
        t = await tenant_repo.get_tenant(session, m.tenant_id)
        if t and t.status == "active":
            tenants.append(_to_response(t))
    return tenants


def _to_response(t) -> TenantResponse:
    return TenantResponse(
        id=str(t.id), slug=t.slug, name=t.name,
        description=t.description, status=t.status,
        settings=t.settings or {},
        max_cases=t.max_cases, max_users=t.max_users,
        created_at=t.created_at, updated_at=t.updated_at,
    )
