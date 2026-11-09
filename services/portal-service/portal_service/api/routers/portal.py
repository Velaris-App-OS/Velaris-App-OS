"""Customer Portal API — P33 + P39a.

Public endpoints (no Velaris auth — accessible by external customers):
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

Error codes
-----------
53 of the 64 raise sites here carry a `VEL-PORTAL-*` catalog code. The portal
UI shows the code as an opaque reference and **never links to the catalog** —
most of these endpoints answer anonymous customers, who have no access to the
docs site. The code is for the support call that follows.

The 11 sites left bare are pure request-shape validation ("rating must be 1–5",
"Unknown form_id"). A code changes nothing about how those are resolved, and
the catalog's own rule is that a code which does not change the resolution is
noise.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger(__name__)

# ── Portal rate limits ──
# Shared by every copy of the service when the cache is on (velaris_shared/
# shared_state.py), so N copies no longer allow N times each limit — the one
# that matters most is the 6-digit OTP submit. Cache off: this process's own
# window, exactly as before.
async def _rate_check(key: str, max_calls: int, window_seconds: int) -> bool:
    """Return True if allowed, False if rate-limited."""
    from velaris_shared import shared_state
    return await shared_state.allow("portal-rl", key, max_calls, window_seconds)

from portal_service.tenant_open import refuse_closed_portal
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.auth.dependencies import get_current_user, require_role
from velaris_shared import hxguard
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.db.models import (
    CaseAuditLogModel,
    CaseInstanceModel,
    CaseSLAInstanceModel,
    CaseTypeModel,
    DocumentModel,
    PaymentDisbursementModel,
    TenantModel,
)
from velaris_shared.core.error_codes import (
    PORTAL_ACCESS_404,
    PORTAL_ACTION_409,
    PORTAL_UNAVAILABLE_503,
    PORTAL_CASETYPE_400,
    PORTAL_CLOSED_400,
    PORTAL_INACTIVE_403,
    PORTAL_NOTFOUND_404,
    PORTAL_DEFAULT_403,
    PORTAL_OTP_INVALID_400,
    PORTAL_OTP_RATE_429,
    PORTAL_RATE_LIMIT_429,
    PORTAL_UPLOAD_400,
    coded,
)
from velaris_shared.db.session import get_session
from velaris_shared.documents import access_tokens
from velaris_shared.storage import get_storage_backend
from velaris_shared.llm.factory import get_llm_backend, check_ai_available
from velaris_shared.core.bank_ref import _encrypt_bank_ref

logger = logging.getLogger(__name__)


async def _internal(path: str, payload: dict[str, Any], session: AsyncSession) -> dict:
    """Ask case-service (R5e step 8). A refusal keeps its status and text; a
    case-service that cannot be reached is a 503, never a silent success."""
    from velaris_shared.internal import client as internal

    try:
        return await internal.call(path, payload, session=session)
    except internal.InternalCallError as exc:
        if exc.status is None:
            log.warning("portal: %s", exc)
            raise coded(503, PORTAL_UNAVAILABLE_503,
                        "The service is temporarily unavailable. Please try again.") from None
        if exc.status == 422 or exc.status >= 500 and exc.status not in (501, 503):
            # A contract error or a fault inside case-service: its text names
            # internal fields and echoes the input (emails, answers), which a
            # public caller must not see and the log must not keep.
            log.error("portal: internal call %s answered %s", exc.path, exc.status)
            from velaris_shared.core.error_codes import SYS_UNHANDLED_500
            raise coded(500, SYS_UNHANDLED_500,
                        "The server hit an unexpected error. It has been logged; "
                        "your request was not applied.") from None
        raise HTTPException(exc.status, exc.detail) from None


async def _case_vars_for(
    session: AsyncSession, case_ids: list[uuid.UUID],
) -> dict[uuid.UUID, dict[str, Any]]:
    """Case data via the case_vars façade (portal context: pii/secret masked) —
    case-service's, read over the internal API."""
    if not case_ids:
        return {}
    got = await _internal("portal/case-vars", {"case_ids": case_ids[:500]}, session)
    return {uuid.UUID(k): v for k, v in got["vars"].items()}

# Two routers: one public, one admin-only
public_router = APIRouter(prefix="/portal", tags=["portal"], dependencies=[Depends(refuse_closed_portal)])
admin_router = APIRouter(prefix="/portal-admin", tags=["portal-admin"])


# ─── SD-5 OTP store ────────────────────────────────────────────────────────
# Keyed by case id; holds only the SHA-256 of the code, for 15 minutes. In the
# shared store (velaris_shared/shared_state.py): with the cache on, a code sent
# by one copy of the service is verified by any other, and survives a restart.
# Cache off: this process only, as before.

_OTP_TTL_SECONDS = 900


async def _store_portal_otp(case_id: str, otp: str) -> None:
    """Hash and store a 6-digit OTP for the given case_id. Expires in 15 minutes."""
    from velaris_shared import shared_state
    h = hashlib.sha256(otp.encode()).hexdigest()
    await shared_state.put("portal-otp", str(case_id), h, _OTP_TTL_SECONDS)


async def _verify_portal_otp(case_id: str, otp: str) -> bool:
    """Constant-time OTP verification, single-use.
    Returns True only when OTP is present, unexpired, and hash-matches.
    """
    from velaris_shared import shared_state
    h = await shared_state.get("portal-otp", str(case_id))
    if not h:
        return False                     # none issued, or expired
    # Constant-time comparison to prevent timing side-channel
    candidate = hashlib.sha256(otp.encode()).hexdigest()
    if not secrets.compare_digest(candidate, h):
        return False
    # Single-use: only the request that actually removes it succeeds, so two
    # copies receiving the same correct code at once cannot both accept it.
    taken = await shared_state.take("portal-otp", str(case_id))
    return taken is not None and secrets.compare_digest(taken, h)


# ─── Pydantic schemas ────────────────────────────────────────────

class PortalConfig(BaseModel):
    slug: str
    name: str
    welcome_text: str
    brand_color: str
    logo_text: str
    enabled: bool
    logo_url: Optional[str] = None   # P5: real uploaded logo
    #: Whether a customer may create their own account. Published because the
    #: sign-in screen has to know whether to offer it — an app that shows the
    #: option and then gets refused is worse than one that never showed it.
    self_registration: bool = True
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
    # Voice input P3b — recording a customer is the tenant's decision to make
    # and defend, so it is opt-in and silence means off.
    voice_input: Optional[bool] = None
    voice_consent_text: Optional[str] = None
    # Whether the public may create their own customer account. Off means staff
    # create them instead — see `POST /portal/{slug}/customers`, which is the
    # only other path to an account.
    self_registration: Optional[bool] = None


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


async def _portal_case_types(session: AsyncSession, tenant: TenantModel) -> list[CaseTypeModel]:
    """The case types this portal may show, and no other tenant's.

    `portal_enabled` says a case type is customer-facing; it does not say
    *whose* customers. Without the tenant rule the public, unauthenticated
    portal config listed another tenant's case type by name, and `submit`
    accepted it — a tenant boundary crossed from an endpoint that needs no
    credentials at all.

    A NULL `tenant_id` is platform-shared and stays visible to everyone, which
    is `same_tenant`'s documented rule rather than an exception invented here.
    """
    from velaris_shared.tenancy.identity import same_tenant

    rows = (await session.execute(
        select(CaseTypeModel).where(CaseTypeModel.portal_enabled == True)  # noqa: E712
    )).scalars().all()
    mine = [ct for ct in rows if await same_tenant(session, ct.tenant_id, tenant.id)]

    allowed_ids = _portal_settings(tenant).get("allowed_case_type_ids", [])
    if allowed_ids:
        mine = [ct for ct in mine if str(ct.id) in allowed_ids]
    return mine


async def _portal_case_type_or_refuse(
    session: AsyncSession, tenant: TenantModel, case_type_id,
) -> CaseTypeModel:
    """One case type, refused identically whether it is missing, not
    portal-enabled, not allowed here, or another tenant's — so the answer
    cannot be used to discover what exists elsewhere."""
    for ct in await _portal_case_types(session, tenant):
        if str(ct.id) == str(case_type_id):
            return ct
    raise coded(400, PORTAL_CASETYPE_400, "Case type not available for portal submission")


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
        # Defaults to True so an existing portal keeps behaving as it did — this
        # setting arrived after self-registration was already live, and a
        # missing key must not silently close a door that was open.
        "self_registration": ps.get("self_registration", True),
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
        raise coded(404, PORTAL_NOTFOUND_404, f"Portal '{slug}' not found")

    ps = _portal_settings(tenant)
    if not ps.get("enabled", False):
        raise coded(403, PORTAL_INACTIVE_403, "This portal is not currently active")

    cts = await _portal_case_types(session, tenant)

    ct_list = [{"id": str(ct.id), "name": ct.name, "description": ct.description,
                "default_priority": ct.default_priority} for ct in cts]
    return _get_portal_config(tenant, ct_list)


# ─── Public: when we are closed ──────────────────────────────────

@public_router.get("/{slug}/closures")
async def get_portal_closures(
    slug: str,
    session: AsyncSession = Depends(get_session),
):
    """The organisation's working hours and holiday dates, for this portal.

    Deliberately the **only** thing Calendar publishes to a customer. A portal
    may say "we are closed 25–26 December" or "we reply within office hours"; it
    may never learn that a particular person is off, which is why nothing here
    reads `user_absences`.

    The calendar is chosen by the portal's `business_calendar` setting, falling
    back to one named `default`. With neither, the portal simply publishes no
    closures rather than inventing office hours.
    """
    from velaris_shared.db.models import BusinessCalendarModel

    tenant = (await session.execute(
        select(TenantModel).where(TenantModel.slug == slug)
    )).scalar_one_or_none()
    if not tenant:
        raise coded(404, PORTAL_NOTFOUND_404, f"Portal '{slug}' not found")

    ps = _portal_settings(tenant)
    if not ps.get("enabled", False):
        raise coded(403, PORTAL_INACTIVE_403, "This portal is not currently active")

    name = ps.get("business_calendar") or "default"
    cal = (await session.execute(
        select(BusinessCalendarModel).where(BusinessCalendarModel.name == name)
    )).scalar_one_or_none()
    if cal is None:
        return {"timezone": None, "work_days": [], "work_start_hour": None,
                "work_end_hour": None, "holidays": []}

    return {
        "timezone": cal.timezone,
        "work_days": cal.work_days or [],
        "work_start_hour": cal.work_start_hour,
        "work_end_hour": cal.work_end_hour,
        "holidays": cal.holidays or [],
    }


# ─── Public: Submit a case ───────────────────────────────────────

@public_router.post("/{slug}/submit", response_model=PortalSubmitResponse)
async def submit_case(
    slug: str,
    body: PortalSubmitRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    tenant = (await session.execute(
        select(TenantModel).where(TenantModel.slug == slug)
    )).scalar_one_or_none()
    if not tenant:
        raise coded(404, PORTAL_NOTFOUND_404, f"Portal '{slug}' not found")

    ps = _portal_settings(tenant)
    if not ps.get("enabled", False):
        raise coded(403, PORTAL_INACTIVE_403, "This portal is not currently active")

    ct = await _portal_case_type_or_refuse(session, tenant, body.case_type_id)

    # A Form Builder form attached to the case type is checked here, as a staff
    # submission is (Form Builder part 10, 2026-10-28) — until then the portal
    # stored whatever `extra_data` arrived. No form attached: as before.
    extra_data = body.extra_data
    form_id = _portal_display(ct).get("form_id")
    if form_id:
        from velaris_shared.db.models import FormDefinitionModel
        from velaris_shared.forms import portal_definition, portal_keys, validate as validate_form
        try:
            form = await session.get(FormDefinitionModel, uuid.UUID(str(form_id)))
        except ValueError:
            form = None
        if form is not None:
            # Only the form's own keys are stored: an anonymous caller does not
            # get to add arbitrary keys to the case.
            allowed = portal_keys(form.definition_json)
            # `context={}`: a customer's form holds nothing from a case, so a
            # read-only field is its default — never the value sent for it,
            # which a formula would otherwise read (Form Builder v2).
            from velaris_shared.forms.reference import load_rows, table_ids
            # In the customer's language when the form has it (§5 step 7):
            # only the messages change, never what is checked.
            from velaris_shared.forms.translate import for_caller
            pdef = for_caller(portal_definition(form.definition_json), request.headers.get("accept-language"))
            # Reference rows: every chosen value is checked against the table
            # (Form Builder v2 step 3); unreadable = every table empty (refuse).
            try:
                refs = await load_rows(session, pdef, tenant.id) if table_ids(pdef) else {}
            except Exception:                       # noqa: BLE001
                refs = {tid: [] for tid in table_ids(pdef)}
            extra_data, field_errors = validate_form(pdef,
                                                     {k: v for k, v in extra_data.items() if k in allowed},
                                                     context={}, references=refs)
            extra_data = {k: v for k, v in extra_data.items() if k in allowed}
            if field_errors:
                raise HTTPException(422, detail={
                    "message": "; ".join(e.message for e in field_errors),
                    "field_errors": [e.as_dict() for e in field_errors],
                })

    # Rollouts, the case, consent, the account link, the idempotency ref and the
    # HxShield event are case-service's: one internal call, one transaction.
    # Idempotent on client_ref (Portal v2 P2): a replay answers the ORIGINAL case.
    got = await _internal("portal/cases", {
        "tenant_id": tenant.id, "slug": slug, "case_type_id": ct.id,
        "submitter_name": body.submitter_name, "submitter_email": body.submitter_email,
        "subject": body.subject, "description": body.description,
        "priority": body.priority, "extra_data": extra_data,
        "client_ref": body.client_ref,
    }, session)
    if got["replayed"]:
        return PortalSubmitResponse(
            tracking_token=got["tracking_token"],
            case_id=str(got["case_id"]),
            message="Your request had already been submitted — this is the original reference.",
        )
    return PortalSubmitResponse(
        tracking_token=got["tracking_token"],
        case_id=got["case_id"],
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
            CaseInstanceModel.portal_tracking_token == token,
            # 199 — a deleted case is hidden from the CUSTOMER too. "Hidden from
            # every list and read" was true of the staff surface and false of
            # this one until 2026-09-08, which is the wrong way round: this is
            # the least-trusted surface on the platform.
            CaseInstanceModel.is_deleted.is_(False),
        )
    )).scalar_one_or_none()
    if not case:
        raise coded(404, PORTAL_ACCESS_404, "Tracking token not found")

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
            CaseInstanceModel.portal_tracking_token == token,
            # 199 — a deleted case is hidden from the CUSTOMER too. "Hidden from
            # every list and read" was true of the staff surface and false of
            # this one until 2026-09-08, which is the wrong way round: this is
            # the least-trusted surface on the platform.
            CaseInstanceModel.is_deleted.is_(False),
        )
    )).scalar_one_or_none()
    if not case:
        raise coded(404, PORTAL_ACCESS_404, "Tracking token not found")
    # Resolved from the tenant's own status set, not from a tuple. A tenant
    # that adds its own terminal status got a case that was closed everywhere
    # except here, where the customer could still upload to it.
    from velaris_shared.case_actions import statuses as _st
    if await _st.is_terminal_for_case(session, case):
        raise coded(400, PORTAL_CLOSED_400, "Cannot upload to a closed case")

    from velaris_shared.middleware.file_security import validate_upload_filename, safe_filename, ALLOWED_DOCUMENT_EXTENSIONS
    filename = file.filename or "upload"
    ok, reason = validate_upload_filename(filename, allowed_extensions=ALLOWED_DOCUMENT_EXTENSIONS)
    if not ok:
        raise coded(400, PORTAL_UPLOAD_400, f"File rejected: {reason}")
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
        raise coded(404, PORTAL_NOTFOUND_404, f"Portal '{slug}' not found")

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
        raise coded(404, PORTAL_NOTFOUND_404, f"Portal '{slug}' not found")

    case = (await session.execute(
        select(CaseInstanceModel).where(
            CaseInstanceModel.id == case_id,
            CaseInstanceModel.portal_submitter_email == email,
        )
    )).scalar_one_or_none()
    if not case:
        raise coded(404, PORTAL_ACCESS_404, "Case not found or email does not match")

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
        raise coded(404, PORTAL_NOTFOUND_404, f"Portal '{slug}' not found")

    case = (await session.execute(
        select(CaseInstanceModel).where(
            CaseInstanceModel.id == case_id,
            CaseInstanceModel.portal_submitter_email == email,
        )
    )).scalar_one_or_none()
    if not case:
        raise coded(404, PORTAL_ACCESS_404, "Case not found or email does not match")

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
                # A signed, expiring, single-document grant — not a bare URL.
                # The authorization decision was made a few lines above (this
                # case matches the caller's email, and the document is either
                # staff-shared or the customer's own upload); the grant carries
                # that decision to a route the customer cannot otherwise reach.
                # Before 2026-08-15 this was an unauthenticated URL, which is
                # what made EVERY document in the deployment world-readable.
                "download_url": access_tokens.download_url(d.id),
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
        raise coded(404, PORTAL_NOTFOUND_404, f"Portal '{slug}' not found")

    case = (await session.execute(
        select(CaseInstanceModel).where(
            CaseInstanceModel.id == case_id,
            CaseInstanceModel.portal_submitter_email == email,
        )
    )).scalar_one_or_none()
    if not case:
        raise coded(404, PORTAL_ACCESS_404, "Case not found or email does not match")

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
        raise coded(404, PORTAL_NOTFOUND_404, f"Portal '{slug}' not found")

    ip = request.client.host if request.client else "unknown"
    if not await _rate_check(f"ask:{ip}", max_calls=10, window_seconds=3600):
        raise coded(429, PORTAL_RATE_LIMIT_429, "Too many requests — please try again later")

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
        raise coded(404, PORTAL_NOTFOUND_404, f"Portal '{slug}' not found")

    case = (await session.execute(
        select(CaseInstanceModel).where(
            CaseInstanceModel.id == case_id,
            CaseInstanceModel.portal_submitter_email == email,
        )
    )).scalar_one_or_none()
    if not case:
        raise coded(404, PORTAL_ACCESS_404, "Case not found or email does not match")

    ip = request.client.host if request.client else "unknown"
    if not await _rate_check(f"chat:{ip}:{case_id}", max_calls=20, window_seconds=86400):
        raise coded(429, PORTAL_RATE_LIMIT_429, "Message limit reached for today — please try again tomorrow")

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
        raise coded(429, PORTAL_OTP_RATE_429, "OTP request limit reached. Please try again after 24 hours.")

    # Verify tenant exists
    tenant = (await session.execute(
        select(TenantModel).where(TenantModel.slug == slug)
    )).scalar_one_or_none()
    if not tenant:
        raise coded(404, PORTAL_NOTFOUND_404, f"Portal '{slug}' not found")

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
        await _store_portal_otp(str(case_id), otp)

        # case-service composes and sends it (internal_api/portal.py: otp_email).
        try:
            await _internal("portal/otp-email", {
                "purpose": "bank_details", "to": str(body.email), "code": otp,
                "case_id": case_id}, session)
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
        raise coded(429, PORTAL_OTP_RATE_429, "Too many attempts. Please request a new OTP and try again.")

    # Verify tenant
    tenant = (await session.execute(
        select(TenantModel).where(TenantModel.slug == slug)
    )).scalar_one_or_none()
    if not tenant:
        raise coded(404, PORTAL_NOTFOUND_404, f"Portal '{slug}' not found")

    # Verify case + email ownership in a single query
    case = (await session.execute(
        select(CaseInstanceModel).where(
            CaseInstanceModel.id == case_id,
            CaseInstanceModel.portal_submitter_email == str(body.email),
        )
    )).scalar_one_or_none()
    if not case:
        raise coded(404, PORTAL_ACCESS_404, "Email does not match this case or case not found.")

    # Verify OTP — single-use, constant-time comparison, expiry enforced inside helper
    if not await _verify_portal_otp(str(case_id), body.otp):
        raise coded(400, PORTAL_OTP_INVALID_400, "Invalid or expired verification code.")

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
        raise coded(404, PORTAL_NOTFOUND_404, "No pending payment disbursement found for this case.")

    # SD-1: one source of truth for the envelope, and the tenant comes from the
    # DISBURSEMENT row rather than the portal session — this is an anonymous
    # customer submitting their own bank details, so the row is the authority on
    # whose data it is, and it decides which key can later destroy it.
    disbursement.bank_reference = await _encrypt_bank_ref(
        session,
        f"{account_name}|{account_number}|{sort_code}",
        getattr(disbursement, "tenant_id", None),
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
    _: AuthenticatedUser = Depends(hxguard.guard("portal.config.read")),
    session: AsyncSession = Depends(get_session),
):
    tenants = (await session.execute(select(TenantModel))).scalars().all()
    # 8d (2026-09-29): every tenant's portal settings, to any portal admin.
    from velaris_shared.tenancy.identity import is_platform_operator, same_tenant

    if not await is_platform_operator(session, _):
        tenants = [t for t in tenants
                   if await same_tenant(session, t.id, getattr(_, "tenant_id", None))]
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
            "voice_input": ps.get("voice_input", False),
            "voice_consent_text": ps.get("voice_consent_text", ""),
            # Default True — the setting arrived after self-registration was
            # already live, so an untouched portal keeps accepting sign-ups.
            "self_registration": ps.get("self_registration", True),
            "portal_case_type_count": len(ct_count),
        })
    return result


async def _config_tenant(slug: str, session: AsyncSession, user: AuthenticatedUser) -> TenantModel:
    """The slug's tenant, if the caller may configure its portal.

    8h (2026-09-29): these two routes took the tenant from the URL and nothing
    else, so any tenant's admin could rewrite another tenant's portal — its
    welcome text, its colours, its logo, whether it is on at all. Its own staff
    may; so may the platform operator, who provisions tenants. Nobody else,
    refused as 404 so a slug says nothing.
    """
    from velaris_shared.tenancy.identity import is_platform_operator, same_tenant
    tenant = (await session.execute(
        select(TenantModel).where(TenantModel.slug == slug)
    )).scalar_one_or_none()
    if not tenant or not (
            await same_tenant(session, tenant.id, getattr(user, "tenant_id", None) or "default")
            or await is_platform_operator(session, user)):
        raise coded(404, PORTAL_NOTFOUND_404, f"Tenant '{slug}' not found")
    return tenant


@admin_router.patch("/tenants/{slug}")
async def update_portal_settings(
    slug: str,
    body: PortalSettingsUpdate,
    _: AuthenticatedUser = Depends(hxguard.guard("portal.config.write")),
    session: AsyncSession = Depends(get_session),
):
    tenant = await _config_tenant(slug, session, _)

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
    if body.voice_input is not None:
        portal["voice_input"] = body.voice_input
    if body.voice_consent_text is not None:
        portal["voice_consent_text"] = body.voice_consent_text.strip() or None
    if body.self_registration is not None:
        portal["self_registration"] = body.self_registration

    settings["portal"] = portal
    tenant.settings = settings
    await session.commit()
    return {"slug": slug, "portal": portal}


@admin_router.get("/submissions")
async def list_portal_submissions(
    slug: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
    _: AuthenticatedUser = Depends(hxguard.guard("portal.submissions.read")),
    session: AsyncSession = Depends(get_session),
):
    q = select(CaseInstanceModel).where(
        CaseInstanceModel.portal_tracking_token.is_not(None)
    ).order_by(CaseInstanceModel.created_at.desc()).limit(limit)

    if status:
        q = q.where(CaseInstanceModel.status == status)
    # 2026-11-04: every tenant's portal submissions — submitter names and
    # emails — went to every tenant's admin (found by the demo-tenant sweep).
    # The caller's tenant only; the platform operator keeps the full list.
    from velaris_shared.tenancy.identity import (
        is_platform_operator, scope_to_caller, strict_case_reads,
    )
    if not await is_platform_operator(session, _):
        q = await scope_to_caller(q, CaseInstanceModel, session, _)
        if strict_case_reads():
            q = q.where(CaseInstanceModel.tenant_id.is_not(None))

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


async def _writable_case_type(session: AsyncSession, user: AuthenticatedUser, case_type_id):
    """A case type the caller may change portal settings on (2026-11-04).

    Another tenant's is already refused by the path guard. A platform DEFAULT
    (no tenant) is read by every tenant, and only the platform operator changes
    a default — a tenant admin turned its portal on or off for everybody.
    """
    from velaris_shared.tenancy.identity import is_platform_operator, same_tenant
    ct = await session.get(CaseTypeModel, case_type_id)
    if not ct:
        raise coded(404, PORTAL_NOTFOUND_404, "Case type not found")
    if ct.tenant_id is None:
        if not await is_platform_operator(session, user):
            raise coded(403, PORTAL_DEFAULT_403,
                        "This is a platform default case type; only the platform operator "
                        "changes it. Edit your own copy instead.")
    elif not (await same_tenant(session, ct.tenant_id, getattr(user, "tenant_id", None) or "default")
              or await is_platform_operator(session, user)):
        raise coded(404, PORTAL_NOTFOUND_404, "Case type not found")
    return ct


@admin_router.patch("/case-types/{case_type_id}/portal")
async def toggle_case_type_portal(
    case_type_id: uuid.UUID,
    enabled: bool,
    _: AuthenticatedUser = Depends(hxguard.guard("portal.config.write")),
    session: AsyncSession = Depends(get_session),
):
    ct = await _writable_case_type(session, _, case_type_id)
    ct.portal_enabled = enabled
    await session.commit()
    return {"case_type_id": str(case_type_id), "portal_enabled": enabled}


# ═══ Portal v2 (P1) — customer-JWT case endpoints ═══════════════════
# Logged-in customers (P65 accounts) reach their cases through the
# portal_customer_cases link, not the email query param. Handlers delegate to
# the email-verified endpoints above using the CASE's own submitter email —
# authorization happens here (link check), the inner email check then always
# passes, and the two paths can never drift apart.

from portal_service.api.routers.portal_customers import _require_customer  # noqa: E402
from velaris_shared.db.models import (  # noqa: E402
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
    # A deleted case answers the SAME uniform 404 as a missing one, a foreign
    # one and an unlinked one — the docstring's anti-oracle property is why the
    # check belongs here rather than at each of the twenty routes that call this.
    if not case or bool(getattr(case, "is_deleted", False)):
        raise coded(404, PORTAL_ACCESS_404, "Case not found")
    return case


def _portal_display(ct: CaseTypeModel | None) -> dict:
    """Per-case-type customer-facing display config (definition_json['portal'])."""
    return ((ct.definition_json or {}).get("portal", {}) if ct else {}) or {}


def _stage_rail(ct: CaseTypeModel | None, case: CaseInstanceModel,
                done: bool = False) -> list[dict]:
    """Ordered stages with customer-friendly labels — internal names never
    leak when a label map is configured; unmapped stages fall back to the
    designer's stage name (already human text, unlike stage ids).

    `done` is passed IN rather than derived here: "has this case concluded" now
    comes from the tenant's own status set, which needs a database session, and
    this helper is deliberately synchronous and session-free so it stays a pure
    function of the definition. It defaults to False, which is what an unfinished
    case looks like — the safe answer if a future caller forgets it, because it
    shows the rail's current step rather than silently marking everything done.
    """
    stages = sorted(((ct.definition_json or {}).get("stages", []) if ct else []),
                    key=lambda s: s.get("order", 0))
    labels = _portal_display(ct).get("stage_labels", {}) or {}
    current_order = next((s.get("order", 0) for s in stages
                          if s.get("id") == case.current_stage_id), None)
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

    # "Concluded" comes from the tenant's own status set, so a tenant that adds
    # its own terminal status gets a rail that reads as finished. Resolved here
    # because `_stage_rail` is a session-free pure function of the definition.
    from velaris_shared.case_actions import statuses as _st
    _concluded = await _st.is_concluded_for_case(session, case)

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
        "stage_rail":     _stage_rail(ct, case, _concluded),
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
        raise coded(400, PORTAL_UPLOAD_400, "This case does not accept portal uploads")
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
    from velaris_shared.db.models import CaseSessionParticipantModel
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
    case = await _linked_case(slug, case_id, customer, session)
    try:
        return await _internal("portal/meet/join", {
            "session_id": session_id, "case_id": case.id, "customer_id": customer.id,
            "display_name": customer.display_name}, session)
    except HTTPException as exc:
        if exc.status_code == 404:   # missing, ended, or not invited — one uniform 404
            raise coded(404, PORTAL_NOTFOUND_404, "Session not found") from None
        raise


# ─── Portal v2: admin — customer-facing display config ───────────

class PortalDisplayUpdate(BaseModel):
    stage_labels: Optional[dict[str, str]] = None   # stage_id → customer label
    expected_days: Optional[int] = None             # typical end-to-end duration
    form_id: Optional[str] = None                   # P5: Form Builder form for portal submit ("" clears)


@admin_router.get("/case-types/{case_type_id}/portal-display")
async def get_case_type_portal_display(
    case_type_id: uuid.UUID,
    _: AuthenticatedUser = Depends(hxguard.guard("portal.config.read")),
    session: AsyncSession = Depends(get_session),
):
    ct = await session.get(CaseTypeModel, case_type_id)
    if not ct:
        raise coded(404, PORTAL_NOTFOUND_404, "Case type not found")
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
    _: AuthenticatedUser = Depends(hxguard.guard("portal.config.write")),
    session: AsyncSession = Depends(get_session),
):
    ct = await _writable_case_type(session, _, case_type_id)
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
            from velaris_shared.db.models import FormDefinitionModel
            try:
                form = await session.get(FormDefinitionModel, uuid.UUID(body.form_id))
            except ValueError:
                form = None
            # Only a form this case type's owner can see: its own, or a default
            # (2026-11-04) — another tenant's form answers as unknown.
            from velaris_shared.tenancy.identity import same_tenant
            ftid = getattr(form, "tenant_id", None) if form is not None else None
            if form is None or (ftid is not None and not (
                    ct.tenant_id is not None and await same_tenant(session, ftid, str(ct.tenant_id)))):
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
    """Customer-assigned steps of one stage — the shared rule case-service's
    internal API re-checks with (`velaris_shared.case_actions.customer_steps`)."""
    from velaris_shared.case_actions.customer_steps import customer_steps
    return customer_steps(ct, stage_id)


@public_router.get("/{slug}/account/cases/{case_id}/actions")
async def account_case_actions(
    slug: str,
    case_id: uuid.UUID,
    customer: PortalCustomerModel = Depends(_require_customer),
    session: AsyncSession = Depends(get_session),
):
    """Pending customer actions on the case's CURRENT stage."""
    from velaris_shared.db.models import CaseStepCompletionModel
    case = await _linked_case(slug, case_id, customer, session)
    ct = await session.get(CaseTypeModel, case.case_type_id)
    steps = _customer_steps(ct, case.current_stage_id)
    from velaris_shared.case_actions import statuses as _st
    if not steps or await _st.is_terminal_for_case(session, case):
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
    from velaris_shared.db.models import CaseStepCompletionModel

    case = await _linked_case(slug, case_id, customer, session)
    from velaris_shared.case_actions import statuses as _st
    if await _st.is_terminal_for_case(session, case):
        raise coded(400, PORTAL_CLOSED_400, "This case is closed")
    ct = await session.get(CaseTypeModel, case.case_type_id)
    step = next((s for s in _customer_steps(ct, case.current_stage_id)
                 if s.get("id") == step_id), None)
    if step is None:
        # Not a customer step / not in the current stage — uniform 404.
        raise coded(404, PORTAL_NOTFOUND_404, "Action not found")

    existing = (await session.execute(
        select(CaseStepCompletionModel).where(
            CaseStepCompletionModel.case_id == case.id,
            CaseStepCompletionModel.step_id == step_id,
        )
    )).scalar_one_or_none()
    if existing is not None:
        raise coded(409, PORTAL_ACTION_409, "This action has already been completed")

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

    # The completion row, its audit, the step_complete outbound rules and the
    # auto-advance are case-service's (internal_api/portal.py: complete_step).
    # case-service re-checks the link, the open case and the step, and records
    # this customer as the actor (it never takes one from the caller).
    got = await _internal("portal/steps/complete", {
        "case_id": case.id, "customer_id": customer.id, "step_id": step_id,
        "status": status,
        "data": {**body.data,
                 **({"decision": body.decision} if body.decision else {}),
                 **({"comment": body.comment} if body.comment else {})},
    }, session)
    if got.get("replayed"):
        raise coded(409, PORTAL_ACTION_409, "This action has already been completed")

    return {"step_id": step_id, "status": status, "auto_advanced": got["auto_advanced"],
            "advance_held_by_risk": got["advance_held_by_risk"]}


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
    from velaris_shared.db.models import CaseMessageModel
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
    from velaris_shared.db.models import CaseMessageModel
    text = (body.body or "").strip()
    if not text or len(text) > 10_000:
        raise HTTPException(400, "Message must be 1–10000 characters")
    case = await _linked_case(slug, case_id, customer, session)
    from velaris_shared.case_actions import statuses as _st
    if await _st.is_thread_closed_for_case(session, case):
        raise coded(400, PORTAL_CLOSED_400, "This case is closed")
    ip = request.client.host if request.client else "unknown"
    if not await _rate_check(f"msg:{ip}:{case_id}", max_calls=30, window_seconds=3600):
        raise coded(429, PORTAL_RATE_LIMIT_429, "Message limit reached — please try again later")
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
    from velaris_shared.db.models import PortalCsatModel
    if not 1 <= body.rating <= 5:
        raise HTTPException(400, "rating must be 1–5")
    case = await _linked_case(slug, case_id, customer, session)
    from velaris_shared.case_actions import statuses as _st
    if not await _st.is_concluded_for_case(session, case):
        raise HTTPException(400, "You can rate a request once it is resolved")
    if await session.get(PortalCsatModel, case.id) is not None:
        raise coded(409, PORTAL_ACTION_409, "This request has already been rated")
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
    from velaris_shared.db.models import PortalCsatModel
    case = await _linked_case(slug, case_id, customer, session)
    row = await session.get(PortalCsatModel, case.id)
    from velaris_shared.case_actions import statuses as _st
    return {"rated": row is not None, "rating": row.rating if row else None,
            "can_rate": row is None and await _st.is_concluded_for_case(session, case)}


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
    from velaris_shared.db.models import PortalAskFeedbackModel
    tenant = (await session.execute(
        select(TenantModel).where(TenantModel.slug == slug)
    )).scalar_one_or_none()
    if not tenant:
        raise coded(404, PORTAL_NOTFOUND_404, f"Portal '{slug}' not found")
    ip = request.client.host if request.client else "unknown"
    if not await _rate_check(f"askfb:{ip}", max_calls=20, window_seconds=3600):
        raise coded(429, PORTAL_RATE_LIMIT_429, "Too many requests")
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
    _: AuthenticatedUser = Depends(hxguard.guard("portal.config.write")),
    session: AsyncSession = Depends(get_session),
):
    tenant = await _config_tenant(slug, session, _)
    ctype = (file.content_type or "").lower()
    if ctype not in _LOGO_TYPES:
        raise coded(400, PORTAL_UPLOAD_400, "Logo must be a PNG, JPEG, or WebP image")
    data = await file.read()
    if len(data) > _LOGO_MAX_BYTES:
        raise coded(400, PORTAL_UPLOAD_400, "Logo must be under 1 MB")
    # Magic-byte sanity: never trust the declared content type alone.
    if not (data[:8] == b"\x89PNG\r\n\x1a\n" or data[:3] == b"\xff\xd8\xff"
            or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")):
        raise coded(400, PORTAL_UPLOAD_400, "File content does not match an accepted image type")

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
        raise coded(404, PORTAL_NOTFOUND_404, "No logo")
    storage = get_storage_backend()
    try:
        data = await storage.get(key)
    except Exception:
        raise coded(404, PORTAL_NOTFOUND_404, "No logo")
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
    from velaris_shared.db.models import FormDefinitionModel
    tenant = (await session.execute(
        select(TenantModel).where(TenantModel.slug == slug)
    )).scalar_one_or_none()
    if not tenant or not _portal_settings(tenant).get("enabled", False):
        raise coded(404, PORTAL_NOTFOUND_404, "Portal not found")
    # Same rule as the config listing and submit: a form belongs to a case type,
    # and a case type belongs to a tenant.
    ct = await _portal_case_type_or_refuse(session, tenant, case_type_id)
    form_id = _portal_display(ct).get("form_id")
    if not form_id:
        return {"fields": []}
    form = await session.get(FormDefinitionModel, uuid.UUID(str(form_id)))
    if form is None:
        return {"fields": []}
    # `definition` (part 10): what the portal draws with the shared renderer and
    # what submit checks — fields a customer cannot fill in are left out.
    from velaris_shared.forms import portal_definition
    from velaris_shared.forms import embed as _embed
    # Embeds (2026-10-29): only when this tenant allows them on the portal, and
    # only from its approved sites. The browser checks the same list again.
    # 2026-11-04: the tenant's own list and the platform's, minus the blocked
    # (Admin > Security > Allowed sites).
    emb = await _embed.effective_for(session, tenant)
    embed_origins = emb["allowed_origins"] if emb["portal"] else None
    definition = portal_definition(form.definition_json, for_browser=True, embed_origins=embed_origins)
    if not emb["allow_forms"]:
        for f in _embed.embed_fields(definition):
            if isinstance(f.get("permissions"), list):
                f["permissions"] = [p for p in f["permissions"] if p != "forms"]
    # The older flat list is built from the same filtered form (2026-10-28):
    # it listed every field, hidden ones and internal lookups included.
    fields = [{
        "key":         f["field_key"],
        "label":       f.get("label", ""),
        "type":        f.get("type", "text"),
        "required":    f.get("required", False),
        "placeholder": f.get("placeholder", ""),
        "options":     f.get("options", []),
    } for section in definition["sections"] for f in section["fields"] if f.get("type") != "embed"]
    return {"fields": fields, "definition": definition, "embed_origins": embed_origins or []}


# ─── Form Builder v2 step 3: reference-table options for a customer ──

class PortalOptionsRequest(BaseModel):
    field_key: str
    #: The field is a column of this repeating group.
    group_key: Optional[str] = None
    q: str = ""
    #: The form's current values — only the filter fields are read, and they only narrow.
    values: dict = {}


@public_router.post("/{slug}/case-types/{case_type_id}/form/options")
async def portal_field_options(
    slug: str,
    case_type_id: uuid.UUID,
    body: PortalOptionsRequest,
    session: AsyncSession = Depends(get_session),
):
    """A portal form field's options from its reference table. Only a field of
    the form attached to this portal case type, as the customer sees it
    (`portal_definition`), and only that field's table, columns and filters."""
    from velaris_shared.db.models import FormDefinitionModel
    from velaris_shared.forms import portal_definition
    from velaris_shared.forms import reference as ref
    tenant = (await session.execute(select(TenantModel).where(TenantModel.slug == slug))).scalar_one_or_none()
    if not tenant or not _portal_settings(tenant).get("enabled", False):
        raise coded(404, PORTAL_NOTFOUND_404, "Portal not found")
    ct = await _portal_case_type_or_refuse(session, tenant, case_type_id)
    form_id = _portal_display(ct).get("form_id")
    form = await session.get(FormDefinitionModel, uuid.UUID(str(form_id))) if form_id else None
    if form is None:
        raise coded(404, PORTAL_NOTFOUND_404, "No form")
    field = None
    for sec in portal_definition(form.definition_json)["sections"]:
        for f in sec["fields"]:
            if body.group_key:
                if f["field_key"] == body.group_key and f.get("type") == "repeating_group":
                    field = next((c for c in f.get("fields", []) if c["field_key"] == body.field_key and not c.get("hidden")), None)
            elif f["field_key"] == body.field_key and not f.get("hidden"):
                field = f
    src = ref.ref_source(field) if field else None
    if src is None:
        raise coded(404, PORTAL_NOTFOUND_404, "No such field")
    rows = (await ref.load_rows(session, {"sections": [{"fields": [field]}]}, tenant.id)).get(str(src.get("table_id") or ""), [])
    values = body.values if isinstance(body.values, dict) else {}
    return {"options": ref.options(ref.matching_rows(rows, src, values.get), src, q=str(body.q)[:100])}
