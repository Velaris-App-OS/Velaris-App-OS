"""Relationship manager: handles cross-case status and priority propagation.

When a child case resolves and ``propagate_status=True``, the parent
may be unblocked.  When a child's priority changes and
``propagate_priority=True``, the parent's priority may be raised.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db import repository as repo
from case_service.db.models import (
    CaseInstanceModel,
    CaseRelationshipModel,
)

logger = logging.getLogger(__name__)

# Priority ordering for "bubble up" comparison
_PRIORITY_ORDER = ["low", "medium", "high", "critical", "blocker"]


def _priority_rank(p: str) -> int:
    try:
        return _PRIORITY_ORDER.index(p)
    except ValueError:
        return 1  # default to "medium"


async def propagate_status_change(
    session: AsyncSession,
    *,
    case_id: uuid.UUID,
    new_status: str,
) -> list[uuid.UUID]:
    """Propagate a status change to related cases.

    When a child case resolves/closes:
    - Check if all required children of the parent are resolved.
    - If so, signal the parent that it can proceed.

    Returns a list of parent case IDs that were affected.
    """
    affected: list[uuid.UUID] = []

    if new_status not in ("resolved", "closed", "cancelled"):
        return affected

    # Find relationships where this case is the target (i.e., it's a child)
    stmt = select(CaseRelationshipModel).where(
        CaseRelationshipModel.target_case_id == case_id,
        CaseRelationshipModel.propagate_status == True,  # noqa: E712
    )
    result = await session.execute(stmt)
    rels = result.scalars().all()

    for rel in rels:
        parent_id = rel.source_case_id

        if rel.relationship_type in ("child", "blocking"):
            # Check if all required children/blockers are resolved
            all_resolved = await _all_required_targets_resolved(
                session, parent_id
            )
            if all_resolved:
                # Update parent status from pending_subcase → open
                parent = await repo.get_case_instance(session, parent_id)
                if parent and parent.status == "pending_subcase":
                    await repo.update_case_instance(
                        session,
                        parent_id,
                        values={"status": "open"},
                    )
                    await repo.append_audit_entry(
                        session,
                        data={
                            "case_id": parent_id,
                            "action": "status_changed",
                            "actor_type": "system",
                            "details": {
                                "reason": "all_required_subcases_resolved",
                                "trigger_case_id": str(case_id),
                            },
                            "previous_value": {"status": "pending_subcase"},
                            "new_value": {"status": "open"},
                        },
                    )
                    affected.append(parent_id)
                    logger.info(
                        "Parent case %s unblocked by child %s",
                        parent_id,
                        case_id,
                    )

    return affected


async def propagate_priority_change(
    session: AsyncSession,
    *,
    case_id: uuid.UUID,
    new_priority: str,
) -> list[uuid.UUID]:
    """Propagate a priority change upward to parent cases.

    If a child's priority exceeds its parent's, the parent is
    escalated (only for ``propagate_priority=True`` relationships).

    Returns a list of parent case IDs that were escalated.
    """
    affected: list[uuid.UUID] = []

    stmt = select(CaseRelationshipModel).where(
        CaseRelationshipModel.target_case_id == case_id,
        CaseRelationshipModel.propagate_priority == True,  # noqa: E712
    )
    result = await session.execute(stmt)
    rels = result.scalars().all()

    child_rank = _priority_rank(new_priority)

    for rel in rels:
        parent_id = rel.source_case_id
        parent = await repo.get_case_instance(session, parent_id)
        if parent is None:
            continue

        parent_rank = _priority_rank(parent.priority)
        if child_rank > parent_rank:
            await repo.update_case_instance(
                session,
                parent_id,
                values={"priority": new_priority},
            )
            await repo.append_audit_entry(
                session,
                data={
                    "case_id": parent_id,
                    "action": "priority_changed",
                    "actor_type": "system",
                    "details": {
                        "reason": "child_priority_escalation",
                        "child_case_id": str(case_id),
                    },
                    "previous_value": {"priority": parent.priority},
                    "new_value": {"priority": new_priority},
                },
            )
            affected.append(parent_id)
            logger.info(
                "Parent case %s escalated to %s by child %s",
                parent_id,
                new_priority,
                case_id,
            )

    return affected


async def check_blocking_resolved(
    session: AsyncSession,
    case_id: uuid.UUID,
) -> bool:
    """Check if all cases blocking this one are resolved."""
    stmt = select(CaseRelationshipModel).where(
        CaseRelationshipModel.target_case_id == case_id,
        CaseRelationshipModel.relationship_type == "blocked_by",
        CaseRelationshipModel.required == True,  # noqa: E712
    )
    result = await session.execute(stmt)
    blockers = result.scalars().all()

    for blocker_rel in blockers:
        blocker = await repo.get_case_instance(
            session, blocker_rel.source_case_id
        )
        if blocker and blocker.status not in ("resolved", "closed"):
            return False

    return True


# ─── Helpers ──────────────────────────────────────────────────────


async def _all_required_targets_resolved(
    session: AsyncSession, parent_id: uuid.UUID
) -> bool:
    """Check if all required child/blocking targets of a parent are done."""
    stmt = select(CaseRelationshipModel).where(
        CaseRelationshipModel.source_case_id == parent_id,
        CaseRelationshipModel.required == True,  # noqa: E712
        CaseRelationshipModel.relationship_type.in_(["child", "blocking"]),
    )
    result = await session.execute(stmt)
    rels = result.scalars().all()

    for rel in rels:
        target = await repo.get_case_instance(session, rel.target_case_id)
        if target and target.status not in ("resolved", "closed", "cancelled"):
            return False

    return True
