"""HELIX P33 — Customer Portal tests (25 tests).

Public endpoints require no auth (token-based for doc upload).
Admin endpoints require admin role.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient

from case_service.db.models import CaseTypeModel, CaseInstanceModel, TenantModel
from case_service.auth.dependencies import get_current_user, require_role


# ── Helpers ──────────────────────────────────────────────────────────

def _fake_admin(user_id: str = "admin-1"):
    from case_service.auth.models import AuthenticatedUser
    return AuthenticatedUser(user_id=user_id, email="admin@test.local", roles=["admin"])


@pytest_asyncio.fixture
async def tenant(session) -> TenantModel:
    t = TenantModel(
        slug="acme",
        name="ACME Corp",
        settings={"portal": {"enabled": True, "welcome_text": "Hello", "brand_color": "#ff0000", "logo_text": "ACME"}},
    )
    session.add(t); await session.flush(); return t


@pytest_asyncio.fixture
async def portal_ct(session) -> CaseTypeModel:
    ct = CaseTypeModel(
        name="Support Request", version="1.0.0",
        definition_json={"stages": []},
        portal_enabled=True,
    )
    session.add(ct); await session.flush(); return ct


@pytest_asyncio.fixture
async def submitted_case(session, tenant, portal_ct) -> CaseInstanceModel:
    token = uuid.uuid4()
    c = CaseInstanceModel(
        case_type_id=portal_ct.id, case_type_version="1.0.0",
        status="new", priority="medium",
        portal_tracking_token=token,
        portal_submitter_name="Jane Doe",
        portal_submitter_email="jane@example.com",
        data={"subject": "My issue", "description": "Details here", "source": "customer_portal"},
        extra_metadata={"portal_slug": "acme"},
        created_by="portal:jane@example.com",
    )
    session.add(c); await session.flush(); return c


# ── 1. Portal config (public) ─────────────────────────────────────

async def test_01_get_portal_config(client: AsyncClient, tenant, portal_ct):
    resp = await client.get("/api/v1/portal/acme")
    assert resp.status_code == 200
    d = resp.json()
    assert d["slug"] == "acme"
    assert d["enabled"] is True
    assert d["brand_color"] == "#ff0000"
    assert any(ct["id"] == str(portal_ct.id) for ct in d["case_types"])


async def test_02_get_portal_unknown_slug(client: AsyncClient):
    resp = await client.get("/api/v1/portal/unknown-slug")
    assert resp.status_code == 404


async def test_03_get_portal_disabled(client: AsyncClient, session):
    t = TenantModel(slug="disabled-co", name="Disabled Co",
                    settings={"portal": {"enabled": False}})
    session.add(t); await session.flush()
    resp = await client.get("/api/v1/portal/disabled-co")
    assert resp.status_code == 403


async def test_04_portal_only_shows_portal_enabled_case_types(client: AsyncClient, tenant, session):
    # Add a non-portal case type
    ct2 = CaseTypeModel(name="Internal Only", version="1.0.0",
                        definition_json={"stages": []}, portal_enabled=False)
    session.add(ct2); await session.flush()
    resp = await client.get("/api/v1/portal/acme")
    assert resp.status_code == 200
    names = [ct["name"] for ct in resp.json()["case_types"]]
    assert "Internal Only" not in names


async def test_05_portal_no_case_types_returns_empty_list(client: AsyncClient, session):
    t = TenantModel(slug="empty-co", name="Empty Co",
                    settings={"portal": {"enabled": True}})
    session.add(t); await session.flush()
    resp = await client.get("/api/v1/portal/empty-co")
    assert resp.status_code == 200
    assert resp.json()["case_types"] == []


# ── 2. Submit case (public) ───────────────────────────────────────

async def test_06_submit_case_creates_case(client: AsyncClient, tenant, portal_ct):
    resp = await client.post("/api/v1/portal/acme/submit", json={
        "case_type_id": str(portal_ct.id),
        "submitter_name": "John Smith",
        "submitter_email": "john@example.com",
        "subject": "Need help",
        "description": "Please assist",
    })
    assert resp.status_code == 200
    d = resp.json()
    assert "tracking_token" in d
    assert "case_id" in d
    assert uuid.UUID(d["tracking_token"])  # valid UUID


async def test_07_submit_returns_unique_token_each_time(client: AsyncClient, tenant, portal_ct):
    body = {"case_type_id": str(portal_ct.id), "submitter_name": "A",
            "submitter_email": "a@a.com", "subject": "S1", "description": "D"}
    r1 = await client.post("/api/v1/portal/acme/submit", json=body)
    body2 = {**body, "subject": "S2"}
    r2 = await client.post("/api/v1/portal/acme/submit", json=body2)
    assert r1.json()["tracking_token"] != r2.json()["tracking_token"]


async def test_08_submit_to_disabled_portal_rejected(client: AsyncClient, session, portal_ct):
    t = TenantModel(slug="off-co", name="Off Co",
                    settings={"portal": {"enabled": False}})
    session.add(t); await session.flush()
    resp = await client.post("/api/v1/portal/off-co/submit", json={
        "case_type_id": str(portal_ct.id), "submitter_name": "X",
        "submitter_email": "x@x.com", "subject": "S", "description": "D",
    })
    assert resp.status_code == 403


async def test_09_submit_non_portal_case_type_rejected(client: AsyncClient, tenant, session):
    ct2 = CaseTypeModel(name="Restricted", version="1.0.0",
                        definition_json={"stages": []}, portal_enabled=False)
    session.add(ct2); await session.flush()
    resp = await client.post("/api/v1/portal/acme/submit", json={
        "case_type_id": str(ct2.id), "submitter_name": "X",
        "submitter_email": "x@x.com", "subject": "S", "description": "D",
    })
    assert resp.status_code == 400


async def test_10_submit_unknown_case_type_rejected(client: AsyncClient, tenant):
    resp = await client.post("/api/v1/portal/acme/submit", json={
        "case_type_id": str(uuid.uuid4()), "submitter_name": "X",
        "submitter_email": "x@x.com", "subject": "S", "description": "D",
    })
    assert resp.status_code in (400, 422)


# ── 3. Track status (public) ──────────────────────────────────────

async def test_11_track_returns_case_status(client: AsyncClient, submitted_case):
    token = str(submitted_case.portal_tracking_token)
    resp = await client.get(f"/api/v1/portal/acme/track/{token}")
    assert resp.status_code == 200
    d = resp.json()
    assert d["status"] == "new"
    assert d["subject"] == "My issue"
    assert d["case_type_name"] == "Support Request"


async def test_12_track_invalid_token_returns_404(client: AsyncClient, tenant):
    resp = await client.get(f"/api/v1/portal/acme/track/{uuid.uuid4()}")
    assert resp.status_code == 404


async def test_13_track_shows_resolved_status(client: AsyncClient, submitted_case, session):
    submitted_case.status = "resolved"
    await session.flush()
    token = str(submitted_case.portal_tracking_token)
    resp = await client.get(f"/api/v1/portal/acme/track/{token}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "resolved"


# ── 4. Document upload (token-auth) ───────────────────────────────

async def test_14_upload_document_via_token(client: AsyncClient, submitted_case):
    token = str(submitted_case.portal_tracking_token)
    mock_storage = MagicMock()
    mock_storage.put = AsyncMock()
    with patch("case_service.api.routers.portal.get_storage_backend", return_value=mock_storage):
        resp = await client.post(
            f"/api/v1/portal/acme/track/{token}/documents",
            files={"file": ("test.txt", b"hello world", "text/plain")},
        )
    assert resp.status_code == 200
    assert resp.json()["filename"] == "test.txt"
    assert resp.json()["size"] == 11
    mock_storage.put.assert_called_once()


async def test_15_upload_with_invalid_token_rejected(client: AsyncClient, tenant):
    resp = await client.post(
        f"/api/v1/portal/acme/track/{uuid.uuid4()}/documents",
        files={"file": ("f.txt", b"data", "text/plain")},
    )
    assert resp.status_code == 404


async def test_16_upload_to_closed_case_rejected(client: AsyncClient, submitted_case, session):
    submitted_case.status = "closed"
    await session.flush()
    token = str(submitted_case.portal_tracking_token)
    with patch("case_service.api.routers.portal.get_storage_backend"):
        resp = await client.post(
            f"/api/v1/portal/acme/track/{token}/documents",
            files={"file": ("f.txt", b"data", "text/plain")},
        )
    assert resp.status_code == 400


# ── 5. Admin: portal settings ─────────────────────────────────────

async def test_17_admin_list_portal_tenants(client: AsyncClient, tenant):
    from case_service.main import app
    app.dependency_overrides[get_current_user] = lambda: _fake_admin()
    resp = await client.get("/api/v1/portal-admin/tenants")
    assert resp.status_code == 200
    slugs = [t["slug"] for t in resp.json()]
    assert "acme" in slugs


async def test_18_admin_enable_portal(client: AsyncClient, session):
    t = TenantModel(slug="new-co", name="New Co", settings={})
    session.add(t); await session.flush()
    from case_service.main import app
    app.dependency_overrides[get_current_user] = lambda: _fake_admin()
    resp = await client.patch("/api/v1/portal-admin/tenants/new-co",
                              json={"enabled": True, "welcome_text": "Hi there"})
    assert resp.status_code == 200
    assert resp.json()["portal"]["enabled"] is True
    assert resp.json()["portal"]["welcome_text"] == "Hi there"


async def test_19_admin_update_branding(client: AsyncClient, tenant):
    from case_service.main import app
    app.dependency_overrides[get_current_user] = lambda: _fake_admin()
    resp = await client.patch("/api/v1/portal-admin/tenants/acme",
                              json={"brand_color": "#00ff00", "logo_text": "ACME Ltd"})
    assert resp.status_code == 200
    portal = resp.json()["portal"]
    assert portal["brand_color"] == "#00ff00"
    assert portal["logo_text"] == "ACME Ltd"


async def test_20_admin_set_allowed_case_types(client: AsyncClient, tenant, portal_ct):
    from case_service.main import app
    app.dependency_overrides[get_current_user] = lambda: _fake_admin()
    resp = await client.patch("/api/v1/portal-admin/tenants/acme",
                              json={"allowed_case_type_ids": [str(portal_ct.id)]})
    assert resp.status_code == 200
    assert str(portal_ct.id) in resp.json()["portal"]["allowed_case_type_ids"]


async def test_21_admin_update_unknown_tenant_returns_404(client: AsyncClient):
    from case_service.main import app
    app.dependency_overrides[get_current_user] = lambda: _fake_admin()
    resp = await client.patch("/api/v1/portal-admin/tenants/no-such-tenant",
                              json={"enabled": True})
    assert resp.status_code == 404


# ── 6. Admin: submissions ─────────────────────────────────────────

async def test_22_admin_list_submissions(client: AsyncClient, submitted_case):
    from case_service.main import app
    app.dependency_overrides[get_current_user] = lambda: _fake_admin()
    resp = await client.get("/api/v1/portal-admin/submissions")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) >= 1
    assert items[0]["submitter_email"] == "jane@example.com"
    assert items[0]["subject"] == "My issue"


async def test_23_admin_submissions_filtered_by_status(client: AsyncClient, submitted_case, session):
    from case_service.main import app
    app.dependency_overrides[get_current_user] = lambda: _fake_admin()
    resp = await client.get("/api/v1/portal-admin/submissions?status=new")
    assert resp.status_code == 200
    assert all(s["status"] == "new" for s in resp.json())


# ── 7. Admin: case type portal flag ──────────────────────────────

async def test_24_admin_toggle_case_type_portal_enabled(client: AsyncClient, portal_ct):
    from case_service.main import app
    app.dependency_overrides[get_current_user] = lambda: _fake_admin()
    resp = await client.patch(
        f"/api/v1/portal-admin/case-types/{portal_ct.id}/portal?enabled=false"
    )
    assert resp.status_code == 200
    assert resp.json()["portal_enabled"] is False


async def test_25_non_admin_cannot_access_portal_admin(client: AsyncClient):
    from case_service.main import app
    from case_service.auth.models import AuthenticatedUser
    viewer = AuthenticatedUser(user_id="v", email="v@v.com", roles=["viewer"])
    app.dependency_overrides[get_current_user] = lambda: viewer
    resp = await client.get("/api/v1/portal-admin/tenants")
    assert resp.status_code == 403
