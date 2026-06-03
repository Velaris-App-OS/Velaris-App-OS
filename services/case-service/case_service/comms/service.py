"""P51 Communications service — Twilio SMS + Slack outbound."""
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
    SlackNotificationModel,
    SmsMessageModel,
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
            case = (await session.execute(
                select(CaseInstanceModel).where(CaseInstanceModel.id == case_id)
            )).scalar_one_or_none()
            if case and case.case_type_id:
                ct = (await session.execute(
                    select(CaseTypeModel).where(CaseTypeModel.id == case.case_type_id)
                )).scalar_one_or_none()
                if ct:
                    from case_service.api.routers.cases import _auto_advance_if_complete
                    await _auto_advance_if_complete(session, case, ct.definition_json or {})
    except Exception as exc:
        logger.warning("Auto-advance failed (non-fatal): %s", exc)


# ── SMS ───────────────────────────────────────────────────────────────────────

async def send_sms(
    session: AsyncSession,
    case_id: uuid.UUID,
    step_id: str,
    to_number: str,
    body: str,
    tenant_id: str,
    actor: str,
    connector_id: uuid.UUID | None = None,
    from_number: str | None = None,
) -> SmsMessageModel:
    case = (await session.execute(select(CaseInstanceModel).where(CaseInstanceModel.id == case_id))).scalar_one_or_none()
    if case is None:
        raise ValueError(f"Case {case_id} not found")

    try:
        conn_row, creds = await _get_connector(session, "twilio", connector_id, tenant_id)
    except ValueError:
        raise

    row = SmsMessageModel(
        tenant_id=tenant_id, case_id=case_id, step_id=step_id,
        connector_id=conn_row.id, provider="twilio",
        to_number=to_number, body=body, status="pending",
    )
    session.add(row)
    await session.flush()

    try:
        from case_service.hxbridge.connectors.twilio_connector import TwilioConnector
        connector = TwilioConnector(conn_row.config or {}, creds)
        result    = await connector.execute({
            "to_number": to_number,
            "body":      body,
            "from_number": from_number,
        })
        row.message_sid = result.get("message_sid")
        row.from_number = result.get("from_number") or from_number
        row.status      = result.get("status", "sent")
        row.sent_at     = _utcnow()
    except Exception as exc:
        row.status = "failed"
        row.error  = str(exc)[:500]

    await session.flush()

    if row.status not in ("failed", "pending"):
        await _complete_step(session, case_id, step_id, "sms_send", actor, {"message_sid": row.message_sid})
        await _auto_advance(session, case_id)

    await _emit(case_id, "sms.sent", {"status": row.status, "to": to_number})
    return row


async def list_sms(session: AsyncSession, case_id: uuid.UUID) -> list[SmsMessageModel]:
    rows = (await session.execute(
        select(SmsMessageModel).where(SmsMessageModel.case_id == case_id).order_by(SmsMessageModel.created_at)
    )).scalars().all()
    return list(rows)


# ── Slack ─────────────────────────────────────────────────────────────────────

async def send_slack(
    session: AsyncSession,
    case_id: uuid.UUID,
    step_id: str,
    message: str,
    tenant_id: str,
    actor: str,
    connector_id: uuid.UUID | None = None,
    channel: str | None = None,
    blocks: list | None = None,
) -> SlackNotificationModel:
    case = (await session.execute(select(CaseInstanceModel).where(CaseInstanceModel.id == case_id))).scalar_one_or_none()
    if case is None:
        raise ValueError(f"Case {case_id} not found")

    try:
        conn_row, creds = await _get_connector(session, "slack", connector_id, tenant_id)
    except ValueError:
        raise

    row = SlackNotificationModel(
        tenant_id=tenant_id, case_id=case_id, step_id=step_id,
        connector_id=conn_row.id, channel=channel,
        message=message, blocks=blocks or [], status="pending",
    )
    session.add(row)
    await session.flush()

    try:
        from case_service.hxbridge.connectors.slack_connector import SlackConnector
        connector = SlackConnector(conn_row.config or {}, creds)
        result    = await connector.execute({
            "message": message,
            "channel": channel,
            "blocks":  blocks or [],
        })
        row.status  = result.get("status", "sent")
        row.sent_at = _utcnow()
    except Exception as exc:
        row.status = "failed"
        row.error  = str(exc)[:500]

    await session.flush()

    if row.status == "sent":
        await _complete_step(session, case_id, step_id, "slack_notify", actor, {"channel": channel})
        await _auto_advance(session, case_id)

    await _emit(case_id, "slack.sent", {"status": row.status, "channel": channel})
    return row


async def list_slack(session: AsyncSession, case_id: uuid.UUID) -> list[SlackNotificationModel]:
    rows = (await session.execute(
        select(SlackNotificationModel).where(SlackNotificationModel.case_id == case_id).order_by(SlackNotificationModel.created_at)
    )).scalars().all()
    return list(rows)
