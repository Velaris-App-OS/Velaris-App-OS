"""MCP tool registry — the curated, default-deny surface (P1: read-only).

Every tool handler receives the *session user* and re-checks authorization
exactly as the REST API would (confused-deputy defence, report §5.6): tools
call the existing router handlers / HxGuard directly, so authz, relationship
filtering, and PII redaction cannot diverge from REST. A tool name absent
from TOOLS does not exist — there is no dynamic dispatch.

P1 contains no mutating tool. No tool may ever touch security / authz /
credentials (design invariant, not just a P1 restriction).

Copyright (c) 2024-2026 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from case_service.auth.models import AuthenticatedUser

MAX_LIMIT = 50            # hard cap on any list-shaped tool result
MAX_QUESTION_CHARS = 2000  # input size cap for free-text tool arguments


class ToolArgError(ValueError):
    """Invalid tool arguments — mapped to JSON-RPC invalid params."""


def _arg_uuid(args: dict, key: str) -> uuid.UUID:
    raw = args.get(key)
    if not isinstance(raw, str):
        raise ToolArgError(f"'{key}' (string uuid) is required")
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise ToolArgError(f"'{key}' is not a valid uuid") from None


def _arg_limit(args: dict) -> int:
    limit = args.get("limit", 20)
    if not isinstance(limit, int) or isinstance(limit, bool) or not (1 <= limit <= MAX_LIMIT):
        raise ToolArgError(f"'limit' must be an integer between 1 and {MAX_LIMIT}")
    return limit


def _arg_opt_str(args: dict, key: str, max_len: int = 200) -> str | None:
    val = args.get(key)
    if val is None:
        return None
    if not isinstance(val, str) or len(val) > max_len:
        raise ToolArgError(f"'{key}' must be a string of at most {max_len} characters")
    return val


# ── Tool handlers ─────────────────────────────────────────────────────
# Each returns a JSON-serializable dict. Router handlers are imported
# lazily (established codebase style) and called with the session user, so
# HTTPException 403/404 semantics — including the anti-probing 404 for
# denied case reads — surface unchanged.


async def _case_search(session: AsyncSession, user: AuthenticatedUser, args: dict) -> dict:
    from case_service.api.routers.cases import list_cases

    case_type_id = None
    if args.get("case_type_id") is not None:
        case_type_id = _arg_uuid(args, "case_type_id")
    result = await list_cases(
        status=_arg_opt_str(args, "status"),
        priority=_arg_opt_str(args, "priority"),
        case_type_id=case_type_id,
        var=None,
        page=1,
        page_size=_arg_limit(args),
        session=session,
        user=user,
    )
    dumped = result.model_dump(mode="json")
    # list results stay lean: drop the per-case variables blob (case_get
    # returns it, redacted) so a broad search can't bulk-export case data
    for item in dumped.get("items", []):
        item.pop("data", None)
    return dumped


async def _case_get(session: AsyncSession, user: AuthenticatedUser, args: dict) -> dict:
    from case_service.api.routers.cases import get_case

    case_id = _arg_uuid(args, "case_id")
    response = await get_case(case_id=case_id, session=session, user=user)
    # get_case returns a plain dict with datetime/uuid values — normalize
    from case_service.api.schemas.cases import CaseResponse
    return CaseResponse.model_validate(response).model_dump(mode="json")


async def _case_timeline(session: AsyncSession, user: AuthenticatedUser, args: dict) -> dict:
    from case_service import hxguard
    from case_service.api.routers.cases import get_case_history
    from case_service.api.schemas.cases import AuditEntryResponse

    case_id = _arg_uuid(args, "case_id")
    # The REST /history route now enforces case.read itself (route-migration
    # 2026-07-09); the tool keeps its own check too — defence in depth for the
    # least-trusted consumers this surface has.
    await hxguard.require_case(session, user, "case.read", case_id)
    entries = await get_case_history(case_id=case_id, session=session, user=user)
    return {"items": [
        AuditEntryResponse.model_validate(e).model_dump(mode="json") for e in entries
    ]}


async def _case_type_list(session: AsyncSession, user: AuthenticatedUser, args: dict) -> dict:
    from case_service.api.routers.case_types import list_case_types

    result = await list_case_types(
        page=1, page_size=_arg_limit(args), tenant_id=None,
        session=session, user=user,
    )
    dumped = result.model_dump(mode="json")
    # definitions are heavy — the list gives identity, get gives the definition
    for item in dumped.get("items", []):
        item.pop("definition_json", None)
    return dumped


async def _case_type_get(session: AsyncSession, user: AuthenticatedUser, args: dict) -> dict:
    from case_service.api.routers.case_types import get_case_type
    from case_service.api.schemas.cases import CaseTypeResponse

    ct = await get_case_type(
        case_type_id=_arg_uuid(args, "case_type_id"), session=session, user=user,
    )
    return CaseTypeResponse.model_validate(ct).model_dump(mode="json")


async def _graph_query(session: AsyncSession, user: AuthenticatedUser, args: dict) -> dict:
    from case_service.hxgraph.query import query_graph

    question = args.get("question")
    if not isinstance(question, str) or not question.strip():
        raise ToolArgError("'question' (non-empty string) is required")
    if len(question) > MAX_QUESTION_CHARS:
        raise ToolArgError(f"'question' must be at most {MAX_QUESTION_CHARS} characters")
    # Same tenant visibility as REST /graph/query: tenant users see global +
    # their own tenant's nodes; tenant-less callers see everything.
    tenant = str(user.tenant_id) if user.tenant_id else None
    return await query_graph(session, question, tenant_id=tenant)


# ── Mutating tool handlers (P2) ───────────────────────────────────────
# Each wraps the same REST handler a human uses, so the case.update HxGuard
# check, the before-image audit, promoted-key rejection, broadcast, and
# webhook outbox all apply unchanged. Idempotency is enforced one layer up
# in server.py (every mutating tool requires an idempotency_key).


def _dummy_request():
    """Some case handlers accept a Request only to reach ``request.app.state``
    (e.g. the Temporal client for lifecycle signals). MCP supplies a minimal
    request carrying the real app so that lookup resolves (and no-ops when
    Temporal is absent). Lazy import avoids a module-load cycle."""
    from fastapi import Request
    from case_service.main import app
    return Request({"type": "http", "headers": [], "method": "POST",
                    "query_string": b"", "app": app})


def _arg_data(args: dict, key: str = "data") -> dict:
    data = args.get(key)
    if not isinstance(data, dict) or not data:
        raise ToolArgError(f"'{key}' must be a non-empty object of variables to set")
    if len(data) > 50:
        raise ToolArgError(f"'{key}' may set at most 50 variables per call")
    return data


async def _case_update_data(session: AsyncSession, user: AuthenticatedUser, args: dict) -> dict:
    from case_service.api.routers.cases import update_case_data
    from case_service.api.schemas.cases import CaseDataUpdate, CaseResponse

    case_id = _arg_uuid(args, "case_id")
    body = CaseDataUpdate(data=_arg_data(args), updated_by=str(user.user_id))
    response = await update_case_data(
        case_id=case_id, body=body, session=session, user=user)
    return CaseResponse.model_validate(response).model_dump(mode="json")


async def _case_set_priority(session: AsyncSession, user: AuthenticatedUser, args: dict) -> dict:
    from case_service.api.routers.cases import change_priority
    from case_service.api.schemas.cases import CasePriorityChange, CaseResponse

    case_id = _arg_uuid(args, "case_id")
    priority = args.get("priority")
    if priority not in ("low", "medium", "high", "critical"):
        raise ToolArgError("'priority' must be one of: low, medium, high, critical")
    body = CasePriorityChange(priority=priority, actor_id=str(user.user_id))
    case = await change_priority(
        case_id=case_id, body=body, session=session, user=user)
    return CaseResponse.model_validate(case).model_dump(mode="json")


async def _case_link(session: AsyncSession, user: AuthenticatedUser, args: dict) -> dict:
    """Link two cases: case.update on the source (it is being modified) and
    case.read on the target (else linking becomes an existence oracle). The
    REST endpoint now enforces the same checks itself (route-migration
    2026-07-09); the tool keeps its own — defence in depth."""
    from case_service import hxguard
    from case_service.api.routers.cases import add_relationship
    from case_service.api.schemas.cases import RelationshipCreate

    case_id = _arg_uuid(args, "case_id")
    target_case_id = _arg_uuid(args, "target_case_id")
    rel_type = _arg_opt_str(args, "relationship_type", max_len=100) or "related"

    await hxguard.require_case(session, user, "case.update", case_id)
    await hxguard.require_case(session, user, "case.read", target_case_id)

    body = RelationshipCreate(target_case_id=target_case_id, relationship_type=rel_type)
    rel = await add_relationship(case_id=case_id, body=body, session=session, user=user)
    # add_relationship returns an ORM model — serialize via its response schema
    from case_service.api.schemas.cases import RelationshipResponse
    return RelationshipResponse.model_validate(rel).model_dump(mode="json")


# ── Stateful tool handlers (P3) ───────────────────────────────────────
# Lifecycle actions. Higher-risk than P2; behind mcp_stateful_enabled and,
# by default, a human-confirm proposal (server.py). The tools carry their own
# confused-deputy checks; since the 2026-07-09 route-migration the wrapped
# REST handlers enforce the same checks too (defence in depth).


async def _case_advance_stage(session: AsyncSession, user: AuthenticatedUser, args: dict) -> dict:
    from case_service import hxguard
    from case_service.api.routers.cases import transition_stage
    from case_service.api.schemas.cases import CaseResponse, CaseStageTransition

    case_id = _arg_uuid(args, "case_id")
    target = _arg_opt_str(args, "target_stage_id", max_len=200)
    if not target:
        raise ToolArgError("'target_stage_id' is required")
    await hxguard.require_case(session, user, "case.update", case_id)   # defence in depth
    body = CaseStageTransition(target_stage_id=target, actor_id=str(user.user_id))
    case = await transition_stage(
        case_id=case_id, body=body, request=_dummy_request(), session=session, user=user)
    return CaseResponse.model_validate(case).model_dump(mode="json")


async def _case_set_status(session: AsyncSession, user: AuthenticatedUser, args: dict) -> dict:
    from case_service.api.routers.cases import change_status
    from case_service.api.schemas.cases import CaseResponse, CaseStatusChange

    case_id = _arg_uuid(args, "case_id")
    status = _arg_opt_str(args, "status", max_len=50)
    if not status:
        raise ToolArgError("'status' is required")
    body = CaseStatusChange(status=status, reason=_arg_opt_str(args, "reason", max_len=500),
                            actor_id=str(user.user_id))
    case = await change_status(case_id=case_id, body=body, request=_dummy_request(),
                               session=session, user=user)
    return CaseResponse.model_validate(case).model_dump(mode="json")


async def _case_close(session: AsyncSession, user: AuthenticatedUser, args: dict) -> dict:
    from case_service.api.routers.cases import close_case
    from case_service.api.schemas.cases import CaseAction, CaseResponse

    case_id = _arg_uuid(args, "case_id")
    body = CaseAction(reason=_arg_opt_str(args, "reason", max_len=500),
                      actor_id=str(user.user_id))
    case = await close_case(case_id=case_id, body=body, request=_dummy_request(),
                            session=session, user=user)
    return CaseResponse.model_validate(case).model_dump(mode="json")


async def _case_create(session: AsyncSession, user: AuthenticatedUser, args: dict) -> dict:
    from case_service.api.routers.cases import create_case
    from case_service.api.schemas.cases import CaseCreate, CaseResponse

    case_type_id = _arg_uuid(args, "case_type_id")
    data = args.get("data") or {}
    if not isinstance(data, dict) or len(data) > 50:
        raise ToolArgError("'data' must be an object with at most 50 entries")
    priority = _arg_opt_str(args, "priority", max_len=20)
    if priority is not None and priority not in ("low", "medium", "high", "critical"):
        raise ToolArgError("'priority' must be one of: low, medium, high, critical")
    body = CaseCreate(case_type_id=case_type_id, data=data, priority=priority)
    case = await create_case(body=body, request=_dummy_request(), session=session, user=user)
    return CaseResponse.model_validate(case).model_dump(mode="json")


# ── Registry ──────────────────────────────────────────────────────────

Handler = Callable[[AsyncSession, AuthenticatedUser, dict], Awaitable[dict]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Handler
    ai: bool = False        # True = backed by an LLM; hidden when mcp_ai_tools=False
    mutating: bool = False  # True = write; needs an idempotency_key
    stateful: bool = False  # True = P3 lifecycle; gated on mcp_stateful_enabled + human-confirm


_UUID_ARG = {"type": "string", "format": "uuid"}
_LIMIT_ARG = {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": 20}
# Every mutating tool requires this — server.py enforces idempotency on it.
_IDEMPOTENCY_ARG = {
    "type": "string", "minLength": 1, "maxLength": 255,
    "description": "Caller-generated unique key; a retry with the same key "
                   "replays the first result instead of re-applying the write.",
}

TOOLS: dict[str, Tool] = {t.name: t for t in (
    Tool(
        name="case_search",
        description=(
            "Search case instances the calling user can access. Returns case "
            "identity/status fields only (use case_get for a case's variables)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "Filter by case status"},
                "priority": {"type": "string", "description": "Filter by priority"},
                "case_type_id": _UUID_ARG,
                "limit": _LIMIT_ARG,
            },
        },
        handler=_case_search,
    ),
    Tool(
        name="case_get",
        description=(
            "Fetch one case by id, including its variables (sensitivity-redacted "
            "for the calling user, exactly as the REST API returns them)."
        ),
        input_schema={
            "type": "object",
            "properties": {"case_id": _UUID_ARG},
            "required": ["case_id"],
        },
        handler=_case_get,
    ),
    Tool(
        name="case_timeline",
        description="Fetch a case's audit history (who did what, when).",
        input_schema={
            "type": "object",
            "properties": {"case_id": _UUID_ARG},
            "required": ["case_id"],
        },
        handler=_case_timeline,
    ),
    Tool(
        name="case_type_list",
        description="List workflow definitions (case types) visible to the calling user.",
        input_schema={
            "type": "object",
            "properties": {"limit": _LIMIT_ARG},
        },
        handler=_case_type_list,
    ),
    Tool(
        name="case_type_get",
        description="Fetch one case type including its full workflow definition.",
        input_schema={
            "type": "object",
            "properties": {"case_type_id": _UUID_ARG},
            "required": ["case_type_id"],
        },
        handler=_case_type_get,
    ),
    Tool(
        name="graph_query",
        description=(
            "Ask a natural-language question over the HxGraph knowledge graph "
            "(architecture, workflows, concepts and how they relate)."
        ),
        input_schema={
            "type": "object",
            "properties": {"question": {"type": "string", "maxLength": MAX_QUESTION_CHARS}},
            "required": ["question"],
        },
        handler=_graph_query,
        ai=True,
    ),
    # ── Mutating tools (P2) — hidden + rejected unless mcp_writes_enabled ──
    Tool(
        name="case_update_data",
        description=(
            "Merge-update a case's variables (set or correct field values). "
            "Requires case.update on the case; every change is audited with its "
            "before-image and is reversible."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "case_id": _UUID_ARG,
                "data": {"type": "object", "description": "variable name -> value"},
                "idempotency_key": _IDEMPOTENCY_ARG,
            },
            "required": ["case_id", "data", "idempotency_key"],
        },
        handler=_case_update_data,
        mutating=True,
    ),
    Tool(
        name="case_set_priority",
        description="Set a case's priority (low/medium/high/critical). Requires case.update.",
        input_schema={
            "type": "object",
            "properties": {
                "case_id": _UUID_ARG,
                "priority": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
                "idempotency_key": _IDEMPOTENCY_ARG,
            },
            "required": ["case_id", "priority", "idempotency_key"],
        },
        handler=_case_set_priority,
        mutating=True,
    ),
    Tool(
        name="case_link",
        description=(
            "Link a case to a related case. Requires case.update on the source "
            "case and case.read on the target."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "case_id": _UUID_ARG,
                "target_case_id": _UUID_ARG,
                "relationship_type": {"type": "string", "maxLength": 100,
                                      "description": "e.g. related, duplicate, blocks"},
                "idempotency_key": _IDEMPOTENCY_ARG,
            },
            "required": ["case_id", "target_case_id", "idempotency_key"],
        },
        handler=_case_link,
        mutating=True,
    ),
    # ── Stateful lifecycle tools (P3) — hidden unless mcp_stateful_enabled;
    #    by default each call becomes a human-confirm proposal (server.py) ──
    Tool(
        name="case_advance_stage",
        description="Advance a case to a target stage. Requires case.update.",
        input_schema={
            "type": "object",
            "properties": {
                "case_id": _UUID_ARG,
                "target_stage_id": {"type": "string", "maxLength": 200},
                "idempotency_key": _IDEMPOTENCY_ARG,
            },
            "required": ["case_id", "target_stage_id", "idempotency_key"],
        },
        handler=_case_advance_stage,
        mutating=True, stateful=True,
    ),
    Tool(
        name="case_set_status",
        description="Set a case's lifecycle status (e.g. in_progress, on_hold). Requires case.update.",
        input_schema={
            "type": "object",
            "properties": {
                "case_id": _UUID_ARG,
                "status": {"type": "string", "maxLength": 50},
                "reason": {"type": "string", "maxLength": 500},
                "idempotency_key": _IDEMPOTENCY_ARG,
            },
            "required": ["case_id", "status", "idempotency_key"],
        },
        handler=_case_set_status,
        mutating=True, stateful=True,
    ),
    Tool(
        name="case_close",
        description="Close a case. Requires case.update.",
        input_schema={
            "type": "object",
            "properties": {
                "case_id": _UUID_ARG,
                "reason": {"type": "string", "maxLength": 500},
                "idempotency_key": _IDEMPOTENCY_ARG,
            },
            "required": ["case_id", "idempotency_key"],
        },
        handler=_case_close,
        mutating=True, stateful=True,
    ),
    Tool(
        name="case_create",
        description="Create a new case of a given case type. Requires an authenticated user.",
        input_schema={
            "type": "object",
            "properties": {
                "case_type_id": _UUID_ARG,
                "data": {"type": "object", "description": "initial variables"},
                "priority": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
                "idempotency_key": _IDEMPOTENCY_ARG,
            },
            "required": ["case_type_id", "idempotency_key"],
        },
        handler=_case_create,
        mutating=True, stateful=True,
    ),
)}


def visible_tools(scope: set[str] | None = None) -> list[Tool]:
    """The advertised tool profile (all default-deny — a hidden tool is not
    callable either, enforced in server.py via this same list):
      - deterministic-only when AI tools are off,
      - P2 writes hidden when writes are off,
      - P3 stateful actions hidden when stateful is off,
      - a scoped token (P4 external agent) sees only its granted intersection
        of the above — a grant can never resurface a globally disabled tool."""
    from case_service.config import get_settings
    settings = get_settings()
    tools = list(TOOLS.values())
    if not settings.mcp_ai_tools:
        tools = [t for t in tools if not t.ai]
    if not settings.mcp_writes_enabled:
        tools = [t for t in tools if not (t.mutating and not t.stateful)]
    if not settings.mcp_stateful_enabled:
        tools = [t for t in tools if not t.stateful]
    if scope is not None:
        tools = [t for t in tools if t.name in scope]
    return tools
