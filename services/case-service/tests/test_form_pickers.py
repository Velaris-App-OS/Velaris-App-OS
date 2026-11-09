"""User, case and document pickers (Form Builder batch M, 2026-10-29).

A picker stores an id; the server accepts only ids the caller may choose: a
person of the caller's tenant, a case of the caller's tenant, a document of the
case being worked. The search route answers only for a saved form's picker.
Tested as the least-privileged caller who should pass and callers who must be
refused, across tenants.

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
    CaseInstanceModel, CaseTypeModel, DocumentModel, FormDefinitionModel, TenantModel, UserDirectoryModel,
)
from velaris_shared.forms import validate
from velaris_shared.forms.pickers import picker_lookups

from case_service.main import app
from case_service.tenancy import identity as tenancy
from tests.conftest import LIFECYCLE_PROCESS_ID, TestSessionFactory  # type: ignore[attr-defined]
from tests.conftest import _headers  # type: ignore[attr-defined]

WORKER = _headers("wendy", ["case_worker"])      # tenant "acme"
OTHER = _headers("otto", ["case_worker"])        # tenant "other"
OPTS = "/api/v1/forms/picker-options"


def _form() -> dict:
    return {"sections": [{"id": "s", "title": "S", "order": 0, "fields": [
        {"id": "u", "type": "user_picker", "label": "Approver", "field_key": "approver"},
        {"id": "c", "type": "case_picker", "label": "Related case", "field_key": "related"},
        {"id": "d", "type": "document_picker", "label": "Evidence", "field_key": "evidence"},
        {"id": "t", "type": "text", "label": "Note", "field_key": "note"},
        {"id": "g", "type": "repeating_group", "label": "Reviewers", "field_key": "reviewers", "fields": [
            {"id": "gu", "type": "user_picker", "label": "Reviewer", "field_key": "who"}]},
    ]}]}


@pytest_asyncio.fixture
async def api():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def world():
    tenancy.clear_cache()
    async with TestSessionFactory() as db:
        base = TenantModel(id=uuid.uuid4(), slug="default", name="Default")
        acme = TenantModel(id=uuid.uuid4(), slug="acme", name="Acme")
        other = TenantModel(id=uuid.uuid4(), slug="other", name="Other")
        db.add_all([base, acme, other])
        await db.flush()
        db.add_all([
            UserDirectoryModel(id=uuid.uuid4(), user_id="wendy", tenant_id="acme", display_name="Wendy Case"),
            UserDirectoryModel(id=uuid.uuid4(), user_id="ana", tenant_id="acme", display_name="Ana Approver"),
            UserDirectoryModel(id=uuid.uuid4(), user_id="gone", tenant_id="acme", display_name="Ana Gone", is_active=False),
            UserDirectoryModel(id=uuid.uuid4(), user_id="otto", tenant_id="other", display_name="Ana Other"),
            # a row from before multi-tenancy: the default tenant's
            UserDirectoryModel(id=uuid.uuid4(), user_id="olda", tenant_id=None, display_name="Ana Legacy"),
        ])
        ct = CaseTypeModel(id=uuid.uuid4(), name="Picker CT", version="1.0.0",
                           lifecycle_process_id=LIFECYCLE_PROCESS_ID, definition_json={})
        db.add(ct)
        await db.flush()
        mk = lambda n, t, **kw: CaseInstanceModel(id=uuid.uuid4(), case_number=n, case_type_id=ct.id,
                                                  case_type_version="1.0.0", status="new", priority="medium",
                                                  created_by="x", data={}, tenant_id=t.id, **kw)
        here, related, theirs, deleted = mk("PK-1", acme), mk("PK-2", acme), mk("PK-9", other), mk("PK-3", acme, is_deleted=True)
        db.add_all([here, related, theirs, deleted])
        await db.flush()
        doc = DocumentModel(id=uuid.uuid4(), case_id=here.id, filename="photo.jpg", tenant_id="acme")
        elsewhere = DocumentModel(id=uuid.uuid4(), case_id=related.id, filename="other.pdf", tenant_id="acme")
        db.add_all([doc, elsewhere])
        form = FormDefinitionModel(id=uuid.uuid4(), name="Pickers", version="1", tenant_id=acme.id, definition_json=_form())
        their_form = FormDefinitionModel(id=uuid.uuid4(), name="Theirs", version="1", tenant_id=other.id, definition_json=_form())
        db.add_all([form, their_form])
        await db.commit()
    yield SimpleNamespace(here=here, related=related, theirs=theirs, deleted=deleted, doc=doc,
                          elsewhere=elsewhere, form=form, their_form=their_form)
    tenancy.clear_cache()


def _user(uid: str, tenant: str):
    return SimpleNamespace(user_id=uid, username=uid, tenant_id=tenant, roles=["case_worker"], is_superadmin=False)


# ── what submit accepts ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_submit_accepts_only_ids_the_caller_may_choose(world):
    w = world
    values = {"approver": "ana", "related": str(w.related.id), "evidence": str(w.doc.id),
              "reviewers": [{"who": "ana"}, {"who": "otto"}]}
    async with TestSessionFactory() as db:
        lk = await picker_lookups(db, _form(), values, user=_user("wendy", "acme"), case_id=w.here.id)
    _, errors = validate(_form(), values, lookups=lk)
    assert [(e.field_key, e.message) for e in errors] == [
        ("reviewers[1].who", "Must be a person you can choose")]            # otto is another tenant's

    bad = {"approver": "otto", "related": str(w.theirs.id), "evidence": str(w.elsewhere.id)}
    async with TestSessionFactory() as db:
        lk = await picker_lookups(db, _form(), bad, user=_user("wendy", "acme"), case_id=w.here.id)
    _, errors = validate(_form(), bad, lookups=lk)
    assert {e.field_key: e.message for e in errors} == {
        "approver": "Must be a person you can choose",
        "related": "Must be a case you can choose",
        "evidence": "Must be a document of this case you can choose",
    }


@pytest.mark.asyncio
async def test_inactive_people_deleted_cases_and_made_up_ids_are_refused(world):
    w = world
    values = {"approver": "gone", "related": str(w.deleted.id), "evidence": "not-a-uuid"}
    async with TestSessionFactory() as db:
        lk = await picker_lookups(db, _form(), values, user=_user("wendy", "acme"), case_id=w.here.id)
    _, errors = validate(_form(), values, lookups=lk)
    assert len(errors) == 3


# ── what the search offers ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_people_search_stays_in_the_callers_tenant(api, world):
    r = await api.post(OPTS, headers=WORKER, json={"form_id": str(world.form.id), "field_key": "approver", "q": "ana"})
    assert r.status_code == 200, r.text
    assert [o["value"] for o in r.json()["options"]] == ["ana"]           # not inactive, not other tenant
    r = await api.post(OPTS, headers=WORKER, json={"form_id": str(world.form.id), "field_key": "who",
                                                   "group_key": "reviewers", "q": "ana"})
    assert [o["value"] for o in r.json()["options"]] == ["ana"]


@pytest.mark.asyncio
async def test_case_search_lists_only_the_tenants_live_cases(api, world):
    r = await api.post(OPTS, headers=WORKER, json={"form_id": str(world.form.id), "field_key": "related", "q": "PK-"})
    assert sorted(o["label"].split(" ")[0] for o in r.json()["options"]) == ["PK-1", "PK-2"]


@pytest.mark.asyncio
async def test_document_search_needs_a_case_the_caller_can_see(api, world):
    base = {"form_id": str(world.form.id), "field_key": "evidence", "q": ""}
    r = await api.post(OPTS, headers=WORKER, json={**base, "case_id": str(world.here.id)})
    assert [o["label"] for o in r.json()["options"]] == ["photo.jpg"]
    assert (await api.post(OPTS, headers=WORKER, json=base)).status_code == 404
    assert (await api.post(OPTS, headers=WORKER, json={**base, "case_id": str(world.theirs.id)})).status_code == 404


@pytest.mark.asyncio
async def test_the_search_is_never_a_free_directory_query(api, world):
    # another tenant's form, a field that is not a picker, a field that does not exist, no login
    assert (await api.post(OPTS, headers=WORKER, json={"form_id": str(world.their_form.id), "field_key": "approver", "q": "ana"})).status_code == 404
    assert (await api.post(OPTS, headers=WORKER, json={"form_id": str(world.form.id), "field_key": "note", "q": "ana"})).status_code == 404
    assert (await api.post(OPTS, headers=WORKER, json={"form_id": str(world.form.id), "field_key": "nope", "q": "ana"})).status_code == 404
    assert (await api.post(OPTS, json={"form_id": str(world.form.id), "field_key": "approver", "q": "ana"})).status_code == 401
    # the other tenant's own worker sees only their own people
    r = await api.post(OPTS, headers=OTHER, json={"form_id": str(world.their_form.id), "field_key": "approver", "q": "ana"})
    assert [o["value"] for o in r.json()["options"]] == ["otto"]


@pytest.mark.asyncio
async def test_a_row_without_a_tenant_is_the_default_tenants_only(api, world):
    """Found live 2026-10-29: most directory rows predate tenants. A default-tenant
    caller may pick them; another tenant's caller never sees or submits them."""
    dflt = _headers("dee", ["case_worker"])                  # no directory row: the default tenant
    async with TestSessionFactory() as db:
        f = FormDefinitionModel(id=uuid.uuid4(), name="Platform pickers", version="1", tenant_id=None, definition_json=_form())
        db.add(f)
        await db.commit()
    r = await api.post(OPTS, headers=dflt, json={"form_id": str(f.id), "field_key": "approver", "q": "ana"})
    assert [o["value"] for o in r.json()["options"]] == ["olda"]
    r = await api.post(OPTS, headers=WORKER, json={"form_id": str(f.id), "field_key": "approver", "q": "ana"})
    assert "olda" not in [o["value"] for o in r.json()["options"]]
    async with TestSessionFactory() as db:
        assert (await picker_lookups(db, _form(), {"approver": "olda"}, user=_user("dee", "default")))["user"] == {"olda"}
        assert (await picker_lookups(db, _form(), {"approver": "olda"}, user=_user("wendy", "acme")))["user"] == set()

