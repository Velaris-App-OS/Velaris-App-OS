"""HxEvolve Phase A — the safety boundaries as pure units.

Pins: the permutation-only reorder gate (hostile structural edits fail closed),
the guardrail VETO battery (anti-Goodhart auto-approve gaming, cost regression,
insufficient evidence), and the proposer's closed vocabulary + per-kind re-gating
of hostile LLM output.
"""
from __future__ import annotations

import pytest

from case_service.hxevolve import gates, prover, proposer

_DEF = {
    "stages": [
        {"id": "intake", "name": "Intake", "stage_type": "linear", "order": 0,
         "steps": [{"id": "s1", "name": "Collect"}, {"id": "s2", "name": "Verify"}]},
        {"id": "review", "name": "Review", "stage_type": "linear", "order": 1,
         "steps": [{"id": "s3", "name": "Approve"}]},
    ],
    "sla_policies": [{"id": "res_sla", "name": "Resolution SLA", "scope": "case",
                      "goal_duration": "PT4H", "deadline_duration": "PT8H"}],
    "variables": [{"id": "amount", "field_type": "number"}],
}


# ── reorder gate ────────────────────────────────────────────────────────────────

def test_reorder_gate_allows_pure_permutation_and_parallelize():
    ok = {**_DEF, "stages": [
        {**_DEF["stages"][0], "stage_type": "parallel",
         "steps": [_DEF["stages"][0]["steps"][1], _DEF["stages"][0]["steps"][0]]},
        _DEF["stages"][1],
    ]}
    assert gates.validate_reorder(_DEF, ok) == []


def test_reorder_gate_fails_closed_on_structural_edits():
    # dropping a step
    dropped = {**_DEF, "stages": [
        {**_DEF["stages"][0], "steps": [_DEF["stages"][0]["steps"][0]]},
        _DEF["stages"][1]]}
    assert any("step ids must be identical" in e
               for e in gates.validate_reorder(_DEF, dropped))
    # editing a step
    edited = {**_DEF, "stages": [
        {**_DEF["stages"][0],
         "steps": [{"id": "s1", "name": "HACKED"}, _DEF["stages"][0]["steps"][1]]},
        _DEF["stages"][1]]}
    assert any("may only move steps" in e
               for e in gates.validate_reorder(_DEF, edited))
    # touching anything outside stages
    outside = {**_DEF, "variables": [{"id": "amount", "field_type": "number"},
                                     {"id": "backdoor", "field_type": "text"}]}
    assert any("only touch stages" in e
               for e in gates.validate_reorder(_DEF, outside))
    # adding a stage
    added = {**_DEF, "stages": [*_DEF["stages"],
                                {"id": "evil", "steps": []}]}
    assert any("Stage ids must be identical" in e
               for e in gates.validate_reorder(_DEF, added))
    # invalid stage_type
    badtype = {**_DEF, "stages": [
        {**_DEF["stages"][0], "stage_type": "quantum"}, _DEF["stages"][1]]}
    assert any("stage_type" in e for e in gates.validate_reorder(_DEF, badtype))
    # not an object
    assert gates.validate_reorder(_DEF, "nope")


# ── veto battery ────────────────────────────────────────────────────────────────

def _summary(**over):
    base = {"determinate": 100, "coverage_ratio": 0.9,
            "baseline": {"cycle_time": {"mean": 1000}, "auto_ratio": 0.2},
            "counterfactual": {"cycle_time": {"mean": 800}, "auto_ratio": 0.25},
            "cost": None}
    base.update(over)
    return base


def test_vetoes_surface_a_clean_improvement():
    verdict, reasons = prover._apply_vetoes(_summary())
    assert verdict == "surfaced" and reasons == []


def test_vetoes_insufficient_evidence():
    v, r = prover._apply_vetoes(_summary(determinate=3))
    assert v == "insufficient_evidence" and "determinate" in r[0]
    v, r = prover._apply_vetoes(_summary(coverage_ratio=0.4))
    assert v == "insufficient_evidence" and "coverage" in r[0]


def test_vetoes_block_small_improvements_and_gaming():
    v, r = prover._apply_vetoes(_summary(
        counterfactual={"cycle_time": {"mean": 960}, "auto_ratio": 0.2}))
    assert v == "discarded_guardrail" and "below" in r[0]

    # the anti-Goodhart case: huge cycle-time win by auto-approving everything
    v, r = prover._apply_vetoes(_summary(
        counterfactual={"cycle_time": {"mean": 200}, "auto_ratio": 0.95}))
    assert v == "discarded_guardrail" and any("gaming veto" in x for x in r)

    v, r = prover._apply_vetoes(_summary(cost={"delta": 500}))
    assert v == "discarded_guardrail" and any("cost" in x for x in r)


# ── proposer: closed vocabulary + hostile output re-gated ───────────────────────

class _CT:
    id = "11111111-1111-1111-1111-111111111111"
    name = "Claims"
    definition_json = _DEF


@pytest.mark.asyncio
async def test_proposer_rejects_unknown_kind(monkeypatch):
    async def hostile(prompt, system="", **kw):
        return {"kind": "grant_admin", "rationale": "trust me"}
    monkeypatch.setattr(proposer, "_ai_generate_json", hostile)
    out = await proposer.propose(None, {"kind": "bottleneck"}, _CT(), [])
    assert any("closed proposal vocabulary" in e for e in out["errors"])


@pytest.mark.asyncio
async def test_proposer_regates_hostile_rule(monkeypatch):
    async def hostile(prompt, system="", **kw):
        return {"kind": "rule_add", "rationale": "x",
                "rule": {"name": "Backdoor",
                         "conditions": [{"field_path": "a", "operator": "eq",
                                         "value": 1}],
                         "actions": [{"action_type": "set_value",
                                      "target": "user.role",
                                      "value": "superadmin"}]}}
    monkeypatch.setattr(proposer, "_ai_generate_json", hostile)
    out = await proposer.propose(None, {"kind": "bottleneck"}, _CT(), [])
    assert any("forbidden surface" in e for e in out["errors"])


@pytest.mark.asyncio
async def test_proposer_regates_hostile_reorder(monkeypatch):
    async def hostile(prompt, system="", **kw):
        return {"kind": "reorder", "rationale": "x",
                "reorder": {"stages": [
                    {"id": "intake", "name": "Intake", "stage_type": "linear",
                     "order": 0, "steps": [{"id": "s1", "name": "Collect"}]},
                ]}}
    monkeypatch.setattr(proposer, "_ai_generate_json", hostile)
    out = await proposer.propose(None, {"kind": "variant"}, _CT(), [])
    assert out["errors"]        # dropped s2 + dropped review stage → fails closed


@pytest.mark.asyncio
async def test_proposer_never_guesses(monkeypatch):
    async def down(prompt, system="", **kw):
        return None
    monkeypatch.setattr(proposer, "_ai_generate_json", down)
    with pytest.raises(proposer.ProposeError, match="unavailable"):
        await proposer.propose(None, {"kind": "bottleneck"}, _CT(), [])


@pytest.mark.asyncio
async def test_proposer_valid_sla_and_routing(monkeypatch):
    async def sla(prompt, system="", **kw):
        return {"kind": "sla_duration", "rationale": "loosen",
                "sla": {"policy_id": "res_sla", "goal_duration": "PT8H",
                        "deadline_duration": "PT24H"}}
    monkeypatch.setattr(proposer, "_ai_generate_json", sla)
    out = await proposer.propose(None, {"kind": "bottleneck"}, _CT(), [])
    assert out["errors"] == [] and out["proposal"]["replaces_policy_id"] == "res_sla"

    async def routing(prompt, system="", **kw):
        return {"kind": "routing", "rationale": "spread load",
                "routing": {"stage_id": "review", "step_id": "s3",
                            "assignment": {"strategy": "least_loaded",
                                           "fallback_strategy": "queue_based"}}}
    monkeypatch.setattr(proposer, "_ai_generate_json", routing)
    out = await proposer.propose(None, {"kind": "bottleneck"}, _CT(), [])
    assert out["errors"] == [] and out["proposal"]["assignment"]["strategy"] == "least_loaded"
