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
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.auth.dependencies import get_current_user, require_role
from case_service.auth.models import AuthenticatedUser
from case_service.db.models import (
    PortalModel, AccessRoleModel, AccessGroupModel,
    OperatorAccessGroupModel, UserDirectoryModel,
)
from case_service.db.session import get_session

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
    _user: AuthenticatedUser = Depends(require_role("admin")),
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
    _user: AuthenticatedUser = Depends(require_role("admin")),
):
    if body.portal_type not in _PORTAL_TYPES:
        raise HTTPException(400, f"portal_type must be one of {_PORTAL_TYPES}")
    p = PortalModel(**body.model_dump())
    session.add(p)
    await session.flush()
    return _portal_out(p)


@router.get("/portals/{portal_id}", response_model=PortalOut)
async def get_portal(
    portal_id: str,
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(require_role("admin")),
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
    _user: AuthenticatedUser = Depends(require_role("admin")),
):
    p = (await session.execute(
        select(PortalModel).where(PortalModel.id == uuid.UUID(portal_id))
    )).scalar_one_or_none()
    if p is None:
        raise HTTPException(404, "Portal not found")
    for k, v in body.model_dump().items():
        setattr(p, k, v)
    await session.flush()
    return _portal_out(p)


@router.delete("/portals/{portal_id}", status_code=204)
async def delete_portal(
    portal_id: str,
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(require_role("admin")),
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


@router.get("/access-roles/catalog")
async def get_privilege_catalog(
    _user: AuthenticatedUser = Depends(require_role("admin")),
):
    """Return all known resource types and their available actions.
    Used by the frontend privilege editor checkbox matrix.
    """
    return _PRIVILEGE_CATALOG


# ── Access Roles ──────────────────────────────────────────────────────────────

@router.get("/access-roles", response_model=list[AccessRoleOut])
async def list_access_roles(
    tenant_id: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(require_role("admin")),
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
    _user: AuthenticatedUser = Depends(require_role("admin")),
):
    r = AccessRoleModel(**body.model_dump())
    session.add(r)
    await session.flush()
    return _role_out(r)


@router.get("/access-roles/{role_id}", response_model=AccessRoleOut)
async def get_access_role(
    role_id: str,
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(require_role("admin")),
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
    _user: AuthenticatedUser = Depends(require_role("admin")),
):
    r = (await session.execute(
        select(AccessRoleModel).where(AccessRoleModel.id == uuid.UUID(role_id))
    )).scalar_one_or_none()
    if r is None:
        raise HTTPException(404, "Access role not found")
    for k, v in body.model_dump().items():
        setattr(r, k, v)
    await session.flush()
    return _role_out(r)


@router.delete("/access-roles/{role_id}", status_code=204)
async def delete_access_role(
    role_id: str,
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(require_role("admin")),
):
    r = (await session.execute(
        select(AccessRoleModel).where(AccessRoleModel.id == uuid.UUID(role_id))
    )).scalar_one_or_none()
    if r:
        await session.delete(r)
        await session.flush()
    from starlette.responses import Response
    return Response(status_code=204)


# ── Access Groups ─────────────────────────────────────────────────────────────

@router.get("/access-groups", response_model=list[AccessGroupOut])
async def list_access_groups(
    tenant_id: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(require_role("admin")),
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
    _user: AuthenticatedUser = Depends(require_role("admin")),
):
    # Verify portal exists
    portal = (await session.execute(
        select(PortalModel).where(PortalModel.id == uuid.UUID(body.portal_id))
    )).scalar_one_or_none()
    if portal is None:
        raise HTTPException(404, f"Portal {body.portal_id} not found")

    g = AccessGroupModel(
        name=body.name, description=body.description,
        tenant_id=body.tenant_id, portal_id=uuid.UUID(body.portal_id),
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
    _user: AuthenticatedUser = Depends(require_role("admin")),
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
    _user: AuthenticatedUser = Depends(require_role("admin")),
):
    g = (await session.execute(
        select(AccessGroupModel).where(AccessGroupModel.id == uuid.UUID(group_id))
    )).scalar_one_or_none()
    if g is None:
        raise HTTPException(404, "Access group not found")
    g.name = body.name
    g.description = body.description
    g.portal_id = uuid.UUID(body.portal_id)
    g.role_ids = [str(r) for r in body.role_ids]
    g.allowed_case_type_ids = body.allowed_case_type_ids
    g.allowed_queue_ids = body.allowed_queue_ids
    g.is_default = body.is_default
    g.is_active = body.is_active
    await session.flush()
    return _group_out(g)


@router.delete("/access-groups/{group_id}", status_code=204)
async def delete_access_group(
    group_id: str,
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(require_role("admin")),
):
    g = (await session.execute(
        select(AccessGroupModel).where(AccessGroupModel.id == uuid.UUID(group_id))
    )).scalar_one_or_none()
    if g:
        g.is_active = False
        await session.flush()
    from starlette.responses import Response
    return Response(status_code=204)


# ── Members ───────────────────────────────────────────────────────────────────

@router.get("/access-groups/{group_id}/members", response_model=list[MemberOut])
async def list_members(
    group_id: str,
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(require_role("admin")),
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
    current_user: AuthenticatedUser = Depends(require_role("admin")),
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
        m = OperatorAccessGroupModel(
            operator_id=body.operator_id,
            access_group_id=uuid.UUID(group_id),
            is_primary=body.is_primary,
            assigned_by=current_user.user_id,
        )
        session.add(m)

    await session.flush()
    return _member_out(m)


@router.delete("/access-groups/{group_id}/members/{operator_id}", status_code=204)
async def remove_member(
    group_id: str,
    operator_id: str,
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(require_role("admin")),
):
    m = (await session.execute(
        select(OperatorAccessGroupModel).where(
            OperatorAccessGroupModel.operator_id == operator_id,
            OperatorAccessGroupModel.access_group_id == uuid.UUID(group_id),
        )
    )).scalar_one_or_none()
    if m:
        await session.delete(m)
        await session.flush()
    from starlette.responses import Response
    return Response(status_code=204)


# ── Auth context endpoints ────────────────────────────────────────────────────

@router.get("/auth/me")
async def get_me(user: AuthenticatedUser = Depends(get_current_user)):
    """Return the current operator's full context — active group, portal, privileges.

    This is the load-bearing endpoint for P38/P39+ frontends to determine
    which portal to render and what the operator is allowed to do.
    """
    return user.to_dict()


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
    from case_service.auth.dependencies import _enrich_with_access_group
    user = await _enrich_with_access_group(user, session)
    return user.to_dict()
