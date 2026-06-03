"""HELIX P28 — HxBridge Connector Protocol Foundation tests (23 tests).

Covers: encryption round-trip, mask_credentials, connector registry
        (list types, create, duplicate 409, list, get detail, update,
        delete, test endpoint, sandbox execute), integration call history
        (list, connector filter), dead letter queue (list, retry),
        inbound webhook receiver, protocol self-registration,
        unknown connector_type 400.
"""
from __future__ import annotations

import uuid
import pytest
from httpx import AsyncClient

from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser, ActiveAccessGroup
from case_service.hxbridge.encryption import encrypt_credentials, decrypt_credentials, mask_credentials
from case_service.hxbridge.protocol import CONNECTOR_REGISTRY
from case_service.main import app
import case_service.hxbridge.connectors  # noqa: ensure registration


# ── Auth ──────────────────────────────────────────────────────────────────────

def _admin():
    return AuthenticatedUser(
        user_id="admin-1", roles=["admin"],
        active_access_group=ActiveAccessGroup(
            id=str(uuid.uuid4()), name="Admins",
            portal_id=str(uuid.uuid4()), portal_type="admin",
            portal_name="Admin Portal", modules=[], homepage="/",
            roles=["admin"], privileges=[],
            allowed_case_type_ids=["*"], allowed_queue_ids=["*"],
        ),
    )

def _override():
    app.dependency_overrides[get_current_user] = lambda: _admin()

def _clear():
    app.dependency_overrides.pop(get_current_user, None)


# ── Encryption tests ──────────────────────────────────────────────────────────

class TestEncryption:
    def test_round_trip(self):
        creds = {"api_key": "sk-secret-123", "token": "bearer-abc"}
        stored = encrypt_credentials(creds)
        assert "_enc" in stored
        assert stored["_enc"].startswith("hxv1:")
        recovered = decrypt_credentials(stored)
        assert recovered == creds

    def test_empty_creds_returns_empty(self):
        assert encrypt_credentials({}) == {}
        assert decrypt_credentials({}) == {}

    def test_unencrypted_passthrough(self):
        plain = {"key": "value"}
        assert decrypt_credentials(plain) == plain

    def test_mask_hides_values(self):
        stored = encrypt_credentials({"secret": "my-secret"})
        masked = mask_credentials(stored)
        assert masked == {"_enc": "***"}

    def test_mask_plain_dict(self):
        masked = mask_credentials({"api_key": "secret", "token": "abc"})
        assert all(v == "***" for v in masked.values())


# ── Protocol registry ─────────────────────────────────────────────────────────

class TestRegistry:
    def test_http_connector_registered(self):
        assert "http" in CONNECTOR_REGISTRY

    def test_webhook_connector_registered(self):
        assert "webhook" in CONNECTOR_REGISTRY


# ── API tests ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestConnectorTypes:
    def setup_method(self): _override()
    def teardown_method(self): _clear()

    async def test_list_connector_types(self, client: AsyncClient):
        r = await client.get("/api/v1/hxbridge/connector-types")
        assert r.status_code == 200
        types = [ct["connector_type"] for ct in r.json()["connector_types"]]
        assert "http" in types
        assert "webhook" in types


@pytest.mark.asyncio
class TestConnectorCRUD:
    def setup_method(self): _override()
    def teardown_method(self): _clear()

    async def test_create_connector(self, client: AsyncClient):
        r = await client.post("/api/v1/hxbridge/connectors", json={
            "name": "My Webhook",
            "connector_type": "webhook",
            "description": "Test webhook",
            "config": {"url": "https://example.com/hook", "method": "POST"},
            "credentials": {},
        })
        assert r.status_code == 201
        d = r.json()
        assert d["name"] == "My Webhook"
        assert d["connector_type"] == "webhook"
        assert "id" in d

    async def test_create_unknown_type_returns_400(self, client: AsyncClient):
        r = await client.post("/api/v1/hxbridge/connectors", json={
            "name": "Bad", "connector_type": "stripe_unknown",
        })
        assert r.status_code == 400

    async def test_duplicate_name_returns_409(self, client: AsyncClient):
        body = {"name": "DupTest", "connector_type": "webhook",
                "config": {"url": "https://example.com"}}
        await client.post("/api/v1/hxbridge/connectors", json=body)
        r = await client.post("/api/v1/hxbridge/connectors", json=body)
        assert r.status_code == 409

    async def test_list_connectors(self, client: AsyncClient):
        await client.post("/api/v1/hxbridge/connectors", json={
            "name": "ListTest", "connector_type": "http",
            "config": {"base_url": "https://api.example.com", "method": "GET"},
        })
        r = await client.get("/api/v1/hxbridge/connectors")
        assert r.status_code == 200
        assert r.json()["total"] >= 1

    async def test_get_connector_detail(self, client: AsyncClient):
        resp = await client.post("/api/v1/hxbridge/connectors", json={
            "name": "DetailTest", "connector_type": "webhook",
            "config": {"url": "https://example.com"},
            "credentials": {"secret": "abc123"},
        })
        cid = resp.json()["id"]
        r = await client.get(f"/api/v1/hxbridge/connectors/{cid}")
        assert r.status_code == 200
        d = r.json()
        assert "config" in d
        assert d["credentials"]["_enc"] == "***"   # masked

    async def test_update_connector(self, client: AsyncClient):
        resp = await client.post("/api/v1/hxbridge/connectors", json={
            "name": "UpdateTest", "connector_type": "webhook",
            "config": {"url": "https://old.example.com"},
        })
        cid = resp.json()["id"]
        r = await client.put(f"/api/v1/hxbridge/connectors/{cid}", json={
            "config": {"url": "https://new.example.com", "method": "POST"},
            "enabled": False,
        })
        assert r.status_code == 200
        assert r.json()["enabled"] is False

    async def test_delete_connector(self, client: AsyncClient):
        resp = await client.post("/api/v1/hxbridge/connectors", json={
            "name": "DeleteTest", "connector_type": "webhook",
            "config": {"url": "https://example.com"},
        })
        cid = resp.json()["id"]
        r = await client.delete(f"/api/v1/hxbridge/connectors/{cid}")
        assert r.status_code == 204
        r2 = await client.get(f"/api/v1/hxbridge/connectors/{cid}")
        assert r2.status_code == 404

    async def test_get_404(self, client: AsyncClient):
        r = await client.get(f"/api/v1/hxbridge/connectors/{uuid.uuid4()}")
        assert r.status_code == 404


@pytest.mark.asyncio
class TestConnectorTest:
    def setup_method(self): _override()
    def teardown_method(self): _clear()

    async def test_test_endpoint_returns_ok_field(self, client: AsyncClient):
        resp = await client.post("/api/v1/hxbridge/connectors", json={
            "name": "TestConn", "connector_type": "webhook",
            "config": {"url": "https://httpbin.org/post"},
        })
        cid = resp.json()["id"]
        r = await client.post(f"/api/v1/hxbridge/connectors/{cid}/test")
        assert r.status_code == 200
        assert "ok" in r.json()

    async def test_sandbox_execute_disabled_connector(self, client: AsyncClient):
        resp = await client.post("/api/v1/hxbridge/connectors", json={
            "name": "DisabledConn", "connector_type": "webhook",
            "config": {"url": "https://example.com"},
        })
        cid = resp.json()["id"]
        await client.put(f"/api/v1/hxbridge/connectors/{cid}", json={"enabled": False})
        r = await client.post(f"/api/v1/hxbridge/connectors/{cid}/execute",
                              json={"input_data": {"test": 1}})
        assert r.status_code == 400


@pytest.mark.asyncio
class TestCallHistory:
    def setup_method(self): _override()
    def teardown_method(self): _clear()

    async def test_calls_list_empty(self, client: AsyncClient):
        r = await client.get("/api/v1/hxbridge/calls")
        assert r.status_code == 200
        assert r.json()["total"] == 0


@pytest.mark.asyncio
class TestDLQ:
    def setup_method(self): _override()
    def teardown_method(self): _clear()

    async def test_dlq_list_empty(self, client: AsyncClient):
        r = await client.get("/api/v1/hxbridge/dlq")
        assert r.status_code == 200
        assert r.json()["total"] == 0

    async def test_dlq_retry_404(self, client: AsyncClient):
        r = await client.post(f"/api/v1/hxbridge/dlq/{uuid.uuid4()}/retry")
        assert r.status_code == 404


@pytest.mark.asyncio
class TestWebhookReceiver:
    async def test_webhook_receive(self, client: AsyncClient):
        _override()
        resp = await client.post("/api/v1/hxbridge/connectors", json={
            "name": "WebhookIn", "connector_type": "webhook",
            "config": {"url": "https://example.com"},
        })
        _clear()
        cid = resp.json()["id"]
        r = await client.post(
            f"/api/v1/webhooks/{cid}/receive",
            json={"event": "payment.succeeded", "amount": 1000},
        )
        assert r.status_code == 202
        assert r.json()["received"] is True

    async def test_webhook_receive_unknown_connector(self, client: AsyncClient):
        r = await client.post(
            f"/api/v1/webhooks/{uuid.uuid4()}/receive",
            json={"event": "test"},
        )
        assert r.status_code == 404
