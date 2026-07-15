"""HxNexus Operator (MCP) P4 — external-agent scoped tokens.

Delegation-of-self: a scoped token is a RESTRICTION of the grantor's own
authority, never an expansion. Every tool call still authorizes as the
grantor through HxGuard (the confused-deputy machinery is unchanged); the
token only shrinks the visible/callable tool surface to its mcp_scope.

Enforcement points:
- get_current_user rejects token_use="mcp" everywhere — a leaked agent token
  cannot call REST, confirm proposals, or mint further tokens.
- get_mcp_caller (below) is the ONLY dependency that accepts scoped tokens,
  and only while VELARIS_CASE_MCP_EXTERNAL_TOKENS_ENABLED is on (kill switch:
  turning it off instantly rejects every outstanding token).
- The grant row is checked on every call — revocation is immediate, not
  eventual, even while the JWT is still time-valid.

Copyright (c) 2024-2026 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from datetime import timedelta, timezone

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials

from case_service.auth.dependencies import (
    _bearer_scheme,
    _enrich_with_access_group,
    _security_check,
    get_current_user,
)
from case_service.auth.jwt_handler import (
    create_dev_token,
    decode_jwt_token,
    extract_user_from_claims,
)
from case_service.auth.models import AuthenticatedUser
from case_service.db.models import MCPTokenGrantModel, _utcnow

log = logging.getLogger(__name__)

SCOPE_ATTR = "mcp_scope"     # user.attributes key: granted tool names (list)
JTI_ATTR = "mcp_jti"         # user.attributes key: grant id (rate-limit bucket)


def mint_scoped_token(
    user: AuthenticatedUser, tools: list[str], ttl_minutes: int, label: str | None,
) -> tuple[str, MCPTokenGrantModel]:
    """Build the grant row + signed JWT. Caller persists the row."""
    from case_service.config import get_settings
    settings = get_settings()

    grant_id = uuid.uuid4()
    grant = MCPTokenGrantModel(
        id=grant_id,
        user_id=str(user.user_id),
        tenant_id=str(user.tenant_id) if user.tenant_id else None,
        tools=sorted(tools),
        label=label,
        expires_at=_utcnow() + timedelta(minutes=ttl_minutes),
    )
    token = create_dev_token(
        user_id=str(user.user_id),
        username=f"mcp-agent:{user.username or user.user_id}",
        roles=list(user.roles or []),           # grantor's authority at mint time
        secret=settings.auth_secret,
        private_key=settings.auth_rsa_private_key or "",
        expire_minutes=ttl_minutes,
        jti=str(grant_id),
        extra_claims={"token_use": "mcp", "mcp_scope": sorted(tools)},
    )
    return token, grant


async def get_mcp_caller(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> AuthenticatedUser:
    """Auth dependency for the MCP transport: accepts normal user tokens
    (identical to get_current_user) AND scoped mcp tokens (nowhere else valid).
    """
    from case_service.config import get_settings
    settings = get_settings()

    if credentials is None:
        raise HTTPException(401, "Authentication required",
                            headers={"WWW-Authenticate": "Bearer"})
    try:
        claims = decode_jwt_token(
            credentials.credentials,
            secret=settings.auth_secret,
            issuer=settings.auth_issuer,
            audience=settings.auth_audience,
            public_key=settings.auth_rsa_public_key or "",
        )
    except Exception:
        raise HTTPException(401, "Invalid or expired token",
                            headers={"WWW-Authenticate": "Bearer"})

    if claims.get("token_use") != "mcp":
        # ordinary session token — the standard path (incl. its own rejections)
        return await get_current_user(request, credentials)

    # ── scoped-token path ──────────────────────────────────────────────
    if not settings.mcp_external_tokens_enabled:
        # kill switch: no outstanding token survives the flag being off
        raise HTTPException(401, "Scoped MCP tokens are disabled",
                            headers={"WWW-Authenticate": "Bearer"})

    jti = claims.get("jti")
    try:
        grant_id = uuid.UUID(str(jti))
    except (ValueError, TypeError):
        raise HTTPException(401, "Invalid or expired token",
                            headers={"WWW-Authenticate": "Bearer"})

    user = AuthenticatedUser(
        token=credentials.credentials, **extract_user_from_claims(claims),
    )

    # grant row = server-side truth: exists, not revoked, not expired.
    # Checked on EVERY call so revocation is instant.
    from case_service.db.session import get_session_factory
    async with get_session_factory()() as session:
        grant = await session.get(MCPTokenGrantModel, grant_id)
        if grant is None or grant.revoked:
            raise HTTPException(401, "Scoped MCP token has been revoked",
                                headers={"WWW-Authenticate": "Bearer"})
        exp = grant.expires_at
        if exp is not None and exp.tzinfo is None:      # SQLite: tz-naive
            exp = exp.replace(tzinfo=timezone.utc)
        if exp is not None and exp < _utcnow():
            raise HTTPException(401, "Scoped MCP token has expired",
                                headers={"WWW-Authenticate": "Bearer"})
        scope = [t for t in (grant.tools or []) if isinstance(t, str)]

        # same enrichment + security posture as a normal session: the grantor's
        # live tenant/access groups apply, a suspended grantor kills the token
        try:
            user = await _enrich_with_access_group(user, session)
            await _security_check(user, credentials.credentials, session)
        except HTTPException:
            raise
        except Exception:
            log.debug("mcp scoped-token enrichment skipped", exc_info=True)

    user.attributes[SCOPE_ATTR] = scope     # registry/server intersect on this
    user.attributes[JTI_ATTR] = str(grant_id)
    return user


def caller_scope(user: AuthenticatedUser) -> set[str] | None:
    """The tool-name scope for this caller, or None for a full session user."""
    scope = (user.attributes or {}).get(SCOPE_ATTR)
    if scope is None:
        return None
    return {t for t in scope if isinstance(t, str)}
