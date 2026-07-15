"""HxReplay Phase A — baseline trace loading/metrics + P1 input reconstruction.

Pins the two Phase-A primitives: (1) the recorded trace and its metrics are read
faithfully from case_event_log; (2) input reconstruction only trusts variables
PROVABLY immutable-at-intake (exactly one lineage write) — anything else is
flagged unreconstructable, never guessed (design §5 no-fabricated-history).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from case_service.db.models import (
    CaseEventLogModel,
    CaseInstanceVariableModel,
    DataLineageEventModel,
    ReplayResultModel,
    ReplayRunModel,
)
from case_service.hxreplay import inputs as I
from case_service.hxreplay import trace as T

pytestmark = pytest.mark.asyncio

_T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _ev(case_id, ct_id, minutes, activity, activity_type, **kw):
    return CaseEventLogModel(
        case_id=case_id, case_type_id=ct_id, activity=activity,
        activity_type=activity_type, timestamp=_T0 + timedelta(minutes=minutes), **kw,
    )


async def _seed_trace(session):
    case_id, ct_id = uuid.uuid4(), uuid.uuid4()
    session.add_all([
        _ev(case_id, ct_id, 0, "case_created", "case_start", actor_type="user", outcome="success"),
        _ev(case_id, ct_id, 5, "review", "stage_enter", stage_id="review",
            actor_type="system", duration_seconds=300),
        _ev(case_id, ct_id, 65, "approve_step", "step_complete", stage_id="review",
            step_id="approve_step", actor_type="user", duration_seconds=3600, outcome="approved"),
        _ev(case_id, ct_id, 70, "case_resolved", "case_end", actor_type="system", outcome="success"),
    ])
    await session.commit()
    return case_id, ct_id


async def test_baseline_trace_ordered_and_metrics(session):
    case_id, _ = await _seed_trace(session)
    tr = await T.load_baseline_trace(session, case_id)
    assert [e["activity"] for e in tr] == ["case_created", "review", "approve_step", "case_resolved"]

    m = T.baseline_metrics(tr)
    assert m["event_count"] == 4
    assert m["cycle_time_seconds"] == 70 * 60
    assert m["auto_count"] == 2 and m["manual_count"] == 2 and m["auto_ratio"] == 0.5
    assert m["stage_durations"] == {"review": 3900}
    assert m["outcomes"] == {"success": 2, "approved": 1}
    assert m["resolved"] is True


async def test_baseline_metrics_empty_and_unresolved(session):
    assert T.baseline_metrics([])["event_count"] == 0
    case_id, ct_id = uuid.uuid4(), uuid.uuid4()
    session.add(_ev(case_id, ct_id, 0, "case_created", "case_start"))
    await session.commit()
    m = T.baseline_metrics(await T.load_baseline_trace(session, case_id))
    assert m["resolved"] is False and m["cycle_time_seconds"] == 0


def _var(case_id, key, **vals):
    return CaseInstanceVariableModel(case_id=case_id, full_key=key, written_by="t", **vals)


def _lin(case_id, key, n):
    return [DataLineageEventModel(case_id=case_id, kind="variable_write", field_path=key,
                                  after_value={"v": i}) for i in range(n)]


async def test_reconstruct_inputs_immutability_proof(session):
    case_id = uuid.uuid4()
    session.add_all([
        _var(case_id, "claim.amount", value_num=450.0),      # 1 write → immutable ✓
        _var(case_id, "claim.status_note", value_text="x"),  # 3 writes → time-varying ✗
        _var(case_id, "claim.untracked", value_text="y"),    # 0 lineage rows → unprovable ✗
        _var(case_id, "claim.flag", value_bool=False),       # 1 write, falsy value ✓
    ])
    session.add_all(_lin(case_id, "claim.amount", 1))
    session.add_all(_lin(case_id, "claim.status_note", 3))
    session.add_all(_lin(case_id, "claim.flag", 1))
    # another case's lineage must not bleed in
    session.add_all(_lin(uuid.uuid4(), "claim.untracked", 1))
    await session.commit()

    r = await I.reconstruct_inputs(session, case_id)
    assert r["variables"] == {"claim.amount": 450.0, "claim.flag": False}
    assert r["unreconstructable"] == ["claim.status_note", "claim.untracked"]


async def test_variable_value_typed_precedence(session):
    case_id = uuid.uuid4()
    v = _var(case_id, "k", value_text="fallback", value_json={"a": 1})
    assert I.variable_value(v) == {"a": 1}
    assert I.variable_value(_var(case_id, "k2", value_bool=False, value_text="no")) is False
    assert I.variable_value(_var(case_id, "k3", value_num=0.0, value_text="no")) == 0.0
    assert I.variable_value(_var(case_id, "k4", value_text="t")) == "t"


async def test_replay_models_roundtrip(session):
    run = ReplayRunModel(tenant_id="default", kind="single", case_id=uuid.uuid4(),
                         created_by="tester")
    session.add(run)
    await session.commit()
    res = ReplayResultModel(run_id=run.id, case_id=run.case_id, tenant_id="default",
                            baseline_metrics={"event_count": 4},
                            trace={"nodes": []})
    session.add(res)
    await session.commit()
    assert res.determinacy == "determinate" and run.status == "pending"
    assert run.config_epoch == "current+branch"
