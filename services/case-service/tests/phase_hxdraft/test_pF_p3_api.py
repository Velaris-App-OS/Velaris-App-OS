"""HxDraft P3 Phase F — escalation draft/apply/stage/modify, routing, lint.

Pins: escalation Apply endpoint-parity (any authenticated user), the
inactive-create → merge-activates stage flow, the surgical routing patch through
the canonical case-type gate, checksum 409s, and lint-is-advisory.
"""
from __future__ import annotations

import uuid

import pytest

from case_service.db.models import (
    ArtifactBranchModel,
    CaseTypeModel,
    EscalationTreeModel,
)
from case_service.nlp import escalation_builder, modify_builder, routing_builder, rule_builder

from .test_pD_p2_api import _make_case_type, _viewer_headers

_TREE_JSON = {
    "name": "Ops escalation", "description": "page then reassign",
    "levels": [
        {"level": 1, "name": "warn", "trigger": {"type": "goal_pct", "value": 80},
         "actions": [{"type": "notify",
                      "target_type": "manager_of_current_assignee"}]},
        {"level": 2, "name": "breach", "trigger": {"type": "at_breach"},
         "actions": [{"type": "reassign", "target_type": "queue",
                      "target_id": "ops"}]},
    ],
}


@pytest.fixture
def fake_escalation_llm(monkeypatch):
    async def fake(prompt, system="", **kw):
        return dict(_TREE_JSON)
    monkeypatch.setattr(escalation_builder, "_ai_generate_json", fake)


# ── escalation drafts ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_escalation_draft_and_apply_any_auth(client, fake_escalation_llm,
                                                   session):
    r = await client.post("/api/v1/hxnexus/draft",
                          json={"kind": "escalation",
                                "description": "page manager at 80%, ops at breach"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["errors"] == []
    assert body["draft"]["tree_json"]["levels"][0]["trigger"]["value"] == 80

    # endpoint parity (signed-off P3): a viewer may apply an escalation draft
    a = await client.post("/api/v1/hxnexus/draft/apply",
                          json={"kind": "escalation", "draft": body["draft"]},
                          headers=_viewer_headers())
    assert a.status_code == 201, a.text
    tree = await session.get(EscalationTreeModel, uuid.UUID(a.json()["id"]))
    assert tree.is_active is True and len(tree.tree_json["levels"]) == 2
    assert "Drafted by HxNexus" in tree.description


@pytest.mark.asyncio
async def test_escalation_apply_regates_tampered_draft(client, fake_escalation_llm):
    r = await client.post("/api/v1/hxnexus/draft",
                          json={"kind": "escalation", "description": "page manager"})
    draft = r.json()["draft"]
    draft["tree_json"]["levels"][0]["actions"] = [{"type": "delete_all_cases"}]
    a = await client.post("/api/v1/hxnexus/draft/apply",
                          json={"kind": "escalation", "draft": draft})
    assert a.status_code == 400 and "closed action set" in a.json()["detail"]


@pytest.mark.asyncio
async def test_escalation_scoped_draft_requires_real_case_type(client,
                                                               fake_escalation_llm):
    r = await client.post("/api/v1/hxnexus/draft",
                          json={"kind": "escalation", "description": "page manager",
                                "case_type_id": str(uuid.uuid4())})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_escalation_stage_inactive_then_merge_activates(client,
                                                              fake_escalation_llm,
                                                              session):
    r = await client.post("/api/v1/hxnexus/draft",
                          json={"kind": "escalation", "description": "page manager"})
    s = await client.post("/api/v1/hxnexus/draft/stage",
                          json={"kind": "escalation", "draft": r.json()["draft"]},
                          headers=_viewer_headers())    # any-auth, like rules
    assert s.status_code == 201, s.text
    body = s.json()

    tree = await session.get(EscalationTreeModel, uuid.UUID(body["tree_id"]))
    assert tree.is_active is False                     # nothing fires until merge
    branch = await session.get(ArtifactBranchModel, uuid.UUID(body["branch_id"]))
    assert branch.artifact_type == "escalation"
    assert branch.base_snapshot["is_active"] is False
    assert branch.content_snapshot["is_active"] is True

    reviewer = str(uuid.uuid4())
    sub = await client.post(f"/api/v1/branches/{body['branch_id']}/submit",
                            json={"assigned_reviewer_id": reviewer})
    assert sub.status_code == 200, sub.text
    rev = await client.post(f"/api/v1/branches/{body['branch_id']}/reviews",
                            json={"decision": "approved"})
    assert rev.status_code == 201, rev.text

    session.expire_all()
    tree2 = await session.get(EscalationTreeModel, uuid.UUID(body["tree_id"]))
    assert tree2.is_active is True


@pytest.mark.asyncio
async def test_escalation_modify_diff_checksum(client, monkeypatch, session):
    tree = EscalationTreeModel(name=f"Tree {uuid.uuid4().hex[:6]}",
                               scope="global", tree_json=dict(
                                   levels=_TREE_JSON["levels"]), is_active=True)
    session.add(tree)
    await session.commit()
    await session.refresh(tree)
    tree_id, tree_name = tree.id, tree.name

    async def fake(prompt, system="", **kw):
        return {"levels": [{"level": 1, "name": "warn",
                            "trigger": {"type": "goal_pct", "value": 90},
                            "actions": [{"type": "notify",
                                         "target_type": "current_assignee"}]}]}
    monkeypatch.setattr(modify_builder, "_ai_generate_json", fake)

    r = await client.post("/api/v1/hxnexus/draft",
                          json={"kind": "escalation", "target_id": str(tree_id),
                                "description": "warn at 90 instead, drop level 2"})
    body = r.json()
    assert body["mode"] == "modify" and body["errors"] == []
    assert body["draft"]["name"] == tree_name          # immutable
    assert body["diff"]["total_changes"] >= 1

    a = await client.post("/api/v1/hxnexus/draft/apply",
                          json={"kind": "escalation", "draft": body["draft"]})
    assert a.status_code == 201, a.text
    session.expire_all()
    t2 = await session.get(EscalationTreeModel, tree_id)
    assert len(t2.tree_json["levels"]) == 1
    assert t2.tree_json["levels"][0]["trigger"]["value"] == 90
    assert t2.is_active is True                        # activation untouched

    a2 = await client.post("/api/v1/hxnexus/draft/apply",
                           json={"kind": "escalation", "draft": body["draft"]})
    assert a2.status_code == 409                       # tree changed → stale draft


# ── routing drafts ──────────────────────────────────────────────────────────────

_ROUTING_CT_DEF = {"stages": [
    {"id": "review", "name": "Review",
     "steps": [{"id": "approve", "name": "Approve", "step_type": "approval",
                "assignment": {"strategy": "queue_based", "target": "default-queue"},
                "form_fields": [{"id": "amount", "field_type": "number"}]}]},
    {"id": "done", "name": "Done", "steps": []}],
    "sla_policies": []}


@pytest.mark.asyncio
async def test_routing_draft_surgical_patch_and_checksum(client, monkeypatch,
                                                         session):
    ct = await _make_case_type(session, definition_json=_ROUTING_CT_DEF)
    ct_id = ct.id

    async def fake(prompt, system="", **kw):
        assert "approve" in prompt                     # structure reaches the model
        return {"stage_id": "review", "step_id": "approve",
                "assignment": {"strategy": "least_loaded",
                               "fallback_strategy": "queue_based"}}
    monkeypatch.setattr(routing_builder, "_ai_generate_json", fake)

    r = await client.post("/api/v1/hxnexus/draft",
                          json={"kind": "routing", "target_id": str(ct_id),
                                "description": "route approvals to least loaded"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "modify" and body["errors"] == []
    assert body["diff"]["total_changes"] == 1          # only the assignment

    # viewer lacks the canonical case-type write gate
    v = await client.post("/api/v1/hxnexus/draft/apply",
                          json={"kind": "routing", "draft": dict(body["draft"])},
                          headers=_viewer_headers())
    assert v.status_code == 403

    a = await client.post("/api/v1/hxnexus/draft/apply",
                          json={"kind": "routing", "draft": body["draft"]})
    assert a.status_code == 201, a.text
    session.expire_all()
    ct2 = await session.get(CaseTypeModel, ct_id)
    step = ct2.definition_json["stages"][0]["steps"][0]
    assert step["assignment"] == {"strategy": "least_loaded",
                                  "fallback_strategy": "queue_based"}
    assert step["form_fields"] == [{"id": "amount", "field_type": "number"}]
    assert ct2.definition_json["stages"][1] == {"id": "done", "name": "Done",
                                                "steps": []}

    a2 = await client.post("/api/v1/hxnexus/draft/apply",
                           json={"kind": "routing", "draft": body["draft"]})
    assert a2.status_code == 409                       # definition changed → stale

    n = await client.post("/api/v1/hxnexus/draft",
                          json={"kind": "routing", "description": "no target"})
    assert n.status_code == 400                        # routing is modify-only


# ── lint (advisory) ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rule_lint_attached_and_never_blocks(client, monkeypatch, session):
    ct = await _make_case_type(session, definition_json={
        "stages": [{"id": "intake", "steps": []}],
        "variables": [{"id": "amount", "field_type": "text"}]})
    ct_id = ct.id

    async def fake(prompt, system="", **kw):
        return {"name": f"Lint rule {uuid.uuid4().hex[:6]}",
                "conditions": [{"field_path": "amount", "operator": "lt",
                                "value": 500}],
                "actions": [{"action_type": "auto_approve"}]}
    monkeypatch.setattr(rule_builder, "_ai_generate_json", fake)

    r = await client.post("/api/v1/hxnexus/draft",
                          json={"kind": "rule", "description": "approve small",
                                "case_type_id": str(ct_id)})
    body = r.json()
    assert body["errors"] == []                        # valid — lint is separate
    assert any("can never fire" in f for f in body["lint"])

    # advisory: the linted draft still applies
    a = await client.post("/api/v1/hxnexus/draft/apply",
                          json={"kind": "rule", "draft": body["draft"]})
    assert a.status_code == 201, a.text


@pytest.mark.asyncio
async def test_rule_lint_absent_without_scope(client, monkeypatch):
    async def fake(prompt, system="", **kw):
        return {"name": f"Global rule {uuid.uuid4().hex[:6]}",
                "conditions": [{"field_path": "amount", "operator": "lt",
                                "value": 500}],
                "actions": [{"action_type": "auto_approve"}]}
    monkeypatch.setattr(rule_builder, "_ai_generate_json", fake)
    r = await client.post("/api/v1/hxnexus/draft",
                          json={"kind": "rule", "description": "approve small"})
    assert "lint" not in r.json()                      # global rules can't be linted
