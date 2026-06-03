"""P49 KYC & E-Sign service."""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import (
    CaseInstanceModel,
    CaseStepCompletionModel,
    ConnectorRegistryModel,
    ESignRequestModel,
    IdentityVerificationModel,
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
    """Complete a case step inside a savepoint."""
    from sqlalchemy import select as _sel
    now = _utcnow()
    try:
        async with session.begin_nested():
            case = (await session.execute(_sel(CaseInstanceModel).where(CaseInstanceModel.id == case_id))).scalar_one_or_none()
            stage_id = (case.current_stage_id if case else None) or "unknown"
            existing = (await session.execute(
                _sel(CaseStepCompletionModel).where(
                    CaseStepCompletionModel.case_id == case_id,
                    CaseStepCompletionModel.step_id == step_id,
                )
            )).scalar_one_or_none()
            if existing:
                existing.status = "completed"
                existing.completed_at = now
                existing.data = data
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


# ── Identity verification ─────────────────────────────────────────────────────

async def create_identity_verification(
    session: AsyncSession, case_id: uuid.UUID, step_id: str,
    tenant_id: str, first_name: str, last_name: str,
    connector_id: uuid.UUID | None = None,
) -> IdentityVerificationModel:
    case = (await session.execute(select(CaseInstanceModel).where(CaseInstanceModel.id == case_id))).scalar_one_or_none()
    if case is None:
        raise ValueError(f"Case {case_id} not found")

    reg, creds = await _get_connector(session, "onfido", connector_id, tenant_id)
    from case_service.hxbridge.connectors.onfido_connector import OnfidoConnector
    connector = OnfidoConnector(config=reg.config or {}, credentials=creds)

    result = await connector.execute({
        "operation":   "create_applicant_and_token",
        "first_name":  first_name,
        "last_name":   last_name,
    })

    iv = IdentityVerificationModel(
        tenant_id        = tenant_id,
        case_id          = case_id,
        step_id          = step_id,
        connector_id     = reg.id,
        provider         = "onfido",
        check_id         = result.get("check_id"),
        applicant_id     = result.get("applicant_id"),
        sdk_token        = result.get("sdk_token"),
        verification_url = result.get("verification_url"),
        status           = "pending",
    )
    session.add(iv)
    await session.flush()

    await _emit(case_id, "kyc_initiated", {
        "verification_id": str(iv.id),
        "step_id":         step_id,
        "provider":        "onfido",
        "verification_url": iv.verification_url,
    })
    return iv


async def handle_onfido_webhook(session: AsyncSession, payload: bytes, sig_header: str, tenant_id: str | None = None) -> dict:
    body        = json.loads(payload)
    payload_obj = body.get("payload", {})
    resource    = payload_obj.get("resource_type", "")
    obj         = payload_obj.get("object", {})
    action      = payload_obj.get("action", "")
    check_id    = obj.get("id") if resource == "check" else None

    # Verify HMAC if check_id found
    iv: IdentityVerificationModel | None = None
    if check_id:
        iv = (await session.execute(
            select(IdentityVerificationModel).where(IdentityVerificationModel.check_id == check_id)
        )).scalar_one_or_none()

    verified = False
    if iv and iv.connector_id and sig_header:
        reg = (await session.execute(select(ConnectorRegistryModel).where(ConnectorRegistryModel.id == iv.connector_id))).scalar_one_or_none()
        if reg:
            from case_service.hxbridge.connectors.onfido_connector import OnfidoConnector
            creds = decrypt_credentials(reg.credentials)
            verified = OnfidoConnector(config=reg.config or {}, credentials=creds).verify_webhook(payload, sig_header)
    elif not sig_header:
        verified = True  # dev/test

    if not verified and sig_header:
        return {"status": "rejected", "reason": "invalid_signature"}

    if iv and action == "check.completed":
        result = obj.get("result")
        iv.status      = "complete"
        iv.result      = result
        iv.result_hash = hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()
        iv.completed_at = _utcnow()
        await session.flush()

        if result == "clear":
            await _complete_step(session, iv.case_id, iv.step_id, "identity_verify",
                                 "onfido_webhook", {"verification_id": str(iv.id), "result": result})
            await _auto_advance(session, iv.case_id)

        await _emit(iv.case_id, "kyc_completed", {
            "verification_id": str(iv.id),
            "result":          result,
            "step_id":         iv.step_id,
        })

    await session.commit()
    return {"status": "ok", "action": action, "processed": True}


async def list_verifications(session: AsyncSession, case_id: uuid.UUID) -> list[IdentityVerificationModel]:
    return list((await session.execute(
        select(IdentityVerificationModel).where(IdentityVerificationModel.case_id == case_id)
        .order_by(IdentityVerificationModel.created_at.desc())
    )).scalars().all())


# ── E-sign ────────────────────────────────────────────────────────────────────

async def create_esign_request(
    session: AsyncSession, case_id: uuid.UUID, step_id: str,
    tenant_id: str, signer_email: str, signer_name: str,
    document_name: str, document_base64: str = "",
    connector_id: uuid.UUID | None = None,
) -> ESignRequestModel:
    case = (await session.execute(select(CaseInstanceModel).where(CaseInstanceModel.id == case_id))).scalar_one_or_none()
    if case is None:
        raise ValueError(f"Case {case_id} not found")

    reg, creds = await _get_connector(session, "docusign", connector_id, tenant_id)
    from case_service.hxbridge.connectors.docusign_connector import DocuSignConnector
    connector = DocuSignConnector(config=reg.config or {}, credentials=creds)

    result = await connector.execute({
        "operation":       "create_envelope",
        "signer_email":    signer_email,
        "signer_name":     signer_name,
        "document_name":   document_name,
        "document_base64": document_base64,
    })

    es = ESignRequestModel(
        tenant_id     = tenant_id,
        case_id       = case_id,
        step_id       = step_id,
        connector_id  = reg.id,
        provider      = "docusign",
        envelope_id   = result.get("envelope_id"),
        signing_url   = result.get("signing_url"),
        document_name = document_name,
        signer_email  = signer_email,
        signer_name   = signer_name,
        status        = "sent",
    )
    session.add(es)
    await session.flush()

    await _emit(case_id, "esign_sent", {
        "esign_id":   str(es.id),
        "step_id":    step_id,
        "signing_url": es.signing_url,
    })
    return es


async def handle_docusign_webhook(session: AsyncSession, payload: bytes, sig_header: str) -> dict:
    body        = json.loads(payload)
    event       = body.get("event", "")
    envelope_id = body.get("data", {}).get("envelopeId")

    es: ESignRequestModel | None = None
    if envelope_id:
        es = (await session.execute(
            select(ESignRequestModel).where(ESignRequestModel.envelope_id == envelope_id)
        )).scalar_one_or_none()

    verified = False
    if es and es.connector_id and sig_header:
        reg = (await session.execute(select(ConnectorRegistryModel).where(ConnectorRegistryModel.id == es.connector_id))).scalar_one_or_none()
        if reg:
            from case_service.hxbridge.connectors.docusign_connector import DocuSignConnector
            creds = decrypt_credentials(reg.credentials)
            verified = DocuSignConnector(config=reg.config or {}, credentials=creds).verify_webhook(payload, sig_header)
    elif not sig_header:
        verified = True

    if not verified and sig_header:
        return {"status": "rejected", "reason": "invalid_signature"}

    if es and event == "envelope-completed":
        es.status    = "completed"
        es.signed_at = _utcnow()
        await session.flush()
        await _complete_step(session, es.case_id, es.step_id, "esign_request",
                             "docusign_webhook", {"esign_id": str(es.id), "envelope_id": envelope_id})
        await _auto_advance(session, es.case_id)
        await _emit(es.case_id, "esign_completed", {"esign_id": str(es.id), "step_id": es.step_id})

    elif es and event in ("envelope-declined", "envelope-voided"):
        es.status = "declined" if event == "envelope-declined" else "voided"
        await _emit(es.case_id, "esign_failed", {"esign_id": str(es.id), "event": event})

    await session.commit()
    return {"status": "ok", "event": event}


async def list_esign_requests(session: AsyncSession, case_id: uuid.UUID) -> list[ESignRequestModel]:
    return list((await session.execute(
        select(ESignRequestModel).where(ESignRequestModel.case_id == case_id)
        .order_by(ESignRequestModel.created_at.desc())
    )).scalars().all())
