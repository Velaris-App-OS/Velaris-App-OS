"""P48 HxConnect — Payment & Financial API.

Endpoints:
  POST /payments/cases/{case_id}/charge          initiate a payment request
  GET  /payments/cases/{case_id}/requests        list payment requests for a case
  GET  /payments/requests/{id}                   get a single payment request
  POST /payments/requests/{id}/refund            refund a succeeded payment
  POST /payments/webhooks/stripe                 Stripe webhook receiver (HMAC verified)
  GET  /payments/connectors                      list enabled payment connectors
  POST /payments/connectors/{id}/test            test a payment connector
  GET  /payments/webhooks                        list recent webhook events
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared import hxguard
from velaris_shared.auth.dependencies import get_current_user
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.db.models import (
    CaseAuditLogModel, ConnectorRegistryModel,
    PaymentDisbursementModel, PaymentRequestModel, PaymentWebhookEventModel,
)
from velaris_shared.db.session import get_session
from velaris_shared.hxbridge.encryption import (
    decrypt_credentials,
    encrypt_credentials,
    mask_credentials,
)
from commerce_service.payments import service as payment_svc

import logging

logger = logging.getLogger(__name__)

# ── Bank reference encryption helpers (SD-1) ─────────────────────────────────

# Moved to the shared layer (step 7): the portal records disbursements too.
from velaris_shared.core.bank_ref import _encrypt_bank_ref  # noqa: E402,F401


def _decrypt_bank_ref(stored: str) -> str:
    """Decrypt an encrypted bank reference. Falls back gracefully for legacy plaintext."""
    try:
        d = json.loads(stored)
        if "_enc" in d:
            return decrypt_credentials(d).get("v", stored)
    except Exception:
        pass
    return stored  # legacy plaintext — safe fallback


def _is_encrypted_ref(value: str | None) -> bool:
    if not value:
        return False
    try:
        return "_enc" in json.loads(value)
    except Exception:
        return False


def _mask_bank_ref(value: str | None) -> str | None:
    """Return masked bank reference for API responses: ****XXXX (last 4 chars visible)."""
    if not value:
        return value
    plain = _decrypt_bank_ref(value) if _is_encrypted_ref(value) else value
    clean = plain.replace(" ", "").replace("-", "")
    if len(clean) >= 4:
        return "*" * max(0, len(clean) - 4) + clean[-4:]
    return "****"


def _has_finance_access(user: AuthenticatedUser) -> bool:
    """Phase 4 step 2 — same redaction question as `crm`/`cases`, same action."""
    return hxguard.user_allows(user, "finance.details.read")

router = APIRouter(prefix="/payments", tags=["payments"])

#: The connector types this router lists and tests.
_PAYMENT_CONNECTOR_TYPES = ("stripe", "paypal", "adyen")


# ── Authorization (2026-09-29) ────────────────────────────────────────────────
#
# Every route here depended on `get_current_user` and nothing else until
# 2026-09-29. Any signed-in user of any role in any tenant could record a
# disbursement on another tenant's case, mark it sent (a ledger posting) and
# refund its payment; the lists returned every tenant's rows. The unauthenticated
# sweep (`test_ungated_routes.py`) could not see it — these routes HAD an
# identity. Now: a privilege per route, and the case named in the path, or the
# case a payment row belongs to, must be one the caller may reach. Another
# tenant's case answers 404, exactly as a missing one does.


async def _reach_case(session: AsyncSession, user: AuthenticatedUser,
                      case_id: uuid.UUID) -> None:
    await hxguard.require_case(session, user, "case.read", case_id)


async def _my_tenant_tokens(session: AsyncSession,
                            user: AuthenticatedUser) -> list[str] | None:
    from velaris_shared.tenancy.identity import caller_tenant_tokens

    return await caller_tenant_tokens(session, user)


async def _payment_or_404(session: AsyncSession, user: AuthenticatedUser,
                          payment_id: uuid.UUID) -> PaymentRequestModel:
    pr = await session.get(PaymentRequestModel, payment_id)
    if pr is None:
        raise HTTPException(404, "Payment request not found")
    try:
        await _reach_case(session, user, pr.case_id)
    except HTTPException:
        raise HTTPException(404, "Payment request not found")
    return pr


async def _disbursement_or_404(session: AsyncSession, user: AuthenticatedUser,
                               disbursement_id: uuid.UUID) -> PaymentDisbursementModel:
    disb = await session.get(PaymentDisbursementModel, disbursement_id)
    if disb is None:
        raise HTTPException(404, "Disbursement not found")
    try:
        await _reach_case(session, user, disb.case_id)
    except HTTPException:
        raise HTTPException(404, "Disbursement not found")
    return disb


# ── Schemas ───────────────────────────────────────────────────────────────────

class ChargeBody(BaseModel):
    step_id:        str
    amount_cents:   int
    currency:       str = "usd"
    description:    str = "Payment"
    connector_id:   Optional[uuid.UUID] = None
    customer_email: Optional[str] = None

class RefundBody(BaseModel):
    amount_cents: Optional[int] = None   # None = full refund

class PaymentRequestOut(BaseModel):
    id:           uuid.UUID
    case_id:      uuid.UUID
    step_id:      str
    provider:     str
    provider_ref: Optional[str]
    checkout_url: Optional[str]
    amount_cents: int
    currency:     str
    status:       str
    description:  Optional[str]
    metadata:     dict
    created_at:   str
    completed_at: Optional[str]

    @classmethod
    def from_model(cls, pr: PaymentRequestModel) -> "PaymentRequestOut":
        return cls(
            id           = pr.id,
            case_id      = pr.case_id,
            step_id      = pr.step_id,
            provider     = pr.provider,
            provider_ref = pr.provider_ref,
            checkout_url = pr.checkout_url,
            amount_cents = pr.amount_cents,
            currency     = pr.currency,
            status       = pr.status,
            description  = pr.description,
            metadata     = pr.payment_meta or {},
            created_at   = pr.created_at.isoformat(),
            completed_at = pr.completed_at.isoformat() if pr.completed_at else None,
        )

    model_config = {"from_attributes": True}


# ── Case-scoped payment endpoints ─────────────────────────────────────────────

@router.post("/cases/{case_id}/charge", response_model=PaymentRequestOut)
async def initiate_charge(
    case_id: uuid.UUID,
    body: ChargeBody,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("payments.requests.write")),
):
    """Create a Stripe checkout session for a case step.

    Returns a checkout_url the customer follows to complete payment.
    The case step auto-advances when Stripe confirms payment via webhook.
    """
    await _reach_case(session, user, case_id)
    if body.connector_id is not None:
        # A named connector must be this tenant's or platform-wide. Without this,
        # a caller could take a payment through another tenant's Stripe account.
        mine = await _my_tenant_tokens(session, user)
        row = await session.get(ConnectorRegistryModel, body.connector_id)
        if row is None or (mine is not None and row.tenant_id is not None
                           and row.tenant_id not in mine):
            raise HTTPException(404, "Connector not found")
    try:
        pr = await payment_svc.create_payment_request(
            session        = session,
            case_id        = case_id,
            step_id        = body.step_id,
            amount_cents   = body.amount_cents,
            currency       = body.currency,
            description    = body.description,
            tenant_id      = getattr(user, "tenant_id", None) or "default",
            connector_id   = body.connector_id,
            customer_email = body.customer_email,
        )
        await session.commit()
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        await session.rollback()
        raise HTTPException(502, f"Payment gateway error: {exc}")
    return PaymentRequestOut.from_model(pr)


# ── Disbursement endpoints ────────────────────────────────────────────────────

class DisburseBody(BaseModel):
    step_id:        str
    amount_cents:   int
    currency:       str = "usd"
    description:    str = "Disbursement"
    bank_reference: Optional[str] = None
    notes:          Optional[str] = None

class DisbursementOut(BaseModel):
    id:                       uuid.UUID
    case_id:                  uuid.UUID
    step_id:                  str
    amount_cents:             int
    currency:                 str
    status:                   str
    description:              Optional[str]
    bank_reference:           Optional[str]   # always masked (SD-1)
    notes:                    Optional[str]
    confirmed_by:             Optional[str]
    confirmed_at:             Optional[str]
    disbursement_executed:    bool = False
    disbursement_executed_at: Optional[str] = None
    created_at:               str

    @classmethod
    def from_model(cls, d: PaymentDisbursementModel) -> "DisbursementOut":
        return cls(
            id                       = d.id,
            case_id                  = d.case_id,
            step_id                  = d.step_id,
            amount_cents             = d.amount_cents,
            currency                 = d.currency,
            status                   = d.status,
            description              = d.description,
            bank_reference           = _mask_bank_ref(d.bank_reference),   # SD-1: always masked
            notes                    = d.notes,
            confirmed_by             = d.confirmed_by,
            confirmed_at             = d.confirmed_at.isoformat() if d.confirmed_at else None,
            disbursement_executed    = getattr(d, "disbursement_executed", False) or False,
            disbursement_executed_at = d.disbursement_executed_at.isoformat() if getattr(d, "disbursement_executed_at", None) else None,
            created_at               = d.created_at.isoformat(),
        )

    model_config = {"from_attributes": True}


@router.post("/cases/{case_id}/disburse", response_model=DisbursementOut)
async def initiate_disbursement(
    case_id: uuid.UUID,
    body: DisburseBody,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("payments.disbursements.write")),
):
    """Confirm a pay-to-customer disbursement on a case step.

    Records the disbursement, completes the step, and auto-advances the stage.
    Actual fund transfer is fulfilled externally (bank transfer, SWIFT, etc.) —
    tracked here for audit and case lifecycle purposes.
    """
    await _reach_case(session, user, case_id)
    # SD-1: encrypt bank_reference before storage, under this tenant's DEK.
    # Resolved once, so the key the value is written under and the tenant
    # recorded on the row cannot disagree about whose data this is.
    _tenant = getattr(user, "tenant_id", None) or "default"
    encrypted_ref = (
        await _encrypt_bank_ref(session, body.bank_reference, _tenant)
        if body.bank_reference else None
    )
    try:
        d = await payment_svc.create_disbursement(
            session        = session,
            case_id        = case_id,
            step_id        = body.step_id,
            amount_cents   = body.amount_cents,
            currency       = body.currency,
            description    = body.description,
            tenant_id      = _tenant,
            actor          = user.email or user.user_id or "unknown",
            bank_reference = encrypted_ref,
            notes          = body.notes,
        )
        await session.commit()
    except Exception as exc:
        await session.rollback()
        raise HTTPException(400, str(exc))
    return DisbursementOut.from_model(d)


@router.get("/cases/{case_id}/disbursements", response_model=list[DisbursementOut])
async def list_case_disbursements(
    case_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("payments.requests.read")),
):
    await _reach_case(session, user, case_id)
    rows = await payment_svc.list_disbursements(session, case_id)
    return [DisbursementOut.from_model(r) for r in rows]


@router.post("/disbursements/{disbursement_id}/reveal")
async def reveal_bank_reference(
    disbursement_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """SD-1: Return decrypted bank reference. Requires finance or admin role. Always audited."""
    if not _has_finance_access(user):
        raise HTTPException(403, "finance or admin role required to view bank references")
    disb = await _disbursement_or_404(session, user, disbursement_id)
    try:
        decrypted = _decrypt_bank_ref(disb.bank_reference) if disb.bank_reference else None
    except Exception:
        raise HTTPException(500, "Could not decrypt bank reference")
    # Audit every reveal — immutable record of who viewed what
    session.add(CaseAuditLogModel(
        case_id = disb.case_id,
        action  = "bank_reference_revealed",
        actor   = user.username or user.user_id or "unknown",
        details = {"disbursement_id": str(disbursement_id), "roles": user.roles},
    ))
    await session.commit()
    return {"bank_reference": decrypted, "disbursement_id": str(disbursement_id)}


@router.post("/disbursements/{disbursement_id}/mark-sent")
async def mark_disbursement_sent(
    disbursement_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("payments.disbursements.execute")),
):
    """SD-3: Staff confirms the bank transfer was processed externally."""
    disb = await _disbursement_or_404(session, user, disbursement_id)
    disb.disbursement_executed    = True
    disb.disbursement_executed_at = datetime.now(timezone.utc)
    disb.status                   = "executed"
    disb.updated_at               = datetime.now(timezone.utc)

    # HxLedger (§8.4): money has now actually left, which is the point at which
    # it belongs on the books. Nested so a ledger fault cannot undo the
    # confirmation — the transfer happened in the real world either way.
    try:
        from velaris_shared.compliance import ledger_posting

        async with session.begin_nested():
            await ledger_posting.post_disbursement(session, disb)
    except Exception as exc:
        logger.warning("Ledger disbursement posting failed (non-fatal): %s", exc)

    await session.commit()
    return {
        "ok": True,
        "disbursement_id": str(disbursement_id),
        "executed_at": disb.disbursement_executed_at.isoformat(),
    }


@router.get("/cases/{case_id}/requests", response_model=list[PaymentRequestOut])
async def list_case_payments(
    case_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("payments.requests.read")),
):
    """List all payment requests associated with a case."""
    await _reach_case(session, user, case_id)
    rows = await payment_svc.list_payment_requests(session, case_id)
    return [PaymentRequestOut.from_model(r) for r in rows]


@router.get("/requests/{payment_id}", response_model=PaymentRequestOut)
async def get_payment(
    payment_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("payments.requests.read")),
):
    pr = await _payment_or_404(session, user, payment_id)
    return PaymentRequestOut.from_model(pr)


@router.post("/requests/{payment_id}/refund", response_model=PaymentRequestOut)
async def refund_payment(
    payment_id: uuid.UUID,
    body: RefundBody,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("payments.refunds.write")),
):
    """Refund a succeeded payment — full or partial."""
    await _payment_or_404(session, user, payment_id)
    try:
        pr = await payment_svc.refund_payment(session, payment_id, body.amount_cents)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        await session.rollback()
        raise HTTPException(502, f"Refund error: {exc}")
    return PaymentRequestOut.from_model(pr)


# ── Webhook receiver ──────────────────────────────────────────────────────────

@router.post("/webhooks/stripe", status_code=200)
async def stripe_webhook(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Receive and process Stripe webhook events.

    No authentication — Stripe sends events here directly.
    Integrity is verified via HMAC-SHA256 (Stripe-Signature header).
    """
    payload    = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    try:
        result = await payment_svc.handle_stripe_webhook(
            session    = session,
            payload    = payload,
            sig_header = sig_header,
        )
    except Exception as exc:
        raise HTTPException(400, f"Webhook processing error: {exc}")

    if result.get("status") == "rejected":
        raise HTTPException(400, result.get("reason", "rejected"))
    return result


# ── Connector management ──────────────────────────────────────────────────────

class ConnectorOut(BaseModel):
    id:             uuid.UUID
    name:           str
    connector_type: str
    description:    Optional[str]
    config:         dict
    credentials:    dict   # masked
    enabled:        bool
    last_tested_at: Optional[str]
    last_test_ok:   Optional[bool]

    model_config = {"from_attributes": True}


@router.get("/connectors", response_model=list[ConnectorOut])
async def list_payment_connectors(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("payments.connectors.read")),
):
    """List the payment connectors registered for the tenant.

    It said "for the tenant" and returned every tenant's. Scoped as the HxBridge
    connector list is: the caller's tenant plus platform-wide (NULL) rows.
    """
    from sqlalchemy import or_

    q = select(ConnectorRegistryModel).where(
        ConnectorRegistryModel.connector_type.in_(list(_PAYMENT_CONNECTOR_TYPES)),
    ).order_by(ConnectorRegistryModel.created_at.desc())
    mine = await _my_tenant_tokens(session, user)
    if mine is not None:
        q = q.where(or_(ConnectorRegistryModel.tenant_id.in_(mine),
                        ConnectorRegistryModel.tenant_id.is_(None)))
    rows = (await session.execute(q)).scalars().all()

    return [
        ConnectorOut(
            id             = r.id,
            name           = r.name,
            connector_type = r.connector_type,
            description    = r.description,
            config         = r.config or {},
            credentials    = mask_credentials(r.credentials),
            enabled        = r.enabled,
            last_tested_at = r.last_tested_at.isoformat() if r.last_tested_at else None,
            last_test_ok   = r.last_test_ok,
        )
        for r in rows
    ]


@router.post("/connectors/{connector_id}/test")
async def test_payment_connector(
    connector_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("payments.connectors.test")),
):
    """Test a payment connector's credentials."""
    from datetime import datetime, timezone
    row = (await session.execute(
        select(ConnectorRegistryModel).where(ConnectorRegistryModel.id == connector_id)
    )).scalar_one_or_none()
    mine = await _my_tenant_tokens(session, user)
    # Another tenant's connector answers as a missing one. Testing it decrypts
    # its credentials and calls the provider with them.
    if row is None or (mine is not None and row.tenant_id is not None
                       and row.tenant_id not in mine):
        raise HTTPException(404, "Connector not found")
    # The payments route tests PAYMENT connectors — the same set its list shows.
    # (Since the commerce split only those register in this service.)
    if row.connector_type not in _PAYMENT_CONNECTOR_TYPES:
        raise HTTPException(404, "Connector not found")

    from velaris_shared.hxbridge.protocol import get_connector
    import velaris_shared.hxbridge.connectors.stripe_connector  # noqa: F401  (registers "stripe")

    creds = decrypt_credentials(row.credentials)
    try:
        connector = get_connector(row.connector_type, row.config or {}, creds)
        ok = await connector.test()
    except Exception as exc:
        ok = False
        row.last_test_ok = False
        row.last_tested_at = datetime.now(timezone.utc)
        await session.commit()
        raise HTTPException(502, f"Connector test failed: {exc}")

    row.last_test_ok   = ok
    row.last_tested_at = datetime.now(timezone.utc)
    await session.commit()
    return {"ok": ok, "connector_id": str(connector_id)}


# ── Webhook event log ─────────────────────────────────────────────────────────

class WebhookEventOut(BaseModel):
    id:           uuid.UUID
    provider:     str
    event_type:   Optional[str]
    provider_ref: Optional[str]
    verified:     bool
    processed:    bool
    error:        Optional[str]
    received_at:  str

    model_config = {"from_attributes": True}


@router.get("/webhooks", response_model=list[WebhookEventOut])
async def list_webhook_events(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("payments.connectors.read")),
):
    """List recent inbound payment webhook events.

    The event log has no tenant column — a provider posts to one platform URL —
    so it is platform-wide, and break-glass only. A tenant admin reading it
    would read every tenant's payment traffic.
    """
    from velaris_shared.tenancy.identity import is_superadmin

    if not is_superadmin(user):
        raise HTTPException(403, "The payment webhook log is platform-wide; superadmin only.")
    rows = await payment_svc.list_webhook_events(session)
    return [
        WebhookEventOut(
            id           = r.id,
            provider     = r.provider,
            event_type   = r.event_type,
            provider_ref = r.provider_ref,
            verified     = r.verified,
            processed    = r.processed,
            error        = r.error,
            received_at  = r.received_at.isoformat(),
        )
        for r in rows
    ]


@router.get("/requests", response_model=list[PaymentRequestOut])
async def list_all_payment_requests(
    status: str | None = None,
    limit: int = 100,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("payments.requests.read")),
):
    from sqlalchemy import select
    from velaris_shared.db.models import PaymentRequestModel
    q = select(PaymentRequestModel).order_by(PaymentRequestModel.created_at.desc()).limit(max(1, min(limit, 500)))
    if status:
        q = q.where(PaymentRequestModel.status == status)
    mine = await _my_tenant_tokens(session, user)
    if mine is not None:
        q = q.where(PaymentRequestModel.tenant_id.in_(mine))
    rows = (await session.execute(q)).scalars().all()
    # Returned raw ORM rows until 2026-09-29, which cannot serialise (`metadata`
    # is SQLAlchemy's MetaData on the model) — so this list 500'd whenever it
    # had anything to list.
    return [PaymentRequestOut.from_model(r) for r in rows]


@router.get("/disbursements", response_model=list[DisbursementOut])
async def list_all_disbursements(
    status: str | None = None,
    limit: int = 100,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("payments.requests.read")),
):
    from sqlalchemy import select
    from velaris_shared.db.models import PaymentDisbursementModel
    q = select(PaymentDisbursementModel).order_by(PaymentDisbursementModel.created_at.desc()).limit(max(1, min(limit, 500)))
    if status:
        q = q.where(PaymentDisbursementModel.status == status)
    mine = await _my_tenant_tokens(session, user)
    if mine is not None:
        q = q.where(PaymentDisbursementModel.tenant_id.in_(mine))
    rows = (await session.execute(q)).scalars().all()
    # Returned raw ORM rows until 2026-09-29, which cannot serialise (`metadata`
    # is SQLAlchemy's MetaData on the model) — so this list 500'd whenever it
    # had anything to list.
    return [DisbursementOut.from_model(r) for r in rows]
