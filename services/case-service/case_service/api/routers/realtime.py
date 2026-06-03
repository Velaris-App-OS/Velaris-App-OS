"""Real-time collaboration WebSocket endpoint.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect, Query

from case_service.auth.dependencies import get_current_user
from case_service.auth.jwt_handler import decode_jwt_token
from case_service.config import get_settings
from case_service.realtime.manager import get_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/realtime", tags=["realtime"])


@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    token: str | None = Query(None),
    user_id: str = Query("anonymous"),
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
    # Validate token before accepting the WebSocket connection
    authenticated_user_id = user_id
    if token:
        try:
            settings = get_settings()
            claims = decode_jwt_token(token, secret=settings.auth_secret,
                                      issuer=settings.auth_issuer, audience=settings.auth_audience)
            authenticated_user_id = claims.get("sub", user_id)
        except Exception:
            await websocket.close(code=4001, reason="Invalid token")
            return
    else:
        await websocket.close(code=4001, reason="Authentication required")
        return

    manager = get_manager()
    conn = await manager.connect(websocket, user_id=authenticated_user_id)
    presence_resources = set()

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
                if channel:
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
                if resource:
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
        for resource in presence_resources:
            await manager.clear_presence(resource, authenticated_user_id)
        await manager.disconnect(conn)


@router.get("/presence/{resource:path}")
async def get_presence(resource: str, _=Depends(get_current_user)):
    """Get users currently present on a resource."""
    manager = get_manager()
    users = await manager.get_presence(resource)
    return {"resource": resource, "users": users, "count": len(users)}


@router.get("/stats")
async def get_stats(_=Depends(get_current_user)):
    """WebSocket connection statistics."""
    manager = get_manager()
    return manager.stats()


@router.post("/broadcast")
async def manual_broadcast(
    channel: str,
    message: dict,
    _=Depends(get_current_user),
):
    """Manual broadcast endpoint for testing."""
    manager = get_manager()
    sent = await manager.broadcast(channel, message)
    return {"channel": channel, "sent_to": sent}
