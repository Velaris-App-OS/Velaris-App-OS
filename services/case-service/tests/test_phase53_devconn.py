"""Tests for P53 HxConnect — Developer & Custom Connectors."""
from __future__ import annotations

import json
import uuid
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import ConnectorRegistryModel, WebhookReceiverRuleModel, WebhookReceiverEventModel
from case_service.hxbridge.encryption import encrypt_credentials

from tests.conftest import client, session, deploy_case_type, create_case  # type: ignore[attr-defined]


# ── helpers ───────────────────────────────────────────────────────────────────

async def _reg_custom(session: AsyncSession, tenant_id: str = "t1") -> ConnectorRegistryModel:
    row = ConnectorRegistryModel(
        name="Custom Test", connector_type="http_custom",
        config={"method": "POST", "url": "https://api.example.com/notify", "headers": {}, "auth_type": "none", "body_template": "", "response_mapping": {}},
        credentials=encrypt_credentials({}),
        tenant_id=tenant_id, enabled=True,
    )
    session.add(row); await session.flush(); return row


async def _webhook_case(client: AsyncClient) -> dict:
    ct = await deploy_case_type(client, name="Webhook Case", definition_json={
        "stages": [{"id": "s1", "name": "Process", "order": 1, "steps": [
            {"id": "step1", "name": "Handle Webhook", "step_type": "user_task", "required": True},
        ]}]
    })
    return await create_case(client, ct["id"])


# ── Custom HTTP Connector Builder ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_build_http_connector(client: AsyncClient):
    r = await client.post("/api/v1/devconn/connectors/build", json={
        "name": "Test API",
        "method": "POST",
        "url": "https://httpbin.org/post",
        "headers": {"X-Custom": "value"},
        "auth_type": "none",
        "body_template": '{"id": "{case_id}"}',
        "response_mapping": {"external_id": "json.id"},
        "credentials": {},
    })
    assert r.status_code == 201
    data = r.json()
    assert data["name"] == "Test API"
    assert data["connector_type"] == "http_custom"
    assert data["enabled"] is True


@pytest.mark.asyncio
async def test_list_custom_connectors(client: AsyncClient, session: AsyncSession):
    await _reg_custom(session, tenant_id="default"); await session.commit()
    r = await client.get("/api/v1/devconn/connectors")
    assert r.status_code == 200
    assert any(c["connector_type"] == "http_custom" for c in r.json())


@pytest.mark.asyncio
async def test_build_connector_with_bearer_auth(client: AsyncClient):
    r = await client.post("/api/v1/devconn/connectors/build", json={
        "name": "Bearer API",
        "method": "GET",
        "url": "https://api.example.com/data",
        "headers": {},
        "auth_type": "bearer",
        "body_template": "",
        "response_mapping": {},
        "credentials": {"token": "secret-token"},
    })
    assert r.status_code == 201
    assert r.json()["name"] == "Bearer API"


# ── Webhook Rules ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_rules_empty(client: AsyncClient):
    r = await client.get("/api/v1/devconn/connectors/inbound")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_create_rule_with_case_id_field(client: AsyncClient):
    r = await client.post("/api/v1/devconn/connectors/inbound", json={
        "name": "Route by case_id",
        "case_id_field": "data.helix_case_id",
        "field_updates": {"status": "data.status"},
        "advance_stage": False,
    })
    assert r.status_code == 201
    data = r.json()
    assert data["name"] == "Route by case_id"
    assert data["case_id_field"] == "data.helix_case_id"
    assert data["field_updates"] == {"status": "data.status"}


@pytest.mark.asyncio
async def test_create_rule_with_match_fields(client: AsyncClient):
    r = await client.post("/api/v1/devconn/connectors/inbound", json={
        "name": "Match by reference",
        "match_case_field": "reference_number",
        "match_payload_field": "order.ref",
        "field_updates": {},
        "advance_stage": True,
    })
    assert r.status_code == 201
    data = r.json()
    assert data["match_case_field"] == "reference_number"
    assert data["advance_stage"] is True


@pytest.mark.asyncio
async def test_delete_rule(client: AsyncClient):
    r = await client.post("/api/v1/devconn/connectors/inbound", json={"name": "To Delete", "field_updates": {}})
    rule_id = r.json()["id"]
    d = await client.delete(f"/api/v1/devconn/connectors/inbound/{rule_id}")
    assert d.status_code == 204
    rules = (await client.get("/api/v1/devconn/connectors/inbound")).json()
    assert not any(rule["id"] == rule_id for rule in rules)


@pytest.mark.asyncio
async def test_rule_listed_after_create(client: AsyncClient):
    await client.post("/api/v1/devconn/connectors/inbound", json={"name": "Rule Alpha", "field_updates": {"a": "b.c"}})
    r = await client.get("/api/v1/devconn/connectors/inbound")
    assert any(rule["name"] == "Rule Alpha" for rule in r.json())


# ── Webhook Receiver ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_receive_webhook_no_match(client: AsyncClient, session: AsyncSession):
    reg = await _reg_custom(session); await session.commit()
    r = await client.post(f"/api/v1/devconn/webhooks/receive/{reg.id}",
                          json={"event": "payment.succeeded", "amount": 5000})
    assert r.status_code == 202
    assert r.json()["status"] in ("no_match", "received")


@pytest.mark.asyncio
async def test_receive_webhook_matches_case_by_id(client: AsyncClient, session: AsyncSession):
    reg = await _reg_custom(session); await session.commit()  # commit BEFORE client calls (they roll back the shared connection)
    case = await _webhook_case(client)

    rule = WebhookReceiverRuleModel(
        tenant_id="t1", connector_id=reg.id,
        name="Route by ID",
        case_id_field="case_id",
        field_updates={}, advance_stage=False, enabled=True,
    )
    session.add(rule); await session.commit()

    r = await client.post(f"/api/v1/devconn/webhooks/receive/{reg.id}",
                          json={"case_id": case["id"], "status": "completed"})
    assert r.status_code == 202
    assert r.json()["status"] == "matched"


@pytest.mark.asyncio
async def test_receive_webhook_unknown_connector(client: AsyncClient):
    # hardened: an unknown connector id is 404 (the unguessable UUID is the
    # gate for secretless connectors — a guessed id must learn nothing)
    r = await client.post(f"/api/v1/devconn/webhooks/receive/{uuid.uuid4()}",
                          json={"payload": "test"})
    assert r.status_code == 404


# ── Events ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_events_empty(client: AsyncClient):
    r = await client.get("/api/v1/devconn/events")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_events_recorded_after_receive(client: AsyncClient, session: AsyncSession):
    reg = await _reg_custom(session); await session.commit()
    await client.post(f"/api/v1/devconn/webhooks/receive/{reg.id}", json={"x": 1})
    r = await client.get(f"/api/v1/devconn/events?connector_id={reg.id}")
    assert r.status_code == 200
    assert len(r.json()) >= 1


@pytest.mark.asyncio
async def test_events_filter_by_status(client: AsyncClient, session: AsyncSession):
    reg = await _reg_custom(session); await session.commit()
    await client.post(f"/api/v1/devconn/webhooks/receive/{reg.id}", json={"x": 1})
    r = await client.get("/api/v1/devconn/events?status=no_match")
    assert r.status_code == 200
    assert all(e["status"] == "no_match" for e in r.json())


# ── OpenAPI Auto-Connector ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_from_openapi_heuristic_parse(client: AsyncClient):
    spec = json.dumps({
        "openapi": "3.0.0",
        "info": {"title": "Test API", "version": "1.0"},
        "servers": [{"url": "https://api.test.com"}],
        "paths": {
            "/users": {
                "post": {"operationId": "createUser", "summary": "Create a user",
                         "requestBody": {}, "responses": {"201": {"description": "Created"}}},
            },
            "/users/{id}": {
                "get": {"operationId": "getUser", "summary": "Get user by ID", "responses": {"200": {}}},
            },
        },
    })
    r = await client.post("/api/v1/devconn/connectors/from-openapi",
                          json={"spec": spec, "connector_name": "Test API"})
    assert r.status_code == 200
    data = r.json()
    assert data["base_url"] == "https://api.test.com"
    assert len(data["suggested_operations"]) >= 1
    op_ids = [op["operation_id"] for op in data["suggested_operations"]]
    assert any("createUser" in o or "post" in o for o in op_ids)


@pytest.mark.asyncio
async def test_from_openapi_returns_name(client: AsyncClient):
    spec = json.dumps({"openapi": "3.0.0", "info": {"title": "T", "version": "1"}, "paths": {}})
    r = await client.post("/api/v1/devconn/connectors/from-openapi",
                          json={"spec": spec, "connector_name": "My Connector"})
    assert r.status_code == 200
    assert r.json()["name"] == "My Connector"
