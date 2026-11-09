"""User directory CRUD + bulk sync."""
from __future__ import annotations
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.auth.dependencies import get_current_user, require_role
from velaris_shared.tenancy.identity import tenant_for_write
from velaris_shared import hxguard
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.db.models import UserDirectoryModel, HelixUserModel
from velaris_shared.db.session import get_session

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
    user: AuthenticatedUser = Depends(hxguard.guard("identity.directory.write")),
):
    q = select(UserDirectoryModel).where(UserDirectoryModel.user_id == body.user_id)
    existing = (await session.execute(q)).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(409, f"User {body.user_id} already exists")
    # A directory row IS tenant membership. Stored as sent until 2026-09-30.
    try:
        tenant_id = await tenant_for_write(session, user, body.tenant_id)
    except PermissionError as exc:
        raise HTTPException(403, str(exc))
    u = UserDirectoryModel(
        user_id=body.user_id, email=body.email, display_name=body.display_name,
        manager_user_id=body.manager_user_id,
        access_group_ids=body.access_group_ids, roles=body.roles,
        timezone=body.timezone, tenant_id=tenant_id, is_active=body.is_active,
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
    user: AuthenticatedUser = Depends(hxguard.guard("identity.directory.read")),
):
    no_claim_scope = None
    if "superadmin" not in (user.roles or []):
        if user.tenant_id:
            tenant_id = user.tenant_id
        else:
            # A caller with no tenant claim is the default tenant, never every
            # tenant — and may not pick one with `?tenant_id=` either. Until
            # 2026-09-17 both were open to any holder of the privilege.
            from velaris_shared.tenancy.identity import default_tenant_tokens
            tenant_id = None
            no_claim_scope = await default_tenant_tokens(session)

    # Superadmin accounts are hidden from the user directory
    hidden = await _superadmin_usernames(session)

    stmt = select(UserDirectoryModel)
    if hidden:
        stmt = stmt.where(~UserDirectoryModel.user_id.in_(hidden))
    if active_only:
        stmt = stmt.where(UserDirectoryModel.is_active.is_(True))
    if tenant_id:
        stmt = stmt.where(UserDirectoryModel.tenant_id == tenant_id)
    elif no_claim_scope is not None:
        from sqlalchemy import or_ as _or
        stmt = stmt.where(_or(UserDirectoryModel.tenant_id.is_(None),
                              UserDirectoryModel.tenant_id.in_(no_claim_scope)))
    if manager_user_id:
        stmt = stmt.where(UserDirectoryModel.manager_user_id == manager_user_id)
    if q:
        pattern = f"%{q}%"
        stmt = stmt.where(
            (UserDirectoryModel.user_id.ilike(pattern))
            | (UserDirectoryModel.email.ilike(pattern))
            | (UserDirectoryModel.display_name.ilike(pattern))
        )
    stmt = stmt.order_by(UserDirectoryModel.updated_at.desc())
    # access_group_ids is a deprecated JSON-array column and this filter is
    # unused by the frontend; filter membership in Python (dialect-portable —
    # avoids PG-only JSONB `@>`) and apply the limit AFTER so it isn't truncated
    # before filtering. The directory is bounded (operators per tenant).
    if not access_group_id:
        stmt = stmt.limit(limit)
    res = await session.execute(stmt)
    rows = res.scalars().all()
    if access_group_id:
        rows = [u for u in rows if access_group_id in (u.access_group_ids or [])][:limit]
    return [_to_response(u) for u in rows]


@router.get("/{user_id}", response_model=UserDirectoryResponse)
async def get_entry(
    user_id: str,
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("identity.directory.read")),
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
    user: AuthenticatedUser = Depends(hxguard.guard("identity.directory.write")),
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
    user: AuthenticatedUser = Depends(hxguard.guard("identity.directory.write")),
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
    _: AuthenticatedUser = Depends(hxguard.guard("identity.directory.sync")),
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
        ("velaris_users",             "username"),
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
    # A purged account's id lives on in history (case_instances.created_by …):
    # it must not come back as a directory entry. The deletion ledger names it
    # by id and by a keyed hash of the username (velaris_shared.identity.ledger).
    from velaris_shared.identity import ledger
    gone_ids = await ledger.purged_ids(session)
    gone_names = await ledger.purged_name_hashes(session)
    skipped = sorted(u for u in missing if u in gone_ids or ledger.name_hash(u) in gone_names)
    missing -= set(skipped)
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
        "skipped_purged": len(skipped),
    }


@router.post("/bulk-sync", status_code=200)
async def bulk_sync(
    entries: list[UserDirectoryEntry],
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("identity.directory.sync")),
):
    """Upsert a batch of users (for SSO/LDAP sync).

    **Behaviour change (enterprise identity P0).** This endpoint used to write
    `user_directory` and nothing else, while `sso_callback` wrote `velaris_users`
    and nothing else — so a leaver deactivated here kept their login. It now goes
    through `identity.provisioning`, the single writer that updates both records
    in one transaction, alongside tenant membership and IdP-mapped access groups.

    An entry with no email is still accepted and still creates a directory-only
    row: this endpoint has always been able to record a person who has no
    Velaris login (a manager named as an escalation target, a `portal:` id), and
    refusing those would be a silent narrowing of a shipped API.
    """
    from user_service.identity import provisioning

    created = updated = deactivated = rejected = 0
    results: list[dict] = []

    for e in entries:
        res = None
        if not e.is_active:
            res = await provisioning.deactivate_principal(
                session, source="bulk_sync",
                username=e.user_id, email=e.email,
                tenant_token=e.tenant_id, actor=user.username,
                allow_directory_only=True,
            )
            # Deactivating somebody who does not exist yet used to create the row
            # inactive, and a sync that leads with its leavers relies on that.
            # Fall through to the upsert rather than reject.
            if res.reason == "not_found":
                res = None

        if res is None:
            res = await provisioning.upsert_principal(
                session, source="bulk_sync",
                username=e.user_id, email=e.email,
                display_name=e.display_name,
                tenant_token=e.tenant_id,
                attributes={
                    "manager": e.manager_user_id,
                    "timezone": e.timezone,
                    "metadata": e.metadata_json,
                },
                roles=e.roles,
                active=e.is_active,
                actor=user.username,
                allow_directory_only=True,
            )

        # `roles` and `access_group_ids` on the directory row are deprecated
        # columns the spine deliberately does not own — authorization lives in
        # `operator_access_groups`. They are still written here because this
        # endpoint has always written them and `list_entries` still filters on
        # them; dropping them silently would break a caller mid-upgrade.
        if res.ok and res.username and (e.roles or e.access_group_ids):
            # res.username, not e.user_id: an entry matched by email can belong
            # to an account renamed in Velaris, and the directory row is keyed on
            # the account's real username.
            row = (await session.execute(
                select(UserDirectoryModel).where(UserDirectoryModel.user_id == res.username)
            )).scalar_one_or_none()
            if row is not None:
                row.roles = e.roles
                row.access_group_ids = e.access_group_ids

        if res.action == "create":
            created += 1
        elif res.action == "update":
            updated += 1
        elif res.action == "deactivate":
            deactivated += 1
        else:
            rejected += 1
            results.append({"user_id": e.user_id, "reason": res.reason})

    await session.flush()
    return {
        "created": created, "updated": updated,
        "deactivated": deactivated, "rejected": rejected,
        "total": created + updated + deactivated,
        "rejections": results,
    }
