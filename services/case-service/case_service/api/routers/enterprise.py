"""Enterprise hardening API — security events, GDPR, retention.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.auth.dependencies import require_role, get_current_user
from case_service.auth.models import AuthenticatedUser
from case_service.db.session import get_session
from case_service.enterprise import security_events, gdpr

router = APIRouter(prefix="/enterprise", tags=["enterprise"], dependencies=[Depends(require_role("admin"))])


# ─── Security events ─────────────────────────────────────────────

@router.get("/security-events")
async def list_security_events(
    event_type: str | None = None,
    user_id: str | None = None,
    severity: str | None = None,
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(100, ge=1, le=1000),
    session: AsyncSession = Depends(get_session),
):
    """Query security events."""
    return await security_events.query_events(
        session,
        event_type=event_type, user_id=user_id,
        severity=severity, days=days, limit=limit,
    )


@router.get("/security-events/stats")
async def security_event_stats(
    days: int = Query(30, ge=1, le=365),
    session: AsyncSession = Depends(get_session),
):
    """Aggregate security event statistics."""
    return await security_events.event_stats(session, days)


class SecurityEventCreate(BaseModel):
    event_type: str
    severity: str = "info"
    user_id: str | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    action: str | None = None
    outcome: str = "success"
    details: dict[str, Any] = Field(default_factory=dict)


@router.post("/security-events", status_code=201)
async def create_security_event(
    body: SecurityEventCreate,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Manually log a security event."""
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent")
    await security_events.log_security_event(
        session,
        event_type=body.event_type, severity=body.severity,
        user_id=body.user_id, resource_type=body.resource_type,
        resource_id=body.resource_id, action=body.action, outcome=body.outcome,
        ip_address=ip, user_agent=ua, details=body.details,
    )
    await session.commit()
    return {"status": "logged"}


# ─── GDPR ────────────────────────────────────────────────────────

class GDPRRequestCreate(BaseModel):
    subject_id: str
    request_type: str = "export"  # export, delete, rectify
    requested_by: str | None = None
    reason: str | None = None


@router.post("/gdpr/requests", status_code=201)
async def create_gdpr_request(
    body: GDPRRequestCreate,
    session: AsyncSession = Depends(get_session),
):
    """Create a GDPR data subject request."""
    if body.request_type not in ("export", "delete", "rectify", "restrict"):
        raise HTTPException(400, "Invalid request_type")

    req_id = await gdpr.create_request(
        session,
        subject_id=body.subject_id,
        request_type=body.request_type,
        requested_by=body.requested_by,
        reason=body.reason,
    )
    await session.commit()

    # Log as security event
    await security_events.log_security_event(
        session,
        event_type="gdpr.request_created",
        severity="info",
        user_id=body.requested_by,
        resource_type="user",
        resource_id=body.subject_id,
        action=body.request_type,
        details={"request_id": str(req_id)},
    )
    await session.commit()

    return {"request_id": str(req_id), "status": "pending"}


@router.get("/gdpr/requests")
async def list_gdpr_requests(
    subject_id: str | None = None,
    status: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    from case_service.db.models import GDPRRequestModel
    stmt = select(GDPRRequestModel).order_by(GDPRRequestModel.created_at.desc()).limit(100)
    if subject_id:
        stmt = stmt.where(GDPRRequestModel.subject_id == subject_id)
    if status:
        stmt = stmt.where(GDPRRequestModel.status == status)

    result = await session.execute(stmt)
    return [
        {
            "id": str(r.id),
            "subject_id": r.subject_id,
            "request_type": r.request_type,
            "status": r.status,
            "requested_by": r.requested_by,
            "reason": r.reason,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "completed_at": r.completed_at.isoformat() if r.completed_at else None,
        }
        for r in result.scalars().all()
    ]


class GDPRRequestUpdate(BaseModel):
    status: str  # "completed" | "rejected" | "in_progress"
    reason: str | None = None


@router.patch("/gdpr/requests/{request_id}")
async def update_gdpr_request(
    request_id: uuid.UUID,
    body: GDPRRequestUpdate,
    session: AsyncSession = Depends(get_session),
):
    """Manually update a GDPR request status (e.g. mark pending → completed)."""
    from case_service.db.models import GDPRRequestModel
    from sqlalchemy import update as sa_update
    allowed = {"pending", "in_progress", "completed", "rejected"}
    if body.status not in allowed:
        raise HTTPException(400, f"status must be one of {allowed}")
    values: dict = {"status": body.status}
    if body.status == "completed":
        values["completed_at"] = datetime.now(timezone.utc)
    if body.reason:
        values["reason"] = body.reason
    await session.execute(
        sa_update(GDPRRequestModel)
        .where(GDPRRequestModel.id == request_id)
        .values(**values)
    )
    await session.commit()
    return {"status": "updated"}


@router.get("/gdpr/lookup/{user_id}")
async def lookup_user_data(
    user_id: str,
    session: AsyncSession = Depends(get_session),
):
    """Check whether a user has any data — summary counts only, no PII returned.

    Covers both internal operators (UserDirectory) and external
    portal/customer users referenced in cases, assignments, and audit logs.
    """
    data = await gdpr.export_user_data(session, user_id)
    counts = {
        "user_profile":       len(data.get("user_profile", [])),
        "cases_created":      len(data["cases_created"]),
        "assignments":        len(data["assignments"]),
        "audit_entries":      len(data["audit_entries"]),
        "tenant_memberships": len(data["tenant_memberships"]),
        "security_events":    len(data["security_events"]),
    }
    exists = sum(counts.values()) > 0
    # Resolve canonical user_id if the lookup matched by email
    canonical_id = data.get("subject_id", user_id)
    if data.get("user_profile"):
        canonical_id = data["user_profile"][0]["user_id"]
    return {
        "user_id": user_id,
        "canonical_id": canonical_id,
        "exists": exists,
        "counts": counts,
    }


@router.get("/gdpr/export/{user_id}")
async def export_user_data(
    user_id: str,
    session: AsyncSession = Depends(get_session),
    actor: AuthenticatedUser = Depends(get_current_user),
):
    """Download all data for a user (GDPR Article 15).

    Automatically logs and completes a GDPR export request record.
    """
    data = await gdpr.export_user_data(session, user_id)

    # Create and immediately complete the GDPR request record
    req_id = await gdpr.create_request(
        session,
        subject_id=user_id,
        request_type="export",
        requested_by=actor.user_id,
    )
    await gdpr.complete_request(session, req_id)

    await security_events.log_security_event(
        session,
        event_type="gdpr.data_exported",
        severity="warning",
        resource_type="user",
        resource_id=user_id,
        action="export",
        outcome="success",
        details={
            "request_id": str(req_id),
            "cases": len(data["cases_created"]),
            "assignments": len(data["assignments"]),
            "audit_entries": len(data["audit_entries"]),
        },
    )
    await session.commit()

    payload = json.dumps(data, indent=2, default=str)
    return Response(
        content=payload,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="gdpr-export-{user_id}.json"'},
    )


@router.post("/gdpr/anonymize/{user_id}")
async def anonymize_user(
    user_id: str,
    session: AsyncSession = Depends(get_session),
    actor: AuthenticatedUser = Depends(get_current_user),
):
    """Anonymize all user data (GDPR right to be forgotten)."""
    result = await gdpr.anonymize_user_data(session, user_id)

    # Create and immediately complete the GDPR delete request record
    req_id = await gdpr.create_request(
        session,
        subject_id=user_id,
        request_type="delete",
        requested_by=actor.user_id,
    )
    await gdpr.complete_request(session, req_id)

    await security_events.log_security_event(
        session,
        event_type="gdpr.user_anonymized",
        severity="critical",
        user_id=actor.user_id,
        resource_type="user",
        resource_id=user_id,
        action="anonymize",
        outcome="success",
        details={**result, "request_id": str(req_id), "performed_by": actor.user_id},
    )
    await session.commit()

    return result


# ─── Retention policies ─────────────────────────────────────────

async def _ensure_default_policies(session: AsyncSession):
    """Ensure default retention policies exist (idempotent)."""
    from case_service.db.models import RetentionPolicyModel
    defaults = [
        ("Resolved case retention", "resolved_cases", 2555, "archive"),
        ("Audit log retention", "audit_log", 2555, "archive"),
        ("Security event retention", "security_events", 1095, "archive"),
        ("Process mining event log", "event_log", 365, "delete"),
    ]
    existing = await session.execute(select(RetentionPolicyModel.resource_type))
    existing_types = {r[0] for r in existing.all()}
    for name, rtype, days, action in defaults:
        if rtype not in existing_types:
            session.add(RetentionPolicyModel(
                name=name, resource_type=rtype,
                retention_days=days, action=action, enabled=False,
            ))
    await session.flush()


@router.get("/retention-policies")
async def list_retention_policies(
    session: AsyncSession = Depends(get_session),
):
    from case_service.db.models import RetentionPolicyModel
    await _ensure_default_policies(session)
    await session.commit()
    stmt = select(RetentionPolicyModel).order_by(RetentionPolicyModel.resource_type)
    result = await session.execute(stmt)
    return [
        {
            "id": str(p.id),
            "name": p.name,
            "resource_type": p.resource_type,
            "retention_days": p.retention_days,
            "action": p.action,
            "enabled": p.enabled,
            "last_run_at": p.last_run_at.isoformat() if p.last_run_at else None,
        }
        for p in result.scalars().all()
    ]


class RetentionPolicyUpdate(BaseModel):
    retention_days: int | None = None
    action: str | None = None
    enabled: bool | None = None


@router.patch("/retention-policies/{policy_id}")
async def update_retention_policy(
    policy_id: uuid.UUID,
    body: RetentionPolicyUpdate,
    session: AsyncSession = Depends(get_session),
):
    from case_service.db.models import RetentionPolicyModel
    from sqlalchemy import update
    values = {k: v for k, v in body.model_dump(exclude_unset=True).items()}
    if values:
        await session.execute(
            update(RetentionPolicyModel).where(
                RetentionPolicyModel.id == policy_id,
            ).values(**values)
        )
        await session.commit()
    return {"status": "updated"}


# ─── System info ────────────────────────────────────────────────

@router.get("/system-info")
async def get_system_info(
    session: AsyncSession = Depends(get_session),
):
    """System status and enterprise compliance statistics."""
    from case_service.db.models import (
        CaseInstanceModel, CaseTypeModel, SecurityEventModel,
        RetentionPolicyModel, GDPRRequestModel, UserDirectoryModel,
    )
    from sqlalchemy import func
    from datetime import timedelta

    async def _count(model, *filters):
        stmt = select(func.count()).select_from(model)
        for f in filters:
            stmt = stmt.where(f)
        return (await session.execute(stmt)).scalar_one()

    total_cases      = await _count(CaseInstanceModel)
    total_case_types = await _count(CaseTypeModel)
    total_users      = await _count(UserDirectoryModel)
    active_users     = await _count(UserDirectoryModel, UserDirectoryModel.is_active == True)

    # Security events last 24h and last 30d
    now = datetime.now(timezone.utc)
    events_24h  = await _count(SecurityEventModel,
        SecurityEventModel.timestamp >= now - timedelta(hours=24))
    events_30d  = await _count(SecurityEventModel,
        SecurityEventModel.timestamp >= now - timedelta(days=30))
    failed_30d  = await _count(SecurityEventModel,
        SecurityEventModel.timestamp >= now - timedelta(days=30),
        SecurityEventModel.outcome.in_(["denied", "error", "failed"]),
    )

    # Retention policies
    active_policies = await _count(RetentionPolicyModel, RetentionPolicyModel.enabled == True)
    total_policies  = await _count(RetentionPolicyModel)

    # GDPR requests
    pending_gdpr    = await _count(GDPRRequestModel, GDPRRequestModel.status == "pending")
    total_gdpr      = await _count(GDPRRequestModel)
    completed_gdpr  = await _count(GDPRRequestModel, GDPRRequestModel.status == "completed")

    # Tenants count
    tenants_count = 0
    try:
        from case_service.db.models import TenantModel
        tenants_count = await _count(TenantModel, TenantModel.status == "active")
    except Exception:
        pass

    return {
        "version": "0.20.0",
        "phase": 20,
        "phase_name": "Enterprise Hardening",
        # Core
        "total_cases": total_cases,
        "total_case_types": total_case_types,
        "active_tenants": tenants_count,
        # Users
        "total_users": total_users,
        "active_users": active_users,
        # Security
        "security_events_24h": events_24h,
        "security_events_30d": events_30d,
        "security_failed_30d": failed_30d,
        # Retention
        "retention_policies_active": active_policies,
        "retention_policies_total": total_policies,
        # GDPR
        "gdpr_requests_pending": pending_gdpr,
        "gdpr_requests_completed": completed_gdpr,
        "gdpr_requests_total": total_gdpr,
    }


# ─── Superadmin-only hidden audit log ───────────────────────────

def _require_superadmin(user: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
    if "superadmin" not in (user.roles or []):
        raise HTTPException(403, "Superadmin access required.")
    return user


@router.get("/superadmin-audit", dependencies=[])
async def superadmin_audit(
    limit: int = Query(200, le=1000),
    offset: int = Query(0),
    session: AsyncSession = Depends(get_session),
    _sa: AuthenticatedUser = Depends(_require_superadmin),
):
    """Hidden audit log — shows __system__ actor events (superadmin actions).

    Only accessible to the superadmin. Not visible to regular admins.
    """
    from case_service.db.models import SecurityEventModel, TraceEventModel
    from sqlalchemy import desc

    # Security events attributed to __system__ (superadmin actions)
    sec_stmt = (
        select(SecurityEventModel)
        .where(SecurityEventModel.user_id == "__system__")
        .order_by(desc(SecurityEventModel.timestamp))
        .offset(offset).limit(limit)
    )
    sec_rows = (await session.execute(sec_stmt)).scalars().all()

    # Trace events attributed to __system__
    trace_stmt = (
        select(TraceEventModel)
        .where(TraceEventModel.actor_user_id == "__system__")
        .order_by(desc(TraceEventModel.occurred_at))
        .offset(offset).limit(limit)
    )
    trace_rows = (await session.execute(trace_stmt)).scalars().all()

    return {
        "security_events": [
            {
                "id": str(r.id), "event_type": r.event_type,
                "severity": r.severity, "resource_type": r.resource_type,
                "action": r.action, "outcome": r.outcome,
                "ip_address": r.ip_address,
                "timestamp": r.timestamp.isoformat() if r.timestamp else None,
                "details": r.details,
            }
            for r in sec_rows
        ],
        "trace_events": [
            {
                "id": str(r.id), "event_type": r.event_type,
                "actor_ip": r.actor_ip, "payload": r.payload,
                "occurred_at": r.occurred_at.isoformat() if r.occurred_at else None,
            }
            for r in trace_rows
        ],
        "total_security": len(sec_rows),
        "total_trace": len(trace_rows),
    }
