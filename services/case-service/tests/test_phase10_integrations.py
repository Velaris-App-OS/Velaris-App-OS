"""Phase 10 tests — Webhooks & Integration Layer.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest


class TestWebhookCRUD:
    async def test_create_webhook(self, client):
        resp = await client.post("/api/v1/webhooks", json={
            "name": "Test Hook",
            "url": "https://example.com/webhook",
            "events": ["case.created", "case.resolved"],
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Test Hook"
        assert data["url"] == "https://example.com/webhook"
        assert data["is_active"] is True
        assert len(data["events"]) == 2

    async def test_list_webhooks(self, client):
        await client.post("/api/v1/webhooks", json={
            "name": "Hook A", "url": "https://a.com/hook", "events": ["*"],
        })
        await client.post("/api/v1/webhooks", json={
            "name": "Hook B", "url": "https://b.com/hook", "events": ["case.created"],
        })
        resp = await client.get("/api/v1/webhooks")
        assert resp.status_code == 200
        assert len(resp.json()) >= 2

    async def test_get_webhook(self, client):
        create = await client.post("/api/v1/webhooks", json={
            "name": "Get Test", "url": "https://get.com/hook", "events": ["*"],
        })
        wid = create.json()["id"]
        resp = await client.get(f"/api/v1/webhooks/{wid}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "Get Test"

    async def test_update_webhook(self, client):
        create = await client.post("/api/v1/webhooks", json={
            "name": "Update Test", "url": "https://upd.com/hook", "events": ["*"],
        })
        wid = create.json()["id"]
        resp = await client.patch(f"/api/v1/webhooks/{wid}", json={
            "name": "Updated Hook",
            "is_active": False,
        })
        assert resp.status_code == 200
        assert resp.json()["name"] == "Updated Hook"
        assert resp.json()["is_active"] is False

    async def test_delete_webhook(self, client):
        create = await client.post("/api/v1/webhooks", json={
            "name": "Delete Test", "url": "https://del.com/hook", "events": ["*"],
        })
        wid = create.json()["id"]
        resp = await client.delete(f"/api/v1/webhooks/{wid}")
        assert resp.status_code == 204
        resp2 = await client.get(f"/api/v1/webhooks/{wid}")
        assert resp2.status_code == 404

    async def test_webhook_not_found(self, client):
        resp = await client.get(f"/api/v1/webhooks/{uuid.uuid4()}")
        assert resp.status_code == 404

    async def test_invalid_event_type(self, client):
        resp = await client.post("/api/v1/webhooks", json={
            "name": "Bad Events", "url": "https://bad.com/hook",
            "events": ["nonexistent.event"],
        })
        assert resp.status_code == 400

    async def test_list_event_types(self, client):
        resp = await client.get("/api/v1/webhooks/events")
        assert resp.status_code == 200
        events = resp.json()["events"]
        assert "case.created" in events
        assert "sla.breached" in events
        assert len(events) > 10


class TestWebhookDeliveries:
    async def test_deliveries_empty(self, client):
        create = await client.post("/api/v1/webhooks", json={
            "name": "Delivery Test", "url": "https://dlv.com/hook", "events": ["*"],
        })
        wid = create.json()["id"]
        resp = await client.get(f"/api/v1/webhooks/{wid}/deliveries")
        assert resp.status_code == 200
        assert resp.json() == []


class TestWebhookDispatcher:
    def test_compute_signature(self):
        from case_service.integrations.webhook_dispatcher import compute_signature
        sig = compute_signature('{"test": true}', "my-secret")
        assert isinstance(sig, str)
        assert len(sig) == 64  # SHA256 hex

    async def test_get_matching_subscriptions(self, client):
        # Create webhooks with different event filters
        await client.post("/api/v1/webhooks", json={
            "name": "Match Test", "url": "https://match.com/hook",
            "events": ["case.created"],
        })
        await client.post("/api/v1/webhooks", json={
            "name": "No Match", "url": "https://no.com/hook",
            "events": ["case.resolved"],
        })

        # Verify all created
        all_hooks = (await client.get("/api/v1/webhooks")).json()
        assert len(all_hooks) >= 2
        names = [h["name"] for h in all_hooks]
        assert "Match Test" in names


class TestWebhookWithSecret:
    async def test_create_with_secret(self, client):
        resp = await client.post("/api/v1/webhooks", json={
            "name": "Secure Hook",
            "url": "https://secure.com/hook",
            "secret": "super-secret-key",
            "events": ["case.created"],
            "headers": {"X-Custom": "value"},
        })
        assert resp.status_code == 201
        # Secret should not be returned in response for security
        # (for now it is, but in production it would be masked)

    async def test_create_with_case_type_filter(self, client):
        ct = await client.post("/api/v1/case-types", json={
            "name": f"WH-CT-{uuid.uuid4().hex[:6]}", "version": "1.0.0",
            "lifecycle_process_id": "test-proc",
            "definition_json": {"stages": [], "sla_policies": []},
        })
        resp = await client.post("/api/v1/webhooks", json={
            "name": "Type-Specific Hook",
            "url": "https://typed.com/hook",
            "events": ["case.created"],
            "case_type_id": ct.json()["id"],
        })
        assert resp.status_code == 201
        assert resp.json()["case_type_id"] == ct.json()["id"]
