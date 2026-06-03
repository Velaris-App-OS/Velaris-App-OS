"""P50 CRM & Accounting service — Salesforce + Xero outbound."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import (
    CaseInstanceModel,
    CaseStepCompletionModel,
    ConnectorRegistryModel,
    CrmSyncRecordModel,
    InvoiceRecordModel,
)
from case_service.hxbridge.encryption import decrypt_credentials

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _emit(case_id: uuid.UUID, event_type: str, data: dict) -> None:
    try:
        from case_service.hxstream.emitter import emit_event
        await emit_event(str(case_id), event_type, data)
    except Exception as exc:
        logger.warning("HxStream emit failed (%s): %s", event_type, exc)


async def _get_connector(session: AsyncSession, connector_type: str, connector_id: uuid.UUID | None, tenant_id: str):
    q = select(ConnectorRegistryModel).where(
        ConnectorRegistryModel.connector_type == connector_type,
        ConnectorRegistryModel.enabled == True,  # noqa: E712
    )
    if connector_id:
        q = q.where(ConnectorRegistryModel.id == connector_id)
    else:
        q = q.where(ConnectorRegistryModel.tenant_id == tenant_id)
    row = (await session.execute(q.limit(1))).scalar_one_or_none()
    if row is None:
        raise ValueError(f"No enabled {connector_type} connector found")
    return row, decrypt_credentials(row.credentials)


async def _complete_step(session: AsyncSession, case_id: uuid.UUID, step_id: str, step_type: str, actor: str, data: dict) -> None:
    now = _utcnow()
    try:
        async with session.begin_nested():
            case     = (await session.execute(select(CaseInstanceModel).where(CaseInstanceModel.id == case_id))).scalar_one_or_none()
            stage_id = (case.current_stage_id if case else None) or "unknown"
            existing = (await session.execute(
                select(CaseStepCompletionModel).where(
                    CaseStepCompletionModel.case_id == case_id,
                    CaseStepCompletionModel.step_id == step_id,
                )
            )).scalar_one_or_none()
            if existing:
                existing.status = "completed"; existing.completed_at = now; existing.data = data
            else:
                session.add(CaseStepCompletionModel(
                    case_id=case_id, step_id=step_id, stage_id=stage_id,
                    step_type=step_type, status="completed", data=data,
                    completed_by=actor, completed_at=now,
                ))
    except Exception as exc:
        logger.warning("Step completion failed (non-fatal): %s", exc)


async def _auto_advance(session: AsyncSession, case_id: uuid.UUID) -> None:
    try:
        async with session.begin_nested():
            from case_service.db.models import CaseTypeModel
            case = (await session.execute(select(CaseInstanceModel).where(CaseInstanceModel.id == case_id))).scalar_one_or_none()
            if case and case.case_type_id:
                ct = (await session.execute(select(CaseTypeModel).where(CaseTypeModel.id == case.case_type_id))).scalar_one_or_none()
                if ct:
                    from case_service.api.routers.cases import _auto_advance_if_complete
                    await _auto_advance_if_complete(session, case_id, case.current_stage_id, ct.definition_json)
    except Exception as exc:
        logger.warning("Auto-advance failed (non-fatal): %s", exc)


# ── CRM sync ──────────────────────────────────────────────────────────────────

async def sync_to_crm(
    session: AsyncSession,
    case_id: uuid.UUID,
    step_id: str,
    tenant_id: str,
    actor: str,
    first_name: str,
    last_name: str,
    email: str,
    subject: str,
    description: str = "",
    connector_id: uuid.UUID | None = None,
) -> CrmSyncRecordModel:
    case = (await session.execute(select(CaseInstanceModel).where(CaseInstanceModel.id == case_id))).scalar_one_or_none()
    if case is None:
        raise ValueError(f"Case {case_id} not found")

    reg, creds = await _get_connector(session, "salesforce", connector_id, tenant_id)
    from case_service.hxbridge.connectors.salesforce_connector import SalesforceConnector
    connector = SalesforceConnector(config=reg.config or {}, credentials=creds)

    sync_data = {"first_name": first_name, "last_name": last_name, "email": email, "subject": subject, "description": description}

    rec = CrmSyncRecordModel(
        tenant_id=tenant_id, case_id=case_id, step_id=step_id,
        connector_id=reg.id, provider="salesforce",
        status="pending", sync_data=sync_data,
    )
    session.add(rec)
    await session.flush()

    try:
        result = await connector.execute({"operation": "upsert_contact_and_case", **sync_data})
        rec.status        = "synced"
        rec.crm_object_type = "Contact+Case"
        rec.crm_record_id   = result.get("id")
        rec.crm_record_url  = result.get("url")
        rec.synced_at       = _utcnow()
        await session.flush()
        await _complete_step(session, case_id, step_id, "crm_sync", actor, {
            "crm_record_id": rec.crm_record_id, "provider": "salesforce",
        })
        await _auto_advance(session, case_id)
        await _emit(case_id, "crm_synced", {"record_id": str(rec.id), "crm_record_id": rec.crm_record_id})
    except Exception as exc:
        rec.status = "failed"
        rec.error  = str(exc)
        await session.flush()
        raise

    return rec


async def list_crm_records(session: AsyncSession, case_id: uuid.UUID) -> list[CrmSyncRecordModel]:
    return list((await session.execute(
        select(CrmSyncRecordModel).where(CrmSyncRecordModel.case_id == case_id)
        .order_by(CrmSyncRecordModel.created_at.desc())
    )).scalars().all())


# ── Invoice ───────────────────────────────────────────────────────────────────

async def generate_invoice(
    session: AsyncSession,
    case_id: uuid.UUID,
    step_id: str,
    tenant_id: str,
    actor: str,
    contact_name: str,
    description: str,
    amount_cents: int,
    currency: str = "usd",
    line_items: list | None = None,
    reference: str = "",
    connector_id: uuid.UUID | None = None,
) -> InvoiceRecordModel:
    case = (await session.execute(select(CaseInstanceModel).where(CaseInstanceModel.id == case_id))).scalar_one_or_none()
    if case is None:
        raise ValueError(f"Case {case_id} not found")

    reg, creds = await _get_connector(session, "xero", connector_id, tenant_id)
    from case_service.hxbridge.connectors.xero_connector import XeroConnector
    connector = XeroConnector(config=reg.config or {}, credentials=creds)

    inv_rec = InvoiceRecordModel(
        tenant_id=tenant_id, case_id=case_id, step_id=step_id,
        connector_id=reg.id, provider="xero",
        contact_name=contact_name, amount_cents=amount_cents,
        currency=currency, line_items=line_items or [],
        status="pending",
    )
    session.add(inv_rec)
    await session.flush()

    try:
        result = await connector.execute({
            "operation":    "create_invoice",
            "contact_name": contact_name,
            "description":  description,
            "amount_cents": amount_cents,
            "currency":     currency,
            "line_items":   line_items or [],
            "reference":    reference,
        })
        inv_rec.invoice_id     = result.get("invoice_id")
        inv_rec.invoice_number = result.get("invoice_number")
        inv_rec.invoice_url    = result.get("invoice_url")
        inv_rec.amount_cents   = result.get("amount_cents", amount_cents)
        inv_rec.status         = "draft"
        inv_rec.issued_at      = _utcnow()
        await session.flush()
        await _complete_step(session, case_id, step_id, "invoice_generate", actor, {
            "invoice_id": inv_rec.invoice_id, "provider": "xero",
        })
        await _auto_advance(session, case_id)
        await _emit(case_id, "invoice_generated", {"record_id": str(inv_rec.id), "invoice_id": inv_rec.invoice_id})
    except Exception as exc:
        inv_rec.status = "failed" if not inv_rec.invoice_id else "draft"
        inv_rec.status = "failed"
        await session.flush()
        raise

    return inv_rec


async def list_invoices(session: AsyncSession, case_id: uuid.UUID) -> list[InvoiceRecordModel]:
    return list((await session.execute(
        select(InvoiceRecordModel).where(InvoiceRecordModel.case_id == case_id)
        .order_by(InvoiceRecordModel.created_at.desc())
    )).scalars().all())
