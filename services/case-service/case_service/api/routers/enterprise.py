"""Enterprise hardening API — security events, GDPR, retention.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response

from velaris_shared.tenancy.identity import caller_tenant_uuid
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.auth.dependencies import require_role, get_current_user
from velaris_shared import hxguard
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.db.session import get_session
from case_service.enterprise import gdpr
from velaris_shared.db import security_events

import logging
from velaris_shared.db.read_routing import get_read_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/enterprise", tags=["enterprise"], dependencies=[Depends(hxguard.guard("compliance.enterprise.access"))])


# ─── Security events ─────────────────────────────────────────────

async def _event_scope(session: AsyncSession, user: AuthenticatedUser) -> list[str] | None:
    """None = every event (the platform operator); else the caller's tenant
    spellings — events stamped with its tenant (migration 230, 2026-11-04).
    Events with no tenant are the platform's and stay the operator's."""
    from case_service.tenancy.identity import caller_tenant_tokens, is_platform_operator
    if await is_platform_operator(session, user):
        return None
    return [str(t) for t in (await caller_tenant_tokens(session, user) or [])]


@router.get("/security-events")
async def list_security_events(
    event_type: str | None = None,
    user_id: str | None = None,
    severity: str | None = None,
    days: int = Query(30, ge=1, le=365),
    limit: int = Query(100, ge=1, le=1000),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Query security events — the operator every one, a tenant's admin its own."""
    return await security_events.query_events(
        session,
        event_type=event_type, user_id=user_id,
        severity=severity, days=days, limit=limit,
        tenants=await _event_scope(session, user),
    )


@router.get("/security-events/stats")
async def security_event_stats(
    days: int = Query(30, ge=1, le=365),
    session: AsyncSession = Depends(get_read_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Aggregate security event statistics — scoped as the list."""
    return await security_events.event_stats(session, days,
                                             tenants=await _event_scope(session, user))


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
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Manually log a security event. A tenant's admin records it as itself,
    in its own tenant (2026-11-04: any caller could name any user_id and feed
    the platform's log and Shield's detectors with someone else's events)."""
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent")
    actor = body.user_id
    if await _event_scope(session, user) is not None:
        actor = str(user.user_id)
    await security_events.log_security_event(
        session,
        event_type=body.event_type, severity=body.severity,
        user_id=actor, resource_type=body.resource_type,
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
    user: AuthenticatedUser = Depends(get_current_user),
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
        tenant_id=await caller_tenant_uuid(session, user),
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
    user: AuthenticatedUser = Depends(get_current_user),
):
    """The operator every tenant's requests; a tenant's admin its own (2026-11-04
    — requests carry a tenant since migration 213; tenant-less ones are the
    default tenant's)."""
    from velaris_shared.db.models import GDPRRequestModel
    from velaris_shared.tenancy.identity import scope_to_caller
    stmt = select(GDPRRequestModel).order_by(GDPRRequestModel.created_at.desc()).limit(100)
    if await _event_scope(session, user) is not None:
        stmt = await scope_to_caller(stmt, GDPRRequestModel, session, user)
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
    from velaris_shared.db.models import GDPRRequestModel
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


def _policy_refusal(exc: PermissionError) -> HTTPException:
    """An HxArbiter refusal is a 403, not a 500.

    `gdpr.export_user_data` raises `PermissionError` when the `export`
    namespace is enforcing and policy refuses — and nothing caught it, so the
    one path whose job is to refuse an export cleanly returned
    `VEL-SYS-UNHANDLED-500` instead. Found only by promoting the namespace on
    an isolated instance: in shadow the refusal never fires, which is why every
    test and every live check had passed.

    A 500 is wrong twice over here: it tells the caller the platform is broken
    when it is working exactly as configured, and it is the status an operator
    pages on.
    """
    return HTTPException(status_code=403, detail=str(exc))


@router.get("/gdpr/lookup/{user_id}")
async def lookup_user_data(
    user_id: str,
    session: AsyncSession = Depends(get_session),
    # Added only so the HxArbiter export PEP can be told which tenant is
    # asking. The router already gates every route here on
    # `compliance.enterprise.access`, so this widens nothing.
    actor: AuthenticatedUser = Depends(get_current_user),
):
    """Check whether a user has any data — summary counts only, no PII returned.

    Covers both internal operators (UserDirectory) and external
    portal/customer users referenced in cases, assignments, and audit logs.
    """
    try:
        data = await gdpr.export_user_data(
            session, user_id, tenant_id=getattr(actor, "tenant_id", None))
    except PermissionError as exc:
        raise _policy_refusal(exc) from exc
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
    try:
        data = await gdpr.export_user_data(
            session, user_id, tenant_id=getattr(actor, "tenant_id", None))
    except PermissionError as exc:
        raise _policy_refusal(exc) from exc

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


# ─── Customer subject access (GDPR Art. 15) — Oct item 8f ──────────────────

class CustomerExportIn(BaseModel):
    model_config = {"extra": "forbid"}
    emails: list[str] = Field(default_factory=list, max_length=20)
    phones: list[str] = Field(default_factory=list, max_length=20)
    portal_customer_ids: list[str] = Field(default_factory=list, max_length=20)
    customer_ids: list[str] = Field(default_factory=list, max_length=20)
    #: Internal staff notes and which agent handled a call. Off by default: an
    #: operator turns it on only after reviewing for third parties' data.
    include_internal: bool = False
    #: Counts only — "do we hold anything?" — nothing personal in the answer.
    summary: bool = False
    reason: str = Field(min_length=5, max_length=2000)


async def _export_gate(session: AsyncSession, actor: AuthenticatedUser,
                       tenant: str, ref: str) -> list:
    """The same HxArbiter `export` gate the staff export passes through."""
    try:
        from case_service.hxarbiter import pep as _arb_pep
        allowed, obligations = await _arb_pep.check_with_obligations(
            session, caller_handles=("watermark_export",), tenant_id=str(tenant),
            action=_arb_pep.ACTION_EXPORT_CASE, subject_id=str(actor.user_id),
            subject_kind=_arb_pep.SubjectKind.USER, resource_kind="data_subject",
            resource_id=ref[:255],
            context={"purpose": "dsar_article_15_customer", "destination_region": "external"},
            legacy_allowed=True, claimed_by="privacy.customer_export")
    except PermissionError:
        raise
    except Exception as exc:                                     # noqa: BLE001
        logger.warning("customer export: HxArbiter PEP unavailable: %s", exc)
        return []
    if not allowed:
        raise PermissionError("VEL-ARB-EXPORT-DENIED-403: policy refuses this export.")
    return obligations


@router.post("/gdpr/customer-export")
async def export_customer_data(
    body: CustomerExportIn,
    session: AsyncSession = Depends(get_session),
    actor: AuthenticatedUser = Depends(hxguard.guard("privacy.customers.export")),
):
    """Everything this tenant holds about one customer (GDPR Art. 15).

    Only the caller's tenant is searched. Recorded as a GDPR request and a
    security event — with counts, never the data.
    """
    from case_service.privacy import customer_export
    tenant = getattr(actor, "tenant_id", None) or "default"
    ref = ",".join((body.emails + body.phones + body.portal_customer_ids
                    + body.customer_ids)[:5]) or "unspecified"
    try:
        obligations = await _export_gate(session, actor, tenant, ref)
        data = await customer_export.build(
            session, tenant=tenant, emails=body.emails, phones=body.phones,
            portal_customer_ids=body.portal_customer_ids, customer_ids=body.customer_ids,
            include_internal=body.include_internal)
    except PermissionError as exc:
        raise _policy_refusal(exc) from exc
    except customer_export.ExportError as exc:
        raise HTTPException(422, f"VEL-PRIVACY-EXPORT-422: {exc}") from exc
    for ob in obligations:
        if str(getattr(ob, "kind", "")) == "watermark_export":
            data["watermark"] = {"applied": True, "label": str(
                (getattr(ob, "params", {}) or {}).get("label")
                or "CONFIDENTIAL — subject access response")}
    counts = customer_export.counts(data)
    if body.summary:
        return {"searched_for": data["searched_for"], "counts": counts,
                "exists": any(counts.values()),
                "internal_notes_withheld": data["internal_notes_withheld"]}

    req_id = await gdpr.create_request(
        session, subject_id=f"customer:{ref}"[:255], request_type="export",
        requested_by=actor.user_id, reason=body.reason)
    await gdpr.complete_request(session, req_id)
    await security_events.log_security_event(
        session, event_type="gdpr.customer_exported", severity="warning",
        user_id=actor.user_id, resource_type="customer", resource_id=ref[:255],
        action="export", outcome="success",
        details={"request_id": str(req_id), "tenant": str(tenant), "counts": counts,
                 "include_internal": body.include_internal})
    await session.commit()
    return Response(
        content=json.dumps(data, indent=2, default=str), media_type="application/json",
        headers={"Content-Disposition":
                 f'attachment; filename="customer-export-{str(req_id)[:8]}.json"'})


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
    from velaris_shared.db.models import RetentionPolicyModel
    defaults = [
        ("Resolved case retention", "resolved_cases", 2555, "archive"),
        ("Audit log retention", "audit_log", 2555, "archive"),
        ("Security event retention", "security_events", 1095, "archive"),
        ("Process mining event log", "event_log", 365, "delete"),
    ]
    existing = await session.execute(
        select(RetentionPolicyModel.resource_type)
        .where(RetentionPolicyModel.tenant_id.is_(None))
    )
    existing_types = {r[0] for r in existing.all()}
    for name, rtype, days, action in defaults:
        if rtype not in existing_types:
            # Seeded with tenant_id NULL — the platform default, applied to
            # any tenant that has not defined its own policy for this type.
            session.add(RetentionPolicyModel(
                name=name, resource_type=rtype, tenant_id=None,
                retention_days=days, action=action, enabled=False,
            ))
    await session.flush()


@router.get("/retention-policies")
async def list_retention_policies(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    from velaris_shared.db.models import RetentionPolicyModel
    await _ensure_default_policies(session)
    await session.commit()
    stmt = select(RetentionPolicyModel).order_by(RetentionPolicyModel.resource_type)
    # Every tenant's rows until 2026-09-29 (8d).
    from case_service.tenancy.identity import scope_to_caller
    stmt = await scope_to_caller(stmt, RetentionPolicyModel, session, user)
    result = await session.execute(stmt)
    return [
        {
            "id": str(p.id),
            "name": p.name,
            "tenant_id": p.tenant_id,
            "resource_type": p.resource_type,
            "retention_days": p.retention_days,
            "action": p.action,
            "enabled": p.enabled,
            "last_run_at": p.last_run_at.isoformat() if p.last_run_at else None,
        }
        for p in result.scalars().all()
    ]


@router.get("/retention-policies/preview")
async def preview_retention(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """What would each enabled policy delete? Counts only, touches nothing.

    The first thing anyone should be able to do with a deletion job is find
    out what it would destroy. This is that, and it is a GET so it cannot be
    mistaken for the thing that acts.
    """
    from case_service.compliance import retention

    # Scoped to the caller's tenant: a preview that counted another tenant's
    # rows would misreport the impact of a policy they cannot even see.
    return await retention.run_all(
        session, apply=False,
        tenant_id=str(user.tenant_id) if user.tenant_id else None,
    )


@router.post("/retention-policies/run")
async def run_retention(
    confirm: bool = False,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("compliance.retention.run")),
):
    """Apply every enabled policy. **Deletes data.**

    Admin-only and gated behind an explicit `confirm=true`, because there is no
    undo: a retention run that removed the wrong rows cannot be reversed, and a
    misclick should not be able to start it. Without the flag this behaves as
    the preview.
    """
    from case_service.compliance import retention

    tenant = str(user.tenant_id) if user.tenant_id else None
    if not confirm:
        out = await retention.run_all(session, apply=False, tenant_id=tenant)
        out["hint"] = ("this was a dry run — re-send with ?confirm=true to delete. "
                       "There is no undo.")
        return out

    out = await retention.run_all(session, apply=True, tenant_id=tenant)
    await session.commit()
    logger.warning(
        "retention run applied by %s — %d row(s) deleted",
        user.user_id, out.get("total_rows", 0),
    )
    return out


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
    from velaris_shared.db.models import RetentionPolicyModel
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
    user: AuthenticatedUser = Depends(get_current_user),
):
    """System status and enterprise compliance statistics. The platform operator
    sees the install's totals; a tenant's admin its own tenant's (2026-11-04:
    every tenant's admin saw the whole install's — cases, users, tenants)."""
    from velaris_shared.db.models import (
        CaseInstanceModel, CaseTypeModel, SecurityEventModel,
        RetentionPolicyModel, GDPRRequestModel, UserDirectoryModel,
    )
    from sqlalchemy import func, or_
    from datetime import timedelta

    tokens = await _event_scope(session, user)          # None = the operator
    mine = await caller_tenant_uuid(session, user) if tokens is not None else None

    def _str_scope(col, shared: bool = False):
        if tokens is None:
            return ()
        return (or_(col.in_(tokens), col.is_(None)),) if shared else (col.in_(tokens),)

    def _uuid_scope(col, shared: bool = False):
        if tokens is None:
            return ()
        return (or_(col == mine, col.is_(None)),) if shared else (col == mine,)

    async def _count(model, *filters):
        stmt = select(func.count()).select_from(model)
        for f in filters:
            stmt = stmt.where(f)
        return (await session.execute(stmt)).scalar_one()

    total_cases      = await _count(CaseInstanceModel, *_uuid_scope(CaseInstanceModel.tenant_id))
    total_case_types = await _count(CaseTypeModel, *_uuid_scope(CaseTypeModel.tenant_id, shared=True))
    total_users      = await _count(UserDirectoryModel, *_str_scope(UserDirectoryModel.tenant_id))
    active_users     = await _count(UserDirectoryModel, UserDirectoryModel.is_active == True,
                                    *_str_scope(UserDirectoryModel.tenant_id))

    # Security events last 24h and last 30d
    now = datetime.now(timezone.utc)
    ev = _str_scope(SecurityEventModel.tenant_id)

    async def _events(*filters):
        # One row can stand for many refused repeats (migration 231).
        stmt = select(func.coalesce(func.sum(SecurityEventModel.repeat_count), 0))
        for f in filters:
            stmt = stmt.where(f)
        return int((await session.execute(stmt)).scalar_one())

    events_24h  = await _events(
        SecurityEventModel.timestamp >= now - timedelta(hours=24), *ev)
    events_30d  = await _events(
        SecurityEventModel.timestamp >= now - timedelta(days=30), *ev)
    failed_30d  = await _events(
        SecurityEventModel.timestamp >= now - timedelta(days=30),
        SecurityEventModel.outcome.in_(["denied", "error", "failed"]), *ev,
    )

    # Retention policies (a tenant's own plus the platform defaults it inherits)
    rp = _str_scope(RetentionPolicyModel.tenant_id, shared=True)
    active_policies = await _count(RetentionPolicyModel, RetentionPolicyModel.enabled == True, *rp)
    total_policies  = await _count(RetentionPolicyModel, *rp)

    # GDPR requests
    gd = _uuid_scope(GDPRRequestModel.tenant_id)
    pending_gdpr    = await _count(GDPRRequestModel, GDPRRequestModel.status == "pending", *gd)
    total_gdpr      = await _count(GDPRRequestModel, *gd)
    completed_gdpr  = await _count(GDPRRequestModel, GDPRRequestModel.status == "completed", *gd)

    # Tenants count — a tenant's admin is in one.
    tenants_count = 1 if tokens is not None else 0
    if tokens is None:
        try:
            from velaris_shared.db.models import TenantModel
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
    session: AsyncSession = Depends(get_read_session),
    _sa: AuthenticatedUser = Depends(_require_superadmin),
):
    """Hidden audit log — shows __system__ actor events (superadmin actions).

    Only accessible to the superadmin. Not visible to regular admins.
    """
    from velaris_shared.db.models import SecurityEventModel, TraceEventModel
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
