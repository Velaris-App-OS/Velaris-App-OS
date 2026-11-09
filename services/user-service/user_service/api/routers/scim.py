"""SCIM 2.0 server — joiner, mover, leaver, driven by the customer's IdP.

Mounted at ``/api/v1/scim/v2``. Every write goes through
:mod:`user_service.identity.provisioning`, so a SCIM deactivation is the same
operation as a `bulk-sync` one and reaches both the login record and the profile
record. Nothing here writes a table directly.

**Authentication is a machine token, not a JWT.** The caller is Entra or Okta
running on its own schedule, not a person: a token that expires with a session,
belongs to somebody who may leave, and carries a human's roles is the wrong
credential. `scim_tokens` follows the two machine-token systems already proven
here — SHA-256 at rest, constant-time compare, one uniform 401, instant
revocation — and, critically, **the tenant comes from the token row and is never
a request parameter**.

**Deprovision is always soft.** `DELETE /Users/{id}` and
`PATCH {"active": false}` both route to `deactivate_principal`, which disables
and revokes sessions rather than deleting. Cases reference their creator and
assignee by user id; a hard delete orphans that history, and SCIM offers no way
to ask for one.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared import hxguard
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.tenancy.identity import tenant_for_write
from velaris_shared.db.models import (
    AccessGroupModel,
    HelixUserModel,
    OperatorAccessGroupModel,
    ScimTokenModel,
    UserDirectoryModel,
)
from velaris_shared.db.session import get_session
from user_service.identity import provisioning, scim
from velaris_shared.middleware.endpoint_rate_limit import rate_limit

log = logging.getLogger(__name__)

router = APIRouter(prefix="/scim", tags=["scim"])

_scim_rl = rate_limit(max_calls=600, window_seconds=60, name="scim")

SCIM_MEDIA_TYPE = "application/scim+json"


def _scim_json(
    body: dict | list, status: int = 200, *, location: str | None = None,
) -> JSONResponse:
    # RFC 7644 §3.1: a create response carries `Location`. Okta's validator
    # checks the header rather than `meta.location`, and a client that follows
    # it has nowhere to go without one.
    headers = {"Location": location} if location else None
    return JSONResponse(content=body, status_code=status,
                        media_type=SCIM_MEDIA_TYPE, headers=headers)


def _error(exc: scim.ScimError) -> JSONResponse:
    return _scim_json(exc.body(), status=exc.status)


# ── authentication ────────────────────────────────────────────────────────

class ScimPrincipal:
    """The authenticated SCIM client. Carries its tenant; the request never does."""

    def __init__(self, token: ScimTokenModel):
        self.token_id = str(token.id)
        self.tenant_token = token.tenant_id
        self.name = token.name
        #: Shaped like a username so audit and provisioning logs name a real actor.
        self.actor = f"scim:{token.name}"


async def scim_principal(
    request: Request, session: AsyncSession = Depends(get_session),
) -> ScimPrincipal:
    """Resolve the bearer token, or refuse identically for every reason.

    Missing header, wrong scheme, unknown token, revoked token, expired token:
    one 401 with one body. Distinguishing them would let a caller probe the
    token space one property at a time — and "revoked" in particular tells an
    attacker their stolen credential was real.
    """
    unauthorized = scim.ScimError(401, "Unauthorized.")

    header = request.headers.get("authorization") or ""
    scheme, _, presented = header.partition(" ")
    if scheme.lower() != "bearer" or not presented.strip():
        raise unauthorized

    digest = hashlib.sha256(presented.strip().encode()).hexdigest()
    row = (await session.execute(
        select(ScimTokenModel).where(ScimTokenModel.token_hash == digest)
    )).scalar_one_or_none()

    # The lookup is by hash so it is a single indexed probe, but the comparison
    # is still constant-time: an index probe on a digest leaks nothing, and this
    # keeps the property true if the lookup ever changes shape.
    if row is None or not secrets.compare_digest(row.token_hash, digest):
        raise unauthorized

    now = datetime.now(timezone.utc)
    if row.revoked_at is not None:
        raise unauthorized
    expires_at = row.expires_at
    if expires_at is not None:
        # SQLite hands back naive datetimes where PostgreSQL returns aware ones.
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= now:
            raise unauthorized

    row.last_used_at = now
    await session.commit()
    return ScimPrincipal(row)


def _base_url(request: Request) -> str:
    return str(request.url).split("/scim/v2")[0] + "/scim/v2"


# ── discovery ─────────────────────────────────────────────────────────────

@router.get("/v2/ServiceProviderConfig", dependencies=[Depends(_scim_rl)])
async def service_provider_config(
    request: Request, _p: ScimPrincipal = Depends(scim_principal),
):
    return _scim_json(scim.service_provider_config(_base_url(request)))


@router.get("/v2/ResourceTypes", dependencies=[Depends(_scim_rl)])
async def resource_types(request: Request, _p: ScimPrincipal = Depends(scim_principal)):
    types = scim.resource_types(_base_url(request))
    return _scim_json(scim.list_response(types, total=len(types), start_index=1,
                                         count=len(types)))


@router.get("/v2/Schemas", dependencies=[Depends(_scim_rl)])
async def schemas(request: Request, _p: ScimPrincipal = Depends(scim_principal)):
    """The schema URIs we honour. Okta's validator refuses to proceed without it."""
    listed = [
        {"id": scim.SCHEMA_USER, "name": "User", "description": "SCIM core User"},
        {"id": scim.SCHEMA_GROUP, "name": "Group", "description": "SCIM core Group"},
        {"id": scim.SCHEMA_ENTERPRISE_USER, "name": "EnterpriseUser",
         "description": "Manager and organisational attributes"},
    ]
    return _scim_json(scim.list_response(listed, total=len(listed), start_index=1,
                                         count=len(listed)))


# ── users ─────────────────────────────────────────────────────────────────

async def _directory_for(session: AsyncSession, username: str) -> UserDirectoryModel | None:
    return (await session.execute(
        select(UserDirectoryModel).where(UserDirectoryModel.user_id == username)
    )).scalar_one_or_none()


async def _groups_of(session: AsyncSession, username: str) -> list[tuple[str, str]]:
    rows = (await session.execute(
        select(AccessGroupModel.id, AccessGroupModel.name)
        .join(OperatorAccessGroupModel,
              OperatorAccessGroupModel.access_group_id == AccessGroupModel.id)
        .where(OperatorAccessGroupModel.operator_id == username)
    )).all()
    return [(str(gid), name) for gid, name in rows]


def _tenant_scope(stmt, principal: ScimPrincipal):
    """Restrict a directory query to the token's tenant.

    A tenantless token (single-tenant deployment) sees everything, which is what
    it is for. A tenant-scoped one sees only its own rows **and** rows tagged
    with no tenant, because the directory predates tenancy and a deployment that
    added a tenant later still has untagged people in it.

    **A missing directory row is not an untagged one.** The query outer-joins,
    so a user with no `user_directory` row also produces `tenant_id IS NULL` —
    which made every account created outside the provisioning spine (local
    admins, pre-spine accounts) visible to *every* tenant's token, and
    deactivatable by it. `user_id IS NOT NULL` is what separates "the directory
    says no tenant" from "the directory has never heard of them".
    """
    if not principal.tenant_token:
        return stmt
    return stmt.where(
        UserDirectoryModel.user_id.is_not(None),
        or_(
            UserDirectoryModel.tenant_id == principal.tenant_token,
            UserDirectoryModel.tenant_id.is_(None),
        ),
    )


async def _visible_user(
    session: AsyncSession, principal: ScimPrincipal, user_id: str,
) -> HelixUserModel | None:
    """A user this token may act on, or None.

    Cross-tenant reads answer 404, never 403: a 403 confirms the id exists.

    A tenant-scoped token needs a directory row to act on, and it must be this
    tenant's (or untagged). **No row at all is a refusal**, not a pass: accounts
    created outside the provisioning spine have none, and treating that as
    "belongs to nobody, so anybody may have it" let one tenant's IdP read and
    deactivate another tenant's administrator.
    """
    try:
        key = uuid.UUID(str(user_id))
    except (ValueError, AttributeError):
        return None
    user = await session.get(HelixUserModel, key)
    # A deleted account (mig 223) is gone as far as the directory is concerned:
    # SCIM may neither read nor re-activate it — only an administrator's undo.
    if user is None or user.deleted_at is not None:
        return None
    if principal.tenant_token:
        entry = await _directory_for(session, user.username)
        if entry is None:
            return None
        if entry.tenant_id and entry.tenant_id != principal.tenant_token:
            return None
    return user


@router.get("/v2/Users", dependencies=[Depends(_scim_rl)])
async def list_users(
    request: Request,
    filter: Optional[str] = Query(None),
    startIndex: int = Query(1, ge=1),
    count: int = Query(scim.DEFAULT_PAGE_SIZE, ge=0),
    session: AsyncSession = Depends(get_session),
    principal: ScimPrincipal = Depends(scim_principal),
):
    """List or search. The filter subset is documented in `identity/scim.py`."""
    try:
        terms = scim.parse_filter(filter)
    except scim.ScimError as exc:
        return _error(exc)

    stmt = select(HelixUserModel).outerjoin(
        UserDirectoryModel, UserDirectoryModel.user_id == HelixUserModel.username)
    stmt = stmt.where(HelixUserModel.deleted_at.is_(None))      # deleted = gone (mig 223)
    stmt = _tenant_scope(stmt, principal)

    for term in terms:
        if term.attribute == "username":
            stmt = stmt.where(func.lower(HelixUserModel.username) == str(term.value).lower())
        elif term.attribute == "externalid":
            stmt = stmt.where(HelixUserModel.external_id == str(term.value))
        elif term.attribute == "active":
            stmt = stmt.where(HelixUserModel.is_active.is_(bool(term.value)))
        elif term.attribute == "displayname":
            stmt = stmt.where(HelixUserModel.display_name == str(term.value))
        elif term.attribute == "emails.value":
            stmt = stmt.where(func.lower(HelixUserModel.email) == str(term.value).lower())
        elif term.attribute == "id":
            try:
                stmt = stmt.where(HelixUserModel.id == uuid.UUID(str(term.value)))
            except ValueError:
                return _scim_json(scim.list_response([], total=0, start_index=startIndex,
                                                     count=0))

    # A superadmin is not a SCIM-manageable principal: it exists so a human can
    # recover the platform, and an IdP that could see it could try to change it.
    stmt = stmt.where(HelixUserModel.is_superadmin.is_(False))

    rows = (await session.execute(stmt.order_by(HelixUserModel.username))).scalars().all()
    total = len(rows)
    page = min(count, scim.MAX_PAGE_SIZE)
    window = rows[startIndex - 1: startIndex - 1 + page] if page else []

    base = _base_url(request)
    resources = []
    for user in window:
        resources.append(scim.user_resource(
            user, await _directory_for(session, user.username),
            base_url=base, groups=await _groups_of(session, user.username),
        ))
    return _scim_json(scim.list_response(
        resources, total=total, start_index=startIndex, count=len(resources)))


@router.get("/v2/Users/{user_id}", dependencies=[Depends(_scim_rl)])
async def get_user(
    user_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: ScimPrincipal = Depends(scim_principal),
):
    user = await _visible_user(session, principal, user_id)
    if user is None or user.is_superadmin:
        return _error(scim.ScimError(404, "User not found."))
    return _scim_json(scim.user_resource(
        user, await _directory_for(session, user.username),
        base_url=_base_url(request), groups=await _groups_of(session, user.username),
    ))


@router.post("/v2/Users", dependencies=[Depends(_scim_rl)])
async def create_user(
    payload: dict,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: ScimPrincipal = Depends(scim_principal),
):
    username = str(payload.get("userName") or "").strip()
    if not username:
        return _error(scim.ScimError(400, "userName is required.", "invalidValue"))

    external_id = str(payload.get("externalId") or "").strip() or None
    email = scim.primary_email(payload)

    # POST means *create*. `upsert_principal` is an upsert and will match an
    # existing account by email — which here would rewrite somebody else's
    # record and hand the IdP their id as the new user's. A collision on any of
    # the three identifiers is a 409, and `uniqueness` tells the client to fetch
    # the existing resource rather than retry forever.
    clash = await provisioning.find_principal(
        session, external_id=external_id, username=username, email=email,
    )
    if clash is not None:
        return _error(scim.ScimError(
            409, "A user with that identifier already exists.", "uniqueness"))

    result = await provisioning.upsert_principal(
        session, source="scim",
        external_id=external_id,
        username=username,
        email=email,
        display_name=scim.display_name(payload),
        tenant_token=principal.tenant_token,
        attributes={
            "manager": scim.manager_of(payload),
            "timezone": payload.get("timezone"),
            "skills": scim.skills_of(payload),
            # Remembered so a later PATCH carrying only one half of the name
            # has the other half to join it to.
            "metadata": {scim.NAME_PARTS_KEY: scim.name_parts_of(payload)},
        },
        active=payload.get("active", True) is not False,
        actor=principal.actor,
    )
    # Keep the provisioning-log row whichever way this went.
    await session.commit()

    if not result.ok:
        # `uniqueness` is the RFC keyword for "this resource already exists",
        # and Entra treats a 409 with it as "fetch the existing one" rather than
        # as a failure to retry.
        if result.reason in {"email_taken", "username_taken"}:
            return _error(scim.ScimError(409, "A user with that identifier already exists.",
                                         "uniqueness"))
        if result.reason == "superadmin_protected":
            return _error(scim.ScimError(403, "That account cannot be managed over SCIM."))
        return _error(scim.ScimError(400, "The user could not be provisioned.",
                                     "invalidValue"))

    user = (await session.execute(
        select(HelixUserModel).where(HelixUserModel.username == result.username)
    )).scalar_one()
    resource = scim.user_resource(
        user, await _directory_for(session, user.username),
        base_url=_base_url(request),
    )
    return _scim_json(resource, status=201,
                      location=resource.get("meta", {}).get("location"))


@router.put("/v2/Users/{user_id}", dependencies=[Depends(_scim_rl)])
async def replace_user(
    user_id: str,
    payload: dict,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: ScimPrincipal = Depends(scim_principal),
):
    user = await _visible_user(session, principal, user_id)
    if user is None or user.is_superadmin:
        return _error(scim.ScimError(404, "User not found."))

    active = payload.get("active", True) is not False
    if not active:
        result = await provisioning.deactivate_principal(
            session, source="scim", username=user.username,
            tenant_token=principal.tenant_token, actor=principal.actor,
        )
    else:
        result = await provisioning.upsert_principal(
            session, source="scim",
            external_id=str(payload.get("externalId") or "").strip() or None,
            username=user.username,
            email=scim.primary_email(payload),
            display_name=scim.display_name(payload),
            tenant_token=principal.tenant_token,
            attributes={
                "manager": scim.manager_of(payload),
                "timezone": payload.get("timezone"),
                "skills": scim.skills_of(payload),
                # Remembered so a later PATCH carrying only one half of the name
                # has the other half to join it to.
                "metadata": {scim.NAME_PARTS_KEY: scim.name_parts_of(payload)},
            },
            active=True,
            actor=principal.actor,
        )
    await session.commit()

    if not result.ok:
        if result.reason == "superadmin_protected":
            return _error(scim.ScimError(403, "That account cannot be managed over SCIM."))
        if result.reason == "email_taken":
            return _error(scim.ScimError(409, "That email address belongs to another user.",
                                         "uniqueness"))
        return _error(scim.ScimError(400, "The user could not be updated.", "invalidValue"))

    await session.refresh(user)
    return _scim_json(scim.user_resource(
        user, await _directory_for(session, user.username),
        base_url=_base_url(request), groups=await _groups_of(session, user.username),
    ))


@router.patch("/v2/Users/{user_id}", dependencies=[Depends(_scim_rl)])
async def patch_user(
    user_id: str,
    payload: dict,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: ScimPrincipal = Depends(scim_principal),
):
    """RFC 7644 §3.5.2. This is how Entra and Okta signal a leaver."""
    user = await _visible_user(session, principal, user_id)
    if user is None or user.is_superadmin:
        return _error(scim.ScimError(404, "User not found."))

    try:
        patch = scim.parse_user_patch(payload)
    except scim.ScimError as exc:
        return _error(exc)

    if patch.active is False:
        result = await provisioning.deactivate_principal(
            session, source="scim", username=user.username,
            tenant_token=principal.tenant_token, actor=principal.actor,
        )
    else:
        # A rename arrives one half of a name at a time, so the halves we were
        # told last time are read back and merged before they are joined.
        directory = await _directory_for(session, user.username)
        stored_parts = (directory.metadata_json or {}).get(scim.NAME_PARTS_KEY) \
            if directory is not None else None
        display, name_parts = scim.compose_display_name(
            patch, stored_parts,
            current_display_name=(directory.display_name if directory is not None
                                  else user.display_name),
        )

        attributes: dict = {}
        if patch.manager is not None:
            attributes["manager"] = patch.manager
        if name_parts:
            attributes["metadata"] = {scim.NAME_PARTS_KEY: name_parts}

        result = await provisioning.upsert_principal(
            session, source="scim",
            external_id=patch.external_id,
            username=user.username,
            email=patch.email,
            display_name=display,
            tenant_token=principal.tenant_token,
            attributes=attributes,
            # `None` when the patch said nothing about `active`: a name change
            # must not reactivate somebody disabled a moment earlier.
            active=patch.active,
            actor=principal.actor,
        )
    await session.commit()

    if not result.ok:
        if result.reason == "superadmin_protected":
            return _error(scim.ScimError(403, "That account cannot be managed over SCIM."))
        if result.reason == "email_taken":
            return _error(scim.ScimError(409, "That email address belongs to another user.",
                                         "uniqueness"))
        return _error(scim.ScimError(400, "The user could not be updated.", "invalidValue"))

    await session.refresh(user)
    return _scim_json(scim.user_resource(
        user, await _directory_for(session, user.username),
        base_url=_base_url(request), groups=await _groups_of(session, user.username),
    ))


@router.delete("/v2/Users/{user_id}", status_code=204, dependencies=[Depends(_scim_rl)])
async def delete_user(
    user_id: str,
    session: AsyncSession = Depends(get_session),
    principal: ScimPrincipal = Depends(scim_principal),
):
    """Deactivates. Never deletes — and still answers 204, so the IdP is satisfied.

    Cases reference their creator and assignee by user id, so removing the row
    orphans history that an audit depends on. There is no hard-delete option to
    ask for.
    """
    user = await _visible_user(session, principal, user_id)
    if user is None or user.is_superadmin:
        return _error(scim.ScimError(404, "User not found."))

    result = await provisioning.deactivate_principal(
        session, source="scim", username=user.username,
        tenant_token=principal.tenant_token, actor=principal.actor,
    )
    await session.commit()
    if not result.ok and result.reason == "superadmin_protected":
        return _error(scim.ScimError(403, "That account cannot be managed over SCIM."))
    return Response(status_code=204)


# ── groups ────────────────────────────────────────────────────────────────

async def _members_of(session: AsyncSession, group_id: uuid.UUID) -> list[tuple[str, str]]:
    rows = (await session.execute(
        select(HelixUserModel.id, HelixUserModel.username)
        .join(OperatorAccessGroupModel,
              OperatorAccessGroupModel.operator_id == HelixUserModel.username)
        .where(OperatorAccessGroupModel.access_group_id == group_id)
    )).all()
    return [(str(uid), username) for uid, username in rows]


@router.get("/v2/Groups", dependencies=[Depends(_scim_rl)])
async def list_groups(
    request: Request,
    filter: Optional[str] = Query(None),
    startIndex: int = Query(1, ge=1),
    count: int = Query(scim.DEFAULT_PAGE_SIZE, ge=0),
    session: AsyncSession = Depends(get_session),
    principal: ScimPrincipal = Depends(scim_principal),
):
    """Access groups, read-only over SCIM.

    Groups are **not** created from the directory: an access group binds a
    portal, roles and case-type scope, all of which are Velaris decisions an IdP
    has no way to express. The IdP asserts *membership*, which arrives through
    `identity_group_mappings`; listing exists so its administrator can see what
    is mappable.
    """
    try:
        terms = scim.parse_filter(filter)
    except scim.ScimError as exc:
        return _error(exc)

    stmt = select(AccessGroupModel)
    if principal.tenant_token:
        stmt = stmt.where(AccessGroupModel.tenant_id == principal.tenant_token)
    for term in terms:
        if term.attribute == "displayname":
            stmt = stmt.where(AccessGroupModel.name == str(term.value))
        elif term.attribute == "id":
            try:
                stmt = stmt.where(AccessGroupModel.id == uuid.UUID(str(term.value)))
            except ValueError:
                return _scim_json(scim.list_response([], total=0, start_index=startIndex,
                                                     count=0))

    rows = (await session.execute(stmt.order_by(AccessGroupModel.name))).scalars().all()
    page = min(count, scim.MAX_PAGE_SIZE)
    window = rows[startIndex - 1: startIndex - 1 + page] if page else []

    base = _base_url(request)
    resources = [
        scim.group_resource(g, await _members_of(session, g.id), base_url=base)
        for g in window
    ]
    return _scim_json(scim.list_response(
        resources, total=len(rows), start_index=startIndex, count=len(resources)))


@router.get("/v2/Groups/{group_id}", dependencies=[Depends(_scim_rl)])
async def get_group(
    group_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    principal: ScimPrincipal = Depends(scim_principal),
):
    try:
        key = uuid.UUID(group_id)
    except ValueError:
        return _error(scim.ScimError(404, "Group not found."))
    group = await session.get(AccessGroupModel, key)
    if group is None or (principal.tenant_token
                         and group.tenant_id != principal.tenant_token):
        return _error(scim.ScimError(404, "Group not found."))
    return _scim_json(scim.group_resource(
        group, await _members_of(session, group.id), base_url=_base_url(request)))


# ── token administration ──────────────────────────────────────────────────

class ScimTokenIn(BaseModel):
    name: str = Field(..., max_length=255)
    tenant_id: Optional[str] = None
    expires_at: Optional[datetime] = None


@router.get("/tokens")
async def list_scim_tokens(
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("identity.scim.read")),
):
    """Metadata only. The token itself is unrecoverable by design."""
    # Every tenant's rows until 2026-09-29 (8d).
    from velaris_shared.tenancy.identity import scope_to_caller
    rows = (await session.execute(await scope_to_caller(
        select(ScimTokenModel).order_by(ScimTokenModel.created_at.desc()),
        ScimTokenModel, session, _user))).scalars().all()
    return {"tokens": [{
        "id": str(r.id), "name": r.name, "tenant_id": r.tenant_id,
        "created_by": r.created_by,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "last_used_at": r.last_used_at.isoformat() if r.last_used_at else None,
        "expires_at": r.expires_at.isoformat() if r.expires_at else None,
        "revoked_at": r.revoked_at.isoformat() if r.revoked_at else None,
    } for r in rows]}


@router.post("/tokens", status_code=201)
async def create_scim_token(
    body: ScimTokenIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("identity.scim.write")),
):
    """Mint a token. **The plaintext is in this response and nowhere else.**"""
    # A SCIM token provisions and deactivates accounts in its tenant. Stored as
    # sent until 2026-09-30 — a tenant admin could mint one for another tenant.
    try:
        tenant_id = await tenant_for_write(session, user, body.tenant_id or None)
    except PermissionError as exc:
        raise HTTPException(403, str(exc))
    plaintext = secrets.token_urlsafe(40)
    row = ScimTokenModel(
        tenant_id=tenant_id,
        name=body.name,
        token_hash=hashlib.sha256(plaintext.encode()).hexdigest(),
        scopes=["users:write", "groups:read"],
        created_by=user.username,
        expires_at=body.expires_at,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)

    from velaris_shared.db.security_events import log_security_event
    await log_security_event(
        session, event_type="scim_token_minted", severity="medium",
        user_id=user.user_id, resource_type="scim_token", resource_id=str(row.id),
        action="create", outcome="success",
        details={"name": row.name, "tenant_id": row.tenant_id},
    )
    await session.commit()

    return {"id": str(row.id), "name": row.name, "tenant_id": row.tenant_id,
            "token": plaintext,
            "warning": "This token is shown once. Store it in the IdP now."}


@router.delete("/tokens/{token_id}", status_code=204)
async def revoke_scim_token(
    token_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("identity.scim.write")),
):
    """Revoke, not delete: the row is why the IdP started getting 401s."""
    row = await session.get(ScimTokenModel, token_id)
    if row is not None and row.revoked_at is None:
        row.revoked_at = datetime.now(timezone.utc)
        await session.commit()

        from velaris_shared.db.security_events import log_security_event
        await log_security_event(
            session, event_type="scim_token_revoked", severity="medium",
            user_id=user.user_id, resource_type="scim_token", resource_id=str(token_id),
            action="revoke", outcome="success", details={"name": row.name},
        )
        await session.commit()
    return Response(status_code=204)
