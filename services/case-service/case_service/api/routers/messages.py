"""Case Messages — Portal v2 P4: human thread between workers and the
case's portal customer.

Worker side (this router): ``messages.read`` / ``messages.write`` via the
case-level PDP, 404 anti-oracle like every case sub-route. The customer side
lives in portal.py under the customer JWT. A worker post that is
portal-visible triggers a best-effort email notification to the linked
customer (honouring their notify_email preference).

Copyright (c) 2024-2026 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service import hxguard
from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser
from case_service.db import repository as repo
from case_service.db.models import (
    CaseMessageModel,
    PortalCustomerCaseLinkModel,
    PortalCustomerModel,
)
from case_service.db.session import get_session

log = logging.getLogger(__name__)

router = APIRouter(prefix="/cases", tags=["case-messages"])


def _tenant(user: AuthenticatedUser) -> str:
    return user.tenant_id or "default"


async def _authorized_case(session, user, case_id: uuid.UUID, action: str):
    case = await repo.get_case_instance(session, case_id)
    if case is None or (case.tenant_id is not None and str(case.tenant_id) != _tenant(user)):
        raise HTTPException(404, "Case not found")
    await hxguard.require_case(session, user, action, case_id)
    return case


def _view(m: CaseMessageModel) -> dict:
    return {
        "id":             str(m.id),
        "author":         m.author,
        "author_name":    m.author_name,
        "body":           m.body,
        "portal_visible": m.portal_visible,
        "created_at":     m.created_at.isoformat() if m.created_at else None,
    }


async def notify_customer_of_message(session: AsyncSession, case_id: uuid.UUID, body: str) -> None:
    """Best-effort email to the linked customer (preferred address), gated on
    their notify_email preference. Failures are logged, never raised."""
    try:
        customer = (await session.execute(
            select(PortalCustomerModel)
            .join(PortalCustomerCaseLinkModel,
                  PortalCustomerCaseLinkModel.customer_id == PortalCustomerModel.id)
            .where(PortalCustomerCaseLinkModel.case_id == case_id)
        )).scalars().first()
        if customer is None or not customer.notify_email:
            return
        to = customer.alt_email if (customer.preferred_email == "alt" and customer.alt_email) \
            else customer.primary_email

        from case_service.db.models import EmailAccountModel
        from case_service.mail import EmailService
        account = (await session.execute(
            select(EmailAccountModel).where(
                EmailAccountModel.is_default_outbound.is_(True),
                EmailAccountModel.is_active.is_(True),
            ).limit(1)
        )).scalar_one_or_none()
        if account is None:
            return
        preview = body if len(body) <= 400 else body[:400] + "…"
        await EmailService().send(
            session, case_id=None, account=account, to_addresses=[to],
            subject="New message on your request",
            body_text=(
                "Our team sent you a new message:\n\n"
                f"{preview}\n\n"
                "Log in to the portal to reply.\n"
                "To stop these notifications, turn off email updates in My Account."
            ),
        )
    except Exception as exc:
        log.warning("case message notification failed for case %s: %s", case_id, exc)


class PostMessageBody(BaseModel):
    body: str = Field(..., min_length=1, max_length=10_000)
    portal_visible: bool = True


@router.get("/{case_id}/messages")
async def list_messages(
    case_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    await _authorized_case(session, user, case_id, "messages.read")
    rows = (await session.execute(
        select(CaseMessageModel)
        .where(CaseMessageModel.case_id == case_id)
        .order_by(CaseMessageModel.created_at)
    )).scalars().all()
    return {"messages": [_view(m) for m in rows]}


@router.post("/{case_id}/messages", status_code=201)
async def post_message(
    case_id: uuid.UUID,
    body: PostMessageBody,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    await _authorized_case(session, user, case_id, "messages.write")
    msg = CaseMessageModel(
        case_id=case_id,
        author=f"user:{user.user_id}",
        author_name=user.username or user.email or user.user_id,
        body=body.body.strip(),
        portal_visible=body.portal_visible,
    )
    session.add(msg)
    await session.commit()
    await session.refresh(msg)
    if msg.portal_visible:
        await notify_customer_of_message(session, case_id, msg.body)
    return _view(msg)
