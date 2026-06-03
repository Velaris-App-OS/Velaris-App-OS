"""
Tests for HttpTaskResolver
============================

Tests the HTTP resolver with mocked HTTP responses.
No real network calls — everything is intercepted by httpx mock transport.

Run with:  uv run python -m pytest engine/tests/unit/test_http_resolver.py -v
"""

from __future__ import annotations

import json
import pytest
import httpx

from helix_ir.models.process import ServiceTask, SendTask, UserTask
from helix_engine.plugins.http_resolver import HttpTaskResolver


# ── Mock HTTP transport ───────────────────────────────────────────────

class MockTransport(httpx.AsyncBaseTransport):
    """Returns canned responses for testing."""

    def __init__(self, responses: dict[str, tuple[int, dict]] | None = None):
        self._responses = responses or {}
        self._calls: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self._calls.append(request)
        url = str(request.url)

        # Find matching response
        for pattern, (status, body) in self._responses.items():
            if pattern in url:
                return httpx.Response(
                    status_code=status,
                    json=body,
                    headers={"content-type": "application/json"},
                )

        # Default: 200 OK with echo
        return httpx.Response(
            status_code=200,
            json={"echo": True, "url": url, "method": request.method},
            headers={"content-type": "application/json"},
        )


def _make_resolver(
    responses: dict[str, tuple[int, dict]] | None = None,
    service_registry: dict[str, str] | None = None,
) -> HttpTaskResolver:
    """Create a resolver with a mocked HTTP client."""
    resolver = HttpTaskResolver(service_registry=service_registry or {})
    transport = MockTransport(responses or {})
    resolver._client = httpx.AsyncClient(transport=transport)
    return resolver


# ── can_handle tests ──────────────────────────────────────────────────

class TestCanHandle:

    @pytest.mark.asyncio
    async def test_handles_https_service_task(self):
        resolver = HttpTaskResolver()
        task = ServiceTask(id="t1", implementation="https://api.example.com/orders")
        assert await resolver.can_handle(task) is True

    @pytest.mark.asyncio
    async def test_handles_http_service_task(self):
        resolver = HttpTaskResolver()
        task = ServiceTask(id="t1", implementation="http://localhost:3000/webhook")
        assert await resolver.can_handle(task) is True

    @pytest.mark.asyncio
    async def test_handles_helix_uri(self):
        resolver = HttpTaskResolver()
        task = ServiceTask(id="t1", implementation="helix://order-service/validate")
        assert await resolver.can_handle(task) is True

    @pytest.mark.asyncio
    async def test_handles_send_task(self):
        resolver = HttpTaskResolver()
        task = SendTask(id="t1", implementation="https://api.example.com/notify")
        assert await resolver.can_handle(task) is True

    @pytest.mark.asyncio
    async def test_rejects_user_task(self):
        resolver = HttpTaskResolver()
        task = UserTask(id="t1", form_key="forms/review")
        assert await resolver.can_handle(task) is False

    @pytest.mark.asyncio
    async def test_rejects_no_implementation(self):
        resolver = HttpTaskResolver()
        task = ServiceTask(id="t1", implementation=None)
        assert await resolver.can_handle(task) is False

    @pytest.mark.asyncio
    async def test_rejects_non_http_implementation(self):
        resolver = HttpTaskResolver()
        task = ServiceTask(id="t1", implementation="grpc://some-service")
        assert await resolver.can_handle(task) is False


# ── resolve tests (with mocked HTTP) ─────────────────────────────────

class TestResolve:

    @pytest.mark.asyncio
    async def test_basic_http_call(self):
        resolver = _make_resolver(responses={
            "api.example.com/orders": (200, {"order_id": "123", "status": "confirmed"}),
        })

        task = ServiceTask(id="create_order", implementation="https://api.example.com/orders")
        result = await resolver.resolve(task, {"customer": "Alice"})

        assert result["_result_create_order"]["order_id"] == "123"
        assert result["_http_status_create_order"] == 200

    @pytest.mark.asyncio
    async def test_custom_result_variable(self):
        resolver = _make_resolver(responses={
            "api.example.com": (200, {"valid": True}),
        })

        task = ServiceTask(
            id="validate",
            implementation="https://api.example.com/validate",
            extensions={"helix:resultVariable": "validation_result"},
        )
        result = await resolver.resolve(task, {})

        assert "validation_result" in result
        assert result["validation_result"]["valid"] is True

    @pytest.mark.asyncio
    async def test_custom_method(self):
        resolver = _make_resolver()

        task = ServiceTask(
            id="get_data",
            implementation="https://api.example.com/data",
            extensions={"helix:method": "GET"},
        )
        result = await resolver.resolve(task, {})

        # Check the mock recorded a GET request
        assert resolver._client._transport._calls[0].method == "GET"

    @pytest.mark.asyncio
    async def test_variable_substitution_in_url(self):
        resolver = _make_resolver()

        task = ServiceTask(
            id="get_order",
            implementation="https://api.example.com/orders/${order_id}",
            extensions={"helix:method": "GET"},
        )
        result = await resolver.resolve(task, {"order_id": "456"})

        called_url = str(resolver._client._transport._calls[0].url)
        assert "456" in called_url

    @pytest.mark.asyncio
    async def test_variable_substitution_in_body(self):
        resolver = _make_resolver(responses={
            "api.example.com": (200, {"sent": True}),
        })

        task = ServiceTask(
            id="notify",
            implementation="https://api.example.com/notify",
            extensions={
                "helix:body": '{"message": "Hello ${name}", "email": "${email}"}',
            },
        )
        result = await resolver.resolve(task, {"name": "Utpal", "email": "u@helix.dev"})

        # Request was made with substituted body
        request = resolver._client._transport._calls[0]
        body = json.loads(request.content.decode())
        assert body["message"] == "Hello Utpal"
        assert body["email"] == "u@helix.dev"

    @pytest.mark.asyncio
    async def test_helix_uri_resolution(self):
        resolver = _make_resolver(
            service_registry={"order-service": "http://localhost:3001"},
        )

        task = ServiceTask(
            id="validate",
            implementation="helix://order-service/validate",
        )
        result = await resolver.resolve(task, {})

        called_url = str(resolver._client._transport._calls[0].url)
        assert "localhost:3001/validate" in called_url

    @pytest.mark.asyncio
    async def test_helix_uri_unknown_service(self):
        resolver = _make_resolver(service_registry={})

        task = ServiceTask(
            id="call",
            implementation="helix://unknown-service/endpoint",
        )
        result = await resolver.resolve(task, {})

        # Should fall back to localhost:8080
        called_url = str(resolver._client._transport._calls[0].url)
        assert "localhost:8080/endpoint" in called_url

    @pytest.mark.asyncio
    async def test_http_error_status(self):
        resolver = _make_resolver(responses={
            "api.example.com": (500, {"error": "internal"}),
        })

        task = ServiceTask(id="fail", implementation="https://api.example.com/fail")
        result = await resolver.resolve(task, {})

        assert result["_http_status_fail"] == 500
        assert result["_result_fail"]["error"] == "internal"

    @pytest.mark.asyncio
    async def test_custom_headers(self):
        resolver = _make_resolver()

        task = ServiceTask(
            id="auth_call",
            implementation="https://api.example.com/secure",
            extensions={
                "helix:headers": '{"Authorization": "Bearer ${token}"}',
            },
        )
        result = await resolver.resolve(task, {"token": "abc123"})

        request = resolver._client._transport._calls[0]
        assert request.headers["authorization"] == "Bearer abc123"

    @pytest.mark.asyncio
    async def test_default_headers(self):
        resolver = _make_resolver()
        resolver._default_headers = {"X-API-Key": "secret"}

        task = ServiceTask(id="t1", implementation="https://api.example.com/data")
        result = await resolver.resolve(task, {})

        request = resolver._client._transport._calls[0]
        assert request.headers["x-api-key"] == "secret"


# ── URL resolution tests ─────────────────────────────────────────────

class TestUrlResolution:

    def test_https_url_passthrough(self):
        resolver = HttpTaskResolver()
        url = resolver._resolve_url("https://api.example.com/path", {})
        assert url == "https://api.example.com/path"

    def test_helix_uri_with_registry(self):
        resolver = HttpTaskResolver(service_registry={
            "my-service": "http://svc.internal:8080",
        })
        url = resolver._resolve_url("helix://my-service/api/v1/items", {})
        assert url == "http://svc.internal:8080/api/v1/items"

    def test_variable_substitution(self):
        resolver = HttpTaskResolver()
        url = resolver._resolve_url(
            "https://api.example.com/users/${user_id}/orders",
            {"user_id": "42"},
        )
        assert url == "https://api.example.com/users/42/orders"

    def test_missing_variable_kept(self):
        resolver = HttpTaskResolver()
        url = resolver._resolve_url(
            "https://api.example.com/${missing}",
            {},
        )
        assert url == "https://api.example.com/${missing}"
