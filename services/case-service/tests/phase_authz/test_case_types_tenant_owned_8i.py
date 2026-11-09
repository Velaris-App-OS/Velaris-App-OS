"""8i (2026-10-01) — case types become tenant-owned; defaults are copy-on-write.

Before: `case_types` was unique on (name, version) across the install, a
`POST /case-types` body chose its own tenant (omitted = a default for every
tenant), a tenant admin's PATCH of a default changed it for EVERY tenant, and
delete/restore were gated on the privilege alone — any tenant's admin could
delete another tenant's case type, or a default from under everybody.

Pinned:
* a tenant creates in its own tenant; only the platform operator writes a
  default, exactly as before;
* name + version is unique per tenant, so a tenant and the defaults may share;
* a tenant's edit of a default lands in the tenant's FORK (design copied), the
  default is untouched, and the fork replaces it in that tenant's list only;
* an operator's later change to the default is offered to the fork as an
  update, never applied over it;
* delete/restore act on the caller's own rows only;
* the backfill moves a legacy default only when one tenant uses it, only when
  the operator applies it;
* an HxDeploy import into a tenant never overwrites a default or another
  tenant's case type.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from velaris_shared.db.models import (
    CaseInstanceModel,
    CaseTypeModel,
    CaseTypeStageModel,
    TenantModel,
    UserDirectoryModel,
)

from case_service.main import app
from case_service.tenancy import identity as tenancy
from tests.conftest import TestSessionFactory  # type: ignore[attr-defined]
from tests.conftest import _headers  # type: ignore[attr-defined]

pytestmark = pytest.mark.asyncio

DEV = _headers("devadmin", ["admin"])
GHOST = _headers("ghostadmin", ["admin"])        # tenant claim that resolves to nothing
OPS = _headers("defadmin", ["admin"])            # default tenant = platform operator
BASE = "/api/v1/case-types"
DEFN = {"stages": [{"id": "s1", "name": "Intake", "steps": []}]}


@pytest_asyncio.fixture(autouse=True)
async def _case_creators(setup_db):
    """Creating a case needs an access group granting it (2026-10-18)."""
    from tests.conftest import grant_case_create
    await grant_case_create('devadmin', 'defadmin', 'ghostadmin')


@pytest_asyncio.fixture
async def api():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def world():
    tenancy.clear_cache()
    async with TestSessionFactory() as db:
        d = TenantModel(id=uuid.uuid4(), slug="default", name="Default")
        v = TenantModel(id=uuid.uuid4(), slug="dev", name="Dev")
        db.add_all([d, v])
        await db.flush()
        db.add(UserDirectoryModel(id=uuid.uuid4(), user_id="devadmin", tenant_id=str(v.id)))
        db.add(UserDirectoryModel(id=uuid.uuid4(), user_id="ghostadmin", tenant_id="ghost"))
        default_ct = CaseTypeModel(id=uuid.uuid4(), name="Onboarding", version="1.0.0",
                                   definition_json=DEFN, description="the default")
        own_ct = CaseTypeModel(id=uuid.uuid4(), name="DefaultOnly", version="1.0.0",
                               tenant_id=d.id, definition_json=DEFN)
        db.add_all([default_ct, own_ct])
        await db.flush()
        db.add(CaseTypeStageModel(id=uuid.uuid4(), case_type_id=default_ct.id,
                                  stage_id="s1", name="Intake", stage_type="primary", order=1,
                                  definition_json={}))
        await db.commit()
    yield {"default": d, "dev": v, "ct": default_ct, "own": own_ct}
    tenancy.clear_cache()


async def _row(cid) -> CaseTypeModel:
    async with TestSessionFactory() as db:
        return await db.get(CaseTypeModel, uuid.UUID(str(cid)))


async def _names(api, headers) -> list[tuple[str, str | None]]:
    r = await api.get(BASE, headers=headers)
    assert r.status_code == 200, r.text
    return [(i["name"], i["tenant_id"]) for i in r.json()["items"]]


# ── create ──────────────────────────────────────────────────────────────────

async def test_a_tenant_creates_in_its_own_tenant(api, world):
    r = await api.post(BASE, headers=DEV, json={"name": "Claims", "version": "1.0.0",
                                                "definition_json": DEFN})
    assert r.status_code == 201, r.text
    assert r.json()["tenant_id"] == str(world["dev"].id)
    assert (await _row(r.json()["id"])).tenant_key == str(world["dev"].id)


async def test_a_tenant_cannot_create_in_another(api, world):
    r = await api.post(BASE, headers=DEV, json={
        "name": "Planted", "version": "1.0.0", "definition_json": DEFN,
        "tenant_id": str(world["default"].id)})
    # 8d's body-tenant rule answers first (404); the handler's own refusal
    # (403) stands behind it.
    assert r.status_code in (403, 404), r.text
    async with TestSessionFactory() as db:
        assert (await db.execute(select(CaseTypeModel).where(
            CaseTypeModel.name == "Planted"))).first() is None


async def test_the_operator_still_creates_a_default(api, world):
    r = await api.post(BASE, headers=OPS, json={"name": "Template", "version": "1.0.0",
                                                "definition_json": DEFN})
    assert r.status_code == 201, r.text
    assert r.json()["tenant_id"] is None
    assert (await _row(r.json()["id"])).tenant_key == "platform"


async def test_name_and_version_are_unique_per_tenant(api, world):
    # The dev tenant may have its own "Onboarding 1.0.0" beside the default…
    r = await api.post(BASE, headers=DEV, json={"name": "Onboarding", "version": "1.0.0",
                                                "definition_json": DEFN})
    assert r.status_code == 201, r.text
    # …but not two, and the operator may not make a second default.
    r = await api.post(BASE, headers=DEV, json={"name": "Onboarding", "version": "1.0.0",
                                                "definition_json": DEFN})
    assert r.status_code == 409
    r = await api.post(BASE, headers=OPS, json={"name": "Onboarding", "version": "1.0.0",
                                                "definition_json": DEFN})
    assert r.status_code == 409


# ── copy-on-write ───────────────────────────────────────────────────────────

async def test_a_tenant_edit_of_a_default_forks_it(api, world):
    ct = world["ct"]
    r = await api.patch(f"{BASE}/{ct.id}", headers=DEV, json={"description": "dev's"})
    assert r.status_code == 200, r.text
    fork = r.json()
    assert fork["id"] != str(ct.id)
    assert fork["tenant_id"] == str(world["dev"].id)
    assert fork["forked_from_id"] == str(ct.id)
    assert fork["description"] == "dev's"
    assert (await _row(ct.id)).description == "the default"      # untouched

    # The design came with it.
    async with TestSessionFactory() as db:
        stages = (await db.execute(select(CaseTypeStageModel).where(
            CaseTypeStageModel.case_type_id == uuid.UUID(fork["id"])))).scalars().all()
    assert [s.stage_id for s in stages] == ["s1"]

    # A second edit through the default's id lands on the SAME fork.
    r = await api.patch(f"{BASE}/{ct.id}", headers=DEV, json={"description": "again"})
    assert r.json()["id"] == fork["id"]

    # Dev sees its fork instead of the default; the operator still the default.
    dev_list = await _names(api, DEV)
    assert ("Onboarding", str(world["dev"].id)) in dev_list
    assert ("Onboarding", None) not in dev_list
    ops_list = await _names(api, OPS)
    assert ("Onboarding", None) in ops_list
    assert ("Onboarding", str(world["dev"].id)) not in ops_list


async def test_an_operator_update_is_offered_never_applied(api, world):
    ct = world["ct"]
    fork = (await api.patch(f"{BASE}/{ct.id}", headers=DEV,
                            json={"description": "dev's"})).json()
    assert fork["update_available"] is False

    r = await api.patch(f"{BASE}/{ct.id}", headers=OPS, json={"description": "v2 default"})
    assert r.status_code == 200 and r.json()["id"] == str(ct.id)  # in place

    got = (await api.get(f"{BASE}/{fork['id']}", headers=DEV)).json()
    assert got["description"] == "dev's"
    assert got["update_available"] is True


async def test_a_case_on_a_forked_default_runs_on_the_fork(api, world):
    """8k: a caller holding the default's id (an old link, a client app) must
    not create cases on the design its tenant replaced."""
    ct = world["ct"]
    fork = (await api.patch(f"{BASE}/{ct.id}", headers=DEV,
                            json={"description": "dev's"})).json()

    r = await api.post("/api/v1/cases", headers=DEV, json={"case_type_id": str(ct.id)})
    assert r.status_code == 201, r.text
    assert r.json()["case_type_id"] == fork["id"]

    # A tenant that did not fork it still gets the default.
    r = await api.post("/api/v1/cases", headers=OPS, json={"case_type_id": str(ct.id)})
    assert r.status_code == 201, r.text
    assert r.json()["case_type_id"] == str(ct.id)


async def test_another_tenants_row_is_not_editable(api, world):
    r = await api.patch(f"{BASE}/{world['own'].id}", headers=DEV, json={"description": "x"})
    assert r.status_code == 404


# ── delete / restore ────────────────────────────────────────────────────────

async def test_delete_acts_on_the_callers_rows_only(api, world):
    r = await api.delete(f"{BASE}/{world['ct'].id}", headers=DEV)
    assert r.status_code == 403
    r = await api.delete(f"{BASE}/{world['own'].id}", headers=DEV)
    assert r.status_code == 404
    assert not (await _row(world["ct"].id)).is_deleted
    assert not (await _row(world["own"].id)).is_deleted

    r = await api.delete(f"{BASE}/{world['ct'].id}", headers=OPS)
    assert r.status_code == 204


# ── an unresolvable tenant ──────────────────────────────────────────────────

async def test_an_unresolvable_tenant_sees_only_defaults(api, world):
    names = await _names(api, GHOST)
    assert ("Onboarding", None) in names
    assert all(t is None for _, t in names)


# ── backfill ────────────────────────────────────────────────────────────────

async def test_backfill_moves_a_one_tenant_default_only_when_applied(api, world):
    ct = world["ct"]
    async with TestSessionFactory() as db:
        db.add(CaseInstanceModel(case_type_id=ct.id, case_type_version="1.0.0",
                                 status="open", tenant_id=world["dev"].id))
        await db.commit()

    assert (await api.get(f"{BASE}/ownership/backfill", headers=DEV)).status_code == 403

    r = await api.get(f"{BASE}/ownership/backfill", headers=OPS)
    assert r.status_code == 200, r.text
    p = next(i for i in r.json()["items"] if i["id"] == str(ct.id))
    assert p["proposal"] == "move" and p["target_tenant"] == "dev"
    assert (await _row(ct.id)).tenant_id is None                  # preview moved nothing

    r = await api.post(f"{BASE}/ownership/backfill", headers=OPS,
                       json={"case_type_ids": [str(ct.id)]})
    assert r.status_code == 200, r.text
    assert [m["id"] for m in r.json()["moved"]] == [str(ct.id)]
    row = await _row(ct.id)
    assert row.tenant_id == world["dev"].id
    assert row.tenant_key == str(world["dev"].id)


async def test_backfill_keeps_an_unused_default(api, world):
    r = await api.post(f"{BASE}/ownership/backfill", headers=OPS,
                       json={"case_type_ids": [str(world["ct"].id)]})
    assert r.json()["moved"] == []
    assert (await _row(world["ct"].id)).tenant_id is None


# ── HxDeploy import ─────────────────────────────────────────────────────────

async def test_an_import_into_a_tenant_forks_instead_of_overwriting(world):
    from case_service.hxdeploy.packager import apply_bundle

    ct = world["ct"]
    bundle = {"case_types": [{"id": str(uuid.uuid4()), "name": "Onboarding",
                              "version": "1.0.0", "definition_json": DEFN,
                              "description": "imported by dev"}]}
    async with TestSessionFactory() as db:
        res = await apply_bundle(db, bundle, "dev")
        await db.commit()
    assert not res["errors"], res
    assert (await _row(ct.id)).description == "the default"
    async with TestSessionFactory() as db:
        mine = (await db.execute(select(CaseTypeModel).where(
            CaseTypeModel.tenant_id == world["dev"].id))).scalars().all()
    assert [(m.name, m.description, m.forked_from_id) for m in mine] == \
        [("Onboarding", "imported by dev", ct.id)]
