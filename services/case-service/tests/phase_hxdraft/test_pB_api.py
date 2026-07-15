"""HxDraft Phase B — /api/v1/hxnexus/draft endpoints.

Pins: draft generation (fake backend), Apply re-validates server-side (a
tampered client draft is rejected), rule Apply gate, conversation-owner check,
and that Apply creates real artifacts through the standard paths.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from case_service.db.models import (
    CopilotConversationModel,
    CopilotMessageModel,
    FormDefinitionModel,
    RuleDefinitionModel,
)
from case_service.nlp import form_builder, rule_builder

_RULE_JSON = {
    "name": "Small claims auto-approve", "description": "under 500",
    "conditions": [{"field_path": "claim.amount", "operator": "lt", "value": 500}],
    "actions": [{"action_type": "auto_approve"}],
}
_FORM_JSON = {
    "name": "Visitor Sign-in", "description": "lobby form",
    "fields": [{"id": "visitor_name", "label": "Visitor name",
                "field_type": "text", "required": True}],
}


@pytest.fixture
def fake_llm(monkeypatch):
    async def fake(prompt, system="", **kw):
        return _RULE_JSON if "rule" in system.lower() else _FORM_JSON
    monkeypatch.setattr(rule_builder, "_ai_generate_json", fake)
    monkeypatch.setattr(form_builder, "_ai_generate_json", fake)


@pytest.mark.asyncio
async def test_draft_rule_and_apply(client, fake_llm, session):
    r = await client.post("/api/v1/hxnexus/draft",
                          json={"kind": "rule", "description": "auto-approve claims under 500"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "rule" and body["errors"] == []
    assert body["draft"]["rule_type"] == "when"

    a = await client.post("/api/v1/hxnexus/draft/apply",
                          json={"kind": "rule", "draft": body["draft"]})
    assert a.status_code == 201, a.text
    rule = (await session.execute(
        select(RuleDefinitionModel).where(RuleDefinitionModel.id == uuid.UUID(a.json()["id"]))
    )).scalar_one()
    assert rule.rule_type == "when" and rule.enabled is True
    assert "Drafted by HxNexus" in rule.definition_json["description"]


@pytest.mark.asyncio
async def test_apply_revalidates_tampered_draft(client, fake_llm):
    r = await client.post("/api/v1/hxnexus/draft",
                          json={"kind": "rule", "description": "auto-approve small claims"})
    draft = r.json()["draft"]
    # client tampers the validated card before applying
    draft["definition_json"]["actions"] = [
        {"action_type": "set_value", "target": "user.role", "value": "superadmin"}]
    a = await client.post("/api/v1/hxnexus/draft/apply", json={"kind": "rule", "draft": draft})
    assert a.status_code == 400 and "forbidden surface" in a.json()["detail"]
    draft["definition_json"]["actions"] = [{"action_type": "delete_case"}]
    a = await client.post("/api/v1/hxnexus/draft/apply", json={"kind": "rule", "draft": draft})
    assert a.status_code == 400 and "closed action set" in a.json()["detail"]


@pytest.mark.asyncio
async def test_draft_form_and_apply(client, fake_llm, session):
    r = await client.post("/api/v1/hxnexus/draft",
                          json={"kind": "form", "description": "visitor sign-in form"})
    body = r.json()
    assert body["errors"] == [] and body["kind"] == "form"
    a = await client.post("/api/v1/hxnexus/draft/apply",
                          json={"kind": "form", "draft": body["draft"]})
    assert a.status_code == 201
    form = (await session.execute(
        select(FormDefinitionModel).where(FormDefinitionModel.id == uuid.UUID(a.json()["id"]))
    )).scalar_one()
    assert form.definition_json["fields"][0]["id"] == "visitor_name"


@pytest.mark.asyncio
async def test_draft_case_type_and_apply(client, monkeypatch, session):
    from case_service.nlp import case_type_builder as CTB

    async def fake(prompt, system="", **kw):
        return None    # force the builder's heuristic fallback — offline path works
    monkeypatch.setattr(CTB, "_ai_generate_json", fake)

    name = f"Drafted CT {uuid.uuid4().hex[:6]}"
    r = await client.post("/api/v1/hxnexus/draft",
                          json={"kind": "case_type",
                                "description": f"{name}: intake then review then done"})
    body = r.json()
    assert body["kind"] == "case_type" and body["draft"]["definition_json"].get("stages")
    body["draft"]["name"] = name
    a = await client.post("/api/v1/hxnexus/draft/apply",
                          json={"kind": "case_type", "draft": body["draft"]})
    assert a.status_code == 201, a.text
    # duplicate apply → 409, not a second case-type
    a2 = await client.post("/api/v1/hxnexus/draft/apply",
                           json={"kind": "case_type", "draft": body["draft"]})
    assert a2.status_code == 409


def _token_user_id() -> str:
    import base64
    import json as _json
    from tests.conftest import ADMIN_TOKEN
    payload = ADMIN_TOKEN.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    claims = _json.loads(base64.urlsafe_b64decode(payload))
    return claims.get("sub") or claims.get("user_id")


@pytest.mark.asyncio
async def test_conversation_append_owner_scoped(client, fake_llm, session):
    mine = CopilotConversationModel(user_id=_token_user_id())
    theirs = CopilotConversationModel(user_id="someone-else")
    session.add_all([mine, theirs])
    await session.commit()

    for conv, expect in ((mine, 2), (theirs, 0)):
        await client.post("/api/v1/hxnexus/draft",
                          json={"kind": "rule", "description": "auto approve tiny claims",
                                "conversation_id": str(conv.id)})
        n = len((await session.execute(
            select(CopilotMessageModel).where(CopilotMessageModel.conversation_id == conv.id)
        )).scalars().all())
        assert n == expect, f"{conv.user_id}: {n}"


@pytest.mark.asyncio
async def test_validation_and_auth_edges(client, anon_client, fake_llm):
    r = await client.post("/api/v1/hxnexus/draft", json={"kind": "widget", "description": "x"})
    assert r.status_code == 400
    r = await client.post("/api/v1/hxnexus/draft/apply", json={"kind": "widget", "draft": {}})
    assert r.status_code == 400
    r = await anon_client.post("/api/v1/hxnexus/draft", json={"kind": "rule", "description": "x"})
    assert r.status_code in (401, 403)
    r = await client.post("/api/v1/hxnexus/draft/apply",
                          json={"kind": "case_type", "draft": {"name": "", "definition_json": {}}})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_ai_down_rule_draft_is_503(client, monkeypatch):
    async def down(prompt, system="", **kw):
        return None
    monkeypatch.setattr(rule_builder, "_ai_generate_json", down)
    r = await client.post("/api/v1/hxnexus/draft",
                          json={"kind": "rule", "description": "auto approve"})
    assert r.status_code == 503
