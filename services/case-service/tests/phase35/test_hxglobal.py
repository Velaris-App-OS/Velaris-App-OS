"""HELIX P35 — HxGlobal tests (30 tests).

Covers: protocol registry, local/aws/gcp/azure adapters,
        sovereignty resolver (specific rule > tenant rule > assignment > primary),
        migration pipeline (success, unreachable target),
        health poller,
        API — regions CRUD + ping, health endpoint, health history,
        sovereignty rules CRUD + resolve, tenant assignments CRUD,
        migrate-tenant, access log.
"""
from __future__ import annotations

import uuid
import pytest
from httpx import AsyncClient

from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser, ActiveAccessGroup
from case_service.hxglobal import regions as _  # noqa: F401
from case_service.hxglobal.protocol import get_region_adapter, _REGISTRY
from case_service.hxglobal.sovereignty import resolve_region
from case_service.hxglobal.migration import migrate_tenant
from case_service.hxglobal.health import poll_region_health
from case_service.db.models import RegionModel
from case_service.main import app


# ── Auth helpers ──────────────────────────────────────────────────────────────

def _admin():
    return AuthenticatedUser(
        user_id="global-admin", roles=["admin"],
        active_access_group=ActiveAccessGroup(
            id=str(uuid.uuid4()), name="Admins",
            portal_id=str(uuid.uuid4()), portal_type="admin",
            portal_name="Admin Portal", modules=[], homepage="/",
            roles=["admin"], privileges=[],
            allowed_case_type_ids=["*"], allowed_queue_ids=["*"],
        ),
    )

def _ov(): app.dependency_overrides[get_current_user] = lambda: _admin()
def _cl(): app.dependency_overrides.pop(get_current_user, None)


# ── Unit: protocol registry ───────────────────────────────────────────────────

class TestProtocolRegistry:
    def test_all_providers_registered(self):
        for p in ("local", "aws", "gcp", "azure"):
            assert p in _REGISTRY

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown region provider"):
            get_region_adapter("nonexistent", {})


# ── Unit: region adapters ─────────────────────────────────────────────────────

class TestRegionAdapters:
    def test_local_ping_ok(self):
        adapter = get_region_adapter("local", {})
        result = adapter.ping()
        assert result["ok"] is True
        assert "latency_ms" in result

    def test_local_no_replication_lag(self):
        adapter = get_region_adapter("local", {})
        assert adapter.replication_lag_ms() is None

    def test_aws_missing_config(self):
        adapter = get_region_adapter("aws", {})
        result = adapter.ping()
        assert result["ok"] is False

    def test_aws_valid_config(self):
        adapter = get_region_adapter("aws", {"region": "eu-west-1", "endpoint": "https://x"})
        result = adapter.ping()
        assert result["ok"] is True

    def test_gcp_missing_project(self):
        adapter = get_region_adapter("gcp", {})
        result = adapter.ping()
        assert result["ok"] is False

    def test_gcp_valid_config(self):
        adapter = get_region_adapter("gcp", {"project_id": "my-proj"})
        result = adapter.ping()
        assert result["ok"] is True

    def test_azure_missing_subscription(self):
        adapter = get_region_adapter("azure", {})
        result = adapter.ping()
        assert result["ok"] is False

    def test_azure_valid_config(self):
        adapter = get_region_adapter("azure", {"subscription_id": "sub-123"})
        result = adapter.ping()
        assert result["ok"] is True


# ── Unit: sovereignty resolver ────────────────────────────────────────────────

@pytest.mark.asyncio
class TestSovereigntyResolver:
    async def test_no_rules_returns_none_without_primary(self, session):
        result = await resolve_region("tenant-x", "case-type-y", session)
        assert result is None

    async def test_primary_region_returned_as_fallback(self, session):
        region = RegionModel(name="primary-test", provider="local", is_primary=True, enabled=True)
        session.add(region)
        await session.flush()
        result = await resolve_region(None, None, session)
        assert result is not None
        assert result.name == "primary-test"

    async def test_sovereignty_rule_overrides_primary(self, session):
        primary = RegionModel(name="primary-r", provider="local", is_primary=True, enabled=True)
        specific = RegionModel(name="eu-specific", provider="local", is_primary=False, enabled=True)
        session.add_all([primary, specific])
        await session.flush()

        from case_service.db.models import SovereigntyRuleModel
        rule = SovereigntyRuleModel(tenant_id="tenant-eu", region_id=specific.id, regulation="GDPR")
        session.add(rule)
        await session.flush()

        result = await resolve_region("tenant-eu", None, session)
        assert result is not None
        assert result.name == "eu-specific"


# ── Unit: migration pipeline ──────────────────────────────────────────────────

@pytest.mark.asyncio
class TestMigrationPipeline:
    async def test_migrate_to_local_region(self, session):
        region = RegionModel(name="target-region", provider="local", enabled=True)
        session.add(region)
        await session.flush()
        result = await migrate_tenant("tenant-abc", region.id, session)
        assert result["status"] == "success"
        assert result["tenant_id"] == "tenant-abc"

    async def test_migrate_to_nonexistent_region_fails(self, session):
        result = await migrate_tenant("tenant-xyz", uuid.uuid4(), session)
        assert result["status"] == "error"


# ── Unit: health poller ───────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestHealthPoller:
    async def test_poll_local_region(self, session):
        region = RegionModel(name="health-test", provider="local", enabled=True)
        session.add(region)
        await session.flush()
        result = await poll_region_health(region, session)
        assert result["status"] == "healthy"
        assert "latency_ms" in result


# ── API: regions CRUD ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestRegionsAPI:
    async def test_create_region(self, client: AsyncClient):
        _ov()
        r = await client.post("/api/v1/global/regions", json={
            "name": "eu-frankfurt", "provider": "aws",
            "location": "Frankfurt, Germany",
            "connection_config": {"region": "eu-central-1", "endpoint": "https://x"},
        })
        _cl()
        assert r.status_code == 201
        assert r.json()["name"] == "eu-frankfurt"

    async def test_list_regions(self, client: AsyncClient):
        _ov()
        await client.post("/api/v1/global/regions", json={"name": "r1", "provider": "local"})
        r = await client.get("/api/v1/global/regions")
        _cl()
        assert r.status_code == 200
        assert r.json()["total"] >= 1

    async def test_get_region(self, client: AsyncClient):
        _ov()
        cr = await client.post("/api/v1/global/regions", json={"name": "get-me", "provider": "local"})
        rid = cr.json()["id"]
        r = await client.get(f"/api/v1/global/regions/{rid}")
        _cl()
        assert r.status_code == 200
        assert r.json()["id"] == rid

    async def test_update_region(self, client: AsyncClient):
        _ov()
        cr = await client.post("/api/v1/global/regions", json={"name": "old-name", "provider": "local"})
        rid = cr.json()["id"]
        r = await client.patch(f"/api/v1/global/regions/{rid}", json={"name": "new-name"})
        _cl()
        assert r.status_code == 200
        assert r.json()["name"] == "new-name"

    async def test_delete_region(self, client: AsyncClient):
        _ov()
        cr = await client.post("/api/v1/global/regions", json={"name": "del-me", "provider": "local"})
        rid = cr.json()["id"]
        r = await client.delete(f"/api/v1/global/regions/{rid}")
        _cl()
        assert r.status_code == 204

    async def test_invalid_provider_rejected(self, client: AsyncClient):
        _ov()
        r = await client.post("/api/v1/global/regions", json={"name": "x", "provider": "invalid"})
        _cl()
        assert r.status_code == 400

    async def test_ping_region(self, client: AsyncClient):
        _ov()
        cr = await client.post("/api/v1/global/regions", json={"name": "ping-me", "provider": "local"})
        rid = cr.json()["id"]
        r = await client.post(f"/api/v1/global/regions/{rid}/ping")
        _cl()
        assert r.status_code == 200
        assert "ok" in r.json()


# ── API: health ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestHealthAPI:
    async def test_global_health_empty(self, client: AsyncClient):
        _ov()
        r = await client.get("/api/v1/global/health")
        _cl()
        assert r.status_code == 200
        assert "health" in r.json()

    async def test_health_history(self, client: AsyncClient):
        _ov()
        cr = await client.post("/api/v1/global/regions", json={"name": "hist-r", "provider": "local"})
        rid = cr.json()["id"]
        await client.get("/api/v1/global/health")
        r = await client.get(f"/api/v1/global/health/{rid}/history")
        _cl()
        assert r.status_code == 200


# ── API: sovereignty rules ────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestSovereigntyAPI:
    async def test_create_and_list_rule(self, client: AsyncClient):
        _ov()
        cr = await client.post("/api/v1/global/regions", json={"name": "sov-r", "provider": "local"})
        rid = cr.json()["id"]
        rr = await client.post("/api/v1/global/sovereignty-rules", json={
            "tenant_id": "tenant-eu", "region_id": rid, "regulation": "GDPR",
            "description": "EU data must stay in Frankfurt",
        })
        assert rr.status_code == 201
        lr = await client.get("/api/v1/global/sovereignty-rules")
        _cl()
        assert lr.json()["total"] >= 1

    async def test_invalid_regulation_rejected(self, client: AsyncClient):
        _ov()
        cr = await client.post("/api/v1/global/regions", json={"name": "sov-r2", "provider": "local"})
        rid = cr.json()["id"]
        r = await client.post("/api/v1/global/sovereignty-rules", json={
            "region_id": rid, "regulation": "INVALID",
        })
        _cl()
        assert r.status_code == 400

    async def test_resolve_returns_region(self, client: AsyncClient):
        _ov()
        cr = await client.post("/api/v1/global/regions", json={"name": "resolve-r", "provider": "local"})
        rid = cr.json()["id"]
        await client.post("/api/v1/global/sovereignty-rules", json={
            "tenant_id": "resolve-tenant", "region_id": rid, "regulation": "HIPAA",
        })
        r = await client.post("/api/v1/global/sovereignty-rules/resolve", json={
            "tenant_id": "resolve-tenant",
        })
        _cl()
        assert r.status_code == 200
        assert r.json()["region"] is not None


# ── API: tenant assignments ───────────────────────────────────────────────────

@pytest.mark.asyncio
class TestTenantAssignmentsAPI:
    async def test_create_assignment(self, client: AsyncClient):
        _ov()
        cr = await client.post("/api/v1/global/regions", json={"name": "assign-r", "provider": "local"})
        rid = cr.json()["id"]
        r = await client.post("/api/v1/global/tenant-assignments", json={
            "tenant_id": "t-assign", "region_id": rid, "assignment_type": "primary",
        })
        _cl()
        assert r.status_code == 201

    async def test_migrate_tenant(self, client: AsyncClient):
        _ov()
        cr = await client.post("/api/v1/global/regions", json={"name": "mig-r", "provider": "local"})
        rid = cr.json()["id"]
        r = await client.post("/api/v1/global/migrate-tenant", json={
            "tenant_id": "migrate-me", "target_region_id": rid,
        })
        _cl()
        assert r.status_code == 200
        assert r.json()["status"] == "success"

    async def test_access_log(self, client: AsyncClient):
        _ov()
        r = await client.get("/api/v1/global/access-log")
        _cl()
        assert r.status_code == 200
        assert "logs" in r.json()
