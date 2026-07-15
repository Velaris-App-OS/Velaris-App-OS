"""Customer Portal API — P33 + P39a.

Public endpoints (no HELIX auth — accessible by external customers):
  GET    /portal/{slug}                         portal config + available case types
  POST   /portal/{slug}/submit                  anonymous case submission → tracking token
  GET    /portal/{slug}/track/{token}           case status by tracking token
  POST   /portal/{slug}/track/{token}/documents upload document (token-auth only)
  GET    /portal/{slug}/my-cases?email=X        all cases for this submitter (P39a)
  GET    /portal/{slug}/cases/{id}/timeline?email=X  customer-visible audit trail (P39a)
  GET    /portal/{slug}/cases/{case_id}/documents?email=X  portal-visible docs (P39b)
  GET    /portal/{slug}/cases/{case_id}/sla?email=X         SLA countdown (P39b)
  POST   /portal/{slug}/ask                     pre-submission RAG self-service (P39c)
  POST   /portal/{slug}/cases/{id}/chat         case-scoped AI chat (P39c)

Admin endpoints (staff, requires auth):
  GET    /portal-admin/tenants             list portals + enabled status
  PATCH  /portal-admin/tenants/{slug}      update portal settings (enable, branding, case types)
  GET    /portal-admin/submissions         all portal submissions across tenants
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger(__name__)

# ── In-process rate limiter (resets on restart — sufficient for single instance) ──
_rl_lock = asyncio.Lock()
_rl_windows: dict[str, list[float]] = defaultdict(list)

async def _rate_check(key: str, max_calls: int, window_seconds: int) -> bool:
    """Return True if allowed, False if rate-limited."""
    now = time.monotonic()
    async with _rl_lock:
        hits = _rl_windows[key]
        # Prune expired entries
        hits[:] = [t for t in hits if now - t < window_seconds]
        if len(hits) >= max_calls:
            return False
        hits.append(now)
        return True

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel
from sqlalchemy import select, desc
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.auth.dependencies import get_current_user, require_role
from case_service.auth.models import AuthenticatedUser
from case_service.db.models import (
    CaseAuditLogModel,
    CaseInstanceModel,
    CaseSLAInstanceModel,
    CaseTypeModel,
    DocumentModel,
    PaymentDisbursementModel,
    TenantModel,
)
from case_service.db.session import get_session
from case_service.storage import get_storage_backend
from case_service.hxnexus.factory import get_llm_backend, check_ai_available
from case_service.api.routers.payments import _encrypt_bank_ref
from case_service.case_vars import service as case_vars


async def _case_vars_for(
    session: AsyncSession, case_ids: list[uuid.UUID],
) -> dict[uuid.UUID, dict[str, Any]]:
    """Case data via the case_vars façade (portal context: pii/secret masked)."""
    ctx = case_vars.CallerContext(kind="portal", actor_id="portal")
    return await case_vars.get_all_bulk(session, ctx, case_ids)

# Two routers: one public, one admin-only
public_router = APIRouter(prefix="/portal", tags=["portal"])
admin_router = APIRouter(prefix="/portal-admin", tags=["portal-admin"])


# ─── SD-5 In-process OTP store ─────────────────────────────────────────────
# Keyed by str(case_id) → (sha256_hex, expiry_unix_seconds).
# In-memory — resets on restart; sufficient for single-instance deployments.
# For multi-replica deployments this should be replaced with a shared Redis store.

_PORTAL_OTPS: dict[str, tuple[str, float]] = {}


def _store_portal_otp(case_id: str, otp: str) -> None:
    """Hash and store a 6-digit OTP for the given case_id. Expires in 15 minutes."""
    h = hashlib.sha256(otp.encode()).hexdigest()
    _PORTAL_OTPS[case_id] = (h, time.time() + 900)


def _verify_portal_otp(case_id: str, otp: str) -> bool:
    """Constant-time OTP verification. Deletes entry on success (single-use).
    Also deletes on expiry to prevent indefinite accumulation.
    Returns True only when OTP is present, unexpired, and hash-matches.
    """
    entry = _PORTAL_OTPS.get(str(case_id))
    if not entry:
        return False
    h, expiry = entry
    if time.time() > expiry:
        # Expired — remove and deny regardless of value
        _PORTAL_OTPS.pop(str(case_id), None)
        return False
    # Constant-time comparison to prevent timing side-channel
    candidate = hashlib.sha256(otp.encode()).hexdigest()
    ok = secrets.compare_digest(candidate, h)
    if ok:
        # Single-use: invalidate immediately on first correct use
        _PORTAL_OTPS.pop(str(case_id), None)
    return ok


# ─── Pydantic schemas ────────────────────────────────────────────

class PortalConfig(BaseModel):
    slug: str
    name: str
    welcome_text: str
    brand_color: str
    logo_text: str
    enabled: bool
    logo_url: Optional[str] = None   # P5: real uploaded logo
    case_types: list[dict]


class PortalSubmitRequest(BaseModel):
    case_type_id: uuid.UUID
    submitter_name: str
    submitter_email: str
    subject: str
    description: str
    priority: str = "medium"
    extra_data: dict[str, Any] = {}
    # Portal v2 P2: client-generated idempotency key for offline/PWA syncs.
    # A replay with the same ref returns the ORIGINAL case, never a duplicate.
    client_ref: Optional[uuid.UUID] = None


class PortalSubmitResponse(BaseModel):
    tracking_token: str
    case_id: str
    message: str


class PortalCaseStatus(BaseModel):
    case_id: str
    subject: str
    status: str
    priority: str
    case_type_name: str
    submitted_at: datetime
    updated_at: datetime
    resolved_at: Optional[datetime] = None


class PortalSettingsUpdate(BaseModel):
    enabled: Optional[bool] = None
    welcome_text: Optional[str] = None
    brand_color: Optional[str] = None
    logo_text: Optional[str] = None
    allowed_case_type_ids: Optional[list[str]] = None


# SD-5 / SD-2 schemas
class BankOtpRequestBody(BaseModel):
    email: str


class BankDetailsSubmitBody(BaseModel):
    email: str
    otp: str
    account_name: str
    account_number: str
    sort_code: str


# ─── Helpers ─────────────────────────────────────────────────────

def _portal_settings(tenant: TenantModel) -> dict:
    return (tenant.settings or {}).get("portal", {})


def _get_portal_config(tenant: TenantModel, case_types: list) -> dict:
    ps = _portal_settings(tenant)
    return {
        "slug": tenant.slug,
        "name": tenant.name,
        "welcome_text": ps.get("welcome_text", f"Submit a request to {tenant.name}"),
        "brand_color": ps.get("brand_color", "#6366f1"),
        "logo_text": ps.get("logo_text", tenant.name),
        "enabled": ps.get("enabled", False),
        "logo_url": f"/api/v1/portal/{tenant.slug}/logo" if ps.get("logo_key") else None,
        "case_types": case_types,
    }


# ─── Public: Portal config ───────────────────────────────────────

@public_router.get("/{slug}", response_model=PortalConfig)
async def get_portal_config(
    slug: str,
    session: AsyncSession = Depends(get_session),
):
    tenant = (await session.execute(
        select(TenantModel).where(TenantModel.slug == slug)
    )).scalar_one_or_none()
    if not tenant:
        raise HTTPException(404, f"Portal '{slug}' not found")

    ps = _portal_settings(tenant)
    if not ps.get("enabled", False):
        raise HTTPException(403, "This portal is not currently active")

    allowed_ids = ps.get("allowed_case_type_ids", [])
    q = select(CaseTypeModel).where(CaseTypeModel.portal_enabled == True)  # noqa: E712
    if allowed_ids:
        q = q.where(CaseTypeModel.id.in_([uuid.UUID(x) for x in allowed_ids]))
    cts = (await session.execute(q)).scalars().all()

    ct_list = [{"id": str(ct.id), "name": ct.name, "description": ct.description,
                "default_priority": ct.default_priority} for ct in cts]
    return _get_portal_config(tenant, ct_list)


# ─── Public: Submit a case ───────────────────────────────────────

@public_router.post("/{slug}/submit", response_model=PortalSubmitResponse)
async def submit_case(
    slug: str,
    body: PortalSubmitRequest,
    session: AsyncSession = Depends(get_session),
):
    tenant = (await session.execute(
        select(TenantModel).where(TenantModel.slug == slug)
    )).scalar_one_or_none()
    if not tenant:
        raise HTTPException(404, f"Portal '{slug}' not found")

    ps = _portal_settings(tenant)
    if not ps.get("enabled", False):
        raise HTTPException(403, "This portal is not currently active")

    ct = await session.get(CaseTypeModel, body.case_type_id)
    if not ct or not ct.portal_enabled:
        raise HTTPException(400, "Case type not available for portal submission")

    allowed_ids = ps.get("allowed_case_type_ids", [])
    if allowed_ids and str(body.case_type_id) not in allowed_ids:
        raise HTTPException(400, "Case type not allowed for this portal")

    # Portal v2 P2 — idempotent replay: same client_ref ⇒ same case.
    from case_service.db.models import PortalSubmissionRefModel as _Ref
    if body.client_ref is not None:
        seen = (await session.execute(
            select(_Ref).where(_Ref.client_ref == body.client_ref)
        )).scalar_one_or_none()
        if seen is not None:
            if seen.tenant_slug != slug:
                # Same random UUID on another tenant is astronomically unlikely
                # — treat as a bad request rather than leak the other case.
                raise HTTPException(400, "Invalid client reference")
            original = await session.get(CaseInstanceModel, seen.case_id)
            return PortalSubmitResponse(
                tracking_token=str(original.portal_tracking_token) if original and original.portal_tracking_token else "",
                case_id=str(seen.case_id),
                message="Your request had already been submitted — this is the original reference.",
            )

    token = uuid.uuid4()
    case = CaseInstanceModel(
        case_type_id=body.case_type_id,
        case_type_version=ct.version,
        status="new",
        priority=body.priority,
        created_by=f"portal:{body.submitter_email}",
        portal_tracking_token=token,
        portal_submitter_name=body.submitter_name,
        portal_submitter_email=body.submitter_email,
        data={
            "subject": body.subject,
            "description": body.description,
            "source": "customer_portal",
            "tenant_slug": slug,
            **body.extra_data,
        },
        extra_metadata={"portal_slug": slug},
    )
    session.add(case)
    await session.flush()

    # Portal v2 (P1): link the case to an existing customer account at submit
    # time — registration only back-links historical cases, so without this a
    # logged-in customer's NEW submissions never appear under My Cases.
    from case_service.db.models import PortalCustomerCaseLinkModel as _Link
    from case_service.db.models import PortalCustomerModel as _Cust
    account = (await session.execute(
        select(_Cust).where(
            _Cust.tenant_id == tenant.id,
            _Cust.primary_email == body.submitter_email.lower(),
        )
    )).scalar_one_or_none()
    if account:
        session.add(_Link(customer_id=account.id, case_id=case.id))

    if body.client_ref is not None:
        session.add(_Ref(client_ref=body.client_ref, tenant_slug=slug, case_id=case.id))

    try:
        await session.commit()
    except IntegrityError:
        # Two racing replays: the loser re-reads the winner's ref row.
        await session.rollback()
        seen = (await session.execute(
            select(_Ref).where(_Ref.client_ref == body.client_ref)
        )).scalar_one()
        original = await session.get(CaseInstanceModel, seen.case_id)
        return PortalSubmitResponse(
            tracking_token=str(original.portal_tracking_token) if original and original.portal_tracking_token else "",
            case_id=str(seen.case_id),
            message="Your request had already been submitted — this is the original reference.",
        )

    return PortalSubmitResponse(
        tracking_token=str(token),
        case_id=str(case.id),
        message="Your request has been submitted. Use the tracking token to check status.",
    )


# ─── Public: Track case status ───────────────────────────────────

@public_router.get("/{slug}/track/{token}", response_model=PortalCaseStatus)
async def track_case(
    slug: str,
    token: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    case = (await session.execute(
        select(CaseInstanceModel).where(
            CaseInstanceModel.portal_tracking_token == token
        )
    )).scalar_one_or_none()
    if not case:
        raise HTTPException(404, "Tracking token not found")

    ct = await session.get(CaseTypeModel, case.case_type_id)
    cvars = (await _case_vars_for(session, [case.id]))[case.id]
    subject = cvars.get("subject", "Your request")

    return PortalCaseStatus(
        case_id=str(case.id),
        subject=subject,
        status=case.status,
        priority=case.priority,
        case_type_name=ct.name if ct else "Unknown",
        submitted_at=case.created_at,
        updated_at=case.updated_at,
        resolved_at=case.resolved_at,
    )


# ─── Public: Upload document (token-auth) ────────────────────────

@public_router.post("/{slug}/track/{token}/documents")
async def portal_upload_document(
    slug: str,
    token: uuid.UUID,
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
):
    case = (await session.execute(
        select(CaseInstanceModel).where(
            CaseInstanceModel.portal_tracking_token == token
        )
    )).scalar_one_or_none()
    if not case:
        raise HTTPException(404, "Tracking token not found")
    if case.status in ("resolved", "closed", "cancelled"):
        raise HTTPException(400, "Cannot upload to a closed case")

    from case_service.middleware.file_security import validate_upload_filename, safe_filename, ALLOWED_DOCUMENT_EXTENSIONS
    filename = file.filename or "upload"
    ok, reason = validate_upload_filename(filename, allowed_extensions=ALLOWED_DOCUMENT_EXTENSIONS)
    if not ok:
        raise HTTPException(400, f"File rejected: {reason}")
    filename = safe_filename(filename)
    data = await file.read()
    content_type = file.content_type or "application/octet-stream"

    storage = get_storage_backend()
    key = f"portal/{case.id}/{uuid.uuid4()}/{filename}"
    await storage.put(key, data, content_type=content_type)

    # Persist a DocumentModel record tagged as customer upload (P39b)
    try:
        doc_id = uuid.uuid4()
        doc = DocumentModel(
            id=doc_id,
            case_id=case.id,
            filename=filename,
            content_type=content_type,
            current_version=1,
            uploaded_by=f"portal:{case.portal_submitter_email}",
            tenant_id=(case.extra_metadata or {}).get("portal_slug"),
            portal_source="customer",
            portal_visible=False,
        )
        session.add(doc)
        await session.flush()
    except Exception:
        pass  # Document record is best-effort — upload already succeeded

    await session.commit()

    return {
        "filename": filename,
        "case_id": str(case.id),
        "size": len(data),
        "message": "Document uploaded successfully",
    }


# ─── Public: My Cases dashboard (P39a) ───────────────────────────
# Trust model: email is the identity, same as tracking token (no stronger auth needed
# for read-only case list — all data is already customer-submitted).

# Audit actions visible to customers — everything else is internal
_CUSTOMER_VISIBLE_ACTIONS = {
    "case_created",
    "stage_transitioned",
    "status_changed",
    "document_uploaded",
    "case_resolved",
    "case_closed",
    "case_reopened",
}

# Human-readable labels for customer-facing timeline
_ACTION_LABELS: dict[str, str] = {
    "case_created":      "Request submitted",
    "stage_transitioned": "Request moved to a new stage",
    "status_changed":    "Status updated",
    "document_uploaded": "A document was shared",
    "case_resolved":     "Request resolved",
    "case_closed":       "Request closed",
    "case_reopened":     "Request reopened",
}


@public_router.get("/{slug}/my-cases")
async def my_cases(
    slug: str,
    email: str,
    session: AsyncSession = Depends(get_session),
):
    """Return all cases submitted by *email* through this portal.

    No stronger auth than email — matches the existing token-track trust model.
    Rate-limiting is applied at the reverse-proxy level.
    """
    tenant = (await session.execute(
        select(TenantModel).where(TenantModel.slug == slug)
    )).scalar_one_or_none()
    if not tenant:
        raise HTTPException(404, f"Portal '{slug}' not found")

    cases = (await session.execute(
        select(CaseInstanceModel)
        .where(CaseInstanceModel.portal_submitter_email == email)
        .order_by(desc(CaseInstanceModel.created_at))
        .limit(100)
    )).scalars().all()

    result = []
    vars_by_case = await _case_vars_for(session, [c.id for c in cases])
    for c in cases:
        ct = await session.get(CaseTypeModel, c.case_type_id)
        cvars = vars_by_case.get(c.id, {})
        result.append({
            "case_id":          str(c.id),
            "case_number":      c.case_number,
            "tracking_token":   str(c.portal_tracking_token),
            "subject":          cvars.get("subject", "Your request"),
            "description":      cvars.get("description", ""),
            "status":           c.status,
            "priority":         c.priority,
            "current_stage_id": c.current_stage_id,
            "case_type_name":   ct.name if ct else "Unknown",
            "submitted_at":     c.created_at.isoformat() if c.created_at else None,
            "updated_at":       c.updated_at.isoformat() if c.updated_at else None,
            "resolved_at":      c.resolved_at.isoformat() if c.resolved_at else None,
        })
    return {"email": email, "cases": result, "total": len(result)}


@public_router.get("/{slug}/cases/{case_id}/timeline")
async def case_timeline(
    slug: str,
    case_id: uuid.UUID,
    email: str,
    session: AsyncSession = Depends(get_session),
):
    """Return customer-visible audit trail for one case.

    Verifies the case was submitted by *email* before returning data.
    Internal events (escalations, SLA, assignments) are stripped.
    """
    tenant = (await session.execute(
        select(TenantModel).where(TenantModel.slug == slug)
    )).scalar_one_or_none()
    if not tenant:
        raise HTTPException(404, f"Portal '{slug}' not found")

    case = (await session.execute(
        select(CaseInstanceModel).where(
            CaseInstanceModel.id == case_id,
            CaseInstanceModel.portal_submitter_email == email,
        )
    )).scalar_one_or_none()
    if not case:
        raise HTTPException(404, "Case not found or email does not match")

    ct = await session.get(CaseTypeModel, case.case_type_id)

    audit_rows = (await session.execute(
        select(CaseAuditLogModel)
        .where(CaseAuditLogModel.case_id == case_id)
        .order_by(CaseAuditLogModel.timestamp)
    )).scalars().all()

    timeline = []
    for row in audit_rows:
        if row.action not in _CUSTOMER_VISIBLE_ACTIONS:
            continue
        entry: dict = {
            "id":         str(row.id),
            "action":     row.action,
            "label":      _ACTION_LABELS.get(row.action, row.action.replace("_", " ").title()),
            "timestamp":  row.timestamp.isoformat() if row.timestamp else None,
            "details":    {},
        }
        # Surface safe detail fields — never expose actor_id or internal values
        if row.action == "stage_transitioned" and row.new_value:
            entry["details"]["stage"] = row.new_value.get("stage_id", "")
        if row.action == "status_changed" and row.new_value:
            entry["details"]["status"] = row.new_value.get("status", "")
        if row.action == "document_uploaded":
            entry["details"]["filename"] = (row.details or {}).get("filename", "")
        timeline.append(entry)

    # SD-2: detect whether a pending payment_disbursement step awaits bank details
    pending_disbursement = (await session.execute(
        select(PaymentDisbursementModel).where(
            PaymentDisbursementModel.case_id == case_id,
            PaymentDisbursementModel.status != "confirmed",
            PaymentDisbursementModel.bank_reference.is_(None),
        ).order_by(PaymentDisbursementModel.created_at.desc()).limit(1)
    )).scalar_one_or_none()

    cvars = (await _case_vars_for(session, [case.id]))[case.id]
    return {
        "case_id":             str(case.id),
        "case_number":         case.case_number,
        "subject":             cvars.get("subject", "Your request"),
        "status":              case.status,
        "priority":            case.priority,
        "case_type_name":      ct.name if ct else "Unknown",
        "submitted_at":        case.created_at.isoformat() if case.created_at else None,
        "resolved_at":         case.resolved_at.isoformat() if case.resolved_at else None,
        "timeline":            timeline,
        "pending_payment_step": pending_disbursement is not None,
    }


# ─── Public: Portal-visible documents (P39b) ─────────────────────

@public_router.get("/{slug}/cases/{case_id}/documents")
async def portal_case_documents(
    slug: str,
    case_id: uuid.UUID,
    email: str,
    session: AsyncSession = Depends(get_session),
):
    """Return documents shared with the customer for this case.

    Returns staff-shared docs (portal_visible=True) plus the customer's
    own uploads (portal_source='customer'), verified by email ownership.
    """
    tenant = (await session.execute(
        select(TenantModel).where(TenantModel.slug == slug)
    )).scalar_one_or_none()
    if not tenant:
        raise HTTPException(404, f"Portal '{slug}' not found")

    case = (await session.execute(
        select(CaseInstanceModel).where(
            CaseInstanceModel.id == case_id,
            CaseInstanceModel.portal_submitter_email == email,
        )
    )).scalar_one_or_none()
    if not case:
        raise HTTPException(404, "Case not found or email does not match")

    docs = (await session.execute(
        select(DocumentModel).where(
            DocumentModel.case_id == case_id,
            DocumentModel.is_deleted == False,
            # Staff-shared OR customer's own uploads
            (DocumentModel.portal_visible == True) |
            (DocumentModel.portal_source == "customer"),
        ).order_by(DocumentModel.created_at)
    )).scalars().all()

    return {
        "case_id": str(case_id),
        "documents": [
            {
                "id":           str(d.id),
                "filename":     d.filename,
                "content_type": d.content_type,
                "size_bytes":   d.versions[-1].size_bytes if d.versions else None,
                "source":       d.portal_source or "staff",
                "uploaded_at":  d.created_at.isoformat() if d.created_at else None,
                "download_url": f"/api/v1/documents/{d.id}/download",
            }
            for d in docs
        ],
    }


# ─── Public: SLA countdown (P39b) ────────────────────────────────

@public_router.get("/{slug}/cases/{case_id}/sla")
async def portal_case_sla(
    slug: str,
    case_id: uuid.UUID,
    email: str,
    session: AsyncSession = Depends(get_session),
):
    """Return SLA deadline and countdown for the customer.

    Color tier: green → on_track | amber → at_risk | red → breached.
    """
    from datetime import timezone as tz

    tenant = (await session.execute(
        select(TenantModel).where(TenantModel.slug == slug)
    )).scalar_one_or_none()
    if not tenant:
        raise HTTPException(404, f"Portal '{slug}' not found")

    case = (await session.execute(
        select(CaseInstanceModel).where(
            CaseInstanceModel.id == case_id,
            CaseInstanceModel.portal_submitter_email == email,
        )
    )).scalar_one_or_none()
    if not case:
        raise HTTPException(404, "Case not found or email does not match")

    # Get the most recent active SLA instance for this case
    sla = (await session.execute(
        select(CaseSLAInstanceModel)
        .where(CaseSLAInstanceModel.case_id == case_id)
        .order_by(CaseSLAInstanceModel.started_at.desc())
        .limit(1)
    )).scalar_one_or_none()

    if sla is None:
        return {"case_id": str(case_id), "sla": None}

    now = datetime.now(tz.utc)
    deadline = sla.deadline_at
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=tz.utc)

    total_seconds = (deadline - sla.started_at.replace(tzinfo=tz.utc)).total_seconds()
    remaining_seconds = (deadline - now).total_seconds()
    pct_remaining = max(0.0, remaining_seconds / total_seconds) if total_seconds > 0 else 0.0

    if sla.status == "breached" or remaining_seconds <= 0:
        tier = "red"
    elif pct_remaining < 0.20:
        tier = "amber"
    else:
        tier = "green"

    return {
        "case_id": str(case_id),
        "sla": {
            "deadline_at":        deadline.isoformat(),
            "status":             sla.status,
            "tier":               tier,
            "remaining_seconds":  max(0, int(remaining_seconds)),
            "breached":           sla.status == "breached" or remaining_seconds <= 0,
            "breached_at":        sla.breached_at.isoformat() if sla.breached_at else None,
        },
    }


# ─── Public: Pre-submission RAG self-service (P39c) ──────────────

class AskRequest(BaseModel):
    question: str

_ASK_SYSTEM = (
    "You are a helpful customer support assistant for a business portal. "
    "A customer is about to submit a support request. "
    "Answer their question concisely and helpfully using common knowledge about the service. "
    "If you cannot answer confidently, say so and encourage them to submit a request."
)


@public_router.post("/{slug}/ask")
async def portal_ask(
    slug: str,
    body: AskRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """RAG self-service before submission — rate-limited 10/hour per IP.

    Attempts to answer the customer's question without creating a ticket.
    Falls back gracefully when AI is unavailable.
    """
    tenant = (await session.execute(
        select(TenantModel).where(TenantModel.slug == slug)
    )).scalar_one_or_none()
    if not tenant:
        raise HTTPException(404, f"Portal '{slug}' not found")

    ip = request.client.host if request.client else "unknown"
    if not await _rate_check(f"ask:{ip}", max_calls=10, window_seconds=3600):
        raise HTTPException(429, "Too many requests — please try again later")

    if not await check_ai_available():
        return {
            "answer": "Our AI assistant is not available right now. Please submit your request and our team will help you.",
            "self_served": False,
            "ai_available": False,
        }

    llm = get_llm_backend()
    try:
        answer = await llm.complete(body.question, system=_ASK_SYSTEM, temperature=0.3)
        return {
            "answer": answer or "I couldn't find a confident answer. Please submit your request.",
            "self_served": True,
            "ai_available": True,
        }
    except Exception:
        return {
            "answer": "I couldn't find an answer right now. Please submit your request and we'll get back to you.",
            "self_served": False,
            "ai_available": True,
        }


# ─── Public: Case-scoped AI chat (P39c) ──────────────────────────

class ChatRequest(BaseModel):
    message: str

_CASE_CHAT_SYSTEM = (
    "You are HxNexus, an AI assistant embedded in a customer support portal. "
    "You are helping a customer understand the status of their support request. "
    "Only answer questions about their case using the context provided. "
    "Be reassuring, concise, and professional. "
    "Never fabricate information not present in the case context."
)


@public_router.post("/{slug}/cases/{case_id}/chat")
async def portal_case_chat(
    slug: str,
    case_id: uuid.UUID,
    body: ChatRequest,
    request: Request,
    email: str,
    session: AsyncSession = Depends(get_session),
):
    """Case-scoped AI chat — rate-limited 20/day per IP+case.

    Customer asks questions about their specific case.
    HxNexus answers using case status, stage, and timeline as context.
    """
    tenant = (await session.execute(
        select(TenantModel).where(TenantModel.slug == slug)
    )).scalar_one_or_none()
    if not tenant:
        raise HTTPException(404, f"Portal '{slug}' not found")

    case = (await session.execute(
        select(CaseInstanceModel).where(
            CaseInstanceModel.id == case_id,
            CaseInstanceModel.portal_submitter_email == email,
        )
    )).scalar_one_or_none()
    if not case:
        raise HTTPException(404, "Case not found or email does not match")

    ip = request.client.host if request.client else "unknown"
    if not await _rate_check(f"chat:{ip}:{case_id}", max_calls=20, window_seconds=86400):
        raise HTTPException(429, "Message limit reached for today — please try again tomorrow")

    if not await check_ai_available():
        return {
            "reply": "Our AI assistant is not available right now. Please contact support directly.",
            "ai_available": False,
        }

    # Build case context for the LLM
    ct = await session.get(CaseTypeModel, case.case_type_id)
    audit_rows = (await session.execute(
        select(CaseAuditLogModel)
        .where(CaseAuditLogModel.case_id == case_id)
        .order_by(CaseAuditLogModel.timestamp)
        .limit(20)
    )).scalars().all()

    visible_actions = {
        "case_created", "stage_transitioned", "status_changed",
        "document_uploaded", "case_resolved", "case_closed", "case_reopened",
    }
    timeline_lines = [
        f"- {row.action.replace('_', ' ').title()} at {row.timestamp.strftime('%Y-%m-%d %H:%M') if row.timestamp else 'unknown'}"
        for row in audit_rows if row.action in visible_actions
    ]

    cvars = (await _case_vars_for(session, [case.id]))[case.id]
    case_context = (
        f"Case subject: {cvars.get('subject', 'Support request')}\n"
        f"Case type: {ct.name if ct else 'Unknown'}\n"
        f"Current status: {case.status}\n"
        f"Priority: {case.priority}\n"
        f"Current stage: {case.current_stage_id or 'Not yet assigned'}\n"
        f"Submitted: {case.created_at.strftime('%Y-%m-%d') if case.created_at else 'Unknown'}\n"
        + (f"Recent activity:\n" + "\n".join(timeline_lines) if timeline_lines else "No activity yet")
    )

    prompt = f"Case context:\n{case_context}\n\nCustomer question: {body.message}"

    llm = get_llm_backend()
    try:
        reply = await llm.complete(prompt, system=_CASE_CHAT_SYSTEM, temperature=0.3)
        return {
            "reply": reply or "I'm not sure. Please contact our support team directly.",
            "ai_available": True,
        }
    except Exception:
        return {
            "reply": "I couldn't process your question right now. Please try again or contact support.",
            "ai_available": True,
        }


# ─── SD-5 + SD-2: Bank details OTP flow ─────────────────────────


@public_router.post("/{slug}/cases/{case_id}/bank-details/request-otp")
async def portal_request_bank_otp(
    slug: str,
    case_id: uuid.UUID,
    body: BankOtpRequestBody,
    session: AsyncSession = Depends(get_session),
):
    """SD-5: Issue a 6-digit OTP to the case's submitter email for bank-detail ownership verification.

    Rate limit: 3 attempts per case per 24 hours.

    Anti-enumeration: always returns the same response shape regardless of whether
    the email matches the case, so this endpoint cannot be used to probe submitter
    addresses. The OTP is only stored (and email sent) when the email matches.
    """
    # Rate limit applied BEFORE the email check to prevent timing-based enumeration
    if not await _rate_check(f"bank_otp:{case_id}", max_calls=3, window_seconds=86400):
        raise HTTPException(429, "OTP request limit reached. Please try again after 24 hours.")

    # Verify tenant exists
    tenant = (await session.execute(
        select(TenantModel).where(TenantModel.slug == slug)
    )).scalar_one_or_none()
    if not tenant:
        raise HTTPException(404, f"Portal '{slug}' not found")

    case = (await session.execute(
        select(CaseInstanceModel).where(CaseInstanceModel.id == case_id)
    )).scalar_one_or_none()

    # Validate email matches — but return identical response to prevent enumeration
    email_matches = (
        case is not None
        and case.portal_submitter_email is not None
        and case.portal_submitter_email.lower() == str(body.email).lower()
    )

    if email_matches and case is not None:
        otp = "".join([str(secrets.randbelow(10)) for _ in range(6)])
        _store_portal_otp(str(case_id), otp)

        from case_service.db.models import EmailAccountModel
        from case_service.mail import EmailService

        try:
            account = (await session.execute(
                select(EmailAccountModel).where(
                    EmailAccountModel.is_default_outbound.is_(True),
                    EmailAccountModel.is_active.is_(True),
                ).limit(1)
            )).scalar_one_or_none()

            if account:
                svc = EmailService()
                await svc.send(
                    session,
                    case_id=case_id,
                    account=account,
                    to_addresses=[str(body.email)],
                    subject="Your secure verification code",
                    body_text=(
                        f"Your one-time verification code is: {otp}\n\n"
                        "This code expires in 15 minutes and can only be used once.\n"
                        "Do not share this code with anyone."
                    ),
                )
                await session.commit()
            else:
                # No SMTP account configured — log OTP for development visibility.
                # TODO: configure a default outbound email account in production.
                log.warning(
                    "SD-5: No default outbound email account configured. "
                    "OTP for case %s: %s (development fallback — do not use in production)",
                    case_id,
                    otp,
                )
        except Exception as exc:
            # Email delivery failure must not prevent the stored OTP from being usable —
            # the customer can retry and the OTP is already in-memory.
            log.warning("SD-5: OTP email delivery failed for case %s: %s", case_id, exc)

    # Uniform response — never indicate whether email matched
    return {"ok": True, "message": "If your email matches this case, a verification code has been sent."}


@public_router.post("/{slug}/cases/{case_id}/bank-details/submit")
async def portal_submit_bank_details(
    slug: str,
    case_id: uuid.UUID,
    body: BankDetailsSubmitBody,
    session: AsyncSession = Depends(get_session),
):
    """SD-2: Accept verified bank details and store encrypted against the pending disbursement.

    Validates: OTP, email ownership, pending disbursement existence.
    Stores: encrypted account_name|account_number|sort_code in bank_reference (SD-1 scheme).
    Creates: audit log entry for forensic trail.
    Rate limit: 10 OTP submission attempts per case per hour (brute-force protection).
    """
    # Rate-limit independently of request-otp to prevent OTP brute-force
    if not await _rate_check(f"bank_otp_submit:{case_id}", max_calls=10, window_seconds=3600):
        raise HTTPException(429, "Too many attempts. Please request a new OTP and try again.")

    # Verify tenant
    tenant = (await session.execute(
        select(TenantModel).where(TenantModel.slug == slug)
    )).scalar_one_or_none()
    if not tenant:
        raise HTTPException(404, f"Portal '{slug}' not found")

    # Verify case + email ownership in a single query
    case = (await session.execute(
        select(CaseInstanceModel).where(
            CaseInstanceModel.id == case_id,
            CaseInstanceModel.portal_submitter_email == str(body.email),
        )
    )).scalar_one_or_none()
    if not case:
        raise HTTPException(403, "Email does not match this case or case not found.")

    # Verify OTP — single-use, constant-time comparison, expiry enforced inside helper
    if not _verify_portal_otp(str(case_id), body.otp):
        raise HTTPException(400, "Invalid or expired verification code.")

    # Validate field content — reject blank values before encryption
    account_name   = body.account_name.strip()
    account_number = body.account_number.strip()
    sort_code      = body.sort_code.strip()
    if not account_name or not account_number or not sort_code:
        raise HTTPException(400, "All bank detail fields are required.")

    # Find the pending disbursement for this case (no bank_reference yet, not confirmed)
    disbursement = (await session.execute(
        select(PaymentDisbursementModel).where(
            PaymentDisbursementModel.case_id == case_id,
            PaymentDisbursementModel.status != "confirmed",
            PaymentDisbursementModel.bank_reference.is_(None),
        ).order_by(PaymentDisbursementModel.created_at.desc()).limit(1)
    )).scalar_one_or_none()

    if not disbursement:
        raise HTTPException(404, "No pending payment disbursement found for this case.")

    # SD-1: encrypt using the existing hxv1: envelope scheme — one source of truth
    disbursement.bank_reference = _encrypt_bank_ref(
        f"{account_name}|{account_number}|{sort_code}"
    )
    disbursement.updated_at = datetime.now(timezone.utc)

    # Audit every portal bank-detail submission — immutable forensic record
    session.add(CaseAuditLogModel(
        case_id=case_id,
        action="bank_reference_submitted_via_portal",
        actor=f"portal:{body.email}",
        details={
            "disbursement_id": str(disbursement.id),
            "step_id": disbursement.step_id,
        },
    ))

    await session.commit()
    return {"ok": True}


# ─── Admin: List portal-enabled tenants ──────────────────────────

@admin_router.get("/tenants")
async def list_portal_tenants(
    _: AuthenticatedUser = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_session),
):
    tenants = (await session.execute(select(TenantModel))).scalars().all()
    result = []
    for t in tenants:
        ps = _portal_settings(t)
        ct_count = (await session.execute(
            select(CaseTypeModel).where(CaseTypeModel.portal_enabled == True)  # noqa: E712
        )).scalars().all()
        result.append({
            "id": str(t.id),
            "slug": t.slug,
            "name": t.name,
            "portal_enabled": ps.get("enabled", False),
            "welcome_text": ps.get("welcome_text", ""),
            "brand_color": ps.get("brand_color", "#6366f1"),
            "logo_text": ps.get("logo_text", t.name),
            "allowed_case_type_ids": ps.get("allowed_case_type_ids", []),
            "portal_case_type_count": len(ct_count),
        })
    return result


@admin_router.patch("/tenants/{slug}")
async def update_portal_settings(
    slug: str,
    body: PortalSettingsUpdate,
    _: AuthenticatedUser = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_session),
):
    tenant = (await session.execute(
        select(TenantModel).where(TenantModel.slug == slug)
    )).scalar_one_or_none()
    if not tenant:
        raise HTTPException(404, f"Tenant '{slug}' not found")

    settings = dict(tenant.settings or {})
    portal = dict(settings.get("portal", {}))

    if body.enabled is not None:
        portal["enabled"] = body.enabled
    if body.welcome_text is not None:
        portal["welcome_text"] = body.welcome_text
    if body.brand_color is not None:
        portal["brand_color"] = body.brand_color
    if body.logo_text is not None:
        portal["logo_text"] = body.logo_text
    if body.allowed_case_type_ids is not None:
        portal["allowed_case_type_ids"] = body.allowed_case_type_ids

    settings["portal"] = portal
    tenant.settings = settings
    await session.commit()
    return {"slug": slug, "portal": portal}


@admin_router.get("/submissions")
async def list_portal_submissions(
    slug: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    _: AuthenticatedUser = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_session),
):
    q = select(CaseInstanceModel).where(
        CaseInstanceModel.portal_tracking_token.is_not(None)
    ).order_by(CaseInstanceModel.created_at.desc()).limit(limit)

    if status:
        q = q.where(CaseInstanceModel.status == status)

    cases = (await session.execute(q)).scalars().all()

    result = []
    vars_by_case = await _case_vars_for(session, [c.id for c in cases])
    for c in cases:
        if slug and (c.extra_metadata or {}).get("portal_slug") != slug:
            continue
        ct = await session.get(CaseTypeModel, c.case_type_id)
        result.append({
            "case_id": str(c.id),
            "tracking_token": str(c.portal_tracking_token),
            "submitter_name": c.portal_submitter_name,
            "submitter_email": c.portal_submitter_email,
            "subject": vars_by_case.get(c.id, {}).get("subject", ""),
            "status": c.status,
            "priority": c.priority,
            "case_type_name": ct.name if ct else "Unknown",
            "portal_slug": (c.extra_metadata or {}).get("portal_slug"),
            "submitted_at": c.created_at,
        })
    return result


@admin_router.patch("/case-types/{case_type_id}/portal")
async def toggle_case_type_portal(
    case_type_id: uuid.UUID,
    enabled: bool,
    _: AuthenticatedUser = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_session),
):
    ct = await session.get(CaseTypeModel, case_type_id)
    if not ct:
        raise HTTPException(404, "Case type not found")
    ct.portal_enabled = enabled
    await session.commit()
    return {"case_type_id": str(case_type_id), "portal_enabled": enabled}


# ═══ Portal v2 (P1) — customer-JWT case endpoints ═══════════════════
# Logged-in customers (P65 accounts) reach their cases through the
# portal_customer_cases link, not the email query param. Handlers delegate to
# the email-verified endpoints above using the CASE's own submitter email —
# authorization happens here (link check), the inner email check then always
# passes, and the two paths can never drift apart.

from case_service.api.routers.portal_customers import _require_customer  # noqa: E402
from case_service.db.models import (  # noqa: E402
    CaseSessionModel,
    PortalCustomerCaseLinkModel,
    PortalCustomerModel,
)


async def _linked_case(
    slug: str,
    case_id: uuid.UUID,
    customer: PortalCustomerModel,
    session: AsyncSession,
) -> CaseInstanceModel:
    """The customer's case, authorized by the account↔case link.

    Uniform 404 for missing case / foreign case / no link — no oracle for
    probing other customers' case ids.
    """
    link = (await session.execute(
        select(PortalCustomerCaseLinkModel).where(
            PortalCustomerCaseLinkModel.customer_id == customer.id,
            PortalCustomerCaseLinkModel.case_id == case_id,
        )
    )).scalar_one_or_none()
    case = await session.get(CaseInstanceModel, case_id) if link else None
    if not case:
        raise HTTPException(404, "Case not found")
    return case


def _portal_display(ct: CaseTypeModel | None) -> dict:
    """Per-case-type customer-facing display config (definition_json['portal'])."""
    return ((ct.definition_json or {}).get("portal", {}) if ct else {}) or {}


def _stage_rail(ct: CaseTypeModel | None, case: CaseInstanceModel) -> list[dict]:
    """Ordered stages with customer-friendly labels — internal names never
    leak when a label map is configured; unmapped stages fall back to the
    designer's stage name (already human text, unlike stage ids)."""
    stages = sorted(((ct.definition_json or {}).get("stages", []) if ct else []),
                    key=lambda s: s.get("order", 0))
    labels = _portal_display(ct).get("stage_labels", {}) or {}
    current_order = next((s.get("order", 0) for s in stages
                          if s.get("id") == case.current_stage_id), None)
    done = case.status in ("resolved", "closed")
    rail = []
    for s in stages:
        sid = s.get("id", "")
        rail.append({
            "id":      sid,
            "label":   labels.get(sid) or s.get("name") or sid.replace("_", " ").title(),
            "current": (not done) and sid == case.current_stage_id,
            "reached": done or (current_order is not None and s.get("order", 0) <= current_order),
        })
    return rail


@public_router.get("/{slug}/account/cases/{case_id}")
async def account_case_detail(
    slug: str,
    case_id: uuid.UUID,
    customer: PortalCustomerModel = Depends(_require_customer),
    session: AsyncSession = Depends(get_session),
):
    """Everything the case-detail page needs in one call: status, friendly
    stage rail, SLA summary, and expected duration."""
    case = await _linked_case(slug, case_id, customer, session)
    ct = await session.get(CaseTypeModel, case.case_type_id)
    cvars = (await _case_vars_for(session, [case.id]))[case.id]

    sla_resp = await portal_case_sla(
        slug, case_id, email=case.portal_submitter_email, session=session)

    return {
        "case_id":        str(case.id),
        "case_number":    case.case_number,
        "tracking_token": str(case.portal_tracking_token) if case.portal_tracking_token else None,
        "subject":        cvars.get("subject", "Your request"),
        "description":    cvars.get("description", ""),
        "status":         case.status,
        "priority":       case.priority,
        "case_type_name": ct.name if ct else "Unknown",
        "submitted_at":   case.created_at.isoformat() if case.created_at else None,
        "updated_at":     case.updated_at.isoformat() if case.updated_at else None,
        "resolved_at":    case.resolved_at.isoformat() if case.resolved_at else None,
        "stage_rail":     _stage_rail(ct, case),
        "expected_days":  _portal_display(ct).get("expected_days"),
        "sla":            sla_resp.get("sla"),
    }


@public_router.get("/{slug}/account/cases/{case_id}/timeline")
async def account_case_timeline(
    slug: str,
    case_id: uuid.UUID,
    customer: PortalCustomerModel = Depends(_require_customer),
    session: AsyncSession = Depends(get_session),
):
    case = await _linked_case(slug, case_id, customer, session)
    payload = await case_timeline(
        slug, case_id, email=case.portal_submitter_email, session=session)
    # Friendly stage labels in transition entries
    ct = await session.get(CaseTypeModel, case.case_type_id)
    labels = _portal_display(ct).get("stage_labels", {}) or {}
    names = {s.get("id"): s.get("name") for s in ((ct.definition_json or {}).get("stages", []) if ct else [])}
    for entry in payload.get("timeline", []):
        sid = entry.get("details", {}).get("stage")
        if sid:
            entry["details"]["stage_label"] = labels.get(sid) or names.get(sid) or sid.replace("_", " ").title()
    return payload


@public_router.get("/{slug}/account/cases/{case_id}/documents")
async def account_case_documents(
    slug: str,
    case_id: uuid.UUID,
    customer: PortalCustomerModel = Depends(_require_customer),
    session: AsyncSession = Depends(get_session),
):
    case = await _linked_case(slug, case_id, customer, session)
    return await portal_case_documents(
        slug, case_id, email=case.portal_submitter_email, session=session)


@public_router.post("/{slug}/account/cases/{case_id}/documents")
async def account_upload_document(
    slug: str,
    case_id: uuid.UUID,
    file: UploadFile = File(...),
    customer: PortalCustomerModel = Depends(_require_customer),
    session: AsyncSession = Depends(get_session),
):
    case = await _linked_case(slug, case_id, customer, session)
    if not case.portal_tracking_token:
        raise HTTPException(400, "This case does not accept portal uploads")
    return await portal_upload_document(
        slug, case.portal_tracking_token, file=file, session=session)


@public_router.post("/{slug}/account/cases/{case_id}/chat")
async def account_case_chat(
    slug: str,
    case_id: uuid.UUID,
    body: ChatRequest,
    request: Request,
    customer: PortalCustomerModel = Depends(_require_customer),
    session: AsyncSession = Depends(get_session),
):
    case = await _linked_case(slug, case_id, customer, session)
    return await portal_case_chat(
        slug, case_id, body, request,
        email=case.portal_submitter_email, session=session)


# ─── Portal v2: HxMeet sessions for logged-in customers ──────────
# A customer sees/joins ONLY sessions they were invited to (participant row
# for customer:{id} exists) — internal-only sessions on their case stay
# invisible. Unlike the emailed single-use link, this path keeps working for
# the whole life of the active session.

@public_router.get("/{slug}/account/cases/{case_id}/sessions")
async def account_case_sessions(
    slug: str,
    case_id: uuid.UUID,
    customer: PortalCustomerModel = Depends(_require_customer),
    session: AsyncSession = Depends(get_session),
):
    from case_service.db.models import CaseSessionParticipantModel
    case = await _linked_case(slug, case_id, customer, session)
    identity = f"customer:{customer.id}"
    rows = (await session.execute(
        select(CaseSessionModel)
        .join(CaseSessionParticipantModel,
              CaseSessionParticipantModel.session_id == CaseSessionModel.id)
        .where(
            CaseSessionModel.case_id == case.id,
            CaseSessionModel.status == "active",
            CaseSessionModel.driver == "embedded",
            CaseSessionParticipantModel.identity == identity,
        )
        .order_by(CaseSessionModel.started_at.desc())
    )).scalars().all()
    return {"sessions": [
        {
            "session_id":    str(s.id),
            "title":         s.title,
            "record_intent": s.record_intent,
            "started_at":    s.started_at.isoformat() if s.started_at else None,
        }
        for s in rows
    ]}


@public_router.post("/{slug}/account/cases/{case_id}/sessions/{session_id}/token")
async def account_session_token(
    slug: str,
    case_id: uuid.UUID,
    session_id: uuid.UUID,
    customer: PortalCustomerModel = Depends(_require_customer),
    session: AsyncSession = Depends(get_session),
):
    """Room token for an invited, logged-in customer. The portal shows the
    recording notice first — requesting the token is the consent act."""
    from case_service.meet import service as meet_service
    case = await _linked_case(slug, case_id, customer, session)
    row = (await session.execute(
        select(CaseSessionModel).where(
            CaseSessionModel.id == session_id,
            CaseSessionModel.case_id == case.id,
        )
    )).scalar_one_or_none()
    if row is None or row.status != "active" or row.driver != "embedded":
        raise HTTPException(404, "Session not found")
    try:
        return await meet_service.join_customer(
            session, row=row, customer_id=customer.id,
            display_name=customer.display_name)
    except ValueError:
        raise HTTPException(404, "Session not found")   # not invited — same 404


# ─── Portal v2: admin — customer-facing display config ───────────

class PortalDisplayUpdate(BaseModel):
    stage_labels: Optional[dict[str, str]] = None   # stage_id → customer label
    expected_days: Optional[int] = None             # typical end-to-end duration
    form_id: Optional[str] = None                   # P5: Form Builder form for portal submit ("" clears)


@admin_router.get("/case-types/{case_type_id}/portal-display")
async def get_case_type_portal_display(
    case_type_id: uuid.UUID,
    _: AuthenticatedUser = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_session),
):
    ct = await session.get(CaseTypeModel, case_type_id)
    if not ct:
        raise HTTPException(404, "Case type not found")
    display = _portal_display(ct)
    return {
        "case_type_id": str(case_type_id),
        "stages": [
            {"id": s.get("id"), "name": s.get("name"), "order": s.get("order", 0)}
            for s in sorted((ct.definition_json or {}).get("stages", []),
                            key=lambda s: s.get("order", 0))
        ],
        "stage_labels":  display.get("stage_labels", {}),
        "expected_days": display.get("expected_days"),
    }


@admin_router.patch("/case-types/{case_type_id}/portal-display")
async def update_case_type_portal_display(
    case_type_id: uuid.UUID,
    body: PortalDisplayUpdate,
    _: AuthenticatedUser = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_session),
):
    ct = await session.get(CaseTypeModel, case_type_id)
    if not ct:
        raise HTTPException(404, "Case type not found")
    valid_ids = {s.get("id") for s in (ct.definition_json or {}).get("stages", [])}
    display = dict(_portal_display(ct))
    if body.stage_labels is not None:
        unknown = set(body.stage_labels) - valid_ids
        if unknown:
            raise HTTPException(400, f"Unknown stage ids: {sorted(unknown)}")
        display["stage_labels"] = {k: v.strip() for k, v in body.stage_labels.items() if v.strip()}
    if body.expected_days is not None:
        if body.expected_days < 0:
            raise HTTPException(400, "expected_days must be >= 0")
        display["expected_days"] = body.expected_days or None
    if body.form_id is not None:
        if body.form_id == "":
            display.pop("form_id", None)
        else:
            from case_service.db.models import FormDefinitionModel
            try:
                form = await session.get(FormDefinitionModel, uuid.UUID(body.form_id))
            except ValueError:
                form = None
            if form is None:
                raise HTTPException(400, "Unknown form_id")
            display["form_id"] = body.form_id
    # Reassign (not mutate) so the JSON column change is tracked
    ct.definition_json = {**(ct.definition_json or {}), "portal": display}
    await session.commit()
    return {"case_type_id": str(case_type_id), "portal": display}


# ═══ Portal v2 (P3) — customer workflow steps ("Action needed") ═════
# A step whose assignment.strategy is "customer" belongs to the case's
# customer, not a worker. The portal surfaces it (approval / form / document
# prompt), the customer completes it with their customer JWT, and the same
# auto-advance used by worker completions moves the stage on. A required
# customer step therefore blocks the stage until the customer acts — by
# construction, not by extra machinery.

def _customer_steps(ct: CaseTypeModel | None, stage_id: str | None) -> list[dict]:
    """Customer-assigned steps of one stage from the case-type definition."""
    if not ct or not stage_id:
        return []
    stage = next((s for s in (ct.definition_json or {}).get("stages", [])
                  if s.get("id") == stage_id), None)
    if not stage:
        return []
    return [
        s for s in stage.get("steps", [])
        if (s.get("assignment") or {}).get("strategy") == "customer"
    ]


@public_router.get("/{slug}/account/cases/{case_id}/actions")
async def account_case_actions(
    slug: str,
    case_id: uuid.UUID,
    customer: PortalCustomerModel = Depends(_require_customer),
    session: AsyncSession = Depends(get_session),
):
    """Pending customer actions on the case's CURRENT stage."""
    from case_service.db.models import CaseStepCompletionModel
    case = await _linked_case(slug, case_id, customer, session)
    ct = await session.get(CaseTypeModel, case.case_type_id)
    steps = _customer_steps(ct, case.current_stage_id)
    if not steps or case.status in ("resolved", "closed", "cancelled"):
        return {"actions": []}

    done = {
        row.step_id for row in (await session.execute(
            select(CaseStepCompletionModel).where(
                CaseStepCompletionModel.case_id == case.id,
                CaseStepCompletionModel.stage_id == case.current_stage_id,
            )
        )).scalars().all()
    }
    actions = []
    for s in steps:
        if s.get("id") in done:
            continue
        ca = s.get("customer_action") or {}
        actions.append({
            "step_id":     s.get("id"),
            "name":        s.get("name") or s.get("id", "").replace("_", " ").title(),
            "type":        ca.get("type", "approval"),   # approval | form | document
            "prompt":      ca.get("prompt", ""),
            "form_fields": ca.get("form_fields", []),    # [{key,label,type}]
            "required":    s.get("required", True),
        })
    return {"actions": actions}


class CustomerActionBody(BaseModel):
    decision: Optional[str] = None          # approval: "approved" | "rejected"
    data: dict[str, Any] = {}               # form answers / free comment
    comment: Optional[str] = None


@public_router.post("/{slug}/account/cases/{case_id}/actions/{step_id}/complete")
async def account_complete_action(
    slug: str,
    case_id: uuid.UUID,
    step_id: str,
    body: CustomerActionBody,
    customer: PortalCustomerModel = Depends(_require_customer),
    session: AsyncSession = Depends(get_session),
):
    from case_service.db.models import CaseStepCompletionModel
    from case_service.api.routers.cases import _audit, _auto_advance_if_complete

    case = await _linked_case(slug, case_id, customer, session)
    if case.status in ("resolved", "closed", "cancelled"):
        raise HTTPException(400, "This case is closed")
    ct = await session.get(CaseTypeModel, case.case_type_id)
    step = next((s for s in _customer_steps(ct, case.current_stage_id)
                 if s.get("id") == step_id), None)
    if step is None:
        # Not a customer step / not in the current stage — uniform 404.
        raise HTTPException(404, "Action not found")

    existing = (await session.execute(
        select(CaseStepCompletionModel).where(
            CaseStepCompletionModel.case_id == case.id,
            CaseStepCompletionModel.step_id == step_id,
        )
    )).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(409, "This action has already been completed")

    ca = step.get("customer_action") or {}
    if ca.get("type", "approval") == "approval":
        if body.decision not in ("approved", "rejected"):
            raise HTTPException(400, "decision must be 'approved' or 'rejected'")
        status = "completed" if body.decision == "approved" else "rejected"
    else:
        missing = [f["key"] for f in ca.get("form_fields", [])
                   if f.get("required", True) and not str(body.data.get(f.get("key", ""), "")).strip()]
        if missing:
            raise HTTPException(400, f"Missing required fields: {missing}")
        status = "completed"

    actor = f"customer:{customer.id}"
    session.add(CaseStepCompletionModel(
        case_id=case.id, stage_id=case.current_stage_id, step_id=step_id,
        step_type=step.get("step_type", "user_task"), status=status,
        data={**body.data,
              **({"decision": body.decision} if body.decision else {}),
              **({"comment": body.comment} if body.comment else {}),
              "source": "customer_portal"},
        completed_by=actor,
    ))
    await session.flush()
    await _audit(session, case.id, "step_completed", actor_id=actor,
                 details={"step_id": step_id, "stage_id": case.current_stage_id,
                          "status": status, "source": "customer_portal"})

    # Parity with the worker complete_step path: fire step_complete connector
    # rules. (Stage-level SLA start + stage_enter/exit rules are handled INSIDE
    # _auto_advance_if_complete below, so both callers get them.) We do NOT nudge
    # the Temporal SLA companion here — that _signal_lifecycle hop needs the
    # worker request context; a customer action that advances the stage still
    # (re)starts the next stage's SLA via auto-advance, which is what matters.
    if status == "completed":
        from case_service.api.routers.cases import fire_outbound_rules
        try:
            await fire_outbound_rules(
                session, trigger_event="step_complete",
                case_id=case.id, case_type_id=case.case_type_id,
                case_data=await case_vars.get_all(
                    session,
                    case_vars.CallerContext(kind="rules", actor_id="outbound-rules"),
                    case.id),
                tenant_id="default",
            )
        except Exception:
            log.warning("step_complete outbound rules failed for customer action %s/%s",
                        case.id, step_id)

    auto_advanced = False
    if status == "completed" and case.status in ("open", "new", "reopened"):
        new_stage = await _auto_advance_if_complete(
            session, case.id, case.current_stage_id, ct.definition_json or {})
        auto_advanced = new_stage is not None
    await session.commit()

    return {"step_id": step_id, "status": status, "auto_advanced": auto_advanced}


# ═══ Portal v2 (P4) — case messages, customer side ══════════════════
# Same thread as the worker router (messages.py); the portal only ever sees
# portal_visible rows. A customer post needs no email notification fan-out —
# workers live in Studio where the thread is on the case.

@public_router.get("/{slug}/account/cases/{case_id}/messages")
async def account_case_messages(
    slug: str,
    case_id: uuid.UUID,
    customer: PortalCustomerModel = Depends(_require_customer),
    session: AsyncSession = Depends(get_session),
):
    from case_service.db.models import CaseMessageModel
    case = await _linked_case(slug, case_id, customer, session)
    rows = (await session.execute(
        select(CaseMessageModel).where(
            CaseMessageModel.case_id == case.id,
            CaseMessageModel.portal_visible.is_(True),
        ).order_by(CaseMessageModel.created_at)
    )).scalars().all()
    me = f"customer:{customer.id}"
    return {"messages": [
        {
            "id":          str(m.id),
            "author_name": m.author_name if m.author != me else "You",
            "mine":        m.author == me,
            "body":        m.body,
            "created_at":  m.created_at.isoformat() if m.created_at else None,
        }
        for m in rows
    ]}


class CustomerMessageBody(BaseModel):
    body: str


@public_router.post("/{slug}/account/cases/{case_id}/messages", status_code=201)
async def account_post_message(
    slug: str,
    case_id: uuid.UUID,
    body: CustomerMessageBody,
    request: Request,
    customer: PortalCustomerModel = Depends(_require_customer),
    session: AsyncSession = Depends(get_session),
):
    from case_service.db.models import CaseMessageModel
    text = (body.body or "").strip()
    if not text or len(text) > 10_000:
        raise HTTPException(400, "Message must be 1–10000 characters")
    case = await _linked_case(slug, case_id, customer, session)
    if case.status in ("closed", "cancelled"):
        raise HTTPException(400, "This case is closed")
    ip = request.client.host if request.client else "unknown"
    if not await _rate_check(f"msg:{ip}:{case_id}", max_calls=30, window_seconds=3600):
        raise HTTPException(429, "Message limit reached — please try again later")
    msg = CaseMessageModel(
        case_id=case.id,
        author=f"customer:{customer.id}",
        author_name=customer.display_name,
        body=text,
        portal_visible=True,
    )
    session.add(msg)
    await session.commit()
    await session.refresh(msg)
    return {"id": str(msg.id), "created_at": msg.created_at.isoformat()}


# ═══ Portal v2 (P5) — CSAT, deflection feedback, logo, real forms ═══

class CsatBody(BaseModel):
    rating: int
    comment: Optional[str] = None


@public_router.post("/{slug}/account/cases/{case_id}/csat", status_code=201)
async def account_case_csat(
    slug: str,
    case_id: uuid.UUID,
    body: CsatBody,
    customer: PortalCustomerModel = Depends(_require_customer),
    session: AsyncSession = Depends(get_session),
):
    """One rating per case, only after resolution."""
    from case_service.db.models import PortalCsatModel
    if not 1 <= body.rating <= 5:
        raise HTTPException(400, "rating must be 1–5")
    case = await _linked_case(slug, case_id, customer, session)
    if case.status not in ("resolved", "closed"):
        raise HTTPException(400, "You can rate a request once it is resolved")
    if await session.get(PortalCsatModel, case.id) is not None:
        raise HTTPException(409, "This request has already been rated")
    session.add(PortalCsatModel(
        case_id=case.id, customer_id=customer.id,
        rating=body.rating, comment=(body.comment or "").strip() or None,
    ))
    await session.commit()
    return {"ok": True}


@public_router.get("/{slug}/account/cases/{case_id}/csat")
async def account_case_csat_status(
    slug: str,
    case_id: uuid.UUID,
    customer: PortalCustomerModel = Depends(_require_customer),
    session: AsyncSession = Depends(get_session),
):
    from case_service.db.models import PortalCsatModel
    case = await _linked_case(slug, case_id, customer, session)
    row = await session.get(PortalCsatModel, case.id)
    return {"rated": row is not None, "rating": row.rating if row else None,
            "can_rate": row is None and case.status in ("resolved", "closed")}


class AskFeedbackBody(BaseModel):
    question: str
    helpful: bool


@public_router.post("/{slug}/ask/feedback", status_code=201)
async def portal_ask_feedback(
    slug: str,
    body: AskFeedbackBody,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Deflection tracking: did the pre-submit AI answer make the ticket
    unnecessary? Anonymous by design — rate-limited per IP."""
    from case_service.db.models import PortalAskFeedbackModel
    tenant = (await session.execute(
        select(TenantModel).where(TenantModel.slug == slug)
    )).scalar_one_or_none()
    if not tenant:
        raise HTTPException(404, f"Portal '{slug}' not found")
    ip = request.client.host if request.client else "unknown"
    if not await _rate_check(f"askfb:{ip}", max_calls=20, window_seconds=3600):
        raise HTTPException(429, "Too many requests")
    q = (body.question or "").strip()[:2000]
    if not q:
        raise HTTPException(400, "question is required")
    session.add(PortalAskFeedbackModel(tenant_slug=slug, question=q, helpful=body.helpful))
    await session.commit()
    return {"ok": True}


# ─── P5: tenant logo (real image, not just logo_text) ────────────

_LOGO_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
_LOGO_MAX_BYTES = 1_000_000


@admin_router.post("/tenants/{slug}/logo")
async def upload_portal_logo(
    slug: str,
    file: UploadFile = File(...),
    _: AuthenticatedUser = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_session),
):
    tenant = (await session.execute(
        select(TenantModel).where(TenantModel.slug == slug)
    )).scalar_one_or_none()
    if not tenant:
        raise HTTPException(404, f"Tenant '{slug}' not found")
    ctype = (file.content_type or "").lower()
    if ctype not in _LOGO_TYPES:
        raise HTTPException(400, "Logo must be a PNG, JPEG, or WebP image")
    data = await file.read()
    if len(data) > _LOGO_MAX_BYTES:
        raise HTTPException(400, "Logo must be under 1 MB")
    # Magic-byte sanity: never trust the declared content type alone.
    if not (data[:8] == b"\x89PNG\r\n\x1a\n" or data[:3] == b"\xff\xd8\xff"
            or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")):
        raise HTTPException(400, "File content does not match an accepted image type")

    storage = get_storage_backend()
    key = f"portal/logo/{tenant.slug}.{_LOGO_TYPES[ctype]}"
    await storage.put(key, data, content_type=ctype)

    settings = dict(tenant.settings or {})
    portal = dict(settings.get("portal", {}))
    portal["logo_key"] = key
    portal["logo_content_type"] = ctype
    settings["portal"] = portal
    tenant.settings = settings
    await session.commit()
    return {"slug": slug, "logo_url": f"/api/v1/portal/{slug}/logo"}


@public_router.get("/{slug}/logo")
async def get_portal_logo(
    slug: str,
    session: AsyncSession = Depends(get_session),
):
    """Public logo stream — the portal page is public, so is its logo."""
    from fastapi.responses import Response
    tenant = (await session.execute(
        select(TenantModel).where(TenantModel.slug == slug)
    )).scalar_one_or_none()
    ps = _portal_settings(tenant) if tenant else {}
    key = ps.get("logo_key")
    if not tenant or not key:
        raise HTTPException(404, "No logo")
    storage = get_storage_backend()
    try:
        data = await storage.get(key)
    except Exception:
        raise HTTPException(404, "No logo")
    return Response(content=data, media_type=ps.get("logo_content_type", "image/png"),
                    headers={"Cache-Control": "public, max-age=300"})


# ─── P5: real Form Builder form on submit ─────────────────────────

@public_router.get("/{slug}/case-types/{case_type_id}/form")
async def portal_case_type_form(
    slug: str,
    case_type_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    """Flattened Form Builder fields for a portal-enabled case type, when the
    admin has attached one via portal-display (form_id). Only field metadata
    is served — never internal form config."""
    from case_service.db.models import FormDefinitionModel
    tenant = (await session.execute(
        select(TenantModel).where(TenantModel.slug == slug)
    )).scalar_one_or_none()
    if not tenant or not _portal_settings(tenant).get("enabled", False):
        raise HTTPException(404, "Portal not found")
    ct = await session.get(CaseTypeModel, case_type_id)
    if not ct or not ct.portal_enabled:
        raise HTTPException(404, "Case type not found")
    form_id = _portal_display(ct).get("form_id")
    if not form_id:
        return {"fields": []}
    form = await session.get(FormDefinitionModel, uuid.UUID(str(form_id)))
    if form is None:
        return {"fields": []}
    fields = []
    for section in (form.definition_json or {}).get("sections", []):
        for f in section.get("fields", []):
            fields.append({
                "key":         f.get("field_key") or f.get("id"),
                "label":       f.get("label", ""),
                "type":        f.get("type", "text"),
                "required":    f.get("required", False),
                "placeholder": f.get("placeholder", ""),
                "options":     f.get("options", []),
            })
    return {"fields": fields}
