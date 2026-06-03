"""Route permission matrix — GET/PUT /admin/permissions

GET  /admin/permissions  — any authenticated user (needed at app load)
PUT  /admin/permissions  — admin only
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.auth.dependencies import get_current_user, require_role
from case_service.auth.models import AuthenticatedUser
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
    row = await session.execute(
        text("SELECT value FROM system_config WHERE key = :key"),
        {"key": _KEY},
    )
    result = row.scalar_one_or_none()
    if result is None:
        return PermissionsMap(permissions=_DEFAULTS)
    return PermissionsMap(permissions=result)


@router.put("/admin/permissions", response_model=PermissionsMap)
async def update_permissions(
    body: PermissionsMap,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(require_role("admin")),
):
    now = datetime.now(timezone.utc)
    await session.execute(
        text("""
            INSERT INTO system_config (key, value, updated_at, updated_by)
            VALUES (:key, CAST(:value AS jsonb), :ts, :by)
            ON CONFLICT (key) DO UPDATE
            SET value      = EXCLUDED.value,
                updated_at = EXCLUDED.updated_at,
                updated_by = EXCLUDED.updated_by
        """),
        {
            "key":   _KEY,
            "value": json.dumps(body.permissions),
            "ts":    now,
            "by":    user.username,
        },
    )
    await session.commit()
    return body
