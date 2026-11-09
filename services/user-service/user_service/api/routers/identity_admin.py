"""Identity administration — group mappings and the provisioning log.

The two screens an administrator needs once SAML or SCIM is live:

* **Group mappings.** An IdP group grants nothing until somebody here maps it
  onto an access group. This is therefore the escalation path: whoever can write
  a mapping can hand a directory group the authorization behind it, which is why
  it is a separate privilege from reading and why every write is a security
  event.
* **The provisioning log.** Answers "why is this person in this group?" and, the
  question that actually gets asked, "the IdP says it sent twelve groups and
  only nine took — which three?".

Mounted at ``/api/v1/identity/provisioning`` rather than ``/api/v1/identity``,
which the KYC webhooks already use.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import String, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared import hxguard
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.db.models import (
    AccessGroupModel,
    HelixUserModel,
    IdentityGroupMappingModel,
    IdentityProvisionLogModel,
)
from velaris_shared.db.session import get_session
from user_service.identity import provisioning
from user_service.identity.provisioning import SOURCES
from velaris_shared.tenancy.identity import resolve_tenant
from velaris_shared.db.read_routing import get_read_session

router = APIRouter(prefix="/identity/provisioning", tags=["identity-admin"])


class GroupMappingIn(BaseModel):
    tenant: str = Field(..., description="Tenant slug or UUID")
    source: str = Field(..., description="saml_jit | scim | oidc | bulk_sync")
    external_group: str = Field(..., max_length=255)
    access_group_id: uuid.UUID
    priority: int = 100


def _mapping_dict(row: IdentityGroupMappingModel, group_name: str | None) -> dict:
    return {
        "id": str(row.id),
        "tenant_id": str(row.tenant_id),
        "source": row.source,
        "external_group": row.external_group,
        "access_group_id": str(row.access_group_id),
        "access_group_name": group_name,
        "priority": row.priority,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.get("/group-mappings")
async def list_group_mappings(
    tenant: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("identity.provisioning.read")),
):
    stmt = select(IdentityGroupMappingModel)
    if tenant:
        resolved = await resolve_tenant(session, tenant)
        if resolved is None:
            # An unresolvable tenant filters to nothing rather than to
            # everything: "deny" is the only safe reading of an unknown tenant.
            return {"mappings": []}
        stmt = stmt.where(IdentityGroupMappingModel.tenant_id == resolved[0])
    if source:
        stmt = stmt.where(IdentityGroupMappingModel.source == source)
    # Every tenant's rows until 2026-09-29 (8d).
    from velaris_shared.tenancy.identity import scope_to_caller
    stmt = await scope_to_caller(stmt, IdentityGroupMappingModel, session, _user)
    rows = (await session.execute(
        stmt.order_by(IdentityGroupMappingModel.external_group)
    )).scalars().all()

    names = dict((await session.execute(
        select(AccessGroupModel.id, AccessGroupModel.name)
    )).all())
    return {"mappings": [
        _mapping_dict(r, names.get(r.access_group_id)) for r in rows
    ]}


@router.post("/group-mappings", status_code=201)
async def create_group_mapping(
    body: GroupMappingIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("identity.provisioning.write")),
):
    """Map an IdP group onto an access group.

    The tenant is **resolved to its UUID** before storage. The platform keeps a
    slug in some tenant columns and a UUID in others, and a mapping keyed on the
    ambiguous form would resolve differently depending on how it was written.
    """
    if body.source not in SOURCES:
        raise HTTPException(400, f"source must be one of: {', '.join(SOURCES)}")

    resolved = await resolve_tenant(session, body.tenant)
    if resolved is None:
        raise HTTPException(404, "Unknown tenant.")

    group = await session.get(AccessGroupModel, body.access_group_id)
    if group is None:
        raise HTTPException(404, "Unknown access group.")
    # 2026-11-04: the tenant and the group came from the body unchecked — a
    # tenant admin mapped an IdP group onto another tenant's (or the system
    # Administrators) group. Both must be the caller's; the operator reaches all.
    from velaris_shared.tenancy.identity import is_platform_operator, same_tenant
    if not await is_platform_operator(session, user):
        mine = getattr(user, "tenant_id", None)
        if not (await same_tenant(session, str(resolved[0]), mine)
                and await same_tenant(session, group.tenant_id, mine)):
            raise HTTPException(404, "Unknown tenant or access group.")

    existing = (await session.execute(
        select(IdentityGroupMappingModel).where(
            IdentityGroupMappingModel.tenant_id == resolved[0],
            IdentityGroupMappingModel.source == body.source,
            IdentityGroupMappingModel.external_group == body.external_group,
        )
    )).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            409, f"'{body.external_group}' is already mapped for this tenant and source.")

    row = IdentityGroupMappingModel(
        tenant_id=resolved[0],
        source=body.source,
        external_group=body.external_group,
        access_group_id=body.access_group_id,
        priority=body.priority,
        created_by=user.username,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)

    from velaris_shared.db.security_events import log_security_event
    await log_security_event(
        session, event_type="identity_group_mapping_created", severity="high",
        user_id=user.user_id, resource_type="identity_group_mapping",
        resource_id=str(row.id), action="create", outcome="success",
        details={"source": row.source, "external_group": row.external_group,
                 "access_group": group.name, "tenant": resolved[1]},
    )
    await session.commit()
    return _mapping_dict(row, group.name)


@router.delete("/group-mappings/{mapping_id}", status_code=204)
async def delete_group_mapping(
    mapping_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("identity.provisioning.write")),
):
    """Remove a mapping.

    Existing memberships are left in place. Deleting a mapping says "stop
    granting this", not "revoke what was granted" — and once it is gone the
    group is no longer *governed*, so the next sync will not strip it either. An
    administrator who wants the membership gone removes it in Access Groups,
    where the effect is visible.
    """
    row = await session.get(IdentityGroupMappingModel, mapping_id)
    if row is not None:
        from velaris_shared.tenancy.identity import is_platform_operator, same_tenant
        if not (await is_platform_operator(session, user)
                or await same_tenant(session, str(row.tenant_id), getattr(user, "tenant_id", None))):
            return          # another tenant's: answered as if absent (204), nothing removed
        await session.delete(row)
        await session.commit()

        from velaris_shared.db.security_events import log_security_event
        await log_security_event(
            session, event_type="identity_group_mapping_deleted", severity="medium",
            user_id=user.user_id, resource_type="identity_group_mapping",
            resource_id=str(mapping_id), action="delete", outcome="success",
            details={"source": row.source, "external_group": row.external_group},
        )
        await session.commit()
    return Response(status_code=204)


# ── account ↔ IdP identity links ──────────────────────────────────────────

class AccountLinkIn(BaseModel):
    identifier: str = Field(..., max_length=255,
                            description="Account id, username or email address")
    external_id: Optional[str] = Field(
        None, max_length=255,
        description="SAML/SCIM: the NameID / externalId the IdP asserts")
    #: OIDC keys on `(sso_provider, sso_subject)` and never reads `external_id`,
    #: so a customer using OIDC needs this pair or the link does nothing for them.
    sso_provider: Optional[str] = Field(None, max_length=30)
    sso_subject: Optional[str] = Field(None, max_length=255)


class AccountUnlinkIn(BaseModel):
    identifier: str = Field(..., max_length=255)


def _link_dict(user: HelixUserModel) -> dict:
    return {
        "user_id": str(user.id),
        "username": user.username,
        "email": user.email,
        "display_name": user.display_name,
        "external_id": user.external_id,
        "sso_provider": user.sso_provider,
        "sso_subject": user.sso_subject,
        "is_active": user.is_active,
        # Not the hash — only whether one exists. An account that can still sign
        # in with a password is the one worth thinking twice about linking.
        "has_password": bool(user.password_hash),
    }


@router.get("/links")
async def list_account_links(
    q: Optional[str] = Query(None, description="Match username, email or external id"),
    linked_only: bool = Query(True),
    limit: int = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("identity.provisioning.read")),
):
    """Which accounts are bound to an IdP identity — and, with `q`, which is not.

    The search is what makes the screen usable: an administrator arrives holding
    an email address out of a support ticket, not a user id.
    """
    stmt = select(HelixUserModel).where(HelixUserModel.is_superadmin.is_(False))
    if linked_only:
        # Either binding counts as linked: a customer on OIDC has no
        # `external_id` at all, and a screen that hid those accounts would say
        # "not linked" about people who sign in through the IdP every morning.
        stmt = stmt.where(or_(
            HelixUserModel.external_id.is_not(None),
            HelixUserModel.sso_subject.is_not(None),
        ))
    if q:
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(or_(
            func.lower(HelixUserModel.username).like(like),
            func.lower(HelixUserModel.email).like(like),
            func.lower(HelixUserModel.external_id).like(like),
            func.lower(HelixUserModel.sso_subject).like(like),
        ))
    # 2026-11-04: every tenant's accounts and emails went to every tenant's
    # admin (found by the demo-tenant sweep). The caller's tenant only — its
    # directory rows; the platform operator keeps the full list.
    from velaris_shared.tenancy.identity import caller_tenant_tokens, is_platform_operator
    if not await is_platform_operator(session, _user):
        from velaris_shared.db.models import UserDirectoryModel
        tokens = await caller_tenant_tokens(session, _user) or []
        mine = select(UserDirectoryModel.user_id).where(UserDirectoryModel.tenant_id.in_(tokens))
        stmt = stmt.where(func.cast(HelixUserModel.id, String).in_(mine))
    rows = (await session.execute(
        stmt.order_by(HelixUserModel.username).limit(limit)
    )).scalars().all()
    return {"links": [_link_dict(r) for r in rows]}


@router.post("/links")
async def link_account(
    body: AccountLinkIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("identity.provisioning.write")),
):
    """Bind an existing account to the identity its IdP asserts.

    A SAML login refuses to adopt an account it matches only by email — an
    assertion naming an address is not proof of owning the account that holds
    it. This is how that link gets made instead: deliberately, by somebody who
    already holds the escalation privilege in this feature, and on the record.
    """
    # 2026-11-04: no tenant check — a tenant admin (a website demo account
    # among them) bound ANOTHER tenant's account, the platform operator's
    # included, to an IdP identity they control, then signed in as it. The
    # account must be in the caller's tenant; the operator reaches all.
    _target = await provisioning.resolve_account(session, body.identifier)
    if _target is not None:
        from user_service.api.routers.auth_real import _target_in_reach
        await _target_in_reach(session, user, _target)
    result = await provisioning.link_identity(
        session, identifier=body.identifier, external_id=body.external_id,
        sso_provider=body.sso_provider, sso_subject=body.sso_subject,
        actor=user.username,
    )
    await session.commit()          # keep the provisioning-log row either way

    if not result.ok:
        if result.reason == "not_found":
            raise HTTPException(404, "No account matches that id, username or email.")
        if result.reason == "superadmin_protected":
            raise HTTPException(403, "That account cannot be linked to a directory.")
        if result.reason == "nothing_to_link":
            raise HTTPException(
                400, "Supply an external id (SAML/SCIM) or both sso_provider and "
                     "sso_subject (OIDC).")
        if result.reason in {"external_id_taken", "sso_identity_taken"}:
            raise HTTPException(
                409, f"That identity already belongs to "
                     f"'{result.detail.get('held_by')}'.")
        raise HTTPException(400, "The account could not be linked.")

    from velaris_shared.db.security_events import log_security_event
    await log_security_event(
        session, event_type="identity_account_linked", severity="high",
        user_id=user.user_id, resource_type="velaris_user",
        resource_id=result.user_id, action="link", outcome="success",
        details={"username": result.username, "external_id": result.external_id,
                 "sso_provider": result.detail.get("sso_provider"),
                 "sso_subject": result.detail.get("sso_subject"),
                 "previous": result.detail.get("previous")},
    )
    await session.commit()

    account = await provisioning.resolve_account(session, result.username)
    return _link_dict(account)


@router.post("/links/unlink")
async def unlink_account(
    body: AccountUnlinkIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("identity.provisioning.write")),
):
    """Drop every binding. The account stays; only the IdP's claim on it goes.

    Separate from the link endpoint rather than an empty `external_id`: for
    something that decides who may authenticate as an account, "I meant to
    remove it" should be impossible to express by accident.
    """
    # 2026-11-04: no tenant check — a tenant admin (a website demo account
    # among them) bound ANOTHER tenant's account, the platform operator's
    # included, to an IdP identity they control, then signed in as it. The
    # account must be in the caller's tenant; the operator reaches all.
    _target = await provisioning.resolve_account(session, body.identifier)
    if _target is not None:
        from user_service.api.routers.auth_real import _target_in_reach
        await _target_in_reach(session, user, _target)
    result = await provisioning.unlink_identity(
        session, identifier=body.identifier, actor=user.username,
    )
    await session.commit()

    if not result.ok:
        if result.reason == "not_found":
            raise HTTPException(404, "No account matches that id, username or email.")
        if result.reason == "superadmin_protected":
            raise HTTPException(403, "That account cannot be linked to a directory.")
        raise HTTPException(400, "The account could not be unlinked.")

    from velaris_shared.db.security_events import log_security_event
    await log_security_event(
        session, event_type="identity_account_unlinked", severity="medium",
        user_id=user.user_id, resource_type="velaris_user",
        resource_id=result.user_id, action="unlink", outcome="success",
        details={"username": result.username,
                 "previous": result.detail.get("previous")},
    )
    await session.commit()

    account = await provisioning.resolve_account(session, result.username)
    return _link_dict(account)


@router.get("/log")
async def list_provision_log(
    tenant: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    username: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_read_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("identity.provisioning.read")),
):
    stmt = select(IdentityProvisionLogModel)
    if tenant:
        stmt = stmt.where(IdentityProvisionLogModel.tenant_id == tenant)
    if source:
        stmt = stmt.where(IdentityProvisionLogModel.source == source)
    if username:
        stmt = stmt.where(IdentityProvisionLogModel.username == username)
    if action:
        stmt = stmt.where(IdentityProvisionLogModel.action == action)
    # Every tenant's rows until 2026-09-29 (8d).
    from velaris_shared.tenancy.identity import scope_to_caller
    stmt = await scope_to_caller(stmt, IdentityProvisionLogModel, session, _user)
    rows = (await session.execute(
        stmt.order_by(desc(IdentityProvisionLogModel.created_at)).limit(limit)
    )).scalars().all()
    return {"entries": [{
        "id": str(r.id),
        "tenant_id": r.tenant_id,
        "source": r.source,
        "external_id": r.external_id,
        "username": r.username,
        "action": r.action,
        "result": r.result,
        "detail": r.detail or {},
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in rows]}


@router.get("/unmapped-groups")
async def list_unmapped_groups(
    limit: int = Query(200, ge=1, le=1000),
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("identity.provisioning.read")),
):
    """Groups the IdP has asserted that map to nothing here.

    This is the screen that turns "SSO works but nobody can see anything" into a
    five-second diagnosis. The names come from the provisioning log, so they are
    what the IdP actually sent — not what somebody thinks it sends.
    """
    rows = (await session.execute(
        select(IdentityProvisionLogModel)
        .order_by(desc(IdentityProvisionLogModel.created_at))
        .limit(limit)
    )).scalars().all()

    seen: dict[tuple[str, str], int] = {}
    for row in rows:
        for name in (row.detail or {}).get("unmapped_groups") or []:
            key = (row.source, str(name))
            seen[key] = seen.get(key, 0) + 1
    return {"unmapped": [
        {"source": source, "external_group": name, "seen": count}
        for (source, name), count in sorted(seen.items(), key=lambda kv: -kv[1])
    ]}
