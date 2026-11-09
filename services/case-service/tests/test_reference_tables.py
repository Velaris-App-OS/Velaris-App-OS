"""Reference tables (Form Builder v2 step 3, 2026-10-29).

Admin-managed lists a form reads. Designers manage them; at run time a form
reads one only through a saved field that names it; on submit the server
checks every chosen value against the table (cascading filters applied) and
fills read-only fields from the chosen row. Tested as the least-privileged
users who should pass, and as users who must be refused.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from velaris_shared.db.models import (
    FormDefinitionModel, ReferenceRowModel, ReferenceTableModel, TenantModel, UserDirectoryModel,
)
from velaris_shared.forms import validate

from case_service.main import app
from case_service.tenancy import identity as tenancy
from tests.conftest import TestSessionFactory  # type: ignore[attr-defined]
from tests.conftest import _headers  # type: ignore[attr-defined]

# An ordinary tenant: a default-tenant account is the platform operator.
DESIGNER = _headers("dora", ["designer"])        # tenant "acme"
WORKER = _headers("wendy", ["case_worker"])      # tenant "acme"
BASE = "/api/v1/reference-tables"

BRANCHES = [{"code": "BER", "name": "Berlin", "region": "east", "fee": 10},
            {"code": "DRS", "name": "Dresden", "region": "east", "fee": 8},
            {"code": "MUC", "name": "Munich", "region": "south", "fee": 12}]


@pytest_asyncio.fixture
async def api():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _form(table_id, tenant_id):
    src = {"kind": "reference", "table_id": str(table_id), "value_column": "code", "label_column": "name",
           "filters": [{"column": "region", "field": "region"}], "fill": [{"column": "fee", "field": "fee"}]}
    return {"sections": [{"id": "s", "fields": [
        {"id": "r", "type": "dropdown", "label": "Region", "field_key": "region",
         "options": [{"label": "East", "value": "east"}, {"label": "South", "value": "south"}]},
        {"id": "b", "type": "dropdown", "label": "Branch", "field_key": "branch", "required": True, "options_source": src},
        {"id": "f", "type": "number", "label": "Fee", "field_key": "fee", "read_only": True},
        {"id": "t", "type": "number", "label": "Twice", "field_key": "twice", "calculation": "fee * 2"},
        {"id": "plain", "type": "dropdown", "label": "Plain", "field_key": "plain", "options": [{"label": "A", "value": "a"}]},
        {"id": "g", "type": "repeating_group", "label": "Visits", "field_key": "visits", "fields": [
            {"id": "gb", "type": "dropdown", "label": "Branch", "field_key": "branch",
             "options_source": {**src, "filters": [{"column": "region", "field": "region"}]}}]},
    ]}]}


@pytest_asyncio.fixture
async def world():
    tenancy.clear_cache()
    async with TestSessionFactory() as db:
        base = TenantModel(id=uuid.uuid4(), slug="default", name="Default")
        d = TenantModel(id=uuid.uuid4(), slug="acme", name="Acme")
        o = TenantModel(id=uuid.uuid4(), slug="other", name="Other")
        db.add_all([base, d, o])
        await db.flush()
        for uid, t in (("dora", "acme"), ("wendy", "acme")):
            db.add(UserDirectoryModel(id=uuid.uuid4(), user_id=uid, tenant_id=t))
        table = ReferenceTableModel(id=uuid.uuid4(), tenant_id=d.id, tenant_key=str(d.id), name="Branches",
                                    columns=[{"key": k, "label": k, "type": "number" if k == "fee" else "text"}
                                             for k in ("code", "name", "region", "fee")], row_count=3)
        theirs = ReferenceTableModel(id=uuid.uuid4(), tenant_id=o.id, tenant_key=str(o.id), name="Secret",
                                     columns=[{"key": "code", "label": "code", "type": "text"}], row_count=1)
        shared = ReferenceTableModel(id=uuid.uuid4(), tenant_id=None, tenant_key="platform", name="Countries",
                                     columns=[{"key": "code", "label": "code", "type": "text"}], row_count=0)
        db.add_all([table, theirs, shared])
        await db.flush()
        db.add_all([ReferenceRowModel(table_id=table.id, sort_order=i, data=r) for i, r in enumerate(BRANCHES)])
        db.add(ReferenceRowModel(table_id=theirs.id, sort_order=0, data={"code": "TOP-SECRET"}))
        form = FormDefinitionModel(id=uuid.uuid4(), name="Visit", version="1", tenant_id=d.id,
                                   definition_json=_form(table.id, d.id))
        their_form = FormDefinitionModel(id=uuid.uuid4(), name="Theirs", version="1", tenant_id=o.id,
                                         definition_json=_form(theirs.id, o.id))
        stealer = FormDefinitionModel(id=uuid.uuid4(), name="Stealer", version="1", tenant_id=d.id,
                                      definition_json=_form(theirs.id, d.id))
        db.add_all([form, their_form, stealer])
        await db.commit()
    yield {"shared": shared, "table": table, "theirs": theirs, "form": form, "their_form": their_form, "stealer": stealer, "d": d}
    tenancy.clear_cache()


# ── design ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_designer_manages_a_table_and_a_case_worker_cannot(api, world):
    r = await api.post(BASE, headers=DESIGNER, json={"name": "Products", "columns": [
        {"key": "sku", "label": "SKU"}, {"key": "price", "label": "Price", "type": "number"}]})
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    r = await api.put(f"{BASE}/{tid}/rows", headers=DESIGNER, json={"rows": [
        {"data": {"sku": "A-1", "price": "9.5"}}, {"data": {"sku": "B-2", "price": 3}, "is_active": False}]})
    assert r.status_code == 200 and r.json()["row_count"] == 2
    got = (await api.get(f"{BASE}/{tid}", headers=DESIGNER)).json()
    assert got["rows"] == [{"data": {"sku": "A-1", "price": 9.5}, "is_active": True},
                           {"data": {"sku": "B-2", "price": 3}, "is_active": False}]
    assert [t["name"] for t in (await api.get(BASE, headers=DESIGNER)).json()["items"]] == ["Branches", "Countries", "Products"]
    for call in (api.get(BASE, headers=WORKER), api.post(BASE, headers=WORKER, json={"name": "X"}),
                 api.put(f"{BASE}/{tid}/rows", headers=WORKER, json={"rows": []}),
                 api.delete(f"{BASE}/{tid}", headers=WORKER)):
        assert (await call).status_code == 403
    assert (await api.delete(f"{BASE}/{tid}", headers=DESIGNER)).status_code == 204


@pytest.mark.asyncio
async def test_another_tenants_table_is_invisible(api, world):
    tid = world["theirs"].id
    assert "Secret" not in str((await api.get(BASE, headers=DESIGNER)).json())
    for call in (api.get(f"{BASE}/{tid}", headers=DESIGNER), api.patch(f"{BASE}/{tid}", headers=DESIGNER, json={"name": "x"}),
                 api.put(f"{BASE}/{tid}/rows", headers=DESIGNER, json={"rows": []}),
                 api.delete(f"{BASE}/{tid}", headers=DESIGNER)):
        assert (await call).status_code == 404


@pytest.mark.asyncio
async def test_a_platform_table_is_read_by_a_tenant_but_changed_only_by_the_operator(api, world):
    tid = world["shared"].id
    assert (await api.get(f"{BASE}/{tid}", headers=DESIGNER)).status_code == 200
    for call in (api.patch(f"{BASE}/{tid}", headers=DESIGNER, json={"name": "x"}),
                 api.put(f"{BASE}/{tid}/rows", headers=DESIGNER, json={"rows": []}),
                 api.delete(f"{BASE}/{tid}", headers=DESIGNER)):
        assert (await call).status_code in (403, 404)


@pytest.mark.asyncio
async def test_bad_tables_and_rows_are_refused(api, world):
    for body in ({"name": "X", "columns": [{"key": "Bad Key"}]}, {"name": "X", "columns": [{"key": "a"}, {"key": "a"}]},
                 {"name": "X", "columns": [{"key": f"c{i}"} for i in range(21)]},
                 {"name": "X", "columns": [{"key": "a", "type": "blob"}]}, {"name": "Branches"}):
        assert (await api.post(BASE, headers=DESIGNER, json=body)).status_code in (409, 422), body
    tid = world["table"].id
    for rows in ([{"data": {"nope": 1}}], [{"data": {"fee": "ten"}}], [{"data": {"name": "x" * 1001}}],
                 [{"data": {"code": "A"}}] * 10001):
        assert (await api.put(f"{BASE}/{tid}/rows", headers=DESIGNER, json={"rows": rows})).status_code == 422


@pytest.mark.asyncio
async def test_a_table_used_by_a_form_cannot_be_deleted(api, world):
    r = await api.delete(f"{BASE}/{world['table'].id}", headers=DESIGNER)
    assert r.status_code == 409 and "Visit" in r.text


# ── run time ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_options_come_only_through_a_saved_field_and_follow_their_parent(api, world):
    body = {"form_id": str(world["form"].id), "field_key": "branch", "values": {"region": "east"}}
    r = await api.post(f"{BASE}/options", headers=WORKER, json=body)
    assert r.status_code == 200, r.text
    assert r.json()["options"] == [{"label": "Berlin", "value": "BER", "row": {"fee": 10}},
                                   {"label": "Dresden", "value": "DRS", "row": {"fee": 8}}]
    r = await api.post(f"{BASE}/options", headers=WORKER, json={**body, "q": "dres"})
    assert [o["value"] for o in r.json()["options"]] == ["DRS"]
    r = await api.post(f"{BASE}/options", headers=WORKER, json={**body, "values": {}})
    assert r.json()["options"] == []                                   # parent empty: nothing yet
    r = await api.post(f"{BASE}/options", headers=WORKER, json={**body, "group_key": "visits"})
    assert [o["value"] for o in r.json()["options"]] == ["BER", "DRS"]
    assert (await api.post(f"{BASE}/options", headers=WORKER, json={**body, "field_key": "plain"})).status_code == 403
    assert (await api.post(f"{BASE}/options", headers=WORKER, json={**body, "form_id": str(world["their_form"].id)})).status_code == 404
    # A form of mine naming another tenant's table reads nothing from it.
    r = await api.post(f"{BASE}/options", headers=WORKER, json={**body, "form_id": str(world["stealer"].id)})
    assert r.json()["options"] == [] and "SECRET" not in r.text


# ── the server's check on submit ─────────────────────────────────────────────

def test_a_chosen_value_must_be_in_the_table_and_its_parents_rows():
    refs = {"t": BRANCHES}
    d = _form("t", None)
    clean, errors = validate(d, {"region": "east", "branch": "BER", "fee": 0, "twice": 1}, references=refs)
    assert errors == [] and clean["fee"] == 10 and clean["twice"] == 20      # read-only filled from the row
    _, errors = validate(d, {"region": "east", "branch": "MUC"}, references=refs)
    assert [e.as_dict() for e in errors] == [{"field_key": "branch", "message": "Must be one of the options"}]
    _, errors = validate(d, {"region": "east", "branch": "FORGED"}, references=refs)
    assert errors and errors[0].field_key == "branch"
    _, errors = validate(d, {"region": "south", "branch": "MUC", "visits": [{"branch": "BER"}, {"branch": "MUC"}]},
                         references=refs)
    assert [e.field_key for e in errors] == ["visits[0].branch"]
    _, errors = validate(d, {"region": "east", "branch": "BER"}, references={"t": []})   # table gone or unreadable
    assert errors and errors[0].field_key == "branch"


@pytest.mark.asyncio
async def test_load_rows_never_reads_another_tenants_table(world):
    from velaris_shared.forms.reference import load_rows
    async with TestSessionFactory() as db:
        mine = await load_rows(db, world["stealer"].definition_json, world["d"].id)
        assert mine == {str(world["theirs"].id): []}
        ok = await load_rows(db, world["form"].definition_json, world["d"].id)
        assert ok[str(world["table"].id)] == BRANCHES


# ── audit 2026-10-29 ─────────────────────────────────────────────────────────

def test_a_reference_field_with_no_table_yet_accepts_nothing():
    d = {"sections": [{"id": "s", "fields": [{"id": "b", "type": "dropdown", "label": "Branch", "field_key": "branch",
                                              "options_source": {"kind": "reference"}}]}]}
    _, errors = validate(d, {"branch": "anything"}, references={})
    assert [e.field_key for e in errors] == ["branch"]


@pytest.mark.asyncio
async def test_a_column_a_form_uses_cannot_be_removed(api, world):
    tid = world["table"].id
    cols = [{"key": k, "label": k} for k in ("code", "name", "region", "fee")]
    r = await api.patch(f"{BASE}/{tid}", headers=DESIGNER, json={"columns": [c for c in cols if c["key"] != "fee"]})
    assert r.status_code == 409 and "fee" in r.text and "Visit" in r.text            # a fill column
    r = await api.patch(f"{BASE}/{tid}", headers=DESIGNER, json={"columns": cols + [{"key": "extra", "label": "Extra"}]})
    assert r.status_code == 200
    r = await api.patch(f"{BASE}/{tid}", headers=DESIGNER, json={"columns": cols})   # an unused one may go
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_a_case_with_no_tenant_reads_the_callers_tables(world):
    from case_service.api.routers.form_submissions import reference_tenant
    from velaris_shared.auth.models import AuthenticatedUser

    class Case:
        tenant_id = None
    async with TestSessionFactory() as db:
        got = await reference_tenant(db, AuthenticatedUser(user_id="wendy", roles=["case_worker"], tenant_id="acme"), Case())
    assert got == world["d"].id
