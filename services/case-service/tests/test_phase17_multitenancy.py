"""Phase 17 tests — Multi-tenancy.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations
import uuid
import pytest


class TestTenantCRUD:
    async def test_create_tenant(self, client):
        slug = f"test-{uuid.uuid4().hex[:6]}"
        resp = await client.post("/api/v1/tenants", json={
            "slug": slug,
            "name": "Test Tenant",
            "description": "A test tenant",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["slug"] == slug
        assert data["name"] == "Test Tenant"
        assert data["status"] == "active"

    async def test_slug_validation(self, client):
        resp = await client.post("/api/v1/tenants", json={
            "slug": "INVALID SLUG WITH SPACES",
            "name": "Bad Slug",
        })
        assert resp.status_code == 400

    async def test_duplicate_slug_rejected(self, client):
        slug = f"dup-{uuid.uuid4().hex[:6]}"
        await client.post("/api/v1/tenants", json={"slug": slug, "name": "First"})
        resp = await client.post("/api/v1/tenants", json={"slug": slug, "name": "Second"})
        assert resp.status_code == 409

    async def test_list_tenants(self, client):
        await client.post("/api/v1/tenants", json={
            "slug": f"list-{uuid.uuid4().hex[:6]}", "name": "List Test",
        })
        resp = await client.get("/api/v1/tenants")
        assert resp.status_code == 200
        assert len(resp.json()) >= 1

    async def test_get_tenant_by_slug(self, client):
        slug = f"bysl-{uuid.uuid4().hex[:6]}"
        await client.post("/api/v1/tenants", json={"slug": slug, "name": "By Slug"})
        resp = await client.get(f"/api/v1/tenants/by-slug/{slug}")
        assert resp.status_code == 200
        assert resp.json()["slug"] == slug

    async def test_update_tenant(self, client):
        create = await client.post("/api/v1/tenants", json={
            "slug": f"upd-{uuid.uuid4().hex[:6]}", "name": "Old Name",
        })
        tid = create.json()["id"]
        resp = await client.patch(f"/api/v1/tenants/{tid}", json={
            "name": "New Name", "max_cases": 1000,
        })
        assert resp.status_code == 200
        assert resp.json()["name"] == "New Name"
        assert resp.json()["max_cases"] == 1000

    async def test_archive_tenant(self, client):
        create = await client.post("/api/v1/tenants", json={
            "slug": f"arc-{uuid.uuid4().hex[:6]}", "name": "To Archive",
        })
        tid = create.json()["id"]
        resp = await client.delete(f"/api/v1/tenants/{tid}")
        assert resp.status_code == 204

        # Verify status=archived
        get_resp = await client.get(f"/api/v1/tenants/{tid}")
        assert get_resp.json()["status"] == "archived"

    async def test_cannot_delete_default(self, client):
        # Create default tenant if not there
        await client.post("/api/v1/tenants", json={
            "slug": "default", "name": "Default",
        })
        # Get its ID
        resp = await client.get("/api/v1/tenants/by-slug/default")
        if resp.status_code == 200:
            tid = resp.json()["id"]
            del_resp = await client.delete(f"/api/v1/tenants/{tid}")
            assert del_resp.status_code == 400


class TestMemberships:
    async def test_add_member(self, client):
        create = await client.post("/api/v1/tenants", json={
            "slug": f"mem-{uuid.uuid4().hex[:6]}", "name": "Member Test",
        })
        tid = create.json()["id"]

        resp = await client.post(f"/api/v1/tenants/{tid}/members", json={
            "user_id": "alice", "role": "admin",
        })
        assert resp.status_code == 201
        assert resp.json()["user_id"] == "alice"
        assert resp.json()["role"] == "admin"

    async def test_invalid_role(self, client):
        create = await client.post("/api/v1/tenants", json={
            "slug": f"rol-{uuid.uuid4().hex[:6]}", "name": "Role Test",
        })
        tid = create.json()["id"]
        resp = await client.post(f"/api/v1/tenants/{tid}/members", json={
            "user_id": "bob", "role": "superuser",
        })
        assert resp.status_code == 400

    async def test_list_members(self, client):
        create = await client.post("/api/v1/tenants", json={
            "slug": f"lst-{uuid.uuid4().hex[:6]}", "name": "List Members",
        })
        tid = create.json()["id"]
        await client.post(f"/api/v1/tenants/{tid}/members", json={
            "user_id": "charlie", "role": "member",
        })
        await client.post(f"/api/v1/tenants/{tid}/members", json={
            "user_id": "dave", "role": "viewer",
        })

        resp = await client.get(f"/api/v1/tenants/{tid}/members")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    async def test_remove_member(self, client):
        create = await client.post("/api/v1/tenants", json={
            "slug": f"rm-{uuid.uuid4().hex[:6]}", "name": "Remove Test",
        })
        tid = create.json()["id"]
        await client.post(f"/api/v1/tenants/{tid}/members", json={
            "user_id": "eve", "role": "member",
        })

        resp = await client.delete(f"/api/v1/tenants/{tid}/members/eve")
        assert resp.status_code == 204

        members = (await client.get(f"/api/v1/tenants/{tid}/members")).json()
        assert "eve" not in [m["user_id"] for m in members]

    async def test_list_user_tenants(self, client):
        create1 = await client.post("/api/v1/tenants", json={
            "slug": f"ut1-{uuid.uuid4().hex[:6]}", "name": "Tenant 1",
        })
        create2 = await client.post("/api/v1/tenants", json={
            "slug": f"ut2-{uuid.uuid4().hex[:6]}", "name": "Tenant 2",
        })
        user = f"frank-{uuid.uuid4().hex[:6]}"
        await client.post(f"/api/v1/tenants/{create1.json()['id']}/members", json={
            "user_id": user, "role": "member",
        })
        await client.post(f"/api/v1/tenants/{create2.json()['id']}/members", json={
            "user_id": user, "role": "admin",
        })

        resp = await client.get(f"/api/v1/tenants/user/{user}/tenants")
        assert resp.status_code == 200
        assert len(resp.json()) == 2


class TestTenantContext:
    def test_set_get_current(self):
        from case_service.tenancy.context import (
            TenantContext, set_current_tenant, get_current_tenant, clear_current_tenant,
        )
        ctx = TenantContext(
            tenant_id=uuid.uuid4(),
            tenant_slug="test",
            tenant_name="Test",
        )
        set_current_tenant(ctx)
        assert get_current_tenant() == ctx

        clear_current_tenant()
        assert get_current_tenant() is None

    def test_get_tenant_id_none(self):
        from case_service.tenancy.context import (
            clear_current_tenant, get_current_tenant_id,
        )
        clear_current_tenant()
        assert get_current_tenant_id() is None


class TestTenantRepository:
    async def test_create_and_get_by_slug(self, session):
        from case_service.tenancy import repository as repo
        slug = f"repo-{uuid.uuid4().hex[:6]}"
        tenant = await repo.create_tenant(session, data={
            "slug": slug, "name": "Repo Test",
        })
        await session.commit()

        fetched = await repo.get_tenant_by_slug(session, slug)
        assert fetched is not None
        assert fetched.slug == slug

    async def test_list_tenants(self, session):
        from case_service.tenancy import repository as repo
        tenants = await repo.list_tenants(session)
        assert isinstance(tenants, list)

    async def test_membership_lifecycle(self, session):
        from case_service.tenancy import repository as repo
        slug = f"ml-{uuid.uuid4().hex[:6]}"
        tenant = await repo.create_tenant(session, data={
            "slug": slug, "name": "ML Test",
        })
        await session.commit()

        await repo.add_membership(
            session, tenant_id=tenant.id, user_id="test_user", role="admin",
        )
        await session.commit()

        role = await repo.get_user_role_in_tenant(
            session, tenant_id=tenant.id, user_id="test_user",
        )
        assert role == "admin"

        members = await repo.list_members_of_tenant(session, tenant.id)
        assert len(members) == 1

        removed = await repo.remove_membership(
            session, tenant_id=tenant.id, user_id="test_user",
        )
        assert removed is True
