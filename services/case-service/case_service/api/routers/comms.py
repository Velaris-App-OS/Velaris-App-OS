"""P51 Communications router — SMS + Slack."""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser
from case_service.db.session import get_session
from case_service.comms import service

router = APIRouter(prefix="/comms", tags=["comms"])


def _tenant(user: AuthenticatedUser) -> str:
    return getattr(user, "tenant_id", None) or "default"


def _actor(user: AuthenticatedUser) -> str:
    return (getattr(user, "username", None) or getattr(user, "email", None) or getattr(user, "user_id", None) or "system")


# ── SMS ───────────────────────────────────────────────────────────────────────

class SmsSendRequest(BaseModel):
    step_id:      str
    to_number:    str
    body:         str
    from_number:  str | None = None
    connector_id: uuid.UUID | None = None


class SmsOut(BaseModel):
    id:          uuid.UUID
    status:      str
    to_number:   str
    from_number: str | None
    message_sid: str | None
    error:       str | None

    model_config = {"from_attributes": True}


@router.get("/sms/cases/{case_id}/messages", response_model=list[SmsOut])
async def list_sms(
    case_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    return await service.list_sms(session, case_id)


@router.post("/sms/cases/{case_id}/send", response_model=SmsOut)
async def send_sms(
    case_id: uuid.UUID,
    body:    SmsSendRequest,
    session: AsyncSession = Depends(get_session),
    user:    AuthenticatedUser = Depends(get_current_user),
):
    try:
        row = await service.send_sms(
            session, case_id, body.step_id,
            body.to_number, body.body,
            _tenant(user), _actor(user),
            connector_id=body.connector_id,
            from_number=body.from_number,
        )
        await session.commit()
        return row
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, str(exc))


# ── Slack ─────────────────────────────────────────────────────────────────────

class SlackSendRequest(BaseModel):
    step_id:      str
    message:      str
    channel:      str | None = None
    blocks:       list[Any] = []
    connector_id: uuid.UUID | None = None


class SlackOut(BaseModel):
    id:       uuid.UUID
    status:   str
    channel:  str | None
    message:  str
    slack_ts: str | None
    error:    str | None

    model_config = {"from_attributes": True}


@router.get("/slack/cases/{case_id}/notifications", response_model=list[SlackOut])
async def list_slack(
    case_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    return await service.list_slack(session, case_id)


@router.post("/slack/cases/{case_id}/send", response_model=SlackOut)
async def send_slack(
    case_id: uuid.UUID,
    body:    SlackSendRequest,
    session: AsyncSession = Depends(get_session),
    user:    AuthenticatedUser = Depends(get_current_user),
):
    try:
        row = await service.send_slack(
            session, case_id, body.step_id,
            body.message, _tenant(user), _actor(user),
            connector_id=body.connector_id,
            channel=body.channel,
            blocks=body.blocks,
        )
        await session.commit()
        return row
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, str(exc))


# ── Connectors list ───────────────────────────────────────────────────────────

@router.get("/connectors")
async def list_comms_connectors(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    from sqlalchemy import select
    from case_service.db.models import ConnectorRegistryModel
    rows = (await session.execute(
        select(ConnectorRegistryModel).where(
            ConnectorRegistryModel.connector_type.in_(["twilio", "slack"]),
            ConnectorRegistryModel.tenant_id == _tenant(user),
        )
    )).scalars().all()
    return [{"id": str(r.id), "name": r.name, "type": r.connector_type, "enabled": r.enabled} for r in rows]


@router.get("/sms/messages", response_model=list[SmsOut])
async def list_all_sms(
    status: str | None = None,
    limit: int = 100,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    from sqlalchemy import select
    from case_service.db.models import SmsMessageModel
    q = select(SmsMessageModel).order_by(SmsMessageModel.created_at.desc()).limit(limit)
    if status:
        q = q.where(SmsMessageModel.status == status)
    rows = (await session.execute(q)).scalars().all()
    return rows


@router.get("/slack/notifications", response_model=list[SlackOut])
async def list_all_slack(
    status: str | None = None,
    limit: int = 100,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    from sqlalchemy import select
    from case_service.db.models import SlackNotificationModel
    q = select(SlackNotificationModel).order_by(SlackNotificationModel.created_at.desc()).limit(limit)
    if status:
        q = q.where(SlackNotificationModel.status == status)
    rows = (await session.execute(q)).scalars().all()
    return rows
