"""HxNexus Operator MCP transport — POST /api/v1/mcp (Streamable HTTP, stateless).

The confused-deputy defence lives at this edge: the endpoint requires the
caller's own Bearer JWT (401 without), and every tool re-checks HxGuard as
that user. No service token exists on this surface in P1; external-agent
scoped tokens are P4.

Copyright (c) 2024-2026 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser
from case_service.db.session import get_session
from case_service.hxmcp import server
from case_service.hxmcp.tokens import JTI_ATTR, get_mcp_caller
from case_service.hxnexus.guard import _RateLimiter

router = APIRouter(prefix="/mcp", tags=["hxmcp"])

MAX_BODY_BYTES = 64 * 1024      # a tool call is small; anything bigger is abuse

_rate_limiter: _RateLimiter | None = None
_ext_rate_limiter: _RateLimiter | None = None


def _check_rate(user: AuthenticatedUser) -> None:
    """Per-caller tool-call budget (sliding window). A scoped external token
    (P4) gets its own, stricter per-grant budget keyed by jti — one runaway
    agent throttles itself, not every session of the granting user."""
    global _rate_limiter, _ext_rate_limiter
    from case_service.config import get_settings

    jti = (user.attributes or {}).get(JTI_ATTR)
    if jti:
        if _ext_rate_limiter is None:
            _ext_rate_limiter = _RateLimiter(
                max_calls=get_settings().mcp_external_rate_per_min, window_seconds=60,
            )
        allowed, retry = _ext_rate_limiter.is_allowed(str(jti))
    else:
        if _rate_limiter is None:
            _rate_limiter = _RateLimiter(
                max_calls=get_settings().mcp_rate_per_min, window_seconds=60,
            )
        allowed, retry = _rate_limiter.is_allowed(str(user.user_id))
    if not allowed:
        raise HTTPException(429, f"MCP call budget exceeded. Try again in {retry}s.",
                            headers={"Retry-After": str(retry)})


@router.post("")
async def mcp_endpoint(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_mcp_caller),   # sessions AND scoped tokens
):
    _check_rate(user)
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(413, "request body too large")
    try:
        message = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return {"jsonrpc": "2.0", "id": None,
                "error": {"code": server.PARSE_ERROR, "message": "invalid JSON"}}

    response = await server.handle_message(session, user, message)
    if response is None:            # notification — accepted, no body
        return Response(status_code=202)
    return response


@router.get("")
async def mcp_no_stream():
    """Stateless server: no server-initiated SSE stream to open."""
    raise HTTPException(405, "This MCP server is stateless — POST JSON-RPC messages")


# ── P3 human-in-the-loop: proposal review + confirmation ──────────────
# A human reviews AI-proposed stateful actions here. Confirmation EXECUTES the
# action as the confirmer (re-checking case.update), so the human is authorized
# and accountable. These are human surfaces, not MCP tools.

import uuid  # noqa: E402


def _proposal_view(p) -> dict:
    return {
        "id": str(p.id), "tool": p.tool_name, "summary": p.summary,
        "case_id": str(p.case_id) if p.case_id else None,
        "status": p.status, "proposed_by": p.user_id,
        "expires_at": p.expires_at.isoformat() if p.expires_at else None,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


@router.get("/proposals")
async def list_proposals(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    from case_service.hxmcp import proposals as prop
    tenant = str(user.tenant_id) if user.tenant_id else None
    return {"items": [_proposal_view(p) for p in await prop.list_pending(session, tenant)]}


def _tenant_ok(proposal, user: AuthenticatedUser) -> bool:
    """A proposal may only be acted on from within its own tenant."""
    p_tenant = proposal.tenant_id
    u_tenant = str(user.tenant_id) if user.tenant_id else None
    return p_tenant == u_tenant


@router.post("/proposals/{proposal_id}/confirm")
async def confirm_proposal(
    proposal_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    from sqlalchemy import select
    from case_service.db.models import MCPActionProposalModel, _utcnow
    from case_service.hxmcp.registry import TOOLS, ToolArgError

    # row lock so two confirmers can't both execute the same proposal
    p = (await session.execute(
        select(MCPActionProposalModel)
        .where(MCPActionProposalModel.id == proposal_id)
        .with_for_update()
    )).scalar_one_or_none()
    if p is None or not _tenant_ok(p, user):
        raise HTTPException(404, "Proposal not found")   # anti-oracle across tenants
    if p.status != "pending":
        raise HTTPException(409, f"Proposal already {p.status}")
    from datetime import timezone
    exp = p.expires_at
    if exp is not None and exp.tzinfo is None:   # SQLite returns tz-naive
        exp = exp.replace(tzinfo=timezone.utc)
    if exp is not None and exp < _utcnow():
        p.status = "expired"
        await session.commit()
        raise HTTPException(409, "Proposal expired")

    tool = TOOLS.get(p.tool_name)
    if tool is None or not tool.stateful:
        raise HTTPException(400, "Proposal references an unknown stateful tool")

    # execute AS THE CONFIRMER — the wrapped handler re-checks case.update, so
    # a human without rights on the case cannot confirm it into effect
    try:
        payload = await tool.handler(session, user, p.arguments_json)
    except ToolArgError as exc:
        raise HTTPException(400, str(exc))
    # HTTPException (403/404 authz) propagates: the proposal stays pending so an
    # authorized human can still confirm it.

    p.status = "executed"
    p.result_json = payload
    p.is_error = False
    p.decided_by = str(user.user_id)
    p.decided_at = _utcnow()
    await session.commit()
    return {"status": "executed", "proposal_id": str(proposal_id), "result": payload}


@router.post("/proposals/{proposal_id}/reject")
async def reject_proposal(
    proposal_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    from sqlalchemy import select
    from case_service.db.models import MCPActionProposalModel, _utcnow

    p = (await session.execute(
        select(MCPActionProposalModel)
        .where(MCPActionProposalModel.id == proposal_id)
        .with_for_update()
    )).scalar_one_or_none()
    if p is None or not _tenant_ok(p, user):
        raise HTTPException(404, "Proposal not found")   # anti-oracle across tenants
    if p.status != "pending":
        raise HTTPException(409, f"Proposal already {p.status}")
    p.status = "rejected"
    p.decided_by = str(user.user_id)
    p.decided_at = _utcnow()
    await session.commit()
    return {"status": "rejected", "proposal_id": str(proposal_id)}


# ── P4 external agents: scoped-token grant management ─────────────────
# Human surfaces (get_current_user — a scoped token can NEVER mint, list, or
# revoke tokens). Minting is HxGuard-gated and the grant is delegation-of-self:
# scope ⊆ currently visible tools, so a grant can't include a security tool
# (none is registered) or resurface a globally disabled write/stateful tool.

from pydantic import BaseModel, Field as _Field  # noqa: E402


class MintTokenBody(BaseModel):
    tools: list[str] = _Field(min_length=1, max_length=20)
    ttl_minutes: int | None = _Field(default=None, ge=1)
    label: str | None = _Field(default=None, max_length=255)


def _grant_view(g) -> dict:
    return {
        "id": str(g.id), "tools": g.tools, "label": g.label,
        "revoked": g.revoked, "expires_at": g.expires_at.isoformat(),
        "created_at": g.created_at.isoformat() if g.created_at else None,
    }


@router.post("/tokens", status_code=201)
async def mint_token(
    body: MintTokenBody,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    from case_service.config import get_settings
    from case_service.hxguard import service as hxguard
    from case_service.hxmcp.registry import visible_tools
    from case_service.hxmcp.tokens import mint_scoped_token

    settings = get_settings()
    if not settings.mcp_external_tokens_enabled:
        raise HTTPException(403, "Scoped MCP tokens are disabled "
                            "(VELARIS_CASE_MCP_EXTERNAL_TOKENS_ENABLED)")
    await hxguard.require(session, hxguard.subject_from_user(user), "mcp.tokens.mint")

    allowed = {t.name for t in visible_tools()}
    requested = set(body.tools)
    unknown = sorted(requested - allowed)
    if unknown:
        # same wording for nonexistent and disabled tools (anti-oracle)
        raise HTTPException(400, f"Tools not grantable: {', '.join(unknown)}")

    ttl = body.ttl_minutes or settings.mcp_token_default_ttl_minutes
    ttl = min(ttl, settings.mcp_token_max_ttl_minutes)

    token, grant = mint_scoped_token(user, sorted(requested), ttl, body.label)
    session.add(grant)
    await session.commit()
    return {
        "token": token,                     # shown once — not retrievable later
        "token_type": "Bearer",
        "grant": _grant_view(grant),
        "note": "Valid only on POST /api/v1/mcp, only for the granted tools, "
                "as your own authority. Revoke via POST /mcp/tokens/{id}/revoke.",
    }


@router.get("/tokens")
async def list_tokens(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """The caller's own grants (newest first)."""
    from sqlalchemy import select
    from case_service.db.models import MCPTokenGrantModel

    rows = (await session.execute(
        select(MCPTokenGrantModel)
        .where(MCPTokenGrantModel.user_id == str(user.user_id))
        .order_by(MCPTokenGrantModel.created_at.desc())
        .limit(100)
    )).scalars().all()
    return {"items": [_grant_view(g) for g in rows]}


@router.post("/tokens/{grant_id}/revoke")
async def revoke_token(
    grant_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Instant revocation — the transport checks the grant row on every call.
    Own grants only (admins included: minting others' tokens isn't a thing,
    so neither is revoking by proxy; the kill switch covers emergencies)."""
    from case_service.db.models import MCPTokenGrantModel, _utcnow as now

    g = await session.get(MCPTokenGrantModel, grant_id)
    if g is None or g.user_id != str(user.user_id):
        raise HTTPException(404, "Grant not found")     # anti-oracle
    if g.revoked:
        return {"status": "already_revoked", "grant_id": str(grant_id)}
    g.revoked = True
    g.revoked_at = now()
    g.revoked_by = str(user.user_id)
    await session.commit()
    return {"status": "revoked", "grant_id": str(grant_id)}
