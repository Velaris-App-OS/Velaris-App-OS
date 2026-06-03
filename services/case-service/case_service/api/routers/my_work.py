"""User-context API router (/my/*).

Provides the current user's assignments, queues, and workload.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from typing import Optional
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.api.schemas.cases import AssignmentResponse
from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser
from case_service.db import repository as repo
from case_service.db.session import get_session

router = APIRouter(prefix="/my", tags=["my-work"])


@router.get("/assignments", response_model=list[AssignmentResponse])
async def my_assignments(
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Return all active assignments for the current user."""
    return await repo.get_assignments_for_user(session, str(user.user_id))


@router.get("/workload")
async def my_workload(
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Workload summary: count of active items."""
    items = await repo.get_assignments_for_user(session, str(user.user_id))
    return {
        "user_id": str(user.user_id),
        "active_count": len(items),
        "assignment_ids": [str(a.id) for a in items],
    }


@router.post("/self-assign", status_code=201)
async def self_assign(
    case_id: Optional[str] = Query(None, description="Attach to a specific case UUID (leave blank to use first open case)"),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Create a test assignment for the current user. Used for Work Center testing."""
    from datetime import datetime, timezone
    from sqlalchemy import select
    from case_service.db.models import CaseAssignmentModel, CaseInstanceModel

    # Resolve a real case to attach to (FK constraint — must exist)
    resolved_case_id: uuid.UUID | None = None
    if case_id:
        try:
            resolved_case_id = uuid.UUID(case_id)
        except ValueError:
            pass

    if not resolved_case_id:
        # Pick any existing case
        result = await session.execute(
            select(CaseInstanceModel)
            .where(CaseInstanceModel.status.in_(["open", "new", "in_progress"]))
            .limit(1)
        )
        row = result.scalar_one_or_none()
        if row:
            resolved_case_id = row.id

    if not resolved_case_id:
        # No open cases — pick absolutely any case
        result = await session.execute(select(CaseInstanceModel).limit(1))
        row = result.scalar_one_or_none()
        if row:
            resolved_case_id = row.id

    if not resolved_case_id:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=422,
            detail="No cases exist yet. Create a case first, then self-assign.",
        )

    assignment = CaseAssignmentModel(
        case_id=resolved_case_id,
        step_id=f"test-step-{datetime.now(timezone.utc).strftime('%H%M%S')}",
        assignee_type="user",
        assignee_id=str(user.user_id),
        status="active",
        assigned_at=datetime.now(timezone.utc),
    )

    session.add(assignment)
    await session.commit()
    await session.refresh(assignment)
    return {
        "id": str(assignment.id),
        "case_id": str(assignment.case_id),
        "step_id": assignment.step_id,
        "assignee_id": assignment.assignee_id,
        "status": assignment.status,
        "assigned_at": assignment.assigned_at.isoformat(),
    }
