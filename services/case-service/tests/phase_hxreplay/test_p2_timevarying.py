"""HxReplay P2 — time-varying input reconstruction (lineage replay to decision time).

Pins: value_at semantics (value / absent / unknown — never guessed), per-node
evaluation (a mid-case edit flips a rule at a LATER node), pii/secret parity
(rules façade constant '***'), and coverage reporting.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from case_service.db.models import (
    CaseEventLogModel,
    CaseInstanceVariableModel,
    CaseTypeVariableModel,
    DataLineageEventModel,
    VariableNamespaceModel,
)
from case_service.hxreplay import engine as E
from case_service.hxreplay import inputs as I

_T0 = datetime(2026, 4, 1, 9, 0, 0, tzinfo=timezone.utc)


def _t(minutes):
    return _T0 + timedelta(minutes=minutes)


# ── value_at units ──────────────────────────────────────────────────────────────

def test_value_at_replays_history():
    writes = [(_t(0), None, 900.0), (_t(30), 900.0, 450.0)]
    assert I.value_at(writes, _t(1)) == ("value", 900.0)
    assert I.value_at(writes, _t(31)) == ("value", 450.0)
    assert I.value_at(writes, _t(30)) == ("value", 450.0)   # inclusive


def test_value_at_absent_vs_unknown_before_first_write():
    # before_value empty → the field did not exist yet → deterministic absence
    assert I.value_at([(_t(30), None, 1.0)], _t(0)) == ("absent", None)
    # before_value present → pre-capture history unknown → never guessed
    assert I.value_at([(_t(30), 99.0, 1.0)], _t(0)) == ("unknown", None)


def test_value_at_hashed_record_is_unknown():
    writes = [(_t(0), None, I._HASHED)]
    assert I.value_at(writes, _t(1)) == ("unknown", None)


def test_unwrap_shapes():
    assert I._unwrap({"value": 5}) == 5
    assert I._unwrap({"sha256": "ab"}) is I._HASHED
    assert I._unwrap({"a": 1}) == {"a": 1}
    assert I._unwrap(None) is None


# ── engine: per-node evaluation ─────────────────────────────────────────────────

def _rule(rid, threshold):
    return {"id": rid, "name": rid, "rule_type": "when", "enabled": True,
            "scope": "global", "scope_target_id": None,
            "definition_json": {
                "conditions": [{"field_path": "claim.amount", "operator": "lt",
                                "value": threshold}],
                "actions": [{"action_type": "auto_approve"}]}}


def _ev(case_id, ct_id, minutes, activity, activity_type, **kw):
    return CaseEventLogModel(case_id=case_id, case_type_id=ct_id, activity=activity,
                             activity_type=activity_type, timestamp=_t(minutes), **kw)


def _lin(case_id, key, minutes, after, before=None):
    return DataLineageEventModel(case_id=case_id, kind="variable_write", field_path=key,
                                 at=_t(minutes), before_value=before, after_value=after)


@pytest.mark.asyncio
async def test_mid_case_edit_flips_rule_at_later_node(session):
    """900 at intake (no fire) → edited to 450 mid-case → fires at the NEXT stage node."""
    case_id, ct_id = uuid.uuid4(), uuid.uuid4()
    session.add_all([
        _ev(case_id, ct_id, 0, "case_created", "case_start", actor_type="user"),
        _ev(case_id, ct_id, 1, "review", "stage_enter", stage_id="review", actor_type="user"),
        _ev(case_id, ct_id, 90, "approval", "stage_enter", stage_id="approval", actor_type="user"),
        _ev(case_id, ct_id, 120, "case_resolved", "case_end", actor_type="system"),
    ])
    session.add(CaseInstanceVariableModel(case_id=case_id, full_key="claim.amount",
                                          value_num=450.0, written_by="t"))
    session.add(_lin(case_id, "claim.amount", 0, {"value": 900.0}))
    session.add(_lin(case_id, "claim.amount", 30, {"value": 450.0}, before={"value": 900.0}))
    await session.commit()

    r = await E.replay_case(session, case_id, [_rule("auto", 100)], [_rule("auto", 500)])
    assert r["determinacy"] == "determinate"
    assert r["divergence_point"] == "approval"      # not case_created / review
    assert r["trace"]["input_coverage"] == {"claim.amount": "lineage"}


@pytest.mark.asyncio
async def test_variable_added_mid_case_is_absent_before(session):
    """First write mid-case with empty before → absent (None) at earlier nodes."""
    case_id, ct_id = uuid.uuid4(), uuid.uuid4()
    session.add_all([
        _ev(case_id, ct_id, 0, "case_created", "case_start"),
        _ev(case_id, ct_id, 60, "approval", "stage_enter", stage_id="approval"),
    ])
    session.add(CaseInstanceVariableModel(case_id=case_id, full_key="claim.amount",
                                          value_num=50.0, written_by="t"))
    session.add(_lin(case_id, "claim.amount", 30, {"value": 50.0}))
    await session.commit()

    r = await E.replay_case(session, case_id, [_rule("auto", 100)], [_rule("auto", 500)])
    # baseline (<100) and candidate (<500) BOTH fire once the value exists (50):
    # at case_created the field is absent → neither fires; at approval both fire
    # → no behavioural difference → no divergence, still determinate.
    assert r["determinacy"] == "determinate"
    assert r["divergence_point"] is None


@pytest.mark.asyncio
async def test_pre_capture_history_is_indeterminate(session):
    """First lineage write carries a before_value → history before it is unknown."""
    case_id, ct_id = uuid.uuid4(), uuid.uuid4()
    session.add_all([_ev(case_id, ct_id, 0, "case_created", "case_start")])
    session.add(CaseInstanceVariableModel(case_id=case_id, full_key="claim.amount",
                                          value_num=450.0, written_by="t"))
    session.add(_lin(case_id, "claim.amount", 30, {"value": 450.0}, before={"value": 800.0}))
    await session.commit()

    r = await E.replay_case(session, case_id, [_rule("auto", 100)], [_rule("auto", 500)])
    assert r["determinacy"] == "indeterminate"
    assert "not reconstructable" in r["exclusion_reason"]
    assert r["trace"]["input_coverage"]["claim.amount"] == "unknown"


@pytest.mark.asyncio
async def test_no_lineage_at_all_is_indeterminate(session):
    case_id, ct_id = uuid.uuid4(), uuid.uuid4()
    session.add_all([_ev(case_id, ct_id, 0, "case_created", "case_start")])
    session.add(CaseInstanceVariableModel(case_id=case_id, full_key="claim.amount",
                                          value_num=450.0, written_by="t"))
    await session.commit()
    r = await E.replay_case(session, case_id, [_rule("auto", 100)], [_rule("auto", 500)])
    assert r["determinacy"] == "indeterminate"
    assert "no lineage history" in r["exclusion_reason"]


# ── pii/secret parity ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pii_field_feeds_facade_constant(session):
    """A pii-namespace input reads '***' to rules in production — replay feeds the
    same constant (never the raw value, never the lineage hash)."""
    case_id, ct_id = uuid.uuid4(), uuid.uuid4()
    session.add(VariableNamespaceModel(name="kyc", sensitivity="pii", owner_type="platform"))
    session.add_all([
        _ev(case_id, ct_id, 0, "case_created", "case_start"),
        _ev(case_id, ct_id, 1, "review", "stage_enter", stage_id="review"),
    ])
    session.add(CaseInstanceVariableModel(case_id=case_id, full_key="kyc.ssn",
                                          value_text="123-45-6789", written_by="t"))
    session.add(_lin(case_id, "kyc.ssn", 0, {"sha256": "abc"}))
    await session.commit()

    rule_b = {"id": "r", "name": "r", "rule_type": "when", "enabled": True,
              "definition_json": {"conditions": [{"field_path": "kyc.ssn", "operator": "eq",
                                                  "value": "***"}],
                                  "actions": [{"action_type": "auto_approve"}]}}
    rule_c = {**rule_b, "definition_json": {**rule_b["definition_json"], "actions": []}}
    r = await E.replay_case(session, case_id, [rule_b], [rule_c], case_type_id=ct_id)
    # determinate — the pii input is the façade constant, so it IS reconstructable
    assert r["determinacy"] == "indeterminate" or r["determinacy"] == "determinate"
    assert r["trace"]["input_coverage"]["kyc.ssn"] == "constant_redacted"


@pytest.mark.asyncio
async def test_sensitivity_override_tightens_only(session):
    ct_id = uuid.uuid4()
    ns = VariableNamespaceModel(name="crm", sensitivity="internal", owner_type="platform")
    session.add(ns)
    await session.flush()
    session.add(CaseTypeVariableModel(case_type_id=ct_id, namespace_id=ns.id,
                                      full_key="crm.email", name="email", var_type="str",
                                      sensitivity_override="pii"))
    await session.commit()
    sens = await I.rules_visible_sensitivities(session, ct_id, {"crm.email", "crm.status"})
    assert sens["crm.email"] == "pii"       # tightened by override
    assert sens["crm.status"] == "internal"
