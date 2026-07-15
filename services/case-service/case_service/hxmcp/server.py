"""Minimal MCP server core — stateless Streamable-HTTP, JSON-RPC 2.0 subset.

Deliberately hand-rolled (no SDK dependency): P1 needs exactly four methods
— initialize, ping, tools/list, tools/call — and fail-closed handling of
everything else. Same in-house posture as safe_expression.

Protocol notes (MCP spec 2025-06-18, stateless mode):
- notifications (no "id") are acknowledged with no body by the transport
- JSON-RPC batching is not supported (removed from the spec) — arrays are
  rejected as invalid requests
- tool *execution* failures are results with isError=true, not protocol
  errors; the anti-probing 404 detail for denied case reads passes through
  verbatim so MCP can't be used as an existence oracle

Copyright (c) 2024-2026 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.auth.models import AuthenticatedUser
from case_service.hxmcp.registry import Tool, ToolArgError, visible_tools

log = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "velaris-hxnexus-operator", "version": "1.0.0"}

# JSON-RPC 2.0 error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def _error(msg_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _result(msg_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _tool_result(msg_id: Any, payload: dict, *, is_error: bool = False) -> dict:
    """CallToolResult: content is DATA the client renders — never instructions."""
    return _result(msg_id, {
        "content": [{"type": "text", "text": json.dumps(payload, default=str)}],
        "isError": is_error,
    })


async def handle_message(
    session: AsyncSession,
    user: AuthenticatedUser,
    message: Any,
) -> dict | None:
    """Process one JSON-RPC message. Returns the response, or None for
    notifications (transport answers 202 with no body)."""
    if not isinstance(message, dict):
        # includes arrays: batching is not part of the supported spec
        return _error(None, INVALID_REQUEST, "expected a single JSON-RPC request object")

    msg_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}

    if message.get("jsonrpc") != "2.0" or not isinstance(method, str):
        return _error(msg_id, INVALID_REQUEST, "not a JSON-RPC 2.0 request")
    if not isinstance(params, dict):
        return _error(msg_id, INVALID_PARAMS, "params must be an object")

    if msg_id is None:                       # notification — acknowledge silently
        return None

    if method == "initialize":
        return _result(msg_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": (
                "Read-only Velaris tool surface. Every call is authorized as "
                "the authenticated user — the AI can never see or do more "
                "than that user could in the product."
            ),
        })

    if method == "ping":
        return _result(msg_id, {})

    if method == "tools/list":
        from case_service.hxmcp.tokens import caller_scope
        return _result(msg_id, {"tools": [
            {"name": t.name, "description": t.description, "inputSchema": t.input_schema}
            for t in visible_tools(caller_scope(user))
        ]})

    if method == "tools/call":
        return await _call_tool(session, user, msg_id, params)

    return _error(msg_id, METHOD_NOT_FOUND, f"method '{method}' not supported")


async def _call_tool(
    session: AsyncSession, user: AuthenticatedUser, msg_id: Any, params: dict,
) -> dict:
    name = params.get("name")
    args = params.get("arguments") or {}
    if not isinstance(name, str):
        return _error(msg_id, INVALID_PARAMS, "'name' (string) is required")
    if not isinstance(args, dict):
        return _error(msg_id, INVALID_PARAMS, "'arguments' must be an object")

    # default-deny: only the currently visible profile is callable, so a
    # hidden AI-backed or write tool can't be invoked by guessing its name;
    # a scoped token (P4) is additionally cut to its granted intersection —
    # out-of-scope reads identically to nonexistent (anti-oracle)
    from case_service.hxmcp.tokens import caller_scope
    tool: Tool | None = next(
        (t for t in visible_tools(caller_scope(user)) if t.name == name), None)
    if tool is None:
        return _error(msg_id, INVALID_PARAMS, f"unknown tool '{name}'")

    if tool.stateful:
        return await _call_stateful_tool(session, user, msg_id, tool, args)
    if tool.mutating:
        return await _call_mutating_tool(session, user, msg_id, tool, args)
    return await _run_tool(session, user, msg_id, tool, args)


async def _run_tool(
    session: AsyncSession, user: AuthenticatedUser, msg_id: Any, tool: Tool, args: dict,
) -> dict:
    """Execute a tool handler and map its outcome to an MCP result/error."""
    started = time.monotonic()
    try:
        payload = await tool.handler(session, user, args)
    except ToolArgError as exc:
        return _error(msg_id, INVALID_PARAMS, str(exc))
    except HTTPException as exc:
        # authz / not-found from the wrapped surface — a tool-level error,
        # relayed with the REST detail (404 anti-oracle wording included)
        log.info("hxmcp tool=%s user=%s denied status=%s", tool.name, user.user_id, exc.status_code)
        return _tool_result(msg_id, {"error": exc.detail, "status": exc.status_code},
                            is_error=True)
    except Exception:
        log.exception("hxmcp tool=%s user=%s failed", tool.name, user.user_id)
        return _error(msg_id, INTERNAL_ERROR, "tool execution failed")

    log.info("hxmcp tool=%s user=%s ok in %.0fms",
             tool.name, user.user_id, (time.monotonic() - started) * 1000)
    return _tool_result(msg_id, payload)


async def _call_mutating_tool(
    session: AsyncSession, user: AuthenticatedUser, msg_id: Any, tool: Tool, args: dict,
) -> dict:
    """Wrap a write tool in the durable idempotency protocol (P2)."""
    from case_service.hxmcp import idempotency

    key = args.get("idempotency_key")
    if not isinstance(key, str) or not (1 <= len(key) <= 255):
        return _error(msg_id, INVALID_PARAMS,
                      "'idempotency_key' (1-255 char string) is required for writes")
    uid = str(user.user_id)
    req_hash = idempotency.request_hash(tool.name, args)

    try:
        replay = await idempotency.claim(uid, key, tool.name, req_hash)
    except idempotency.IdempotencyConflict as exc:
        return _error(msg_id, INVALID_PARAMS, str(exc))
    if replay is not None:                       # already applied — replay verbatim
        log.info("hxmcp tool=%s user=%s idempotent-replay key=%s", tool.name, uid, key)
        return _tool_result(msg_id, replay.response, is_error=replay.is_error)

    # Claimed. The key is consumed ONLY by a successful application: a genuine
    # write commits, so we store the response and a retry replays it. Every
    # non-success — a protocol error, or a tool-error result (authz denial /
    # bad args) where the wrapped handler rolled back and applied nothing —
    # releases the claim, so a legitimate retry can re-run.
    result = await _run_tool(session, user, msg_id, tool, args)
    tool_res = result.get("result")
    applied = tool_res is not None and not tool_res.get("isError")
    if applied:
        await idempotency.complete(uid, key, _extract_payload(tool_res), is_error=False)
    else:
        await idempotency.release(uid, key)
    return result


def _extract_payload(tool_res: dict) -> dict:
    """Recover the JSON payload dict from a CallToolResult for storage."""
    try:
        return json.loads(tool_res["content"][0]["text"])
    except (KeyError, IndexError, ValueError, TypeError):
        return {}


async def _call_stateful_tool(
    session: AsyncSession, user: AuthenticatedUser, msg_id: Any, tool: Tool, args: dict,
) -> dict:
    """P3 lifecycle tool. Requires an idempotency_key. When human-confirm is
    on (default), the call is recorded as a proposal and NOT executed — a human
    confirms it via the proposals API. When off, it executes immediately with
    the same idempotency protection as a P2 write."""
    from case_service.config import get_settings
    from case_service.hxmcp import proposals

    key = args.get("idempotency_key")
    if not isinstance(key, str) or not (1 <= len(key) <= 255):
        return _error(msg_id, INVALID_PARAMS,
                      "'idempotency_key' (1-255 char string) is required for writes")

    if get_settings().mcp_confirm_stateful:
        envelope = await proposals.create_proposal(session, user, tool.name, args)
        log.info("hxmcp stateful tool=%s user=%s proposed id=%s",
                 tool.name, user.user_id, envelope["proposal_id"])
        return _tool_result(msg_id, envelope)

    return await _call_mutating_tool(session, user, msg_id, tool, args)
