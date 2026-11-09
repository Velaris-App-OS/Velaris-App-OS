"""Reusable form sections and form versions per case (Form Builder §5 step 6, 2026-10-29).

A designer builds a section once and links it from forms; the server copies
its fields into each form, and a change reaches every linking form together —
or none. A form versioned per case is pinned for a case the first time the case
uses it. Tested as the least-privileged callers who should pass and callers who
must be refused, across tenants.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from velaris_shared.db.models import (
    CaseInstanceModel, CaseTypeModel, FormDefinitionModel, FormSectionModel, TenantModel, UserDirectoryModel,
)
from velaris_shared.forms.versions import form_for_case

from case_service.main import app
from case_service.tenancy import identity as tenancy
from tests.conftest import LIFECYCLE_PROCESS_ID, TestSessionFactory  # type: ignore[attr-defined]
from tests.conftest import _headers  # type: ignore[attr-defined]

DESIGNER = _headers("dora", ["designer"])        # tenant "acme"
WORKER = _headers("wendy", ["case_worker"])      # tenant "acme"
OTHER = _headers("otto", ["designer"])           # tenant "other"
SECTIONS = "/api/v1/form-sections"
FORMS = "/api/v1/forms"

ADDRESS = {"title": "Address", "fields": [
    {"id": "street", "type": "text", "label": "Street", "field_key": "street", "required": True},
    {"id": "town", "type": "text", "label": "Town", "field_key": "town"}]}


def _form(*sections) -> dict:
    return {"sections": [{"id": "own", "title": "Own", "order": 0, "fields": [
        {"id": "name", "type": "text", "label": "Name", "field_key": "name"}]}, *sections]}


def _link(section_id, sid="s2", fields=None) -> dict:
    return {"id": sid, "title": "", "order": 1, "section_ref": {"id": str(section_id)}, "fields": fields or []}


@pytest_asyncio.fixture
async def api():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def world():
    tenancy.clear_cache()
    async with TestSessionFactory() as db:
        acme = TenantModel(id=uuid.uuid4(), slug="acme", name="Acme")
        db.add_all([TenantModel(id=uuid.uuid4(), slug="default", name="Default"), acme,
                    TenantModel(id=uuid.uuid4(), slug="other", name="Other")])
        await db.flush()
        for uid, t in (("dora", "acme"), ("wendy", "acme"), ("otto", "other")):
            db.add(UserDirectoryModel(id=uuid.uuid4(), user_id=uid, tenant_id=t))
        ct = CaseTypeModel(id=uuid.uuid4(), name="Ver CT", version="1.0.0",
                           lifecycle_process_id=LIFECYCLE_PROCESS_ID, definition_json={})
        db.add(ct)
        await db.flush()
        mk = lambda n: CaseInstanceModel(id=uuid.uuid4(), case_number=n, case_type_id=ct.id, case_type_version="1.0.0",
                                         status="new", priority="medium", created_by="x", data={}, tenant_id=acme.id)
        c1, c2 = mk("VER-1"), mk("VER-2")
        db.add_all([c1, c2])
        await db.commit()
    yield SimpleNamespace(acme=acme, c1=c1, c2=c2)
    tenancy.clear_cache()


async def _section(api, who=DESIGNER, name="Address", body=ADDRESS):
    r = await api.post(SECTIONS, headers=who, json={"name": name, "section": body})
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ── reusable sections ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_designer_builds_sections_and_a_case_worker_cannot(api, world):
    assert (await api.post(SECTIONS, headers=WORKER, json={"name": "X", "section": ADDRESS})).status_code == 403
    assert (await api.get(SECTIONS, headers=WORKER)).status_code == 403
    sid = await _section(api)
    assert [s["name"] for s in (await api.get(SECTIONS, headers=DESIGNER)).json()["items"]] == ["Address"]
    # another tenant neither lists, reads, changes nor deletes it
    assert (await api.get(SECTIONS, headers=OTHER)).json()["items"] == []
    for call in (api.get(f"{SECTIONS}/{sid}", headers=OTHER),
                 api.put(f"{SECTIONS}/{sid}", headers=OTHER, json={"name": "Address", "section": ADDRESS}),
                 api.delete(f"{SECTIONS}/{sid}", headers=OTHER)):
        assert (await call).status_code == 404


@pytest.mark.asyncio
async def test_a_form_gets_the_sections_fields_and_cannot_change_them(api, world):
    sid = await _section(api)
    tampered = [{"id": "street", "type": "text", "label": "Street", "field_key": "street", "required": False},
                {"id": "evil", "type": "text", "label": "Evil", "field_key": "approved"}]
    r = await api.post(FORMS, headers=DESIGNER, json={"name": "Claim", "version": "1",
                                                      "definition_json": _form(_link(sid, fields=tampered))})
    assert r.status_code == 201, r.text
    linked = r.json()["definition_json"]["sections"][1]
    assert [f["field_key"] for f in linked["fields"]] == ["street", "town"]
    assert linked["fields"][0]["required"] is True and linked["section_ref"]["version"] == 1
    assert linked["title"] == "Address"


@pytest.mark.asyncio
async def test_links_that_cross_a_tenant_or_repeat_are_refused(api, world):
    theirs = await _section(api, who=OTHER, name="Theirs")
    r = await api.post(FORMS, headers=DESIGNER, json={"name": "A", "version": "1", "definition_json": _form(_link(theirs))})
    assert r.status_code == 422
    sid = await _section(api)
    r = await api.post(FORMS, headers=DESIGNER, json={"name": "B", "version": "1",
                                                      "definition_json": _form(_link(sid), _link(sid, "s3"))})
    assert r.status_code == 422 and "used twice" in r.text
    r = await api.post(FORMS, headers=DESIGNER, json={"name": "C", "version": "1",
                                                      "definition_json": _form(_link("not-a-uuid"))})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_a_platform_form_links_only_platform_sections(api, world):
    sid = await _section(api)
    op = _headers("opal", ["designer"])              # no directory row: the default tenant = the platform
    r = await api.post(FORMS, headers=op, json={"name": "Default", "version": "1", "definition_json": _form(_link(sid))})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_a_change_reaches_every_linking_form_or_none(api, world):
    sid = await _section(api)
    a = (await api.post(FORMS, headers=DESIGNER, json={"name": "A", "version": "1", "definition_json": _form(_link(sid))})).json()
    b_def = _form(_link(sid))
    b_def["sections"][0]["fields"].append({"id": "postcode", "type": "text", "label": "Postcode", "field_key": "postcode"})
    b = (await api.post(FORMS, headers=DESIGNER, json={"name": "B", "version": "1", "definition_json": b_def})).json()

    more = {"title": "Address", "fields": ADDRESS["fields"] + [{"id": "country", "type": "country", "label": "Country", "field_key": "country"}]}
    r = await api.put(f"{SECTIONS}/{sid}", headers=DESIGNER, json={"name": "Address", "section": more})
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 2 and r.json()["updated_forms"] == ["A", "B"]
    got = (await api.get(f"{FORMS}/{a['id']}", headers=DESIGNER)).json()["definition_json"]["sections"][1]
    assert [f["field_key"] for f in got["fields"]] == ["street", "town", "country"] and got["section_ref"]["version"] == 2

    # a change that clashes with form B's own "postcode" stops everything
    clash = {"title": "Address", "fields": more["fields"] + [{"id": "pc", "type": "text", "label": "PC", "field_key": "postcode"}]}
    r = await api.put(f"{SECTIONS}/{sid}", headers=DESIGNER, json={"name": "Address", "section": clash})
    assert r.status_code == 409 and "“B”" in r.text
    assert (await api.get(f"{SECTIONS}/{sid}", headers=DESIGNER)).json()["version"] == 2
    got = (await api.get(f"{FORMS}/{a['id']}", headers=DESIGNER)).json()["definition_json"]["sections"][1]
    assert len(got["fields"]) == 3                                          # A unchanged too


@pytest.mark.asyncio
async def test_a_linked_section_cannot_be_deleted(api, world):
    sid = await _section(api)
    f = (await api.post(FORMS, headers=DESIGNER, json={"name": "A", "version": "1", "definition_json": _form(_link(sid))})).json()
    r = await api.delete(f"{SECTIONS}/{sid}", headers=DESIGNER)
    assert r.status_code == 409 and "A" in r.text
    await api.patch(f"{FORMS}/{f['id']}", headers=DESIGNER, json={"definition_json": _form()})
    assert (await api.delete(f"{SECTIONS}/{sid}", headers=DESIGNER)).status_code == 204


@pytest.mark.asyncio
async def test_a_form_without_links_saves_exactly_as_before(api, world):
    dup = _form({"id": "s2", "title": "Dup", "order": 1, "fields": [{"id": "x", "type": "text", "label": "Name", "field_key": "name"}]})
    r = await api.post(FORMS, headers=DESIGNER, json={"name": "Old", "version": "1", "definition_json": dup})
    assert r.status_code == 201                         # duplicate keys were saved before; still are


# ── form versions per case ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_without_the_setting_a_case_reads_the_current_form(api, world):
    async with TestSessionFactory() as db:
        f = FormDefinitionModel(id=uuid.uuid4(), name="Plain", version="1", tenant_id=world.acme.id, definition_json=_form())
        db.add(f)
        await db.commit()
        assert await form_for_case(db, f, world.c1.id) == _form()
        f.definition_json = {"sections": []}
        await db.commit()
        assert await form_for_case(db, f, world.c1.id) == {"sections": []}


@pytest.mark.asyncio
async def test_a_case_keeps_the_version_it_first_used(api, world):
    v1 = {**_form(), "settings": {"versioning": "per_case"}}
    async with TestSessionFactory() as db:
        f = FormDefinitionModel(id=uuid.uuid4(), name="Pinned", version="1", tenant_id=world.acme.id, definition_json=v1)
        db.add(f)
        await db.commit()
        fid = f.id
        assert await form_for_case(db, f, world.c1.id) == v1                # pinned now
        await db.commit()
    v2 = {**v1, "sections": v1["sections"] + [{"id": "new", "title": "New", "order": 1, "fields": []}]}
    async with TestSessionFactory() as db:
        f = await db.get(FormDefinitionModel, fid)
        f.definition_json = v2
        await db.commit()
        assert await form_for_case(db, f, world.c1.id) == v1                # the old case keeps v1
        assert await form_for_case(db, f, world.c2.id) == v2                # a new case starts on v2
        await db.commit()
    # Case Manager reads the same, through the case
    r = await api.get(f"/api/v1/form-submissions/cases/{world.c1.id}/forms/{fid}", headers=WORKER)
    assert r.status_code == 200 and r.json()["definition_json"] == v1
    # another tenant's caller does not reach the case
    assert (await api.get(f"/api/v1/form-submissions/cases/{world.c1.id}/forms/{fid}", headers=OTHER)).status_code == 404
    # deleting the form leaves the pinned copy readable by the case's code paths
    async with TestSessionFactory() as db:
        from velaris_shared.db.models import CaseFormVersionModel, FormSnapshotModel
        pin = await db.get(CaseFormVersionModel, (world.c1.id, fid))
        assert (await db.get(FormSnapshotModel, pin.snapshot_id)).definition_json == v1
