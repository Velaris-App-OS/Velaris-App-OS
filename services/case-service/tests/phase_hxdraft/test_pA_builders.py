"""HxDraft Phase A — rule/form builders: the validation gate IS the security boundary.

Pins: closed action set, forbidden set_value targets (fail closed), operator
whitelist, expression CONFORMING gate, caps, schema-strict normalization (unknown
keys never survive), rules never fall back to guessing, forms degrade to a
labelled template.
"""
from __future__ import annotations

import pytest

from case_service.nlp import form_builder as F
from case_service.nlp import rule_builder as R


def _rule_draft(conditions=None, actions=None, rule_type="when", name="Auto approve"):
    return {
        "name": name, "rule_type": rule_type,
        "definition_json": {
            "conditions": conditions if conditions is not None else [
                {"field_path": "claim.amount", "operator": "lt", "value": 500}],
            "actions": actions if actions is not None else [
                {"action_type": "auto_approve", "target": None, "value": None}],
        },
    }


# ── rule validation gate ────────────────────────────────────────────────────────

def test_valid_when_rule_passes():
    assert R.validate_rule_draft(_rule_draft()) == []


def test_closed_action_set_is_closed():
    for bad in ("delete_case", "grant_role", "execute", "http_call", "create_user", ""):
        errs = R.validate_rule_draft(_rule_draft(actions=[{"action_type": bad}]))
        assert any("closed action set" in e for e in errs), bad


def test_forbidden_set_value_targets_fail_closed():
    for target in ("case.security.level", "user.role", "authz.grants", "api_token",
                   "kyc.password_hash", "namespace.grant", "tenant_id",
                   "case.status"):    # bare case.* system field also rejected
        errs = R.validate_rule_draft(_rule_draft(
            actions=[{"action_type": "set_value", "target": target, "value": "x"}]))
        assert errs, target
    # a plain case-variable path is fine, in both accepted spellings
    for target in ("claim.reviewed", "case.data.claim.reviewed"):
        assert R.validate_rule_draft(_rule_draft(
            actions=[{"action_type": "set_value", "target": target, "value": True}])) == []


def test_operator_whitelist_and_condition_shape():
    errs = R.validate_rule_draft(_rule_draft(
        conditions=[{"field_path": "x", "operator": "regex_bomb", "value": "y"}]))
    assert any("unknown operator" in e for e in errs)
    errs = R.validate_rule_draft(_rule_draft(conditions=[{"operator": "eq"}]))
    assert any("field_path" in e for e in errs)
    assert any("at least one condition" in e
               for e in R.validate_rule_draft(_rule_draft(conditions=[])))
    assert any("at least one action" in e
               for e in R.validate_rule_draft(_rule_draft(actions=[])))


def test_caps_and_rule_type():
    many_c = [{"field_path": f"a.b{i}", "operator": "eq", "value": i} for i in range(21)]
    assert any("Too many conditions" in e
               for e in R.validate_rule_draft(_rule_draft(conditions=many_c)))
    many_a = [{"action_type": "log", "value": i} for i in range(11)]
    assert any("Too many actions" in e
               for e in R.validate_rule_draft(_rule_draft(actions=many_a)))
    assert any("Only WHEN rules" in e
               for e in R.validate_rule_draft(_rule_draft(rule_type="expression")))


def test_expression_must_be_conforming():
    d = _rule_draft()
    d["definition_json"]["expression"] = "__import__('os').system('rm -rf /')"
    assert any("HxSandbox" in e for e in R.validate_rule_draft(d))
    d["definition_json"]["expression"] = "claim_amount < 500"
    assert R.validate_rule_draft(d) == []


# ── rule normalization ──────────────────────────────────────────────────────────

def test_normalize_is_schema_strict_and_carries_provenance():
    raw = {"name": "Big Rule", "description": "does things",
           "conditions": [{"field_path": "claim.amount", "operator": "lt", "value": 500,
                           "evil_key": "ignored"}],
           "actions": [{"action_type": "auto_approve", "shell": "rm -rf"}],
           "sql": "DROP TABLE cases"}
    d = R.normalize_rule_draft(raw, scope_target_id="ct-1", prompt="approve small claims")
    assert d["rule_type"] == "when" and d["scope"] == "case_type"
    assert d["scope_target_id"] == "ct-1" and d["enabled"] is True
    assert "sql" not in d and "sql" not in d["definition_json"]
    assert "evil_key" not in d["definition_json"]["conditions"][0]
    assert "shell" not in d["definition_json"]["actions"][0]
    assert 'Drafted by HxNexus — "approve small claims"' in d["description"]
    assert R.validate_rule_draft(d) == []


def test_normalize_global_scope_and_value_field_path():
    raw = {"name": "n", "conditions": [{"field_path": "a.x", "operator": "eq",
                                        "value_field_path": "a.y"}],
           "actions": [{"action_type": "log", "value": "hi"}]}
    d = R.normalize_rule_draft(raw)
    assert d["scope"] == "global" and d["scope_target_id"] is None
    assert d["definition_json"]["conditions"][0]["value_field_path"] == "a.y"
    assert "value" not in d["definition_json"]["conditions"][0]


# ── rule generation (fake backend; no network) ──────────────────────────────────

@pytest.mark.asyncio
async def test_generate_rule_draft_happy_path(monkeypatch):
    async def fake(prompt, system="", **kw):
        return {"name": "Small claims", "description": "auto",
                "conditions": [{"field_path": "claim.amount", "operator": "lt", "value": 500}],
                "actions": [{"action_type": "auto_approve"}]}
    monkeypatch.setattr(R, "_ai_generate_json", fake)
    out = await R.generate_rule_draft("auto-approve claims under 500")
    assert out["errors"] == []
    assert out["draft"]["definition_json"]["actions"][0]["action_type"] == "auto_approve"


@pytest.mark.asyncio
async def test_generate_rule_draft_hostile_llm_output_is_gated(monkeypatch):
    async def fake(prompt, system="", **kw):
        return {"name": "Sneaky",
                "conditions": [{"field_path": "x", "operator": "eq", "value": 1}],
                "actions": [{"action_type": "set_value", "target": "user.role",
                             "value": "superadmin"}]}
    monkeypatch.setattr(R, "_ai_generate_json", fake)
    out = await R.generate_rule_draft("innocent description")
    assert any("forbidden surface" in e for e in out["errors"])


@pytest.mark.asyncio
async def test_generate_rule_draft_never_guesses(monkeypatch):
    async def down(prompt, system="", **kw):
        return None
    monkeypatch.setattr(R, "_ai_generate_json", down)
    with pytest.raises(R.RuleDraftError, match="AI backend unavailable"):
        await R.generate_rule_draft("auto-approve everything")
    with pytest.raises(R.RuleDraftError, match="Describe"):
        await R.generate_rule_draft("   ")
    with pytest.raises(R.RuleDraftError, match="too long"):
        await R.generate_rule_draft("x" * 4001)


# ── form builder ────────────────────────────────────────────────────────────────

def test_form_validation_gate():
    ok = {"name": "Intake", "definition_json": {"fields": [
        {"id": "summary", "label": "Summary", "field_type": "text", "required": True}]}}
    assert F.validate_form_draft(ok) == []
    errs = F.validate_form_draft({"name": "", "definition_json": {"fields": []}})
    assert any("needs a name" in e for e in errs) or any("at least one field" in e for e in errs)
    bad = {"name": "F", "definition_json": {"fields": [
        {"id": "a", "label": "A", "field_type": "hologram"},
        {"id": "a", "label": "Dup", "field_type": "text"},
        {"id": "pick", "label": "Pick", "field_type": "select"}]}}
    errs = F.validate_form_draft(bad)
    assert any("unknown field_type" in e for e in errs)
    assert any("duplicate id" in e for e in errs)
    assert any("needs options" in e for e in errs)
    too_many = {"name": "F", "definition_json": {"fields": [
        {"id": f"f{i}", "label": "x", "field_type": "text"} for i in range(41)]}}
    assert any("Too many fields" in e for e in F.validate_form_draft(too_many))


def test_form_normalize_schema_strict():
    raw = {"name": "Claim Intake", "description": "intake",
           "fields": [{"id": "Amount!", "label": "Amount", "field_type": "currency",
                       "required": True, "onclick": "alert(1)"},
                      {"id": "kind", "label": "Kind", "field_type": "select",
                       "options": ["a", "b"], "hidden_eval": "x"}]}
    d = F.normalize_form_draft(raw, prompt="claim intake form")
    fields = d["definition_json"]["fields"]
    assert fields[0]["id"] == "amount" and "onclick" not in fields[0]
    assert fields[1]["options"] == ["a", "b"] and "hidden_eval" not in fields[1]
    assert "Drafted by HxNexus" in d["definition_json"]["description"]
    assert F.validate_form_draft(d) == []


@pytest.mark.asyncio
async def test_form_generation_falls_back_to_labelled_template(monkeypatch):
    async def down(prompt, system="", **kw):
        return None
    monkeypatch.setattr(F, "_ai_generate_json", down)
    out = await F.generate_form_draft("visitor sign-in form")
    assert out["source"] == "template" and out["errors"] == []
    assert "AI unavailable" in out["draft"]["definition_json"]["description"]
