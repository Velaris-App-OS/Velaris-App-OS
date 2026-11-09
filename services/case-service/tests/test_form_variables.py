"""Which case variables a form can pre-fill from (Form Builder part 6, 2026-10-28).

`GET /forms/{id}/variables`: the variables of the case types whose steps use
the form — the caller's tenant's and platform-shared ones, never another
tenant's. Designers only.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from velaris_shared.db.models import (
    CaseTypeModel, CaseTypeVariableModel, FormDefinitionModel, TenantModel,
    UserDirectoryModel, VariableNamespaceModel,
)

from case_service.main import app
from case_service.tenancy import identity as tenancy
from tests.conftest import LIFECYCLE_PROCESS_ID, TestSessionFactory  # type: ignore[attr-defined]
from tests.conftest import _headers  # type: ignore[attr-defined]

DESIGNER = _headers("dora", ["designer"])     # tenant "default"
WORKER = _headers("wendy", ["case_worker"])   # tenant "default"


@pytest_asyncio.fixture
async def api():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _ct(name, tenant_id, form_id):
    return CaseTypeModel(id=uuid.uuid4(), name=name, version="1", lifecycle_process_id=LIFECYCLE_PROCESS_ID,
                         tenant_id=tenant_id, definition_json={"stages": [{"id": "s", "steps": [
                             {"id": "x", "step_type": "user_task", "form_id": str(form_id)}]}]})


@pytest_asyncio.fixture
async def world():
    tenancy.clear_cache()
    async with TestSessionFactory() as db:
        d = TenantModel(id=uuid.uuid4(), slug="default", name="Default")
        o = TenantModel(id=uuid.uuid4(), slug="other", name="Other")
        db.add_all([d, o])
        await db.flush()
        for uid in ("dora", "wendy"):
            db.add(UserDirectoryModel(id=uuid.uuid4(), user_id=uid, tenant_id="default"))
        form = FormDefinitionModel(id=uuid.uuid4(), name="Claim", version="1", tenant_id=d.id,
                                   definition_json={"sections": []})
        unused = FormDefinitionModel(id=uuid.uuid4(), name="Unused", version="1", tenant_id=d.id,
                                     definition_json={"sections": []})
        mine, theirs = _ct("Motor claim", d.id, form.id), _ct("Their claim", o.id, form.id)
        ns = VariableNamespaceModel(id=uuid.uuid4(), name="crm", owner_type="connector")
        db.add_all([form, unused, mine, theirs, ns])
        await db.flush()
        db.add_all([
            CaseTypeVariableModel(id=uuid.uuid4(), case_type_id=mine.id, namespace_id=ns.id,
                                  name="account_status", full_key="crm.account_status", var_type="string",
                                  label="Account status"),
            CaseTypeVariableModel(id=uuid.uuid4(), case_type_id=theirs.id, namespace_id=ns.id,
                                  name="secret", full_key="crm.secret", var_type="string"),
        ])
        await db.commit()
    yield {"form": form, "unused": unused}
    tenancy.clear_cache()


@pytest.mark.asyncio
async def test_a_designer_sees_the_variables_of_case_types_using_the_form(api, world):
    r = await api.get(f"/api/v1/forms/{world['form'].id}/variables", headers=DESIGNER)
    assert r.status_code == 200, r.text
    body = r.json()
    assert [c["name"] for c in body["case_types"]] == ["Motor claim"]
    assert [(v["full_key"], v["label"], v["case_types"]) for v in body["variables"]] == [
        ("crm.account_status", "Account status", ["Motor claim"])]


@pytest.mark.asyncio
async def test_an_unused_form_has_no_variables(api, world):
    r = await api.get(f"/api/v1/forms/{world['unused'].id}/variables", headers=DESIGNER)
    assert r.status_code == 200 and r.json() == {"case_types": [], "variables": []}


@pytest.mark.asyncio
async def test_a_case_worker_is_refused(api, world):
    r = await api.get(f"/api/v1/forms/{world['form'].id}/variables", headers=WORKER)
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_saving_a_form_designer_yes_viewer_no(api, world):
    """Least-privileged user who should succeed (designer) and one who must be refused."""
    body = {"definition_json": {"settings": {"mode": "steps"}, "sections": [
        {"id": "s", "title": "S", "order": 0, "fields": [
            {"id": "a", "type": "text", "label": "A", "field_key": "a", "required": False,
             "show_when": {"rules": [{"field": "b", "op": "not_empty"}]}}]}]}}
    ok = await api.patch(f"/api/v1/forms/{world['form'].id}", headers=DESIGNER, json=body)
    assert ok.status_code == 200, ok.text
    assert ok.json()["definition_json"]["settings"] == {"mode": "steps"}
    for refused in (WORKER, _headers("vic", ["viewer"])):
        r = await api.patch(f"/api/v1/forms/{world['form'].id}", headers=refused, json=body)
        assert r.status_code == 403, r.text
