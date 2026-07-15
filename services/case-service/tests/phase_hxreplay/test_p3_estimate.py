"""HxReplay P3 — substitution ladder for lost-flow-action nodes (opt-in, labelled).

Pins: rung matching (policy_alternative / authored default / Monte-Carlo),
seeded-deterministic MC, estimated results NEVER entering hard metrics, and the
default (estimate=False) still excluding honestly.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from case_service.db.models import (
    CaseEventLogModel,
    CaseInstanceVariableModel,
    DataLineageEventModel,
)
from case_service.hxreplay import aggregate as A
from case_service.hxreplay import engine as E
from case_service.hxreplay import substitute as SUB
from tests.conftest import create_case, deploy_case_type

_T0 = datetime(2026, 5, 1, 9, 0, 0, tzinfo=timezone.utc)


def _t(minutes):
    return _T0 + timedelta(minutes=minutes)


# ── rung units ──────────────────────────────────────────────────────────────────

def test_match_policy_alternative_is_conservative():
    ev = {"stage_id": "review", "step_id": None, "activity": "review"}
    ds = [
        {"decision_point": "other", "source": "ai", "policy_alternative": "reject"},
        {"decision_point": "review", "source": "policy", "policy_alternative": None},
        {"decision_point": "review", "source": "ai", "policy_alternative": "approve"},
    ]
    assert SUB.match_policy_alternative(ds, ev) == "approve"
    # no name match → None (never borrows the wrong decision)
    assert SUB.match_policy_alternative(ds[:1], ev) is None
    # policy-source entries carry no alternative → not a substitution source
    assert SUB.match_policy_alternative([ds[1]], ev) is None


def test_policy_default_for_reads_authored_default_only():
    dj = {"stages": [{"id": "review", "name": "Review", "default_outcome": "approved"},
                     {"id": "intake", "name": "Intake"}]}
    assert SUB.policy_default_for(dj, "review") == "approved"
    assert SUB.policy_default_for(dj, "intake") is None      # nothing authored
    assert SUB.policy_default_for(dj, "ghost") is None
    assert SUB.policy_default_for(None, "review") is None


def test_monte_carlo_is_seeded_deterministic():
    stats = {"remaining_seconds": [1000.0, 2000.0, 3000.0],
             "outcomes": {"approved": 2, "rejected": 1}, "cases": 3}
    a = SUB.monte_carlo_estimate(60.0, stats, seed=42)
    b = SUB.monte_carlo_estimate(60.0, stats, seed=42)
    c = SUB.monte_carlo_estimate(60.0, stats, seed=43)
    assert a == b                                   # reproducible
    assert a != c                                   # seed actually matters
    assert 1060.0 <= a["cycle_time_seconds"] <= 3060.0
    assert a["outcome_distribution"] == {"approved": 0.6667, "rejected": 0.3333}
    assert SUB.monte_carlo_estimate(60.0, {"remaining_seconds": []}, seed=1) is None


# ── engine e2e ──────────────────────────────────────────────────────────────────

def _rule(rid, threshold):
    return {"id": rid, "name": rid, "rule_type": "when", "enabled": True,
            "scope": "global", "scope_target_id": None,
            "definition_json": {
                "conditions": [{"field_path": "claim.amount", "operator": "lt",
                                "value": threshold}],
                "actions": [{"action_type": "auto_approve"}]}}


async def _seed_lost_at_review(session, ct_id):
    """Value absent at intake, 50 at the review node → baseline(<100) fires at
    'review', candidate(<10) doesn't → lost flow action at a STAGE node."""
    case_id = uuid.uuid4()
    session.add_all([
        CaseEventLogModel(case_id=case_id, case_type_id=ct_id, activity="case_created",
                          activity_type="case_start", timestamp=_t(0)),
        CaseEventLogModel(case_id=case_id, case_type_id=ct_id, activity="review",
                          activity_type="stage_enter", stage_id="review", timestamp=_t(1)),
    ])
    session.add(CaseInstanceVariableModel(case_id=case_id, full_key="claim.amount",
                                          value_num=50.0, written_by="t"))
    session.add(DataLineageEventModel(case_id=case_id, kind="variable_write",
                                      field_path="claim.amount", at=_t(0.5),
                                      after_value={"value": 50.0}))
    return case_id


def _seed_history_cases(session, ct_id, n=5):
    """Other cases of the type: entered review, ended 1000·i seconds later."""
    for i in range(1, n + 1):
        cid = uuid.uuid4()
        enter = _t(0)
        session.add_all([
            CaseEventLogModel(case_id=cid, case_type_id=ct_id, activity="review",
                              activity_type="stage_enter", stage_id="review",
                              timestamp=enter),
            CaseEventLogModel(case_id=cid, case_type_id=ct_id, activity="case_resolved",
                              activity_type="case_end",
                              outcome="approved" if i % 2 else "rejected",
                              timestamp=enter + timedelta(seconds=1000 * i)),
        ])


@pytest.mark.asyncio
async def test_estimate_off_stays_indeterminate(session):
    ct_id = uuid.uuid4()
    case_id = await _seed_lost_at_review(session, ct_id)
    await session.commit()
    r = await E.replay_case(session, case_id, [_rule("auto", 100)], [_rule("auto", 10)])
    assert r["determinacy"] == "indeterminate"
    assert "enable estimation" in r["exclusion_reason"]


@pytest.mark.asyncio
async def test_monte_carlo_rung_estimates_and_is_reproducible(session):
    ct_id = uuid.uuid4()
    case_id = await _seed_lost_at_review(session, ct_id)
    _seed_history_cases(session, ct_id)
    await session.commit()

    r1 = await E.replay_case(session, case_id, [_rule("auto", 100)], [_rule("auto", 10)],
                             case_type_id=ct_id, estimate=True)
    r2 = await E.replay_case(session, case_id, [_rule("auto", 100)], [_rule("auto", 10)],
                             case_type_id=ct_id, estimate=True)
    assert r1["determinacy"] == "estimated"
    m = r1["counterfactual_metrics"]
    assert m["estimated"] is True and m["source"] == "monte_carlo"
    assert m["history_cases"] == 5 and m["samples"] == SUB.MC_SAMPLES
    assert m["cycle_time_seconds"] >= 60          # time-to-node + sampled remaining
    assert "ESTIMATED" in r1["trace"]["note"]
    assert r1["counterfactual_metrics"] == r2["counterfactual_metrics"]   # seeded


@pytest.mark.asyncio
async def test_policy_default_rung_beats_monte_carlo(session):
    ct_id = uuid.uuid4()
    case_id = await _seed_lost_at_review(session, ct_id)
    _seed_history_cases(session, ct_id)
    await session.commit()
    dj = {"stages": [{"id": "review", "default_outcome": "approved"}]}
    r = await E.replay_case(session, case_id, [_rule("auto", 100)], [_rule("auto", 10)],
                            case_type_id=ct_id, estimate=True, definition_json=dj)
    assert r["determinacy"] == "estimated"
    assert r["counterfactual_metrics"]["source"] == "policy_default"
    assert r["counterfactual_metrics"]["substituted_outcome"] == "approved"


@pytest.mark.asyncio
async def test_policy_alternative_rung_from_recorded_decision(client, session):
    from case_service.db import repository as repo
    ct = await deploy_case_type(client, name=f"P3 CT {uuid.uuid4().hex[:6]}")
    case = await create_case(client, ct["id"])
    case_id, ct_id = uuid.UUID(case["id"]), uuid.UUID(ct["id"])

    session.add_all([
        CaseEventLogModel(case_id=case_id, case_type_id=ct_id, activity="case_created",
                          activity_type="case_start", timestamp=_t(0)),
        CaseEventLogModel(case_id=case_id, case_type_id=ct_id, activity="review",
                          activity_type="stage_enter", stage_id="review", timestamp=_t(1)),
    ])
    session.add(CaseInstanceVariableModel(case_id=case_id, full_key="claim.amount",
                                          value_num=50.0, written_by="t"))
    session.add(DataLineageEventModel(case_id=case_id, kind="variable_write",
                                      field_path="claim.amount", at=_t(0.5),
                                      after_value={"value": 50.0}))
    await repo.append_audit_entry(session, data={
        "case_id": case_id, "action": "decision_point", "actor_type": "system",
        "details": {"decision_point": "review", "source": "ai", "confidence": 0.93,
                    "decision": "auto_approve", "policy_alternative": "manual_review",
                    "reason": "ai:test"}})
    await session.commit()

    r = await E.replay_case(session, case_id, [_rule("auto", 100)], [_rule("auto", 10)],
                            case_type_id=ct_id, estimate=True)
    assert r["determinacy"] == "estimated"
    assert r["counterfactual_metrics"]["source"] == "policy_alternative"
    assert r["counterfactual_metrics"]["substituted_outcome"] == "manual_review"


# ── aggregation separation ──────────────────────────────────────────────────────

def test_estimated_never_enters_hard_metrics():
    det = {"determinacy": "determinate", "divergence_point": None, "exclusion_reason": None,
           "baseline_metrics": {"cycle_time_seconds": 100.0, "auto_ratio": 0.5},
           "counterfactual_metrics": {"cycle_time_seconds": 100.0, "auto_ratio": 0.5}}
    est = {"determinacy": "estimated", "divergence_point": "review", "exclusion_reason": None,
           "baseline_metrics": {"cycle_time_seconds": 100.0, "auto_ratio": 0.5},
           "counterfactual_metrics": {"estimated": True, "source": "monte_carlo",
                                      "cycle_time_seconds": 99999.0,
                                      "outcome_distribution": {"approved": 1.0}}}
    s = A.aggregate([det, est])
    assert s["determinate"] == 1 and s["estimated"] == 1
    assert s["coverage_ratio"] == 0.5                      # estimated ≠ determinate
    # the wild estimated cycle must NOT leak into the hard counterfactual stats
    assert s["counterfactual"]["cycle_time"]["mean"] == 100.0
    blk = s["estimated_block"]
    assert blk["cases"] == 1 and blk["cycle_time"]["mean"] == 99999.0
    assert blk["sources"] == {"monte_carlo": 1}
    assert "ESTIMATED" in blk["label"]
    assert A.aggregate([det])["estimated_block"] is None
