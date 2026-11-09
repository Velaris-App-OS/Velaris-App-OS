"""R7 — archiving a tenant closes it (2026-10-30).

Archiving set `status = "archived"` and nothing read it: an offboarded tenant's
staff kept signing in, and its portal kept taking submissions. Now its people
are refused on every request, its portal answers 404, and restoring the tenant
opens it again — through the real routes, so the cache clears with them.
"""
from __future__ import annotations

import uuid

import pytest_asyncio

from case_service.tenancy import identity as tenancy
from velaris_shared.db.models import TenantModel, UserDirectoryModel


def _h(user_id: str, roles: list[str]) -> dict[str, str]:
    from velaris_shared.auth.jwt_handler import create_dev_token
    from velaris_shared.config import get_settings
    s = get_settings()
    t = create_dev_token(user_id=user_id, username=user_id, roles=roles,
                         secret=s.auth_secret, private_key=s.auth_rsa_private_key or "")
    return {"Authorization": f"Bearer {t}"}


@pytest_asyncio.fixture
async def world(session):
    tenancy.clear_cache()
    suffix = uuid.uuid4().hex[:6]
    gone = TenantModel(id=uuid.uuid4(), slug=f"gone-{suffix}", name="Gone")
    stays = TenantModel(id=uuid.uuid4(), slug=f"stays-{suffix}", name="Stays")
    session.add_all([gone, stays,
                     UserDirectoryModel(id=uuid.uuid4(), user_id=f"g-{suffix}", tenant_id=gone.slug),
                     UserDirectoryModel(id=uuid.uuid4(), user_id=f"s-{suffix}", tenant_id=stays.slug)])
    await session.commit()
    return {"gone": gone, "stays": stays, "g": f"g-{suffix}", "s": f"s-{suffix}"}


async def test_archiving_closes_the_tenant_and_restoring_opens_it(client, world):
    operator = _h("r7-operator", ["admin"])          # default tenant = the install's operator
    staff = _h(world["g"], ["staff"])
    other = _h(world["s"], ["staff"])
    assert (await client.get("/api/v1/cases", headers=staff)).status_code == 200

    r = await client.delete(f"/api/v1/tenants/{world['gone'].id}", headers=operator)
    assert r.status_code == 204, r.text
    r = await client.get("/api/v1/cases", headers=staff)
    assert r.status_code == 403 and "closed" in r.json()["detail"]
    assert (await client.get("/api/v1/cases", headers=other)).status_code == 200
    # superadmin still reaches everything (break-glass, and who restores it)
    assert (await client.get("/api/v1/cases", headers=_h("r7-root", ["superadmin"]))).status_code == 200

    r = await client.patch(f"/api/v1/tenants/{world['gone'].id}", headers=operator,
                           json={"status": "active"})
    assert r.status_code == 200, r.text
    assert (await client.get("/api/v1/cases", headers=staff)).status_code == 200


async def test_an_archived_tenants_portal_is_closed(client, world):
    slug = world["gone"].slug
    before = await client.get(f"/api/v1/portal/{slug}")
    assert before.status_code != 404 or "Portal not found" not in before.text
    r = await client.delete(f"/api/v1/tenants/{world['gone'].id}", headers=_h("r7-operator", ["admin"]))
    assert r.status_code == 204
    r = await client.get(f"/api/v1/portal/{slug}")
    assert r.status_code == 404
    assert (await client.get(f"/api/v1/portal/{world['stays'].slug}")).status_code == before.status_code


async def test_a_tenant_admin_cannot_archive_another_tenant(client, world):
    r = await client.delete(f"/api/v1/tenants/{world['gone'].id}", headers=_h(world["s"], ["admin"]))
    assert r.status_code in (403, 404)
    assert (await client.get("/api/v1/cases", headers=_h(world["g"], ["staff"]))).status_code == 200


async def test_tenant_writes_are_the_platform_operators(client, world):
    # Every tenant's admin holds the tenants privilege: they could create
    # tenants, raise their own quotas and delete their own tenant (2026-10-30).
    own_admin = _h(world["s"], ["admin"])
    sid = world["stays"].id
    r = await client.post("/api/v1/tenants", headers=own_admin,
                          json={"name": "x", "slug": f"new-{uuid.uuid4().hex[:6]}"})
    assert r.status_code == 403
    r = await client.patch(f"/api/v1/tenants/{sid}", headers=own_admin, json={"max_users": 100000})
    assert r.status_code == 403
    assert (await client.delete(f"/api/v1/tenants/{sid}", headers=own_admin)).status_code == 403
    assert (await client.delete(f"/api/v1/tenants/{sid}/permanent", headers=own_admin)).status_code == 403
    # still reads its own tenant
    assert (await client.get(f"/api/v1/tenants/{sid}", headers=own_admin)).status_code == 200
    # the operator still does all of it
    r = await client.post("/api/v1/tenants", headers=_h("r7-operator", ["admin"]),
                          json={"name": "x", "slug": f"new-{uuid.uuid4().hex[:6]}"})
    assert r.status_code == 201, r.text


async def test_metrics_are_not_served_to_the_internet(client):
    assert (await client.get("/metrics")).status_code == 200                       # scraper, direct
    assert (await client.get("/metrics", headers={"X-Forwarded-For": "127.0.0.1"})).status_code == 200
    for fwd in ("203.0.113.9", "127.0.0.1, 203.0.113.9"):                            # via an ingress
        assert (await client.get("/metrics", headers={"X-Forwarded-For": fwd})).status_code == 404
        assert (await client.get("/health/deep", headers={"X-Forwarded-For": fwd})).status_code == 404


async def test_an_archived_tenants_schedules_are_skipped_and_said(session, world):
    from datetime import datetime, timezone
    from velaris_shared.db.models import ScheduleModel, ScheduleRunModel
    from sqlalchemy import select
    from case_service.scheduler.runner import loop
    world["gone"].status = "archived"
    await session.commit()
    tenancy.clear_cache()
    cols = {c.name for c in ScheduleModel.__table__.columns}
    kw = dict(id=uuid.uuid4(), name="nightly", tenant_id=world["gone"].slug, action_kind="noop",
              created_by="r7-operator")
    for k, v in (("cron", "0 1 * * *"), ("enabled", True), ("timezone", "UTC"), ("action_config", {}),
                 ("next_run_at", datetime.now(timezone.utc))):
        if k in cols:
            kw[k] = v
    row = ScheduleModel(**kw)
    session.add(row)
    await session.commit()
    await loop._fire(row.id, missed=False)
    runs = (await session.execute(select(ScheduleRunModel).where(
        ScheduleRunModel.schedule_id == row.id))).scalars().all()
    assert runs and runs[-1].status == "skipped" and "archived" in str(runs[-1].detail)
