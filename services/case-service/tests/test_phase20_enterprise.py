"""Phase 20 tests — Enterprise Hardening + Site Map.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations
import uuid
import pytest


class TestSecurityEvents:
    async def test_log_and_query_event(self, client):
        resp = await client.post("/api/v1/enterprise/security-events", json={
            "event_type": "auth.login",
            "severity": "info",
            "user_id": "test_user",
            "outcome": "success",
        })
        assert resp.status_code == 201

        query = await client.get("/api/v1/enterprise/security-events?event_type=auth.login")
        assert query.status_code == 200
        events = query.json()
        assert len(events) >= 1

    async def test_event_stats(self, client):
        # Create a few events
        for outcome in ("success", "denied", "error"):
            await client.post("/api/v1/enterprise/security-events", json={
                "event_type": "test.event",
                "user_id": "stats_user",
                "outcome": outcome,
            })

        resp = await client.get("/api/v1/enterprise/security-events/stats")
        assert resp.status_code == 200
        stats = resp.json()
        assert "by_type" in stats
        assert "failed_count" in stats
        # 2 failures (denied + error)
        assert stats["failed_count"] >= 2


class TestGDPR:
    async def test_create_gdpr_request(self, client):
        resp = await client.post("/api/v1/enterprise/gdpr/requests", json={
            "subject_id": "alice@example.com",
            "request_type": "export",
            "requested_by": "admin",
        })
        assert resp.status_code == 201
        assert "request_id" in resp.json()

    async def test_invalid_request_type(self, client):
        resp = await client.post("/api/v1/enterprise/gdpr/requests", json={
            "subject_id": "test",
            "request_type": "hack",
        })
        assert resp.status_code == 400

    async def test_list_requests(self, client):
        await client.post("/api/v1/enterprise/gdpr/requests", json={
            "subject_id": "list_test", "request_type": "export",
        })
        resp = await client.get("/api/v1/enterprise/gdpr/requests")
        assert resp.status_code == 200
        assert len(resp.json()) >= 1

    async def test_export_user_data(self, client):
        resp = await client.get("/api/v1/enterprise/gdpr/export/test_user_export")
        assert resp.status_code == 200
        # Should be JSON attachment
        assert "attachment" in resp.headers.get("content-disposition", "")

    async def test_anonymize_user(self, client):
        # Create a case with this user first
        ct = await client.post("/api/v1/case-types", json={
            "name": f"GDPR-{uuid.uuid4().hex[:6]}", "version": "1.0.0",
            "lifecycle_process_id": "test",
            "definition_json": {"stages": [], "sla_policies": []},
        })
        case = await client.post("/api/v1/cases", json={
            "case_type_id": ct.json()["id"],
            "data": {},
            "created_by": "anon_test_user",
        })

        # Anonymize
        resp = await client.post("/api/v1/enterprise/gdpr/anonymize/anon_test_user")
        assert resp.status_code == 200
        result = resp.json()
        assert "anonymized_id" in result
        assert result["anonymized_id"].startswith("anon-")


class TestRetentionPolicies:
    async def test_list_policies(self, client):
        resp = await client.get("/api/v1/enterprise/retention-policies")
        assert resp.status_code == 200
        policies = resp.json()
        # Should have seeded defaults
        assert len(policies) >= 1

    async def test_update_policy(self, client):
        list_resp = await client.get("/api/v1/enterprise/retention-policies")
        if not list_resp.json():
            return  # Skip if no policies
        pid = list_resp.json()[0]["id"]

        update = await client.patch(f"/api/v1/enterprise/retention-policies/{pid}", json={
            "enabled": True,
        })
        assert update.status_code == 200


class TestSystemInfo:
    async def test_system_info(self, client):
        resp = await client.get("/api/v1/enterprise/system-info")
        assert resp.status_code == 200
        info = resp.json()
        assert "version" in info
        assert "phase" in info
        assert info["phase"] == 20


class TestSiteMap:
    async def test_list_modules(self, client):
        resp = await client.get("/api/v1/sitemap/modules")
        assert resp.status_code == 200
        data = resp.json()
        assert "modules" in data
        assert data["total"] > 10  # Should have many modules

    async def test_list_categories(self, client):
        resp = await client.get("/api/v1/sitemap/categories")
        assert resp.status_code == 200
        cats = resp.json()
        cat_names = [c["name"] for c in cats]
        assert "Process" in cat_names
        assert "Cases" in cat_names
        assert "Admin" in cat_names

    async def test_list_phases(self, client):
        resp = await client.get("/api/v1/sitemap/phases")
        assert resp.status_code == 200
        data = resp.json()
        assert "phases" in data
        assert data["total"] >= 20

    async def test_search_modules(self, client):
        resp = await client.get("/api/v1/sitemap/search?q=case")
        assert resp.status_code == 200
        results = resp.json()["results"]
        assert len(results) > 0

    async def test_search_no_results(self, client):
        resp = await client.get("/api/v1/sitemap/search?q=xyznonexistent")
        assert resp.status_code == 200
        assert len(resp.json()["results"]) == 0


class TestSecurityEventLogger:
    async def test_log_directly(self, session):
        from case_service.enterprise.security_events import log_security_event
        await log_security_event(
            session,
            event_type="test.direct",
            user_id="test",
            action="test_action",
        )
        await session.commit()

    async def test_query_events(self, session):
        from case_service.enterprise.security_events import (
            log_security_event, query_events,
        )
        await log_security_event(session, event_type="query.test", user_id="q_user")
        await session.commit()

        events = await query_events(session, user_id="q_user")
        assert len(events) >= 1


class TestGDPRModule:
    async def test_export_returns_structure(self, session):
        from case_service.enterprise.gdpr import export_user_data
        data = await export_user_data(session, "nobody_here")
        assert "subject_id" in data
        assert "cases_created" in data
        assert "assignments" in data

    async def test_anonymize_function(self, session):
        from case_service.enterprise.gdpr import anonymize_user_data
        result = await anonymize_user_data(session, "nobody_here")
        await session.commit()
        assert "anonymized_id" in result
        assert result["anonymized_id"].startswith("anon-")
