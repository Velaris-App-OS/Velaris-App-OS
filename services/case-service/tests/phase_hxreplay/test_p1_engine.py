"""HxReplay Phase B — engine: rule-set diff, divergence classes, honest exclusion.

Pins the determinism split (§4): gained terminal → truncation; gained skip →
stage elision (wall-clock reported, timestamps never shifted); lost flow action
→ indeterminate (never guessed); unsupported rule types / unprovable inputs →
indeterminate. All engine paths are read-only.
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
from case_service.hxreplay import engine as E

_T0 = datetime(2026, 2, 1, 9, 0, 0, tzinfo=timezone.utc)


def _rule(rid, conds, actions, rule_type="when", enabled=True):
    return {"id": rid, "name": rid, "rule_type": rule_type, "enabled": enabled,
            "scope": "global", "scope_target_id": None,
            "definition_json": {"conditions": conds, "actions": actions}}


_AUTO_APPROVE = {"action_type": "auto_approve", "target": None, "value": None}
_SKIP_REVIEW = {"action_type": "skip_stage", "target": "review", "value": None}
_NOTIFY = {"action_type": "send_notification", "target": "ops", "value": "hi"}


# ── pure units ──────────────────────────────────────────────────────────────────

def test_diff_rule_sets():
    a = _rule("r1", [{"field_path": "x", "operator": "lt", "value": 5}], [_AUTO_APPROVE])
    a2 = _rule("r1", [{"field_path": "x", "operator": "lt", "value": 500}], [_AUTO_APPROVE])
    b = _rule("r2", [], [_NOTIFY])
    assert E.diff_rule_sets([a, b], [a, b]) == []
    d = E.diff_rule_sets([a, b], [a2, b])
    assert [c["change"] for c in d] == ["modified"]
    assert [c["change"] for c in E.diff_rule_sets([a], [a, b])] == ["added"]
    assert [c["change"] for c in E.diff_rule_sets([a, b], [b])] == ["removed"]
    # enabled flip counts as a change
    a_off = {**a, "enabled": False}
    assert [c["change"] for c in E.diff_rule_sets([a], [a_off])] == ["modified"]


def test_action_class():
    assert E.action_class(_AUTO_APPROVE) == "terminal"
    assert E.action_class(_SKIP_REVIEW) == "skip"
    assert E.action_class(_NOTIFY) == "neutral"
    assert E.action_class({"action_type": "set_value", "target": "case.status",
                           "value": "resolved"}) == "terminal"
    assert E.action_class({"action_type": "set_value", "target": "claim.note",
                           "value": "resolved"}) == "neutral"


def test_unresolvable_inputs():
    changed = [{"change": "modified",
                "baseline": _rule("r", [{"field_path": "claim.amount", "operator": "lt", "value": 5}], []),
                "candidate": _rule("r", [{"field_path": "claim.amount", "operator": "lt", "value": 9},
                                         {"field_path": "case.status", "operator": "eq", "value": "open"}], [])}]
    bad = E.unresolvable_inputs(changed, {"claim.amount": 3.0}, [])
    assert bad == {"case.status"}                       # runtime attr → P2
    bad = E.unresolvable_inputs(changed, {}, ["claim.amount"])
    assert "claim.amount" in bad                        # edited var
    changed[0]["candidate"]["definition_json"]["conditions"] = [
        {"field_path": "claim.missing", "operator": "is_empty", "value": None}]
    changed[0]["baseline"]["definition_json"]["conditions"] = []
    assert E.unresolvable_inputs(changed, {"claim.amount": 3.0}, []) == set()  # absent = deterministic None


# ── DB-backed replay ────────────────────────────────────────────────────────────

def _ev(case_id, ct_id, minutes, activity, activity_type, **kw):
    return CaseEventLogModel(case_id=case_id, case_type_id=ct_id, activity=activity,
                             activity_type=activity_type,
                             timestamp=_T0 + timedelta(minutes=minutes), **kw)


async def _seed_case(session, amount=450.0):
    """intake → review (60 min of manual work) → resolved."""
    case_id, ct_id = uuid.uuid4(), uuid.uuid4()
    session.add_all([
        _ev(case_id, ct_id, 0, "case_created", "case_start", actor_type="user"),
        _ev(case_id, ct_id, 1, "review", "stage_enter", stage_id="review", actor_type="system"),
        _ev(case_id, ct_id, 61, "approve_step", "step_complete", stage_id="review",
            step_id="approve_step", actor_type="user", duration_seconds=3600, outcome="approved"),
        _ev(case_id, ct_id, 62, "case_resolved", "case_end", actor_type="system", outcome="success"),
    ])
    session.add(CaseInstanceVariableModel(case_id=case_id, full_key="claim.amount",
                                          value_num=amount, written_by="t"))
    session.add(DataLineageEventModel(case_id=case_id, kind="variable_write",
                                      field_path="claim.amount", after_value={"value": amount},
                                      at=_T0))
    await session.commit()
    return case_id


_BASE = [_rule("auto", [{"field_path": "claim.amount", "operator": "lt", "value": 100}],
               [_AUTO_APPROVE])]


def _cand(threshold):
    return [_rule("auto", [{"field_path": "claim.amount", "operator": "lt", "value": threshold}],
                  [_AUTO_APPROVE])]


@pytest.mark.asyncio
async def test_gained_terminal_truncates(session):
    case_id = await _seed_case(session, amount=450.0)
    # raise the auto-approve threshold 100 → 500: this 450 case now auto-approves at intake
    r = await E.replay_case(session, case_id, _BASE, _cand(500))
    assert r["determinacy"] == "determinate"
    assert r["divergence_point"] == "case_created"
    assert r["counterfactual_metrics"]["cycle_time_seconds"] == 0
    assert r["counterfactual_metrics"]["manual_count"] < r["baseline_metrics"]["manual_count"]
    classes = [n["_class"] for n in r["trace"]["nodes"]]
    assert "synthetic" in classes and "elided" in classes


@pytest.mark.asyncio
async def test_no_divergence_when_rule_never_bites(session):
    case_id = await _seed_case(session, amount=450.0)
    # 100 → 200 still doesn't catch a 450 case
    r = await E.replay_case(session, case_id, _BASE, _cand(200))
    assert r["determinacy"] == "determinate"
    assert r["divergence_point"] is None
    assert r["counterfactual_metrics"] == r["baseline_metrics"]


@pytest.mark.asyncio
async def test_identical_config_short_circuits(session):
    case_id = await _seed_case(session)
    r = await E.replay_case(session, case_id, _BASE, [dict(x) for x in _BASE])
    assert r["determinacy"] == "determinate" and r["divergence_point"] is None


@pytest.mark.asyncio
async def test_lost_flow_action_is_indeterminate(session):
    case_id = await _seed_case(session, amount=50.0)
    # baseline auto-approved (<100); candidate lowers to <10 → the never-recorded
    # manual path becomes reachable → excluded, never guessed
    r = await E.replay_case(session, case_id, _BASE, _cand(10))
    assert r["determinacy"] == "indeterminate"
    assert "previously-skipped path" in r["exclusion_reason"]


@pytest.mark.asyncio
async def test_unsupported_rule_type_is_indeterminate(session):
    case_id = await _seed_case(session)
    cand = [_rule("auto", [], [], rule_type="decision_table")]
    r = await E.replay_case(session, case_id, _BASE, cand)
    assert r["determinacy"] == "indeterminate"
    assert "not replayable in P1" in r["exclusion_reason"]


@pytest.mark.asyncio
async def test_edited_input_uses_value_at_node(session):
    # P2: a later edit no longer excludes the case — each node sees the value
    # the decision actually saw. The edit (450→9) happens AFTER the last walk
    # node, so divergence still fires on the intake value.
    case_id = await _seed_case(session, amount=450.0)
    session.add(DataLineageEventModel(case_id=case_id, kind="variable_write",
                                      field_path="claim.amount", after_value={"value": 9.0},
                                      at=_T0 + timedelta(hours=2)))
    await session.commit()
    r = await E.replay_case(session, case_id, _BASE, _cand(500))
    assert r["determinacy"] == "determinate"
    assert r["divergence_point"] == "case_created"
    assert r["trace"]["input_coverage"] == {"claim.amount": "lineage"}


@pytest.mark.asyncio
async def test_gained_skip_elides_stage_and_reports_wall_clock(session):
    case_id = await _seed_case(session, amount=450.0)
    base = [_rule("skip", [{"field_path": "claim.amount", "operator": "lt", "value": 100}],
                  [_SKIP_REVIEW])]
    cand = [_rule("skip", [{"field_path": "claim.amount", "operator": "lt", "value": 500}],
                  [_SKIP_REVIEW])]
    r = await E.replay_case(session, case_id, base, cand)
    assert r["determinacy"] == "determinate"
    elided = [n for n in r["trace"]["nodes"] if n.get("_elided")]
    assert {n["activity"] for n in elided} == {"review", "approve_step"}
    # review stage_enter (t+1) → case_end (t+62) exclusive: 60 min of elided wall clock
    assert r["counterfactual_metrics"]["elided_wall_seconds"] == 3600
    # timestamps are held fixed — cycle time unchanged, saving shown as elided time
    assert r["counterfactual_metrics"]["cycle_time_seconds"] == r["baseline_metrics"]["cycle_time_seconds"]


@pytest.mark.asyncio
async def test_oversize_trace_excluded_not_truncated(session, monkeypatch):
    monkeypatch.setattr(E, "_MAX_TRACE_EVENTS", 3)
    case_id = await _seed_case(session)   # 4 events > 3
    r = await E.replay_case(session, case_id, _BASE, _cand(500))
    assert r["determinacy"] == "indeterminate"
    assert "exceeds the replay limit" in r["exclusion_reason"]
