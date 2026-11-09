"""Assignment API router.

Handles claiming, releasing, reassigning, and completing work items.

Copyright (c) 2024-2025 Velaris Contributors
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
from velaris_shared.db import repository as repo
from velaris_shared.auth.dependencies import get_current_user
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.hxguard.step_submit import acts_as, may_receive_work, require_step_submit
from velaris_shared.db.session import get_session


async def _sync_assignee_tuple(
    session: AsyncSession, case_id: uuid.UUID, user_id: str | None,
    *, add: bool,
) -> None:
    """HxGuard Phase B: keep the case#assignee@user tuple in step with
    assignment state, in the SAME transaction.

    Removal only happens when the user holds NO other active user-assignment
    on the case — one completed step must not revoke access granted by a
    still-active one."""
    if not user_id:
        return
    from velaris_shared.hxguard import tuples as hxg_tuples
    if add:
        await hxg_tuples.write_tuple(
            session, object_type="case", object_id=case_id,
            relation="assignee", subject_type="user",
            subject_id=str(user_id), created_by="assignment",
        )
        return
    from sqlalchemy import select
    from velaris_shared.db.models import CaseAssignmentModel
    remaining = (await session.execute(
        select(CaseAssignmentModel.id)
        .where(CaseAssignmentModel.case_id == case_id)
        .where(CaseAssignmentModel.assignee_id == str(user_id))
        .where(CaseAssignmentModel.assignee_type == "user")
        .where(CaseAssignmentModel.status == "active")
        .limit(1)
    )).scalar_one_or_none()
    if remaining is None:
        await hxg_tuples.remove_tuple(
            session, object_type="case", object_id=case_id,
            relation="assignee", subject_type="user", subject_id=str(user_id),
        )

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/assignments", tags=["assignments"], dependencies=[Depends(get_current_user)])


async def _guard(session: AsyncSession, user: AuthenticatedUser, assignment) -> None:
    """Claim, release, reassign, delete and complete take one rule (2026-10-28):
    whoever may complete the work. Otherwise reassigning it to yourself, or
    deleting it, walks around `require_step_submit`."""
    case_row = await repo.get_case_instance(session, assignment.case_id)
    if case_row is None:
        raise HTTPException(status_code=404, detail="Case not found")
    await require_step_submit(session, user, case_row, assignment)


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
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Self-service: user claims an item from a queue."""
    assignment = await _get_assignment_or_404(session, assignment_id)
    # The caller claims for themselves: `user_id` in the body was trusted, so
    # anyone could claim in someone else's name (2026-10-28).
    if not acts_as(user, body.user_id):
        raise HTTPException(status_code=403, detail="You can only claim work for yourself.")
    await _guard(session, user, assignment)
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
    await _sync_assignee_tuple(session, assignment.case_id, body.user_id, add=True)

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
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Return an item back to its queue (unclaim)."""
    assignment = await _get_assignment_or_404(session, assignment_id)
    # Checked against the token, not the body, which named whoever it liked.
    if not acts_as(user, body.user_id) or not acts_as(user, assignment.assignee_id):
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
    await _sync_assignee_tuple(session, assignment.case_id, body.user_id, add=False)

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
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Supervisor or rule reassigns an item to a different user."""
    assignment = await _get_assignment_or_404(session, assignment_id)
    await _guard(session, user, assignment)
    # The new assignee must be someone who may hold this work — else any
    # outsider could be handed it and complete it (2026-10-28).
    from velaris_shared.tenancy.identity import is_superadmin
    case_row = await repo.get_case_instance(session, assignment.case_id)
    if not is_superadmin(user) and not await may_receive_work(session, body.new_assignee_id, case_row):
        raise HTTPException(status_code=422, detail=(
            "That person is not in an access group covering this case type, so the work cannot go to them."))
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
    if assignment.assignee_type == "user":
        await _sync_assignee_tuple(session, assignment.case_id, prev_assignee, add=False)
    await _sync_assignee_tuple(session, assignment.case_id, body.new_assignee_id, add=True)

    await repo.append_audit_entry(
        session,
        data={
            "case_id": assignment.case_id,
            "action": "assignment_reassigned",
            "actor_id": user.user_id,
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
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Mark a work item as completed and signal the lifecycle workflow."""
    assignment = await _get_assignment_or_404(session, assignment_id)
    if assignment.status != "active":
        raise HTTPException(
            status_code=409, detail="Assignment is not active"
        )
    case_row = await repo.get_case_instance(session, assignment.case_id)
    if case_row is None:
        raise HTTPException(status_code=404, detail="Case not found")
    # Only the assignee, their access group or their N+1 (2026-10-28).
    await require_step_submit(session, user, case_row, assignment)
    if not acts_as(user, body.completed_by):
        raise HTTPException(status_code=403, detail="You can only complete work in your own name.")
    # A step with a form is completed by submitting the form (2026-10-28
    # audit): this route took no values, so a required form was skippable.
    from case_service.api.routers.form_submissions import _step_form_id
    if await _step_form_id(session, case_row, assignment.step_id):
        raise HTTPException(status_code=409, detail="This step has a form — submit the form to complete it.")

    now = datetime.now(timezone.utc)
    await repo.update_assignment(
        session,
        assignment_id,
        values={"status": "completed", "completed_at": now},
    )
    if assignment.assignee_type == "user":
        await _sync_assignee_tuple(session, assignment.case_id, assignment.assignee_id, add=False)

    await repo.append_audit_entry(
        session,
        data={
            "case_id": assignment.case_id,
            "action": "step_completed",
            "actor_id": user.user_id,
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
                        "completed_by": user.user_id,
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
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Delete an assignment (e.g. test assignments created via self-assign)."""
    assignment = await _get_assignment_or_404(session, assignment_id)
    await _guard(session, user, assignment)
    case_id, assignee_type, assignee_id = (
        assignment.case_id, assignment.assignee_type, assignment.assignee_id,
    )
    await session.delete(assignment)
    await session.flush()
    if assignee_type == "user":
        await _sync_assignee_tuple(session, case_id, assignee_id, add=False)
    await session.commit()
