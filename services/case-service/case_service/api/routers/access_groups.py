"""P37 — Access Group, Portal, and Access Role management.

Endpoints:
  Portals     GET/POST /portals, GET/PATCH/DELETE /portals/{id}
  AccessRoles GET/POST /access-roles, GET/PATCH/DELETE /access-roles/{id}
  AccessGroups
              GET/POST /access-groups
              GET/PATCH/DELETE /access-groups/{id}
              GET/POST /access-groups/{id}/members
              DELETE /access-groups/{id}/members/{operator_id}
  Auth context
              GET  /auth/me
              POST /auth/switch-context
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.hxguard import generation as _hxguard_generation
from velaris_shared.hxguard import lockout
from velaris_shared import hxguard
from velaris_shared.auth.dependencies import get_current_user, require_role
from velaris_shared.tenancy.identity import tenant_for_write
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.db.models import (
    PortalModel, AccessRoleModel, AccessGroupModel,
    OperatorAccessGroupModel, UserDirectoryModel,
)
from velaris_shared.db.session import get_session

router = APIRouter(tags=["access-groups"])

_PORTAL_TYPES = {"staff", "customer", "manager", "admin", "mobile"}


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class PortalIn(BaseModel):
    name: str
    portal_type: str = "staff"
    modules: list[str] = []
    homepage: str = "/work-center"
    theme: dict = {}
    tenant_id: Optional[str] = None
    is_active: bool = True


class PortalOut(PortalIn):
    id: str
    created_at: str
    updated_at: str


class AccessRoleIn(BaseModel):
    name: str
    description: str = ""
    privileges: list[dict] = []
    tenant_id: Optional[str] = None


class AccessRoleOut(AccessRoleIn):
    id: str
    created_at: str
    updated_at: str


class AccessGroupIn(BaseModel):
    name: str
    description: str = ""
    tenant_id: str
    portal_id: str
    role_ids: list[str] = []
    allowed_case_type_ids: list[str] = Field(default_factory=lambda: ["*"])
    allowed_queue_ids: list[str] = Field(default_factory=lambda: ["*"])
    is_default: bool = False
    is_active: bool = True


class AccessGroupOut(AccessGroupIn):
    id: str
    created_at: str
    updated_at: str


class MemberIn(BaseModel):
    operator_id: str
    is_primary: bool = False


class MemberOut(MemberIn):
    id: str
    assigned_by: Optional[str]
    assigned_at: str


class SwitchContextIn(BaseModel):
    access_group_id: str


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ts(dt: datetime | None) -> str:
    return dt.isoformat() if dt else ""


def _portal_out(p: PortalModel) -> PortalOut:
    return PortalOut(
        id=str(p.id), name=p.name, portal_type=p.portal_type,
        modules=p.modules or [], homepage=p.homepage, theme=p.theme or {},
        tenant_id=p.tenant_id, is_active=p.is_active,
        created_at=_ts(p.created_at), updated_at=_ts(p.updated_at),
    )


def _role_out(r: AccessRoleModel) -> AccessRoleOut:
    return AccessRoleOut(
        id=str(r.id), name=r.name, description=r.description,
        privileges=r.privileges or [], tenant_id=r.tenant_id,
        created_at=_ts(r.created_at), updated_at=_ts(r.updated_at),
    )


def _group_out(g: AccessGroupModel) -> AccessGroupOut:
    return AccessGroupOut(
        id=str(g.id), name=g.name, description=g.description,
        tenant_id=g.tenant_id, portal_id=str(g.portal_id),
        role_ids=[str(r) for r in (g.role_ids or [])],
        allowed_case_type_ids=g.allowed_case_type_ids or ["*"],
        allowed_queue_ids=g.allowed_queue_ids or ["*"],
        is_default=g.is_default, is_active=g.is_active,
        created_at=_ts(g.created_at), updated_at=_ts(g.updated_at),
    )


def _member_out(m: OperatorAccessGroupModel) -> MemberOut:
    return MemberOut(
        id=str(m.id), operator_id=m.operator_id, is_primary=m.is_primary,
        assigned_by=m.assigned_by, assigned_at=_ts(m.assigned_at),
    )


# ── Portals ───────────────────────────────────────────────────────────────────

@router.get("/portals", response_model=list[PortalOut])
async def list_portals(
    tenant_id: Optional[str] = Query(None),
    portal_type: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("identity.portals.read")),
):
    if _user.tenant_id and "superadmin" not in (_user.roles or []):
        tenant_id = _user.tenant_id
    stmt = select(PortalModel).where(PortalModel.is_active.is_(True))
    if tenant_id:
        stmt = stmt.where(PortalModel.tenant_id == tenant_id)
    if portal_type:
        stmt = stmt.where(PortalModel.portal_type == portal_type)
    rows = (await session.execute(stmt.order_by(PortalModel.name))).scalars().all()
    return [_portal_out(p) for p in rows]


@router.post("/portals", response_model=PortalOut, status_code=201)
async def create_portal(
    body: PortalIn,
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("identity.portals.write")),
):
    if body.portal_type not in _PORTAL_TYPES:
        raise HTTPException(400, f"portal_type must be one of {_PORTAL_TYPES}")
    # A portal's tenant was stored as sent until 2026-09-30 (NULL = every
    # tenant's). The operator keeps that choice; a tenant admin makes their own.
    try:
        body.tenant_id = await tenant_for_write(session, _user, body.tenant_id)
    except PermissionError as exc:
        raise HTTPException(403, str(exc))
    p = PortalModel(**body.model_dump())
    session.add(p)
    await session.flush()
    return _portal_out(p)


@router.get("/portals/{portal_id}", response_model=PortalOut)
async def get_portal(
    portal_id: str,
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("identity.portals.read")),
):
    p = (await session.execute(
        select(PortalModel).where(PortalModel.id == uuid.UUID(portal_id))
    )).scalar_one_or_none()
    if p is None:
        raise HTTPException(404, "Portal not found")
    return _portal_out(p)


@router.patch("/portals/{portal_id}", response_model=PortalOut)
async def update_portal(
    portal_id: str,
    body: PortalIn,
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("identity.portals.write")),
):
    p = (await session.execute(
        select(PortalModel).where(PortalModel.id == uuid.UUID(portal_id))
    )).scalar_one_or_none()
    if p is None:
        raise HTTPException(404, "Portal not found")
    try:
        body.tenant_id = await tenant_for_write(session, _user, body.tenant_id)
    except PermissionError as exc:
        raise HTTPException(403, str(exc))
    for k, v in body.model_dump().items():
        setattr(p, k, v)
    await session.flush()
    return _portal_out(p)


@router.delete("/portals/{portal_id}", status_code=204)
async def delete_portal(
    portal_id: str,
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("identity.portals.write")),
):
    p = (await session.execute(
        select(PortalModel).where(PortalModel.id == uuid.UUID(portal_id))
    )).scalar_one_or_none()
    if p:
        p.is_active = False
        await session.flush()
    from starlette.responses import Response
    return Response(status_code=204)


# ── Privilege catalog (all known resources + their actions) ───────────────────

_PRIVILEGE_CATALOG = {
    "resources": [
        {"id": "case",         "label": "Cases",           "actions": [
            {"id": "create", "label": "Create"},
            {"id": "read",   "label": "Read / View"},
            {"id": "update", "label": "Update"},
            {"id": "delete", "label": "Delete"},
            {"id": "assign", "label": "Assign"},
        ]},
        {"id": "case_type",    "label": "Case Types",      "actions": [
            {"id": "create", "label": "Create"},
            {"id": "read",   "label": "Read / View"},
            {"id": "update", "label": "Update"},
            {"id": "delete", "label": "Delete"},
        ]},
        {"id": "assignment",   "label": "Assignments",     "actions": [
            {"id": "create", "label": "Create"},
            {"id": "read",   "label": "Read"},
            {"id": "update", "label": "Update"},
            {"id": "delete", "label": "Delete"},
        ]},
        {"id": "form",         "label": "Forms",           "actions": [
            {"id": "create", "label": "Create"},
            {"id": "read",   "label": "Read"},
            {"id": "update", "label": "Update"},
            {"id": "delete", "label": "Delete"},
            {"id": "submit", "label": "Submit"},
        ]},
        {"id": "document",     "label": "Documents",       "actions": [
            {"id": "create", "label": "Upload"},
            {"id": "read",   "label": "Read / Download"},
            {"id": "update", "label": "Update"},
            {"id": "delete", "label": "Delete"},
        ]},
        {"id": "analytics",    "label": "Analytics",       "actions": [
            {"id": "read",   "label": "View"},
            {"id": "export", "label": "Export"},
        ]},
        {"id": "report",       "label": "Reports",         "actions": [
            {"id": "read",   "label": "View"},
            {"id": "export", "label": "Export"},
        ]},
        {"id": "audit",        "label": "Audit Logs",      "actions": [
            {"id": "read",   "label": "View"},
        ]},
        {"id": "user",         "label": "Users",           "actions": [
            {"id": "create",     "label": "Create"},
            {"id": "read",       "label": "View"},
            {"id": "update",     "label": "Update"},
            {"id": "deactivate", "label": "Deactivate"},
        ]},
        {"id": "access_group", "label": "Access Groups",   "actions": [
            {"id": "create", "label": "Create"},
            {"id": "read",   "label": "View"},
            {"id": "update", "label": "Update"},
            {"id": "delete", "label": "Delete"},
        ]},
        {"id": "portal",       "label": "Portals",         "actions": [
            {"id": "read",   "label": "View"},
            {"id": "manage", "label": "Manage / Edit"},
        ]},
        {"id": "enterprise",   "label": "Enterprise / GDPR", "actions": [
            {"id": "read",   "label": "View"},
            {"id": "manage", "label": "Manage (GDPR, retention)"},
        ]},
        {"id": "security",     "label": "Security Events", "actions": [
            {"id": "read",   "label": "View"},
        ]},
        {"id": "db_manager",   "label": "DB Manager",      "actions": [
            {"id": "view_sensitive", "label": "View masked account/financial data (DBView, requires re-auth)"},
        ]},
        {"id": "admin",        "label": "Admin Console",   "actions": [
            {"id": "read",   "label": "View"},
            {"id": "manage", "label": "Manage"},
        ]},
        {"id": "workflow",     "label": "Workflows",       "actions": [
            {"id": "create",  "label": "Create"},
            {"id": "read",    "label": "View"},
            {"id": "update",  "label": "Update"},
            {"id": "delete",  "label": "Delete"},
            {"id": "execute", "label": "Execute / Trigger"},
        ]},
    ]
}


def _platform_catalog_resources() -> list[dict]:
    """The platform half of the catalogue, GENERATED from the action registry.

    Hand-listing it would guarantee drift: `_PRIVILEGE_CATALOG` above lists 16
    legacy case-level resources and, before Phase 6, nothing else — so the
    privilege editor could not name a single platform action, and the one data
    path that reached them was unusable from any screen. Deriving it means a new
    action is grantable the moment it is registered, with no second list to
    remember.

    Only resources that actually cross the namespace boundary are offered. An
    action the boundary rejects would render as a checkbox that silently grants
    nothing, which is worse than its absence.
    """
    from velaris_shared.hxguard.actions import ACTIONS, resource_of, verb_of
    from velaris_shared.hxguard.privileges import _crosses_into_platform_namespace

    by_resource: dict[str, dict[str, str]] = {}
    for name, action in ACTIONS.items():
        resource, verb = resource_of(name), verb_of(name)
        if not _crosses_into_platform_namespace(
                {"resource": resource, "actions": [verb]}):
            continue
        by_resource.setdefault(resource, {})[verb] = action.summary

    return [
        {
            "id": resource,
            "label": resource.replace(".", " › ").replace("_", " "),
            "platform": True,
            "actions": [{"id": v, "label": v, "description": verbs[v]}
                        for v in sorted(verbs)],
        }
        for resource, verbs in sorted(by_resource.items())
    ]


@router.get("/access-roles/catalog")
async def get_privilege_catalog(
    _user: AuthenticatedUser = Depends(hxguard.guard("identity.roles.read")),
):
    """Return all known resource types and their available actions.

    Two halves, kept distinguishable by the `platform` flag: the legacy
    case-level vocabulary (hand-listed, and read by `has_privilege` /
    `core/access_control.py`), and the platform actions generated from the
    registry. They are different namespaces with different evaluators, and an
    editor that blurred them would let someone grant `case.read` expecting it to
    open a platform screen.
    """
    return {
        "resources": _PRIVILEGE_CATALOG["resources"] + _platform_catalog_resources(),
    }


# ── Access Roles ──────────────────────────────────────────────────────────────

@router.get("/access-roles", response_model=list[AccessRoleOut])
async def list_access_roles(
    tenant_id: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("identity.roles.read")),
):
    if _user.tenant_id and "superadmin" not in (_user.roles or []):
        tenant_id = _user.tenant_id
    stmt = select(AccessRoleModel)
    if tenant_id:
        stmt = stmt.where(
            (AccessRoleModel.tenant_id == tenant_id) | (AccessRoleModel.tenant_id.is_(None))
        )
    rows = (await session.execute(stmt.order_by(AccessRoleModel.name))).scalars().all()
    return [_role_out(r) for r in rows]


@router.post("/access-roles", response_model=AccessRoleOut, status_code=201)
async def create_access_role(
    body: AccessRoleIn,
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("identity.roles.write")),
):
    await _role_tenant(session, _user, body)
    r = AccessRoleModel(**body.model_dump())
    session.add(r)
    await _flush_role(session, body.name)
    return _role_out(r)


async def _role_tenant(session: AsyncSession, user: AuthenticatedUser, body: AccessRoleIn) -> None:
    """A role's tenant was stored as sent (2026-10-30): a tenant admin who sent
    none made a built-in role — in every tenant's list — and a PATCH could move
    their role there. The operator keeps that choice; a tenant admin writes
    into their own tenant, as for portals and groups."""
    try:
        body.tenant_id = await tenant_for_write(session, user, body.tenant_id)
    except PermissionError as exc:
        raise HTTPException(403, str(exc))


async def _flush_role(session: AsyncSession, name: str) -> None:
    """A role name is unique per tenant; a duplicate answered 500 (R6, 2026-10-30)."""
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, f"An access role named '{name}' already exists")


@router.get("/access-roles/{role_id}", response_model=AccessRoleOut)
async def get_access_role(
    role_id: str,
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("identity.roles.read")),
):
    r = (await session.execute(
        select(AccessRoleModel).where(AccessRoleModel.id == uuid.UUID(role_id))
    )).scalar_one_or_none()
    if r is None:
        raise HTTPException(404, "Access role not found")
    return _role_out(r)


@router.patch("/access-roles/{role_id}", response_model=AccessRoleOut)
async def update_access_role(
    role_id: str,
    body: AccessRoleIn,
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("identity.roles.write")),
):
    r = (await session.execute(
        select(AccessRoleModel).where(AccessRoleModel.id == uuid.UUID(role_id))
    )).scalar_one_or_none()
    if r is None:
        raise HTTPException(404, "Access role not found")
    # Rewriting `privileges` can strip the bootstrap action from a role that
    # every access group carries — the same lockout as deleting the role, by a
    # quieter route. Checked before the write, like every other holder check.
    await _role_tenant(session, _user, body)
    fields = body.model_dump()
    await lockout.assert_role_edit_survivable(
        session, r, fields.get("privileges"), fields.get("name"),
        context=f"updating access role {role_id}")
    for k, v in fields.items():
        setattr(r, k, v)
    await _flush_role(session, body.name)
    return _role_out(r)


@router.delete("/access-roles/{role_id}", status_code=204)
async def delete_access_role(
    role_id: str,
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("identity.roles.write")),
):
    r = (await session.execute(
        select(AccessRoleModel).where(AccessRoleModel.id == uuid.UUID(role_id))
    )).scalar_one_or_none()
    if r:
        await lockout.assert_role_delete_survivable(
            session, r, context=f"deleting access role {role_id}")
        await session.delete(r)
        await session.flush()
    from starlette.responses import Response
    return Response(status_code=204)


# ── Access Groups ─────────────────────────────────────────────────────────────

@router.get("/access-groups", response_model=list[AccessGroupOut])
async def list_access_groups(
    tenant_id: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("identity.groups.read")),
):
    if _user.tenant_id and "superadmin" not in (_user.roles or []):
        tenant_id = _user.tenant_id
    stmt = select(AccessGroupModel).where(AccessGroupModel.is_active.is_(True))
    if tenant_id:
        stmt = stmt.where(AccessGroupModel.tenant_id == tenant_id)
    rows = (await session.execute(stmt.order_by(AccessGroupModel.name))).scalars().all()
    return [_group_out(g) for g in rows]


@router.post("/access-groups", response_model=AccessGroupOut, status_code=201)
async def create_access_group(
    body: AccessGroupIn,
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("identity.groups.write")),
):
    # Verify portal exists
    portal = (await session.execute(
        select(PortalModel).where(PortalModel.id == uuid.UUID(body.portal_id))
    )).scalar_one_or_none()
    if portal is None:
        raise HTTPException(404, f"Portal {body.portal_id} not found")
    # The group's privileges apply in the tenant it names — stored as sent
    # until 2026-09-30, so a tenant admin could mint a group in another tenant.
    try:
        tenant_id = await tenant_for_write(session, _user, body.tenant_id)
    except PermissionError as exc:
        raise HTTPException(403, str(exc))

    g = AccessGroupModel(
        name=body.name, description=body.description,
        tenant_id=tenant_id, portal_id=uuid.UUID(body.portal_id),
        role_ids=[str(r) for r in body.role_ids],
        allowed_case_type_ids=body.allowed_case_type_ids,
        allowed_queue_ids=body.allowed_queue_ids,
        is_default=body.is_default, is_active=body.is_active,
    )
    session.add(g)
    await session.flush()
    return _group_out(g)


@router.get("/access-groups/{group_id}", response_model=AccessGroupOut)
async def get_access_group(
    group_id: str,
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("identity.groups.read")),
):
    g = (await session.execute(
        select(AccessGroupModel).where(AccessGroupModel.id == uuid.UUID(group_id))
    )).scalar_one_or_none()
    if g is None:
        raise HTTPException(404, "Access group not found")
    return _group_out(g)


@router.patch("/access-groups/{group_id}", response_model=AccessGroupOut)
async def update_access_group(
    group_id: str,
    body: AccessGroupIn,
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("identity.groups.write")),
):
    g = (await session.execute(
        select(AccessGroupModel).where(AccessGroupModel.id == uuid.UUID(group_id))
    )).scalar_one_or_none()
    if g is None:
        raise HTTPException(404, "Access group not found")
    # PATCH sets is_active and rewrites role_ids, so it can revoke exactly what
    # DELETE revokes — the guarded door and the unguarded one led to the same
    # room until this call existed.
    await lockout.assert_group_edit_survivable(
        session, g, body.role_ids, body.is_active,
        context=f"updating access group {group_id}")
    g.name = body.name
    g.description = body.description
    g.portal_id = uuid.UUID(body.portal_id)
    g.role_ids = [str(r) for r in body.role_ids]
    g.allowed_case_type_ids = body.allowed_case_type_ids
    g.allowed_queue_ids = body.allowed_queue_ids
    g.is_default = body.is_default
    g.is_active = body.is_active
    await session.flush()
    # The evaluator reads access-group membership and the group's own
    # allowed_case_type_ids directly (hxguard/service.py), and caches the
    # answer for 30s in EVERY service. Without these two lines a revocation
    # kept working for half a minute platform-wide — the same defect #16-C
    # closed for role privileges, on a different input.
    hxguard.invalidate_cache()   # this process
    await _hxguard_generation.bump(
        session, reason=f"access_group.update:{group_id}", actor=_user.user_id)
    return _group_out(g)


@router.delete("/access-groups/{group_id}", status_code=204)
async def delete_access_group(
    group_id: str,
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("identity.groups.write")),
):
    g = (await session.execute(
        select(AccessGroupModel).where(AccessGroupModel.id == uuid.UUID(group_id))
    )).scalar_one_or_none()
    if g:
        members = (await session.execute(
            select(OperatorAccessGroupModel.operator_id).where(
                OperatorAccessGroupModel.access_group_id == g.id)
        )).scalars().all()
        # Deactivating a group revokes it for every member at once, which is the
        # fastest way to lock the platform out of its own access administration.
        await lockout.assert_not_last_holder(
            session, removing_user_ids=[str(m) for m in members],
            context=f"deactivating access group {group_id}")
        g.is_active = False
        await session.flush()
        # The evaluator reads access-group membership and the group's own
        # allowed_case_type_ids directly (hxguard/service.py), and caches the
        # answer for 30s in EVERY service. Without these two lines a revocation
        # kept working for half a minute platform-wide — the same defect #16-C
        # closed for role privileges, on a different input.
        hxguard.invalidate_cache()   # this process
        await _hxguard_generation.bump(
            session, reason=f"access_group.delete:{group_id}", actor=_user.user_id)
    from starlette.responses import Response
    return Response(status_code=204)


# ── Members ───────────────────────────────────────────────────────────────────

@router.get("/access-groups/{group_id}/members", response_model=list[MemberOut])
async def list_members(
    group_id: str,
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("identity.groups.read")),
):
    rows = (await session.execute(
        select(OperatorAccessGroupModel).where(
            OperatorAccessGroupModel.access_group_id == uuid.UUID(group_id)
        )
    )).scalars().all()
    return [_member_out(m) for m in rows]


@router.post("/access-groups/{group_id}/members", response_model=MemberOut, status_code=201)
async def add_member(
    group_id: str,
    body: MemberIn,
    session: AsyncSession = Depends(get_session),
    current_user: AuthenticatedUser = Depends(hxguard.guard("identity.groups.write")),
):
    # Check group exists
    g = (await session.execute(
        select(AccessGroupModel).where(AccessGroupModel.id == uuid.UUID(group_id))
    )).scalar_one_or_none()
    if g is None:
        raise HTTPException(404, "Access group not found")

    # Upsert membership
    existing = (await session.execute(
        select(OperatorAccessGroupModel).where(
            OperatorAccessGroupModel.operator_id == body.operator_id,
            OperatorAccessGroupModel.access_group_id == uuid.UUID(group_id),
        )
    )).scalar_one_or_none()

    if existing:
        existing.is_primary = body.is_primary
        m = existing
    else:
        # A group of tenant A takes only tenant A's people (2026-11-09). Joining
        # a group never moved anyone: sign-in takes the tenant from the user's
        # directory row, so a default-tenant `admin` put in a tenant's group
        # stayed the install's operator and saw every tenant. The operator too
        # is refused — the mismatch is the defect, whoever makes it. Existing
        # memberships are left as they are.
        if g.tenant_id:
            from velaris_shared.tenancy.home import home_tenant
            from velaris_shared.tenancy.identity import same_tenant
            home = await home_tenant(session, body.operator_id)
            if home is None:
                # Nobody by this id yet. Only the operator may record a
                # membership ahead of the person (checked when their account is
                # made); a tenant's admin could otherwise pre-seed an id that is
                # later created in ANOTHER tenant.
                from velaris_shared.tenancy.identity import is_platform_operator
                if not await is_platform_operator(session, current_user):
                    raise HTTPException(422, f"No user '{body.operator_id}' in your tenant.")
            elif not await same_tenant(session, g.tenant_id, home):
                raise HTTPException(422, (
                    f"{body.operator_id} is in tenant '{home}', and this access group "
                    "belongs to another tenant. Set the user's tenant in User "
                    "Directory first."))
        m = OperatorAccessGroupModel(
            operator_id=body.operator_id,
            access_group_id=uuid.UUID(group_id),
            is_primary=body.is_primary,
            assigned_by=current_user.user_id,
        )
        session.add(m)

    await session.flush()
    # The evaluator reads access-group membership and the group's own
    # allowed_case_type_ids directly (hxguard/service.py), and caches the
    # answer for 30s in EVERY service. Without these two lines a revocation
    # kept working for half a minute platform-wide — the same defect #16-C
    # closed for role privileges, on a different input.
    hxguard.invalidate_cache()   # this process
    await _hxguard_generation.bump(
        session, reason=f"access_group.member.add:{group_id}",
        actor=current_user.user_id)
    return _member_out(m)


@router.delete("/access-groups/{group_id}/members/{operator_id}", status_code=204)
async def remove_member(
    group_id: str,
    operator_id: str,
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("identity.groups.write")),
):
    m = (await session.execute(
        select(OperatorAccessGroupModel).where(
            OperatorAccessGroupModel.operator_id == operator_id,
            OperatorAccessGroupModel.access_group_id == uuid.UUID(group_id),
        )
    )).scalar_one_or_none()
    if m is None:
        # The membership may name the same person by account id or email (SSO
        # provisioning does); User Directory removes by username (2026-11-09).
        from velaris_shared.tenancy.home import account_identifiers
        m = (await session.execute(
            select(OperatorAccessGroupModel).where(
                OperatorAccessGroupModel.operator_id.in_(
                    await account_identifiers(session, operator_id)),
                OperatorAccessGroupModel.access_group_id == uuid.UUID(group_id),
            )
        )).scalars().first()
        if m is not None:
            operator_id = m.operator_id
    if m:
        # Checked BEFORE the delete: afterwards there may be nobody left to
        # authorise putting it back (§6.1 of the privilege-authz design).
        await lockout.assert_not_last_holder(
            session, removing_user_ids=[operator_id],
            context=f"removing {operator_id} from access group {group_id}")
        await session.delete(m)
        await session.flush()
        # The evaluator reads access-group membership and the group's own
        # allowed_case_type_ids directly (hxguard/service.py), and caches the
        # answer for 30s in EVERY service. Without these two lines a revocation
        # kept working for half a minute platform-wide — the same defect #16-C
        # closed for role privileges, on a different input.
        hxguard.invalidate_cache()   # this process
        await _hxguard_generation.bump(
            session, reason=f"access_group.member.remove:{group_id}",
            actor=_user.user_id)
    from starlette.responses import Response
    return Response(status_code=204)


# ── Auth context endpoints ────────────────────────────────────────────────────

# `GET /auth/me` was defined here as well, but `api/routers/auth.py` was always
# registered first and answered it, so this copy was never reached. It went with
# the sign-in split (R5e step 10): `/auth/me` is user-service's.


@router.post("/auth/switch-context")
async def switch_context(
    body: SwitchContextIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Switch the operator's active access group.

    Persists the choice in user_directory.current_access_group_id so it
    survives across sessions. Returns the updated /auth/me payload.
    """
    target_id = uuid.UUID(body.access_group_id)

    # Verify the operator actually belongs to this group
    membership = (await session.execute(
        select(OperatorAccessGroupModel).where(
            OperatorAccessGroupModel.operator_id == user.user_id,
            OperatorAccessGroupModel.access_group_id == target_id,
        )
    )).scalar_one_or_none()
    if membership is None:
        raise HTTPException(403, "You are not a member of this access group")

    # Persist the switch
    dir_row = (await session.execute(
        select(UserDirectoryModel).where(UserDirectoryModel.user_id == user.user_id)
    )).scalar_one_or_none()
    if dir_row:
        dir_row.current_access_group_id = target_id
        await session.flush()

    # Re-enrich and return
    from velaris_shared.auth.dependencies import _enrich_with_access_group
    user = await _enrich_with_access_group(user, session)
    return user.to_dict()


# ── Phase 6: role → privilege, in DATA ────────────────────────────────────────
#
# Until this existed, a platform role's privileges came only from
# `SEEDED_ROLE_PRIVILEGES`, derived in code from each action's `legacy_roles` —
# the parity oracle, not a policy knob. Changing who may do what meant editing
# the record of what `require_role` used to answer, and a deploy.
#
# Entries here are ADDED to the seed. See `hxguard/role_store.py` for why that,
# and not replacement, is the only safe shape for a cache-served authorization
# source: an empty cache must mean "as before", never "nobody holds anything".


class RolePrivilegesBody(BaseModel):
    #: Bounded for the same reason the HxSentinel rule params are: this column
    #: holds a handful of grants, and an admin — trusted or not — should not be
    #: able to park an arbitrary blob in a table read on every request.
    privileges: list[dict] = Field(default_factory=list, max_length=200)


def _role_privilege_view(role: str) -> dict:
    from velaris_shared.hxguard import role_store
    from velaris_shared.hxguard.privileges import (
        SEEDED_ROLE_PRIVILEGES, effective_privileges,
    )
    return {
        "role": role,
        # Split deliberately three ways. "What did I grant" and "what does this
        # role actually hold" are different questions, and an admin who cannot
        # see the seed will re-grant something they already had — or try to
        # remove it by deleting a row that was never there.
        "seeded": list(SEEDED_ROLE_PRIVILEGES.get(role, [])),
        "stored": role_store.privileges_for(role),
        "effective": effective_privileges([role]),
    }


@router.get("/role-privileges")
async def list_role_privileges(
    _user: AuthenticatedUser = Depends(hxguard.guard("identity.roles.read")),
):
    """Every platform role, with its seeded, stored and effective privileges."""
    from velaris_shared.hxguard.privileges import KNOWN_ROLES
    return {"roles": [_role_privilege_view(r) for r in KNOWN_ROLES]}


@router.put("/role-privileges/{role}")
async def put_role_privileges(
    role: str,
    body: RolePrivilegesBody,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("identity.roles.write")),
):
    """Replace one role's STORED entries (the seed is untouched).

    Guarded against lockout the same way group and role edits are, and for a
    sharper reason: a `deny` entry here revokes for **every holder of the role at
    once**, which is the fastest route to nobody holding `identity.groups.write`
    that the platform has.
    """
    from velaris_shared.db.security_events import log_security_event
    from velaris_shared.hxguard import role_store
    from velaris_shared.hxguard.privileges import KNOWN_ROLES

    if role not in KNOWN_ROLES:
        raise HTTPException(404, f"Unknown platform role {role!r}")

    try:
        role_store._valid_or_raise(body.privileges)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    before = role_store.privileges_for(role)

    # Compares holders BEFORE against holders AFTER. Staging the change and then
    # calling `lockout.assert_not_last_holder` looks equivalent and is not — see
    # that function's docstring for how it defeats the guard.
    await role_store.assert_write_keeps_bootstrap_holders(
        session, role=role, privileges=list(body.privileges),
        context=f"editing privileges of role '{role}'")

    stored = await role_store.set_role_privileges(
        session, role=role, privileges=list(body.privileges),
        updated_by=user.user_id)
    await log_security_event(
        session, event_type="role_privileges_changed", severity="warning",
        user_id=user.user_id, resource_type="role", resource_id=role,
        action="identity.roles.write",
        details={"before": before, "after": stored},
    )
    # The generation bump rides the same transaction as the privilege change:
    # committed together, or neither. Without it the other services would keep
    # answering from their 30s decision cache and, worse, would not reload the
    # role store until their next 60s cron tick — an admin's grant appearing to
    # work on some routes and not others, which is exactly what the #16-C audit
    # found and is the reason this row exists.
    await _hxguard_generation.bump(
        session, reason=f"role.privileges:{role}", actor=user.user_id)
    await session.commit()

    # Refresh from committed state so this worker answers with the new grants
    # immediately; the generation carries it to the others.
    await role_store.refresh(session)
    hxguard.invalidate_cache()
    return _role_privilege_view(role)
