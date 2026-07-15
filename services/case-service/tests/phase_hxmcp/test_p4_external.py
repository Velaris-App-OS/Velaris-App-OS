"""HxNexus Operator (MCP) P4 — external agents via scoped tokens.

Delegation-of-self: a scoped token restricts the grantor's own authority
(scope ⊆ visible tools), is valid ONLY on POST /api/v1/mcp, and dies
instantly on grant revocation or the master kill switch.

Copyright (c) 2024-2026 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
from tests.phase_hxmcp.test_p1_server import MCP, rpc, call, tool_payload, _limited_headers

TOKENS = "/api/v1/mcp/tokens"


@pytest.fixture
def _ext_on(monkeypatch):
    from case_service.config import get_settings
    monkeypatch.setattr(get_settings(), "mcp_external_tokens_enabled", True)
    return get_settings()


async def _mint(client, tools, **kw) -> dict:
    resp = await client.post(TOKENS, json={"tools": tools, **kw})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _hdr(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── Kill switch / opt-in ──────────────────────────────────────────────


class TestKillSwitch:
    async def test_mint_403_when_disabled(self, client):
        resp = await client.post(TOKENS, json={"tools": ["case_search"]})
        assert resp.status_code == 403

    async def test_outstanding_token_dies_when_disabled(self, client, _ext_on, monkeypatch):
        minted = await _mint(client, ["case_search"])
        from case_service.config import get_settings
        monkeypatch.setattr(get_settings(), "mcp_external_tokens_enabled", False)
        resp = await client.post(MCP, json=rpc("ping"), headers=_hdr(minted["token"]))
        assert resp.status_code == 401


# ── Minting rules ─────────────────────────────────────────────────────


class TestMinting:
    async def test_mint_requires_role(self, client, _ext_on):
        resp = await client.post(TOKENS, json={"tools": ["case_search"]},
                                 headers=_limited_headers(["user"]))
        assert resp.status_code == 403

    async def test_mint_returns_token_and_grant(self, client, _ext_on):
        minted = await _mint(client, ["case_search", "case_get"], label="test agent")
        assert minted["token"]
        assert minted["grant"]["tools"] == ["case_get", "case_search"]
        assert minted["grant"]["label"] == "test agent"
        assert minted["grant"]["revoked"] is False

    async def test_unknown_and_disabled_tools_not_grantable(self, client, _ext_on):
        # nonexistent tool
        r1 = await client.post(TOKENS, json={"tools": ["drop_database"]})
        assert r1.status_code == 400
        # real tool, but writes are globally OFF — identical wording (anti-oracle)
        r2 = await client.post(TOKENS, json={"tools": ["case_update_data"]})
        assert r2.status_code == 400
        assert r1.json()["detail"].split(":")[0] == r2.json()["detail"].split(":")[0]

    async def test_ttl_capped_at_max(self, client, _ext_on):
        from datetime import datetime, timedelta, timezone
        from case_service.config import get_settings
        minted = await _mint(client, ["case_search"], ttl_minutes=100000)
        exp = datetime.fromisoformat(minted["grant"]["expires_at"])
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        cap = datetime.now(timezone.utc) + timedelta(
            minutes=get_settings().mcp_token_max_ttl_minutes + 1)
        assert exp < cap

    async def test_list_own_grants_only(self, client, _ext_on):
        minted = await _mint(client, ["case_search"], label="mine")
        listed = (await client.get(TOKENS)).json()["items"]
        assert any(g["id"] == minted["grant"]["id"] for g in listed)
        # another user sees nothing of it (fresh uuid identity)
        other = (await client.get(TOKENS, headers=_limited_headers(["user"]))).json()["items"]
        assert all(g["id"] != minted["grant"]["id"] for g in other)


# ── Scoped-token surface on /mcp ──────────────────────────────────────


class TestScopedSurface:
    async def test_tools_list_is_granted_intersection(self, client, _ext_on):
        minted = await _mint(client, ["case_search", "case_get"])
        listed = (await client.post(MCP, json=rpc("tools/list"),
                                    headers=_hdr(minted["token"]))).json()
        names = {t["name"] for t in listed["result"]["tools"]}
        assert names == {"case_search", "case_get"}

    async def test_in_scope_call_works_as_grantor(self, client, _ext_on):
        minted = await _mint(client, ["case_type_list"])
        resp = await client.post(MCP, json=call("case_type_list", {"limit": 5}),
                                 headers=_hdr(minted["token"]))
        out = tool_payload(resp.json())
        assert "items" in out

    async def test_out_of_scope_call_unknown_tool(self, client, _ext_on):
        minted = await _mint(client, ["case_search"])
        resp = await client.post(MCP, json=call("case_type_list", {}),
                                 headers=_hdr(minted["token"]))
        err = resp.json()["error"]
        assert err["code"] == -32602 and "unknown tool" in err["message"]

    async def test_scope_cannot_resurface_disabled_tool(self, client, _ext_on, monkeypatch):
        # grant minted while writes were ON, then writes get switched OFF:
        # the intersection must drop the write tool even though it's in scope
        from case_service.config import get_settings
        monkeypatch.setattr(get_settings(), "mcp_writes_enabled", True)
        minted = await _mint(client, ["case_update_data", "case_search"])
        monkeypatch.setattr(get_settings(), "mcp_writes_enabled", False)
        listed = (await client.post(MCP, json=rpc("tools/list"),
                                    headers=_hdr(minted["token"]))).json()
        names = {t["name"] for t in listed["result"]["tools"]}
        assert names == {"case_search"}


# ── Single-purpose token: rejected everywhere else ────────────────────


class TestSinglePurpose:
    async def test_scoped_token_rejected_on_rest(self, client, _ext_on):
        minted = await _mint(client, ["case_search"])
        resp = await client.get("/api/v1/case-types", headers=_hdr(minted["token"]))
        assert resp.status_code == 401

    async def test_scoped_token_cannot_touch_proposals(self, client, _ext_on):
        minted = await _mint(client, ["case_search"])
        resp = await client.get("/api/v1/mcp/proposals", headers=_hdr(minted["token"]))
        assert resp.status_code == 401
        resp = await client.post(f"/api/v1/mcp/proposals/{uuid.uuid4()}/confirm",
                                 headers=_hdr(minted["token"]))
        assert resp.status_code == 401

    async def test_scoped_token_cannot_mint_or_revoke(self, client, _ext_on):
        minted = await _mint(client, ["case_search"])
        resp = await client.post(TOKENS, json={"tools": ["case_search"]},
                                 headers=_hdr(minted["token"]))
        assert resp.status_code == 401
        resp = await client.post(f"{TOKENS}/{minted['grant']['id']}/revoke",
                                 headers=_hdr(minted["token"]))
        assert resp.status_code == 401


# ── Revocation + expiry (server-side truth beats JWT validity) ────────


class TestRevocation:
    async def test_revoke_is_instant(self, client, _ext_on):
        minted = await _mint(client, ["case_search"])
        assert (await client.post(MCP, json=rpc("ping"),
                                  headers=_hdr(minted["token"]))).status_code == 200
        r = await client.post(f"{TOKENS}/{minted['grant']['id']}/revoke")
        assert r.status_code == 200 and r.json()["status"] == "revoked"
        assert (await client.post(MCP, json=rpc("ping"),
                                  headers=_hdr(minted["token"]))).status_code == 401

    async def test_revoke_foreign_grant_404(self, client, _ext_on):
        minted = await _mint(client, ["case_search"])
        resp = await client.post(f"{TOKENS}/{minted['grant']['id']}/revoke",
                                 headers=_limited_headers(["user"]))
        assert resp.status_code == 404      # anti-oracle

    async def test_expired_grant_rejected(self, client, _ext_on, session):
        from datetime import timedelta
        from sqlalchemy import update
        from case_service.db.models import MCPTokenGrantModel, _utcnow

        minted = await _mint(client, ["case_search"])
        await session.execute(
            update(MCPTokenGrantModel)
            .where(MCPTokenGrantModel.id == uuid.UUID(minted["grant"]["id"]))
            .values(expires_at=_utcnow() - timedelta(minutes=1))
        )
        await session.commit()
        assert (await client.post(MCP, json=rpc("ping"),
                                  headers=_hdr(minted["token"]))).status_code == 401


# ── Per-grant rate limit ──────────────────────────────────────────────


class TestExternalRateLimit:
    async def test_per_jti_budget_429(self, client, _ext_on, monkeypatch):
        from case_service.api.routers import hxmcp as transport
        from case_service.hxnexus.guard import _RateLimiter
        monkeypatch.setattr(transport, "_ext_rate_limiter",
                            _RateLimiter(max_calls=3, window_seconds=60))
        minted = await _mint(client, ["case_search"])
        hdrs = _hdr(minted["token"])
        for _ in range(3):
            assert (await client.post(MCP, json=rpc("ping"), headers=hdrs)).status_code == 200
        resp = await client.post(MCP, json=rpc("ping"), headers=hdrs)
        assert resp.status_code == 429
        # the grantor's own session budget is untouched by the agent's burn
        assert (await client.post(MCP, json=rpc("ping"))).status_code == 200
