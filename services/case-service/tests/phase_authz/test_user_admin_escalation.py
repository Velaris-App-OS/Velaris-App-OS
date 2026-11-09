"""A tenant's admin could make anyone — themselves included — superadmin.

Found 2026-09-29 while making 8d "100%". `POST /auth/real/register` and
`PATCH /auth/real/users/{id}` took `roles` verbatim, and login copies those
roles into the token. So any holder of `identity.users.write` — every tenant's
`admin` — could add `superadmin`, the one role that crosses every tenant
boundary on the platform. And a user registered by a tenant's admin landed in
NO tenant, which the platform reads as the default tenant: the install's own.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from velaris_shared.db.models import HelixUserModel, TenantModel, UserDirectoryModel

from case_service.main import app
from case_service.tenancy import identity as tenancy
from tests.conftest import TestSessionFactory  # type: ignore[attr-defined]
from tests.conftest import _headers  # type: ignore[attr-defined]


@pytest_asyncio.fixture
async def api():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def world():
    tenancy.clear_cache()
    async with TestSessionFactory() as db:
        acme = TenantModel(id=uuid.uuid4(), slug="acme", name="Acme")
        db.add(acme)
        mallory = HelixUserModel(id=uuid.uuid4(), username="mallory", email="m@x.test",
                                 password_hash="x", roles=["admin"])
        platform = HelixUserModel(id=uuid.uuid4(), username="opsadmin", email="o@x.test",
                                  password_hash="x", roles=["admin"])
        db.add_all([mallory, platform])
        await db.flush()
        db.add(UserDirectoryModel(id=uuid.uuid4(), user_id=str(mallory.id), tenant_id="acme"))
        db.add(UserDirectoryModel(id=uuid.uuid4(), user_id=str(platform.id), tenant_id=None))
        await db.commit()
        return {"mallory": mallory, "platform": platform}


async def test_an_admin_cannot_make_themselves_superadmin(api, world):
    me = world["mallory"]
    r = await api.patch(f"/api/v1/auth/real/users/{me.id}", headers=_headers(str(me.id), ["admin"]),
                        json={"roles": ["admin", "superadmin"]})
    assert r.status_code == 403, r.text
    async with TestSessionFactory() as db:
        assert "superadmin" not in (await db.get(HelixUserModel, me.id)).roles


async def test_an_admin_cannot_register_a_superadmin(api, world):
    me = world["mallory"]
    r = await api.post("/api/v1/auth/real/register", headers=_headers(str(me.id), ["admin"]),
                       json={"username": "sneaky", "email": "s@x.test",
                             "password": "longenough1", "roles": ["SuperAdmin"]})
    assert r.status_code == 403, r.text


async def test_a_user_registered_by_a_tenant_admin_joins_that_tenant(api, world):
    me = world["mallory"]
    r = await api.post("/api/v1/auth/real/register", headers=_headers(str(me.id), ["admin"]),
                       json={"username": "newbie", "email": "n@x.test",
                             "password": "longenough1", "roles": ["admin"]})
    assert r.status_code == 201, r.text
    async with TestSessionFactory() as db:
        row = (await db.execute(select(UserDirectoryModel).where(
            UserDirectoryModel.user_id == r.json()["user_id"]))).scalars().first()
    assert row is not None and row.tenant_id == "acme"


async def test_a_tenant_admin_cannot_edit_a_platform_user(api, world):
    me, ops = world["mallory"], world["platform"]
    h = _headers(str(me.id), ["admin"])
    r = await api.patch(f"/api/v1/auth/real/users/{ops.id}", headers=h, json={"is_active": False})
    assert r.status_code == 404, r.text
    r = await api.get(f"/api/v1/auth/real/users/{ops.id}", headers=h)
    assert r.status_code == 404, r.text


async def test_a_tenant_admin_cannot_adopt_a_platform_directory_row(api, world):
    me, ops = world["mallory"], world["platform"]
    r = await api.patch(f"/api/v1/user-directory/{ops.id}", headers=_headers(str(me.id), ["admin"]),
                        json={"tenant_id": "acme"})
    assert r.status_code == 404, r.text
