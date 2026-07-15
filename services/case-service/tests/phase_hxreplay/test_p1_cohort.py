"""HxReplay Phase C — cohort replay, honest aggregation, anchoring, GDPR scrub."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from case_service.db.models import (
    CaseAuditLogModel,
    CaseEventLogModel,
    CaseInstanceVariableModel,
    DataLineageEventModel,
    ReplayResultModel,
    ReplayRunModel,
)
from case_service.hxreplay import aggregate as A
from case_service.hxreplay import anchor as ANC
from case_service.hxreplay import runner as R
from tests.conftest import create_case, deploy_case_type

_T0 = datetime(2026, 3, 1, 9, 0, 0, tzinfo=timezone.utc)


# ── aggregate units ─────────────────────────────────────────────────────────────

def _outcome(det=True, diverged=False, cycle=100.0, reason=None):
    m = {"cycle_time_seconds": cycle, "auto_ratio": 0.5, "event_count": 4,
         "manual_count": 2, "auto_count": 2}
    return {"determinacy": "determinate" if det else "indeterminate",
            "divergence_point": "case_created" if diverged else None,
            "exclusion_reason": reason,
            "baseline_metrics": m,
            "counterfactual_metrics": {**m, "cycle_time_seconds": cycle / 2} if diverged else m}


def test_aggregate_coverage_and_caveats():
    s = A.aggregate([_outcome(cycle=100), _outcome(diverged=True, cycle=200),
                     _outcome(det=False, reason="changed rules depend on inputs: x")])
    assert s["cases"] == 3 and s["determinate"] == 2 and s["indeterminate"] == 1
    assert s["coverage_ratio"] == round(2 / 3, 4)
    assert s["diverged"] == 1 and s["divergence_rate"] == 0.5
    assert s["exclusion_profile"]["reasons"] == {"changed rules depend on inputs": 1}
    assert s["baseline"]["cycle_time"]["mean"] == 150.0
    assert s["counterfactual"]["cycle_time"]["mean"] == 100.0
    assert "NOT a random sample" in s["bias_caveat"]
    assert s["assumption"].startswith("exogenous")


def test_result_digest_is_canonical():
    a = ANC.result_digest({"b": 1, "a": 2})
    b = ANC.result_digest({"a": 2, "b": 1})
    assert a == b and len(a) == 64
    assert ANC.result_digest(None) == ANC.result_digest({})


# ── cohort end-to-end ───────────────────────────────────────────────────────────

def _seed_history(session, case_id, ct_id, amount):
    session.add_all([
        CaseEventLogModel(case_id=case_id, case_type_id=ct_id, activity="case_created",
                          activity_type="case_start", actor_type="user", timestamp=_T0),
        CaseEventLogModel(case_id=case_id, case_type_id=ct_id, activity="review",
                          activity_type="stage_enter", stage_id="review",
                          actor_type="user", timestamp=_T0 + timedelta(hours=1)),
    ])
    session.add(CaseInstanceVariableModel(case_id=case_id, full_key="claim.amount",
                                          value_num=amount, written_by="t"))
    session.add(DataLineageEventModel(case_id=case_id, kind="variable_write",
                                      field_path="claim.amount", at=_T0,
                                      after_value={"value": amount}))


_CANDIDATE = {"rules": [{
    "id": "auto", "name": "auto", "rule_type": "when", "enabled": True,
    "definition_json": {
        "conditions": [{"field_path": "claim.amount", "operator": "lt", "value": 500}],
        "actions": [{"action_type": "auto_approve"}],
    },
}]}


@pytest.mark.asyncio
async def test_run_cohort_end_to_end(client, session):
    ct = await deploy_case_type(client, name=f"Cohort CT {uuid.uuid4().hex[:6]}")
    amounts = [450.0, 900.0]        # one diverges (<500), one doesn't
    case_ids = []
    for amt in amounts:
        c = await create_case(client, ct["id"])
        case_ids.append(uuid.UUID(c["id"]))
        _seed_history(session, case_ids[-1], uuid.UUID(ct["id"]), amt)
    await session.commit()

    run = ReplayRunModel(tenant_id="default", kind="cohort",
                         cohort_filter={"case_type_id": ct["id"]},
                         candidate=_CANDIDATE, created_by="tester")
    session.add(run)
    await session.commit()

    summary = await R.run_cohort(session, session, run)
    await session.commit()

    assert summary["cases"] == 2
    assert summary["determinate"] == 2
    assert summary["diverged"] == 1
    assert "bias_caveat" in summary and "assumption" in summary
    assert run.status == "complete"
    # anchored: digest recorded + a chained audit entry on the carrier case
    assert run.anchored is True and len(run.result_digest) == 64
    entry = (await session.execute(
        select(CaseAuditLogModel).where(CaseAuditLogModel.action == "hxreplay.result_anchored")
    )).scalars().all()
    assert any(e.details.get("run_id") == str(run.id) for e in entry)

    results = (await session.execute(
        select(ReplayResultModel).where(ReplayResultModel.run_id == run.id)
    )).scalars().all()
    assert len(results) == 2


@pytest.mark.asyncio
async def test_cohort_requires_case_type_and_caps(session):
    run = ReplayRunModel(tenant_id="default", kind="cohort", cohort_filter={},
                         created_by="t")
    with pytest.raises(R.ReplayError, match="case_type_id"):
        await R.select_cohort(session, "default", {})
    with pytest.raises(R.ReplayError, match="matched no cases"):
        run.cohort_filter = {"case_type_id": str(uuid.uuid4())}
        await R.run_cohort(session, session, run)


# ── cohort API ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cohort_endpoint_validation(client):
    r = await client.post("/api/v1/hxreplay/runs", json={"kind": "cohort"})
    assert r.status_code == 400 and "case_type_id" in r.json()["detail"]
    r = await client.post("/api/v1/hxreplay/runs", json={"kind": "nope", "case_id": "x"})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_cohort_concurrency_cap(client, session):
    session.add(ReplayRunModel(tenant_id="default", kind="cohort", status="running",
                               cohort_filter={"case_type_id": str(uuid.uuid4())},
                               created_by="someone"))
    await session.commit()
    r = await client.post("/api/v1/hxreplay/runs",
                          json={"kind": "cohort",
                                "cohort_filter": {"case_type_id": str(uuid.uuid4())}})
    assert r.status_code == 409


# ── GDPR scrub ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_gdpr_anonymizes_replay_rows(session):
    from case_service.enterprise.gdpr import anonymize_user_data
    run = ReplayRunModel(tenant_id="default", kind="single", case_id=uuid.uuid4(),
                         created_by="forget-me")
    session.add(run)
    await session.flush()
    session.add(ReplayResultModel(
        run_id=run.id, case_id=run.case_id, tenant_id="default",
        baseline_metrics={}, trace={"nodes": [
            {"activity": "approve", "actor_id": "forget-me"},
            {"activity": "other", "actor_id": "keep-me"},
        ]}))
    await session.commit()

    out = await anonymize_user_data(session, "forget-me")
    await session.commit()
    assert out["counts"]["replay_runs"] == 1
    assert out["counts"]["replay_traces"] == 1

    await session.refresh(run)
    assert run.created_by == out["anonymized_id"]
    res = (await session.execute(
        select(ReplayResultModel).where(ReplayResultModel.run_id == run.id)
    )).scalars().one()
    actors = [n["actor_id"] for n in res.trace["nodes"]]
    assert "forget-me" not in actors and "keep-me" in actors
