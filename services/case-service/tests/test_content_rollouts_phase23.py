"""Oct item 18a, phases 2 and 3 — and the phase-1 limitations closed with them.

Pinned: targeting (pilot, exclude, field match); policy validation; a scheduled
ramp that steps and stops; a guard that holds or rolls back only on a real,
significant difference with enough cases; the A/B comparison and its verdicts;
shadow evaluation that changes NOTHING about the case; rebase after a rollback
(dry run by default, and never where work would be lost); version status;
webhook events for every change; the negative cache that an approval clears.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from velaris_shared.db.models import (
    CaseAuditLogModel,
    CaseInstanceModel,
    CaseSLAInstanceModel,
    CaseTypeModel,
    ContentRolloutCaseModel,
    ContentRolloutModel,
    ContentRolloutPolicyModel,
    ContentRolloutShadowModel,
    OutboxEventModel,
    TenantModel,
)

from case_service.main import app
from case_service.rollouts import outcomes, service, sweep
from case_service.tenancy import identity as tenancy
from tests.conftest import LIFECYCLE_PROCESS_ID, TestSessionFactory  # type: ignore[attr-defined]
from tests.conftest import _headers  # type: ignore[attr-defined]

pytestmark = pytest.mark.asyncio

ALICE = _headers("alice", ["admin"])
BOB = _headers("bob", ["admin"])
NOW = datetime.now(timezone.utc)

STAGES = {"stages": [{"id": "s1", "order": 1, "steps": [{"id": "a"}]},
                     {"id": "s2", "order": 2, "steps": [{"id": "b"}]}]}


@pytest_asyncio.fixture
async def api():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def world():
    tenancy.clear_cache()
    service.forget_cache()
    service._table_state.update(ok=None, until=0.0)
    async with TestSessionFactory() as db:
        t = TenantModel(id=uuid.uuid4(), slug="default", name="Default")
        db.add(t)
        await db.flush()
        v1 = CaseTypeModel(id=uuid.uuid4(), name="Claims", version="1", tenant_id=t.id,
                           lifecycle_process_id=LIFECYCLE_PROCESS_ID, definition_json=STAGES)
        v2 = CaseTypeModel(id=uuid.uuid4(), name="Claims", version="2", tenant_id=t.id,
                           lifecycle_process_id=LIFECYCLE_PROCESS_ID,
                           definition_json={"stages": [{"id": "s1", "order": 1}]})
        db.add_all([v1, v2])
        await db.commit()
    yield {"t": t, "v1": v1, "v2": v2}
    service.forget_cache()


async def _open(api, w, strategy="percentage", percent=0):
    r = await api.post("/api/v1/rollouts", headers=ALICE, json={
        "baseline_id": str(w["v1"].id), "candidate_id": str(w["v2"].id),
        "strategy": strategy, "percent": percent})
    rid = r.json()["rollout"]["id"]
    assert (await api.post(f"/api/v1/rollouts/{rid}/approve", headers=BOB,
                           json={})).status_code == 200
    return rid


async def _policy(api, rid, **body):
    return await api.put(f"/api/v1/rollouts/{rid}/policy", headers=BOB, json=body)


async def _resolve(w, subject, data=None):
    async with TestSessionFactory() as db:
        return await service.resolve(db, case_type_id=w["v1"].id, tenant=w["t"].id,
                                     subject=subject, data=data)


# ══ targeting ═══════════════════════════════════════════════════════════════


async def test_a_pilot_gets_it_at_zero_and_an_exclusion_never(api, world):
    rid = await _open(api, world, percent=0)
    r = await _policy(api, rid, targeting={"include": ["Pilot@Bank.test"],
                                           "exclude": ["vip-1"]})
    assert r.status_code == 200, r.text
    res = await _resolve(world, " pilot@bank.TEST ")
    assert res.case_type_id == world["v2"].id and res.reason == "pilot"
    await api.patch(f"/api/v1/rollouts/{rid}/percent", headers=BOB, json={"percent": 100})
    res = await _resolve(world, "VIP-1")
    assert res.case_type_id == world["v1"].id and res.reason == "excluded"
    assert (await _resolve(world, "anyone")).case_type_id == world["v2"].id


async def test_a_field_match_limits_who_is_eligible(api, world):
    rid = await _open(api, world, percent=100)
    await _policy(api, rid, targeting={"field": "region", "values": ["EU"]})
    assert (await _resolve(world, "a", {"region": "EU"})).case_type_id == world["v2"].id
    res = await _resolve(world, "a", {"region": "US"})
    assert res.case_type_id == world["v1"].id and res.reason == "not_targeted"
    assert (await _resolve(world, "a", None)).case_type_id == world["v1"].id


async def test_a_requested_rollout_ignores_its_pilot(api, world):
    r = await api.post("/api/v1/rollouts", headers=ALICE, json={
        "baseline_id": str(world["v1"].id), "candidate_id": str(world["v2"].id),
        "strategy": "percentage"})
    rid = r.json()["rollout"]["id"]
    await _policy(api, rid, targeting={"include": ["p"]})
    assert (await _resolve(world, "p")).case_type_id == world["v1"].id


@pytest.mark.parametrize("body,needle", [
    ({"ramp": {"steps": [50, 10], "every_hours": 1}}, "ascending"),
    ({"ramp": {"steps": [10], "every_hours": 0}}, "every_hours"),
    ({"guard": {"metric": "happiness", "max_delta": 1}}, "guard.metric"),
    ({"guard": {"metric": "reopen_rate", "max_delta": 5}}, "fraction"),
    ({"targeting": {"include": ["a"], "exclude": ["A"]}}, "both included and excluded"),
    ({"targeting": {"field": "region"}}, "targeting.values"),
    ({"targeting": {"who": ["x"]}}, "targeting takes"),
])
async def test_a_bad_policy_is_refused_with_a_reason(api, world, body, needle):
    rid = await _open(api, world)
    r = await _policy(api, rid, **body)
    assert r.status_code == 409 and needle in r.text, r.text


async def test_a_ramp_is_for_percentage_only_and_a_closed_rollout_has_no_policy(api, world):
    rid = await _open(api, world, strategy="blue_green")
    assert (await _policy(api, rid, ramp={"steps": [10], "every_hours": 1})).status_code == 409
    await api.post(f"/api/v1/rollouts/{rid}/rollback", headers=BOB, json={})
    assert (await _policy(api, rid, shadow=True)).status_code == 409


# ══ ramp and guard ══════════════════════════════════════════════════════════


async def _sweep(at):
    async with TestSessionFactory() as db:
        return await sweep.sweep(db, now=at)


async def _row(rid):
    async with TestSessionFactory() as db:
        return await db.get(ContentRolloutModel, uuid.UUID(rid))


async def test_a_ramp_steps_on_schedule_and_stops(api, world):
    rid = await _open(api, world, percent=0)
    await _policy(api, rid, ramp={"steps": [10, 50], "every_hours": 1})
    await _sweep(NOW)                                   # schedules the first step
    assert (await _row(rid)).percent == 0
    await _sweep(NOW + timedelta(hours=1, minutes=1))
    assert (await _row(rid)).percent == 10
    await _sweep(NOW + timedelta(hours=1, minutes=30))  # not due yet
    assert (await _row(rid)).percent == 10
    await _sweep(NOW + timedelta(hours=2, minutes=5))
    assert (await _row(rid)).percent == 50
    await _sweep(NOW + timedelta(hours=9))
    row = await _row(rid)
    assert row.percent == 50 and row.state == "rolling"   # never promotes itself
    body = (await api.get(f"/api/v1/rollouts/{rid}", headers=BOB)).json()
    assert [e["actor"] for e in body["events"] if e["action"] == "percent_changed"] == [
        "rollout-ramp", "rollout-ramp"]


async def _seed_arm(w, rid, arm, n, *, breached=0, hours=10.0):
    ct = w["v2"] if arm == "candidate" else w["v1"]
    async with TestSessionFactory() as db:
        for i in range(n):
            created = NOW - timedelta(days=3)
            c = CaseInstanceModel(id=uuid.uuid4(), case_type_id=ct.id, case_type_version=ct.version,
                                  status="resolved", priority="medium", tenant_id=w["t"].id,
                                  created_by="x", data={}, created_at=created,
                                  resolved_at=created + timedelta(hours=hours + (i % 3)))
            db.add(c)
            await db.flush()
            db.add(ContentRolloutCaseModel(case_id=c.id, rollout_id=uuid.UUID(rid),
                                           tenant_id=w["t"].id, arm=arm, bucket=i % 100))
            if i < breached:
                db.add(CaseSLAInstanceModel(id=uuid.uuid4(), case_id=c.id, sla_policy_id="p",
                                            target_id="t", status="breached", started_at=created,
                                            goal_at=created, deadline_at=created))
        await db.commit()


async def test_the_ab_comparison_reports_verdicts(api, world):
    rid = await _open(api, world, percent=50)
    await _seed_arm(world, rid, "baseline", 60, breached=3, hours=10)
    await _seed_arm(world, rid, "candidate", 60, breached=30, hours=4)
    body = (await api.get(f"/api/v1/rollouts/{rid}/outcomes", headers=BOB)).json()
    m = body["metrics"]
    assert m["sla_breach_rate"]["verdict"] == "worse"
    assert m["avg_resolution_hours"]["verdict"] == "better"
    assert body["arms"]["candidate"]["sla_breached"] == 30


async def test_too_few_cases_is_not_a_verdict(api, world):
    rid = await _open(api, world, percent=50)
    await _seed_arm(world, rid, "baseline", 4, breached=0)
    await _seed_arm(world, rid, "candidate", 4, breached=4)
    body = (await api.get(f"/api/v1/rollouts/{rid}/outcomes", headers=BOB)).json()
    assert body["metrics"]["sla_breach_rate"]["verdict"] == "not enough cases"


@pytest.mark.parametrize("action,state", [("hold", "rolling"), ("rollback", "rolled_back")])
async def test_the_guard_acts_on_a_real_difference(api, world, action, state):
    rid = await _open(api, world, percent=20)
    await _policy(api, rid, ramp={"steps": [50, 100], "every_hours": 1},
                  guard={"metric": "sla_breach_rate", "max_delta": 0.1, "min_cases": 30,
                         "action": action})
    await _seed_arm(world, rid, "baseline", 60, breached=3)
    await _seed_arm(world, rid, "candidate", 60, breached=30)
    await _sweep(NOW)
    await _sweep(NOW + timedelta(hours=5))
    row = await _row(rid)
    assert row.state == state and row.percent in (0, 20)        # never ramped past the trip
    async with TestSessionFactory() as db:
        pol = await db.get(ContentRolloutPolicyModel, uuid.UUID(rid))
    assert pol.guard_state == ("held" if action == "hold" else "tripped")
    assert pol.guard_detail["metric"] == "sla_breach_rate"
    events = (await api.get(f"/api/v1/rollouts/{rid}", headers=BOB)).json()["events"]
    assert any(e["action"] == "guard_tripped" and e["actor"] == "rollout-guard" for e in events)


async def test_the_guard_ignores_noise_and_small_arms(api, world):
    rid = await _open(api, world, percent=20)
    await _policy(api, rid, guard={"metric": "sla_breach_rate", "max_delta": 0.01,
                                   "min_cases": 30, "action": "rollback"})
    await _seed_arm(world, rid, "baseline", 40, breached=4)
    await _seed_arm(world, rid, "candidate", 40, breached=6)   # worse, but not significant
    await _sweep(NOW)
    assert (await _row(rid)).state == "rolling"


# ══ shadow ══════════════════════════════════════════════════════════════════


async def test_shadow_evaluates_baseline_cases_and_changes_nothing(api, world):
    rid = await _open(api, world, strategy="flag")                 # dormant: all baseline
    await _policy(api, rid, shadow=True)
    await _seed_arm(world, rid, "baseline", 3)
    async with TestSessionFactory() as db:
        before = [(c.id, c.case_type_id, c.status, dict(c.data), c.current_stage_id)
                  for c in (await db.execute(select(CaseInstanceModel))).scalars().all()]
        c0 = before[0][0]
        db.add(CaseAuditLogModel(id=uuid.uuid4(), case_id=c0, action="x", actor_id="x",
                                 timestamp=NOW))
        case = await db.get(CaseInstanceModel, c0)
        case.current_stage_id = "s2"                  # a stage v2 does not define
        await db.commit()
    out = await _sweep(NOW)
    assert out["shadowed"] == 3
    async with TestSessionFactory() as db:
        after = [(c.id, c.case_type_id, c.status, dict(c.data))
                 for c in (await db.execute(select(CaseInstanceModel))).scalars().all()]
        rows = (await db.execute(select(ContentRolloutShadowModel))).scalars().all()
    assert sorted(after) == sorted([b[:4] for b in before])      # nothing changed
    flagged = [r for r in rows if r.case_id == c0][0]
    assert flagged.diverged and flagged.detail["stages_missing_in_candidate"] == ["s2"]
    body = (await api.get(f"/api/v1/rollouts/{rid}/shadow", headers=BOB)).json()
    assert body["evaluated"] == 3 and body["diverged"] >= 1
    assert (await _sweep(NOW))["shadowed"] == 0                   # once per case


# ══ rebase, version status, events, cache ═══════════════════════════════════


async def test_rebase_after_rollback_moves_only_what_is_safe(api, world):
    rid = await _open(api, world, strategy="blue_green")
    await api.patch(f"/api/v1/rollouts/{rid}/percent", headers=BOB, json={"percent": 100})
    await _seed_arm(world, rid, "candidate", 2)
    async with TestSessionFactory() as db:
        cases = (await db.execute(select(CaseInstanceModel))).scalars().all()
        for c in cases:
            c.resolved_at, c.status = None, "open"
        cases[0].current_stage_id = "s1"
        cases[1].current_stage_id = "gone"
        await db.commit()
        safe, lost = cases[0].id, cases[1].id
    r = await api.post(f"/api/v1/rollouts/{rid}/rebase", headers=BOB, json={"reason": "cleanup"})
    assert r.status_code == 409                                  # still open
    await api.post(f"/api/v1/rollouts/{rid}/rollback", headers=BOB, json={})
    dry = (await api.post(f"/api/v1/rollouts/{rid}/rebase", headers=BOB,
                          json={"reason": "cleanup"})).json()
    assert [m["case_id"] for m in dry["movable"]] == [str(safe)]
    assert [b["case_id"] for b in dry["blocked"]] == [str(lost)]
    async with TestSessionFactory() as db:
        assert (await db.get(CaseInstanceModel, safe)).case_type_id == world["v2"].id  # dry run
    await api.post(f"/api/v1/rollouts/{rid}/rebase", headers=BOB,
                   json={"reason": "cleanup", "dry_run": False})
    async with TestSessionFactory() as db:
        assert (await db.get(CaseInstanceModel, safe)).case_type_id == world["v1"].id
        assert (await db.get(CaseInstanceModel, lost)).case_type_id == world["v2"].id
        audit = (await db.execute(select(CaseAuditLogModel).where(
            CaseAuditLogModel.action == "rollout_rebased"))).scalars().all()
    assert [a.case_id for a in audit] == [safe]


async def test_version_status_names_retired_and_withdrawn(api, world):
    rid = await _open(api, world, percent=10)
    await api.post(f"/api/v1/rollouts/{rid}/promote", headers=BOB, json={})
    v = {x["case_type_id"]: x for x in
         (await api.get("/api/v1/rollouts/versions", headers=BOB)).json()["versions"]}
    assert v[str(world["v1"].id)]["status"] == "retired"
    assert v[str(world["v1"].id)]["new_cases_go_to"] == str(world["v2"].id)


async def test_every_change_is_a_webhook_event_on_the_baseline(api, world):
    rid = await _open(api, world, strategy="flag")
    await api.patch(f"/api/v1/rollouts/{rid}/percent", headers=BOB, json={"percent": 100})
    async with TestSessionFactory() as db:
        rows = (await db.execute(select(OutboxEventModel).where(
            OutboxEventModel.event_type.like("rollout.%")))).scalars().all()
    assert [r.event_type for r in rows] == ["rollout.requested", "rollout.approved",
                                           "rollout.activated"]
    assert {r.case_type_id for r in rows} == {world["v1"].id}
    assert "reason" not in rows[0].payload                        # no free text leaves


async def test_the_negative_cache_does_not_hide_a_new_rollout(api, world):
    assert (await _resolve(world, "a")).rollout_id is None     # caches "no rollouts"
    rid = await _open(api, world, strategy="blue_green")
    await api.patch(f"/api/v1/rollouts/{rid}/percent", headers=BOB, json={"percent": 100})
    assert (await _resolve(world, "a")).case_type_id == world["v2"].id


async def test_a_staged_candidate_belongs_to_its_tenant(api, world):
    from velaris_shared.db.models import EnvironmentRegistryModel
    async with TestSessionFactory() as db:
        db.add(EnvironmentRegistryModel(id=uuid.uuid4(), tenant_id="default", name="p",
                                        label="P", order_index=1, import_api_key="k-18a-t"))
        await db.commit()
    r = await api.post("/api/v1/deploy/import", headers={"Authorization": "Bearer k-18a-t"}, json={
        "bundle_schema_version": "2", "needs_configuration": [],
        "rollout_request": {"strategy": "flag"},
        "case_types": [{"id": str(uuid.uuid4()), "name": "Claims", "version": "7",
                        "definition_json": {"stages": []}}]})
    assert r.status_code == 200, r.text
    async with TestSessionFactory() as db:
        cand = await db.get(CaseTypeModel, uuid.UUID(r.json()["rollouts"][0]["candidate_id"]))
    assert cand.tenant_id == world["t"].id


def test_the_statistics_are_sane():
    same = outcomes.compare_rates(10, 100, 10, 100)
    assert same["delta"] == 0 and same["p_value"] == 1.0
    big = outcomes.compare_rates(5, 200, 60, 200)
    assert big["p_value"] < 0.001
    assert outcomes.verdict(big, 200, 200) == "worse"
    assert outcomes.verdict(big, 200, 200, lower_is_better=False) == "better"


# ══ who may (least-privileged first, then refused) ══════════════════════════
# Studio's proxy reached /api/v1/rollouts only from 2026-11-01 (HxDeploy →
# Rollouts tab was "Not Found"); these pin who the route now answers.

async def test_least_privileged_reader_and_requester_succeed(api, world):
    manager = _headers("mgr", ["manager"])            # read only
    dev = _headers("dev1", ["developer"])             # read + request
    assert (await api.get("/api/v1/rollouts?state=open", headers=manager)).status_code == 200
    r = await api.post("/api/v1/rollouts", headers=dev, json={
        "baseline_id": str(world["v1"].id), "candidate_id": str(world["v2"].id),
        "strategy": "percentage", "percent": 0})
    assert r.status_code in (200, 201), r.text


async def test_refused_users(api, world):
    viewer = _headers("vic", ["viewer"])
    worker = _headers("cw", ["case_worker"])
    manager = _headers("mgr", ["manager"])
    dev = _headers("dev1", ["developer"])
    assert (await api.get("/api/v1/rollouts?state=open")).status_code == 401
    for who in (viewer, worker):
        assert (await api.get("/api/v1/rollouts?state=open", headers=who)).status_code == 403
    body = {"baseline_id": str(world["v1"].id), "candidate_id": str(world["v2"].id),
            "strategy": "percentage", "percent": 0}
    assert (await api.post("/api/v1/rollouts", headers=manager, json=body)).status_code == 403
    rid = (await api.post("/api/v1/rollouts", headers=dev, json=body)).json()["rollout"]["id"]
    # Only an admin approves — the developer who asked cannot.
    assert (await api.post(f"/api/v1/rollouts/{rid}/approve", headers=dev, json={})).status_code == 403
