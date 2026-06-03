"""Assignment API router.

Handles claiming, releasing, reassigning, and completing work items.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.api.schemas.cases import (
    AssignmentClaim,
    AssignmentComplete,
    AssignmentReassign,
    AssignmentResponse,
)
from case_service.db import repository as repo
from case_service.auth.dependencies import get_current_user
from case_service.db.session import get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/assignments", tags=["assignments"], dependencies=[Depends(get_current_user)])


async def _get_assignment_or_404(
    session: AsyncSession, assignment_id: uuid.UUID
):
    assignment = await repo.get_assignment(session, assignment_id)
    if assignment is None:
        raise HTTPException(
            status_code=404, detail="Assignment not found"
        )
    return assignment


@router.post("/{assignment_id}/claim", response_model=AssignmentResponse)
async def claim_assignment(
    assignment_id: uuid.UUID,
    body: AssignmentClaim,
    session: AsyncSession = Depends(get_session),
):
    """Self-service: user claims an item from a queue."""
    assignment = await _get_assignment_or_404(session, assignment_id)
    if assignment.status != "active":
        raise HTTPException(
            status_code=409, detail="Assignment is not claimable"
        )
    if assignment.claimed_at is not None:
        raise HTTPException(
            status_code=409, detail="Assignment already claimed"
        )

    now = datetime.now(timezone.utc)
    await repo.update_assignment(
        session,
        assignment_id,
        values={
            "assignee_id": body.user_id,
            "assignee_type": "user",
            "claimed_at": now,
        },
    )

    await repo.append_audit_entry(
        session,
        data={
            "case_id": assignment.case_id,
            "action": "assignment_claimed",
            "actor_id": body.user_id,
            "details": {
                "assignment_id": str(assignment_id),
                "step_id": assignment.step_id,
            },
        },
    )

    return await repo.get_assignment(session, assignment_id)


@router.post(
    "/{assignment_id}/release", response_model=AssignmentResponse
)
async def release_assignment(
    assignment_id: uuid.UUID,
    body: AssignmentClaim,
    session: AsyncSession = Depends(get_session),
):
    """Return an item back to its queue (unclaim)."""
    assignment = await _get_assignment_or_404(session, assignment_id)
    if assignment.assignee_id != body.user_id:
        raise HTTPException(
            status_code=403,
            detail="Only the current assignee can release",
        )

    await repo.update_assignment(
        session,
        assignment_id,
        values={
            "assignee_type": "queue",
            "claimed_at": None,
        },
    )

    await repo.append_audit_entry(
        session,
        data={
            "case_id": assignment.case_id,
            "action": "assignment_released",
            "actor_id": body.user_id,
            "details": {"assignment_id": str(assignment_id)},
        },
    )

    return await repo.get_assignment(session, assignment_id)


@router.post(
    "/{assignment_id}/reassign", response_model=AssignmentResponse
)
async def reassign_assignment(
    assignment_id: uuid.UUID,
    body: AssignmentReassign,
    session: AsyncSession = Depends(get_session),
):
    """Supervisor or rule reassigns an item to a different user."""
    assignment = await _get_assignment_or_404(session, assignment_id)
    prev_assignee = assignment.assignee_id

    await repo.update_assignment(
        session,
        assignment_id,
        values={
            "assignee_id": body.new_assignee_id,
            "assignee_type": "user",
            "claimed_at": datetime.now(timezone.utc),
        },
    )

    await repo.append_audit_entry(
        session,
        data={
            "case_id": assignment.case_id,
            "action": "assignment_reassigned",
            "details": {
                "assignment_id": str(assignment_id),
                "from": prev_assignee,
                "to": body.new_assignee_id,
                "reason": body.reason,
            },
        },
    )

    return await repo.get_assignment(session, assignment_id)


@router.post(
    "/{assignment_id}/complete", response_model=AssignmentResponse
)
async def complete_assignment(
    assignment_id: uuid.UUID,
    body: AssignmentComplete,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Mark a work item as completed and signal the lifecycle workflow."""
    assignment = await _get_assignment_or_404(session, assignment_id)
    if assignment.status != "active":
        raise HTTPException(
            status_code=409, detail="Assignment is not active"
        )

    now = datetime.now(timezone.utc)
    await repo.update_assignment(
        session,
        assignment_id,
        values={"status": "completed", "completed_at": now},
    )

    await repo.append_audit_entry(
        session,
        data={
            "case_id": assignment.case_id,
            "action": "step_completed",
            "actor_id": body.completed_by,
            "details": {
                "assignment_id": str(assignment_id),
                "step_id": assignment.step_id,
                "result": body.result,
            },
        },
    )

    # Signal the Temporal workflow that this step is done
    temporal_client = getattr(request.app.state, "temporal_client", None)
    if temporal_client is not None:
        # Load the case to get the workflow ID
        case = await repo.get_case_instance(session, assignment.case_id)
        if case and case.process_instance_id:
            try:
                handle = temporal_client.get_workflow_handle(
                    case.process_instance_id
                )
                await handle.signal(
                    "step_completed",
                    {
                        "step_id": assignment.step_id,
                        "completed_by": body.completed_by or "system",
                    },
                )
                logger.info(
                    "Signaled workflow %s: step %s completed",
                    case.process_instance_id,
                    assignment.step_id,
                )
            except Exception as e:
                logger.warning(
                    "Failed to signal workflow %s: %s",
                    case.process_instance_id,
                    e,
                )

    return await repo.get_assignment(session, assignment_id)


@router.delete("/{assignment_id}", status_code=204)
async def delete_assignment(
    assignment_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    """Delete an assignment (e.g. test assignments created via self-assign)."""
    assignment = await _get_assignment_or_404(session, assignment_id)
    await session.delete(assignment)
    await session.commit()
