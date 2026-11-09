"""Connector operations — several named requests on one connector (2026-11-04).

The operation is the saved form field's (or the integration admin's Test), never
the form filler's; its path joins the connector's own base address with
placeholders URL-encoded, so no input can change the host. A connector with no
operations, or a call naming none, is unchanged.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest

from tests.conftest import TestSessionFactory, _headers
from velaris_shared.db.models import ConnectorRegistryModel, FormDefinitionModel
from velaris_shared.hxbridge.operations import (
    OperationError, apply_operation, fill_path, validate_operations,
)

OPS = [{"key": "find_customer", "label": "Find customer", "method": "GET", "path": "/customers/{cid}"},
       {"key": "list_orders", "label": "List orders", "method": "POST", "path": "/orders"}]


# ── the rules ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["customers", "//evil.test/x", "/x?y=1", "/x#f", "/a/../b",
                                  "https://evil.test/x", "/a\\b", "/./x"])
def test_a_path_that_could_leave_the_connector_is_refused(path):
    with pytest.raises(OperationError):
        validate_operations("http_custom", {"operations": [{"key": "a", "method": "GET", "path": path}]})


def test_keys_methods_and_types_are_checked():
    for bad in ([{"key": "Bad Key", "method": "GET", "path": "/x"}],
                [{"key": "a", "method": "TRACE", "path": "/x"}],
                [{"key": "a", "method": "GET", "path": "/x"}, {"key": "a", "method": "GET", "path": "/y"}]):
        with pytest.raises(OperationError):
            validate_operations("http", {"operations": bad})
    with pytest.raises(OperationError):
        validate_operations("stripe", {"operations": OPS})
    assert validate_operations("stripe", {}) == []          # no operations: as before
    assert [o["key"] for o in validate_operations("http", {"operations": OPS})] == ["find_customer", "list_orders"]


def test_a_placeholder_cannot_change_the_host_query_or_directory():
    assert fill_path("/customers/{cid}", {"cid": "42"}) == "/customers/42"
    for evil in ("@evil.test", "../admin", "x?admin=1", "x#y", "/../../etc", "evil.test/"):
        out = fill_path("/customers/{cid}", {"cid": evil})
        assert "/" not in out[len("/customers/"):] and "?" not in out and "#" not in out and "@" not in out


def test_naming_no_operation_changes_nothing_and_an_unknown_one_is_refused():
    cfg = {"url": "https://api.example.com", "method": "GET", "operations": OPS}
    assert apply_operation("http_custom", cfg, None) == cfg
    assert apply_operation("http_custom", cfg, "list_orders")["_op_path"] == "/orders"
    assert apply_operation("http_custom", cfg, "list_orders")["method"] == "POST"
    with pytest.raises(OperationError):
        apply_operation("http_custom", cfg, "drop_tables")


async def test_http_custom_joins_the_path_to_its_own_host_and_pins_it(monkeypatch):
    import httpx
    from case_service.hxbridge.connectors.http_custom_connector import HttpCustomConnector
    from velaris_shared.hxbridge import security
    seen = {}

    async def fake_request(self, method, url, **kw):
        seen.update(method=method, url=url, headers=kw.get("headers") or {}, ext=kw.get("extensions") or {})
        return httpx.Response(200, json={"ok": True}, request=httpx.Request(method, url))
    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    monkeypatch.setattr(security, "validate_outbound_url", lambda url: _noop())

    async def fake_ip(host):
        return "93.184.216.34"
    monkeypatch.setattr(security, "pinned_public_ip", fake_ip)
    cfg = apply_operation("http_custom", {"url": "https://api.example.com/v1", "operations": OPS}, "find_customer")
    conn = HttpCustomConnector(security.runtime_config(cfg, False), {})
    await conn.execute({"cid": "@evil.test/x"})
    assert seen["method"] == "GET"
    # Sent to the address that was checked, named as the real host.
    assert httpx.URL(seen["url"]).host == "93.184.216.34"
    assert httpx.URL(seen["url"]).path.startswith("/v1/customers/")
    assert seen["headers"]["Host"] == "api.example.com"
    assert seen["ext"] == {"sni_hostname": "api.example.com"}


async def _noop():
    return None


# ── the route: the saved field chooses, not the caller ────────────────────────

@pytest.fixture
async def saved(monkeypatch):
    from case_service.hxbridge import executor
    calls = []

    async def fake_execute(session, connector, input_data, **kw):
        calls.append(kw.get("operation"))
        return {"ran": kw.get("operation")}
    monkeypatch.setattr(executor, "execute_connector", fake_execute)
    async with TestSessionFactory() as db:
        c = ConnectorRegistryModel(name=f"crm-{uuid.uuid4().hex[:6]}", connector_type="http_custom",
                                   config={"url": "https://api.example.com", "operations": OPS},
                                   credentials={}, tenant_id="default", enabled=True)
        db.add(c)
        await db.flush()
        f = FormDefinitionModel(name="Lookup form", version="1.0.0", tenant_id=None, definition_json={
            "sections": [{"id": "s", "title": "S", "fields": [
                {"id": "who", "type": "connector_lookup", "label": "Who", "field_key": "who",
                 "source": {"connector_id": str(c.id), "operation": "find_customer",
                            "input_mapping": {"cid": "cid"}}}]}]})
        db.add(f)
        await db.commit()
    return {"connector": c, "form": f, "calls": calls}


async def test_the_saved_field_names_the_operation(client, saved):
    worker = _headers("worker-1", ["case_worker"])
    r = await client.post(f"/api/v1/hxbridge/connectors/{saved['connector'].id}/form-lookup", headers=worker,
                          json={"input_data": {"cid": "7"}, "form_id": str(saved["form"].id),
                                "field_key": "who", "operation": "list_orders"})   # tries to swap it
    assert r.status_code == 200, r.text
    assert saved["calls"] == ["find_customer"]


async def test_a_connector_is_refused_a_bad_operation_on_save(client):
    r = await client.post("/api/v1/hxbridge/connectors", headers=_headers("op", ["admin", "superadmin"]), json={
        "name": f"x-{uuid.uuid4().hex[:6]}", "connector_type": "http_custom",
        "config": {"url": "https://api.example.com", "operations": [
            {"key": "a", "method": "GET", "path": "https://evil.test/"}]}, "credentials": {}})
    assert r.status_code == 400, r.text


# ── wiring sweep (2026-11-04) ─────────────────────────────────────────────────

def test_a_planted_reserved_key_is_refused_on_save_and_dropped_at_run():
    for key in ("_op_path", "_op_path_placeholders", "_platform_owned"):
        with pytest.raises(OperationError):
            validate_operations("http_custom", {"url": "https://api.example.com", key: "x"})
    cfg = apply_operation("http_custom", {"url": "https://api.example.com", "_op_path": "/x"}, None)
    assert "_op_path" not in cfg


async def test_an_operator_connector_with_a_templated_host_is_held_to_public_targets():
    from case_service.hxbridge.connectors.http_custom_connector import HttpCustomConnector
    from velaris_shared.hxbridge.security import runtime_config
    conn = HttpCustomConnector(runtime_config({"method": "GET", "url": "http://{host}/x"}, True), {})
    with pytest.raises(ValueError):
        await conn.execute({"host": "127.0.0.1:5173"})


async def test_a_workflow_step_runs_its_connector_through_the_executor(monkeypatch):
    """It called the connector TYPE factory with an id, so every service task failed."""
    from case_service.hxbridge import executor
    seen = {}

    async def fake_execute(session, connector, payload, **kw):
        seen.update(name=connector.name, op=kw.get("operation"), step=kw.get("step_id"))
        return {"ok": True}
    monkeypatch.setattr(executor, "execute_connector", fake_execute)
    async with TestSessionFactory() as db:
        mine = ConnectorRegistryModel(name="mine", connector_type="http_custom", tenant_id="acme",
                                      config={"url": "https://api.example.com", "operations": OPS},
                                      credentials={}, enabled=True)
        theirs = ConnectorRegistryModel(name="theirs", connector_type="http_custom", tenant_id="evil",
                                        config={"url": "https://api.example.com"}, credentials={}, enabled=True)
        off = ConnectorRegistryModel(name="off", connector_type="http_custom", tenant_id="acme",
                                     config={"url": "https://api.example.com"}, credentials={}, enabled=False)
        db.add_all([mine, theirs, off])
        await db.commit()
        out = await executor.run_connector_by_id(db, str(mine.id), {"cid": "1"}, owner_tenant="acme",
                                                 step_id="s1", operation="find_customer")
        assert out == {"ok": True} and seen == {"name": "mine", "op": "find_customer", "step": "s1"}
        for cid in (theirs.id, off.id, uuid.uuid4(), "not-a-uuid"):
            with pytest.raises(RuntimeError):
                await executor.run_connector_by_id(db, str(cid), {}, owner_tenant="acme")


async def test_an_operator_connector_is_the_default_tenants_alone(monkeypatch):
    """Workflow steps, schedules, KYC, documents, comms and meetings all ask
    `usable_by`: a tenant-less connector carries the operator's accounts."""
    from case_service.hxbridge import executor
    from velaris_shared.hxbridge.security import usable_by

    async def fake_execute(session, connector, payload, **kw):
        return {"ok": True}
    monkeypatch.setattr(executor, "execute_connector", fake_execute)
    async with TestSessionFactory() as db:
        op = ConnectorRegistryModel(name="operator-crm", connector_type="http_custom", tenant_id=None,
                                    config={"url": "https://api.example.com"}, credentials={}, enabled=True)
        db.add(op)
        await db.commit()
        assert await usable_by(db, op, "default") is True
        assert await usable_by(db, op, "acme") is False
        assert await executor.run_connector_by_id(db, op.id, {}, owner_tenant="default") == {"ok": True}
        with pytest.raises(RuntimeError):
            await executor.run_connector_by_id(db, op.id, {}, owner_tenant="acme")
