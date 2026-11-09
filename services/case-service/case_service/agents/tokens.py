"""F-00 — agent tokens.

An agent token is authority handed to something that will act unattended. That
single sentence sets every choice in this file, and the shape is taken almost
unchanged from `hxmcp/tokens.py`, which got it right:

* **The JWT is stateless; the grant row is the truth.** It is re-read on EVERY
  call, so revoking a grant, suspending the agent or narrowing its ceiling all
  take effect at the agent's next request. Time-validity is not revocation.
* **A kill switch, not just a feature flag.** `agent_tokens_enabled` off
  rejects every outstanding token, not merely new mints.
* **Single-purpose.** `get_current_user` refuses `token_use="agent"` on every
  other surface, so a leaked agent token cannot call REST, mint another token,
  or escalate through any endpoint written for humans.

And one thing the MCP design did not need, because its tokens are a restriction
of a *human's* authority: an agent's authority is its own. So the capability
set is intersected with the live ceiling on every call, and both the agent's
status and the token's audience are re-checked there too.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import timedelta, timezone
from typing import Any

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials

from velaris_shared.auth.dependencies import _bearer_scheme
from velaris_shared.auth.jwt_handler import create_dev_token, decode_jwt_token
from velaris_shared.db.models import AgentModel, AgentTokenGrantModel, _utcnow

from . import identity

log = logging.getLogger(__name__)

TOKEN_USE = "agent"

#: 401 body prefixes. Deliberately uniform: an agent token that is unknown,
#: revoked, expired, disabled or belongs to a suspended agent all get the same
#: refusal, because telling a caller *which* of those it is turns the endpoint
#: into an oracle for probing live agent ids (the portal OTP lesson, 2026-08-20).
_REFUSED = "VEL-AGENT-TOKEN-401: agent token is not valid"


@dataclass(frozen=True)
class AgentCaller:
    """The authenticated agent behind a request.

    Not an `AuthenticatedUser`, and that is deliberate. An agent has no access
    groups, no MFA age, no roles and no session; giving it the human shape
    would let it flow into code that assumes those and reads them as empty
    rather than absent. Everything that authorizes an agent takes this type.
    """
    agent_id: str
    tenant_id: str
    slug: str
    kind: str                          # internal | marketplace | external
    #: The capabilities this call actually has: what the token froze at mint,
    #: intersected with the ceiling as it stands right now.
    capabilities: frozenset[str] = frozenset()
    model_binding: str | None = None
    #: Set when the token is case-scoped: valid for this case and no other.
    case_id: str | None = None
    grant_id: str = ""
    token: str = field(default="", repr=False)

    def can(self, capability: str) -> bool:
        return str(capability) in self.capabilities

    def require(self, capability: str) -> None:
        """Refuse unless the agent holds the capability.

        A capability check is not an authorization decision — HxArbiter's
        `agent` namespace is. This is the cheaper gate in front of it: it says
        the token was never issued for this, which is true regardless of policy.
        """
        if not self.can(capability):
            raise HTTPException(
                403,
                f"VEL-AGENT-CAPABILITY-403: this agent token does not carry "
                f"{capability!r}")


async def mint_agent_token(
    session: Any,
    agent: AgentModel,
    *,
    capabilities: list[str] | None = None,
    ttl_minutes: int | None = None,
    case_id: Any | None = None,
    label: str | None = None,
    issued_by: Any | None = None,
) -> tuple[str, AgentTokenGrantModel]:
    """Build the grant row and its signed JWT. The caller commits.

    `capabilities` defaults to the agent's whole live ceiling; passing a subset
    is how a call site hands out less than it holds. Passing something the
    ceiling does not contain is refused rather than trimmed — a caller asking
    for authority the agent has never held is a bug, and silently narrowing it
    would hide that bug until the token failed somewhere else.
    """
    from velaris_shared.config import get_settings
    settings = get_settings()

    if not settings.agent_tokens_enabled:
        raise identity.AgentError(
            "agent tokens are disabled on this deployment "
            "(VELARIS_CASE_AGENT_TOKENS_ENABLED)")
    if agent.status != "active":
        raise identity.AgentError(f"agent {agent.slug!r} is {agent.status}")
    if agent.kind not in identity.TOKENABLE_KINDS:
        raise identity.AgentError(
            f"tokens are not issued to {agent.kind!r} agents in this build")

    ceiling = await identity.live_capabilities(session, agent.id)
    if capabilities is None:
        wanted = sorted(ceiling)
    else:
        wanted = identity.validate_capabilities(capabilities)
        beyond = [c for c in wanted if c not in ceiling]
        if beyond:
            raise identity.AgentError(
                "these capabilities are outside the agent's ceiling: "
                + ", ".join(repr(c) for c in beyond))
    if not wanted:
        raise identity.AgentError(
            "an agent token with no capabilities can do nothing; grant the "
            "agent a capability first")

    ttl = int(ttl_minutes or settings.agent_token_default_ttl_minutes)
    if ttl <= 0:
        raise identity.AgentError("ttl_minutes must be positive")
    if ttl > settings.agent_token_max_ttl_minutes:
        raise identity.AgentError(
            f"ttl_minutes exceeds this deployment's ceiling of "
            f"{settings.agent_token_max_ttl_minutes}")

    aud_case = identity._as_uuid(case_id, "case id") if case_id else None
    grant_id = uuid.uuid4()
    grant = AgentTokenGrantModel(
        id=grant_id,
        agent_id=agent.id,
        tenant_id=agent.tenant_id,
        capabilities=wanted,
        audience_case_id=aud_case,
        label=str(label)[:255] if label else None,
        issued_by=(identity._as_uuid(issued_by, "issuer id")
                   if issued_by is not None else None),
        expires_at=_utcnow() + timedelta(minutes=ttl),
    )
    token = create_dev_token(
        user_id=str(agent.id),
        username=f"agent:{agent.slug}",
        # An agent holds NO roles, ever. Roles are the human authorization
        # vocabulary; an agent's authority is its capability set and the
        # `agent` HxArbiter namespace. An empty list here means that if an
        # agent token ever did reach a role check, it would fail closed.
        roles=[],
        secret=settings.auth_secret,
        private_key=settings.auth_rsa_private_key or "",
        expire_minutes=ttl,
        jti=str(grant_id),
        extra_claims={
            "token_use": TOKEN_USE,
            "agent_id": str(agent.id),
            "agent_tenant": str(agent.tenant_id),
            "agent_caps": wanted,
            "agent_case": str(aud_case) if aud_case else None,
        },
    )
    session.add(grant)
    log.info("agent token minted: agent=%s ttl=%smin caps=%s case=%s",
             agent.slug, ttl, wanted, aud_case or "-")
    return token, grant


async def revoke_grant(
    session: Any, grant: AgentTokenGrantModel, *, revoked_by: Any | None = None,
) -> AgentTokenGrantModel:
    if grant.revoked:
        return grant
    grant.revoked = True
    grant.revoked_at = _utcnow()
    grant.revoked_by = (identity._as_uuid(revoked_by, "revoker id")
                        if revoked_by is not None else None)
    return grant


def _expired(grant: AgentTokenGrantModel) -> bool:
    exp = grant.expires_at
    if exp is None:
        return False
    if exp.tzinfo is None:                     # SQLite / MySQL: tz-naive
        exp = exp.replace(tzinfo=timezone.utc)
    return exp < _utcnow()


async def resolve_agent_token(session: Any, raw_token: str) -> AgentCaller:
    """Turn a raw agent JWT into an `AgentCaller`, or refuse.

    The whole server-side check lives here so that the FastAPI dependency and
    any in-process caller (a worker verifying a token it was handed) cannot
    drift apart — the audit of 2026-08-29 found three PEPs whose halves were
    each asserted and whose composition never was.
    """
    from velaris_shared.config import get_settings
    settings = get_settings()

    # Kill switch first: when it is off, no outstanding token is valid, and no
    # database work should happen on behalf of one.
    if not settings.agent_tokens_enabled:
        raise HTTPException(401, _REFUSED, headers={"WWW-Authenticate": "Bearer"})

    try:
        claims = decode_jwt_token(
            raw_token,
            secret=settings.auth_secret,
            issuer=settings.auth_issuer,
            audience=settings.auth_audience,
            public_key=settings.auth_rsa_public_key or "",
        )
    except Exception:
        raise HTTPException(401, _REFUSED, headers={"WWW-Authenticate": "Bearer"})

    if claims.get("token_use") != TOKEN_USE:
        raise HTTPException(401, _REFUSED, headers={"WWW-Authenticate": "Bearer"})

    try:
        grant_id = uuid.UUID(str(claims.get("jti")))
    except (ValueError, TypeError):
        raise HTTPException(401, _REFUSED, headers={"WWW-Authenticate": "Bearer"})

    grant = await session.get(AgentTokenGrantModel, grant_id)
    if grant is None or grant.revoked or _expired(grant):
        raise HTTPException(401, _REFUSED, headers={"WWW-Authenticate": "Bearer"})

    agent = await session.get(AgentModel, grant.agent_id)
    if agent is None or agent.status != "active":
        # Suspension is the incident-response kill switch and has to bite here,
        # not at the next mint: an agent misbehaving at 3am is stopped by
        # `agents.status.change`, and this is the line that makes that true.
        raise HTTPException(401, _REFUSED, headers={"WWW-Authenticate": "Bearer"})

    # The token's claim is a copy; the grant row is the record. If they ever
    # disagree, the row wins — a claim is whatever was signed, while the row is
    # what an operator has since done.
    frozen = {str(c) for c in (grant.capabilities or []) if isinstance(c, str)}
    live = await identity.live_capabilities(session, agent.id)
    effective = frozen & live
    if not effective:
        # Every capability has been revoked since the mint. The token is a
        # bearer of nothing, and saying so as 401 rather than letting it through
        # with an empty set means the agent stops instead of quietly no-opping.
        raise HTTPException(401, _REFUSED, headers={"WWW-Authenticate": "Bearer"})
    # An archived (offboarded) tenant's agents stop with its people (R7).
    from velaris_shared.tenancy.identity import tenant_closed
    if await tenant_closed(session, agent.tenant_id):
        raise HTTPException(401, _REFUSED, headers={"WWW-Authenticate": "Bearer"})

    return AgentCaller(
        agent_id=str(agent.id),
        tenant_id=str(agent.tenant_id),
        slug=agent.slug,
        kind=agent.kind,
        capabilities=frozenset(effective),
        model_binding=agent.model_binding,
        case_id=str(grant.audience_case_id) if grant.audience_case_id else None,
        grant_id=str(grant.id),
        token=raw_token,
    )


async def get_agent_caller(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> AgentCaller:
    """FastAPI dependency: the ONLY place an agent token is accepted.

    A route wearing this dependency accepts agents and nothing else — not a
    human session token. That asymmetry is intentional: a surface built for an
    unattended actor should not quietly double as a human one, because then its
    rate limits, its logging and its policy namespace all describe traffic they
    were not written for.
    """
    if credentials is None:
        raise HTTPException(401, "Authentication required",
                            headers={"WWW-Authenticate": "Bearer"})
    from velaris_shared.db.commit_route import request_session

    session = request_session(request)   # no second connection (request_session)
    if session is not None:
        return await resolve_agent_token(session, credentials.credentials)

    from velaris_shared.db.session import get_session_factory
    async with get_session_factory()() as own_session:
        return await resolve_agent_token(own_session, credentials.credentials)


def require_case(caller: AgentCaller, case_id: Any) -> None:
    """Refuse unless a case-scoped token is being used on its own case.

    An agent-scoped token (no audience) passes: it was issued without a case
    restriction. A case-scoped one is valid for exactly one case, which is the
    property F-09's work orders depend on.
    """
    if caller.case_id is None:
        return
    if str(case_id) != caller.case_id:
        raise HTTPException(
            403,
            "VEL-AGENT-AUDIENCE-403: this agent token is scoped to a "
            "different case")
