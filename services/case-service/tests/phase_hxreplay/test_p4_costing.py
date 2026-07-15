"""HxReplay P4 — case costing: rate cards, automatic time rollup, cost delta.

Pins: cost derives ONLY from determinate outcomes (trust-class inheritance),
manual-vs-auto attribution, HxGuard gating on rates, and the replay summary
cost block appearing exactly when a rate card exists.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from case_service.costing import service as C
from case_service.db.models import CaseEventLogModel, RateCardModel
from tests.conftest import create_case, deploy_case_type

_T0 = datetime(2026, 6, 1, 9, 0, 0, tzinfo=timezone.utc)


# ── cost block (pure) ───────────────────────────────────────────────────────────

def _o(det="determinate", base_manual=3600, cf_manual=0):
    return {"determinacy": det, "divergence_point": "x", "exclusion_reason": None,
            "baseline_metrics": {"manual_seconds": base_manual},
            "counterfactual_metrics": {"manual_seconds": cf_manual}}


def test_cost_block_math_and_trust_class():
    b = C.cost_block([_o(), _o(base_manual=7200, cf_manual=3600)], hourly_rate=100.0)
    assert b["cases"] == 2
    assert b["baseline_cost"] == 300.0        # (3600+7200)/3600 × 100
    assert b["counterfactual_cost"] == 100.0
    assert b["savings"] == 200.0
    assert "automated work costs 0" in b["basis"]
    # estimated/indeterminate outcomes NEVER contribute to a cost figure
    poisoned = _o(det="estimated", base_manual=999999, cf_manual=0)
    assert C.cost_block([_o(), poisoned], 100.0)["savings"] == 100.0
    assert C.cost_block([poisoned], 100.0) is None          # nothing determinate
    assert C.cost_block([_o()], None) is None               # no rate card


# ── rollup ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_case_time_rollup_attribution(session):
    case_id, ct_id = uuid.uuid4(), uuid.uuid4()
    session.add_all([
        CaseEventLogModel(case_id=case_id, case_type_id=ct_id, activity="review",
                          activity_type="stage_enter", stage_id="review",
                          actor_type="user", duration_seconds=3600, timestamp=_T0),
        CaseEventLogModel(case_id=case_id, case_type_id=ct_id, activity="auto_check",
                          activity_type="step_complete", stage_id="review",
                          actor_type="system", duration_seconds=60,
                          timestamp=_T0 + timedelta(minutes=1)),
        CaseEventLogModel(case_id=case_id, case_type_id=ct_id, activity="no_duration",
                          activity_type="step_complete", actor_type="user",
                          timestamp=_T0 + timedelta(minutes=2)),
    ])
    await session.commit()
    r = await C.case_time_rollup(session, case_id)
    assert r["total_recorded_seconds"] == 3660
    assert r["manual_seconds"] == 3600 and r["auto_seconds"] == 60
    assert r["by_stage"] == {"review": 3660}
    assert r["by_activity"] == {"review": 3600, "auto_check": 60}


# ── rate card API + gating ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rate_card_roundtrip_and_validation(client):
    r = await client.get("/api/v1/costing/rate-card")
    assert r.status_code == 200 and r.json()["configured"] is False
    r = await client.put("/api/v1/costing/rate-card", json={"hourly_rate": 85.5, "currency": "EUR"})
    assert r.status_code == 200, r.text
    got = (await client.get("/api/v1/costing/rate-card")).json()
    assert got == {**got, "configured": True, "hourly_rate": 85.5, "currency": "EUR"}
    # update, not duplicate
    await client.put("/api/v1/costing/rate-card", json={"hourly_rate": 90})
    assert (await client.get("/api/v1/costing/rate-card")).json()["hourly_rate"] == 90
    assert (await client.put("/api/v1/costing/rate-card",
                             json={"hourly_rate": -5})).status_code == 422


@pytest.mark.asyncio
async def test_rate_card_requires_auth(anon_client):
    assert (await anon_client.get("/api/v1/costing/rate-card")).status_code in (401, 403)
    assert (await anon_client.put("/api/v1/costing/rate-card",
                                  json={"hourly_rate": 1})).status_code in (401, 403)


@pytest.mark.asyncio
async def test_case_time_endpoint(client, session):
    ct = await deploy_case_type(client, name=f"Cost CT {uuid.uuid4().hex[:6]}")
    case = await create_case(client, ct["id"])
    session.add(CaseEventLogModel(case_id=uuid.UUID(case["id"]), case_type_id=uuid.UUID(ct["id"]),
                                  activity="review", activity_type="stage_enter",
                                  stage_id="review", actor_type="user",
                                  duration_seconds=1800, timestamp=_T0))
    await session.commit()
    r = await client.get(f"/api/v1/costing/cases/{case['id']}/time")
    assert r.status_code == 200 and r.json()["manual_seconds"] == 1800
    assert (await client.get(f"/api/v1/costing/cases/{uuid.uuid4()}/time")).status_code == 404


# ── replay summary integration ──────────────────────────────────────────────────

_CANDIDATE = {"rules": [{
    "id": "auto", "name": "auto", "rule_type": "when", "enabled": True,
    "definition_json": {
        "conditions": [{"field_path": "claim.amount", "operator": "lt", "value": 500}],
        "actions": [{"action_type": "auto_approve"}]},
}]}


@pytest.mark.asyncio
async def test_replay_summary_gains_cost_block_with_rate_card(client, session):
    from case_service.db.models import CaseInstanceVariableModel, DataLineageEventModel
    ct = await deploy_case_type(client, name=f"CostRep CT {uuid.uuid4().hex[:6]}")
    case = await create_case(client, ct["id"])
    cid, ctid = uuid.UUID(case["id"]), uuid.UUID(ct["id"])
    session.add_all([
        CaseEventLogModel(case_id=cid, case_type_id=ctid, activity="case_created",
                          activity_type="case_start", actor_type="user", timestamp=_T0),
        CaseEventLogModel(case_id=cid, case_type_id=ctid, activity="review",
                          activity_type="stage_enter", stage_id="review", actor_type="user",
                          duration_seconds=7200, timestamp=_T0 + timedelta(minutes=1)),
    ])
    session.add(CaseInstanceVariableModel(case_id=cid, full_key="claim.amount",
                                          value_num=450.0, written_by="t"))
    session.add(DataLineageEventModel(case_id=cid, kind="variable_write",
                                      field_path="claim.amount", at=_T0,
                                      after_value={"value": 450.0}))
    await session.commit()

    # no rate card → no cost block
    r1 = await client.post("/api/v1/hxreplay/runs",
                           json={"kind": "single", "case_id": case["id"],
                                 "candidate": _CANDIDATE})
    assert r1.status_code == 201 and r1.json()["summary"]["cost"] is None

    await client.put("/api/v1/costing/rate-card", json={"hourly_rate": 100, "currency": "USD"})
    r2 = await client.post("/api/v1/hxreplay/runs",
                           json={"kind": "single", "case_id": case["id"],
                                 "candidate": _CANDIDATE})
    cost = r2.json()["summary"]["cost"]
    # auto-approve at intake elides the 2h manual review → $200 saved
    assert cost["baseline_cost"] == 200.0
    assert cost["counterfactual_cost"] == 0.0
    assert cost["savings"] == 200.0 and cost["currency"] == "USD"
