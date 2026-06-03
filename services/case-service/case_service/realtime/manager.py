"""WebSocket connection manager and pub/sub for real-time updates.

Channels follow a hierarchical structure:
  cases.*           — all case events
  cases.{id}        — events for a specific case
  assignments.{user_id}  — worklist updates for a user
  presence.{resource}    — active users viewing/editing a resource
  events.global          — global system events

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import defaultdict
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class Connection:
    """A single WebSocket connection with subscription state."""
    def __init__(self, ws: WebSocket, user_id: str | None = None):
        self.id = str(uuid.uuid4())
        self.ws = ws
        self.user_id = user_id
        self.channels: set[str] = set()
        self.connected_at = time.time()
        self.last_activity = time.time()

    async def send(self, message: dict[str, Any]) -> bool:
        """Send a message. Returns False if the connection is closed."""
        try:
            await self.ws.send_text(json.dumps(message, default=str))
            self.last_activity = time.time()
            return True
        except Exception as e:
            logger.debug("Send failed on %s: %s", self.id, e)
            return False


class ConnectionManager:
    """Manages all active WebSocket connections and their subscriptions."""

    def __init__(self):
        self._connections: dict[str, Connection] = {}
        # channel → set of connection_ids
        self._subscriptions: dict[str, set[str]] = defaultdict(set)
        self._lock = asyncio.Lock()
        # Presence: resource → { user_id: last_seen_ts }
        self._presence: dict[str, dict[str, float]] = defaultdict(dict)
        # P32 Redis bridge (set via attach_redis_bridge)
        self._redis_bridge = None

    async def connect(self, ws: WebSocket, user_id: str | None = None) -> Connection:
        await ws.accept()
        conn = Connection(ws, user_id)
        async with self._lock:
            self._connections[conn.id] = conn
        logger.debug("Connected: %s (user=%s)", conn.id, user_id)
        return conn

    async def disconnect(self, conn: Connection) -> None:
        async with self._lock:
            self._connections.pop(conn.id, None)
            for chan in list(conn.channels):
                self._subscriptions[chan].discard(conn.id)
                if not self._subscriptions[chan]:
                    del self._subscriptions[chan]
            # Remove from presence
            for resource, users in list(self._presence.items()):
                if conn.user_id and conn.user_id in users:
                    del users[conn.user_id]
        logger.debug("Disconnected: %s", conn.id)

    async def subscribe(self, conn: Connection, channel: str) -> None:
        async with self._lock:
            conn.channels.add(channel)
            self._subscriptions[channel].add(conn.id)

    async def unsubscribe(self, conn: Connection, channel: str) -> None:
        async with self._lock:
            conn.channels.discard(channel)
            self._subscriptions[channel].discard(conn.id)
            if not self._subscriptions[channel]:
                del self._subscriptions[channel]

    async def _local_fanout(self, channel: str, envelope: dict[str, Any]) -> int:
        """Fan-out an already-enveloped message to local subscribers only (no Redis)."""
        matching_channels = {channel}
        parts = channel.split(".")
        for i in range(1, len(parts) + 1):
            wild = ".".join(parts[:i-1] + ["*"]) if i > 0 else "*"
            matching_channels.add(wild)
        matching_channels.add("events.global")

        conn_ids: set[str] = set()
        async with self._lock:
            for chan in matching_channels:
                conn_ids.update(self._subscriptions.get(chan, set()))

        sent = 0
        dead = []
        for cid in conn_ids:
            conn = self._connections.get(cid)
            if conn:
                ok = await conn.send(envelope)
                if ok:
                    sent += 1
                else:
                    dead.append(conn)
        for conn in dead:
            await self.disconnect(conn)
        return sent

    async def broadcast(self, channel: str, message: dict[str, Any]) -> int:
        """Send a message to all subscribers of a channel. Also matches wildcards.

        e.g. broadcasting to "cases.abc123" also reaches subscribers of "cases.*".
        """
        envelope = {
            "channel": channel,
            "timestamp": time.time(),
            "data": message,
        }
        sent = await self._local_fanout(channel, envelope)
        if self._redis_bridge is not None:
            await self._redis_bridge.publish(channel, envelope)
        return sent

    def attach_redis_bridge(self, bridge) -> None:
        """Attach a RedisBridge — enables cross-instance broadcasting."""
        self._redis_bridge = bridge

    async def set_presence(
        self, resource: str, user_id: str, action: str = "viewing",
    ) -> None:
        """Mark a user as present on a resource (viewing/editing)."""
        async with self._lock:
            self._presence[resource][user_id] = time.time()

        # Broadcast to presence channel
        await self.broadcast(f"presence.{resource}", {
            "type": "user_joined",
            "user_id": user_id,
            "action": action,
            "resource": resource,
        })

    async def clear_presence(self, resource: str, user_id: str) -> None:
        async with self._lock:
            if resource in self._presence:
                self._presence[resource].pop(user_id, None)

        await self.broadcast(f"presence.{resource}", {
            "type": "user_left",
            "user_id": user_id,
            "resource": resource,
        })

    async def get_presence(self, resource: str) -> list[str]:
        """Get list of user_ids currently present on a resource (within 5 min)."""
        now = time.time()
        cutoff = now - 300  # 5 minutes
        async with self._lock:
            users = self._presence.get(resource, {})
            active = [u for u, ts in users.items() if ts >= cutoff]
            # Prune old entries
            self._presence[resource] = {u: ts for u, ts in users.items() if ts >= cutoff}
        return active

    def stats(self) -> dict[str, Any]:
        return {
            "connections": len(self._connections),
            "subscriptions": {ch: len(s) for ch, s in self._subscriptions.items()},
            "presence_resources": len(self._presence),
        }


# Global singleton
_manager: ConnectionManager | None = None


def get_manager() -> ConnectionManager:
    global _manager
    if _manager is None:
        _manager = ConnectionManager()
    return _manager
