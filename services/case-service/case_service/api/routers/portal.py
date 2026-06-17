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
    case_types: list[dict]


class PortalSubmitRequest(BaseModel):
    case_type_id: uuid.UUID
    submitter_name: str
    submitter_email: str
    subject: str
    description: str
    priority: str = "medium"
    extra_data: dict[str, Any] = {}


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
    await session.commit()

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
