"""Integration tests for the case-service API (Phase 1).

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
import pytest
from tests.conftest import deploy_case_type, create_case


class TestCaseTypes:
    async def test_deploy_and_list(self, client):
        ct = await deploy_case_type(client)
        assert ct["name"] == "Test Case"
        resp = await client.get("/api/v1/case-types")
        assert resp.json()["total"] == 1

    async def test_get_by_id(self, client):
        ct = await deploy_case_type(client)
        resp = await client.get(f"/api/v1/case-types/{ct['id']}")
        assert resp.status_code == 200

    async def test_duplicate_version_rejected(self, client):
        await deploy_case_type(client)
        resp = await client.post("/api/v1/case-types", json={
            "name": "Test Case", "version": "1.0.0",
            "lifecycle_process_id": str(uuid.uuid4()),
            "definition_json": {},
        })
        assert resp.status_code == 409

    async def test_delete(self, client):
        ct = await deploy_case_type(client)
        resp = await client.delete(f"/api/v1/case-types/{ct['id']}")
        assert resp.status_code == 204
        # Soft-deleted: still fetchable by ID but excluded from list
        list_resp = await client.get("/api/v1/case-types")
        ids = [i["id"] for i in list_resp.json()["items"]]
        assert ct["id"] not in ids


class TestCases:
    async def test_create_and_get(self, client):
        ct = await deploy_case_type(client)
        case = await create_case(client, ct["id"])
        assert case["status"] == "new"
        resp = await client.get(f"/api/v1/cases/{case['id']}")
        assert resp.status_code == 200

    async def test_list_with_filters(self, client):
        ct = await deploy_case_type(client)
        await create_case(client, ct["id"], priority="high", data={})
        await create_case(client, ct["id"], priority="low", data={})
        resp = await client.get("/api/v1/cases?priority=high")
        assert resp.json()["total"] == 1

    async def test_update_data(self, client):
        ct = await deploy_case_type(client)
        case = await create_case(client, ct["id"])
        resp = await client.patch(
            f"/api/v1/cases/{case['id']}", json={"data": {"amount": 500}},
        )
        assert resp.json()["data"]["amount"] == 500
        assert resp.json()["data"]["foo"] == "bar"

    async def test_status_transitions(self, client):
        ct = await deploy_case_type(client)
        case = await create_case(client, ct["id"])

        r = await client.post(f"/api/v1/cases/{case['id']}/status", json={"status": "open"})
        assert r.json()["status"] == "open"
        r = await client.post(f"/api/v1/cases/{case['id']}/resolve", json={})
        assert r.json()["status"] == "resolved"
        assert r.json()["resolved_at"] is not None
        r = await client.post(f"/api/v1/cases/{case['id']}/close", json={})
        assert r.json()["status"] == "closed"

    async def test_reopen(self, client):
        ct = await deploy_case_type(client)
        case = await create_case(client, ct["id"])
        await client.post(f"/api/v1/cases/{case['id']}/close", json={})
        r = await client.post(f"/api/v1/cases/{case['id']}/reopen", json={"reason": "new evidence"})
        assert r.json()["status"] == "reopened"

    async def test_cancel(self, client):
        ct = await deploy_case_type(client)
        case = await create_case(client, ct["id"])
        r = await client.post(f"/api/v1/cases/{case['id']}/cancel", json={"reason": "dup"})
        assert r.json()["status"] == "cancelled"

    async def test_priority_change(self, client):
        ct = await deploy_case_type(client)
        case = await create_case(client, ct["id"])
        r = await client.post(f"/api/v1/cases/{case['id']}/priority", json={"priority": "critical"})
        assert r.json()["priority"] == "critical"

    async def test_stage_transition(self, client):
        ct = await deploy_case_type(client)
        case = await create_case(client, ct["id"])
        r = await client.post(f"/api/v1/cases/{case['id']}/stage", json={"target_stage_id": "review"})
        assert r.json()["current_stage_id"] == "review"


class TestAuditTrail:
    async def test_history_tracks_actions(self, client):
        ct = await deploy_case_type(client)
        case = await create_case(client, ct["id"])
        await client.post(f"/api/v1/cases/{case['id']}/status", json={"status": "open"})
        await client.post(f"/api/v1/cases/{case['id']}/priority", json={"priority": "high"})
        resp = await client.get(f"/api/v1/cases/{case['id']}/history")
        actions = [e["action"] for e in resp.json()]
        assert "created" in actions
        assert "status_changed" in actions


class TestRelationships:
    async def test_add_and_list(self, client):
        ct = await deploy_case_type(client)
        a = await create_case(client, ct["id"])
        b = await create_case(client, ct["id"])
        resp = await client.post(f"/api/v1/cases/{a['id']}/relationships", json={
            "target_case_id": b["id"], "relationship_type": "related",
        })
        assert resp.status_code == 201
        resp = await client.get(f"/api/v1/cases/{a['id']}/relationships")
        assert len(resp.json()) == 1

    async def test_child_case_creation(self, client):
        ct = await deploy_case_type(client)
        parent = await create_case(client, ct["id"])
        resp = await client.post(
            f"/api/v1/cases/{parent['id']}/children",
            json={"case_type_id": ct["id"], "data": {"sub": True}},
        )
        assert resp.status_code == 201
        assert resp.json()["parent_case_id"] == parent["id"]


class TestWorkQueues:
    async def test_create_and_list(self, client):
        resp = await client.post("/api/v1/queues", json={"name": "Claims Queue"})
        assert resp.status_code == 201
        resp = await client.get("/api/v1/queues")
        assert len(resp.json()) == 1

    async def test_queue_stats(self, client):
        resp = await client.post("/api/v1/queues", json={"name": "Test Queue"})
        qid = resp.json()["id"]
        resp = await client.get(f"/api/v1/queues/{qid}/stats")
        assert resp.status_code == 200
        assert resp.json()["total_items"] == 0


class TestHealth:
    async def test_health_endpoint(self, client):
        resp = await client.get("/health")
        assert resp.json()["status"] == "ok"
