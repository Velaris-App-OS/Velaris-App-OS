"""Route permission matrix — GET/PUT /admin/permissions

GET  /admin/permissions  — any authenticated user (needed at app load)
PUT  /admin/permissions  — admin only
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.auth.dependencies import get_current_user, require_role
from case_service.auth.models import AuthenticatedUser
from case_service.db.models import SystemConfigModel
from case_service.db.session import get_session

router = APIRouter(tags=["permissions"])

_KEY = "route_permissions"

# Mirrors the NAV_ITEMS roles in AppLayout.tsx — used when DB row is absent
_DEFAULTS: dict[str, list[str]] = {
    "/sitemap":        ["admin", "manager", "designer", "developer"],
    "/analytics":      ["case_worker", "admin"],
    "/hxanalytics":    ["case_worker", "admin"],
    "/documents":      ["case_worker", "admin"],
    "/inbox":          ["case_worker", "admin"],
    "/case-designer":  ["designer", "admin"],
    "/form-builder":   ["designer", "admin"],
    "/nlp-builder":    ["designer", "admin"],
    "/modeler":        ["designer", "admin"],
    "/app-builder":    ["designer", "admin"],
    "/hxwork":         ["designer", "admin"],
    "/hxbranch":       ["designer", "admin"],
    "/graph":          ["designer", "admin"],
    "/process-mining": ["designer", "admin"],
    "/live-activity":  ["designer", "admin"],
    "/monitor":        ["designer", "admin"],
    "/deploy":         ["devops", "admin"],
    "/hxmigrate":      ["devops", "admin"],
    "/scout":          ["devops", "admin"],
    "/scout-ai":       ["devops", "admin"],
    "/orchestrator":   ["devops", "admin"],
    "/hxconnect":      ["integration", "admin"],
    "/hxbridge":       ["integration", "admin"],
    "/devconn":        ["integration", "admin", "designer"],
    "/hxsync":         ["integration", "admin"],
    "/hxfusion":       ["integration", "admin", "designer"],
    "/hxshield":       ["security", "admin"],
    "/hxstream":       ["security", "admin", "designer"],
    "/hxlogs":         ["security", "admin", "designer"],
    "/compliance":     ["security", "admin"],
    "/observability":  ["security", "admin"],
    "/portal-admin":   ["admin"],
    "/access-directory": ["admin"],
    "/access-groups":    ["admin"],
    "/user-directory": ["admin"],
    "/admin":          ["admin"],
    "/tenants":        ["admin"],
    "/enterprise":     ["admin"],
    "/email-admin":    ["admin"],
    "/push-admin":     ["admin"],
    "/hxglobal":       ["admin"],
    "/escalation":     ["admin", "designer"],
}


class PermissionsMap(BaseModel):
    permissions: dict[str, list[str]]


@router.get("/admin/permissions", response_model=PermissionsMap)
async def get_permissions(
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(get_current_user),
):
    setting = await session.get(SystemConfigModel, _KEY)
    if setting is None or setting.value is None:
        return PermissionsMap(permissions=_DEFAULTS)
    return PermissionsMap(permissions=setting.value)


@router.put("/admin/permissions", response_model=PermissionsMap)
async def update_permissions(
    body: PermissionsMap,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(require_role("admin")),
):
    # Get-or-create via the ORM (dialect-portable; PortableJSON serializes the
    # dict per-dialect, updated_at via the model's default/onupdate).
    setting = await session.get(SystemConfigModel, _KEY)
    if setting is None:
        session.add(SystemConfigModel(
            key=_KEY, value=body.permissions, updated_by=user.username,
        ))
    else:
        setting.value = body.permissions
        setting.updated_by = user.username
    await session.commit()
    return body
