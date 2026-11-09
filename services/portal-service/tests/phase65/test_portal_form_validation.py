"""The portal form: what a customer is shown, and the check on submit
(Form Builder part 10, 2026-10-28).

Until now the portal served a flat field list (drawn as text boxes, dropdowns
included) and stored whatever `extra_data` arrived. Now it also serves the form
minus what an anonymous customer cannot use or must not see (lookups, connector
options, uploads; connector ids, variable names), and submit is checked against
that by the shared validator. No form attached: as before.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import pytest_asyncio

from velaris_shared.db.models import CaseInstanceModel, CaseTypeModel, FormDefinitionModel, TenantModel
from sqlalchemy import select

PORTAL = "/api/v1/portal"
PADMIN = "/api/v1/portal-admin"

DEF = {"settings": {"columns": 2, "mode": "steps", "submit_label": "Send"}, "sections": [
    {"id": "s1", "order": 0, "title": "About", "fields": [
        {"id": "f1", "field_key": "policy_no", "label": "Policy", "type": "text", "required": True,
         "validation": {"pattern": "[A-Z]{3}-[0-9]+", "pattern_message": "Like POL-123"}, "variable": "crm.policy"},
        {"id": "f2", "field_key": "kind", "label": "Kind", "type": "dropdown", "required": True,
         "options": [{"label": "Car", "value": "car"}, {"label": "Home", "value": "home"}]},
        {"id": "f3", "field_key": "plate", "label": "Plate", "type": "text", "required": True,
         "show_when": {"rules": [{"field": "kind", "op": "equals", "value": "car"}]}},
        {"id": "f4", "field_key": "channel", "label": "Channel", "type": "text", "hidden": True, "default_value": "portal"},
        {"id": "f5", "field_key": "lookup", "label": "Find", "type": "connector_lookup",
         "source": {"connector_id": "secret-connector-id", "input_mapping": {}}},
        {"id": "f6", "field_key": "branch", "label": "Branch", "type": "dropdown",
         "options_source": {"kind": "connector", "connector_id": "secret-connector-id"}},
        {"id": "f7", "field_key": "scan", "label": "Scan", "type": "file_upload"},
    ]},
    {"id": "s2", "order": 1, "title": "Only internal", "fields": [
        {"id": "g1", "field_key": "orders", "label": "Orders", "type": "table_display"}]},
]}


@pytest_asyncio.fixture
async def world(session, client):
    t = TenantModel(slug="acme", name="ACME", settings={"portal": {"enabled": True}})
    ct = CaseTypeModel(name="Claims", version="1.0", definition_json={"stages": []}, portal_enabled=True)
    form = FormDefinitionModel(name="Claim", version="1.0", definition_json=DEF)
    session.add_all([t, ct, form])
    await session.commit()
    r = await client.patch(f"{PADMIN}/case-types/{ct.id}/portal-display", json={"form_id": str(form.id)})
    assert r.status_code == 200, r.text
    return {"ct": ct}


def _submit(ct, extra):
    return {"case_type_id": str(ct.id), "submitter_name": "Jane", "submitter_email": "jane@example.com",
            "subject": "Claim", "description": "x", "extra_data": extra}


async def test_the_portal_gets_only_what_a_customer_can_fill_in(client, world):
    r = await client.get(f"{PORTAL}/acme/case-types/{world['ct'].id}/form")
    assert r.status_code == 200
    d = r.json()["definition"]
    keys = [f["field_key"] for s in d["sections"] for f in s["fields"]]
    assert keys == ["policy_no", "kind", "plate"]          # the hidden "channel" stays on the server
    assert d["settings"] == {"columns": 2, "submit_label": "Send"}       # no steps inside the portal's own form
    assert "secret-connector-id" not in r.text and "crm.policy" not in r.text.split('"definition"')[1]


async def test_submit_is_checked_against_the_form(client, world):
    r = await client.post(f"{PORTAL}/acme/submit", json=_submit(world["ct"], {"policy_no": "pol-1", "kind": "car"}))
    assert r.status_code == 422, r.text
    errors = {e["field_key"]: e["message"] for e in r.json()["detail"]["field_errors"]}
    assert errors == {"policy_no": "Like POL-123", "plate": "Plate is required"}


async def test_a_valid_submit_stores_the_checked_values(client, world, session):
    r = await client.post(f"{PORTAL}/acme/submit", json=_submit(world["ct"], {
        "policy_no": "POL-1", "kind": "home", "plate": "left over", "channel": "forged"}))
    assert r.status_code == 200, r.text
    case = (await session.execute(select(CaseInstanceModel).where(
        CaseInstanceModel.case_type_id == world["ct"].id))).scalar_one()
    assert case.data.get("policy_no") == "POL-1"
    assert case.data.get("channel") == "portal"         # hidden: the form's value, not the customer's
    assert "plate" not in case.data                     # not shown for a home claim, so not stored


async def test_hidden_fields_are_not_sent_to_the_browser(client, world):
    r = await client.get(f"{PORTAL}/acme/case-types/{world['ct'].id}/form")
    keys = [f["field_key"] for s in r.json()["definition"]["sections"] for f in s["fields"]]
    assert "channel" not in keys and '"portal"' not in r.text.split('"definition"')[1]
    # Nor in the older flat list, which listed every field (live probe 2026-10-28).
    flat = [f["key"] for f in r.json()["fields"]]
    assert "channel" not in flat and "lookup" not in flat and "orders" not in flat
    assert flat == keys


async def test_a_customer_cannot_add_keys_or_overwrite_platform_keys(client, world, session):
    """2026-10-28 audit: extra_data was spread AFTER source / tenant_slug /
    subject, and any key was stored."""
    r = await client.post(f"{PORTAL}/acme/submit", json=_submit(world["ct"], {
        "policy_no": "POL-2", "kind": "home",
        "source": "staff", "tenant_slug": "other", "subject": "forged", "approved": True}))
    assert r.status_code == 200, r.text
    case = (await session.execute(select(CaseInstanceModel).where(
        CaseInstanceModel.case_type_id == world["ct"].id))).scalar_one()
    assert case.data["source"] == "customer_portal"
    assert case.data["tenant_slug"] == "acme"
    assert case.data["subject"] == "Claim"
    assert "approved" not in case.data


async def test_formulas_run_on_the_server_from_the_forms_own_values(client, session):
    """Form Builder v2: a total is the server's, and a read-only input is its
    default — a customer sending a value for either changes nothing."""
    d = {"sections": [{"id": "s", "order": 0, "title": "S", "fields": [
        {"id": "q", "field_key": "qty", "label": "Qty", "type": "number", "required": True},
        {"id": "p", "field_key": "unit_price", "label": "Price", "type": "number", "read_only": True, "default_value": 40},
        {"id": "t", "field_key": "total", "label": "Total", "type": "number", "calculation": "qty * unit_price"},
        {"id": "r", "field_key": "reason", "label": "Reason", "type": "text", "required_when": {"expression": "total > 100"}},
    ]}]}
    t = TenantModel(slug="calc", name="Calc", settings={"portal": {"enabled": True}})
    ct = CaseTypeModel(name="Orders", version="1.0", definition_json={"stages": []}, portal_enabled=True)
    form = FormDefinitionModel(name="Order", version="1.0", definition_json=d)
    session.add_all([t, ct, form])
    await session.commit()
    r = await client.patch(f"{PADMIN}/case-types/{ct.id}/portal-display", json={"form_id": str(form.id)})
    assert r.status_code == 200, r.text
    r = await client.post(f"{PORTAL}/calc/submit", json=_submit(ct, {"qty": 3, "unit_price": 0.01, "total": 0.03}))
    assert r.status_code == 422 and "reason" in r.text           # 3 × 40 = 120 > 100: reason required
    r = await client.post(f"{PORTAL}/calc/submit", json=_submit(ct, {"qty": 2, "unit_price": 0.01, "total": 0.02}))
    assert r.status_code == 200, r.text
    case = (await session.execute(select(CaseInstanceModel).where(CaseInstanceModel.case_type_id == ct.id))).scalar_one()
    assert case.data["total"] == 80 and case.data["unit_price"] == 40


async def test_a_repeating_group_on_the_portal(client, session):
    """Rows reach the customer without hidden or internal columns, and every row is checked."""
    d = {"sections": [{"id": "s", "order": 0, "title": "S", "fields": [
        {"id": "g", "field_key": "vehicles", "label": "Vehicles", "type": "repeating_group", "required": True, "max_rows": 3,
         "fields": [
            {"id": "p", "field_key": "plate", "label": "Plate", "type": "text", "required": True},
            {"id": "v", "field_key": "value", "label": "Value", "type": "number"},
            {"id": "r", "field_key": "risk", "label": "Risk", "type": "number", "hidden": True, "calculation": "value / 1000"},
            {"id": "l", "field_key": "check", "label": "Check", "type": "connector_lookup", "source": {"connector_id": "secret"}},
         ]},
    ]}]}
    t = TenantModel(slug="grp", name="Grp", settings={"portal": {"enabled": True}})
    ct = CaseTypeModel(name="Fleet", version="1.0", definition_json={"stages": []}, portal_enabled=True)
    form = FormDefinitionModel(name="Fleet", version="1.0", definition_json=d)
    session.add_all([t, ct, form])
    await session.commit()
    assert (await client.patch(f"{PADMIN}/case-types/{ct.id}/portal-display", json={"form_id": str(form.id)})).status_code == 200
    got = await client.get(f"{PORTAL}/grp/case-types/{ct.id}/form")
    cols = [c["field_key"] for c in got.json()["definition"]["sections"][0]["fields"][0]["fields"]]
    assert cols == ["plate", "value"] and "secret" not in got.text and "1000" not in got.text
    r = await client.post(f"{PORTAL}/grp/submit", json=_submit(ct, {"vehicles": [{"plate": "B-1", "value": 5000}, {"value": 1}]}))
    assert r.status_code == 422 and "vehicles[1].plate" in r.text
    r = await client.post(f"{PORTAL}/grp/submit", json=_submit(ct, {"vehicles": [{"plate": "B-1", "value": 5000, "risk": 0, "x": 1}] * 4}))
    assert r.status_code == 422 and "At most 3 rows" in r.text
    r = await client.post(f"{PORTAL}/grp/submit", json=_submit(ct, {"vehicles": [{"plate": "B-1", "value": 5000, "risk": 0, "x": 1}]}))
    assert r.status_code == 200, r.text
    case = (await session.execute(select(CaseInstanceModel).where(CaseInstanceModel.case_type_id == ct.id))).scalar_one()
    assert case.data["vehicles"] == [{"plate": "B-1", "value": 5000, "risk": 5}]


async def test_reference_options_and_checks_on_the_portal(client, session):
    """A customer reads a reference field's options only through the portal form,
    never the table's id; on submit the choice is checked and a read-only field
    filled from the row."""
    import uuid as _uuid
    from velaris_shared.db.models import ReferenceRowModel, ReferenceTableModel
    t = TenantModel(slug="refs", name="Refs", settings={"portal": {"enabled": True}})
    other = TenantModel(slug="refs-other", name="Other")
    session.add_all([t, other])
    await session.flush()
    table = ReferenceTableModel(id=_uuid.uuid4(), tenant_id=t.id, tenant_key=str(t.id), name="Branches", columns=[])
    secret = ReferenceTableModel(id=_uuid.uuid4(), tenant_id=other.id, tenant_key=str(other.id), name="S", columns=[])
    session.add_all([table, secret])
    await session.flush()
    for i, r in enumerate([{"code": "BER", "name": "Berlin", "region": "east", "fee": 10},
                           {"code": "MUC", "name": "Munich", "region": "south", "fee": 12}]):
        session.add(ReferenceRowModel(table_id=table.id, sort_order=i, data=r))
    session.add(ReferenceRowModel(table_id=secret.id, sort_order=0, data={"code": "TOP-SECRET", "name": "x", "region": "east"}))
    src = {"kind": "reference", "table_id": str(table.id), "value_column": "code", "label_column": "name",
           "filters": [{"column": "region", "field": "region"}], "fill": [{"column": "fee", "field": "fee"}]}
    d = {"sections": [{"id": "s", "order": 0, "title": "S", "fields": [
        {"id": "r", "field_key": "region", "label": "Region", "type": "text"},
        {"id": "b", "field_key": "branch", "label": "Branch", "type": "dropdown", "required": True, "options_source": src},
        {"id": "s2", "field_key": "sneaky", "label": "Sneaky", "type": "dropdown", "options_source": {**src, "table_id": str(secret.id)}},
        {"id": "f", "field_key": "fee", "label": "Fee", "type": "number", "read_only": True},
    ]}]}
    ct = CaseTypeModel(name="Visits", version="1.0", definition_json={"stages": []}, portal_enabled=True)
    form = FormDefinitionModel(name="Visit", version="1.0", definition_json=d)
    session.add_all([ct, form])
    await session.commit()
    assert (await client.patch(f"{PADMIN}/case-types/{ct.id}/portal-display", json={"form_id": str(form.id)})).status_code == 200
    got = await client.get(f"{PORTAL}/refs/case-types/{ct.id}/form")
    assert str(table.id) not in got.text and str(secret.id) not in got.text
    url = f"{PORTAL}/refs/case-types/{ct.id}/form/options"
    r = await client.post(url, json={"field_key": "branch", "values": {"region": "east"}})
    assert r.status_code == 200 and r.json()["options"] == [{"label": "Berlin", "value": "BER", "row": {"fee": 10}}]
    r = await client.post(url, json={"field_key": "sneaky", "values": {"region": "east"}})
    assert r.json()["options"] == [] and "SECRET" not in r.text            # another tenant's table: nothing
    assert (await client.post(url, json={"field_key": "region"})).status_code == 404
    assert (await client.post(url, json={"field_key": "nope"})).status_code == 404
    r = await client.post(f"{PORTAL}/refs/submit", json=_submit(ct, {"region": "east", "branch": "MUC"}))
    assert r.status_code == 422 and "branch" in r.text
    r = await client.post(f"{PORTAL}/refs/submit", json=_submit(ct, {"region": "east", "branch": "BER", "fee": 0}))
    assert r.status_code == 200, r.text
    case = (await session.execute(select(CaseInstanceModel).where(CaseInstanceModel.case_type_id == ct.id))).scalar_one()
    assert case.data["branch"] == "BER" and case.data["fee"] == 10
