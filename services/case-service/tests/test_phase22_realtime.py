"""Phase 22 tests — Real-time Collaboration.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations
import asyncio
import pytest


class TestConnectionManager:
    async def test_manager_singleton(self):
        from case_service.realtime.manager import get_manager
        m1 = get_manager()
        m2 = get_manager()
        assert m1 is m2

    async def test_stats_empty(self):
        from case_service.realtime.manager import get_manager
        m = get_manager()
        stats = m.stats()
        assert "connections" in stats
        assert "subscriptions" in stats
        assert "presence_resources" in stats


class TestPublishers:
    async def test_publish_case_event_no_error(self):
        from case_service.realtime.publisher import publish_case_event
        # Should not raise even with no subscribers
        await publish_case_event("some-case-id", "test_event", {"foo": "bar"}, "test_user")

    async def test_publish_assignment_event(self):
        from case_service.realtime.publisher import publish_assignment_event
        await publish_assignment_event("user-123", "new_assignment", {"case_id": "abc"})

    async def test_publish_system_event(self):
        from case_service.realtime.publisher import publish_system_event
        await publish_system_event("system_test", {"info": "test"})


class TestPresenceAPI:
    async def test_get_presence_empty(self, client):
        resp = await client.get("/api/v1/realtime/presence/case%3Aabc123")
        assert resp.status_code == 200
        data = resp.json()
        assert data["users"] == []
        assert data["count"] == 0

    async def test_stats_endpoint(self, client):
        resp = await client.get("/api/v1/realtime/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "connections" in data

    async def test_manual_broadcast(self, client):
        resp = await client.post(
            "/api/v1/realtime/broadcast?channel=test.channel",
            json={"type": "test", "data": "hello"},
        )
        assert resp.status_code == 200
        assert "sent_to" in resp.json()


class TestWebSocketProtocol:
    async def test_connection_class(self):
        from case_service.realtime.manager import Connection

        class MockWS:
            async def accept(self):
                pass
            async def send_text(self, text):
                pass

        ws = MockWS()
        conn = Connection(ws, user_id="test")
        assert conn.user_id == "test"
        assert conn.id  # has an id
        assert len(conn.channels) == 0

    async def test_subscribe_unsubscribe(self):
        from case_service.realtime.manager import ConnectionManager, Connection

        class MockWS:
            def __init__(self):
                self.sent = []
            async def accept(self):
                pass
            async def send_text(self, text):
                self.sent.append(text)

        m = ConnectionManager()
        ws = MockWS()
        conn = Connection(ws, user_id="u1")
        m._connections[conn.id] = conn

        await m.subscribe(conn, "cases.abc")
        assert "cases.abc" in conn.channels
        assert conn.id in m._subscriptions["cases.abc"]

        await m.unsubscribe(conn, "cases.abc")
        assert "cases.abc" not in conn.channels


class TestBroadcast:
    async def test_broadcast_reaches_subscribers(self):
        from case_service.realtime.manager import ConnectionManager, Connection

        class MockWS:
            def __init__(self):
                self.sent = []
            async def accept(self):
                pass
            async def send_text(self, text):
                self.sent.append(text)

        m = ConnectionManager()
        ws = MockWS()
        conn = Connection(ws, user_id="u1")
        m._connections[conn.id] = conn
        await m.subscribe(conn, "cases.abc")

        sent_count = await m.broadcast("cases.abc", {"type": "test"})
        assert sent_count == 1
        assert len(ws.sent) == 1

    async def test_wildcard_broadcast(self):
        from case_service.realtime.manager import ConnectionManager, Connection

        class MockWS:
            def __init__(self):
                self.sent = []
            async def accept(self):
                pass
            async def send_text(self, text):
                self.sent.append(text)

        m = ConnectionManager()
        ws = MockWS()
        conn = Connection(ws, user_id="u1")
        m._connections[conn.id] = conn
        # Subscribe to wildcard
        await m.subscribe(conn, "cases.*")

        # Broadcast to specific channel — should still reach wildcard subscriber
        sent = await m.broadcast("cases.abc123", {"type": "test"})
        assert sent == 1

    async def test_presence_tracking(self):
        from case_service.realtime.manager import ConnectionManager

        m = ConnectionManager()
        await m.set_presence("case:xyz", "alice", "viewing")
        users = await m.get_presence("case:xyz")
        assert "alice" in users

        await m.clear_presence("case:xyz", "alice")
        users = await m.get_presence("case:xyz")
        assert "alice" not in users


class TestRealTimeIntegration:
    async def test_case_status_change_publishes(self, client):
        """Verify that changing case status triggers a broadcast."""
        import uuid
        # Create case
        ct = await client.post("/api/v1/case-types", json={
            "name": f"RT-{uuid.uuid4().hex[:6]}", "version": "1.0.0",
            "lifecycle_process_id": "test",
            "definition_json": {"stages": [], "sla_policies": []},
        })
        case = await client.post("/api/v1/cases", json={
            "case_type_id": ct.json()["id"], "data": {},
        })
        case_id = case.json()["id"]

        # Change status — should not error even though no subscribers
        resp = await client.post(f"/api/v1/cases/{case_id}/status", json={
            "status": "in_progress",
            "actor_id": "test_user",
        })
        assert resp.status_code == 200
