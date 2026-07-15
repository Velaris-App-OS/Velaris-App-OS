"""Human-in-the-loop proposals for stateful MCP actions (P3).

When confirmation is required, a stateful tool call records a proposal instead
of executing. A human then confirms it — execution re-checks authorization as
the CONFIRMER (who is accountable) and the row's status gates it to exactly
once (pending -> executed). Proposals expire.

Copyright (c) 2024-2026 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.auth.models import AuthenticatedUser
from case_service.db.models import MCPActionProposalModel, _utcnow


def _summary(tool_name: str, args: dict) -> str:
    bits = [f"{k}={v}" for k, v in args.items() if k != "idempotency_key"]
    return f"{tool_name}({', '.join(bits)})"


async def create_proposal(
    session: AsyncSession, user: AuthenticatedUser, tool_name: str, args: dict,
) -> dict:
    """Record a pending proposal and return the confirmation envelope."""
    from case_service.config import get_settings

    case_id = None
    raw = args.get("case_id")
    if isinstance(raw, str):
        try:
            case_id = uuid.UUID(raw)
        except ValueError:
            case_id = None

    ttl = get_settings().mcp_proposal_ttl_minutes
    row = MCPActionProposalModel(
        user_id=str(user.user_id),
        tenant_id=str(user.tenant_id) if user.tenant_id else None,
        tool_name=tool_name, arguments_json=args,
        case_id=case_id, summary=_summary(tool_name, args), status="pending",
        expires_at=_utcnow() + timedelta(minutes=ttl),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return {
        "requires_confirmation": True,
        "proposal_id": str(row.id),
        "tool": tool_name,
        "summary": row.summary,
        "expires_at": row.expires_at.isoformat(),
        "message": "A human must confirm this action via "
                   "POST /api/v1/mcp/proposals/{id}/confirm before it takes effect.",
    }


async def get_proposal(session: AsyncSession, proposal_id: uuid.UUID) -> MCPActionProposalModel | None:
    return (await session.execute(
        select(MCPActionProposalModel).where(MCPActionProposalModel.id == proposal_id)
    )).scalar_one_or_none()


async def list_pending(
    session: AsyncSession, tenant_id: str | None, limit: int = 50,
) -> list[MCPActionProposalModel]:
    """Pending proposals for the caller's tenant only.

    A proposal's summary embeds argument values, so listing must be scoped:
    a caller sees proposals from their own tenant (or the untenanted set when
    they have no tenant). ``IS NOT DISTINCT FROM`` makes NULL match NULL.
    """
    rows = (await session.execute(
        select(MCPActionProposalModel)
        .where(
            MCPActionProposalModel.status == "pending",
            MCPActionProposalModel.tenant_id.is_not_distinct_from(tenant_id),
        )
        .order_by(MCPActionProposalModel.created_at.desc())
        .limit(limit)
    )).scalars().all()
    return list(rows)
