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


def _to_response(u: UserDirectoryModel, live: dict | None = None) -> UserDirectoryResponse:
    """`live` (from `_live`) replaces the row's `roles` and `access_group_ids`
    with what sign-in actually uses — see `_live`."""
    roles, groups = (live or {}).get(u.user_id, (u.roles or [], u.access_group_ids or []))
    return UserDirectoryResponse(
        id=u.id, user_id=u.user_id, email=u.email, display_name=u.display_name,
        manager_user_id=u.manager_user_id,
        access_group_ids=groups, roles=roles,
        timezone=u.timezone or "UTC", tenant_id=u.tenant_id, is_active=u.is_active,
        metadata_json=u.metadata_json or {},
        created_at=u.created_at.isoformat() if u.created_at else "",
        updated_at=u.updated_at.isoformat() if u.updated_at else "",
    )


async def _live(session: AsyncSession, rows: list[UserDirectoryModel]) -> dict:
    """{user_id: (roles, access_group_ids)} as sign-in reads them (2026-11-09).

    The row's own `roles` and `access_group_ids` are deprecated columns that
    nothing reads: the token's roles are the ACCOUNT's (`velaris_users.roles`),
    and access groups are `operator_access_groups`. The User Directory screen
    showed and edited the dead columns, so a role or group set there did
    nothing. A row with no account keeps its own columns, as before.
    """
    from sqlalchemy import or_
    from velaris_shared.db.models import OperatorAccessGroupModel as O

    ids = [r.user_id for r in rows]
    if not ids:
        return {}
    uuids = []
    for i in ids:
        try:
            uuids.append(uuid.UUID(str(i)))
        except ValueError:
            pass
    conds = [HelixUserModel.username.in_(ids), HelixUserModel.email.in_(ids)]
    if uuids:
        # A row made by a tenant's admin is keyed by the account's id.
        conds.append(HelixUserModel.id.in_(uuids))
    accts = (await session.execute(select(HelixUserModel).where(or_(*conds)))).scalars().all()
    by_id: dict[str, HelixUserModel] = {}
    for a in accts:
        by_id.setdefault(str(a.id), a)
        by_id.setdefault(a.username, a)
        if a.email:
            by_id.setdefault(a.email, a)
    idents: dict[str, list[str]] = {}
    for r in rows:
        a = by_id.get(r.user_id)
        idents[r.user_id] = ([str(a.id), a.username] + ([a.email] if a.email else [])) if a else [r.user_id]
    flat = {i for v in idents.values() for i in v}
    member = {}
    for o in (await session.execute(select(O).where(O.operator_id.in_(flat)))).scalars().all():
        member.setdefault(o.operator_id, []).append(str(o.access_group_id))
    out = {}
    for r in rows:
        a = by_id.get(r.user_id)
        if a is None:
            continue
        groups: list[str] = []
        for i in idents[r.user_id]:
            groups += [g for g in member.get(i, []) if g not in groups]
        out[r.user_id] = (list(a.roles or []), groups)
    return out


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
    # A tenant admin's register already made this account's row, keyed by the
    # account id (tenancy/placement.py); Studio's second step names the
    # username. Fill that row in rather than make a second one — two rows for
    # one person showed twice, and sign-in reads only one (2026-11-09).
    from velaris_shared.tenancy import home as _home
    idents = await _home.account_identifiers(session, body.user_id)
    prior = await _home.directory_rows(session, idents) if len(idents) > 1 else []
    if prior:
        row = prior[0]
        try:
            wanted = await tenant_for_write(session, user, body.tenant_id)
        except PermissionError as exc:
            raise HTTPException(403, str(exc))
        from velaris_shared.tenancy.identity import DEFAULT_TENANT, same_tenant
        if wanted and not await same_tenant(session, row.tenant_id or DEFAULT_TENANT, wanted):
            raise HTTPException(409, "This user already has a directory entry in another "
                                     "tenant; change its tenant with Edit.")
        row.email = body.email or row.email
        row.display_name = body.display_name or row.display_name
        row.manager_user_id = body.manager_user_id
        row.timezone = body.timezone
        row.is_active = body.is_active
        row.metadata_json = {**(row.metadata_json or {}), **(body.metadata_json or {})}
        await session.flush()
        return _to_response(row, await _live(session, [row]))
    # A directory row IS tenant membership. Stored as sent until 2026-09-30.
    try:
        tenant_id = await tenant_for_write(session, user, body.tenant_id)
    except PermissionError as exc:
        raise HTTPException(403, str(exc))
    if tenant_id:
        # An operator's write is stored as sent; a tenant that does not exist
        # left the user in no tenant anyone could manage (2026-11-09).
        from velaris_shared.tenancy.identity import resolve_tenant
        resolved = await resolve_tenant(session, tenant_id)
        if resolved is None:
            raise HTTPException(422, f"No tenant '{tenant_id}'.")
    # Memberships recorded before the person existed must match the tenant
    # they are now given (2026-11-09).
    from velaris_shared.tenancy import home
    from velaris_shared.tenancy.identity import DEFAULT_TENANT
    clash = await home.groups_outside(
        session, await home.account_identifiers(session, body.user_id), tenant_id or DEFAULT_TENANT)
    if clash:
        raise HTTPException(409, "This user is already in access groups of another tenant: "
                                 + ", ".join(sorted(g.name for g in clash))
                                 + ". Choose that tenant, or remove them from those groups first.")
    u = UserDirectoryModel(
        user_id=body.user_id, email=body.email, display_name=body.display_name,
        manager_user_id=body.manager_user_id,
        access_group_ids=body.access_group_ids, roles=body.roles,
        timezone=body.timezone, tenant_id=tenant_id, is_active=body.is_active,
        metadata_json=body.metadata_json,
    )
    session.add(u)
    await session.flush()
    return _to_response(u, await _live(session, [u]))


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
        # The column holds a tenant's id OR its slug, depending on who wrote the
        # row (a tenant admin's write stores their claim's spelling); match
        # both, or a tenant's own users go missing from its list (2026-11-09).
        from velaris_shared.tenancy.identity import resolve_tenant
        resolved = await resolve_tenant(session, tenant_id)
        spellings = {str(tenant_id)} | ({str(resolved[0]), str(resolved[1])} if resolved else set())
        stmt = stmt.where(UserDirectoryModel.tenant_id.in_(sorted(spellings)))
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
    live = await _live(session, list(rows))
    return [_to_response(u, live) for u in rows]


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
    return _to_response(u, await _live(session, [u]))


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
    live = (await _live(session, [u])).get(u.user_id)
    sent = body.model_fields_set
    if "tenant_id" in sent:
        await _move_tenant(session, user, u, body.tenant_id)
    if live is not None and "roles" in sent and sorted(body.roles) != sorted(live[0]):
        await _set_account_roles(session, user, u, body.roles)
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
    return _to_response(u, await _live(session, [u]))


async def _move_tenant(session: AsyncSession, actor: AuthenticatedUser,
                       row: UserDirectoryModel, requested: Optional[str]) -> None:
    """Put a user in another tenant — the tenant sign-in gives them (2026-11-09).

    Until now nothing could: PATCH ignored `tenant_id`, and the screens that
    looked as if they set it (an access group's tenant, Tenants → Members) are
    not what sign-in reads. Only the platform operator moves people between
    tenants; a tenant's admin sending their own row's tenant back is a no-op.
    Every directory row of the account moves, so sign-in's precedence cannot
    pick a stale one. Refused while the user is in another tenant's access
    group — that membership would otherwise follow them across.
    """
    from velaris_shared.tenancy import home
    from velaris_shared.tenancy.identity import (
        DEFAULT_TENANT, is_platform_operator, resolve_tenant, same_tenant,
    )

    target = (requested or "").strip() or None
    if await same_tenant(session, row.tenant_id or DEFAULT_TENANT, target or DEFAULT_TENANT) \
            and await same_tenant(session, target or DEFAULT_TENANT, row.tenant_id or DEFAULT_TENANT):
        return
    if not await is_platform_operator(session, actor):
        raise HTTPException(403, "Only the platform operator may move a user to another tenant.")
    idents_self = {i for i in (actor.user_id, actor.username, getattr(actor, "email", None)) if i}
    if row.user_id in idents_self:
        # Moving yourself out of the default tenant ends your operator powers
        # mid-session; another operator (or superadmin) must do it.
        raise HTTPException(409, "You cannot move your own account to another tenant.")
    if target is not None:
        resolved = await resolve_tenant(session, target)
        if resolved is None:
            raise HTTPException(422, f"No tenant '{target}'.")
        from velaris_shared.tenancy.limits import TenantLimitError, require_user_room
        try:
            await require_user_room(session, target)
        except TenantLimitError as exc:
            raise HTTPException(403, str(exc)) from exc
    idents = await home.account_identifiers(session, row.user_id)
    clash = await home.groups_outside(session, idents, target or DEFAULT_TENANT)
    if clash:
        raise HTTPException(409, "Remove the user from these access groups of another tenant "
                                 "first: " + ", ".join(sorted(g.name for g in clash)))
    for r in await home.directory_rows(session, idents):
        r.tenant_id = target
    await _bump_authz(session, actor, f"user.tenant:{row.user_id}")


async def _bump_authz(session: AsyncSession, actor: AuthenticatedUser, reason: str) -> None:
    """Answers are cached 30 s in every service; a move or a role change must
    not keep working for that long (the same rule as access-group edits)."""
    from velaris_shared.hxguard import generation as _gen
    hxguard.invalidate_cache()
    await _gen.bump(session, reason=reason, actor=actor.user_id)


async def _set_account_roles(session: AsyncSession, actor: AuthenticatedUser,
                             row: UserDirectoryModel, roles: list[str]) -> None:
    """A role edit on this screen changes the account's roles — what the token
    carries (2026-11-09; before, it wrote a column nothing reads). The same
    rules as `PATCH /auth/real/users/{id}`, which it stands in for."""
    from user_service.api.routers.auth_real import (
        _assert_can_modify, _assert_role_grant, _target_in_reach,
    )

    conds = [HelixUserModel.username == row.user_id, HelixUserModel.email == row.user_id]
    try:
        conds.append(HelixUserModel.id == uuid.UUID(str(row.user_id)))
    except ValueError:
        pass
    from sqlalchemy import or_
    acct = (await session.execute(select(HelixUserModel).where(or_(*conds)))).scalars().first()
    if acct is None:
        return
    if not hxguard.user_allows(actor, "identity.users.write"):
        raise HTTPException(403, "Changing a user's roles needs identity.users.write.")
    await _target_in_reach(session, actor, acct)
    _assert_can_modify(actor, acct)
    _assert_role_grant(actor, roles, acct.roles)
    if "superadmin" in (acct.roles or []) and "superadmin" not in roles:
        raise HTTPException(403, "Cannot remove superadmin role via API.")
    acct.roles = list(roles)
    acct.updated_at = datetime.now(timezone.utc)
    await _bump_authz(session, actor, f"user.roles:{row.user_id}")


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
