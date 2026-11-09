"""Route permission matrix — GET/PUT /admin/permissions

GET  /admin/permissions  — any authenticated user (needed at app load)
PUT  /admin/permissions  — admin only
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.auth.dependencies import get_current_user, require_role
from velaris_shared import hxguard
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.db.models import SystemConfigModel
from velaris_shared.db.session import get_session

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
    # 198 — the status set and action configuration. Absent from here, the
    # frontend's `getRouteRoles` returns [] and `RequireRole` denies everybody,
    # so the page renders "not found" while the route, the nav entry and the
    # sitemap all look correct. The nav-data defaults only apply until this
    # endpoint answers.
    "/case-config":    ["designer", "admin"],
    "/form-builder":   ["designer", "admin"],
    "/reference-tables": ["designer", "admin"],   # Form Builder v2 step 3
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
    "/bots":           ["admin", "developer"],
    # Read-only for a manager: seeing what runs overnight is not the same as
    # being able to change it. The server gates each call by privilege anyway —
    # this map is what the UI hides, never what protects the data.
    "/scheduler":      ["admin", "developer", "manager"],
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

    # **Defaults UNDER the stored map, never instead of it.**
    #
    # The stored value used to replace `_DEFAULTS` outright, so a route added to
    # this file after an operator had saved the matrix once was simply absent
    # from the answer — and an absent route means `getRouteRoles` returns `[]`,
    # `RequireRole` denies everybody, and the page renders "not found" while the
    # route, the nav entry and the sitemap all look perfectly correct. It is
    # invisible on a fresh install (no stored row, defaults used) and permanent
    # on any install that has ever opened the permissions screen.
    #
    # Found on 2026-09-07 with `/case-config`; it would have silently swallowed
    # every route added from here on. An operator's saved choices still win for
    # every route they actually made a choice about — this only supplies the
    # ones they have never seen.
    merged = {**_DEFAULTS, **(setting.value or {})}
    return PermissionsMap(permissions=merged)


@router.put("/admin/permissions", response_model=PermissionsMap)
async def update_permissions(
    body: PermissionsMap,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("identity.permissions.write")),
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
