"""Per-tenant limits — `tenants.max_users` / `tenants.max_cases`.

Both columns existed since Phase 17 and nothing read them. They are enforced
from 2026-11-04 (demo tenants, Nov_TODO §0 "SECURITY"). A NULL limit is
unlimited, and every tenant that existed before had NULL, so nothing changes
for them.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession


class TenantLimitError(Exception):
    """The tenant is at its limit. Routes answer 403 with the message."""


async def _tenant(session: AsyncSession, token: Any):
    from velaris_shared.db.models import TenantModel
    from velaris_shared.tenancy.identity import resolve_tenant

    if token is None or not str(token).strip():
        return None
    hit = await resolve_tenant(session, token)
    if hit is None:
        return None
    return await session.get(TenantModel, hit[0])


async def require_user_room(session: AsyncSession, token: Any) -> None:
    """Refuse a new account in a tenant already holding `max_users`."""
    from velaris_shared.db.models import UserDirectoryModel

    t = await _tenant(session, token)
    if t is None or t.max_users is None:
        return
    n = (await session.execute(select(func.count()).select_from(UserDirectoryModel).where(
        or_(UserDirectoryModel.tenant_id == t.slug,
            UserDirectoryModel.tenant_id == str(t.id))))).scalar_one()
    if n >= t.max_users:
        raise TenantLimitError(
            f"VEL-TENANT-LIMIT-USERS: this tenant holds its limit of {t.max_users} users.")


async def require_case_room(session: AsyncSession, token: Any) -> None:
    """Refuse a new case in a tenant already holding `max_cases`."""
    from velaris_shared.db.models import CaseInstanceModel

    t = await _tenant(session, token)
    if t is None or t.max_cases is None:
        return
    n = (await session.execute(select(func.count()).select_from(CaseInstanceModel).where(
        CaseInstanceModel.tenant_id == t.id))).scalar_one()
    if n >= t.max_cases:
        raise TenantLimitError(
            f"VEL-TENANT-LIMIT-CASES: this tenant holds its limit of {t.max_cases} cases.")
