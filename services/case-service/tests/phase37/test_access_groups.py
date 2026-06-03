"""HELIX P37 — Operator & Access Group Model tests (25 tests).

Covers: portals CRUD, access-roles CRUD, access-groups CRUD,
        member management, /auth/me, /auth/switch-context,
        tenant isolation, and RBAC guards.
"""
from __future__ import annotations

import uuid
import pytest
import pytest_asyncio
from httpx import AsyncClient

from case_service.main import app
from case_service.auth.dependencies import get_current_user, require_role
from case_service.auth.models import AuthenticatedUser
from case_service.db.models import (
    PortalModel, AccessRoleModel, AccessGroupModel,
    OperatorAccessGroupModel, UserDirectoryModel,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _admin(uid: str = "admin-1") -> AuthenticatedUser:
    return AuthenticatedUser(user_id=uid, email="admin@test.local", roles=["admin"])


def _staff(uid: str = "staff-1") -> AuthenticatedUser:
    return AuthenticatedUser(user_id=uid, email="staff@test.local", roles=["staff"])


def _override_admin():
    app.dependency_overrides[get_current_user] = lambda: _admin()
    app.dependency_overrides[require_role("admin")] = lambda: _admin()


def _override_staff():
    app.dependency_overrides[get_current_user] = lambda: _staff()
    app.dependency_overrides.pop(require_role("admin"), None)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def portal(session) -> PortalModel:
    p = PortalModel(
        name="Test Staff Portal", portal_type="staff",
        modules=["work-center", "case-manager"], homepage="/work-center",
    )
    session.add(p); await session.flush(); return p


@pytest_asyncio.fixture
async def role(session) -> AccessRoleModel:
    r = AccessRoleModel(
        name="case_worker", description="Can create and work cases",
        privileges=[{"resource": "case", "case_type_id": "*", "actions": ["create", "read"]}],
    )
    session.add(r); await session.flush(); return r


@pytest_asyncio.fixture
async def group(session, portal, role) -> AccessGroupModel:
    g = AccessGroupModel(
        name="Testers", description="Test group",
        tenant_id="tenant-1", portal_id=portal.id,
        role_ids=[str(role.id)],
        allowed_case_type_ids=["*"], allowed_queue_ids=["*"],
        is_default=True,
    )
    session.add(g); await session.flush(); return g


@pytest_asyncio.fixture
async def operator(session, group) -> UserDirectoryModel:
    u = UserDirectoryModel(user_id="op-1", email="op@test.local", display_name="Operator One")
    session.add(u); await session.flush()
    oag = OperatorAccessGroupModel(
        operator_id="op-1", access_group_id=group.id, is_primary=True, assigned_by="admin-1",
    )
    session.add(oag); await session.flush()
    return u


# ── Portal CRUD ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_portal(client: AsyncClient):
    _override_admin()
    resp = await client.post("/api/v1/portals", json={
        "name": "Manager View", "portal_type": "manager",
        "modules": ["analytics"], "homepage": "/analytics",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Manager View"
    assert data["portal_type"] == "manager"


@pytest.mark.asyncio
async def test_create_portal_invalid_type(client: AsyncClient):
    _override_admin()
    resp = await client.post("/api/v1/portals", json={
        "name": "Bad", "portal_type": "nonexistent",
    })
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_list_portals(client: AsyncClient, portal):
    _override_admin()
    resp = await client.get("/api/v1/portals")
    assert resp.status_code == 200
    names = [p["name"] for p in resp.json()]
    assert "Test Staff Portal" in names


@pytest.mark.asyncio
async def test_update_portal(client: AsyncClient, portal):
    _override_admin()
    resp = await client.patch(f"/api/v1/portals/{portal.id}", json={
        "name": "Updated Portal", "portal_type": "staff",
        "modules": ["work-center"], "homepage": "/work-center",
    })
    assert resp.status_code == 200
    assert resp.json()["name"] == "Updated Portal"


@pytest.mark.asyncio
async def test_get_portal(client: AsyncClient, portal):
    _override_admin()
    resp = await client.get(f"/api/v1/portals/{portal.id}")
    assert resp.status_code == 200
    assert resp.json()["portal_type"] == "staff"


@pytest.mark.asyncio
async def test_delete_portal_soft(client: AsyncClient, portal):
    _override_admin()
    resp = await client.delete(f"/api/v1/portals/{portal.id}")
    assert resp.status_code == 204
    list_resp = await client.get("/api/v1/portals")
    names = [p["name"] for p in list_resp.json()]
    assert "Test Staff Portal" not in names


# ── Access Role CRUD ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_access_role(client: AsyncClient):
    _override_admin()
    resp = await client.post("/api/v1/access-roles", json={
        "name": "adjuster", "description": "Claims adjuster",
        "privileges": [{"resource": "case", "case_type_id": "claim", "actions": ["read", "resolve"]}],
        "tenant_id": "tenant-1",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "adjuster"
    assert len(data["privileges"]) == 1


@pytest.mark.asyncio
async def test_list_access_roles_returns_records(client: AsyncClient, role):
    _override_admin()
    resp = await client.get("/api/v1/access-roles")
    assert resp.status_code == 200
    names = [r["name"] for r in resp.json()]
    assert "case_worker" in names


@pytest.mark.asyncio
async def test_get_access_role(client: AsyncClient, role):
    _override_admin()
    resp = await client.get(f"/api/v1/access-roles/{role.id}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "case_worker"


@pytest.mark.asyncio
async def test_update_access_role(client: AsyncClient, role):
    _override_admin()
    resp = await client.patch(f"/api/v1/access-roles/{role.id}", json={
        "name": "case_worker", "description": "Updated description",
        "privileges": [{"resource": "case", "actions": ["*"]}],
    })
    assert resp.status_code == 200
    assert resp.json()["description"] == "Updated description"


@pytest.mark.asyncio
async def test_delete_access_role(client: AsyncClient, role):
    _override_admin()
    resp = await client.delete(f"/api/v1/access-roles/{role.id}")
    assert resp.status_code == 204
    get_resp = await client.get(f"/api/v1/access-roles/{role.id}")
    assert get_resp.status_code == 404


# ── Access Group CRUD ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_access_group(client: AsyncClient, portal, role):
    _override_admin()
    resp = await client.post("/api/v1/access-groups", json={
        "name": "Claim Adjusters", "description": "Handles insurance claims",
        "tenant_id": "acme", "portal_id": str(portal.id),
        "role_ids": [str(role.id)],
        "allowed_case_type_ids": ["*"], "allowed_queue_ids": ["*"],
        "is_default": False,
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Claim Adjusters"
    assert data["tenant_id"] == "acme"
    assert str(role.id) in data["role_ids"]


@pytest.mark.asyncio
async def test_create_group_nonexistent_portal(client: AsyncClient):
    _override_admin()
    resp = await client.post("/api/v1/access-groups", json={
        "name": "Bad Group", "tenant_id": "t1",
        "portal_id": str(uuid.uuid4()),
    })
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_access_groups_tenant_filter(client: AsyncClient, group):
    _override_admin()
    resp = await client.get("/api/v1/access-groups?tenant_id=tenant-1")
    assert resp.status_code == 200
    names = [g["name"] for g in resp.json()]
    assert "Testers" in names


@pytest.mark.asyncio
async def test_update_access_group(client: AsyncClient, group, portal):
    _override_admin()
    resp = await client.patch(f"/api/v1/access-groups/{group.id}", json={
        "name": "Testers Renamed", "tenant_id": "tenant-1",
        "portal_id": str(portal.id), "role_ids": [],
        "allowed_case_type_ids": ["ct-1"], "allowed_queue_ids": ["*"],
        "is_default": True,
    })
    assert resp.status_code == 200
    assert resp.json()["name"] == "Testers Renamed"
    assert resp.json()["allowed_case_type_ids"] == ["ct-1"]


@pytest.mark.asyncio
async def test_soft_delete_access_group(client: AsyncClient, group):
    _override_admin()
    resp = await client.delete(f"/api/v1/access-groups/{group.id}")
    assert resp.status_code == 204
    list_resp = await client.get("/api/v1/access-groups?tenant_id=tenant-1")
    assert all(g["name"] != "Testers" for g in list_resp.json())


# ── Member Management ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_add_member_to_group(client: AsyncClient, group):
    _override_admin()
    resp = await client.post(f"/api/v1/access-groups/{group.id}/members", json={
        "operator_id": "user-xyz", "is_primary": True,
    })
    assert resp.status_code == 201
    assert resp.json()["operator_id"] == "user-xyz"
    assert resp.json()["is_primary"] is True


@pytest.mark.asyncio
async def test_list_group_members(client: AsyncClient, group, operator):
    _override_admin()
    resp = await client.get(f"/api/v1/access-groups/{group.id}/members")
    assert resp.status_code == 200
    ids = [m["operator_id"] for m in resp.json()]
    assert "op-1" in ids


@pytest.mark.asyncio
async def test_add_member_idempotent(client: AsyncClient, group, operator):
    _override_admin()
    resp = await client.post(f"/api/v1/access-groups/{group.id}/members", json={
        "operator_id": "op-1", "is_primary": False,
    })
    assert resp.status_code == 201  # upsert — not 409


@pytest.mark.asyncio
async def test_remove_member(client: AsyncClient, group, operator):
    _override_admin()
    resp = await client.delete(f"/api/v1/access-groups/{group.id}/members/op-1")
    assert resp.status_code == 204
    list_resp = await client.get(f"/api/v1/access-groups/{group.id}/members")
    assert all(m["operator_id"] != "op-1" for m in list_resp.json())


# ── /auth/me ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_auth_me_basic(client: AsyncClient):
    """Any authenticated user can call /auth/me and gets back their user_id."""
    app.dependency_overrides[get_current_user] = lambda: _staff("me-user")
    resp = await client.get("/api/v1/auth/me")
    assert resp.status_code == 200
    data = resp.json()
    assert data["user_id"] == "me-user"
    assert "roles" in data


@pytest.mark.asyncio
async def test_auth_me_is_authenticated_user(client: AsyncClient):
    """Response contains expected user fields from AuthenticatedUser.to_dict()."""
    app.dependency_overrides[get_current_user] = lambda: _admin()
    resp = await client.get("/api/v1/auth/me")
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_admin"] is True
    assert "admin" in data["roles"]


# ── /auth/switch-context ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_switch_context_valid(client: AsyncClient, group, operator):
    """Operator can switch to a group they belong to."""
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        user_id="op-1", roles=["staff"]
    )
    resp = await client.post("/api/v1/auth/switch-context", json={
        "access_group_id": str(group.id),
    })
    assert resp.status_code == 200
    assert resp.json()["user_id"] == "op-1"


@pytest.mark.asyncio
async def test_switch_context_forbidden(client: AsyncClient, group):
    """Operator cannot switch to a group they don't belong to."""
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        user_id="outsider", roles=["staff"]
    )
    resp = await client.post("/api/v1/auth/switch-context", json={
        "access_group_id": str(group.id),
    })
    assert resp.status_code == 403


# ── RBAC guards ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_portals_requires_admin(client: AsyncClient):
    _override_staff()
    resp = await client.get("/api/v1/portals")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_access_groups_requires_admin(client: AsyncClient):
    _override_staff()
    resp = await client.get("/api/v1/access-groups")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_access_roles_requires_admin(client: AsyncClient):
    _override_staff()
    resp = await client.get("/api/v1/access-roles")
    assert resp.status_code == 403
