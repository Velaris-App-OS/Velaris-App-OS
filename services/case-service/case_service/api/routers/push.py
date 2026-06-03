"""Push notification API — P27.

Endpoints:
  POST   /push/devices              register a device token
  DELETE /push/devices/{id}         deregister a device
  GET    /push/devices              list own devices (token masked)
  GET    /push/admin/devices        list all devices (admin)
  GET    /push/admin/logs           delivery log (admin)
  GET    /push/preferences          get own preferences
  PUT    /push/preferences/{event}  upsert preference
  GET    /push/case-types/{id}/overrides       get overrides
  PUT    /push/case-types/{id}/overrides/{ev}  upsert override (admin)
  GET    /push/vapid-public-key     VAPID public key for browser subscription
  POST   /push/test-send            diagnostic test send (admin)
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.auth.dependencies import get_current_user, require_role
from case_service.auth.models import AuthenticatedUser
from case_service.db.models import (
    DeviceTokenModel,
    NotificationPreferenceModel,
    CaseTypeNotificationOverrideModel,
    NotificationLogModel,
)
from case_service.db.session import get_session
from case_service.push.service import get_vapid_public_key, send_to_user
from case_service.push.protocol import PushPayload

router = APIRouter(prefix="/push", tags=["push"])

_VALID_CHANNELS = {"fcm", "apns", "webpush"}


# ─── Pydantic models ────────────────────────────────────────────────

class DeviceIn(BaseModel):
    channel: str = Field(..., pattern=r"^(fcm|apns|webpush)$")
    token: str
    platform: Optional[str] = Field(None, pattern=r"^(android|ios|web)$")
    label: Optional[str] = None
    tenant_id: Optional[str] = None


class DeviceOut(BaseModel):
    id: uuid.UUID
    user_id: str
    channel: str
    token_prefix: str       # first 8 chars
    platform: Optional[str]
    label: Optional[str]
    is_active: bool
    last_seen_at: Optional[datetime]
    created_at: datetime


class PreferenceIn(BaseModel):
    channels: list[str] = Field(default_factory=list)
    enabled: bool = True


class PreferenceOut(BaseModel):
    event_type: str
    channels: list[str]
    enabled: bool


class OverrideIn(BaseModel):
    channels: list[str] = Field(default_factory=list)
    enabled: bool = True


class TestSendIn(BaseModel):
    user_id: str
    event_type: str = "test"
    title: str = "HELIX Test"
    body: str = "Push notification test from HELIX."
    case_type_id: Optional[uuid.UUID] = None


def _mask(device: DeviceTokenModel) -> DeviceOut:
    return DeviceOut(
        id=device.id,
        user_id=device.user_id,
        channel=device.channel,
        token_prefix=device.token[:8],
        platform=device.platform,
        label=device.label,
        is_active=device.is_active,
        last_seen_at=device.last_seen_at,
        created_at=device.created_at,
    )


# ─── Device registration ────────────────────────────────────────────

@router.post("/devices", status_code=201)
async def register_device(
    body: DeviceIn,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    # Upsert by (user_id, channel, token[:64]) to avoid duplicates
    existing = await session.scalar(
        select(DeviceTokenModel).where(
            DeviceTokenModel.user_id == current_user.user_id,
            DeviceTokenModel.channel == body.channel,
            DeviceTokenModel.token == body.token,
        )
    )
    if existing:
        existing.is_active = True
        existing.last_seen_at = datetime.now(timezone.utc)
        if body.label:
            existing.label = body.label
        await session.commit()
        return _mask(existing)

    device = DeviceTokenModel(
        user_id=current_user.user_id,
        channel=body.channel,
        token=body.token,
        platform=body.platform,
        label=body.label,
        tenant_id=body.tenant_id or getattr(current_user, "tenant_id", None),
        last_seen_at=datetime.now(timezone.utc),
    )
    session.add(device)
    await session.commit()
    await session.refresh(device)
    return _mask(device)


@router.delete("/devices/{device_id}", status_code=204)
async def deregister_device(
    device_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    device = await session.get(DeviceTokenModel, device_id)
    if device is None or device.user_id != current_user.user_id:
        raise HTTPException(status_code=404, detail="Device not found")
    device.is_active = False
    await session.commit()


@router.get("/devices", response_model=list[DeviceOut])
async def list_my_devices(
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    rows = await session.execute(
        select(DeviceTokenModel).where(
            DeviceTokenModel.user_id == current_user.user_id,
            DeviceTokenModel.is_active == True,  # noqa: E712
        ).order_by(DeviceTokenModel.created_at.desc())
    )
    return [_mask(d) for d in rows.scalars()]


# ─── Admin: all devices + logs ───────────────────────────────────────

@router.get("/admin/devices")
async def admin_list_devices(
    channel: Optional[str] = Query(None),
    active_only: bool = Query(True),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    _: AuthenticatedUser = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_session),
):
    q = select(DeviceTokenModel)
    if channel:
        q = q.where(DeviceTokenModel.channel == channel)
    if active_only:
        q = q.where(DeviceTokenModel.is_active == True)  # noqa: E712
    total = await session.scalar(select(func.count()).select_from(q.subquery()))
    rows = await session.execute(
        q.order_by(DeviceTokenModel.created_at.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )
    return {
        "devices": [_mask(d) for d in rows.scalars()],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/admin/logs")
async def admin_delivery_logs(
    user_id: Optional[str] = Query(None),
    channel: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    _: AuthenticatedUser = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_session),
):
    q = select(NotificationLogModel)
    if user_id:
        q = q.where(NotificationLogModel.user_id == user_id)
    if channel:
        q = q.where(NotificationLogModel.channel == channel)
    if status:
        q = q.where(NotificationLogModel.status == status)
    total = await session.scalar(select(func.count()).select_from(q.subquery()))
    rows = await session.execute(
        q.order_by(NotificationLogModel.sent_at.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )
    items = [
        {
            "id": str(log.id),
            "device_id": str(log.device_id) if log.device_id else None,
            "user_id": log.user_id,
            "event_type": log.event_type,
            "channel": log.channel,
            "status": log.status,
            "error": log.error,
            "sent_at": log.sent_at.isoformat() if log.sent_at else None,
        }
        for log in rows.scalars()
    ]
    return {"logs": items, "total": total, "page": page, "page_size": page_size}


# ─── Preferences ─────────────────────────────────────────────────────

@router.get("/preferences", response_model=list[PreferenceOut])
async def get_preferences(
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    rows = await session.execute(
        select(NotificationPreferenceModel).where(
            NotificationPreferenceModel.user_id == current_user.user_id
        )
    )
    return [
        PreferenceOut(event_type=p.event_type, channels=p.channels or [], enabled=p.enabled)
        for p in rows.scalars()
    ]


@router.put("/preferences/{event_type}", response_model=PreferenceOut)
async def upsert_preference(
    event_type: str,
    body: PreferenceIn,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    pref = await session.scalar(
        select(NotificationPreferenceModel).where(
            NotificationPreferenceModel.user_id == current_user.user_id,
            NotificationPreferenceModel.event_type == event_type,
        )
    )
    if pref is None:
        pref = NotificationPreferenceModel(
            user_id=current_user.user_id, event_type=event_type,
        )
        session.add(pref)
    invalid = [c for c in body.channels if c not in _VALID_CHANNELS]
    if invalid:
        raise HTTPException(400, f"Unknown channels: {invalid}")
    pref.channels = body.channels
    pref.enabled = body.enabled
    await session.commit()
    return PreferenceOut(event_type=pref.event_type, channels=pref.channels or [], enabled=pref.enabled)


# ─── Case-type overrides (admin) ─────────────────────────────────────

@router.get("/case-types/{case_type_id}/overrides")
async def get_ct_overrides(
    case_type_id: uuid.UUID,
    _: AuthenticatedUser = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_session),
):
    rows = await session.execute(
        select(CaseTypeNotificationOverrideModel).where(
            CaseTypeNotificationOverrideModel.case_type_id == case_type_id
        )
    )
    return [
        {"event_type": o.event_type, "channels": o.channels or [], "enabled": o.enabled}
        for o in rows.scalars()
    ]


@router.put("/case-types/{case_type_id}/overrides/{event_type}")
async def upsert_ct_override(
    case_type_id: uuid.UUID,
    event_type: str,
    body: OverrideIn,
    _: AuthenticatedUser = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_session),
):
    override = await session.scalar(
        select(CaseTypeNotificationOverrideModel).where(
            CaseTypeNotificationOverrideModel.case_type_id == case_type_id,
            CaseTypeNotificationOverrideModel.event_type == event_type,
        )
    )
    if override is None:
        override = CaseTypeNotificationOverrideModel(
            case_type_id=case_type_id, event_type=event_type,
        )
        session.add(override)
    invalid = [c for c in body.channels if c not in _VALID_CHANNELS]
    if invalid:
        raise HTTPException(400, f"Unknown channels: {invalid}")
    override.channels = body.channels
    override.enabled = body.enabled
    await session.commit()
    return {"event_type": override.event_type, "channels": override.channels, "enabled": override.enabled}


# ─── VAPID public key (browser needs this) ───────────────────────────

@router.get("/vapid-public-key")
async def vapid_public_key(
    _: AuthenticatedUser = Depends(get_current_user),
):
    key = get_vapid_public_key()
    if not key:
        raise HTTPException(503, "Web Push not configured (VAPID_PUBLIC_KEY not set)")
    return {"vapid_public_key": key}


# ─── Admin test send ─────────────────────────────────────────────────

@router.post("/test-send")
async def test_send(
    body: TestSendIn,
    _: AuthenticatedUser = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_session),
):
    payload = PushPayload(title=body.title, body=body.body,
                          data={"event_type": body.event_type})
    results = await send_to_user(
        session, body.user_id, body.event_type, payload, body.case_type_id
    )
    return {
        "results": [
            {
                "channel": r.channel,
                "token_prefix": r.token_prefix,
                "success": r.success,
                "error": r.error,
            }
            for r in results
        ]
    }
