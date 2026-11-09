"""Form Builder v2 §5 step 7 (2026-10-29): field-level security, drafts,
translations, test scenarios.

Every access test runs as the least-privileged user who should pass and as one
who must be refused; a case worker in an access group, never the superadmin.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from case_service.main import app
from tests.conftest import TestSessionFactory
from velaris_shared.auth.dependencies import get_current_user
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.db import repository as repo
from velaris_shared.db.models import (
    AccessGroupModel, AccessRoleModel, OperatorAccessGroupModel, PortalModel,
)
from velaris_shared.forms import portal_definition, validate
from velaris_shared.forms.security import field_access, secure_definition
from velaris_shared.forms.translate import for_caller, pick_language, translate

FORM = {
    "sections": [{"id": "s", "title": "Claim", "order": 0, "fields": [
        {"id": "f1", "type": "text", "label": "Name", "field_key": "name", "required": True},
        {"id": "f2", "type": "number", "label": "Approved amount", "field_key": "approved",
         "required": True, "security": {"edit": ["underwriter"]}},
        {"id": "f3", "type": "text", "label": "Fraud note", "field_key": "fraud_note",
         "security": {"view": ["investigator"], "edit": ["investigator"]}},
        {"id": "f4", "type": "dropdown", "label": "Colour", "field_key": "colour",
         "options": [{"label": "Red", "value": "r"}, {"label": "Blue", "value": "b"}]},
    ]}],
    "settings": {"submit_label": "Send"},
    "translations": {"de": {
        "form": {"submit_label": "Senden"},
        "sections": {"s": {"title": "Schaden"}},
        "fields": {"name": {"label": "Name (de)", "required_message": "Bitte Namen angeben"},
                   "colour": {"label": "Farbe", "options": {"r": "Rot"}}},
    }},
}


# ── pure: the rules ──────────────────────────────────────────────────────────

def test_field_access_rules():
    f = FORM["sections"][0]["fields"]
    assert field_access(f[0], set()) == "edit"                  # no security: everyone
    assert field_access(f[1], set()) == "view"                  # edit limited: others read-only
    assert field_access(f[1], {"underwriter"}) == "edit"
    assert field_access(f[2], {"underwriter"}) == "none"        # view limited: others never see it
    assert field_access(f[2], {"investigator"}) == "edit"
    assert field_access(f[2], None) == "edit"                   # superadmin


def test_served_form_drops_unseen_and_locks_view_only():
    d = secure_definition(FORM, {"underwriter"}, for_browser=True)
    keys = [x["field_key"] for x in d["sections"][0]["fields"]]
    assert "fraud_note" not in keys and "approved" in keys
    d = secure_definition(FORM, set(), for_browser=True)
    approved = next(x for x in d["sections"][0]["fields"] if x["field_key"] == "approved")
    assert approved["read_only"] is True and approved["required"] is False
    assert secure_definition(FORM, None, for_browser=True) is FORM


def test_restricted_values_sent_anyway_are_dropped():
    d = secure_definition(FORM, set(), for_browser=False)
    clean, errors = validate(d, {"name": "Jane", "approved": 999999, "fraud_note": "x"}, context={})
    assert errors == []
    assert "approved" not in clean and "fraud_note" not in clean
    # The underwriter changes it; still required for them.
    d = secure_definition(FORM, {"underwriter"}, for_browser=False)
    clean, errors = validate(d, {"name": "Jane"}, context={})
    assert [e.field_key for e in errors] == ["approved"]


def test_a_form_without_security_is_unchanged():
    plain = {"sections": [{"id": "s", "fields": [{"id": "a", "type": "text", "field_key": "a"}]}]}
    assert secure_definition(plain, set(), for_browser=True) is plain


def test_the_portal_never_shows_a_restricted_field():
    keys = [x["field_key"] for x in portal_definition(FORM)["sections"][0]["fields"]]
    assert keys == ["name", "colour"]
    tr = portal_definition(FORM)["translations"]["de"]["fields"]
    assert set(tr) == {"name", "colour"}


# ── pure: translations ───────────────────────────────────────────────────────

def test_language_choice():
    assert pick_language(FORM, "de-AT,de;q=0.9,en;q=0.8") == "de"
    assert pick_language(FORM, "fr-FR,en") is None
    assert pick_language(FORM, None) is None
    assert pick_language({"sections": []}, "de") is None


def test_translation_changes_text_only():
    d = translate(FORM, "de")
    f = d["sections"][0]["fields"]
    assert d["sections"][0]["title"] == "Schaden" and d["settings"]["submit_label"] == "Senden"
    assert f[0]["label"] == "Name (de)" and f[0]["field_key"] == "name"
    assert f[3]["options"] == [{"label": "Rot", "value": "r"}, {"label": "Blue", "value": "b"}]
    assert FORM["sections"][0]["title"] == "Claim"              # the original is untouched
    _, errors = validate(for_caller(FORM, "de"), {}, context={})
    assert errors[0].message == "Bitte Namen angeben"
    assert translate(FORM, None) is FORM and translate(FORM, "xx") is FORM


# ── through the API ──────────────────────────────────────────────────────────

async def _group(members: list[str], role_ids: list[str]) -> None:
    async with TestSessionFactory() as db:
        portal = PortalModel(name=f"P-{uuid.uuid4().hex[:6]}", portal_type="staff")
        db.add(portal)
        await db.flush()
        g = AccessGroupModel(name=f"G-{uuid.uuid4().hex[:6]}", tenant_id="default", portal_id=portal.id,
                             role_ids=role_ids, allowed_case_type_ids=["*"], allowed_queue_ids=["*"])
        db.add(g)
        await db.flush()
        for m in members:
            db.add(OperatorAccessGroupModel(operator_id=m, access_group_id=g.id))
        await db.commit()


def _as(uid: str, roles=("case_worker",)):
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(user_id=uid, roles=list(roles))


@pytest.fixture(autouse=True)
def _restore():
    yield
    app.dependency_overrides.pop(get_current_user, None)


async def _case(client: AsyncClient) -> tuple[str, str]:
    f = await client.post("/api/v1/forms", json={
        "name": f"F7-{uuid.uuid4().hex[:6]}", "version": "1.0.0", "definition_json": FORM})
    assert f.status_code == 201, f.text
    ct = await client.post("/api/v1/case-types", json={
        "name": f"S7-{uuid.uuid4().hex[:6]}", "version": "1.0.0", "lifecycle_process_id": "p",
        "definition_json": {"stages": [{"id": "st", "name": "S", "stage_type": "linear", "order": 0, "steps": [
            {"id": "step-1", "name": "Do", "step_type": "user_task", "required": True,
             "form_id": f.json()["id"]}]}], "sla_policies": []}})
    assert ct.status_code == 201, ct.text
    case = await client.post("/api/v1/cases", json={"case_type_id": ct.json()["id"], "data": {"approved": 10}})
    assert case.status_code == 201, case.text
    return case.json()["id"], f.json()["id"]


@pytest.mark.asyncio
async def test_api_field_security_on_serve_and_submit(client: AsyncClient, session):
    case_id, form_id = await _case(client)
    async with TestSessionFactory() as db:
        role = AccessRoleModel(name=f"underwriter", description="", tenant_id="default")
        db.add(role)
        await db.commit()
        role_id = str(role.id)
    await _group(["fs-worker"], [])
    await _group(["fs-uw"], [role_id])                        # by id; the form names it
    a = await repo.create_assignment(session, data={
        "case_id": uuid.UUID(case_id), "step_id": "step-1", "assignee_type": "user",
        "assignee_id": "fs-worker", "status": "active"})
    await session.commit()

    _as("fs-worker")
    r = await client.get(f"/api/v1/form-submissions/{a.id}/form")
    fields = {x["field_key"]: x for x in r.json()["form"]["definition_json"]["sections"][0]["fields"]}
    assert "fraud_note" not in fields and fields["approved"]["read_only"] is True
    r = await client.get(f"/api/v1/form-submissions/cases/{case_id}/forms/{form_id}")
    assert "fraud_note" not in [x["field_key"] for x in r.json()["definition_json"]["sections"][0]["fields"]]
    # The worker sends a value for the field they may only read: dropped; the case keeps 10.
    r = await client.post(f"/api/v1/form-submissions/{a.id}", json={
        "form_id": form_id, "values": {"name": "Jane", "approved": 999999, "fraud_note": "planted"}})
    assert r.status_code == 200, r.text
    assert "approved" not in r.json()["values"] and "fraud_note" not in r.json()["values"]
    case = await client.get(f"/api/v1/cases/{case_id}")
    assert case.json()["data"].get("approved") == 10 and "fraud_note" not in case.json()["data"]

    # The underwriter (role by id, matched by name in the form) changes it.
    a2 = await repo.create_assignment(session, data={
        "case_id": uuid.UUID(case_id), "step_id": "step-1", "assignee_type": "user",
        "assignee_id": "fs-uw", "status": "active"})
    await session.commit()
    _as("fs-uw")
    r = await client.post(f"/api/v1/form-submissions/{a2.id}", json={
        "form_id": form_id, "values": {"name": "Jane"}}, headers={"Accept-Language": "de"})
    assert r.status_code == 422 and r.json()["detail"]["field_errors"][0]["field_key"] == "approved"
    r = await client.post(f"/api/v1/form-submissions/{a2.id}", json={
        "form_id": form_id, "values": {}}, headers={"Accept-Language": "de"})
    assert "Bitte Namen angeben" in r.text                    # the caller's language
    r = await client.post(f"/api/v1/form-submissions/{a2.id}", json={
        "form_id": form_id, "values": {"name": "Jane", "approved": 500}})
    assert r.status_code == 200, r.text and r.json()["values"]["approved"] == 500


@pytest.mark.asyncio
async def test_api_drafts(client: AsyncClient, session):
    case_id, form_id = await _case(client)
    await _group(["dr-worker", "dr-other"], [])
    a = await repo.create_assignment(session, data={
        "case_id": uuid.UUID(case_id), "step_id": "step-1", "assignee_type": "user",
        "assignee_id": "dr-worker", "status": "active"})
    await session.commit()
    url = f"/api/v1/form-submissions/cases/{case_id}/forms/{form_id}/draft"

    _as("dr-nobody")                                           # in no access group: refused
    assert (await client.put(url, json={"values": {"name": "x"}})).status_code == 403
    assert (await client.get(url)).status_code == 403

    _as("dr-worker")
    assert (await client.get(url)).json() == {"draft": None}
    r = await client.put(url, json={"values": {"name": "Ja", "approved": 1, "fraud_note": "x",
                                               "status": "closed", "colour": "not-an-option"}})
    assert r.status_code == 200, r.text
    # Only keys the caller may change; never checked (an invalid choice is kept).
    assert r.json()["draft"]["values"] == {"name": "Ja", "colour": "not-an-option"}
    assert (await client.get(url)).json()["draft"]["values"]["name"] == "Ja"
    assert (await client.put(url, json={"values": {"name": "x" * 300_000}})).status_code == 413

    _as("dr-other")                                            # a colleague: the draft is not theirs
    assert (await client.get(url)).json() == {"draft": None}

    _as("dr-worker")
    r = await client.post(f"/api/v1/form-submissions/{a.id}", json={"form_id": form_id, "values": {"name": "Jane"}})
    assert r.status_code == 200, r.text
    assert (await client.get(url)).json() == {"draft": None}   # submitted: gone
    await client.put(url, json={"values": {"name": "again"}})
    assert (await client.delete(url)).status_code == 204
    assert (await client.get(url)).json() == {"draft": None}


@pytest.mark.asyncio
async def test_api_test_run_and_roles(client: AsyncClient):
    tests = [
        {"name": "ok", "values": {"name": "Jane", "approved": 5}, "expect": {"valid": True, "values": {"approved": 5}}},
        {"name": "missing name", "values": {"approved": 5}, "expect": {"valid": False, "errors": ["name"]}},
        {"name": "wrong", "values": {"name": "Jane", "approved": 5}, "expect": {"valid": False}},
        {"name": "as a worker", "roles": ["nobody"], "values": {"name": "J", "approved": 5},
         "expect": {"valid": True, "values": {"approved": None}}},
    ]
    r = await client.post("/api/v1/forms/test-run", json={"definition": FORM, "tests": tests})
    assert r.status_code == 200, r.text
    assert [x["passed"] for x in r.json()["results"]] == [True, True, False, True]
    assert r.json()["results"][2]["problems"] == ["Expected it to be refused"]

    _as("tr-worker", roles=("case_worker",))                   # not a designer
    assert (await client.post("/api/v1/forms/test-run", json={"definition": FORM, "tests": []})).status_code == 403
    assert (await client.get("/api/v1/forms/access-roles")).status_code == 403
    _as("tr-designer", roles=("designer",))
    assert (await client.post("/api/v1/forms/test-run", json={"definition": FORM, "tests": tests})).status_code == 200
    r = await client.get("/api/v1/forms/access-roles")
    assert r.status_code == 200 and isinstance(r.json()["roles"], list)


def test_shared_translation_table():
    """Studio's translate.ts runs the same table (Step7.test.tsx)."""
    import json
    import pathlib
    import velaris_shared.forms as forms_pkg
    cases = json.loads((pathlib.Path(forms_pkg.__file__).parent / "translation_cases.json").read_text())
    for c in cases["pick"]:
        assert pick_language(cases["definition"], ",".join(c["accept"]) or None) == c["lang"], c
    for c in cases["translate"]:
        assert translate(cases["definition"], c["lang"]) == c["expect"]


@pytest.mark.asyncio
async def test_api_draft_only_for_the_cases_own_forms(client: AsyncClient, session):
    """A form no step of the case uses has no draft on that case (404)."""
    case_id, form_id = await _case(client)
    other = await client.post("/api/v1/forms", json={
        "name": f"F7o-{uuid.uuid4().hex[:6]}", "version": "1.0.0", "definition_json": FORM})
    await _group(["dr2-worker"], [])
    await repo.create_assignment(session, data={
        "case_id": uuid.UUID(case_id), "step_id": "step-1", "assignee_type": "user",
        "assignee_id": "dr2-worker", "status": "active"})
    await session.commit()
    _as("dr2-worker")
    r = await client.put(f"/api/v1/form-submissions/cases/{case_id}/forms/{other.json()['id']}/draft",
                         json={"values": {"name": "x"}})
    assert r.status_code == 404
    r = await client.put(f"/api/v1/form-submissions/cases/{case_id}/forms/{form_id}/draft",
                         json={"values": {"name": "x"}})
    assert r.status_code == 200, r.text
