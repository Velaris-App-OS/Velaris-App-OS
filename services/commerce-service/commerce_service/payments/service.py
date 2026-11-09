"""P48 HxConnect Payment service.

Responsibilities:
  - Create a Stripe checkout session and persist a payment_requests row
  - Handle inbound Stripe webhook events (HMAC verified)
  - On payment success: update status + create a step completion + fire HxStream event
  - Refund a succeeded payment
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import (
    CaseInstanceModel,
    CaseStepCompletionModel,
    ConnectorRegistryModel,
    PaymentDisbursementModel,
    PaymentRequestModel,
    PaymentWebhookEventModel,
)
from velaris_shared.compliance import ledger_posting
from velaris_shared.hxbridge.encryption import decrypt_credentials
from velaris_shared.hxbridge.connectors.stripe_connector import StripeConnector

logger = logging.getLogger(__name__)


# ── helpers ───────────────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _get_stripe_connector(
    session: AsyncSession,
    connector_id: uuid.UUID | None,
    tenant_id: str,
) -> tuple[StripeConnector, ConnectorRegistryModel]:
    """Load and decrypt a Stripe connector from the registry."""
    q = select(ConnectorRegistryModel).where(
        ConnectorRegistryModel.connector_type == "stripe",
        ConnectorRegistryModel.enabled == True,  # noqa: E712
    )
    if connector_id:
        q = q.where(ConnectorRegistryModel.id == connector_id)
    else:
        q = q.where(ConnectorRegistryModel.tenant_id == tenant_id)

    row = (await session.execute(q.limit(1))).scalar_one_or_none()
    if row is None:
        raise ValueError("No enabled Stripe connector found for this tenant")
    creds = decrypt_credentials(row.credentials)
    return StripeConnector(config=row.config, credentials=creds), row


async def _emit_hxstream(case_id: uuid.UUID, event_type: str, data: dict) -> None:
    """Fire an HxStream event without blocking."""
    try:
        # `emit_event` never existed in the emitter, so until 2026-09-29 every
        # payment event failed here and was logged as a warning.
        from velaris_shared.hxstream.emitter import emit_trace
        await emit_trace(event_type, data, case_id=case_id)
    except Exception as exc:
        logger.warning("HxStream emit failed (%s): %s", event_type, exc)


# ── public API ────────────────────────────────────────────────────────────────

async def create_payment_request(
    session: AsyncSession,
    case_id: uuid.UUID,
    step_id: str,
    amount_cents: int,
    currency: str,
    description: str,
    tenant_id: str,
    connector_id: uuid.UUID | None = None,
    customer_email: str | None = None,
    idempotency_key: str | None = None,
) -> PaymentRequestModel:
    """Create a Stripe checkout session and persist the payment request."""
    connector, reg = await _get_stripe_connector(session, connector_id, tenant_id)

    result = await connector.execute({
        "operation":       "checkout_session",
        "amount_cents":    amount_cents,
        "currency":        currency,
        "description":     description,
        "customer_email":  customer_email or "",
        "idempotency_key": idempotency_key or str(uuid.uuid4()),
    })

    pr = PaymentRequestModel(
        tenant_id    = tenant_id,
        case_id      = case_id,
        step_id      = step_id,
        connector_id = reg.id,
        provider     = "stripe",
        provider_ref = result.get("payment_intent_id"),
        checkout_url = result.get("checkout_url"),
        amount_cents = amount_cents,
        currency     = currency,
        status       = "pending",
        description  = description,
        payment_meta = {"session_id": result.get("session_id"), "source": "hxconnect"},
    )
    session.add(pr)
    await session.flush()

    await _emit_hxstream(case_id, "payment_initiated", {
        "payment_request_id": str(pr.id),
        "provider":           "stripe",
        "amount_cents":       amount_cents,
        "currency":           currency,
        "checkout_url":       pr.checkout_url,
        "step_id":            step_id,
    })

    return pr


async def handle_stripe_webhook(
    session: AsyncSession,
    payload: bytes,
    sig_header: str,
    tenant_id: str | None = None,
) -> dict:
    """Process a raw Stripe webhook. Returns a result summary."""
    import json

    body = json.loads(payload)
    event_type   = body.get("type", "")
    payment_obj  = body.get("data", {}).get("object", {})
    provider_ref = payment_obj.get("id") or payment_obj.get("payment_intent")

    # Persist the raw event first (even if verification fails)
    evt = PaymentWebhookEventModel(
        provider     = "stripe",
        event_type   = event_type,
        provider_ref = provider_ref,
        payload      = body,
        verified     = False,
    )
    session.add(evt)
    await session.flush()

    # Find the payment request to determine which connector to use for HMAC
    pr: PaymentRequestModel | None = None
    if provider_ref:
        pr = (await session.execute(
            select(PaymentRequestModel).where(PaymentRequestModel.provider_ref == provider_ref)
        )).scalar_one_or_none()

    # Verify HMAC using the matching connector's webhook_secret
    verified = False
    if pr and pr.connector_id and sig_header:
        try:
            reg = (await session.execute(
                select(ConnectorRegistryModel).where(ConnectorRegistryModel.id == pr.connector_id)
            )).scalar_one_or_none()
            if reg:
                creds = decrypt_credentials(reg.credentials)
                sc = StripeConnector(config=reg.config, credentials=creds)
                verified = sc.verify_webhook(payload, sig_header)
        except Exception as exc:
            logger.warning("Stripe HMAC error: %s", exc)
    # FAIL CLOSED (2026-08-15). This used to read:
    #
    #     elif not sig_header:
    #         verified = True          # dev/test
    #     if not verified and sig_header:
    #         return {{"status": "rejected", ...}}
    #
    # so a caller who sent NO signature header at all was treated as verified,
    # and the rejection could only fire for someone who had bothered to send a
    # (bad) signature. Proven exploitable anonymously: an unsigned POST to
    # /api/v1/payments/webhooks/stripe marked a real payment request
    # `succeeded`, completed its workflow step and posted to the ledger.
    #
    # Unsigned is now simply unverified. The tests already sign their webhooks,
    # so nothing legitimate depended on the shortcut.

    evt.verified = verified
    if not verified:
        evt.error = ("HMAC verification failed" if sig_header
                     else "unsigned webhook rejected")
        await session.commit()
        return {"status": "rejected", "reason": "invalid_signature"}

    # Process the event
    if event_type == "payment_intent.succeeded" and pr:
        pr.status       = "succeeded"
        pr.completed_at = _utcnow()
        evt.processed   = True
        await session.flush()   # persist status before attempting step completion
        try:
            async with session.begin_nested():
                await _on_payment_succeeded(session, pr)
        except Exception as exc:
            logger.warning("Post-payment step completion failed (non-fatal): %s", exc)

        # HxLedger (§8.4): money arrived, so the books must say so. Nested so a
        # ledger problem cannot roll back the payment status written above —
        # a failed post surfaces in /ledger/reconcile, which is what it is for.
        try:
            async with session.begin_nested():
                await ledger_posting.post_capture(session, pr)
        except Exception as exc:
            logger.warning("Ledger capture posting failed (non-fatal): %s", exc)

    elif event_type in ("payment_intent.payment_failed", "checkout.session.expired") and pr:
        pr.status     = "failed"
        evt.processed = True
        await _emit_hxstream(pr.case_id, "payment_failed", {
            "payment_request_id": str(pr.id),
            "event_type":         event_type,
            "step_id":            pr.step_id,
        })

    elif event_type == "charge.refunded" and pr:
        pr.status     = "refunded"
        evt.processed = True

        # A refund is its own transaction, not a reversal of the capture —
        # both events really happened. Without it the refunded amount stays on
        # the books while dropping out of the captured set, and reconciliation
        # reports a discrepancy that is not one.
        try:
            async with session.begin_nested():
                await ledger_posting.post_refund(session, pr)
        except Exception as exc:
            logger.warning("Ledger refund posting failed (non-fatal): %s", exc)

        await _emit_hxstream(pr.case_id, "payment_refunded", {
            "payment_request_id": str(pr.id),
            "step_id":            pr.step_id,
        })

    await session.commit()
    return {"status": "ok", "event_type": event_type, "processed": evt.processed}


async def _on_payment_succeeded(session: AsyncSession, pr: PaymentRequestModel) -> None:
    """Create step completion and attempt stage auto-advance."""
    now      = _utcnow()
    stage_id = (await _get_current_stage(session, pr.case_id)) or "unknown"

    # DB-agnostic upsert: check existence first, then insert or update
    existing = (await session.execute(
        select(CaseStepCompletionModel).where(
            CaseStepCompletionModel.case_id == pr.case_id,
            CaseStepCompletionModel.step_id == pr.step_id,
        )
    )).scalar_one_or_none()

    if existing:
        existing.status       = "completed"
        existing.completed_at = now
        existing.data         = {"payment_request_id": str(pr.id), "provider": "stripe"}
    else:
        session.add(CaseStepCompletionModel(
            case_id      = pr.case_id,
            step_id      = pr.step_id,
            stage_id     = stage_id,
            step_type    = "payment_request",
            status       = "completed",
            data         = {"payment_request_id": str(pr.id), "provider": "stripe"},
            completed_by = "stripe_webhook",
            completed_at = now,
        ))
    await session.flush()

    await _emit_hxstream(pr.case_id, "payment_succeeded", {
        "payment_request_id": str(pr.id),
        "amount_cents":       pr.amount_cents,
        "currency":           pr.currency,
        "step_id":            pr.step_id,
    })

    # Auto-advance if all required steps are complete — case-service's call
    # (internal API, R5e step 7); a no-op when the stage is not complete.
    try:
        from velaris_shared.internal import client as internal
        await internal.call("cases/auto-advance", {"case_id": str(pr.case_id)}, session=session)
    except Exception as exc:
        logger.warning("Auto-advance after payment succeeded failed: %s", exc)


async def _get_current_stage(session: AsyncSession, case_id: uuid.UUID) -> str:
    case = (await session.execute(
        select(CaseInstanceModel).where(CaseInstanceModel.id == case_id)
    )).scalar_one_or_none()
    return case.current_stage_id if case else ""


async def list_payment_requests(
    session: AsyncSession,
    case_id: uuid.UUID,
    limit: int = 50,
) -> list[PaymentRequestModel]:
    rows = (await session.execute(
        select(PaymentRequestModel)
        .where(PaymentRequestModel.case_id == case_id)
        .order_by(PaymentRequestModel.created_at.desc())
        .limit(limit)
    )).scalars().all()
    return list(rows)


async def get_payment_request(
    session: AsyncSession,
    payment_id: uuid.UUID,
) -> PaymentRequestModel | None:
    return (await session.execute(
        select(PaymentRequestModel).where(PaymentRequestModel.id == payment_id)
    )).scalar_one_or_none()


async def refund_payment(
    session: AsyncSession,
    payment_id: uuid.UUID,
    amount_cents: int | None = None,
) -> PaymentRequestModel:
    pr = await get_payment_request(session, payment_id)
    if pr is None:
        raise ValueError("Payment request not found")
    if pr.status != "succeeded":
        raise ValueError(f"Cannot refund a payment with status '{pr.status}'")

    reg = (await session.execute(
        select(ConnectorRegistryModel).where(ConnectorRegistryModel.id == pr.connector_id)
    )).scalar_one_or_none()
    if reg is None:
        raise ValueError("Connector not found")

    creds = decrypt_credentials(reg.credentials)
    sc = StripeConnector(config=reg.config, credentials=creds)
    result = await sc.execute({
        "operation":         "refund",
        "payment_intent_id": pr.provider_ref,
        "amount_cents":      amount_cents,
    })

    pr.status       = "refunded"
    pr.completed_at = _utcnow()
    pr.payment_meta = {**(pr.payment_meta or {}), "refund_id": result.get("refund_id")}
    await session.commit()

    await _emit_hxstream(pr.case_id, "payment_refunded", {
        "payment_request_id": str(pr.id),
        "refund_id":          result.get("refund_id"),
        "amount_cents":       result.get("amount_cents"),
        "step_id":            pr.step_id,
    })
    return pr


# ── Disbursement (pay to customer) ────────────────────────────────────────────

async def create_disbursement(
    session: AsyncSession,
    case_id: uuid.UUID,
    step_id: str,
    amount_cents: int,
    currency: str,
    description: str,
    tenant_id: str,
    actor: str,
    bank_reference: str | None = None,
    notes: str | None = None,
) -> PaymentDisbursementModel:
    """Record a confirmed disbursement, complete the step, and fire HxStream."""
    case_check = (await session.execute(
        select(CaseInstanceModel).where(CaseInstanceModel.id == case_id)
    )).scalar_one_or_none()
    if case_check is None:
        raise ValueError(f"Case {case_id} not found")

    now = _utcnow()
    d = PaymentDisbursementModel(
        tenant_id      = tenant_id,
        case_id        = case_id,
        step_id        = step_id,
        amount_cents   = amount_cents,
        currency       = currency,
        status         = "confirmed",
        description    = description,
        bank_reference = bank_reference,
        notes          = notes,
        confirmed_by   = actor,
        confirmed_at   = now,
    )
    session.add(d)
    await session.flush()   # persist disbursement row first

    # Step completion + auto-advance in a savepoint so any failure is isolated
    try:
        async with session.begin_nested():
            stage_id = (await _get_current_stage(session, case_id)) or "unknown"
            existing = (await session.execute(
                select(CaseStepCompletionModel).where(
                    CaseStepCompletionModel.case_id == case_id,
                    CaseStepCompletionModel.step_id == step_id,
                )
            )).scalar_one_or_none()

            if existing:
                existing.status       = "completed"
                existing.completed_at = now
                existing.data         = {"disbursement_id": str(d.id), "amount_cents": amount_cents}
            else:
                session.add(CaseStepCompletionModel(
                    case_id      = case_id,
                    step_id      = step_id,
                    stage_id     = stage_id,
                    step_type    = "payment_disbursement",
                    status       = "completed",
                    data         = {"disbursement_id": str(d.id), "amount_cents": amount_cents},
                    completed_by = actor,
                    completed_at = now,
                ))
    except Exception as exc:
        logger.warning("Disbursement step completion failed (non-fatal): %s", exc)

    # Auto-advance in a separate savepoint — case-service's call (internal API,
    # R5e step 7); a no-op when the stage is not complete.
    try:
        async with session.begin_nested():
            from velaris_shared.internal import client as internal
            await internal.call("cases/auto-advance", {"case_id": str(case_id)},
                                session=session)
    except Exception as exc:
        logger.warning("Auto-advance after disbursement failed (non-fatal): %s", exc)

    await _emit_hxstream(case_id, "payment_disbursed", {
        "disbursement_id": str(d.id),
        "amount_cents":    amount_cents,
        "currency":        currency,
        "step_id":         step_id,
        "actor":           actor,
    })
    return d


async def list_disbursements(
    session: AsyncSession,
    case_id: uuid.UUID,
) -> list[PaymentDisbursementModel]:
    rows = (await session.execute(
        select(PaymentDisbursementModel)
        .where(PaymentDisbursementModel.case_id == case_id)
        .order_by(PaymentDisbursementModel.created_at.desc())
    )).scalars().all()
    return list(rows)


async def list_webhook_events(
    session: AsyncSession,
    limit: int = 100,
) -> list[PaymentWebhookEventModel]:
    rows = (await session.execute(
        select(PaymentWebhookEventModel)
        .order_by(PaymentWebhookEventModel.received_at.desc())
        .limit(limit)
    )).scalars().all()
    return list(rows)
