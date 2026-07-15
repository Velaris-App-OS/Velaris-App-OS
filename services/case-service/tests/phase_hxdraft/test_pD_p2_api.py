"""HxDraft P2 Phase D — stage-in-HxBranch, SLA drafts, modify-existing, user drafts.

Pins: the disabled-create→branch→merge-enables flow (incl. SOD + the regen parity
hook path), base-checksum 409s on every modify apply, credentials never entering a
connector-modify prompt, and the user apply's admin gate + server-side password.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from case_service.db.models import (
    ArtifactBranchModel,
    CaseTypeModel,
    ConnectorRegistryModel,
    FormDefinitionModel,
    HelixUserModel,
    RuleDefinitionModel,
)
from case_service.nlp import modify_builder, rule_builder, sla_builder, user_builder

_RULE_JSON = {
    "name": "Small claims auto-approve", "description": "under 500",
    "conditions": [{"field_path": "claim.amount", "operator": "lt", "value": 500}],
    "actions": [{"action_type": "auto_approve"}],
}


def _viewer_headers() -> dict:
    from case_service.auth.jwt_handler import create_dev_token
    from case_service.config import get_settings
    s = get_settings()
    token = create_dev_token(
        user_id=str(uuid.uuid4()), username="test-viewer", roles=["viewer"],
        secret=s.auth_secret, private_key=s.auth_rsa_private_key or "")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def fake_rule_llm(monkeypatch):
    async def fake(prompt, system="", **kw):
        return dict(_RULE_JSON)
    monkeypatch.setattr(rule_builder, "_ai_generate_json", fake)


async def _make_case_type(session, **overrides) -> CaseTypeModel:
    ct = CaseTypeModel(
        name=overrides.get("name", f"CT {uuid.uuid4().hex[:6]}"),
        version="1.0.0",
        definition_json=overrides.get("definition_json", {
            "stages": [{"id": "intake", "steps": []}, {"id": "review", "steps": []}],
            "sla_policies": []}),
        default_priority="medium",
    )
    session.add(ct)
    await session.commit()
    await session.refresh(ct)
    return ct


# ── stage in HxBranch ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stage_creates_disabled_rule_and_enabling_branch(client, fake_rule_llm,
                                                               session):
    r = await client.post("/api/v1/hxnexus/draft",
                          json={"kind": "rule", "description": "auto approve small"})
    draft = r.json()["draft"]

    s = await client.post("/api/v1/hxnexus/draft/stage",
                          json={"kind": "rule", "draft": draft})
    assert s.status_code == 201, s.text
    body = s.json()

    rule = await session.get(RuleDefinitionModel, uuid.UUID(body["rule_id"]))
    assert rule.enabled is False           # nothing live until the merge

    branch = await session.get(ArtifactBranchModel, uuid.UUID(body["branch_id"]))
    assert branch.artifact_type == "rule"
    assert branch.artifact_id == body["rule_id"]
    assert branch.base_snapshot["enabled"] is False
    assert branch.content_snapshot["enabled"] is True   # the diff IS the enabling
    assert branch.status == "open"


@pytest.mark.asyncio
async def test_stage_merge_enables_rule_with_sod(client, fake_rule_llm, session):
    r = await client.post("/api/v1/hxnexus/draft",
                          json={"kind": "rule", "description": "auto approve small"})
    s = await client.post("/api/v1/hxnexus/draft/stage",
                          json={"kind": "rule", "draft": r.json()["draft"]})
    body = s.json()

    # SOD: the owner cannot review their own branch — a different reviewer id
    reviewer = str(uuid.uuid4())
    sub = await client.post(f"/api/v1/branches/{body['branch_id']}/submit",
                            json={"assigned_reviewer_id": reviewer})
    assert sub.status_code == 200, sub.text

    # approval auto-merges (admin may review); merge sets enabled=True on main
    rev = await client.post(f"/api/v1/branches/{body['branch_id']}/reviews",
                            json={"decision": "approved", "comments": "evidence ok"})
    assert rev.status_code == 201, rev.text
    assert rev.json().get("auto_merged") is True

    session.expire_all()
    rule = await session.get(RuleDefinitionModel, uuid.UUID(body["rule_id"]))
    assert rule.enabled is True


@pytest.mark.asyncio
async def test_stage_open_to_any_authenticated_user_but_gated_on_tamper(
        client, fake_rule_llm, session):
    r = await client.post("/api/v1/hxnexus/draft",
                          json={"kind": "rule", "description": "auto approve small"})
    draft = r.json()["draft"]

    # signed-off P2 decision: a viewer MAY stage (propose→review), …
    s = await client.post("/api/v1/hxnexus/draft/stage",
                          json={"kind": "rule", "draft": dict(draft)},
                          headers=_viewer_headers())
    assert s.status_code == 201, s.text
    # … but a viewer may NOT Apply directly (admin+designer gate unchanged)
    a = await client.post("/api/v1/hxnexus/draft/apply",
                          json={"kind": "rule", "draft": dict(draft)},
                          headers=_viewer_headers())
    assert a.status_code == 403

    # staging re-validates: a tampered draft never becomes a disabled rule either
    draft["definition_json"]["actions"] = [
        {"action_type": "set_value", "target": "case.security_level", "value": 0}]
    t = await client.post("/api/v1/hxnexus/draft/stage",
                          json={"kind": "rule", "draft": draft})
    assert t.status_code == 400

    n = await client.post("/api/v1/hxnexus/draft/stage",
                          json={"kind": "form", "draft": {}})
    assert n.status_code == 400            # rules only in P2


@pytest.mark.asyncio
async def test_duplicate_rule_name_is_409_not_500(client, fake_rule_llm):
    """(name, version) is DB-unique — a duplicate draft must fail cleanly on both
    the Apply and Stage paths, never as an IntegrityError 500."""
    r = await client.post("/api/v1/hxnexus/draft",
                          json={"kind": "rule", "description": "auto approve small"})
    draft = r.json()["draft"]

    first = await client.post("/api/v1/hxnexus/draft/apply",
                              json={"kind": "rule", "draft": dict(draft)})
    assert first.status_code == 201, first.text

    again = await client.post("/api/v1/hxnexus/draft/apply",
                              json={"kind": "rule", "draft": dict(draft)})
    assert again.status_code == 409, again.text

    staged = await client.post("/api/v1/hxnexus/draft/stage",
                               json={"kind": "rule", "draft": dict(draft)})
    assert staged.status_code == 409, staged.text


# ── sla drafts ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sla_draft_and_apply_appends_policy(client, monkeypatch, session):
    ct = await _make_case_type(session)

    async def fake(prompt, system="", **kw):
        return {"name": "Resolution SLA", "scope": "case",
                "goal_duration": "PT24H", "deadline_duration": "PT48H"}
    monkeypatch.setattr(sla_builder, "_ai_generate_json", fake)

    ct_id = ct.id
    r = await client.post("/api/v1/hxnexus/draft",
                          json={"kind": "sla", "description": "resolve in 48h",
                                "case_type_id": str(ct_id)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["errors"] == [] and body["draft"]["base_checksum"]

    a = await client.post("/api/v1/hxnexus/draft/apply",
                          json={"kind": "sla", "draft": body["draft"]})
    assert a.status_code == 201, a.text

    session.expire_all()
    ct2 = await session.get(CaseTypeModel, ct_id)
    policies = ct2.definition_json["sla_policies"]
    assert len(policies) == 1 and policies[0]["goal_duration"] == "PT24H"
    assert "Drafted by HxNexus" in policies[0]["description"]

    # the definition changed on apply → the SAME draft is now stale (409)
    a2 = await client.post("/api/v1/hxnexus/draft/apply",
                           json={"kind": "sla", "draft": body["draft"]})
    assert a2.status_code == 409


@pytest.mark.asyncio
async def test_sla_replace_by_id_swaps_not_duplicates(client, monkeypatch, session):
    ct = await _make_case_type(session, definition_json={
        "stages": [{"id": "intake", "steps": []}],
        "sla_policies": [{"id": "resolution_sla", "name": "Resolution SLA",
                          "goal_duration": "PT4H", "deadline_duration": "PT8H"}]})

    async def fake(prompt, system="", **kw):
        return {"name": "Resolution SLA", "scope": "case",
                "goal_duration": "PT24H", "deadline_duration": "PT48H"}
    monkeypatch.setattr(sla_builder, "_ai_generate_json", fake)

    ct_id = ct.id
    r = await client.post("/api/v1/hxnexus/draft",
                          json={"kind": "sla", "description": "loosen to 48h",
                                "case_type_id": str(ct_id)})
    body = r.json()
    assert body["errors"] == []
    assert body["draft"]["replaces_policy_id"] == "resolution_sla"
    assert body["draft"]["before_policy"]["goal_duration"] == "PT4H"

    a = await client.post("/api/v1/hxnexus/draft/apply",
                          json={"kind": "sla", "draft": body["draft"]})
    assert a.status_code == 201, a.text
    session.expire_all()
    ct2 = await session.get(CaseTypeModel, ct_id)
    policies = ct2.definition_json["sla_policies"]
    assert len(policies) == 1 and policies[0]["goal_duration"] == "PT24H"


@pytest.mark.asyncio
async def test_sla_fallback_is_deterministic_and_labelled(client, monkeypatch, session):
    ct = await _make_case_type(session)

    async def down(prompt, system="", **kw):
        return None
    monkeypatch.setattr(sla_builder, "_ai_generate_json", down)

    r = await client.post("/api/v1/hxnexus/draft",
                          json={"kind": "sla",
                                "description": "respond within 48h, goal 24 hours",
                                "case_type_id": str(ct.id)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "fallback" and body["errors"] == []
    assert body["draft"]["policy"]["goal_duration"] == "P1D"
    assert body["draft"]["policy"]["deadline_duration"] == "P2D"

    # no explicit durations → honest 503, never a guessed deadline
    r2 = await client.post("/api/v1/hxnexus/draft",
                           json={"kind": "sla", "description": "make it fast",
                                 "case_type_id": str(ct.id)})
    assert r2.status_code == 503


@pytest.mark.asyncio
async def test_sla_draft_requires_case_type(client):
    r = await client.post("/api/v1/hxnexus/draft",
                          json={"kind": "sla", "description": "48h"})
    assert r.status_code == 400
    r = await client.post("/api/v1/hxnexus/draft",
                          json={"kind": "sla", "description": "48h",
                                "case_type_id": str(uuid.uuid4())})
    assert r.status_code == 404


# ── modify-existing: rules ──────────────────────────────────────────────────────

async def _make_rule(session) -> RuleDefinitionModel:
    rule = RuleDefinitionModel(
        name=f"Rule {uuid.uuid4().hex[:6]}", version="1.0.0", rule_type="when",
        scope="global", definition_json={
            "conditions": [{"field_path": "claim.amount", "operator": "lt",
                            "value": 500}],
            "actions": [{"action_type": "auto_approve"}]},
        enabled=True)
    session.add(rule)
    await session.commit()
    await session.refresh(rule)
    return rule


@pytest.mark.asyncio
async def test_modify_rule_diff_apply_and_checksum(client, monkeypatch, session):
    rule = await _make_rule(session)
    rule_id, rule_name = rule.id, rule.name

    async def fake(prompt, system="", **kw):
        return {"conditions": [{"field_path": "claim.amount", "operator": "lt",
                                "value": 250},
                               {"field_path": "kyc.status", "operator": "eq",
                                "value": "passed"}],
                "actions": [{"action_type": "auto_approve"}]}
    monkeypatch.setattr(modify_builder, "_ai_generate_json", fake)

    r = await client.post("/api/v1/hxnexus/draft",
                          json={"kind": "rule", "target_id": str(rule_id),
                                "description": "tighten to 250 and require KYC"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "modify" and body["errors"] == []
    assert body["diff"]["total_changes"] >= 1
    assert body["draft"]["id"] == str(rule_id)      # simulate replays as MODIFIED
    assert body["draft"]["name"] == rule_name       # name immutable

    a = await client.post("/api/v1/hxnexus/draft/apply",
                          json={"kind": "rule", "draft": body["draft"]})
    assert a.status_code == 201, a.text
    session.expire_all()
    updated = await session.get(RuleDefinitionModel, rule_id)
    assert updated.definition_json["conditions"][0]["value"] == 250
    assert updated.name == rule_name

    # the rule changed → the same draft is now stale
    a2 = await client.post("/api/v1/hxnexus/draft/apply",
                           json={"kind": "rule", "draft": body["draft"]})
    assert a2.status_code == 409


@pytest.mark.asyncio
async def test_modify_rule_tamper_and_gates(client, monkeypatch, session):
    rule = await _make_rule(session)

    async def fake(prompt, system="", **kw):
        return {"conditions": [{"field_path": "a.b", "operator": "eq", "value": 1}],
                "actions": [{"action_type": "log"}]}
    monkeypatch.setattr(modify_builder, "_ai_generate_json", fake)

    r = await client.post("/api/v1/hxnexus/draft",
                          json={"kind": "rule", "target_id": str(rule.id),
                                "description": "log instead"})
    draft = r.json()["draft"]

    # viewer cannot apply a rule modification
    v = await client.post("/api/v1/hxnexus/draft/apply",
                          json={"kind": "rule", "draft": dict(draft)},
                          headers=_viewer_headers())
    assert v.status_code == 403

    # tampered modification is re-gated server-side
    tampered = dict(draft)
    tampered["definition_json"] = {
        "conditions": [{"field_path": "a.b", "operator": "eq", "value": 1}],
        "actions": [{"action_type": "set_value", "target": "grant.admin",
                     "value": True}]}
    t = await client.post("/api/v1/hxnexus/draft/apply",
                          json={"kind": "rule", "draft": tampered})
    assert t.status_code == 400

    # unknown target
    r404 = await client.post("/api/v1/hxnexus/draft",
                             json={"kind": "rule", "target_id": str(uuid.uuid4()),
                                   "description": "x"})
    assert r404.status_code == 404


# ── modify-existing: case types + forms ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_modify_case_type_full_replacement(client, monkeypatch, session):
    ct = await _make_case_type(session)
    ct_id = ct.id
    new_def = {"stages": [{"id": "intake", "steps": []},
                          {"id": "triage", "steps": []},
                          {"id": "review", "steps": []}],
               "sla_policies": []}

    async def fake(prompt, system="", **kw):
        return new_def
    monkeypatch.setattr(modify_builder, "_ai_generate_json", fake)

    r = await client.post("/api/v1/hxnexus/draft",
                          json={"kind": "case_type", "target_id": str(ct_id),
                                "description": "add a triage stage"})
    body = r.json()
    assert body["mode"] == "modify" and body["errors"] == []

    a = await client.post("/api/v1/hxnexus/draft/apply",
                          json={"kind": "case_type", "draft": body["draft"]})
    assert a.status_code == 201, a.text
    session.expire_all()
    ct2 = await session.get(CaseTypeModel, ct_id)
    assert [s["id"] for s in ct2.definition_json["stages"]] == ["intake", "triage",
                                                                "review"]


@pytest.mark.asyncio
async def test_modify_form_preserves_other_definition_keys(client, monkeypatch,
                                                           session):
    form = FormDefinitionModel(
        name=f"Form {uuid.uuid4().hex[:6]}", version="1.0.0",
        definition_json={"fields": [{"id": "amount", "label": "Amount",
                                     "field_type": "number", "required": True}],
                         "layout": {"columns": 2}})
    session.add(form)
    await session.commit()
    await session.refresh(form)
    form_id = form.id

    async def fake(prompt, system="", **kw):
        return {"fields": [{"id": "amount", "label": "Amount",
                            "field_type": "number", "required": True},
                           {"id": "reason", "label": "Reason",
                            "field_type": "textarea", "required": False}]}
    monkeypatch.setattr(modify_builder, "_ai_generate_json", fake)

    r = await client.post("/api/v1/hxnexus/draft",
                          json={"kind": "form", "target_id": str(form_id),
                                "description": "add a reason field"})
    body = r.json()
    assert body["mode"] == "modify" and body["errors"] == []

    a = await client.post("/api/v1/hxnexus/draft/apply",
                          json={"kind": "form", "draft": body["draft"]})
    assert a.status_code == 201, a.text
    session.expire_all()
    f2 = await session.get(FormDefinitionModel, form_id)
    assert len(f2.definition_json["fields"]) == 2
    assert f2.definition_json["layout"] == {"columns": 2}   # untouched


# ── modify-existing: connector config ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_modify_connector_config_only_and_creds_never_in_prompt(
        client, monkeypatch, session):
    connector = ConnectorRegistryModel(
        name=f"conn-{uuid.uuid4().hex[:6]}", connector_type="webhook",
        config={"url": "https://old.example", "retries": 1},
        credentials={"password": "hunter2-super-secret"})
    session.add(connector)
    await session.commit()
    await session.refresh(connector)
    connector_id = connector.id

    prompts: list[str] = []

    async def fake(prompt, system="", **kw):
        prompts.append(prompt)
        return {"config": {"url": "https://new.example", "retries": 5}}
    monkeypatch.setattr(modify_builder, "_ai_generate_json", fake)

    r = await client.post("/api/v1/hxnexus/draft",
                          json={"kind": "connector", "target_id": str(connector_id),
                                "description": "point at new.example, 5 retries"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["errors"] == []
    assert "hunter2" not in " ".join(prompts)   # credentials never enter the prompt

    # viewer lacks the admin/integration gate
    v = await client.post("/api/v1/hxnexus/draft/apply",
                          json={"kind": "connector", "draft": dict(body["draft"])},
                          headers=_viewer_headers())
    assert v.status_code == 403

    a = await client.post("/api/v1/hxnexus/draft/apply",
                          json={"kind": "connector", "draft": body["draft"]})
    assert a.status_code == 201, a.text
    session.expire_all()
    c2 = await session.get(ConnectorRegistryModel, connector_id)
    assert c2.config == {"url": "https://new.example", "retries": 5}
    assert c2.credentials == {"password": "hunter2-super-secret"}   # untouched

    # a draft smuggling a credential key into config is rejected
    smuggle = dict(body["draft"])
    smuggle["config"] = {"url": "https://new.example", "api_token": "x"}
    smuggle["base_checksum"] = None
    t = await client.post("/api/v1/hxnexus/draft/apply",
                          json={"kind": "connector", "draft": smuggle})
    assert t.status_code in (400, 409)

    # connector drafts are modify-only: no target → 400
    n = await client.post("/api/v1/hxnexus/draft",
                          json={"kind": "connector", "description": "make one"})
    assert n.status_code == 400


# ── user drafts ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_user_draft_apply_generates_password_server_side(client, monkeypatch,
                                                               session):
    uname = f"jdoe{uuid.uuid4().hex[:6]}"

    async def fake(prompt, system="", **kw):
        return {"username": uname, "email": f"{uname}@example.com",
                "display_name": "John Doe", "roles": ["viewer"],
                "password": "llm-should-never-set-this"}
    monkeypatch.setattr(user_builder, "_ai_generate_json", fake)

    r = await client.post("/api/v1/hxnexus/draft",
                          json={"kind": "user",
                                "description": f"create a user named {uname}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["errors"] == [] and "password" not in body["draft"]

    a = await client.post("/api/v1/hxnexus/draft/apply",
                          json={"kind": "user", "draft": body["draft"]})
    assert a.status_code == 201, a.text
    out = a.json()
    assert out["temp_password"] and out["temp_password"] != "llm-should-never-set-this"
    assert out["password_change_required"] is True

    row = (await session.execute(
        select(HelixUserModel).where(HelixUserModel.username == uname)
    )).scalar_one()
    assert row.password_change_required is True and row.roles == ["viewer"]

    # duplicate → 409 (parity with /auth/register)
    a2 = await client.post("/api/v1/hxnexus/draft/apply",
                           json={"kind": "user", "draft": body["draft"]})
    assert a2.status_code == 409


@pytest.mark.asyncio
async def test_user_apply_is_admin_only_and_roles_regated(client, monkeypatch):
    uname = f"eve{uuid.uuid4().hex[:6]}"

    async def fake(prompt, system="", **kw):
        return {"username": uname, "email": f"{uname}@example.com",
                "roles": ["viewer"]}
    monkeypatch.setattr(user_builder, "_ai_generate_json", fake)

    r = await client.post("/api/v1/hxnexus/draft",
                          json={"kind": "user", "description": "create eve"})
    draft = r.json()["draft"]

    v = await client.post("/api/v1/hxnexus/draft/apply",
                          json={"kind": "user", "draft": dict(draft)},
                          headers=_viewer_headers())
    assert v.status_code == 403

    # tampering the card to grant admin is re-gated server-side
    tampered = {**draft, "roles": ["admin"]}
    t = await client.post("/api/v1/hxnexus/draft/apply",
                          json={"kind": "user", "draft": tampered})
    assert t.status_code == 400 and "allowlist" in t.json()["detail"]
