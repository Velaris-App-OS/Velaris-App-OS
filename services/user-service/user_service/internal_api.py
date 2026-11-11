"""user-service's internal API — `/internal/v1/demo/*` (2026-11-04).

Called only by the website's registration service, straight to user-service on
host loopback (never through the gateway, which refuses `/internal`), with its
own demo provisioning secret (`velaris_shared.internal.auth.require_demo_caller`)
— never the internal service secret. It replaces
the registration service's admin login, which made every sign-up `admin` of the
default tenant — a platform operator. See `velaris_shared.tenancy.demo`.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.session import get_session
from velaris_shared.internal.auth import require_demo_caller
from velaris_shared.tenancy import demo

router = APIRouter(prefix="/internal/v1/demo", tags=["internal"],
                   dependencies=[Depends(require_demo_caller)],
                   include_in_schema=False)


class DemoTenantIn(BaseModel):
    request_key: str = Field(min_length=8, max_length=200)
    username: str = Field(min_length=3, max_length=100, pattern=r"^[A-Za-z0-9._-]+$")
    email: str = Field(min_length=3, max_length=255, pattern=r"^[^@\s]+@[^@\s]+$")
    password: str = Field(min_length=12, max_length=200)
    display_name: str | None = Field(default=None, max_length=200)
    # trial (2026-11-10): a website build's own app, deleted at expiry
    kind: str = Field(default="free", pattern="^(free|work|trial)$")


class MoveIn(BaseModel):
    request_key: str = Field(min_length=8, max_length=200)
    username: str = Field(min_length=1, max_length=255)
    kind: str = Field(default="free", pattern="^(free|work)$")


class ExtendIn(BaseModel):
    days: int = Field(ge=1, le=90)


@router.post("/tenants", status_code=201)
async def create_demo_tenant(body: DemoTenantIn, session: AsyncSession = Depends(get_session)):
    try:
        out = await demo.provision(
            session, request_key=body.request_key, username=body.username,
            email=body.email, password=body.password, display_name=body.display_name,
            kind=body.kind)
    except demo.DemoError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    await session.commit()
    return out


@router.post("/move")
async def move_demo_account(body: MoveIn, session: AsyncSession = Depends(get_session)):
    """An account the website made before 2026-11-04 (admin of the default
    tenant) moves into a demo tenant of its own."""
    try:
        out = await demo.move_existing(session, request_key=body.request_key,
                                       username=body.username, kind=body.kind)
    except demo.DemoError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    await session.commit()
    return out


@router.post("/tenants/{tenant_id}/extend")
async def extend_demo_tenant(tenant_id: str, body: ExtendIn,
                             session: AsyncSession = Depends(get_session)):
    try:
        out = await demo.extend(session, tenant_id, body.days)
    except demo.DemoError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    await session.commit()
    return out
