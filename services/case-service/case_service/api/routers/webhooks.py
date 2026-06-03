"""Webhook management API.

CRUD for webhook subscriptions + delivery history.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, func, update, delete
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import WebhookSubscriptionModel, WebhookDeliveryModel
from case_service.auth.dependencies import get_current_user
from case_service.db.session import get_session
from case_service.integrations.webhook_dispatcher import CASE_EVENTS

router = APIRouter(prefix="/webhooks", tags=["webhooks"], dependencies=[Depends(get_current_user)])


# ─── Schemas ──────────────────────────────────────────────────────

class WebhookCreate(BaseModel):
    name: str
    url: str
    secret: str | None = None
    events: list[str] = Field(default_factory=lambda: ["*"])
    case_type_id: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    retry_count: int = 3
    timeout_seconds: int = 10


class WebhookUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    secret: str | None = None
    events: list[str] | None = None
    case_type_id: str | None = None
    is_active: bool | None = None
    headers: dict[str, str] | None = None
    retry_count: int | None = None
    timeout_seconds: int | None = None


class WebhookResponse(BaseModel):
    id: str
    name: str
    url: str
    events: list[str]
    case_type_id: str | None
    is_active: bool
    headers: dict[str, str]
    retry_count: int
    timeout_seconds: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class WebhookDeliveryResponse(BaseModel):
    id: str
    subscription_id: str
    event_type: str
    payload: dict[str, Any]
    response_status: int | None
    attempt: int
    status: str
    error_message: str | None
    delivered_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class WebhookTestResponse(BaseModel):
    success: bool
    status_code: int | None = None
    error: str | None = None


# ─── Endpoints ────────────────────────────────────────────────────

@router.get("/events")
async def list_event_types():
    """List all supported webhook event types."""
    return {"events": CASE_EVENTS}


@router.post("", response_model=WebhookResponse, status_code=201)
async def create_webhook(
    body: WebhookCreate,
    session: AsyncSession = Depends(get_session),
):
    # Validate events
    for evt in body.events:
        if evt != "*" and evt not in CASE_EVENTS:
            raise HTTPException(400, f"Unknown event type: {evt}")

    model = WebhookSubscriptionModel(
        name=body.name,
        url=body.url,
        secret=body.secret,
        events=body.events,
        case_type_id=uuid.UUID(body.case_type_id) if body.case_type_id else None,
        headers=body.headers,
        retry_count=body.retry_count,
        timeout_seconds=body.timeout_seconds,
    )
    session.add(model)
    await session.flush()

    return _to_response(model)


@router.get("", response_model=list[WebhookResponse])
async def list_webhooks(
    session: AsyncSession = Depends(get_session),
):
    stmt = select(WebhookSubscriptionModel).order_by(WebhookSubscriptionModel.name)
    result = await session.execute(stmt)
    return [_to_response(m) for m in result.scalars().all()]


@router.get("/{webhook_id}", response_model=WebhookResponse)
async def get_webhook(
    webhook_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    model = await _get_or_404(session, webhook_id)
    return _to_response(model)


@router.patch("/{webhook_id}", response_model=WebhookResponse)
async def update_webhook(
    webhook_id: uuid.UUID,
    body: WebhookUpdate,
    session: AsyncSession = Depends(get_session),
):
    await _get_or_404(session, webhook_id)
    values = {k: v for k, v in body.model_dump(exclude_unset=True).items()}
    if "case_type_id" in values and values["case_type_id"]:
        values["case_type_id"] = uuid.UUID(values["case_type_id"])
    if values:
        stmt = update(WebhookSubscriptionModel).where(
            WebhookSubscriptionModel.id == webhook_id
        ).values(**values)
        await session.execute(stmt)
    stmt2 = select(WebhookSubscriptionModel).where(WebhookSubscriptionModel.id == webhook_id)
    result = await session.execute(stmt2)
    return _to_response(result.scalar_one())


@router.delete("/{webhook_id}", status_code=204)
async def delete_webhook(
    webhook_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    await _get_or_404(session, webhook_id)
    stmt = delete(WebhookSubscriptionModel).where(WebhookSubscriptionModel.id == webhook_id)
    await session.execute(stmt)


@router.get("/{webhook_id}/deliveries", response_model=list[WebhookDeliveryResponse])
async def list_deliveries(
    webhook_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
):
    await _get_or_404(session, webhook_id)
    stmt = (
        select(WebhookDeliveryModel)
        .where(WebhookDeliveryModel.subscription_id == webhook_id)
        .order_by(WebhookDeliveryModel.created_at.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return [_delivery_response(m) for m in result.scalars().all()]


@router.post("/{webhook_id}/test", response_model=WebhookTestResponse)
async def test_webhook(
    webhook_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    """Send a test ping to the webhook URL."""
    model = await _get_or_404(session, webhook_id)
    import json
    try:
        import httpx
        payload = json.dumps({
            "event": "webhook.test",
            "timestamp": datetime.now().isoformat(),
            "webhook_id": str(model.id),
        })
        headers = {"Content-Type": "application/json", **(model.headers or {})}
        if model.secret:
            from case_service.integrations.webhook_dispatcher import compute_signature
            headers["X-Helix-Signature"] = compute_signature(payload, model.secret)

        async with httpx.AsyncClient(timeout=model.timeout_seconds) as client:
            resp = await client.post(model.url, content=payload, headers=headers)
            return WebhookTestResponse(
                success=resp.status_code < 400,
                status_code=resp.status_code,
            )
    except Exception as e:
        return WebhookTestResponse(success=False, error=str(e)[:500])


# ─── Helpers ──────────────────────────────────────────────────────

async def _get_or_404(session, webhook_id):
    stmt = select(WebhookSubscriptionModel).where(WebhookSubscriptionModel.id == webhook_id)
    result = await session.execute(stmt)
    model = result.scalar_one_or_none()
    if model is None:
        raise HTTPException(404, "Webhook not found")
    return model


def _to_response(model) -> dict:
    return {
        "id": str(model.id),
        "name": model.name,
        "url": model.url,
        "events": model.events or [],
        "case_type_id": str(model.case_type_id) if model.case_type_id else None,
        "is_active": model.is_active,
        "headers": model.headers or {},
        "retry_count": model.retry_count,
        "timeout_seconds": model.timeout_seconds,
        "created_at": model.created_at,
        "updated_at": model.updated_at,
    }


def _delivery_response(model) -> dict:
    return {
        "id": str(model.id),
        "subscription_id": str(model.subscription_id),
        "event_type": model.event_type,
        "payload": model.payload,
        "response_status": model.response_status,
        "attempt": model.attempt,
        "status": model.status,
        "error_message": model.error_message,
        "delivered_at": model.delivered_at,
        "created_at": model.created_at,
    }
