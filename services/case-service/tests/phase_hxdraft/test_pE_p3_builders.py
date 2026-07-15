"""HxDraft P3 Phase E — builder gate units (escalation / routing / lint).

Pins: closed trigger/action/target sets for escalation trees, the
strategy-from-the-engine-registry gate for routing, the surgical patch, and the
deterministic advisory lint.
"""
from __future__ import annotations

import pytest

from case_service.nlp import escalation_builder, routing_builder, rule_lint

# ── escalation_builder ──────────────────────────────────────────────────────────

def _tree(levels):
    return escalation_builder.normalize_escalation_draft(
        {"name": "Ops escalation", "levels": levels})


def test_escalation_closed_sets():
    bad = _tree([{"level": 1, "name": "a", "trigger": {"type": "cron", "value": 5},
                  "actions": [{"type": "delete_case"},
                              {"type": "notify", "target_type": "everyone"}]}])
    errs = escalation_builder.validate_escalation_draft(bad)
    assert any("closed trigger set" in e for e in errs)
    assert any("closed action set" in e for e in errs)
    assert any("not resolvable" in e for e in errs)


def test_escalation_trigger_value_gates():
    pct = _tree([{"level": 1, "name": "a", "trigger": {"type": "goal_pct", "value": 250},
                  "actions": [{"type": "notify", "target_type": "current_assignee"}]}])
    assert any("(0, 200]" in e for e in escalation_builder.validate_escalation_draft(pct))

    dur = _tree([{"level": 1, "name": "a",
                  "trigger": {"type": "fixed_duration", "value": "whenever"},
                  "actions": [{"type": "notify", "target_type": "current_assignee"}]}])
    assert any("ISO 8601" in e for e in escalation_builder.validate_escalation_draft(dur))

    # human duration from the LLM is converted during normalization
    ok = _tree([{"level": 1, "name": "a",
                 "trigger": {"type": "fixed_duration", "value": "4h"},
                 "actions": [{"type": "notify", "target_type": "current_assignee"}]}])
    assert escalation_builder.validate_escalation_draft(ok) == []
    assert ok["tree_json"]["levels"][0]["trigger"]["value"] == "PT4H"


def test_escalation_level_ordering_and_target_requirements():
    unordered = _tree([
        {"level": 2, "name": "a", "trigger": {"type": "at_breach"},
         "actions": [{"type": "notify", "target_type": "current_assignee"}]},
        {"level": 1, "name": "b", "trigger": {"type": "at_breach"},
         "actions": [{"type": "notify", "target_type": "current_assignee"}]}])
    assert any("strictly increasing" in e
               for e in escalation_builder.validate_escalation_draft(unordered))

    no_target = _tree([{"level": 1, "name": "a", "trigger": {"type": "at_breach"},
                        "actions": [{"type": "reassign", "target_type": "queue"}]}])
    assert any("needs a target_id" in e
               for e in escalation_builder.validate_escalation_draft(no_target))

    bad_priority = _tree([{"level": 1, "name": "a", "trigger": {"type": "at_breach"},
                           "actions": [{"type": "priority", "set": "asap"}]}])
    assert any("priority must set" in e
               for e in escalation_builder.validate_escalation_draft(bad_priority))


def test_escalation_normalize_schema_strict():
    d = _tree([{"level": 1, "name": "a", "trigger": {"type": "at_breach"},
                "run_shell": "rm -rf /",
                "actions": [{"type": "notify", "target_type": "current_assignee",
                             "sudo": True}]}])
    lvl = d["tree_json"]["levels"][0]
    assert set(lvl) == {"level", "name", "trigger", "actions"}
    assert set(lvl["actions"][0]) <= {"type", "target_type", "target_id",
                                      "message", "set"}
    assert "Drafted by HxNexus" in d["description"]


@pytest.mark.asyncio
async def test_escalation_never_guesses(monkeypatch):
    async def down(prompt, system="", **kw):
        return None
    monkeypatch.setattr(escalation_builder, "_ai_generate_json", down)
    with pytest.raises(escalation_builder.EscalationDraftError, match="unavailable"):
        await escalation_builder.generate_escalation_draft("page the manager")


# ── routing_builder ─────────────────────────────────────────────────────────────

_CT_DEF = {"stages": [
    {"id": "review", "name": "Review",
     "steps": [{"id": "approve", "name": "Approve",
                "assignment": {"strategy": "queue_based", "target": "default-queue"},
                "form_fields": []}]},
    {"id": "done", "name": "Done", "steps": []}]}


def test_routing_gate_uses_engine_registry():
    bad = routing_builder.normalize_routing_draft(
        {"stage_id": "review", "step_id": "approve",
         "assignment": {"strategy": "coin_flip", "fallback_strategy": "dice"}})
    errs = routing_builder.validate_routing_draft(bad, _CT_DEF)
    assert any("registry" in e for e in errs)
    assert sum("registry" in e for e in errs) == 2   # primary + fallback


def test_routing_gate_step_must_exist_and_target_required():
    ghost = routing_builder.normalize_routing_draft(
        {"stage_id": "ghost", "step_id": "approve",
         "assignment": {"strategy": "least_loaded"}})
    assert any("does not exist" in e
               for e in routing_builder.validate_routing_draft(ghost, _CT_DEF))

    needs_target = routing_builder.normalize_routing_draft(
        {"stage_id": "review", "step_id": "approve",
         "assignment": {"strategy": "specific_user"}})
    assert any("needs a target" in e
               for e in routing_builder.validate_routing_draft(needs_target, _CT_DEF))


def test_routing_patch_is_surgical():
    draft = routing_builder.normalize_routing_draft(
        {"stage_id": "review", "step_id": "approve",
         "assignment": {"strategy": "least_loaded",
                        "fallback_strategy": "queue_based", "sudo": True}})
    assert routing_builder.validate_routing_draft(draft, _CT_DEF) == []
    patched = routing_builder.patch_step_assignment(_CT_DEF, draft)
    step = patched["stages"][0]["steps"][0]
    assert step["assignment"] == {"strategy": "least_loaded",
                                  "fallback_strategy": "queue_based"}
    assert step["form_fields"] == []                 # rest of the step untouched
    assert patched["stages"][1] == _CT_DEF["stages"][1]   # other stages untouched
    assert _CT_DEF["stages"][0]["steps"][0]["assignment"]["strategy"] \
        == "queue_based"                             # original never mutated


# ── rule_lint ───────────────────────────────────────────────────────────────────

_LINT_CT = {"stages": [{"id": "intake", "steps": [
                {"id": "s1", "form_fields": [{"id": "amount", "field_type": "text"}]}]},
            {"id": "review", "steps": []}],
            "variables": [{"id": "score", "field_type": "number"}]}


def _rule(conditions, actions=None):
    return {"definition_json": {"conditions": conditions,
                                "actions": actions or [{"action_type": "log"}]}}


def test_lint_unknown_variable_and_type_mismatch():
    findings = rule_lint.lint_rule_draft(
        _rule([{"field_path": "claim.amount", "operator": "lt", "value": 5},
               {"field_path": "claim.owner", "operator": "eq", "value": "x"},
               {"field_path": "score", "operator": "gt", "value": 3}]), _LINT_CT)
    assert any("can never fire" in f and "text" in f for f in findings)
    assert any("claim.owner" in f and "not a declared" in f for f in findings)
    assert not any("score" in f.split("'")[1] if "'" in f else False
                   for f in findings if "score" in f)   # number gt is fine


def test_lint_stage_action_targets():
    findings = rule_lint.lint_rule_draft(
        _rule([{"field_path": "score", "operator": "eq", "value": 1}],
              [{"action_type": "advance_stage", "target": "done"},
               {"action_type": "advance_stage", "target": "review"}]), _LINT_CT)
    assert any("'done'" in f and "not a stage" in f for f in findings)
    assert not any("'review'" in f for f in findings)


def test_lint_resolves_case_data_prefix_and_is_quiet_when_clean():
    findings = rule_lint.lint_rule_draft(
        _rule([{"field_path": "case.data.score", "operator": "gte", "value": 1}]),
        _LINT_CT)
    assert findings == []
