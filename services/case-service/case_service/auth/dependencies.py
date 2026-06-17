"""FastAPI auth dependencies.

Provides get_current_user and require_role dependencies for routes.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import hashlib
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Callable

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.auth.models import AuthenticatedUser, ActiveAccessGroup
from case_service.auth.jwt_handler import decode_jwt_token, extract_user_from_claims
from case_service.config import get_settings

logger = logging.getLogger(__name__)

# ── Revoked-session in-memory cache (30s TTL) ─────────────────────────────────
# Avoids a DB round-trip on every request in the common (non-revoked) case.
_REVOKED_HASHES: set[str] = set()
_REVOKED_CACHE_AT: float  = 0.0
_REVOKED_CACHE_TTL: float = 30.0


async def _refresh_revoked_cache(session: AsyncSession) -> None:
    global _REVOKED_CACHE_AT
    from case_service.db.models import RevokedSessionModel
    now_utc = datetime.now(timezone.utc)
    rows = (await session.execute(
        select(RevokedSessionModel.token_hash).where(
            RevokedSessionModel.expires_at > now_utc
        )
    )).scalars().all()
    _REVOKED_HASHES.clear()
    _REVOKED_HASHES.update(rows)
    _REVOKED_CACHE_AT = time.monotonic()


def _is_token_revoked_cached(raw_token: str) -> bool:
    return hashlib.sha256(raw_token.encode()).hexdigest() in _REVOKED_HASHES

_bearer_scheme = HTTPBearer(auto_error=False)


async def _enrich_with_access_group(
    user: AuthenticatedUser,
    session: AsyncSession,
) -> AuthenticatedUser:
    """Load the operator's active access group from the DB and enrich the user.

    Falls back gracefully — if no access groups are assigned, the user object
    is returned unchanged so all existing require_role() checks still work.
    """
    from case_service.db.models import (
        UserDirectoryModel, AccessGroupModel, AccessRoleModel,
        OperatorAccessGroupModel, PortalModel,
    )

    # Load operator record
    dir_row = (await session.execute(
        select(UserDirectoryModel).where(UserDirectoryModel.user_id == user.user_id)
    )).scalar_one_or_none()

    # Load all access group memberships for this operator
    oag_rows = (await session.execute(
        select(OperatorAccessGroupModel).where(
            OperatorAccessGroupModel.operator_id == user.user_id
        )
    )).scalars().all()

    # Always populate tenant_id from user_directory regardless of access groups
    if dir_row and dir_row.tenant_id:
        user.tenant_id = dir_row.tenant_id

    if not oag_rows:
        return user  # no P37 groups assigned yet — legacy flat roles still work

    # Determine active group: persisted current > primary > first
    active_group_id: uuid.UUID | None = None
    if dir_row and dir_row.current_access_group_id:
        # Verify it's still a valid membership
        ids = {str(r.access_group_id) for r in oag_rows}
        if str(dir_row.current_access_group_id) in ids:
            active_group_id = dir_row.current_access_group_id

    if active_group_id is None:
        primary = next((r for r in oag_rows if r.is_primary), None) or oag_rows[0]
        active_group_id = primary.access_group_id

    # Load the active group + its portal
    ag = (await session.execute(
        select(AccessGroupModel).where(AccessGroupModel.id == active_group_id)
    )).scalar_one_or_none()

    if ag is None:
        return user

    portal = (await session.execute(
        select(PortalModel).where(PortalModel.id == ag.portal_id)
    )).scalar_one_or_none()

    # Resolve role names from role_ids
    role_names: list[str] = []
    all_privileges: list[dict] = []
    if ag.role_ids:
        role_uuids = [uuid.UUID(r) if isinstance(r, str) else r for r in ag.role_ids]
        role_rows = (await session.execute(
            select(AccessRoleModel).where(AccessRoleModel.id.in_(role_uuids))
        )).scalars().all()
        for rr in role_rows:
            role_names.append(rr.name)
            all_privileges.extend(rr.privileges or [])

    # Build available_access_groups summary
    all_ag_ids = [r.access_group_id for r in oag_rows]
    all_ags = (await session.execute(
        select(AccessGroupModel).where(AccessGroupModel.id.in_(all_ag_ids))
    )).scalars().all()
    available = [
        {
            "id": str(a.id),
            "name": a.name,
            "is_primary": any(r.is_primary and r.access_group_id == a.id for r in oag_rows),
        }
        for a in all_ags
    ]

    active_ag = ActiveAccessGroup(
        id=str(ag.id),
        name=ag.name,
        portal_id=str(portal.id) if portal else "",
        portal_type=portal.portal_type if portal else "staff",
        portal_name=portal.name if portal else "",
        modules=portal.modules if portal else [],
        homepage=portal.homepage if portal else "/work-center",
        roles=role_names,
        privileges=all_privileges,
        allowed_case_type_ids=ag.allowed_case_type_ids or ["*"],
        allowed_queue_ids=ag.allowed_queue_ids or ["*"],
    )

    # Merge role names into AuthenticatedUser.roles so require_role() keeps working
    merged_roles = list(set(user.roles) | set(role_names))

    user.roles = merged_roles
    user.active_access_group = active_ag
    user.available_access_groups = available
    return user


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> AuthenticatedUser:
    """Extract and validate the current user from the request.

    In dev mode, returns a mock admin user.
    In jwt/oidc mode, validates the Bearer token.

    P37: enriches the user with active access group context when available.
    Falls back gracefully so all pre-P37 routes are unaffected.
    """
    settings = get_settings()

    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        claims = decode_jwt_token(
            credentials.credentials,
            secret=settings.auth_secret,
            issuer=settings.auth_issuer,
            audience=settings.auth_audience,
            public_key=settings.auth_rsa_public_key or "",
        )
        user = AuthenticatedUser(
            token=credentials.credentials,
            **extract_user_from_claims(claims),
        )
    except Exception as e:
        logger.warning("JWT validation failed: %s", e)
        raise HTTPException(
            status_code=401,
            detail=f"Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # P37 enrichment + security checks (is_active + revoked session)
    try:
        from case_service.db.session import get_session_factory
        from case_service.db.models import HelixUserModel

        session: AsyncSession | None = getattr(request.state, "db_session", None)
        if session is None:
            async with get_session_factory()() as session:
                user = await _enrich_with_access_group(user, session)
                await _security_check(user, credentials.credentials, session)
        else:
            user = await _enrich_with_access_group(user, session)
            await _security_check(user, credentials.credentials, session)
    except HTTPException:
        raise
    except Exception:
        logger.debug("P37 enrichment / security check skipped", exc_info=True)

    return user


async def _security_check(
    user: AuthenticatedUser,
    raw_token: str,
    session: AsyncSession,
) -> None:
    """Verify the token has not been revoked and the account is still active."""
    from case_service.db.models import HelixUserModel

    # Refresh revoked-token cache if stale
    if time.monotonic() - _REVOKED_CACHE_AT > _REVOKED_CACHE_TTL:
        try:
            await _refresh_revoked_cache(session)
        except Exception:
            pass  # Cache miss is acceptable; we'll check DB on next refresh

    if _is_token_revoked_cached(raw_token):
        raise HTTPException(401, "Session has been revoked",
                            headers={"WWW-Authenticate": "Bearer"})

    # Check is_active on helix_users (breach auto-disable lands here)
    try:
        hu = await session.get(HelixUserModel, uuid.UUID(user.user_id))
        if hu is not None and not hu.is_active:
            raise HTTPException(401, "Account is suspended. Contact your administrator.",
                                headers={"WWW-Authenticate": "Bearer"})
    except HTTPException:
        raise
    except Exception:
        pass  # If the lookup fails, don't block auth — log and continue


def require_role(*roles: str) -> Callable:
    """Dependency factory — require user to have at least one of the given roles.

    Superadmin bypasses all role checks unconditionally.
    """
    async def _check(user: AuthenticatedUser = Depends(get_current_user)):
        # Superadmin passes every gate
        if "superadmin" in (user.roles or []):
            return user
        if user.is_admin:
            return user
        for role in roles:
            if user.has_role(role):
                return user
        raise HTTPException(
            status_code=403,
            detail=f"Requires one of: {', '.join(roles)}",
        )
    return _check


def require_admin() -> Callable:
    """Dependency — require admin role."""
    return require_role("admin")
