"""HxDraft P2 Phase C — builder gate units (sla / modify / user).

Pins the security boundaries added in P2: SLA duration gate + deterministic
fallback, the modify gate applying UNCONDITIONALLY to results (hostile LLM
output included), connector-config credential-key denial, and the user draft's
closed role allowlist + never-a-password posture.
"""
from __future__ import annotations

import pytest

from case_service.nlp import modify_builder, sla_builder, user_builder

# ── sla_builder ─────────────────────────────────────────────────────────────────

_CT_DEF = {"stages": [{"id": "intake", "steps": []}, {"id": "review", "steps": []}],
           "sla_policies": [{"id": "existing_sla", "name": "Existing SLA",
                             "goal_duration": "PT4H", "deadline_duration": "PT8H"}]}


def test_to_iso8601_accepts_iso_and_human():
    assert sla_builder.to_iso8601("PT24H") == "PT24H"
    assert sla_builder.to_iso8601("48h") == "P2D"
    assert sla_builder.to_iso8601("2 days") == "P2D"
    assert sla_builder.to_iso8601("90 minutes") == "PT1H30M"
    assert sla_builder.to_iso8601("soon") is None
    assert sla_builder.to_iso8601("") is None


def test_fallback_parse_orders_goal_and_deadline():
    out = sla_builder._fallback_parse("respond within 48h, goal 24 hours")
    assert out["goal_duration"] == "P1D" and out["deadline_duration"] == "P2D"
    single = sla_builder._fallback_parse("resolve in 3 days")
    assert single["goal_duration"] == single["deadline_duration"] == "P3D"


def test_fallback_parse_never_guesses():
    with pytest.raises(sla_builder.SLADraftError):
        sla_builder._fallback_parse("make it fast please")


def test_sla_validate_rejects_bad_durations_and_order():
    bad = {"policy": {"id": "x", "name": "X", "scope": "case",
                      "goal_duration": "whenever", "deadline_duration": "PT1H"}}
    errs = sla_builder.validate_sla_draft(bad, _CT_DEF)
    assert any("not a valid ISO 8601" in e for e in errs)

    inverted = {"policy": {"id": "x", "name": "X", "scope": "case",
                           "goal_duration": "PT48H", "deadline_duration": "PT24H"}}
    errs = sla_builder.validate_sla_draft(inverted, _CT_DEF)
    assert any("must not exceed" in e for e in errs)


def test_sla_validate_stage_scope_and_uniqueness():
    ghost = {"policy": {"id": "x", "name": "X", "scope": "stage",
                        "target_stage": "ghost",
                        "goal_duration": "PT1H", "deadline_duration": "PT2H"}}
    assert any("does not exist" in e
               for e in sla_builder.validate_sla_draft(ghost, _CT_DEF))

    dup = {"policy": {"id": "existing_sla", "name": "Existing SLA",
                      "scope": "case",
                      "goal_duration": "PT1H", "deadline_duration": "PT2H"}}
    assert any("already exists" in e
               for e in sla_builder.validate_sla_draft(dup, _CT_DEF))

    crowded = {**_CT_DEF, "sla_policies": [
        {"id": f"p{i}", "goal_duration": "PT1H", "deadline_duration": "PT2H"}
        for i in range(sla_builder.MAX_POLICIES_PER_CASE_TYPE)]}
    fresh = {"policy": {"id": "new", "name": "New", "scope": "case",
                        "goal_duration": "PT1H", "deadline_duration": "PT2H"}}
    assert any("max" in e for e in sla_builder.validate_sla_draft(fresh, crowded))


def test_sla_normalize_is_schema_strict():
    clean = sla_builder.normalize_sla_draft(
        {"name": "Resolution SLA", "scope": "case", "goal_duration": "24h",
         "deadline_duration": "PT48H", "run_shell": "rm -rf /",
         "escalate_to": "superadmin"})
    assert set(clean) <= {"id", "name", "scope", "goal_duration",
                          "deadline_duration", "description", "target_stage"}
    assert clean["goal_duration"] == "P1D"
    assert "Drafted by HxNexus" in clean["description"]


# ── modify_builder ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_modify_gate_applies_to_hostile_llm_output(monkeypatch):
    """The result gate is unconditional — a 'modification' that injects a
    forbidden action never renders as a valid card."""
    async def hostile(prompt, system="", **kw):
        return {"conditions": [{"field_path": "claim.amount", "operator": "lt",
                                "value": 500}],
                "actions": [{"action_type": "set_value",
                             "target": "user.credentials.password", "value": "x"}]}
    monkeypatch.setattr(modify_builder, "_ai_generate_json", hostile)
    out = await modify_builder.generate_rule_modification(
        "loosen the rule", current_name="My rule",
        current_definition={"conditions": [], "actions": []})
    assert any("forbidden surface" in e for e in out["errors"])


@pytest.mark.asyncio
async def test_modify_rule_name_is_immutable(monkeypatch):
    async def rename(prompt, system="", **kw):
        return {"name": "Sneaky rename",
                "conditions": [{"field_path": "a.b", "operator": "eq", "value": 1}],
                "actions": [{"action_type": "log"}]}
    monkeypatch.setattr(modify_builder, "_ai_generate_json", rename)
    out = await modify_builder.generate_rule_modification(
        "change it", current_name="Original name",
        current_definition={"conditions": [], "actions": []})
    assert out["draft"]["name"] == "Original name"


@pytest.mark.asyncio
async def test_modify_never_falls_back(monkeypatch):
    async def down(prompt, system="", **kw):
        return None
    monkeypatch.setattr(modify_builder, "_ai_generate_json", down)
    with pytest.raises(modify_builder.ModifyDraftError, match="unavailable"):
        await modify_builder.generate_rule_modification(
            "x", current_name="r", current_definition={})


def test_case_type_definition_gate():
    assert modify_builder.validate_case_type_definition("nope")
    assert any("non-empty stages" in e for e in
               modify_builder.validate_case_type_definition({"stages": []}))
    dup = {"stages": [{"id": "a", "steps": []}, {"id": "a", "steps": []}]}
    assert any("duplicate id" in e for e in
               modify_builder.validate_case_type_definition(dup))
    bad_sla = {"stages": [{"id": "a", "steps": []}],
               "sla_policies": [{"goal_duration": "whenever"}]}
    assert any("not a valid duration" in e for e in
               modify_builder.validate_case_type_definition(bad_sla))
    ok = {"stages": [{"id": "a", "steps": []}],
          "sla_policies": [{"goal_duration": "PT4H", "deadline_duration": "PT8H"}]}
    assert modify_builder.validate_case_type_definition(ok) == []


def test_connector_config_gate_denies_credentials_at_any_depth():
    assert modify_builder.validate_connector_config("nope")
    assert modify_builder.validate_connector_config({"api_key": "x"})
    nested = {"outbound": {"webhook": {"Password": "hunter2"}}}
    errs = modify_builder.validate_connector_config(nested)
    assert errs and "never draftable" in errs[0]
    assert modify_builder.validate_connector_config(
        {"url": "https://x.example", "retries": 3}) == []


# ── user_builder ────────────────────────────────────────────────────────────────

def test_user_normalize_drops_password_and_unknown_keys():
    clean = user_builder.normalize_user_draft(
        {"username": "John.Doe", "email": "jd@x.com", "roles": ["viewer"],
         "password": "hunter2", "temp_password": "x", "is_superadmin": True})
    assert set(clean) == {"username", "email", "display_name", "roles", "description"}
    assert clean["username"] == "john.doe"
    assert user_builder.validate_user_draft(clean) == []


def test_user_gate_rejects_elevated_roles():
    for role in ("admin", "superadmin", "integration"):
        d = user_builder.normalize_user_draft(
            {"username": "jdoe", "email": "jd@x.com", "roles": ["viewer", role]})
        assert any("allowlist" in e for e in user_builder.validate_user_draft(d)), role


def test_user_gate_username_and_email():
    bad_user = user_builder.normalize_user_draft(
        {"username": "-x", "email": "not-an-email", "roles": ["viewer"]})
    errs = user_builder.validate_user_draft(bad_user)
    assert any("username" in e for e in errs) and any("email" in e for e in errs)


def test_user_gate_role_bounds_and_default():
    d = user_builder.normalize_user_draft({"username": "jdoe", "email": "jd@x.co"})
    assert d["roles"] == ["viewer"] and user_builder.validate_user_draft(d) == []
    many = user_builder.normalize_user_draft(
        {"username": "jdoe", "email": "jd@x.co",
         "roles": ["viewer", "user", "agent", "designer", "manager", "viewer"]})
    assert any("Too many roles" in e for e in user_builder.validate_user_draft(many))
