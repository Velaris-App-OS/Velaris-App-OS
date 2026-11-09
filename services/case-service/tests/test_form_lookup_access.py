"""Who may run a connector as a form lookup (Form Builder redesign part 0, 2026-10-28).

Across tenants it was already safe: the app-wide tenant path guard
(`velaris_shared/tenancy/path_guard.py`) refuses another tenant's connector and
another tenant's `case_id` with 404 before the handler runs.

Inside a tenant it was not: any signed-in user, a viewer included, ran any of
the tenant's connectors with any input and read the raw response. Now the
request must name a saved form whose lookup field uses this connector, and only
that field's mapped inputs are sent. Free input stays for
`integration.connectors.execute` (the builder's Test).

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from velaris_shared.db.models import (
    CaseInstanceModel, CaseTypeModel, ConnectorRegistryModel, FormDefinitionModel,
    TenantModel, UserDirectoryModel,
)

from case_service.hxbridge import executor
from case_service.main import app
from case_service.tenancy import identity as tenancy
from tests.conftest import LIFECYCLE_PROCESS_ID, TestSessionFactory  # type: ignore[attr-defined]
from tests.conftest import _headers  # type: ignore[attr-defined]

VIEWER_DEV = _headers("vic", ["viewer"])              # tenant "dev"
WORKER_DEFAULT = _headers("wendy", ["case_worker"])  # tenant "default"
INTEGRATOR = _headers("ivan", ["integration"])       # tenant "default"


def _lookup_form(connector_id, tenant_id, key="acct_lookup"):
    return FormDefinitionModel(
        id=uuid.uuid4(), name="Lookup form", version="1.0", tenant_id=tenant_id,
        definition_json={"sections": [{"id": "s1", "title": "S", "order": 0, "fields": [
            {"id": "f1", "type": "text", "label": "Account", "field_key": "acct_no", "required": False},
            {"id": "f2", "type": "connector_lookup", "label": "Find", "field_key": key, "required": False,
             "source": {"connector_id": str(connector_id), "input_mapping": {"account": "acct_no"},
                        "response_path": ""}},
        ]}]})


@pytest_asyncio.fixture
async def api():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def world(monkeypatch):
    calls: list[dict] = []

    async def fake_execute(session, connector, input_data, **kw):
        calls.append({"connector": connector.name, "input": input_data})
        return {"account": "ACME-SECRET", "balance": 1234}

    monkeypatch.setattr(executor, "execute_connector", fake_execute)
    tenancy.clear_cache()
    async with TestSessionFactory() as db:
        d = TenantModel(id=uuid.uuid4(), slug="default", name="Default")
        v = TenantModel(id=uuid.uuid4(), slug="dev", name="Dev")
        db.add_all([d, v])
        await db.flush()
        db.add(UserDirectoryModel(id=uuid.uuid4(), user_id="vic", tenant_id="dev"))
        db.add(UserDirectoryModel(id=uuid.uuid4(), user_id="wendy", tenant_id="default"))
        db.add(UserDirectoryModel(id=uuid.uuid4(), user_id="ivan", tenant_id="default"))
        conn = ConnectorRegistryModel(id=uuid.uuid4(), name="default-crm", connector_type="http",
                                      config={}, credentials={}, tenant_id=str(d.id), enabled=True)
        ct = CaseTypeModel(id=uuid.uuid4(), name="CT", version="1",
                           lifecycle_process_id=LIFECYCLE_PROCESS_ID,
                           definition_json={}, tenant_id=d.id)
        db.add_all([conn, ct])
        await db.flush()
        case = CaseInstanceModel(id=uuid.uuid4(), case_type_id=ct.id, case_type_version="1",
                                 tenant_id=d.id, status="new", data={}, created_by="seed")
        db.add(case)
        other_conn = ConnectorRegistryModel(id=uuid.uuid4(), name="other", connector_type="http",
                                            config={}, credentials={}, tenant_id=str(d.id), enabled=True)
        db.add(other_conn)
        form = _lookup_form(conn.id, d.id)
        foreign_form = _lookup_form(conn.id, v.id)
        db.add_all([form, foreign_form])
        await db.commit()
    yield {"connector": conn, "other_connector": other_conn, "case": case, "calls": calls,
           "form": form, "foreign_form": foreign_form}
    tenancy.clear_cache()


def _url(c) -> str:
    return f"/api/v1/hxbridge/connectors/{c.id}/form-lookup"


@pytest.mark.asyncio
async def test_another_tenants_connector_is_refused(api, world):
    r = await api.post(_url(world["connector"]), headers=VIEWER_DEV,
                       json={"input_data": {"q": "anything"}, "field_key": "x"})
    assert r.status_code == 404, r.text
    assert world["calls"] == []


@pytest.mark.asyncio
async def test_another_tenants_case_is_refused(api, world):
    r = await api.post(_url(world["connector"]), headers=VIEWER_DEV,
                       json={"input_data": {}, "field_key": "x",
                             "case_id": str(world["case"].id)})
    assert r.status_code == 404, r.text
    assert world["calls"] == []




@pytest.mark.asyncio
async def test_a_global_connector_with_another_tenants_case_is_refused(api, world):
    async with TestSessionFactory() as db:
        g = ConnectorRegistryModel(id=uuid.uuid4(), name="global", connector_type="http",
                                   config={}, credentials={}, tenant_id=None, enabled=True)
        db.add(g)
        await db.commit()
    r = await api.post(_url(g), headers=VIEWER_DEV,
                       json={"input_data": {}, "field_key": "x", "case_id": str(world["case"].id)})
    assert r.status_code == 404, r.text
    assert world["calls"] == []



# ── inside the tenant: only what a saved form says ──────────────────────────

def _body(form, key="acct_lookup", **inputs):
    return {"form_id": str(form.id), "field_key": key, "input_data": inputs}


@pytest.mark.asyncio
async def test_saved_form_lookup_runs_with_only_the_mapped_inputs(api, world):
    """Least-privileged user who should succeed: a case worker filling the form."""
    r = await api.post(_url(world["connector"]), headers=WORKER_DEFAULT,
                       json=_body(world["form"], account="A-1", smuggled="ANY-OTHER-CUSTOMER"))
    assert r.status_code == 200 and r.json()["ok"], r.text
    assert world["calls"] == [{"connector": "default-crm", "input": {"account": "A-1"}}]


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [WORKER_DEFAULT, _headers("tim", ["viewer"])])
async def test_free_input_without_a_form_is_refused(api, world, headers):
    r = await api.post(_url(world["connector"]), headers=headers,
                       json={"input_data": {"account": "ANY-OTHER-CUSTOMER"}, "field_key": "x"})
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "VEL-CONN-FORM-LOOKUP-403"
    assert world["calls"] == []


@pytest.mark.asyncio
async def test_a_form_whose_field_uses_another_connector_is_refused(api, world):
    r = await api.post(_url(world["other_connector"]), headers=WORKER_DEFAULT,
                       json=_body(world["form"], account="A-1"))
    assert r.status_code == 403, r.text
    assert world["calls"] == []


@pytest.mark.asyncio
async def test_an_unknown_field_is_refused(api, world):
    r = await api.post(_url(world["connector"]), headers=WORKER_DEFAULT,
                       json=_body(world["form"], key="acct_no", account="A-1"))
    assert r.status_code == 403, r.text
    assert world["calls"] == []


@pytest.mark.asyncio
async def test_another_tenants_form_is_refused(api, world):
    r = await api.post(_url(world["connector"]), headers=WORKER_DEFAULT,
                       json=_body(world["foreign_form"], account="A-1"))
    assert r.status_code == 403, r.text
    assert world["calls"] == []


@pytest.mark.asyncio
async def test_integration_role_keeps_free_input_for_the_builders_test(api, world):
    r = await api.post(_url(world["connector"]), headers=INTEGRATOR,
                       json={"input_data": {"account": "A-9"}, "field_key": "x"})
    assert r.status_code == 200 and r.json()["ok"], r.text
    assert world["calls"] == [{"connector": "default-crm", "input": {"account": "A-9"}}]


@pytest.mark.asyncio
async def test_a_choice_field_with_connector_options_may_run_its_connector(api, world):
    """Form Builder part 4: a dropdown's options can come from a connector."""
    async with TestSessionFactory() as db:
        form = FormDefinitionModel(
            id=uuid.uuid4(), name="Options form", version="1.0", tenant_id=world["form"].tenant_id,
            definition_json={"sections": [{"id": "s1", "title": "S", "order": 0, "fields": [
                {"id": "f1", "type": "dropdown", "label": "Branch", "field_key": "branch", "required": False,
                 "options_source": {"kind": "connector", "connector_id": str(world["connector"].id),
                                    "input_mapping": {}, "list_path": "items"}},
                {"id": "f2", "type": "dropdown", "label": "Fixed", "field_key": "fixed", "required": False,
                 "options": [{"label": "A", "value": "a"}]},
            ]}]})
        db.add(form)
        await db.commit()
    ok = await api.post(_url(world["connector"]), headers=WORKER_DEFAULT,
                        json={"form_id": str(form.id), "field_key": "branch", "input_data": {"x": 1}})
    assert ok.status_code == 200, ok.text
    assert world["calls"][-1]["input"] == {}
    fixed = await api.post(_url(world["connector"]), headers=WORKER_DEFAULT,
                           json={"form_id": str(form.id), "field_key": "fixed", "input_data": {}})
    assert fixed.status_code == 403, fixed.text
