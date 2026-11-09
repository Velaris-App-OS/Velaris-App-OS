"""8e part a (2026-10-01) — forms, rules, data models and calendars are
inherited defaults with copy-on-write tenant forks (as case types in 8i).

Pinned, for each of the four:
* a tenant creates in its own tenant; the operator still creates a default;
* name (+ version) is unique per tenant, not per install;
* a tenant's edit of a default lands in its fork; the default is untouched;
* the fork replaces the default in that tenant's list, and a read of the
  DEFAULT's id by that tenant returns the fork — case types name these by id;
* another tenant's row is 404 by id and absent from lists;
* a default may be deleted only by the operator.
And at runtime: rule-based assignment for a case uses its tenant's fork.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from velaris_shared.db.models import (
    BusinessCalendarModel,
    CaseInstanceModel,
    CaseTypeModel,
    DataModelModel,
    FormDefinitionModel,
    RuleDefinitionModel,
    TenantModel,
    UserDirectoryModel,
)

from case_service.main import app
from case_service.tenancy import identity as tenancy
from tests.conftest import TestSessionFactory  # type: ignore[attr-defined]
from tests.conftest import _headers  # type: ignore[attr-defined]

pytestmark = pytest.mark.asyncio

DEV = _headers("devadmin", ["admin"])
OPS = _headers("defadmin", ["admin"])

RULE_DEF = {"conditions": [], "actions": []}

#: (base url, model, create body, patch body, field the patch changes, list key)
KINDS = {
    "forms": ("/api/v1/forms", FormDefinitionModel,
              {"name": "Intake", "version": "1.0.0", "definition_json": {"fields": []}},
              {"definition_json": {"fields": [{"id": "dev"}]}}, "definition_json", "items"),
    "rules": ("/api/v1/rules", RuleDefinitionModel,
              {"name": "Route", "version": "1.0.0", "rule_type": "when", "scope": "global",
               "definition_json": RULE_DEF},
              {"priority": 7}, "priority", "items"),
    "data_models": ("/api/v1/data-models", DataModelModel,
                    {"name": "Customer", "version": "1.0.0", "definition_json": {"fields": []}},
                    {"definition_json": {"fields": [{"id": "dev"}]}}, "definition_json", "items"),
}


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
        await db.commit()
    yield {"default": d, "dev": v}
    tenancy.clear_cache()


async def _row(model, rid):
    async with TestSessionFactory() as db:
        return await db.get(model, uuid.UUID(str(rid)))


@pytest.mark.parametrize("kind", list(KINDS))
async def test_inherited_with_copy_on_write(api, world, kind):
    base, model, body, patch, field, key = KINDS[kind]

    # The operator makes a default, as before.
    r = await api.post(base, headers=OPS, json=body)
    assert r.status_code == 201, r.text
    default_id = r.json()["id"]
    assert (await _row(model, default_id)).tenant_id is None
    before = getattr(await _row(model, default_id), field)

    # Dev may have its own of the same name + version, but not two.
    own = await api.post(base, headers=DEV, json={**body, "version": "9.9.9"})
    assert own.status_code == 201, own.text
    assert (await _row(model, own.json()["id"])).tenant_id == world["dev"].id
    assert (await api.post(base, headers=DEV, json={**body, "version": "9.9.9"})).status_code == 409

    # Dev edits the default → its fork; the default is untouched.
    r = await api.patch(f"{base}/{default_id}", headers=DEV, json=patch)
    assert r.status_code == 200, r.text
    fork_id = r.json()["id"]
    assert fork_id != default_id
    fork = await _row(model, fork_id)
    assert fork.tenant_id == world["dev"].id and str(fork.forked_from_id) == default_id
    assert getattr(await _row(model, default_id), field) == before

    # Reading the default's id as dev gives the fork; as the operator, the default.
    assert (await api.get(f"{base}/{default_id}", headers=DEV)).json()["id"] == fork_id
    assert (await api.get(f"{base}/{default_id}", headers=OPS)).json()["id"] == default_id

    # Lists: dev sees its fork and own row, not the default; the operator
    # sees the default and none of dev's.
    dev_ids = {i["id"] for i in (await api.get(base, headers=DEV)).json()[key]}
    ops_ids = {i["id"] for i in (await api.get(base, headers=OPS)).json()[key]}
    assert fork_id in dev_ids and own.json()["id"] in dev_ids and default_id not in dev_ids
    assert default_id in ops_ids and fork_id not in ops_ids and own.json()["id"] not in ops_ids

    # Another tenant's row is 404 by id; a default is the operator's to delete.
    assert (await api.get(f"{base}/{own.json()['id']}", headers=OPS)).status_code == 404
    assert (await api.delete(f"{base}/{default_id}", headers=DEV)).status_code == 403
    assert (await api.delete(f"{base}/{default_id}", headers=OPS)).status_code == 204


async def test_another_tenants_rows_are_404(api, world):
    async with TestSessionFactory() as db:
        f = FormDefinitionModel(id=uuid.uuid4(), name="Theirs", version="1",
                                definition_json={}, tenant_id=world["default"].id)
        r = RuleDefinitionModel(id=uuid.uuid4(), name="Theirs", version="1", rule_type="when",
                                scope="global", definition_json=RULE_DEF,
                                tenant_id=world["default"].id)
        db.add_all([f, r])
        await db.commit()
    assert (await api.get(f"/api/v1/forms/{f.id}", headers=DEV)).status_code == 404
    assert (await api.patch(f"/api/v1/forms/{f.id}", headers=DEV,
                            json={"definition_json": {}})).status_code == 404
    assert (await api.get(f"/api/v1/rules/{r.id}", headers=DEV)).status_code == 404
    # …and a rule id named in a BODY is refused the same way.
    res = await api.post("/api/v1/rules/evaluate/batch", headers=DEV,
                         json={"rule_ids": [str(r.id)], "context": {}})
    assert res.status_code == 404


async def test_calendars_follow_the_same_rule(api, world):
    r = await api.post("/api/v1/admin/calendars", headers=OPS, json={"name": "Office"})
    assert r.status_code == 201, r.text
    default_id = r.json()["id"]
    r = await api.patch(f"/api/v1/admin/calendars/{default_id}", headers=DEV,
                        json={"work_start_hour": 7})
    assert r.status_code == 200, r.text
    assert r.json()["id"] != default_id
    assert (await _row(BusinessCalendarModel, default_id)).work_start_hour == 9
    dev_names = [(c["id"], c["tenant_id"]) for c in
                 (await api.get("/api/v1/admin/calendars", headers=DEV)).json()]
    assert (r.json()["id"], str(world["dev"].id)) in dev_names
    assert all(cid != default_id for cid, _ in dev_names)


@pytest.mark.parametrize("kind", list(KINDS))
async def test_a_changed_default_is_offered_to_its_fork(api, world, kind):
    """8k: the fork is told, on list and get; it is never changed."""
    base, model, body, patch, field, key = KINDS[kind]
    default_id = (await api.post(base, headers=OPS, json=body)).json()["id"]
    fork = (await api.patch(f"{base}/{default_id}", headers=DEV, json=patch)).json()
    got = (await api.get(f"{base}/{fork['id']}", headers=DEV)).json()
    assert got["update_available"] is False

    r = await api.patch(f"{base}/{default_id}", headers=OPS, json=patch)
    assert r.status_code == 200 and r.json()["id"] == default_id, r.text

    got = (await api.get(f"{base}/{fork['id']}", headers=DEV)).json()
    assert got["update_available"] is True
    listed = {i["id"]: i for i in (await api.get(base, headers=DEV)).json()[key]}
    assert listed[fork["id"]]["update_available"] is True
    assert getattr(await _row(model, fork["id"]), field) == fork[field]   # untouched
    # The operator's own default is never "behind" itself.
    assert (await api.get(f"{base}/{default_id}", headers=OPS)).json()["update_available"] is False


async def test_a_changed_default_calendar_is_offered(api, world):
    default_id = (await api.post("/api/v1/admin/calendars", headers=OPS,
                                 json={"name": "Office"})).json()["id"]
    fork_id = (await api.patch(f"/api/v1/admin/calendars/{default_id}", headers=DEV,
                               json={"work_start_hour": 7})).json()["id"]
    flags = lambda rows: {c["id"]: c["update_available"] for c in rows}  # noqa: E731
    assert flags((await api.get("/api/v1/admin/calendars", headers=DEV)).json())[fork_id] is False
    await api.patch(f"/api/v1/admin/calendars/{default_id}", headers=OPS,
                    json={"work_start_hour": 8})
    assert flags((await api.get("/api/v1/admin/calendars", headers=DEV)).json())[fork_id] is True


async def test_rule_based_assignment_uses_the_cases_tenants_fork(world):
    from velaris_shared.tenancy import inherit
    from case_service.core.assignment_router import _resolve_rule_based

    def _rule(target):
        return {"rule_type": "when", "conditions": [],
                "actions": [{"action_type": "assign_to", "target": "queue", "value": target}]}

    async with TestSessionFactory() as db:
        default = RuleDefinitionModel(id=uuid.uuid4(), name="Assign", version="1",
                                      rule_type="when", scope="global",
                                      definition_json=_rule("platform-q"))
        db.add(default)
        await db.flush()
        fork = await inherit.fork(db, RuleDefinitionModel, default, world["dev"].id)
        fork.definition_json = _rule("dev-q")
        ct = CaseTypeModel(id=uuid.uuid4(), name="T", version="1", definition_json={})
        db.add(ct)
        await db.flush()
        dev_case = CaseInstanceModel(case_type_id=ct.id, case_type_version="1",
                                     status="open", tenant_id=world["dev"].id)
        ops_case = CaseInstanceModel(case_type_id=ct.id, case_type_version="1",
                                     status="open", tenant_id=world["default"].id)
        db.add_all([dev_case, ops_case])
        await db.commit()

        rule = {"strategy": "rule_based", "rule_id": str(default.id), "target": "fallback"}
        assert await _resolve_rule_based(db, rule, {"case_id": str(dev_case.id)}) == ("queue", "dev-q")
        assert await _resolve_rule_based(db, rule, {"case_id": str(ops_case.id)}) == ("queue", "platform-q")
