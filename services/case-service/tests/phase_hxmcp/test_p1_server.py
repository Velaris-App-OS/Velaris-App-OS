"""HxNexus Operator (MCP) P1 — protocol, tool registry, and authz tests.

Copyright (c) 2024-2026 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
from tests.conftest import deploy_case_type, create_case

MCP = "/api/v1/mcp"


def rpc(method: str, params: dict | None = None, msg_id: int | str | None = 1) -> dict:
    msg: dict = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    if msg_id is not None:
        msg["id"] = msg_id
    return msg


def call(name: str, arguments: dict | None = None) -> dict:
    return rpc("tools/call", {"name": name, "arguments": arguments or {}})


def tool_payload(body: dict) -> dict:
    """Extract the JSON payload from a CallToolResult."""
    import json
    assert "result" in body, body
    return json.loads(body["result"]["content"][0]["text"])


def _limited_headers(roles: list[str]) -> dict:
    from case_service.auth.jwt_handler import create_dev_token
    from case_service.config import get_settings

    s = get_settings()
    token = create_dev_token(
        user_id=str(uuid.uuid4()), username="mcp-limited", roles=roles,
        secret=s.auth_secret, private_key=s.auth_rsa_private_key or "",
    )
    return {"Authorization": f"Bearer {token}"}


# ── Protocol ──────────────────────────────────────────────────────────


class TestProtocol:
    async def test_initialize(self, client):
        resp = await client.post(MCP, json=rpc("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "t", "version": "0"},
        }))
        assert resp.status_code == 200
        result = resp.json()["result"]
        assert result["protocolVersion"] == "2025-06-18"
        assert "tools" in result["capabilities"]
        assert result["serverInfo"]["name"] == "velaris-hxnexus-operator"

    async def test_ping(self, client):
        resp = await client.post(MCP, json=rpc("ping"))
        assert resp.json()["result"] == {}

    async def test_notification_gets_202_no_body(self, client):
        resp = await client.post(
            MCP, json=rpc("notifications/initialized", msg_id=None))
        assert resp.status_code == 202
        assert resp.content == b""

    async def test_unknown_method_fails_closed(self, client):
        resp = await client.post(MCP, json=rpc("resources/list"))
        assert resp.json()["error"]["code"] == -32601

    async def test_batch_rejected(self, client):
        resp = await client.post(MCP, json=[rpc("ping"), rpc("ping", msg_id=2)])
        assert resp.json()["error"]["code"] == -32600

    async def test_invalid_json_is_parse_error(self, client):
        resp = await client.post(MCP, content=b"{nope",
                                 headers={"Content-Type": "application/json"})
        assert resp.json()["error"]["code"] == -32700

    async def test_anonymous_401(self, anon_client):
        resp = await anon_client.post(MCP, json=rpc("ping"))
        assert resp.status_code == 401

    async def test_oversized_body_413(self, client):
        big = rpc("tools/call", {"name": "case_search",
                                 "arguments": {"status": "x" * 70_000}})
        resp = await client.post(MCP, json=big)
        assert resp.status_code == 413

    async def test_get_has_no_stream(self, client):
        assert (await client.get(MCP)).status_code == 405


# ── Tool registry ─────────────────────────────────────────────────────


class TestRegistry:
    async def test_tools_list_full_profile(self, client):
        resp = await client.post(MCP, json=rpc("tools/list"))
        tools = {t["name"] for t in resp.json()["result"]["tools"]}
        assert tools == {"case_search", "case_get", "case_timeline",
                         "case_type_list", "case_type_get", "graph_query"}

    async def test_deterministic_profile_hides_and_blocks_ai_tools(self, client):
        from case_service.config import get_settings
        settings = get_settings()
        original = settings.mcp_ai_tools
        settings.mcp_ai_tools = False
        try:
            resp = await client.post(MCP, json=rpc("tools/list"))
            tools = {t["name"] for t in resp.json()["result"]["tools"]}
            assert "graph_query" not in tools and "case_get" in tools
            # a hidden tool is not callable either — default-deny, not cosmetic
            resp = await client.post(MCP, json=call("graph_query", {"question": "hi"}))
            assert resp.json()["error"]["code"] == -32602
        finally:
            settings.mcp_ai_tools = original

    async def test_unknown_tool_fails_closed(self, client):
        resp = await client.post(MCP, json=call("case_delete", {"case_id": "x"}))
        assert resp.json()["error"]["code"] == -32602

    async def test_bad_uuid_is_invalid_params(self, client):
        resp = await client.post(MCP, json=call("case_get", {"case_id": "not-a-uuid"}))
        assert resp.json()["error"]["code"] == -32602

    async def test_limit_cap_enforced(self, client):
        resp = await client.post(MCP, json=call("case_search", {"limit": 500}))
        assert resp.json()["error"]["code"] == -32602


# ── Tools against real data ───────────────────────────────────────────


class TestTools:
    async def test_case_search_and_get_roundtrip(self, client):
        ct = await deploy_case_type(client, name="MCP CT")
        case = await create_case(client, ct["id"], data={"amount": 5})

        found = tool_payload((await client.post(
            MCP, json=call("case_search", {"case_type_id": ct["id"]}))).json())
        assert found["total"] == 1
        assert found["items"][0]["id"] == case["id"]
        assert "data" not in found["items"][0]      # search stays lean

        got = tool_payload((await client.post(
            MCP, json=call("case_get", {"case_id": case["id"]}))).json())
        assert got["id"] == case["id"]
        assert got["data"]["amount"] == 5           # variables only on case_get

    async def test_case_timeline(self, client):
        ct = await deploy_case_type(client, name="MCP Timeline CT")
        case = await create_case(client, ct["id"])
        payload = tool_payload((await client.post(
            MCP, json=call("case_timeline", {"case_id": case["id"]}))).json())
        assert isinstance(payload["items"], list)

    async def test_case_type_list_and_get(self, client):
        ct = await deploy_case_type(client, name="MCP Types CT")
        listed = tool_payload((await client.post(
            MCP, json=call("case_type_list"))).json())
        assert any(i["id"] == ct["id"] for i in listed["items"])
        assert all("definition_json" not in i for i in listed["items"])

        got = tool_payload((await client.post(
            MCP, json=call("case_type_get", {"case_type_id": ct["id"]}))).json())
        assert got["id"] == ct["id"] and "definition_json" in got

    async def test_missing_case_is_tool_error_404(self, client):
        resp = await client.post(
            MCP, json=call("case_get", {"case_id": str(uuid.uuid4())}))
        body = resp.json()
        assert body["result"]["isError"] is True
        assert tool_payload(body)["status"] == 404


# ── Confused-deputy defence ───────────────────────────────────────────


class TestAuthz:
    async def test_enforce_mode_denied_case_reads_404_not_oracle(self, client, monkeypatch):
        """An unrelated user gets the same anti-probing 404 through MCP as
        through REST — for detail AND timeline."""
        from case_service.config import get_settings
        monkeypatch.setattr(get_settings(), "hxguard_case_enforcement", "enforce")

        ct = await deploy_case_type(client, name="MCP Authz CT")
        case = await create_case(client, ct["id"])       # owned by admin fixture
        hdrs = _limited_headers(["user"])

        for tool in ("case_get", "case_timeline"):
            body = (await client.post(
                MCP, json=call(tool, {"case_id": case["id"]}), headers=hdrs)).json()
            assert body["result"]["isError"] is True, tool
            payload = tool_payload(body)
            assert payload["status"] == 404, tool        # never 403 — no oracle

    async def test_enforce_mode_list_is_relationship_filtered(self, client, monkeypatch):
        from case_service.config import get_settings
        monkeypatch.setattr(get_settings(), "hxguard_case_enforcement", "enforce")

        ct = await deploy_case_type(client, name="MCP Filter CT")
        await create_case(client, ct["id"])
        hdrs = _limited_headers(["user"])
        found = tool_payload((await client.post(
            MCP, json=call("case_search", {"case_type_id": ct["id"]}),
            headers=hdrs)).json())
        assert found["total"] == 0                       # unrelated user sees nothing

    async def test_rate_limit_429(self, client, monkeypatch):
        from case_service.api.routers import hxmcp as transport
        from case_service.hxnexus.guard import _RateLimiter
        monkeypatch.setattr(transport, "_rate_limiter",
                            _RateLimiter(max_calls=2, window_seconds=60))
        hdrs = _limited_headers(["user"])                # fresh user id
        assert (await client.post(MCP, json=rpc("ping"), headers=hdrs)).status_code == 200
        assert (await client.post(MCP, json=rpc("ping"), headers=hdrs)).status_code == 200
        resp = await client.post(MCP, json=rpc("ping"), headers=hdrs)
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers
