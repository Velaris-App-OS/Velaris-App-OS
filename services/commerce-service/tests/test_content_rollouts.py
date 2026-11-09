"""Oct item 18a — staged rollout of content versions.

Pinned: no rollout (or no table) is exactly the old behaviour; a requested
rollout changes nothing until a DIFFERENT person approves it; percentage is
sticky per subject; flag and blue-green are on/off; promote redirects the old
id forward and rollback redirects the candidate back, with in-flight cases
untouched; one open rollout per case type (refused, with the three ways
forward — each exercised); rules and processes are refused by name; the API is
tenant-scoped; EVERY create path goes through the resolver (exercised one by
one, and a static sweep holds new paths to it); HxDeploy stages a candidate
beside the baseline and never in place.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import ast
import pathlib
import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from velaris_shared.db.models import (
    CaseInstanceModel,
    CaseTypeModel,
    ContentRolloutCaseModel,
    ContentRolloutModel,
    EnvironmentRegistryModel,
    ScheduleModel,
    TenantModel,
    UserDirectoryModel,
)

from case_service.hxdeploy import packages
from tests.conftest import app  # the in-process gateway: commerce + case-service
from case_service.rollouts import service
from case_service.tenancy import identity as tenancy
from tests.conftest import LIFECYCLE_PROCESS_ID, TestSessionFactory  # type: ignore[attr-defined]
from tests.conftest import _headers  # type: ignore[attr-defined]

pytestmark = pytest.mark.asyncio

ALICE = _headers("alice", ["admin"])      # requests
BOB = _headers("bob", ["admin"])          # approves


@pytest_asyncio.fixture(autouse=True)
async def _case_creators(setup_db):
    """Creating a case needs an access group granting it (2026-10-18)."""
    from tests.conftest import grant_case_create
    await grant_case_create('alice', 'bob', 'mal')


@pytest_asyncio.fixture
async def api():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _ct(t, version, name="Claims", **kw):
    return CaseTypeModel(id=uuid.uuid4(), name=name, version=version,
                         lifecycle_process_id=LIFECYCLE_PROCESS_ID, tenant_id=t.id,
                         definition_json={"stages": [], "v": version}, **kw)


@pytest_asyncio.fixture
async def world():
    tenancy.clear_cache()
    service._table_state.update(ok=None, until=0.0)
    async with TestSessionFactory() as db:
        t = TenantModel(id=uuid.uuid4(), slug="default", name="Default",
                        settings={"portal": {"enabled": True}})
        o = TenantModel(id=uuid.uuid4(), slug="other", name="Other")
        db.add_all([t, o])
        await db.flush()
        db.add(UserDirectoryModel(id=uuid.uuid4(), user_id="mal", tenant_id="other"))
        v1, v2, v3 = _ct(t, "1.0.0", portal_enabled=True), _ct(t, "2.0.0"), _ct(t, "3.0.0")
        other_ct = _ct(t, "1.0.0", name="Other type")
        db.add_all([v1, v2, v3, other_ct])
        await db.commit()
    yield {"t": t, "o": o, "v1": v1, "v2": v2, "v3": v3, "other": other_ct}
    tenancy.clear_cache()


async def _request(api, w, cand="v2", strategy="percentage", percent=0, base="v1", h=ALICE):
    return await api.post("/api/v1/rollouts", headers=h, json={
        "baseline_id": str(w[base].id), "candidate_id": str(w[cand].id),
        "strategy": strategy, "percent": percent})


async def _open(api, w, strategy="percentage", percent=0, cand="v2", base="v1"):
    r = await _request(api, w, cand=cand, strategy=strategy, percent=percent, base=base)
    assert r.status_code == 201, r.text
    rid = r.json()["rollout"]["id"]
    r = await api.post(f"/api/v1/rollouts/{rid}/approve", headers=BOB, json={})
    assert r.status_code == 200, r.text
    return rid


async def _resolve(w, subject, ct="v1"):
    async with TestSessionFactory() as db:
        return await service.resolve(db, case_type_id=w[ct].id, tenant=w["t"].id,
                                     subject=subject)


# ══ the empty case ══════════════════════════════════════════════════════════


async def test_no_rollout_resolves_every_id_to_itself(api, world):
    for key in ("v1", "v2", "v3"):
        res = await _resolve(world, "anyone", key)
        assert res.case_type_id == world[key].id and res.rollout_id is None
    r = await api.post("/api/v1/cases", headers=ALICE,
                       json={"case_type_id": str(world["v1"].id), "data": {}})
    assert r.status_code == 201, r.text
    assert r.json()["case_type_id"] == str(world["v1"].id)
    async with TestSessionFactory() as db:
        assert (await db.execute(select(ContentRolloutCaseModel))).first() is None


async def test_a_missing_table_is_immediate_not_an_error(world):
    service._table_state.update(ok=False, until=10**12)
    try:
        res = await _resolve(world, "x")
        assert res.case_type_id == world["v1"].id and res.rollout_id is None
    finally:
        service._table_state.update(ok=None, until=0.0)


async def test_a_requested_rollout_changes_nothing(api, world):
    r = await _request(api, world, percent=100)
    assert r.status_code == 201 and r.json()["rollout"]["state"] == "requested"
    for s in ("a", "b", "c"):
        assert (await _resolve(world, s)).case_type_id == world["v1"].id


async def test_the_requester_cannot_approve_their_own_rollout(api, world):
    rid = (await _request(api, world)).json()["rollout"]["id"]
    r = await api.post(f"/api/v1/rollouts/{rid}/approve", headers=ALICE, json={})
    assert r.status_code == 403, r.text


# ══ strategies ══════════════════════════════════════════════════════════════


async def test_percentage_is_sticky_and_roughly_right(api, world):
    await _open(api, world, percent=30)
    arms = {}
    for i in range(400):
        res = await _resolve(world, f"customer-{i}")
        arms[i] = res.case_type_id
        assert res.arm == ("candidate" if res.case_type_id == world["v2"].id else "baseline")
    share = sum(1 for v in arms.values() if v == world["v2"].id) / 400
    assert 0.2 < share < 0.4
    for i in range(0, 400, 37):                   # the same person, the same arm
        assert (await _resolve(world, f"customer-{i}")).case_type_id == arms[i]
    # Asking by the CANDIDATE's id is the same family: the bucket still decides.
    assert (await _resolve(world, "customer-5", "v2")).case_type_id == arms[5]


async def test_raising_the_percentage_only_moves_people_forward(api, world):
    rid = await _open(api, world, percent=20)
    before = {i: (await _resolve(world, f"c{i}")).case_type_id for i in range(200)}
    r = await api.patch(f"/api/v1/rollouts/{rid}/percent", headers=BOB, json={"percent": 60})
    assert r.status_code == 200, r.text
    for i in range(200):
        if before[i] == world["v2"].id:
            assert (await _resolve(world, f"c{i}")).case_type_id == world["v2"].id


async def test_a_flag_is_dormant_until_switched_on(api, world):
    rid = await _open(api, world, strategy="flag")
    r = await api.get(f"/api/v1/rollouts/{rid}", headers=BOB)
    assert r.json()["rollout"]["state"] == "dormant"
    assert (await _resolve(world, "a")).case_type_id == world["v1"].id
    assert (await api.patch(f"/api/v1/rollouts/{rid}/percent", headers=BOB,
                            json={"percent": 40})).status_code == 409
    await api.patch(f"/api/v1/rollouts/{rid}/percent", headers=BOB, json={"percent": 100})
    assert (await _resolve(world, "a")).case_type_id == world["v2"].id
    await api.patch(f"/api/v1/rollouts/{rid}/percent", headers=BOB, json={"percent": 0})
    assert (await _resolve(world, "a")).case_type_id == world["v1"].id


async def test_blue_green_switches_everyone_and_back(api, world):
    rid = await _open(api, world, strategy="blue_green")
    assert {(await _resolve(world, f"s{i}")).case_type_id for i in range(30)} == {world["v1"].id}
    await api.patch(f"/api/v1/rollouts/{rid}/percent", headers=BOB, json={"percent": 100})
    assert {(await _resolve(world, f"s{i}")).case_type_id for i in range(30)} == {world["v2"].id}
    await api.patch(f"/api/v1/rollouts/{rid}/percent", headers=BOB, json={"percent": 0})
    assert {(await _resolve(world, f"s{i}")).case_type_id for i in range(30)} == {world["v1"].id}


async def test_immediate_promotes_on_approval(api, world):
    rid = await _open(api, world, strategy="immediate")
    r = await api.get(f"/api/v1/rollouts/{rid}", headers=BOB)
    assert r.json()["rollout"]["state"] == "promoted"
    assert (await _resolve(world, "a")).case_type_id == world["v2"].id


async def test_the_approver_may_override_the_request(api, world):
    rid = (await _request(api, world, strategy="percentage", percent=90)).json()["rollout"]["id"]
    r = await api.post(f"/api/v1/rollouts/{rid}/approve", headers=BOB,
                       json={"strategy": "flag", "percent": 0})
    assert r.json()["rollout"]["strategy"] == "flag" and r.json()["rollout"]["state"] == "dormant"


# ══ promote, roll back, and pinning ═════════════════════════════════════════


async def test_promote_redirects_the_old_id_forward(api, world):
    rid = await _open(api, world, percent=10)
    assert (await api.post(f"/api/v1/rollouts/{rid}/promote", headers=BOB, json={})).status_code == 200
    for s in ("a", "b", "c"):
        assert (await _resolve(world, s, "v1")).case_type_id == world["v2"].id
    # And a v3 rollout now runs against v2 — reached even from v1's id.
    rid3 = await _open(api, world, cand="v3", base="v2", strategy="blue_green")
    await api.patch(f"/api/v1/rollouts/{rid3}/percent", headers=BOB, json={"percent": 100})
    assert (await _resolve(world, "a", "v1")).case_type_id == world["v3"].id


async def test_rollback_sends_new_cases_back_and_leaves_old_ones_pinned(api, world):
    rid = await _open(api, world, strategy="blue_green")
    await api.patch(f"/api/v1/rollouts/{rid}/percent", headers=BOB, json={"percent": 100})
    r = await api.post("/api/v1/cases", headers=ALICE,
                       json={"case_type_id": str(world["v1"].id), "data": {}})
    in_flight = r.json()["id"]
    assert r.json()["case_type_id"] == str(world["v2"].id)
    assert (await api.post(f"/api/v1/rollouts/{rid}/rollback", headers=BOB,
                           json={"reason": "complaints"})).status_code == 200
    assert (await _resolve(world, "a", "v2")).case_type_id == world["v1"].id
    async with TestSessionFactory() as db:
        case = await db.get(CaseInstanceModel, uuid.UUID(in_flight))
        assert case.case_type_id == world["v2"].id
        rec = await db.get(ContentRolloutCaseModel, case.id)
        assert rec.arm == "candidate" and rec.via == "api"
    body = (await api.get(f"/api/v1/rollouts/{rid}", headers=BOB)).json()
    assert [e["action"] for e in body["events"]] == ["requested", "approved", "switched",
                                                     "rolled_back"]
    assert body["rollout"]["arms"]["candidate"] == 1


# ══ the lock ════════════════════════════════════════════════════════════════


async def test_one_open_rollout_per_case_type_and_each_way_forward(api, world):
    first = await _open(api, world, percent=10)
    r = await _request(api, world, cand="v3")
    assert r.status_code == 409
    d = r.json()["detail"]
    assert d["open_rollout"]["id"] == first and d["options"] == ["promote", "rollback", "supersede"]

    # supersede: v2 rolled back, v3 requested against v1 — needs approval again.
    r = await api.post(f"/api/v1/rollouts/{first}/supersede", headers=BOB, json={
        "candidate_id": str(world["v3"].id), "strategy": "percentage", "percent": 5})
    assert r.status_code == 201, r.text
    new = r.json()["rollout"]
    assert new["baseline_id"] == str(world["v1"].id) and new["state"] == "requested"
    old = (await api.get(f"/api/v1/rollouts/{first}", headers=BOB)).json()["rollout"]
    assert old["state"] == "superseded" and old["superseded_by"] == new["id"]
    assert (await _resolve(world, "x", "v2")).case_type_id == world["v1"].id

    # roll back frees the lock; so does promote (checked in the redirect test).
    assert (await api.post(f"/api/v1/rollouts/{new['id']}/rollback", headers=BOB,
                           json={})).status_code == 200
    assert (await _request(api, world, cand="v3")).status_code == 201


async def test_the_lock_is_the_database_not_a_check(world):
    """Two rows with the same open key cannot both exist, whatever raced."""
    from sqlalchemy.exc import IntegrityError
    async with TestSessionFactory() as db:
        key = service._open_key(world["t"].id, "case_type", "Claims")
        for cand in ("v2", "v3"):
            db.add(ContentRolloutModel(
                id=uuid.uuid4(), tenant_id=world["t"].id, artifact_kind="case_type",
                artifact_name="Claims", baseline_id=world["v1"].id,
                candidate_id=world[cand].id, strategy="flag", percent=0,
                state="requested", open_key=key))
        with pytest.raises(IntegrityError):
            await db.flush()


# ══ refusals ════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("kind", ["rule", "process"])
async def test_rules_and_processes_are_refused_by_name(api, world, kind):
    r = await api.post("/api/v1/rollouts", headers=ALICE, json={
        "artifact_kind": kind, "baseline_id": str(world["v1"].id),
        "candidate_id": str(world["v2"].id), "strategy": "flag"})
    assert r.status_code == 409 and "inside a new case type version" in r.text


async def test_two_different_case_types_are_not_a_rollout(api, world):
    r = await _request(api, world, cand="other")
    assert r.status_code == 409 and "different case types" in r.text


async def test_the_api_is_tenant_scoped_and_gated(api, world):
    rid = await _open(api, world)
    mal = _headers("mal", ["admin"])
    assert (await api.get(f"/api/v1/rollouts/{rid}", headers=mal)).status_code == 404
    assert (await api.post(f"/api/v1/rollouts/{rid}/rollback", headers=mal,
                           json={})).status_code == 404
    assert (await api.get("/api/v1/rollouts", headers=mal)).json()["count"] == 0
    assert (await _request(api, world, cand="v3", h=mal)).status_code == 404
    assert (await api.get("/api/v1/rollouts", headers=_headers("v", ["viewer"]))).status_code == 403
    assert (await api.get("/api/v1/rollouts")).status_code in (401, 403)


# ══ every create path goes through the resolver ═════════════════════════════


@pytest_asyncio.fixture
async def all_candidate(api, world):
    """A rollout sending 100% of new cases to v2."""
    rid = await _open(api, world, strategy="blue_green")
    await api.patch(f"/api/v1/rollouts/{rid}/percent", headers=BOB, json={"percent": 100})
    return rid


async def _landed(case_id, world, via):
    async with TestSessionFactory() as db:
        case = await db.get(CaseInstanceModel, uuid.UUID(str(case_id)))
        rec = await db.get(ContentRolloutCaseModel, case.id)
    assert case.case_type_id == world["v2"].id and case.case_type_version == "2.0.0"
    assert rec is not None and rec.arm == "candidate" and rec.via == via
    return case


async def test_path_api_create(api, world, all_candidate):
    r = await api.post("/api/v1/cases", headers=ALICE,
                       json={"case_type_id": str(world["v1"].id), "data": {"customer_id": "c1"}})
    await _landed(r.json()["id"], world, "api")


async def test_path_child_case_and_it_now_keeps_the_parents_tenant(api, world, all_candidate):
    parent = (await api.post("/api/v1/cases", headers=ALICE,
                             json={"case_type_id": str(world["other"].id), "data": {}})).json()
    r = await api.post(f"/api/v1/cases/{parent['id']}/children", headers=ALICE,
                       json={"case_type_id": str(world["v1"].id), "data": {}})
    assert r.status_code == 201, r.text
    child = await _landed(r.json()["id"], world, "child")
    assert child.tenant_id == world["t"].id          # was NULL before 18a


async def test_path_case_action_split(api, world, all_candidate):
    parent = (await api.post("/api/v1/cases", headers=ALICE,
                             json={"case_type_id": str(world["other"].id), "data": {}})).json()
    r = await api.post(f"/api/v1/cases/{parent['id']}/actions/split", headers=ALICE,
                       json={"params": {"case_type_id": str(world["v1"].id)}})
    assert r.status_code == 200, r.text
    async with TestSessionFactory() as db:
        child = (await db.execute(select(CaseInstanceModel).where(
            CaseInstanceModel.parent_case_id == uuid.UUID(parent["id"])))).scalars().one()
    await _landed(child.id, world, "case_action")


async def test_path_portal_and_it_now_has_a_tenant(api, world, all_candidate):
    r = await api.post("/api/v1/portal/default/submit", json={
        "case_type_id": str(world["v1"].id), "submitter_name": "Pat",
        "submitter_email": "pat@example.test", "subject": "s", "description": "d"})
    assert r.status_code == 200, r.text
    case = await _landed(r.json()["case_id"], world, "portal")
    assert case.tenant_id == world["t"].id           # was NULL before 18a


async def test_path_channels_intake(world, all_candidate):
    from case_service.intake import service as intake
    async with TestSessionFactory() as db:
        (await db.get(CaseTypeModel, world["v1"].id)).intake_trigger = "webhook"
        out = await intake.ingest(db, case_type_id=world["v1"].id,
                                  payload={"email": "x@example.test"})
        await db.commit()
    await _landed(out["case_id"], world, "intake_webhook")


async def test_path_scheduler(world, all_candidate):
    from case_service.scheduler.actions import CaseAction
    schedule = ScheduleModel(id=uuid.uuid4(), name="nightly", tenant_id="default")
    async with TestSessionFactory() as db:
        await CaseAction().run(db, schedule, {"case_type_id": str(world["v1"].id), "data": {}})
        await db.commit()
        case = (await db.execute(select(CaseInstanceModel).where(
            CaseInstanceModel.created_by == "schedule:nightly"))).scalars().one()
    await _landed(case.id, world, "schedule")


async def test_path_hxfusion_spawn(world, all_candidate):
    from velaris_shared.db.models import ProcessDefinitionModel, ProcessInstanceModel

    from case_service.hxfusion import engine
    instance = SimpleNamespace(id=uuid.uuid4(), tenant_id=world["t"].id)
    async with TestSessionFactory() as db:          # PostgreSQL enforces the binding's FK
        d = ProcessDefinitionModel(id=uuid.uuid4(), name="p", bpmn_xml="<x/>")
        db.add(d)
        await db.flush()
        db.add(ProcessInstanceModel(id=instance.id, definition_id=d.id))
        await db.commit()
    node = SimpleNamespace(extensions={"case_type_id": str(world["v1"].id)})
    await engine._spawn_child_case(instance, node, {"customer_id": "c9"})
    async with TestSessionFactory() as db:
        case = (await db.execute(select(CaseInstanceModel).where(
            CaseInstanceModel.tenant_id == world["t"].id))).scalars().one()
    await _landed(case.id, world, "hxfusion_spawn")


async def test_path_checkout(api, world):
    from case_service.internal_api import order_case_types as ct_svc
    from commerce_service.checkout import service as checkout
    async with TestSessionFactory() as db:
        order_v1 = await ct_svc.resolve_order_case_type(db, None)
        order_v2 = CaseTypeModel(id=uuid.uuid4(), name=order_v1.name, version="9.0.0",
                                 lifecycle_process_id=order_v1.lifecycle_process_id,
                                 definition_json=order_v1.definition_json)
        db.add(order_v2)
        await db.commit()
    rid = (await api.post("/api/v1/rollouts", headers=ALICE, json={
        "baseline_id": str(order_v1.id), "candidate_id": str(order_v2.id),
        "strategy": "blue_green"})).json()["rollout"]["id"]
    await api.post(f"/api/v1/rollouts/{rid}/approve", headers=BOB, json={"percent": 100})
    async with TestSessionFactory() as db:
        out = await checkout.create_order(
            db, tenant="default", basket=[{"sku": "S", "name": "N", "quantity": 1,
                                           "unit_price": 0}],
            customer={"email": "buyer@example.test"}, shipping={})
        await db.commit()
        case = (await db.execute(select(CaseInstanceModel).where(
            CaseInstanceModel.created_by == "hxcheckout"))).scalars().one()
        rec = await db.get(ContentRolloutCaseModel, case.id)
    assert out["order_id"] and case.case_type_id == order_v2.id and rec.via == "checkout"


#: Every place that constructs a case, and why it may skip the resolver.
EXEMPT = {
    ("hxdbmigrate/migrate.py", "create_case_instance"): "copies EXISTING cases between databases",
    ("hxdbmigrate/sync.py", "create_case_instance"): "copies EXISTING cases between databases",
    ("hxdeploy/twin.py", "CaseInstanceModel"): "the F-04 twin rehearses, it is not production",
    ("commerce:marketplace/packs.py", "CaseInstanceModel"): "synthetic sample data at pack install",
    ("healing/service.py", "CaseInstanceModel"): "an incident about THIS case type version",
}


def test_every_case_construction_resolves_or_is_named_exempt():
    """A create path that bypasses the resolver silently ignores the rollout."""
    # Both services' code (commerce moved out at R5e step 7; its paths are
    # prefixed `commerce:`). Case creation itself is case-service's: commerce
    # opens an order's case through the internal API (internal_api/order_cases.py).
    services = pathlib.Path(__file__).resolve().parents[2]
    roots = {"": services / "case-service" / "case_service",
             "commerce:": services / "commerce-service" / "commerce_service"}
    found, where = {}, {}
    for prefix, root in roots.items():
        for py in root.rglob("*.py"):
            rel = prefix + str(py.relative_to(root))
            where[rel] = py
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
                if name in ("CaseInstanceModel", "create_case_instance"):
                    found.setdefault((rel, name), 0)
                    found[(rel, name)] += 1
    unresolved = []
    for (rel, name) in found:
        if (rel, name) in EXEMPT:
            continue
        src = where[rel].read_text(encoding="utf-8")
        if "_rollouts.resolve_case_type(" not in src or "_rollouts.record(" not in src:
            unresolved.append(f"{rel}:{name}")
    assert not unresolved, f"create paths that ignore rollouts: {unresolved}"
    assert set(EXEMPT) <= set(found), "an exemption names a path that no longer exists"


# ══ HxDeploy ════════════════════════════════════════════════════════════════


@pytest_asyncio.fixture
async def env(world):
    async with TestSessionFactory() as db:
        db.add(EnvironmentRegistryModel(id=uuid.uuid4(), tenant_id="default", name="prod",
                                        label="Prod", order_index=1, import_api_key="k-18a"))
        await db.commit()
    return {"Authorization": "Bearer k-18a"}


def _bundle(version="4.0.0", request=None, **extra):
    b = {"bundle_schema_version": "2", "needs_configuration": [],
         "case_types": [{"id": str(uuid.uuid4()), "name": "Claims", "version": version,
                         "definition_json": {"stages": [], "v": version}}], **extra}
    if request:
        b["rollout_request"] = request
    return b


async def test_a_staged_import_installs_beside_and_waits_for_approval(api, world, env):
    r = await api.post("/api/v1/deploy/import", headers=env,
                       json=_bundle(request={"strategy": "percentage", "percent": 20}))
    assert r.status_code == 200, r.text
    ro = r.json()["rollouts"][0]
    assert ro["state"] == "requested" and ro["source"] == "hxdeploy" and ro["percent"] == 20
    async with TestSessionFactory() as db:
        base = await db.get(CaseTypeModel, uuid.UUID(ro["baseline_id"]))
        cand = await db.get(CaseTypeModel, uuid.UUID(ro["candidate_id"]))
    assert cand.version == "4.0.0" and base.definition_json["v"] != "4.0.0"   # not in place
    assert (await _resolve(world, "a", "v1")).case_type_id == world["v1"].id  # no effect yet
    r = await api.post(f"/api/v1/rollouts/{ro['id']}/approve", headers=BOB, json={})
    assert r.status_code == 200 and r.json()["rollout"]["state"] == "rolling"


async def test_a_staged_import_that_would_change_shared_things_is_refused(api, world, env):
    r = await api.post("/api/v1/deploy/import", headers=env, json=_bundle(
        request={"strategy": "flag"},
        form_definitions=[{"id": str(uuid.uuid4()), "name": "F"}]))
    assert r.status_code == 409 and "form_definitions" in r.text
    async with TestSessionFactory() as db:
        assert (await db.execute(select(CaseTypeModel).where(
            CaseTypeModel.version == "4.0.0"))).first() is None


async def test_a_staged_import_respects_the_lock(api, world, env):
    await _open(api, world)
    r = await api.post("/api/v1/deploy/import", headers=env,
                       json=_bundle(request={"strategy": "flag"}))
    assert r.status_code == 409 and r.json()["detail"]["options"] == [
        "promote", "rollback", "supersede"]


async def test_an_immediate_import_does_not_overwrite_under_an_open_rollout(api, world, env):
    await _open(api, world)
    r = await api.post("/api/v1/deploy/import", headers=env, json=_bundle(version="1.0.0"))
    assert r.status_code == 200 and any("rollout is open" in e for e in r.json()["errors"])


async def test_an_old_style_import_still_replaces_in_place(api, world, env):
    """No request: exactly the old behaviour — and a name with several versions
    no longer crashes the import (it used to raise MultipleResultsFound)."""
    r = await api.post("/api/v1/deploy/import", headers=env, json=_bundle(version="1.0.0"))
    assert r.status_code == 200 and not r.json()["errors"], r.text
    assert "rollouts" not in r.json()


async def test_the_request_rides_outside_the_seal():
    b = _bundle()
    assert packages.checksum_of(b) == packages.checksum_of(
        {**b, "rollout_request": {"strategy": "flag"}})


@pytest.mark.parametrize("request_", [{"strategy": "percentage", "percent": 250},
                                      {"strategy": "flag", "percent": 40},
                                      {"strategy": "canary-ish"}])
async def test_a_malformed_request_is_refused_not_a_500(api, world, env, request_):
    r = await api.post("/api/v1/deploy/import", headers=env, json=_bundle(request=request_))
    assert r.status_code == 409, r.text
