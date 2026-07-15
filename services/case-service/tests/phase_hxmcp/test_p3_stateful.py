"""HxNexus Operator (MCP) P3 — stateful actions + human-in-the-loop confirm.

Copyright (c) 2024-2026 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
from tests.conftest import deploy_case_type, create_case
from tests.phase_hxmcp.test_p1_server import (
    MCP, rpc, call, tool_payload, _limited_headers,
)


@pytest.fixture
def _stateful_on(monkeypatch):
    from case_service.config import get_settings
    monkeypatch.setattr(get_settings(), "mcp_stateful_enabled", True)
    return get_settings()


def scall(name: str, args: dict, key: str | None = None) -> dict:
    body = dict(args)
    body["idempotency_key"] = key or f"k-{uuid.uuid4()}"
    return call(name, body)


# ── Profile gating ────────────────────────────────────────────────────


class TestStatefulProfile:
    async def test_stateful_hidden_and_blocked_when_disabled(self, client):
        listed = {t["name"] for t in
                  (await client.post(MCP, json=rpc("tools/list"))).json()["result"]["tools"]}
        assert not ({"case_advance_stage", "case_set_status", "case_close",
                     "case_create"} & listed)
        resp = await client.post(MCP, json=scall("case_close", {"case_id": str(uuid.uuid4())}))
        assert resp.json()["error"]["code"] == -32602

    async def test_stateful_independent_of_writes_flag(self, client, _stateful_on, monkeypatch):
        # stateful on, P2 writes OFF: stateful tools show, P2 writes don't
        from case_service.config import get_settings
        monkeypatch.setattr(get_settings(), "mcp_writes_enabled", False)
        listed = {t["name"] for t in
                  (await client.post(MCP, json=rpc("tools/list"))).json()["result"]["tools"]}
        assert {"case_advance_stage", "case_close"} <= listed
        assert "case_update_data" not in listed


# ── Human-confirm proposal flow (default) ─────────────────────────────


class TestProposalFlow:
    async def test_stateful_call_creates_proposal_not_execution(self, client, _stateful_on):
        ct = await deploy_case_type(client, name="MCP P3 Propose CT")
        case = await create_case(client, ct["id"])
        out = tool_payload((await client.post(MCP, json=scall(
            "case_set_status", {"case_id": case["id"], "status": "on_hold"}))).json())
        assert out["requires_confirmation"] is True
        assert out["proposal_id"] and out["tool"] == "case_set_status"
        # not applied yet
        got = tool_payload((await client.post(
            MCP, json=call("case_get", {"case_id": case["id"]}))).json())
        assert got["status"] != "on_hold"

    async def test_confirm_executes_the_action(self, client, _stateful_on):
        ct = await deploy_case_type(client, name="MCP P3 Confirm CT")
        case = await create_case(client, ct["id"])
        pid = tool_payload((await client.post(MCP, json=scall(
            "case_set_status", {"case_id": case["id"], "status": "on_hold"}))).json())["proposal_id"]
        # visible in pending list
        pend = (await client.get("/api/v1/mcp/proposals")).json()["items"]
        assert any(p["id"] == pid for p in pend)
        # confirm → executes
        conf = await client.post(f"/api/v1/mcp/proposals/{pid}/confirm")
        assert conf.status_code == 200
        assert conf.json()["result"]["status"] == "on_hold"
        # now applied
        got = tool_payload((await client.post(
            MCP, json=call("case_get", {"case_id": case["id"]}))).json())
        assert got["status"] == "on_hold"

    async def test_double_confirm_409(self, client, _stateful_on):
        ct = await deploy_case_type(client, name="MCP P3 Double CT")
        case = await create_case(client, ct["id"])
        pid = tool_payload((await client.post(MCP, json=scall(
            "case_set_status", {"case_id": case["id"], "status": "on_hold"}))).json())["proposal_id"]
        assert (await client.post(f"/api/v1/mcp/proposals/{pid}/confirm")).status_code == 200
        assert (await client.post(f"/api/v1/mcp/proposals/{pid}/confirm")).status_code == 409

    async def test_reject_prevents_execution(self, client, _stateful_on):
        ct = await deploy_case_type(client, name="MCP P3 Reject CT")
        case = await create_case(client, ct["id"])
        pid = tool_payload((await client.post(MCP, json=scall(
            "case_set_status", {"case_id": case["id"], "status": "on_hold"}))).json())["proposal_id"]
        assert (await client.post(f"/api/v1/mcp/proposals/{pid}/reject")).status_code == 200
        assert (await client.post(f"/api/v1/mcp/proposals/{pid}/confirm")).status_code == 409
        got = tool_payload((await client.post(
            MCP, json=call("case_get", {"case_id": case["id"]}))).json())
        assert got["status"] != "on_hold"

    async def test_confirm_missing_proposal_404(self, client, _stateful_on):
        assert (await client.post(
            f"/api/v1/mcp/proposals/{uuid.uuid4()}/confirm")).status_code == 404


# ── Cross-tenant isolation of proposals ───────────────────────────────
# A proposal's summary embeds argument values, so listing/acting must be
# scoped to the proposer's tenant — never disclosed or tamperable across it.


class TestProposalTenantScoping:
    async def _seed(self, session, tenant: str | None) -> uuid.UUID:
        from datetime import timedelta
        from case_service.db.models import MCPActionProposalModel, _utcnow
        row = MCPActionProposalModel(
            user_id=str(uuid.uuid4()), tenant_id=tenant,
            tool_name="case_set_status",
            arguments_json={"case_id": str(uuid.uuid4()), "status": "on_hold"},
            summary="case_set_status(secret-tenant-data)", status="pending",
            expires_at=_utcnow() + timedelta(minutes=60),
        )
        session.add(row)
        await session.commit()
        return row.id

    async def test_list_pending_isolates_by_tenant(self, session):
        from case_service.hxmcp import proposals as prop
        a = await self._seed(session, "tenant-A")
        b = await self._seed(session, "tenant-B")
        seen_a = {p.id for p in await prop.list_pending(session, "tenant-A")}
        assert a in seen_a and b not in seen_a
        seen_none = {p.id for p in await prop.list_pending(session, None)}
        assert a not in seen_none and b not in seen_none  # NULL matches only NULL

    async def test_tenant_ok_gate(self):
        from types import SimpleNamespace
        from case_service.api.routers.hxmcp import _tenant_ok
        prop = SimpleNamespace(tenant_id="tenant-A")
        assert _tenant_ok(prop, SimpleNamespace(tenant_id="tenant-A"))
        assert not _tenant_ok(prop, SimpleNamespace(tenant_id="tenant-B"))
        assert not _tenant_ok(prop, SimpleNamespace(tenant_id=None))
        assert _tenant_ok(SimpleNamespace(tenant_id=None),
                          SimpleNamespace(tenant_id=None))


# ── Confirm re-checks authorization as the confirmer ──────────────────


class TestConfirmAuthz:
    async def test_confirmer_without_case_rights_cannot_execute(self, client, _stateful_on):
        from case_service.config import get_settings
        get_settings().hxguard_case_enforcement = "enforce"
        try:
            ct = await deploy_case_type(client, name="MCP P3 Authz CT")
            case = await create_case(client, ct["id"])            # admin-owned
            pid = tool_payload((await client.post(MCP, json=scall(
                "case_set_status", {"case_id": case["id"], "status": "on_hold"}))).json())["proposal_id"]
            hdrs = _limited_headers(["user"])
            # unrelated human confirms → handler's case.update denies (404 anti-oracle)
            resp = await client.post(f"/api/v1/mcp/proposals/{pid}/confirm", headers=hdrs)
            assert resp.status_code == 404
            # proposal stays pending → an authorized human can still confirm
            ok = await client.post(f"/api/v1/mcp/proposals/{pid}/confirm")
            assert ok.status_code == 200
        finally:
            get_settings().hxguard_case_enforcement = "shadow"


# ── Immediate execution when confirmation is disabled ─────────────────


class TestNoConfirm:
    async def test_stateful_executes_immediately_when_confirm_off(self, client, _stateful_on, monkeypatch):
        from case_service.config import get_settings
        monkeypatch.setattr(get_settings(), "mcp_confirm_stateful", False)
        ct = await deploy_case_type(client, name="MCP P3 NoConfirm CT")
        case = await create_case(client, ct["id"])
        out = tool_payload((await client.post(MCP, json=scall(
            "case_set_status", {"case_id": case["id"], "status": "on_hold"}))).json())
        assert out["status"] == "on_hold"          # executed directly, no proposal

    async def test_stateful_missing_idempotency_key_rejected(self, client, _stateful_on):
        ct = await deploy_case_type(client, name="MCP P3 NoKey CT")
        case = await create_case(client, ct["id"])
        resp = await client.post(MCP, json=call(
            "case_set_status", {"case_id": case["id"], "status": "on_hold"}))
        assert resp.json()["error"]["code"] == -32602
        assert "idempotency_key" in resp.json()["error"]["message"]
