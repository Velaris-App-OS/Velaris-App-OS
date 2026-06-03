"""P49 HxConnect — Identity (Onfido) & E-Sign (DocuSign) API."""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser
from case_service.db.models import ESignRequestModel, IdentityVerificationModel
from case_service.db.session import get_session
from case_service.kyc import service as kyc_svc

identity_router = APIRouter(prefix="/identity", tags=["kyc"])
esign_router    = APIRouter(prefix="/esign",    tags=["esign"])


# ── Identity schemas ──────────────────────────────────────────────────────────

class VerifyBody(BaseModel):
    step_id:      str
    first_name:   str
    last_name:    str = ""
    connector_id: Optional[uuid.UUID] = None

class VerificationOut(BaseModel):
    id:               uuid.UUID
    case_id:          uuid.UUID
    step_id:          str
    provider:         str
    status:           str
    result:           Optional[str]
    verification_url: Optional[str]
    created_at:       str
    completed_at:     Optional[str]

    @classmethod
    def from_model(cls, v: IdentityVerificationModel) -> "VerificationOut":
        return cls(
            id               = v.id,
            case_id          = v.case_id,
            step_id          = v.step_id,
            provider         = v.provider,
            status           = v.status,
            result           = v.result,
            verification_url = v.verification_url,
            created_at       = v.created_at.isoformat(),
            completed_at     = v.completed_at.isoformat() if v.completed_at else None,
        )
    model_config = {"from_attributes": True}


# ── Identity endpoints ────────────────────────────────────────────────────────

@identity_router.post("/cases/{case_id}/verify", response_model=VerificationOut)
async def initiate_verification(
    case_id: uuid.UUID, body: VerifyBody,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    try:
        iv = await kyc_svc.create_identity_verification(
            session, case_id, body.step_id,
            tenant_id    = getattr(user, "tenant_id", None) or "default",
            first_name   = body.first_name,
            last_name    = body.last_name,
            connector_id = body.connector_id,
        )
        await session.commit()
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        await session.rollback()
        raise HTTPException(502, f"KYC gateway error: {exc}")
    return VerificationOut.from_model(iv)


@identity_router.get("/cases/{case_id}/verifications", response_model=list[VerificationOut])
async def list_verifications(
    case_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    rows = await kyc_svc.list_verifications(session, case_id)
    return [VerificationOut.from_model(r) for r in rows]


@identity_router.post("/webhooks/onfido", status_code=200)
async def onfido_webhook(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    payload    = await request.body()
    sig_header = request.headers.get("x-sha2-signature", "")
    result = await kyc_svc.handle_onfido_webhook(session, payload, sig_header)
    if result.get("status") == "rejected":
        raise HTTPException(400, result.get("reason"))
    return result


# ── E-sign schemas ────────────────────────────────────────────────────────────

class ESignBody(BaseModel):
    step_id:         str
    signer_email:    str
    signer_name:     str = ""
    document_name:   str = "Document for Signature"
    document_base64: str = ""
    connector_id:    Optional[uuid.UUID] = None

class ESignOut(BaseModel):
    id:           uuid.UUID
    case_id:      uuid.UUID
    step_id:      str
    provider:     str
    envelope_id:  Optional[str]
    signing_url:  Optional[str]
    document_name: Optional[str]
    signer_email: Optional[str]
    status:       str
    signed_at:    Optional[str]
    created_at:   str

    @classmethod
    def from_model(cls, e: ESignRequestModel) -> "ESignOut":
        return cls(
            id            = e.id,
            case_id       = e.case_id,
            step_id       = e.step_id,
            provider      = e.provider,
            envelope_id   = e.envelope_id,
            signing_url   = e.signing_url,
            document_name = e.document_name,
            signer_email  = e.signer_email,
            status        = e.status,
            signed_at     = e.signed_at.isoformat() if e.signed_at else None,
            created_at    = e.created_at.isoformat(),
        )
    model_config = {"from_attributes": True}


# ── E-sign endpoints ──────────────────────────────────────────────────────────

@esign_router.post("/cases/{case_id}/send", response_model=ESignOut)
async def send_esign(
    case_id: uuid.UUID, body: ESignBody,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    try:
        es = await kyc_svc.create_esign_request(
            session, case_id, body.step_id,
            tenant_id       = getattr(user, "tenant_id", None) or "default",
            signer_email    = body.signer_email,
            signer_name     = body.signer_name,
            document_name   = body.document_name,
            document_base64 = body.document_base64,
            connector_id    = body.connector_id,
        )
        await session.commit()
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        await session.rollback()
        raise HTTPException(502, f"E-sign gateway error: {exc}")
    return ESignOut.from_model(es)


@esign_router.get("/cases/{case_id}/requests", response_model=list[ESignOut])
async def list_esign(
    case_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    rows = await kyc_svc.list_esign_requests(session, case_id)
    return [ESignOut.from_model(r) for r in rows]


@esign_router.post("/webhooks/docusign", status_code=200)
async def docusign_webhook(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    payload    = await request.body()
    sig_header = request.headers.get("x-docusign-signature-1", "")
    result = await kyc_svc.handle_docusign_webhook(session, payload, sig_header)
    if result.get("status") == "rejected":
        raise HTTPException(400, result.get("reason"))
    return result


@identity_router.get("/verifications", response_model=list[VerificationOut])
async def list_all_verifications(
    status: str | None = None,
    limit: int = 100,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    from sqlalchemy import select
    from case_service.db.models import IdentityVerificationModel
    q = select(IdentityVerificationModel).order_by(IdentityVerificationModel.created_at.desc()).limit(limit)
    if status:
        q = q.where(IdentityVerificationModel.status == status)
    rows = (await session.execute(q)).scalars().all()
    return [VerificationOut.from_model(r) for r in rows]


@esign_router.get("/requests", response_model=list[ESignOut])
async def list_all_esign_requests(
    status: str | None = None,
    limit: int = 100,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    from sqlalchemy import select
    from case_service.db.models import ESignRequestModel
    q = select(ESignRequestModel).order_by(ESignRequestModel.created_at.desc()).limit(limit)
    if status:
        q = q.where(ESignRequestModel.status == status)
    rows = (await session.execute(q)).scalars().all()
    return [ESignOut.from_model(r) for r in rows]
