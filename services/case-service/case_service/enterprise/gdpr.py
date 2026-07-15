"""GDPR compliance tools — data export, deletion, rectification.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def export_user_data(
    session: AsyncSession, user_id: str,
) -> dict[str, Any]:
    """Compile all data about a user for GDPR export (Article 15)."""
    from case_service.db.models import (
        CaseInstanceModel, CaseAuditLogModel, CaseAssignmentModel,
        TenantMembershipModel, SecurityEventModel,
    )

    export = {
        "subject_id": user_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "user_profile": [],
        "cases_created": [],
        "assignments": [],
        "audit_entries": [],
        "tenant_memberships": [],
        "security_events": [],
    }

    # User directory profile (covers internal operators; also match by email)
    try:
        from case_service.db.models import UserDirectoryModel
        from sqlalchemy import or_
        ud_stmt = select(UserDirectoryModel).where(
            or_(UserDirectoryModel.user_id == user_id, UserDirectoryModel.email == user_id)
        )
        ud_result = await session.execute(ud_stmt)
        for u in ud_result.scalars().all():
            export["user_profile"].append({
                "user_id": u.user_id,
                "email": u.email,
                "display_name": u.display_name,
                "roles": u.roles,
                "is_active": u.is_active,
                "tenant_id": u.tenant_id,
                "created_at": u.created_at.isoformat() if u.created_at else None,
            })
            # Normalise to the canonical user_id if looked up by email
            if u.user_id != user_id:
                user_id = u.user_id
    except Exception:
        pass

    # Cases created by user
    cases_stmt = select(CaseInstanceModel).where(CaseInstanceModel.created_by == user_id)
    cases_result = await session.execute(cases_stmt)
    for c in cases_result.scalars().all():
        export["cases_created"].append({
            "id": str(c.id),
            "case_type_id": str(c.case_type_id),
            "status": c.status,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        })

    # Assignments
    assign_stmt = select(CaseAssignmentModel).where(CaseAssignmentModel.assignee_id == user_id)
    assign_result = await session.execute(assign_stmt)
    for a in assign_result.scalars().all():
        export["assignments"].append({
            "id": str(a.id),
            "case_id": str(a.case_id),
            "step_id": a.step_id,
            "status": a.status,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        })

    # Audit entries actor
    audit_stmt = select(CaseAuditLogModel).where(CaseAuditLogModel.actor_id == user_id)
    audit_result = await session.execute(audit_stmt)
    for a in audit_result.scalars().all():
        export["audit_entries"].append({
            "id": str(a.id),
            "case_id": str(a.case_id),
            "action": a.action,
            "timestamp": a.timestamp.isoformat() if a.timestamp else None,
        })

    # Tenant memberships
    try:
        tm_stmt = select(TenantMembershipModel).where(TenantMembershipModel.user_id == user_id)
        tm_result = await session.execute(tm_stmt)
        for m in tm_result.scalars().all():
            export["tenant_memberships"].append({
                "tenant_id": str(m.tenant_id),
                "role": m.role,
            })
    except Exception:
        pass  # Tenants table might not exist in older setups

    # Security events
    try:
        sec_stmt = select(SecurityEventModel).where(SecurityEventModel.user_id == user_id).limit(500)
        sec_result = await session.execute(sec_stmt)
        for e in sec_result.scalars().all():
            export["security_events"].append({
                "event_type": e.event_type,
                "action": e.action,
                "outcome": e.outcome,
                "timestamp": e.timestamp.isoformat() if e.timestamp else None,
            })
    except Exception:
        pass

    return export


async def anonymize_user_data(
    session: AsyncSession, user_id: str,
) -> dict[str, Any]:
    """Anonymize (don't delete) user data — GDPR-compliant right to be forgotten.

    Replaces user_id with 'anon-{short-hash}' to preserve audit trail integrity
    while removing personal identification.
    """
    from case_service.db.models import (
        CaseInstanceModel, CaseAuditLogModel, CaseAssignmentModel,
        TenantMembershipModel,
    )
    import hashlib

    anon_id = "anon-" + hashlib.sha256(user_id.encode()).hexdigest()[:12]

    counts = {}

    # Anonymize cases.created_by
    stmt = update(CaseInstanceModel).where(
        CaseInstanceModel.created_by == user_id
    ).values(created_by=anon_id)
    result = await session.execute(stmt)
    counts["cases"] = result.rowcount

    # Anonymize audit.actor_id
    stmt = update(CaseAuditLogModel).where(
        CaseAuditLogModel.actor_id == user_id
    ).values(actor_id=anon_id)
    result = await session.execute(stmt)
    counts["audit"] = result.rowcount

    # Anonymize assignments.assignee_id
    stmt = update(CaseAssignmentModel).where(
        CaseAssignmentModel.assignee_id == user_id
    ).values(assignee_id=anon_id)
    result = await session.execute(stmt)
    counts["assignments"] = result.rowcount

    # Remove tenant memberships entirely
    try:
        stmt = delete(TenantMembershipModel).where(TenantMembershipModel.user_id == user_id)
        result = await session.execute(stmt)
        counts["memberships_removed"] = result.rowcount
    except Exception:
        counts["memberships_removed"] = 0

    # HxReplay traces are DERIVED case data (design §9 retention): anonymize the
    # run creator and any recorded actor ids inside stored counterfactual traces.
    from case_service.db.models import ReplayResultModel, ReplayRunModel

    stmt = update(ReplayRunModel).where(
        ReplayRunModel.created_by == user_id
    ).values(created_by=anon_id)
    counts["replay_runs"] = (await session.execute(stmt)).rowcount

    import copy as _copy
    scrubbed = 0
    results = (await session.execute(select(ReplayResultModel))).scalars().all()
    for res in results:
        if not res.trace or user_id not in str(res.trace):
            continue
        # deep-copy FIRST: mutating the loaded dict in place would leave the new
        # value == the old one at flush, and the JSON column would never update
        trace = _copy.deepcopy(res.trace)
        for node in trace.get("nodes", []) or []:
            if node.get("actor_id") == user_id:
                node["actor_id"] = anon_id
        res.trace = trace
        scrubbed += 1
    counts["replay_traces"] = scrubbed

    return {
        "subject_id": user_id,
        "anonymized_id": anon_id,
        "counts": counts,
        "anonymized_at": datetime.now(timezone.utc).isoformat(),
    }


async def create_request(
    session: AsyncSession,
    *,
    subject_id: str,
    request_type: str,
    requested_by: str | None = None,
    reason: str | None = None,
) -> uuid.UUID:
    """Record a GDPR data subject request."""
    from case_service.db.models import GDPRRequestModel

    req = GDPRRequestModel(
        subject_id=subject_id,
        request_type=request_type,
        status="pending",
        requested_by=requested_by,
        reason=reason,
    )
    session.add(req)
    await session.flush()
    return req.id


async def complete_request(
    session: AsyncSession,
    request_id: uuid.UUID,
    result_file: str | None = None,
) -> None:
    """Mark a GDPR request as completed."""
    from case_service.db.models import GDPRRequestModel

    stmt = update(GDPRRequestModel).where(
        GDPRRequestModel.id == request_id,
    ).values(
        status="completed",
        completed_at=datetime.now(timezone.utc),
        result_file=result_file,
    )
    await session.execute(stmt)
