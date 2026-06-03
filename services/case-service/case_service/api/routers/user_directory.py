"""User directory CRUD + bulk sync."""
from __future__ import annotations
import uuid
from datetime import datetime, timezone
from typing import Optional

import json
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, cast, literal
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.auth.dependencies import get_current_user, require_role
from case_service.auth.models import AuthenticatedUser
from case_service.db.models import UserDirectoryModel, HelixUserModel
from case_service.db.session import get_session

router = APIRouter(prefix="/user-directory", tags=["user-directory"])


class UserDirectoryEntry(BaseModel):
    id: Optional[uuid.UUID] = None
    user_id: str
    email: Optional[str] = None
    display_name: Optional[str] = None
    manager_user_id: Optional[str] = None
    access_group_ids: list[str] = []
    roles: list[str] = []
    timezone: str = "UTC"
    tenant_id: Optional[str] = None
    is_active: bool = True
    metadata_json: dict = {}


class UserDirectoryResponse(UserDirectoryEntry):
    created_at: str
    updated_at: str


def _to_response(u: UserDirectoryModel) -> UserDirectoryResponse:
    return UserDirectoryResponse(
        id=u.id, user_id=u.user_id, email=u.email, display_name=u.display_name,
        manager_user_id=u.manager_user_id,
        access_group_ids=u.access_group_ids or [], roles=u.roles or [],
        timezone=u.timezone or "UTC", tenant_id=u.tenant_id, is_active=u.is_active,
        metadata_json=u.metadata_json or {},
        created_at=u.created_at.isoformat() if u.created_at else "",
        updated_at=u.updated_at.isoformat() if u.updated_at else "",
    )


@router.post("", response_model=UserDirectoryResponse, status_code=201)
async def create_entry(
    body: UserDirectoryEntry,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(require_role("admin")),
):
    q = select(UserDirectoryModel).where(UserDirectoryModel.user_id == body.user_id)
    existing = (await session.execute(q)).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(409, f"User {body.user_id} already exists")
    u = UserDirectoryModel(
        user_id=body.user_id, email=body.email, display_name=body.display_name,
        manager_user_id=body.manager_user_id,
        access_group_ids=body.access_group_ids, roles=body.roles,
        timezone=body.timezone, tenant_id=body.tenant_id, is_active=body.is_active,
        metadata_json=body.metadata_json,
    )
    session.add(u)
    await session.flush()
    return _to_response(u)


async def _superadmin_usernames(session: AsyncSession) -> set[str]:
    """Return set of usernames that belong to the superadmin account."""
    rows = (await session.execute(
        select(HelixUserModel.username).where(HelixUserModel.is_superadmin == True)  # noqa: E712
    )).scalars().all()
    return set(rows)


@router.get("", response_model=list[UserDirectoryResponse])
async def list_entries(
    q: Optional[str] = Query(None),
    tenant_id: Optional[str] = Query(None),
    manager_user_id: Optional[str] = Query(None),
    access_group_id: Optional[str] = Query(None),
    active_only: bool = Query(True),
    limit: int = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(require_role("admin")),
):
    if user.tenant_id and "superadmin" not in (user.roles or []):
        tenant_id = user.tenant_id

    # Superadmin accounts are hidden from the user directory
    hidden = await _superadmin_usernames(session)

    stmt = select(UserDirectoryModel)
    if hidden:
        stmt = stmt.where(~UserDirectoryModel.user_id.in_(hidden))
    if active_only:
        stmt = stmt.where(UserDirectoryModel.is_active.is_(True))
    if tenant_id:
        stmt = stmt.where(UserDirectoryModel.tenant_id == tenant_id)
    if manager_user_id:
        stmt = stmt.where(UserDirectoryModel.manager_user_id == manager_user_id)
    if access_group_id:
        # JSONB array contains filter: access_group_ids @> '["<id>"]'
        stmt = stmt.where(
            UserDirectoryModel.access_group_ids.op("@>")(
                cast(literal(json.dumps([access_group_id])), JSONB)
            )
        )
    if q:
        pattern = f"%{q}%"
        stmt = stmt.where(
            (UserDirectoryModel.user_id.ilike(pattern))
            | (UserDirectoryModel.email.ilike(pattern))
            | (UserDirectoryModel.display_name.ilike(pattern))
        )
    stmt = stmt.order_by(UserDirectoryModel.updated_at.desc()).limit(limit)
    res = await session.execute(stmt)
    return [_to_response(u) for u in res.scalars().all()]


@router.get("/{user_id}", response_model=UserDirectoryResponse)
async def get_entry(
    user_id: str,
    session: AsyncSession = Depends(get_session),
):
    q = select(UserDirectoryModel).where(UserDirectoryModel.user_id == user_id)
    u = (await session.execute(q)).scalar_one_or_none()
    if u is None:
        raise HTTPException(404, "User not found")
    return _to_response(u)


async def _sync_auth_active(session: AsyncSession, user_id: str, is_active: bool) -> None:
    """Mirror is_active from UserDirectory into HelixUserModel (the auth table)."""
    auth_user = (await session.execute(
        select(HelixUserModel).where(HelixUserModel.username == user_id)
    )).scalar_one_or_none()
    if auth_user:
        auth_user.is_active = is_active


@router.patch("/{user_id}", response_model=UserDirectoryResponse)
async def update_entry(
    user_id: str,
    body: UserDirectoryEntry,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(require_role("admin")),
):
    q = select(UserDirectoryModel).where(UserDirectoryModel.user_id == user_id)
    u = (await session.execute(q)).scalar_one_or_none()
    if u is None:
        raise HTTPException(404, "User not found")
    prev_active = u.is_active
    u.email = body.email
    u.display_name = body.display_name
    u.manager_user_id = body.manager_user_id
    u.access_group_ids = body.access_group_ids
    u.roles = body.roles
    u.timezone = body.timezone
    u.is_active = body.is_active
    u.metadata_json = body.metadata_json
    # Sync is_active change into the auth login table
    if prev_active != body.is_active:
        await _sync_auth_active(session, user_id, body.is_active)
    await session.flush()
    return _to_response(u)


@router.delete("/{user_id}", status_code=204)
async def deactivate_entry(
    user_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(require_role("admin")),
):
    q = select(UserDirectoryModel).where(UserDirectoryModel.user_id == user_id)
    u = (await session.execute(q)).scalar_one_or_none()
    if u is not None:
        u.is_active = False
        await _sync_auth_active(session, user_id, False)
        await session.flush()
    from starlette.responses import Response
    return Response(status_code=204)


@router.post("/sync-from-db", status_code=200)
async def sync_from_db(
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(require_role("admin")),
):
    """Scan all DB tables for user IDs and create directory entries for any that are missing."""
    from sqlalchemy import text

    # Tables and columns that hold user/actor identifiers
    sources = [
        ("case_instances",          "created_by"),
        ("region_access_logs",      "actor_id"),
        ("notification_preferences","user_id"),
        ("device_tokens",           "user_id"),
        ("operator_access_groups",  "operator_id"),
        ("case_assignments",        "user_id"),
        ("helix_users",             "username"),
    ]

    discovered: set[str] = set()
    for table, col in sources:
        try:
            rows = (await session.execute(
                text(f"SELECT DISTINCT {col} FROM {table} WHERE {col} IS NOT NULL AND {col} != ''")
            )).fetchall()
            discovered.update(r[0] for r in rows)
        except Exception:
            await session.rollback()

    # Fetch already-known user_ids (including inactive)
    existing_rows = (await session.execute(
        select(UserDirectoryModel.user_id)
    )).fetchall()
    existing = {r[0] for r in existing_rows}

    missing = discovered - existing
    created = 0
    for uid in sorted(missing):
        # Infer email and display_name from the user_id format
        if uid.startswith("portal:"):
            email = uid[len("portal:"):]
            display_name = email.split("@")[0]
        elif "@" in uid:
            email = uid
            display_name = uid.split("@")[0]
        else:
            email = None
            display_name = uid

        session.add(UserDirectoryModel(
            user_id=uid,
            email=email,
            display_name=display_name,
            is_active=True,
            metadata_json={"auto_synced": True},
        ))
        created += 1

    await session.commit()
    return {
        "scanned": len(discovered),
        "already_in_directory": len(existing),
        "created": created,
        "new_user_ids": sorted(missing),
    }


@router.post("/bulk-sync", status_code=200)
async def bulk_sync(
    entries: list[UserDirectoryEntry],
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(require_role("admin")),
):
    """Upsert a batch of users (for SSO/LDAP sync)."""
    created, updated = 0, 0
    for e in entries:
        q = select(UserDirectoryModel).where(UserDirectoryModel.user_id == e.user_id)
        existing = (await session.execute(q)).scalar_one_or_none()
        if existing:
            existing.email = e.email
            existing.display_name = e.display_name
            existing.manager_user_id = e.manager_user_id
            existing.access_group_ids = e.access_group_ids
            existing.roles = e.roles
            existing.timezone = e.timezone
            existing.tenant_id = e.tenant_id
            existing.is_active = e.is_active
            existing.metadata_json = e.metadata_json
            updated += 1
        else:
            session.add(UserDirectoryModel(
                user_id=e.user_id, email=e.email, display_name=e.display_name,
                manager_user_id=e.manager_user_id,
                access_group_ids=e.access_group_ids, roles=e.roles,
                timezone=e.timezone, tenant_id=e.tenant_id, is_active=e.is_active,
                metadata_json=e.metadata_json,
            ))
            created += 1
    await session.flush()
    return {"created": created, "updated": updated, "total": created + updated}
