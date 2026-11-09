"""Admin Studio access administration stays inside the caller's tenant (2026-10-30).

Found auditing R6: a tenant admin could rewrite or delete a built-in access role
(no tenant — in every tenant's list), rewrite a system-wide portal, create a
"built-in" role by sending no tenant, put another tenant's role in their group,
and put another tenant's user in it. Each is refused here as that tenant admin;
the same admin still manages their own tenant's; the platform operator (default
tenant) is unchanged.
"""
from __future__ import annotations

import uuid

import pytest_asyncio
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient

from case_service.main import app
from case_service.tenancy import identity as tenancy
from tests.conftest import TestSessionFactory  # type: ignore[attr-defined]
from velaris_shared.auth.jwt_handler import create_dev_token
from velaris_shared.config import get_settings
from velaris_shared.db.models import (
    AccessGroupModel, AccessRoleModel, PortalModel, TenantModel, UserDirectoryModel,
)

ACME = uuid.UUID("00000000-0000-0000-0000-0000000a11ce")
EVIL = uuid.UUID("00000000-0000-0000-0000-0000000e0e0e")


def _h(user_id: str) -> dict[str, str]:
    s = get_settings()
    token = create_dev_token(user_id=user_id, username=user_id, roles=["admin"],
                             secret=s.auth_secret, private_key=s.auth_rsa_private_key or "")
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def api():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def world():
    """acme and evil, an admin in each, a built-in role, a system portal, and
    one role, group and user of acme's."""
    tenancy.clear_cache()
    ids = {}
    async with TestSessionFactory() as db:
        await db.execute(sa.text("DELETE FROM tenants WHERE slug IN ('acme','evil')"))
        db.add_all([TenantModel(id=ACME, slug="acme", name="Acme"),
                    TenantModel(id=EVIL, slug="evil", name="Evil")])
        for uid, t in (("ann", "acme"), ("mallory", "evil"), ("acme-user", "acme")):
            db.add(UserDirectoryModel(id=uuid.uuid4(), user_id=f"{uid}-{uuid.uuid4().hex[:6]}",
                                      tenant_id=t))
        await db.flush()
        users = {r.user_id.rsplit("-", 1)[0]: r.user_id for r in (await db.execute(
            sa.select(UserDirectoryModel).where(UserDirectoryModel.tenant_id.in_(["acme", "evil"]))
        )).scalars()}
        builtin = AccessRoleModel(name=f"builtin-{uuid.uuid4().hex[:6]}", privileges=[])
        system_portal = PortalModel(name=f"sys-{uuid.uuid4().hex[:6]}", portal_type="staff",
                                    modules=[], homepage="/")
        acme_role = AccessRoleModel(name=f"acme-{uuid.uuid4().hex[:6]}", privileges=[],
                                    tenant_id="acme")
        db.add_all([builtin, system_portal, acme_role])
        await db.flush()
        evil_group = AccessGroupModel(name=f"evil-{uuid.uuid4().hex[:6]}", tenant_id="evil",
                                      portal_id=system_portal.id, role_ids=[],
                                      allowed_case_type_ids=["*"], allowed_queue_ids=["*"])
        db.add(evil_group)
        await db.commit()
        ids.update(users=users, builtin=str(builtin.id), portal=str(system_portal.id),
                   acme_role=str(acme_role.id), evil_group=str(evil_group.id))
    return ids


def _role_body(name: str, **kw) -> dict:
    return {"name": name, "description": "", "privileges": [], **kw}


async def test_a_tenant_admin_cannot_change_a_built_in_role(api, world):
    h = _h(world["users"]["mallory"])
    r = await api.patch(f"/api/v1/access-roles/{world['builtin']}", headers=h,
                        json=_role_body("pwned"))
    assert r.status_code == 404, r.text
    r = await api.delete(f"/api/v1/access-roles/{world['builtin']}", headers=h)
    assert r.status_code == 404, r.text
    # still readable: every tenant's groups may hold it
    r = await api.get(f"/api/v1/access-roles/{world['builtin']}", headers=h)
    assert r.status_code == 200, r.text


async def test_a_tenant_admin_cannot_change_a_system_portal(api, world):
    r = await api.patch(f"/api/v1/portals/{world['portal']}", headers=_h(world["users"]["mallory"]),
                        json={"name": "pwned", "portal_type": "staff", "modules": [], "homepage": "/"})
    assert r.status_code == 404, r.text
    r = await api.get(f"/api/v1/portals/{world['portal']}", headers=_h(world["users"]["mallory"]))
    assert r.status_code == 200, r.text


async def test_a_tenant_admins_new_role_is_their_tenants_not_built_in(api, world):
    r = await api.post("/api/v1/access-roles", headers=_h(world["users"]["mallory"]),
                       json=_role_body(f"mine-{uuid.uuid4().hex[:6]}"))
    assert r.status_code == 201, r.text
    assert r.json()["tenant_id"] == "evil"
    rid = r.json()["id"]
    # nor can a PATCH move it to "built-in"
    r = await api.patch(f"/api/v1/access-roles/{rid}", headers=_h(world["users"]["mallory"]),
                        json=_role_body("still-mine", tenant_id=None))
    assert r.status_code == 200, r.text
    assert r.json()["tenant_id"] == "evil"


async def test_a_tenant_admin_cannot_take_another_tenants_role_or_user(api, world):
    h = _h(world["users"]["mallory"])
    g = world["evil_group"]
    r = await api.patch(f"/api/v1/access-groups/{g}", headers=h, json={
        "name": "g", "tenant_id": "evil", "portal_id": world["portal"], "role_ids": [world["acme_role"]],
        "allowed_case_type_ids": ["*"], "allowed_queue_ids": ["*"]})
    assert r.status_code == 404, r.text
    r = await api.post(f"/api/v1/access-groups/{g}/members", headers=h,
                       json={"operator_id": world["users"]["acme-user"], "is_primary": False})
    assert r.status_code == 404, r.text


async def test_the_tenant_admin_still_manages_their_own(api, world):
    h = _h(world["users"]["mallory"])
    g = world["evil_group"]
    r = await api.patch(f"/api/v1/access-groups/{g}", headers=h, json={
        "name": "g2", "tenant_id": "evil", "portal_id": world["portal"], "role_ids": [world["builtin"]],
        "allowed_case_type_ids": ["*"], "allowed_queue_ids": ["*"]})
    assert r.status_code == 200, r.text
    r = await api.post(f"/api/v1/access-groups/{g}/members", headers=h,
                       json={"operator_id": world["users"]["mallory"], "is_primary": False})
    assert r.status_code == 201, r.text


async def test_the_platform_operator_is_unchanged(api, world):
    # no tenant claim = the platform's default tenant = the install's operator
    h = _h(f"op-{uuid.uuid4().hex[:6]}")
    r = await api.patch(f"/api/v1/access-roles/{world['builtin']}", headers=h,
                        json=_role_body(f"renamed-{uuid.uuid4().hex[:6]}"))
    assert r.status_code == 200, r.text
    assert r.json()["tenant_id"] is None
