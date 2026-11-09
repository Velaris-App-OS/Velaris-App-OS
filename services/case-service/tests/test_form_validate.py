"""The server-side check of form values (Form Builder redesign part 1, 2026-10-28).

`velaris_shared.forms.validate` runs the case table Studio also runs
(`validation_cases.json`), and both form paths use it: a form submission, and
completing a Case Manager step that has a form — which until now checked
nothing.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
import pathlib
import uuid
from datetime import date

import pytest
import pytest_asyncio
from httpx import AsyncClient

from case_service.main import app
from velaris_shared.auth.dependencies import get_current_user
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.db import repository as repo
from velaris_shared.forms import validate

import velaris_shared.forms as _forms_pkg

CASES = json.loads((pathlib.Path(_forms_pkg.__file__).parent / "validation_cases.json").read_text())["cases"]


def _def(fields):
    return {"sections": [{"id": "s", "title": "S", "order": 0, "fields": fields}]}


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_shared_case_table(case):
    today = date.fromisoformat(case["today"]) if "today" in case else None
    clean, errors = validate(_def(case["fields"]), case["values"], today=today)
    assert {e.field_key: e.message for e in errors} == case["errors"]
    if "clean" in case:
        assert clean == case["clean"]


# ── the two server paths ──────────────────────────────────────────────────────

@pytest_asyncio.fixture(autouse=True)
async def _in_an_access_group():
    """Since 2026-10-28 completing a step needs the caller in an access group
    covering the case type (velaris_shared/hxguard/step_submit.py)."""
    from tests.conftest import grant_case_create
    await grant_case_create("worker-1")


FIELDS = [
    {"id": "f1", "type": "text", "label": "Full Name", "field_key": "full_name", "required": True},
    {"id": "f2", "type": "text", "label": "Channel", "field_key": "channel", "hidden": True,
     "default_value": "web"},
]


def _worker() -> AuthenticatedUser:
    """Least-privileged user who fills a form in."""
    return AuthenticatedUser(user_id="worker-1", roles=["case_worker"])


async def _setup(client: AsyncClient, session, form_id: str | None = None):
    if form_id is None:
        form = await client.post("/api/v1/forms", json={
            "name": f"F-{uuid.uuid4().hex[:6]}", "version": "1.0.0", "definition_json": _def(FIELDS)})
        assert form.status_code == 201, form.text
        form_id = form.json()["id"]
    ct = await client.post("/api/v1/case-types", json={
        "name": f"FV-{uuid.uuid4().hex[:6]}", "version": "1.0.0", "lifecycle_process_id": "p",
        "definition_json": {"stages": [{"id": "st", "name": "S", "stage_type": "linear", "order": 0,
            "steps": [{"id": "step-form", "name": "Fill", "step_type": "user_task", "required": True,
                       "form_id": form_id, "assignment": {"strategy": "queue_based"}}]}],
            "sla_policies": []}})
    assert ct.status_code == 201, ct.text
    case = await client.post("/api/v1/cases", json={"case_type_id": ct.json()["id"], "data": {}})
    assert case.status_code == 201, case.text
    return form_id, case.json()["id"]


def _complete(case_id, data, status="completed"):
    return f"/api/v1/cases/{case_id}/steps/step-form/complete", {
        "stage_id": "st", "step_type": "user_task", "status": status, "data": data}


@pytest.mark.asyncio
async def test_form_submission_refuses_an_empty_required_value(client: AsyncClient, session):
    form_id, case_id = await _setup(client, session)
    a = await repo.create_assignment(session, data={
        "case_id": uuid.UUID(case_id), "step_id": "step-form", "assignee_type": "user",
        "assignee_id": "worker-1", "status": "active"})
    await session.commit()
    r = await client.post(f"/api/v1/form-submissions/{a.id}",
                          json={"form_id": form_id, "values": {"full_name": ""}})
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert detail["field_errors"] == [{"field_key": "full_name", "message": "Full Name is required"}]
    assert detail["validation_errors"] and detail["message"]


@pytest.mark.asyncio
async def test_form_submission_stores_the_default_not_a_forged_hidden_value(client: AsyncClient, session):
    form_id, case_id = await _setup(client, session)
    a = await repo.create_assignment(session, data={
        "case_id": uuid.UUID(case_id), "step_id": "step-form", "assignee_type": "user",
        "assignee_id": "worker-1", "status": "active"})
    await session.commit()
    r = await client.post(f"/api/v1/form-submissions/{a.id}", json={
        "form_id": form_id, "values": {"full_name": "Jane", "channel": "forged"}})
    assert r.status_code == 200, r.text
    data = (await client.get(f"/api/v1/cases/{case_id}")).json()["data"]
    assert data["full_name"] == "Jane" and data["channel"] == "web"


@pytest.mark.asyncio
async def test_step_completion_is_checked_against_its_form(client: AsyncClient, session):
    _, case_id = await _setup(client, session)
    app.dependency_overrides[get_current_user] = _worker
    try:
        url, body = _complete(case_id, {"channel": "forged"})
        r = await client.post(url, json=body)
        assert r.status_code == 422, r.text
        assert r.json()["detail"]["field_errors"][0]["field_key"] == "full_name"

        url, body = _complete(case_id, {"full_name": "Jane", "channel": "forged"})
        r = await client.post(url, json=body)
        assert r.status_code == 200, r.text
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    done = (await client.get(f"/api/v1/cases/{case_id}/step-completions")).json()
    row = next(c for c in done if c["step_id"] == "step-form")
    assert row["data"] == {"full_name": "Jane", "channel": "web"}


@pytest.mark.asyncio
async def test_a_rejection_is_not_checked_against_the_form(client: AsyncClient, session):
    _, case_id = await _setup(client, session)
    app.dependency_overrides[get_current_user] = _worker
    try:
        url, body = _complete(case_id, {"reason": "wrong customer"}, status="rejected")
        r = await client.post(url, json=body)
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_a_step_whose_form_is_gone_completes_as_before(client: AsyncClient, session):
    _, case_id = await _setup(client, session, form_id=str(uuid.uuid4()))
    app.dependency_overrides[get_current_user] = _worker
    try:
        url, body = _complete(case_id, {"anything": 1})
        r = await client.post(url, json=body)
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert r.status_code == 200, r.text


# ── never a crash, never a hang (2026-10-28 audit) ────────────────────────────

SAFE = json.loads((pathlib.Path(_forms_pkg.__file__).parent / "validation_cases.json").read_text())["safe_patterns"]


@pytest.mark.parametrize("pattern,expected", list(SAFE.items()))
def test_safe_pattern_table(pattern, expected):
    from velaris_shared.forms.validate import safe_pattern
    assert safe_pattern(pattern) is expected


def test_malformed_definitions_never_raise():
    import random
    rnd = random.Random(7)
    junk = [None, 1, "x", [], {}, [1], {"a": 1}, True, 1.5, "(a+)+", {"rules": "x"}, [{"field": 1}]]
    keys = ["type", "field_key", "id", "key", "required", "validation", "options", "show_when",
            "conditions", "hidden", "read_only", "default_value", "options_source", "required_message"]
    types = ["text", "email", "number", "date", "dropdown", "multi_select", "checkbox", "file_upload", "rating", None]
    for _ in range(3000):
        fields = []
        for i in range(rnd.randint(0, 6)):
            f = {k: rnd.choice(junk) for k in rnd.sample(keys, rnd.randint(0, len(keys)))}
            f["type"] = rnd.choice(types)
            f["field_key"] = f"k{i}"
            fields.append(f)
        definition = rnd.choice([{"sections": [{"fields": fields}]}, {"sections": fields}, {"sections": "x"},
                                 None, {"sections": [{"fields": "x"}]}, {"sections": [None, {"fields": fields}]}])
        values = {f"k{i}": rnd.choice(junk) for i in range(6)}
        validate(definition, values)          # must not raise


def test_a_long_rule_chain_does_not_exhaust_the_stack():
    fields = [{"id": f"f{i}", "field_key": f"f{i}", "type": "text", "label": "x",
               "show_when": {"rules": [{"field": f"f{i + 1}", "op": "is_empty"}]}} for i in range(3000)]
    validate({"sections": [{"fields": fields}]}, {})


@pytest.mark.asyncio
async def test_a_form_submission_must_use_the_steps_own_form(client: AsyncClient, session):
    """2026-10-28 audit: the caller named `form_id`, so an empty form skipped the check."""
    form_id, case_id = await _setup(client, session)
    lenient = await client.post("/api/v1/forms", json={
        "name": "Empty", "version": "1.0.0", "definition_json": {"sections": []}})
    a = await repo.create_assignment(session, data={
        "case_id": uuid.UUID(case_id), "step_id": "step-form", "assignee_type": "user",
        "assignee_id": "worker-1", "status": "active"})
    await session.commit()
    r = await client.post(f"/api/v1/form-submissions/{a.id}",
                          json={"form_id": lenient.json()["id"], "values": {"anything": 1}})
    assert r.status_code == 422, r.text
    assert "not the form of this step" in r.json()["detail"]["message"]


@pytest.mark.asyncio
async def test_only_an_exact_rejection_skips_the_form_check(client: AsyncClient, session):
    _, case_id = await _setup(client, session)
    app.dependency_overrides[get_current_user] = _worker
    try:
        for status in ("completed ", "done", "Completed"):
            url, body = _complete(case_id, {}, status=status)
            r = await client.post(url, json=body)
            assert r.status_code == 422, (status, r.text)
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_a_submission_stores_only_the_forms_keys(client: AsyncClient, session):
    """2026-10-28 audit: any key a caller added reached case data."""
    form_id, case_id = await _setup(client, session)
    a = await repo.create_assignment(session, data={
        "case_id": uuid.UUID(case_id), "step_id": "step-form", "assignee_type": "user",
        "assignee_id": "worker-1", "status": "active"})
    await session.commit()
    r = await client.post(f"/api/v1/form-submissions/{a.id}", json={
        "form_id": form_id, "values": {"full_name": "Jane", "approved": True, "status": "closed"}})
    assert r.status_code == 200, r.text
    data = (await client.get(f"/api/v1/cases/{case_id}")).json()["data"]
    assert data["full_name"] == "Jane" and "approved" not in data
    assert data.get("status") != "closed"


@pytest.mark.asyncio
async def test_a_step_with_a_form_cannot_be_completed_without_it(client: AsyncClient, session):
    """2026-10-28 audit: POST /assignments/{id}/complete took no values, so it skipped a required form."""
    _, case_id = await _setup(client, session)
    a = await repo.create_assignment(session, data={
        "case_id": uuid.UUID(case_id), "step_id": "step-form", "assignee_type": "user",
        "assignee_id": "worker-1", "status": "active"})
    await session.commit()
    r = await client.post(f"/api/v1/assignments/{a.id}/complete", json={"completed_by": "worker-1"})
    assert r.status_code == 409, r.text
    assert "submit the form" in r.text


# ── Form Builder v2: formulas read the case, not a forged value (2026-10-28) ──

V2 = {"sections": [{"id": "s", "title": "S", "order": 0, "fields": [
    {"id": "q", "type": "number", "label": "Qty", "field_key": "qty"},
    {"id": "p", "type": "number", "label": "Unit price", "field_key": "unit_price", "read_only": True},
    {"id": "t", "type": "number", "label": "Total", "field_key": "total", "calculation": "qty * unit_price"},
]}]}


def test_a_read_only_input_reads_the_case_value_not_the_submitted_one():
    clean, errors = validate(V2, {"qty": 2, "unit_price": 0.01, "total": 0.02}, context={"unit_price": 50})
    assert errors == [] and clean["total"] == 100 and "unit_price" not in clean


def test_a_bound_read_only_input_reads_its_variable():
    d = json.loads(json.dumps(V2))
    d["sections"][0]["fields"][1]["variable"] = "price_band"
    clean, _ = validate(d, {"qty": 3, "unit_price": 1}, context={"price_band": 10, "unit_price": 99})
    assert clean["total"] == 30


def test_without_the_case_a_read_only_input_reads_as_before():
    clean, _ = validate(V2, {"qty": 2, "unit_price": 7})
    assert clean["total"] == 14


@pytest.mark.asyncio
async def test_a_submission_computes_the_total_from_the_case(client: AsyncClient, session):
    form = await client.post("/api/v1/forms", json={
        "name": f"F-{uuid.uuid4().hex[:6]}", "version": "1.0.0", "definition_json": V2})
    form_id, case_id = await _setup(client, session, form.json()["id"])
    case = await repo.get_case_instance(session, uuid.UUID(case_id))
    case.data = {**(case.data or {}), "unit_price": 50}
    await session.commit()
    a = await repo.create_assignment(session, data={
        "case_id": uuid.UUID(case_id), "step_id": "step-form", "assignee_type": "user",
        "assignee_id": "worker-1", "status": "active"})
    await session.commit()
    app.dependency_overrides[get_current_user] = _worker
    try:
        r = await client.post(f"/api/v1/form-submissions/{a.id}", json={
            "form_id": form_id, "values": {"qty": 2, "unit_price": 0.01, "total": 0.02}})
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert r.status_code == 200, r.text
    data = (await client.get(f"/api/v1/cases/{case_id}")).json()["data"]
    assert data["total"] == 100 and data["unit_price"] == 50


def test_formulas_cannot_stall_or_crash_a_submit():
    """Long chains, wide fan-in, deep nesting, cycles and junk keys: bounded and never raising."""
    import time
    fields = [{"id": "f0", "type": "number", "field_key": "f0"}]
    for i in range(1, 400):                                    # a chain 400 long
        fields.append({"id": f"f{i}", "type": "number", "field_key": f"f{i}", "calculation": f"f{i-1} + 1",
                       "show_when": {"expression": f"f{i-1} >= 0"}, "required_when": {"expression": f"f{i-1} > 5"},
                       "checks": [{"expression": f"f{i} > f{i-1}", "message": "x"}]})
    wide = " + ".join(f"f{i}" for i in range(0, 150))           # fan-in over 150 fields
    fields.append({"id": "w", "type": "number", "field_key": "w", "calculation": wide})
    fields.append({"id": "d", "type": "number", "field_key": "d", "calculation": "(" * 60 + "f0" + ")" * 60})
    fields.append({"id": "c1", "type": "number", "field_key": "c1", "calculation": "c2 + 1", "read_only_when": {"expression": "c2 > 0"}})
    fields.append({"id": "c2", "type": "number", "field_key": "c2", "calculation": "c1 + 1", "show_when": {"expression": "c1 > 0"}})
    fields.append({"id": "junk", "type": "text", "field_key": "junk", "calculation": {"x": 1},
                   "show_when": {"expression": 5}, "required_when": {"rules": "no"}, "checks": "no", "read_only_when": []})
    d = {"sections": [{"id": "s", "fields": fields}]}
    t = time.monotonic()
    clean, errors = validate(d, {"f0": 1, "junk": [[[[]]]]})
    assert time.monotonic() - t < 5
    assert clean["d"] == 1 and "c1" not in clean and isinstance(errors, list)


# ── Form Builder v2: repeating groups (2026-10-29) ────────────────────────────

GROUPS = {"sections": [{"id": "s", "title": "S", "order": 0, "fields": [
    {"id": "i", "type": "repeating_group", "label": "Items", "field_key": "items", "required": True, "fields": [
        {"id": "d", "type": "text", "label": "Description", "field_key": "desc", "required": True},
        {"id": "q", "type": "number", "label": "Qty", "field_key": "qty"},
        {"id": "p", "type": "number", "label": "Price", "field_key": "price", "read_only": True},
        {"id": "l", "type": "number", "label": "Line", "field_key": "line", "calculation": "qty * price"},
    ]},
    {"id": "t", "type": "number", "label": "Total", "field_key": "total", "calculation": "sum(items.line)"},
]}]}


def test_a_row_read_only_value_comes_from_the_case_row():
    clean, errors = validate(GROUPS, {"items": [{"desc": "A", "qty": 2, "price": 0.01}, {"desc": "B", "qty": 1, "price": 0.01}]},
                             context={"items": [{"price": 10}, {"price": 7}]})
    assert errors == []
    # The forged prices are dropped (the case keeps its own), and each line uses the case's price.
    assert clean["items"] == [{"desc": "A", "qty": 2, "line": 20}, {"desc": "B", "qty": 1, "line": 7}]
    assert clean["total"] == 27


def test_too_many_rows_are_refused_before_any_is_read():
    _, errors = validate(GROUPS, {"items": [{"desc": "x"}] * 501})
    assert [e.as_dict() for e in errors] == [{"field_key": "items", "message": "At most 500 rows"}]


@pytest.mark.asyncio
async def test_a_submission_stores_checked_rows_and_the_servers_total(client: AsyncClient, session):
    form = await client.post("/api/v1/forms", json={
        "name": f"F-{uuid.uuid4().hex[:6]}", "version": "1.0.0", "definition_json": GROUPS})
    form_id, case_id = await _setup(client, session, form.json()["id"])
    a = await repo.create_assignment(session, data={
        "case_id": uuid.UUID(case_id), "step_id": "step-form", "assignee_type": "user",
        "assignee_id": "worker-1", "status": "active"})
    await session.commit()
    app.dependency_overrides[get_current_user] = _worker
    try:
        bad = await client.post(f"/api/v1/form-submissions/{a.id}", json={
            "form_id": form_id, "values": {"items": [{"desc": "A", "qty": 1}, {"qty": 2}]}})
        assert bad.status_code == 422
        assert {"field_key": "items[1].desc", "message": "Description is required"} in bad.json()["detail"]["field_errors"]
        r = await client.post(f"/api/v1/form-submissions/{a.id}", json={"form_id": form_id, "values": {
            "items": [{"desc": "A", "qty": 2, "price": 99, "line": 1, "evil": True}], "total": 5000}})
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert r.status_code == 200, r.text
    data = (await client.get(f"/api/v1/cases/{case_id}")).json()["data"]
    # Price is read-only and the case holds none: no line, and the total is the
    # server's sum of nothing — never the 5000 or the line sent.
    assert data["items"] == [{"desc": "A", "qty": 2}] and data["total"] == 0


def test_a_location_reaches_the_portal_and_is_checked_there():
    """Location field (2026-10-29): a customer can drop a pin on the portal; the
    portal's copy of the form keeps the field, and its submit stores the two
    numbers only."""
    from velaris_shared.forms.portal import portal_definition, portal_keys
    form = _def([{"id": "where", "type": "location", "label": "Where", "field_key": "where", "required": True}])
    portal = portal_definition(form, for_browser=True)
    assert [f["type"] for f in portal["sections"][0]["fields"]] == ["location"]
    assert portal_keys(form) == {"where"}
    clean, errors = validate(portal_definition(form), {"where": {"lat": 1, "lng": 2, "x": "<script>"}})
    assert errors == [] and clean == {"where": {"lat": 1.0, "lng": 2.0}}
    _, errors = validate(portal_definition(form), {"where": {"lat": float("nan"), "lng": 2}})
    assert [e.message for e in errors] == ["Enter both latitude and longitude"]


def test_small_controls_reach_the_portal_with_their_words():
    """Small controls (2026-10-29): a portal form keeps them, the static blocks
    keep their text, and a value sent under a heading is never stored."""
    from velaris_shared.forms.portal import portal_definition, portal_keys
    form = _def([
        {"id": "h", "type": "heading", "label": "About you", "field_key": "h"},
        {"id": "c", "type": "callout", "label": "Note", "field_key": "c", "text": "Read me", "tone": "warning"},
        {"id": "ok", "type": "consent", "label": "Terms", "field_key": "ok", "required": True, "terms": "T&C"},
        {"id": "acct", "type": "iban", "label": "IBAN", "field_key": "acct"},
    ])
    fields = portal_definition(form, for_browser=True)["sections"][0]["fields"]
    assert [f["type"] for f in fields] == ["heading", "callout", "consent", "iban"]
    assert fields[1]["text"] == "Read me" and fields[1]["tone"] == "warning" and fields[2]["terms"] == "T&C"
    assert portal_keys(form) == {"h", "c", "ok", "acct"}
    clean, errors = validate(portal_definition(form), {"h": "x", "ok": True, "acct": "de89 3704 0044 0532 0130 00"})
    assert errors == [] and clean == {"ok": True, "acct": "DE89370400440532013000"}
    _, errors = validate(portal_definition(form), {})
    assert [e.message for e in errors] == ["Please agree to continue"]


def test_batch_m_on_the_portal_pickers_are_left_out():
    """Batch M (2026-10-29): a customer may fill an address, sign, write rich
    text and press a button; pickers search the tenant's people and cases, so
    an anonymous portal form never has one."""
    from velaris_shared.forms.portal import portal_definition, portal_keys
    form = _def([
        {"id": "a", "type": "address", "label": "Address", "field_key": "a"},
        {"id": "s", "type": "signature", "label": "Sign", "field_key": "s"},
        {"id": "r", "type": "rich_text", "label": "Notes", "field_key": "r"},
        {"id": "b", "type": "button", "label": "Go", "field_key": "b",
         "actions": [{"action": "open_link", "url": "https://example.com"}]},
        {"id": "u", "type": "user_picker", "label": "Who", "field_key": "u"},
        {"id": "c", "type": "case_picker", "label": "Case", "field_key": "c"},
        {"id": "d", "type": "document_picker", "label": "Doc", "field_key": "d"},
    ])
    fields = portal_definition(form, for_browser=True)["sections"][0]["fields"]
    assert [f["type"] for f in fields] == ["address", "signature", "rich_text", "button"]
    assert fields[3]["actions"] == [{"action": "open_link", "url": "https://example.com"}]
    assert portal_keys(form) == {"a", "s", "r", "b"}
    clean, errors = validate(portal_definition(form), {"b": "pressed", "r": "hi", "u": "ana"})
    # the portal stores only its form's keys (the route filters by portal_keys)
    assert errors == [] and {k: v for k, v in clean.items() if k in portal_keys(form)} == {"r": "hi"}
