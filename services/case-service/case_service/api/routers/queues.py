"""Work Queue API router.

Handles queue CRUD, item listing, and queue health stats.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.api.schemas.cases import (
    AssignmentResponse,
    QueueStatsResponse,
    WorkQueueCreate,
    WorkQueueResponse,
)
from case_service.auth.dependencies import get_current_user
from case_service.db import repository as repo
from case_service.db.models import CaseAssignmentModel, CaseSLAInstanceModel
from case_service.db.session import get_session

router = APIRouter(prefix="/queues", tags=["queues"], dependencies=[Depends(get_current_user)])


@router.post("", response_model=WorkQueueResponse, status_code=201)
async def create_queue(
    body: WorkQueueCreate,
    session: AsyncSession = Depends(get_session),
):
    queue = await repo.create_work_queue(
        session,
        data={
            "name": body.name,
            "description": body.description,
            "tenant_id": body.tenant_id,
            "filter_criteria": body.filter_criteria,
            "sort_fields": body.sort_fields,
            "sort_ascending": body.sort_ascending,
            "visible_to_roles": body.visible_to_roles,
            "auto_assignment": body.auto_assignment,
            "urgency_formula": body.urgency_formula,
            "max_items": body.max_items,
        },
    )
    return queue


@router.get("", response_model=list[WorkQueueResponse])
async def list_queues(
    tenant_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
):
    return await repo.list_work_queues(session, tenant_id=tenant_id)


@router.get("/{queue_id}", response_model=WorkQueueResponse)
async def get_queue(
    queue_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    queue = await repo.get_work_queue(session, queue_id)
    if queue is None:
        raise HTTPException(status_code=404, detail="Queue not found")
    return queue


@router.get(
    "/{queue_id}/items", response_model=list[AssignmentResponse]
)
async def get_queue_items(
    queue_id: uuid.UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    status: str | None = Query(None, description="Filter by status: active, claimed, completed"),
    search: str | None = Query(None, description="Search by assignee_id or step_id"),
    session: AsyncSession = Depends(get_session),
):
    """Return assignments matching this queue's filter criteria with optional status/search filters."""
    queue = await repo.get_work_queue(session, queue_id)
    if queue is None:
        raise HTTPException(status_code=404, detail="Queue not found")

    status_filter = status or "active"
    stmt = select(CaseAssignmentModel).where(
        CaseAssignmentModel.status == status_filter
    )

    for key, val in (queue.filter_criteria or {}).items():
        if hasattr(CaseAssignmentModel, key):
            stmt = stmt.where(getattr(CaseAssignmentModel, key) == val)

    if search:
        from sqlalchemy import or_
        pattern = f"%{search}%"
        stmt = stmt.where(
            or_(
                CaseAssignmentModel.assignee_id.ilike(pattern),
                CaseAssignmentModel.step_id.ilike(pattern),
                CaseAssignmentModel.assigned_by.ilike(pattern),
            )
        )

    stmt = (
        stmt.order_by(CaseAssignmentModel.assigned_at.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


@router.get("/{queue_id}/items/count")
async def get_queue_item_count(
    queue_id: uuid.UUID,
    status: str | None = Query(None),
    search: str | None = Query(None),
    session: AsyncSession = Depends(get_session),
):
    """Return total item count for pagination."""
    queue = await repo.get_work_queue(session, queue_id)
    if queue is None:
        raise HTTPException(status_code=404, detail="Queue not found")

    status_filter = status or "active"
    stmt = select(func.count()).select_from(CaseAssignmentModel).where(
        CaseAssignmentModel.status == status_filter
    )
    for key, val in (queue.filter_criteria or {}).items():
        if hasattr(CaseAssignmentModel, key):
            stmt = stmt.where(getattr(CaseAssignmentModel, key) == val)

    if search:
        from sqlalchemy import or_
        pattern = f"%{search}%"
        stmt = stmt.where(
            or_(
                CaseAssignmentModel.assignee_id.ilike(pattern),
                CaseAssignmentModel.step_id.ilike(pattern),
                CaseAssignmentModel.assigned_by.ilike(pattern),
            )
        )

    result = await session.execute(stmt)
    return {"count": result.scalar_one()}


@router.get("/{queue_id}/stats", response_model=QueueStatsResponse)
async def get_queue_stats(
    queue_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    """Queue health: counts and SLA distribution."""
    queue = await repo.get_work_queue(session, queue_id)
    if queue is None:
        raise HTTPException(status_code=404, detail="Queue not found")

    # Total and active assignments matching queue filters
    base = select(CaseAssignmentModel)
    for key, val in (queue.filter_criteria or {}).items():
        if hasattr(CaseAssignmentModel, key):
            base = base.where(
                getattr(CaseAssignmentModel, key) == val
            )

    total_stmt = select(func.count()).select_from(base.subquery())
    total = (await session.execute(total_stmt)).scalar_one()

    active_base = base.where(CaseAssignmentModel.status == "active")
    active_stmt = select(func.count()).select_from(
        active_base.subquery()
    )
    active = (await session.execute(active_stmt)).scalar_one()

    # SLA distribution — simplified: count SLA instances across all
    # cases that have active assignments in this queue's scope.
    sla_counts = {"on_track": 0, "at_risk": 0, "breached": 0}
    sla_stmt = (
        select(
            CaseSLAInstanceModel.status,
            func.count().label("cnt"),
        )
        .where(
            CaseSLAInstanceModel.status.in_(
                ["on_track", "at_risk", "breached"]
            )
        )
        .group_by(CaseSLAInstanceModel.status)
    )
    sla_result = await session.execute(sla_stmt)
    for row in sla_result:
        if row.status in sla_counts:
            sla_counts[row.status] = row.cnt

    return QueueStatsResponse(
        queue_id=queue_id,
        total_items=total,
        active_items=active,
        avg_wait_seconds=None,  # TODO: compute from assigned_at deltas
        sla_on_track=sla_counts["on_track"],
        sla_at_risk=sla_counts["at_risk"],
        sla_breached=sla_counts["breached"],
    )
