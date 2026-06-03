"""P50 HxConnect — CRM (Salesforce) & Accounting (Xero) API."""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser
from case_service.db.models import CrmSyncRecordModel, InvoiceRecordModel
from case_service.db.session import get_session
from case_service.crm import service as crm_svc

crm_router     = APIRouter(prefix="/crm",      tags=["crm"])
invoice_router = APIRouter(prefix="/invoices",  tags=["invoices"])


# ── SD-8: Invoice line-item access control ────────────────────────────────────

def _has_sensitive_access(user: AuthenticatedUser) -> bool:
    """True if the caller may see unredacted invoice line-item financials.

    Mirrors the SD-4 rule in cases.py: admin and finance roles see full data;
    all other authenticated roles receive totals-only (unit_price, amount, and
    description are replaced with '***' to avoid cost/pricing disclosure).
    """
    return user.is_admin or "finance" in user.roles or "admin" in user.roles


def _redact_line_items(line_items: list | None, full_access: bool) -> list | None:
    """Replace sensitive financial fields in line items for non-privileged callers.

    Fields redacted: unit_price, amount, description.
    Fields preserved: quantity and all non-financial metadata.
    Redaction is applied post-fetch so query logic is never altered.
    """
    if full_access or not line_items:
        return line_items
    return [
        {**item, "unit_price": "***", "amount": "***", "description": "***"}
        for item in line_items
    ]


# ── CRM schemas ───────────────────────────────────────────────────────────────

class CrmSyncBody(BaseModel):
    step_id:      str
    first_name:   str
    last_name:    str = ""
    email:        str = ""
    subject:      str = ""
    description:  str = ""
    connector_id: Optional[uuid.UUID] = None

class CrmSyncOut(BaseModel):
    id:             uuid.UUID
    case_id:        uuid.UUID
    step_id:        str
    provider:       str
    crm_object_type: Optional[str]
    crm_record_id:  Optional[str]
    crm_record_url: Optional[str]
    status:         str
    error:          Optional[str]
    created_at:     str
    synced_at:      Optional[str]

    @classmethod
    def from_model(cls, r: CrmSyncRecordModel) -> "CrmSyncOut":
        return cls(
            id=r.id, case_id=r.case_id, step_id=r.step_id,
            provider=r.provider, crm_object_type=r.crm_object_type,
            crm_record_id=r.crm_record_id, crm_record_url=r.crm_record_url,
            status=r.status, error=r.error,
            created_at=r.created_at.isoformat(),
            synced_at=r.synced_at.isoformat() if r.synced_at else None,
        )
    model_config = {"from_attributes": True}


@crm_router.post("/cases/{case_id}/sync", response_model=CrmSyncOut)
async def sync_to_crm(
    case_id: uuid.UUID, body: CrmSyncBody,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    try:
        rec = await crm_svc.sync_to_crm(
            session, case_id, body.step_id,
            tenant_id    = getattr(user, "tenant_id", None) or "default",
            actor        = user.email or user.user_id or "unknown",
            first_name   = body.first_name,
            last_name    = body.last_name,
            email        = body.email,
            subject      = body.subject,
            description  = body.description,
            connector_id = body.connector_id,
        )
        await session.commit()
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        await session.rollback()
        raise HTTPException(502, f"CRM sync error: {exc}")
    return CrmSyncOut.from_model(rec)


@crm_router.get("/cases/{case_id}/records", response_model=list[CrmSyncOut])
async def list_crm_records(
    case_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    rows = await crm_svc.list_crm_records(session, case_id)
    return [CrmSyncOut.from_model(r) for r in rows]


# ── Invoice schemas ───────────────────────────────────────────────────────────

class InvoiceBody(BaseModel):
    step_id:      str
    contact_name: str
    description:  str = ""
    amount_cents: int
    currency:     str = "usd"
    line_items:   list = []
    reference:    str = ""
    connector_id: Optional[uuid.UUID] = None

class InvoiceOut(BaseModel):
    id:             uuid.UUID
    case_id:        uuid.UUID
    step_id:        str
    provider:       str
    invoice_id:     Optional[str]
    invoice_number: Optional[str]
    invoice_url:    Optional[str]
    amount_cents:   Optional[int]
    currency:       str
    status:         str
    contact_name:   Optional[str]
    # SD-8: line_items exposed here; access-controlled at the endpoint level.
    # finance and admin roles see full item data; all others see totals only
    # (unit_price, amount, description are replaced with '***').
    line_items:     Optional[list] = None
    created_at:     str
    issued_at:      Optional[str]

    @classmethod
    def from_model(cls, r: InvoiceRecordModel, *, full_access: bool = True) -> "InvoiceOut":
        """Construct an InvoiceOut from a DB model row.

        Pass full_access=False for non-finance/admin callers — line_items will
        have their sensitive financial fields redacted before inclusion.
        """
        return cls(
            id=r.id, case_id=r.case_id, step_id=r.step_id,
            provider=r.provider, invoice_id=r.invoice_id,
            invoice_number=r.invoice_number, invoice_url=r.invoice_url,
            amount_cents=r.amount_cents, currency=r.currency,
            status=r.status, contact_name=r.contact_name,
            line_items=_redact_line_items(r.line_items, full_access),
            created_at=r.created_at.isoformat(),
            issued_at=r.issued_at.isoformat() if r.issued_at else None,
        )
    model_config = {"from_attributes": True}


@invoice_router.post("/cases/{case_id}/generate", response_model=InvoiceOut)
async def generate_invoice(
    case_id: uuid.UUID, body: InvoiceBody,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    try:
        rec = await crm_svc.generate_invoice(
            session, case_id, body.step_id,
            tenant_id    = getattr(user, "tenant_id", None) or "default",
            actor        = user.email or user.user_id or "unknown",
            contact_name = body.contact_name,
            description  = body.description,
            amount_cents = body.amount_cents,
            currency     = body.currency,
            line_items   = body.line_items,
            reference    = body.reference,
            connector_id = body.connector_id,
        )
        await session.commit()
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        await session.rollback()
        raise HTTPException(502, f"Invoice generation error: {exc}")
    return InvoiceOut.from_model(rec)


@invoice_router.get("/cases/{case_id}/records", response_model=list[InvoiceOut])
async def list_invoices(
    case_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    # SD-8: determine caller privilege once, then apply uniformly to all rows.
    # finance and admin roles see full line-item financials; all other roles
    # receive totals-only with unit_price, amount, and description redacted.
    full_access = _has_sensitive_access(user)
    rows = await crm_svc.list_invoices(session, case_id)
    return [InvoiceOut.from_model(r, full_access=full_access) for r in rows]


@crm_router.get("/records", response_model=list[CrmSyncOut])
async def list_all_crm_records(
    status: str | None = None,
    limit: int = 100,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    from sqlalchemy import select
    from case_service.db.models import CrmSyncRecordModel
    q = select(CrmSyncRecordModel).order_by(CrmSyncRecordModel.created_at.desc()).limit(limit)
    if status:
        q = q.where(CrmSyncRecordModel.status == status)
    rows = (await session.execute(q)).scalars().all()
    return [CrmSyncOut.from_model(r) for r in rows]


@invoice_router.get("/records", response_model=list[InvoiceOut])
async def list_all_invoices(
    status: str | None = None,
    limit: int = 100,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    # SD-8: same redaction rule as list_invoices — the global list must not
    # serve as a bypass path around per-case access control.
    full_access = _has_sensitive_access(user)
    from sqlalchemy import select
    from case_service.db.models import InvoiceRecordModel
    q = select(InvoiceRecordModel).order_by(InvoiceRecordModel.created_at.desc()).limit(limit)
    if status:
        q = q.where(InvoiceRecordModel.status == status)
    rows = (await session.execute(q)).scalars().all()
    return [InvoiceOut.from_model(r, full_access=full_access) for r in rows]
