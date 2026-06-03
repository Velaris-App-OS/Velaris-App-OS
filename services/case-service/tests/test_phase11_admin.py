"""Phase 11 tests — Admin Console API.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations
import uuid
import pytest


class TestSystemInfo:
    async def test_system_info_empty(self, client):
        resp = await client.get("/api/v1/admin/system-info")
        assert resp.status_code == 200
        data = resp.json()
        assert data["cases"] == 0
        assert "queues" in data
        assert "rules" in data

    async def test_system_info_with_data(self, client):
        await client.post("/api/v1/case-types", json={
            "name": f"SI-{uuid.uuid4().hex[:6]}", "version": "1.0.0",
            "lifecycle_process_id": "test", "definition_json": {"stages": []},
        })
        resp = await client.get("/api/v1/admin/system-info")
        assert resp.json()["case_types"] >= 1


class TestAuditLogSearch:
    async def test_audit_log_empty(self, client):
        resp = await client.get("/api/v1/admin/audit-log")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    async def test_audit_log_with_data(self, client):
        ct = await client.post("/api/v1/case-types", json={
            "name": f"Aud-{uuid.uuid4().hex[:6]}", "version": "1.0.0",
            "lifecycle_process_id": "test", "definition_json": {"stages": []},
        })
        await client.post("/api/v1/cases", json={
            "case_type_id": ct.json()["id"], "data": {},
        })
        resp = await client.get("/api/v1/admin/audit-log")
        assert resp.json()["total"] >= 1
        assert resp.json()["items"][0]["action"] == "created"

    async def test_audit_log_filter_action(self, client):
        ct = await client.post("/api/v1/case-types", json={
            "name": f"AudF-{uuid.uuid4().hex[:6]}", "version": "1.0.0",
            "lifecycle_process_id": "test", "definition_json": {"stages": []},
        })
        case = await client.post("/api/v1/cases", json={
            "case_type_id": ct.json()["id"], "data": {},
        })
        await client.post(f"/api/v1/cases/{case.json()['id']}/resolve", json={})

        resp = await client.get("/api/v1/admin/audit-log?action=resolved")
        assert resp.json()["total"] >= 1
        assert all(i["action"] == "resolved" for i in resp.json()["items"])

    async def test_audit_actions_list(self, client):
        ct = await client.post("/api/v1/case-types", json={
            "name": f"ActL-{uuid.uuid4().hex[:6]}", "version": "1.0.0",
            "lifecycle_process_id": "test", "definition_json": {"stages": []},
        })
        await client.post("/api/v1/cases", json={
            "case_type_id": ct.json()["id"], "data": {},
        })
        resp = await client.get("/api/v1/admin/audit-log/actions")
        assert resp.status_code == 200
        assert "created" in resp.json()["actions"]


class TestQueueAdmin:
    async def test_create_queue(self, client):
        resp = await client.post("/api/v1/admin/queues", json={
            "name": "Test Queue", "description": "For testing",
            "visible_to_roles": ["agent"],
        })
        assert resp.status_code == 201
        assert resp.json()["name"] == "Test Queue"

    async def test_update_queue(self, client):
        create = await client.post("/api/v1/admin/queues", json={"name": "Update Me"})
        qid = create.json()["id"]
        resp = await client.patch(f"/api/v1/admin/queues/{qid}", json={
            "name": "Updated Queue", "auto_assignment": True,
        })
        assert resp.status_code == 200
        assert resp.json()["name"] == "Updated Queue"
        assert resp.json()["auto_assignment"] is True

    async def test_delete_queue(self, client):
        create = await client.post("/api/v1/admin/queues", json={"name": "Delete Me"})
        qid = create.json()["id"]
        resp = await client.delete(f"/api/v1/admin/queues/{qid}")
        assert resp.status_code == 204

    async def test_delete_queue_not_found(self, client):
        resp = await client.delete(f"/api/v1/admin/queues/{uuid.uuid4()}")
        assert resp.status_code == 404


class TestCalendarAdmin:
    async def test_list_calendars(self, client):
        resp = await client.get("/api/v1/admin/calendars")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)
