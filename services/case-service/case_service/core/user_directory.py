"""UserDirectory — resolves dynamic targets for escalation actions.

This module never raises on lookup-miss; returns [] so escalations degrade
gracefully instead of failing. Log lines flag missing data.
"""
from __future__ import annotations
import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import (
    UserDirectoryModel, CaseAssignmentModel,
)

log = logging.getLogger(__name__)


async def get_user(session: AsyncSession, user_id: str) -> Optional[UserDirectoryModel]:
    q = select(UserDirectoryModel).where(
        UserDirectoryModel.user_id == user_id,
        UserDirectoryModel.is_active.is_(True),
    )
    res = await session.execute(q)
    return res.scalar_one_or_none()


async def get_manager(session: AsyncSession, user_id: str) -> Optional[str]:
    """Return the manager's user_id, or None if no manager / user not found."""
    user = await get_user(session, user_id)
    if user is None:
        log.warning("user_directory miss: user_id=%s not found", user_id)
        return None
    return user.manager_user_id


async def get_current_assignee_for_case(
    session: AsyncSession, case_id,
) -> Optional[str]:
    """Return the current (most recent) active assignee for a case."""
    q = (
        select(CaseAssignmentModel)
        .where(CaseAssignmentModel.case_id == case_id)
        .where(CaseAssignmentModel.status == "active")
        .order_by(CaseAssignmentModel.assigned_at.desc())
        .limit(1)
    )
    res = await session.execute(q)
    a = res.scalar_one_or_none()
    if a is None or a.assignee_type != "user":
        return None
    return a.assignee_id


async def users_in_access_group(
    session: AsyncSession, group_id: str, tenant_id: Optional[str] = None,
) -> list[str]:
    """Return user_ids of active users belonging to the given access group."""
    q = select(UserDirectoryModel).where(
        UserDirectoryModel.is_active.is_(True),
    )
    if tenant_id:
        q = q.where(UserDirectoryModel.tenant_id == tenant_id)
    res = await session.execute(q)
    out = []
    for u in res.scalars().all():
        groups = u.access_group_ids or []
        if group_id in groups:
            out.append(u.user_id)
    return out


async def users_with_role(
    session: AsyncSession, role: str, tenant_id: Optional[str] = None,
) -> list[str]:
    q = select(UserDirectoryModel).where(
        UserDirectoryModel.is_active.is_(True),
    )
    if tenant_id:
        q = q.where(UserDirectoryModel.tenant_id == tenant_id)
    res = await session.execute(q)
    out = []
    for u in res.scalars().all():
        roles = u.roles or []
        if role in roles:
            out.append(u.user_id)
    return out
