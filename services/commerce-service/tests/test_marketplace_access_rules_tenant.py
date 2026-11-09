"""Another tenant's admin cannot set or reset a marketplace access rule on a
group that is not theirs — the path guard (velaris_shared.tenancy.path_guard)
answers 404. Pinned when commerce-service took the route (R5e step 7): the
platform probe's sweep cannot tell this route's real case from a no-op on an id
that is not an access group, so the real case is proven here.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
import uuid

from httpx import ASGITransport, AsyncClient

from velaris_shared.auth.jwt_handler import create_dev_token
from velaris_shared.config import get_settings
from velaris_shared.db.models import AccessGroupModel, PortalModel, TenantModel, UserDirectoryModel

from tests.conftest import TestSessionFactory, app


def _h(user: str) -> dict:
    s = get_settings()
    return {"Authorization": "Bearer " + create_dev_token(
        user_id=user, username=user, roles=["admin"], secret=s.auth_secret,
        private_key=s.auth_rsa_private_key or "")}


async def _world():
    acme = uuid.uuid4()
    async with TestSessionFactory() as db:
        db.add_all([TenantModel(id=acme, slug="acme", name="Acme"),
                    TenantModel(id=uuid.uuid4(), slug="evil", name="Evil")])
        db.add(UserDirectoryModel(id=uuid.uuid4(), user_id="mallory", tenant_id="evil"))
        db.add(UserDirectoryModel(id=uuid.uuid4(), user_id="ann", tenant_id="acme"))
        portal = PortalModel(name="Staff", portal_type="staff")
        db.add(portal)
        await db.flush()
        group = AccessGroupModel(name="Acme group", tenant_id=str(acme), portal_id=portal.id,
                                 role_ids=[])
        db.add(group)
        await db.commit()
    return group.id


async def test_another_tenants_admin_cannot_touch_the_rule():
    gid = await _world()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        put = await c.put(f"/api/v1/marketplace/access-rules/{gid}", headers=_h("mallory"),
                          json={"rule_type": "deny_all"})
        delete = await c.delete(f"/api/v1/marketplace/access-rules/{gid}", headers=_h("mallory"))
    assert put.status_code == 404 and delete.status_code == 404


async def test_the_groups_own_admin_still_can():
    gid = await _world()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        delete = await c.delete(f"/api/v1/marketplace/access-rules/{gid}", headers=_h("ann"))
    assert delete.status_code != 404, delete.text
