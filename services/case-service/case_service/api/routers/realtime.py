"""Real-time collaboration WebSocket endpoint.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect, Query

from velaris_shared import hxguard
from fastapi import HTTPException

from velaris_shared.auth.dependencies import (
    WS_CLOSE_UNAUTHORIZED, authenticate_websocket, get_current_user,
    watch_websocket_auth, websocket_close_code,
)
from case_service.core.tenant_path_guard import platform_operator_only
from case_service.realtime.manager import get_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/realtime", tags=["realtime"])


class _Caller:
    """Who holds this socket — enough to authorize a channel."""

    def __init__(self, user_id: str, roles: list[str], tenant: str | None):
        self.user_id, self.roles, self.tenant_id = user_id, roles, tenant


async def authorize_channel(caller: _Caller, channel: str) -> bool:
    """May this socket hear this channel? — 8d, 2026-09-29.

    Until today any signed-in user could subscribe to any channel: `cases.<id>`
    of another tenant's case (its events carry changed data), another user's
    `assignments.<id>` worklist, or `cases.*` — every tenant's case events.
    """
    from velaris_shared.db.models import CaseInstanceModel
    from velaris_shared.db.session import get_session_factory
    from velaris_shared.tenancy.identity import (
        is_platform_operator, is_superadmin, same_tenant,
    )

    if is_superadmin(caller):
        return True
    parts = channel.split(".", 1)
    async with get_session_factory()() as session:
        if "*" in channel:
            return await is_platform_operator(session, caller)
        head, rest = parts[0], (parts[1] if len(parts) > 1 else "")
        if head == "assignments":
            return rest == caller.user_id
        if head == "presence":
            head, rest = ("cases", rest.split(":", 1)[1]) if rest.startswith("case:") else ("", "")
        if head == "cases":
            import uuid as _uuid
            try:
                case = await session.get(CaseInstanceModel, _uuid.UUID(rest))
            except ValueError:
                return False
            # case_visible_to, not same_tenant: a tenant-less case follows the
            # strict-reads switch here as on every other read (2026-11-04).
            from velaris_shared.tenancy.identity import case_visible_to
            return case is not None and await case_visible_to(session, case.tenant_id,
                                                              caller.tenant_id)
    return True     # a channel nothing tenant-owned is published on


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    token: str | None = Query(None),
    user_id: str = Query("anonymous"),   # ignored: the token names the user
):
    """WebSocket endpoint for real-time collaboration.

    Client protocol (JSON messages):

      Subscribe:    {"type": "subscribe", "channel": "cases.abc123"}
      Unsubscribe:  {"type": "unsubscribe", "channel": "cases.abc123"}
      Presence:     {"type": "presence", "resource": "case:abc123", "action": "viewing"}
      Ping:         {"type": "ping"}

    Server messages:

      Event:   {"channel": "...", "timestamp": 1234, "data": {...}}
      Pong:    {"type": "pong"}
      Error:   {"type": "error", "message": "..."}
    """
    # The same sign-in check as every HTTP route (2026-10-19; until then the
    # signature only — a revoked token, a suspended account or an agent token
    # got a socket). Refused before the socket is accepted.
    if not token:
        await websocket.close(code=WS_CLOSE_UNAUTHORIZED, reason="Authentication required")
        return
    try:
        user = await authenticate_websocket(websocket, token)
    except HTTPException as exc:
        await websocket.close(code=websocket_close_code(exc), reason="Invalid token")
        return
    authenticated_user_id = user.user_id
    caller = _Caller(user.user_id, list(user.roles or []), user.tenant_id)

    manager = get_manager()
    conn = await manager.connect(websocket, user_id=authenticated_user_id)
    presence_resources = set()
    # Checked again while open: a revocation closes the socket within a minute.
    watcher = asyncio.create_task(watch_websocket_auth(websocket, token))

    try:
        # Send welcome
        await conn.send({
            "type": "connected",
            "connection_id": conn.id,
            "user_id": authenticated_user_id,
        })

        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await conn.send({"type": "error", "message": "Invalid JSON"})
                continue

            mtype = msg.get("type")

            if mtype == "subscribe":
                channel = msg.get("channel")
                if channel and not await authorize_channel(caller, str(channel)):
                    # The same answer as a channel that does not exist.
                    await conn.send({"type": "error", "message": "Unknown channel"})
                elif channel:
                    await manager.subscribe(conn, channel)
                    await conn.send({"type": "subscribed", "channel": channel})

            elif mtype == "unsubscribe":
                channel = msg.get("channel")
                if channel:
                    await manager.unsubscribe(conn, channel)
                    await conn.send({"type": "unsubscribed", "channel": channel})

            elif mtype == "presence":
                resource = msg.get("resource")
                action = msg.get("action", "viewing")
                if resource and await authorize_channel(caller, f"presence.{resource}"):
                    await manager.set_presence(resource, authenticated_user_id, action)
                    presence_resources.add(resource)

            elif mtype == "ping":
                await conn.send({"type": "pong"})

            else:
                await conn.send({"type": "error", "message": f"Unknown type: {mtype}"})

    except WebSocketDisconnect:
        logger.debug("Client disconnected: %s", conn.id)
    except Exception as e:
        logger.warning("WebSocket error on %s: %s", conn.id, e)
    finally:
        watcher.cancel()
        for resource in presence_resources:
            await manager.clear_presence(resource, authenticated_user_id)
        await manager.disconnect(conn)


@router.get("/presence/{resource:path}")
async def get_presence(resource: str, _=Depends(get_current_user)):
    """Get users currently present on a resource."""
    if not await authorize_channel(_Caller(_.user_id, _.roles, _.tenant_id),
                                   f"presence.{resource}"):
        from fastapi import HTTPException
        raise HTTPException(404, "Not found")
    manager = get_manager()
    users = await manager.get_presence(resource)
    return {"resource": resource, "users": users, "count": len(users)}


@router.get("/stats")
async def get_stats(
    _=Depends(hxguard.guard("platform.realtime.read")),
    __=Depends(platform_operator_only),
):
    """WebSocket connection statistics.

    Channel names carry case ids from every tenant on the install. A login was
    the only gate until 2026-09-30; it is the operator's view now, the same as
    the broadcast below."""
    manager = get_manager()
    return manager.stats()


@router.post("/broadcast")
async def manual_broadcast(
    channel: str,
    message: dict,
    _=Depends(get_current_user),
    __=Depends(platform_operator_only),
):
    """Manual broadcast endpoint for testing.

    Any signed-in user could push any message into any channel — onto another
    tenant's open case screens — until 2026-09-29 (8d). Operator only now.
    """
    manager = get_manager()
    sent = await manager.broadcast(channel, message)
    return {"channel": channel, "sent_to": sent}
