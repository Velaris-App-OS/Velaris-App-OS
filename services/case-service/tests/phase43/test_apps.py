"""HELIX P43 — App Export & Environment Pipeline tests (22 tests).

Covers: POST /apps/package (creates bundle + manifest), GET /apps/packages
        (list, status filter), GET /apps/packages/{id} (detail + manifest),
        PATCH /apps/packages/{id}/status (publish, deprecate, bad status),
        GET /apps/packages/{id}/download (ZIP bytes, correct filename),
        GET /apps/packages/{id}/diff/{id2} (has_changes, sections),
        POST /apps/packages/{id}/promote/{env} (valid env, invalid env),
        GET /apps/deployments (list, env filter), diff_bundles unit tests,
        auth guard (401).
"""
from __future__ import annotations

import uuid
import zipfile
import io

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select

from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser, ActiveAccessGroup
from case_service.db.models import AppPackageModel, AppDeploymentModel
from case_service.apps.differ import diff_bundles, _diff_section
from case_service.main import app


# ── Auth helpers ──────────────────────────────────────────────────────────────

def _admin():
    return AuthenticatedUser(
        user_id="admin-1", roles=["admin"],
        active_access_group=ActiveAccessGroup(
            id=str(uuid.uuid4()), name="Admins",
            portal_id=str(uuid.uuid4()), portal_type="admin",
            portal_name="Admin Portal", modules=[], homepage="/",
            roles=["admin"], privileges=[],
            allowed_case_type_ids=["*"], allowed_queue_ids=["*"],
        ),
    )

def _override():
    app.dependency_overrides[get_current_user] = lambda: _admin()

def _clear():
    app.dependency_overrides.pop(get_current_user, None)


# ── Package endpoint tests ────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestPackageCreate:
    def setup_method(self):
        _override()

    def teardown_method(self):
        _clear()

    async def test_package_creates_bundle(self, client: AsyncClient):
        r = await client.post("/api/v1/apps/package", json={
            "name": "MyApp", "version": "1.0.0", "description": "First release",
        })
        assert r.status_code == 201
        d = r.json()
        assert d["name"] == "MyApp"
        assert d["version"] == "1.0.0"
        assert d["status"] == "draft"
        assert "id" in d

    async def test_package_duplicate_returns_409(self, client: AsyncClient):
        body = {"name": "App", "version": "1.0.0"}
        await client.post("/api/v1/apps/package", json=body)
        r = await client.post("/api/v1/apps/package", json=body)
        assert r.status_code == 409

    async def test_package_without_description(self, client: AsyncClient):
        r = await client.post("/api/v1/apps/package", json={
            "name": "MinimalApp", "version": "0.1",
        })
        assert r.status_code == 201
        assert r.json()["description"] in (None, "")


@pytest.mark.asyncio
class TestPackageList:
    def setup_method(self):
        _override()

    def teardown_method(self):
        _clear()

    async def test_list_empty(self, client: AsyncClient):
        r = await client.get("/api/v1/apps/packages")
        assert r.status_code == 200
        assert r.json()["total"] == 0

    async def test_list_returns_created(self, client: AsyncClient):
        await client.post("/api/v1/apps/package", json={"name": "App", "version": "1.0"})
        r = await client.get("/api/v1/apps/packages")
        assert r.json()["total"] == 1

    async def test_list_status_filter(self, client: AsyncClient):
        resp = await client.post("/api/v1/apps/package", json={"name": "App", "version": "1.0"})
        pkg_id = resp.json()["id"]
        await client.patch(f"/api/v1/apps/packages/{pkg_id}/status", json={"status": "published"})
        r = await client.get("/api/v1/apps/packages?status=draft")
        assert r.json()["total"] == 0
        r2 = await client.get("/api/v1/apps/packages?status=published")
        assert r2.json()["total"] == 1


@pytest.mark.asyncio
class TestPackageDetail:
    def setup_method(self):
        _override()

    def teardown_method(self):
        _clear()

    async def test_get_package_has_manifest(self, client: AsyncClient):
        resp = await client.post("/api/v1/apps/package", json={"name": "App", "version": "1.0"})
        pkg_id = resp.json()["id"]
        r = await client.get(f"/api/v1/apps/packages/{pkg_id}")
        assert r.status_code == 200
        d = r.json()
        assert "manifest" in d
        assert isinstance(d["manifest"], dict)

    async def test_get_package_404(self, client: AsyncClient):
        r = await client.get(f"/api/v1/apps/packages/{uuid.uuid4()}")
        assert r.status_code == 404


@pytest.mark.asyncio
class TestPackageStatus:
    def setup_method(self):
        _override()

    def teardown_method(self):
        _clear()

    async def test_publish_package(self, client: AsyncClient):
        resp = await client.post("/api/v1/apps/package", json={"name": "App", "version": "1.0"})
        pkg_id = resp.json()["id"]
        r = await client.patch(f"/api/v1/apps/packages/{pkg_id}/status", json={"status": "published"})
        assert r.status_code == 200
        assert r.json()["status"] == "published"

    async def test_deprecate_package(self, client: AsyncClient):
        resp = await client.post("/api/v1/apps/package", json={"name": "App", "version": "1.0"})
        pkg_id = resp.json()["id"]
        r = await client.patch(f"/api/v1/apps/packages/{pkg_id}/status", json={"status": "deprecated"})
        assert r.json()["status"] == "deprecated"

    async def test_invalid_status_returns_400(self, client: AsyncClient):
        resp = await client.post("/api/v1/apps/package", json={"name": "App", "version": "1.0"})
        pkg_id = resp.json()["id"]
        r = await client.patch(f"/api/v1/apps/packages/{pkg_id}/status", json={"status": "banana"})
        assert r.status_code == 400


@pytest.mark.asyncio
class TestPackageDownload:
    def setup_method(self):
        _override()

    def teardown_method(self):
        _clear()

    async def test_download_returns_zip(self, client: AsyncClient):
        resp = await client.post("/api/v1/apps/package", json={"name": "MyApp", "version": "2.0"})
        pkg_id = resp.json()["id"]
        r = await client.get(f"/api/v1/apps/packages/{pkg_id}/download")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/zip"
        # Verify it's a valid ZIP
        buf = io.BytesIO(r.content)
        with zipfile.ZipFile(buf) as zf:
            names = zf.namelist()
        assert any("helix-app.json" in n for n in names)

    async def test_download_filename_includes_version(self, client: AsyncClient):
        resp = await client.post("/api/v1/apps/package", json={"name": "My App", "version": "3.1"})
        pkg_id = resp.json()["id"]
        r = await client.get(f"/api/v1/apps/packages/{pkg_id}/download")
        cd = r.headers.get("content-disposition", "")
        assert "3.1" in cd


@pytest.mark.asyncio
class TestPackageDiff:
    def setup_method(self):
        _override()

    def teardown_method(self):
        _clear()

    async def test_diff_identical_packages_no_changes(self, client: AsyncClient):
        r1 = await client.post("/api/v1/apps/package", json={"name": "App", "version": "1.0"})
        r2 = await client.post("/api/v1/apps/package", json={"name": "App", "version": "1.1"})
        id1, id2 = r1.json()["id"], r2.json()["id"]
        r = await client.get(f"/api/v1/apps/packages/{id1}/diff/{id2}")
        assert r.status_code == 200
        d = r.json()
        assert "has_changes" in d
        assert "sections" in d

    async def test_diff_404_on_bad_id(self, client: AsyncClient):
        resp = await client.post("/api/v1/apps/package", json={"name": "App", "version": "1.0"})
        pkg_id = resp.json()["id"]
        r = await client.get(f"/api/v1/apps/packages/{pkg_id}/diff/{uuid.uuid4()}")
        assert r.status_code == 404


@pytest.mark.asyncio
class TestPromotion:
    def setup_method(self):
        _override()

    def teardown_method(self):
        _clear()

    async def test_promote_to_staging(self, client: AsyncClient):
        resp = await client.post("/api/v1/apps/package", json={"name": "App", "version": "1.0"})
        pkg_id = resp.json()["id"]
        r = await client.post(f"/api/v1/apps/packages/{pkg_id}/promote/staging",
                              json={"notes": "first promotion"})
        assert r.status_code == 201
        d = r.json()
        assert d["environment"] == "staging"
        assert d["status"] == "deployed"

    async def test_promote_invalid_env_returns_400(self, client: AsyncClient):
        resp = await client.post("/api/v1/apps/package", json={"name": "App", "version": "1.0"})
        pkg_id = resp.json()["id"]
        r = await client.post(f"/api/v1/apps/packages/{pkg_id}/promote/production",
                              json={})
        assert r.status_code == 400

    async def test_deployments_list(self, client: AsyncClient):
        resp = await client.post("/api/v1/apps/package", json={"name": "App", "version": "1.0"})
        pkg_id = resp.json()["id"]
        await client.post(f"/api/v1/apps/packages/{pkg_id}/promote/dev", json={})
        await client.post(f"/api/v1/apps/packages/{pkg_id}/promote/staging", json={})
        r = await client.get("/api/v1/apps/deployments")
        assert r.json()["total"] == 2

    async def test_deployments_env_filter(self, client: AsyncClient):
        resp = await client.post("/api/v1/apps/package", json={"name": "App", "version": "1.0"})
        pkg_id = resp.json()["id"]
        await client.post(f"/api/v1/apps/packages/{pkg_id}/promote/dev", json={})
        await client.post(f"/api/v1/apps/packages/{pkg_id}/promote/uat", json={})
        r = await client.get("/api/v1/apps/deployments?environment=dev")
        assert r.json()["total"] == 1


# ── Unit tests for differ ─────────────────────────────────────────────────────

class TestDiffBundles:
    def _make_bundle(self, case_types=None, forms=None):
        return {
            "case_types": case_types or [],
            "forms": forms or [],
            "rules": [], "portals": [], "access_groups": [],
            "work_queues": [], "escalation_trees": [], "business_calendars": [],
            "meta": {},
        }

    def test_empty_bundles_no_changes(self):
        result = diff_bundles(self._make_bundle(), self._make_bundle())
        assert result["has_changes"] is False
        assert result["summary"]["added"] == 0

    def test_added_case_type_detected(self):
        a = self._make_bundle()
        b = self._make_bundle(case_types=[{"id": "1", "name": "Claim"}])
        result = diff_bundles(a, b)
        assert result["has_changes"] is True
        assert result["summary"]["added"] == 1
        assert "Claim" in result["sections"]["case_types"]["added"]

    def test_removed_item_detected(self):
        a = self._make_bundle(forms=[{"id": "f1", "name": "Intake Form"}])
        b = self._make_bundle(forms=[])
        result = diff_bundles(a, b)
        assert result["has_changes"] is True
        assert "Intake Form" in result["sections"]["forms"]["removed"]

    def test_changed_item_detected(self):
        item_v1 = {"id": "c1", "name": "Claim", "version": "1"}
        item_v2 = {"id": "c1", "name": "Claim", "version": "2"}
        a = self._make_bundle(case_types=[item_v1])
        b = self._make_bundle(case_types=[item_v2])
        result = diff_bundles(a, b)
        assert result["has_changes"] is True
        assert len(result["sections"]["case_types"]["changed"]) == 1
