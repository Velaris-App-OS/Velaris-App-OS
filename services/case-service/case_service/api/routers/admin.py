"""Admin API router.

System-wide audit log search, queue management, webhook overview,
and rule management endpoints for the admin console.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from velaris_shared.auth.dependencies import get_current_user
from velaris_shared.auth.models import AuthenticatedUser
from pydantic import BaseModel, Field
from sqlalchemy import select, func, delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import (
    CaseAuditLogModel,
    CaseInstanceModel,
    WorkQueueModel,
    RuleDefinitionModel,
    WebhookSubscriptionModel,
    WebhookDeliveryModel,
    BusinessCalendarModel,
)
from velaris_shared.auth.dependencies import require_role
from velaris_shared.db.session import get_session

from velaris_shared import hxguard
from velaris_shared.db.read_routing import get_read_session

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(hxguard.guard("platform.admin.access"))])


# ─── Schemas ──────────────────────────────────────────────────────

class AuditSearchResponse(BaseModel):
    items: list[dict[str, Any]]
    total: int
    page: int
    page_size: int


class SystemInfo(BaseModel):
    case_types: int
    cases: int
    assignments: int
    queues: int
    rules: int
    forms: int
    webhooks: int
    calendars: int
    audit_entries: int
    # Counts for the console's later tabs — Permissions, SSO, Identity and
    # Marketplace Sources all had a tab and no line on the overview.
    users: int = 0
    sso_providers: int = 0
    marketplace_sources: int = 0
    configured_permissions: int = 0


class QueueCreateUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    filter_criteria: dict[str, Any] | None = None
    sort_fields: list[str] | None = None
    sort_ascending: bool | None = None
    visible_to_roles: list[str] | None = None
    auto_assignment: bool | None = None
    max_items: int | None = None


# ─── System overview ─────────────────────────────────────────────

@router.get("/system-info", response_model=SystemInfo)
async def get_system_info(
    session: AsyncSession = Depends(get_session),
):
    """System-wide entity counts."""
    from velaris_shared.db.models import (
        CaseTypeModel, CaseAssignmentModel,
        FormDefinitionModel, DataModelModel,
        HelixUserModel, SsoProviderModel, MarketplaceSourceModel,
        HelixSettingModel,
    )

    async def _count(model):
        return (await session.execute(
            select(func.count()).select_from(model)
        )).scalar_one()

    # The permission matrix is one settings row holding a path -> roles map,
    # not a table, so "configured" means how many routes the admin has
    # explicitly decided about. An absent row means none.
    perms = (await session.execute(
        select(HelixSettingModel.value).where(
            HelixSettingModel.key == "route_permissions"
        )
    )).scalar_one_or_none()

    return SystemInfo(
        case_types=await _count(CaseTypeModel),
        cases=await _count(CaseInstanceModel),
        assignments=await _count(CaseAssignmentModel),
        queues=await _count(WorkQueueModel),
        rules=await _count(RuleDefinitionModel),
        forms=await _count(FormDefinitionModel),
        webhooks=await _count(WebhookSubscriptionModel),
        calendars=await _count(BusinessCalendarModel),
        audit_entries=await _count(CaseAuditLogModel),
        users=await _count(HelixUserModel),
        sso_providers=await _count(SsoProviderModel),
        marketplace_sources=await _count(MarketplaceSourceModel),
        configured_permissions=len(perms) if isinstance(perms, dict) else 0,
    )


# ─── Audit log search ───────────────────────────────────────────

@router.get("/audit-log", response_model=AuditSearchResponse)
async def search_audit_log(
    action: str | None = None,
    actor_id: str | None = None,
    case_id: str | None = None,
    days: int = Query(7, ge=1, le=365),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_read_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Search audit log across all cases with filters."""
    since = datetime.now(timezone.utc) - timedelta(days=days)

    base = select(CaseAuditLogModel).where(
        CaseAuditLogModel.timestamp >= since
    )
    count_base = select(func.count()).select_from(CaseAuditLogModel).where(
        CaseAuditLogModel.timestamp >= since
    )

    if action:
        base = base.where(CaseAuditLogModel.action == action)
        count_base = count_base.where(CaseAuditLogModel.action == action)
    if actor_id:
        base = base.where(CaseAuditLogModel.actor_id == actor_id)
        count_base = count_base.where(CaseAuditLogModel.actor_id == actor_id)
    if case_id:
        base = base.where(CaseAuditLogModel.case_id == uuid.UUID(case_id))
        count_base = count_base.where(CaseAuditLogModel.case_id == uuid.UUID(case_id))

    # Every tenant's rows until 2026-09-29 (8d).
    from case_service.tenancy.identity import scope_to_caller
    from velaris_shared.db.models import CaseInstanceModel
    _via = (CaseAuditLogModel.case_id, CaseInstanceModel)
    base = await scope_to_caller(base, CaseAuditLogModel, session, user, via=_via)
    count_base = await scope_to_caller(count_base, CaseAuditLogModel, session, user, via=_via)
    total = (await session.execute(count_base)).scalar_one()

    stmt = (
        base.order_by(CaseAuditLogModel.timestamp.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await session.execute(stmt)

    items = []
    for row in result.scalars().all():
        items.append({
            "id": str(row.id),
            "case_id": str(row.case_id),
            "action": row.action,
            "actor_id": row.actor_id,
            "actor_type": row.actor_type,
            "timestamp": row.timestamp.isoformat() if row.timestamp else None,
            "details": row.details,
        })

    return AuditSearchResponse(
        items=items, total=total, page=page, page_size=page_size
    )


@router.get("/audit-log/actions")
async def list_audit_actions(
    session: AsyncSession = Depends(get_read_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """List distinct audit action types."""
    stmt = select(CaseAuditLogModel.action).distinct().order_by(CaseAuditLogModel.action)
    # Every tenant's until 2026-09-29 (8d).
    from case_service.tenancy.identity import scope_to_caller
    from velaris_shared.db.models import CaseInstanceModel
    stmt = await scope_to_caller(stmt, CaseAuditLogModel, session, user,
                                 via=(CaseAuditLogModel.case_id, CaseInstanceModel))
    result = await session.execute(stmt)
    return {"actions": [r[0] for r in result.all()]}


# ─── Queue admin ─────────────────────────────────────────────────

@router.post("/queues", status_code=201)
async def create_queue(
    body: QueueCreateUpdate,
    session: AsyncSession = Depends(get_session),
):
    model = WorkQueueModel(
        name=body.name or "New Queue",
        description=body.description or "",
        filter_criteria=body.filter_criteria or {},
        sort_fields=body.sort_fields or ["urgency"],
        sort_ascending=body.sort_ascending if body.sort_ascending is not None else True,
        visible_to_roles=body.visible_to_roles or [],
        auto_assignment=body.auto_assignment or False,
        max_items=body.max_items,
    )
    session.add(model)
    await session.flush()
    return _queue_dict(model)


@router.patch("/queues/{queue_id}")
async def update_queue(
    queue_id: uuid.UUID,
    body: QueueCreateUpdate,
    session: AsyncSession = Depends(get_session),
):
    existing = await session.get(WorkQueueModel, queue_id)
    if not existing:
        raise HTTPException(404, "Queue not found")
    values = {k: v for k, v in body.model_dump(exclude_unset=True).items()}
    if values:
        stmt = update(WorkQueueModel).where(WorkQueueModel.id == queue_id).values(**values)
        await session.execute(stmt)
    result = await session.get(WorkQueueModel, queue_id)
    return _queue_dict(result)


@router.delete("/queues/{queue_id}", status_code=204)
async def delete_queue(
    queue_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    existing = await session.get(WorkQueueModel, queue_id)
    if not existing:
        raise HTTPException(404, "Queue not found")
    stmt = delete(WorkQueueModel).where(WorkQueueModel.id == queue_id)
    await session.execute(stmt)


def _queue_dict(m):
    return {
        "id": str(m.id), "name": m.name, "description": m.description,
        "filter_criteria": m.filter_criteria, "sort_fields": m.sort_fields,
        "sort_ascending": m.sort_ascending, "visible_to_roles": m.visible_to_roles,
        "auto_assignment": m.auto_assignment, "max_items": m.max_items,
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "updated_at": m.updated_at.isoformat() if m.updated_at else None,
    }


# ─── Calendar admin ─────────────────────────────────────────────

async def _inherit(awaitable):
    """Run an `inherit` step (8e), answering its refusal as HTTP."""
    from velaris_shared.tenancy import inherit
    try:
        return await awaitable
    except inherit.InheritError as exc:
        raise HTTPException(exc.status, str(exc))


@router.get("/calendars")
async def list_calendars(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    from velaris_shared.tenancy import inherit
    stmt = select(BusinessCalendarModel).order_by(BusinessCalendarModel.name)
    stmt = await inherit.scope_list(stmt, BusinessCalendarModel, session, user)   # 8e
    cals = list((await session.execute(stmt)).scalars().all())
    stale = await inherit.updates_available(session, BusinessCalendarModel, cals)   # 8k
    return [
        {
            "id": str(c.id), "name": c.name, "timezone": c.timezone,
            "work_days": c.work_days, "work_start_hour": c.work_start_hour,
            "work_end_hour": c.work_end_hour, "holidays": c.holidays,
            "tenant_id": str(c.tenant_id) if c.tenant_id else None,
            "forked_from_id": str(c.forked_from_id) if c.forked_from_id else None,
            "update_available": c.id in stale,
        }
        for c in cals
    ]


class CalendarCreateUpdate(BaseModel):
    name: str | None = None
    timezone: str = "UTC"
    work_days: list[int] = [1, 2, 3, 4, 5]
    work_start_hour: int = 9
    work_end_hour: int = 17
    description: str = ""
    holidays: list[str] = []


@router.post("/calendars", status_code=201)
async def create_calendar(
    body: CalendarCreateUpdate,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("platform.calendars.write")),
):
    # 8e: a tenant's calendar is its own; only the operator makes a default.
    from velaris_shared.tenancy import inherit
    owner = await _inherit(inherit.owner_for_create(session, user))
    if await inherit.name_taken(session, BusinessCalendarModel, owner,
                                name=body.name or "New Calendar"):
        raise HTTPException(409, f"A calendar named '{body.name or 'New Calendar'}' already exists")
    cal = BusinessCalendarModel(
        tenant_id=owner,
        name=body.name or "New Calendar",
        timezone=body.timezone,
        work_days=body.work_days,
        work_start_hour=body.work_start_hour,
        work_end_hour=body.work_end_hour,
        description=body.description,
        holidays=body.holidays,
    )
    session.add(cal)
    await session.commit()
    await session.refresh(cal)
    return {
        "id": str(cal.id), "name": cal.name, "timezone": cal.timezone,
        "work_days": cal.work_days, "work_start_hour": cal.work_start_hour,
        "work_end_hour": cal.work_end_hour, "holidays": cal.holidays,
        "description": cal.description,
    }


@router.patch("/calendars/{calendar_id}")
async def update_calendar(
    calendar_id: uuid.UUID,
    body: CalendarCreateUpdate,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("platform.calendars.write")),
):
    cal = await session.get(BusinessCalendarModel, calendar_id)
    if not cal:
        raise HTTPException(404, "Calendar not found")
    # 8e: a tenant's edit of a default calendar lands in its fork.
    from velaris_shared.tenancy import inherit
    cal = await _inherit(inherit.target_for_write(session, user, BusinessCalendarModel, cal))
    for field, val in body.model_dump(exclude_unset=True).items():
        setattr(cal, field, val)
    await session.commit()
    await session.refresh(cal)
    return {
        "id": str(cal.id), "name": cal.name, "timezone": cal.timezone,
        "work_days": cal.work_days, "work_start_hour": cal.work_start_hour,
        "work_end_hour": cal.work_end_hour, "holidays": cal.holidays,
        "description": cal.description,
    }
