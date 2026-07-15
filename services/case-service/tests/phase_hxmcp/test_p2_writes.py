"""HxNexus Operator (MCP) P2 — low-risk writes + idempotency.

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


@pytest.fixture(autouse=True)
def _enable_writes(monkeypatch):
    """P2 writes are opt-in; turn them on for this module."""
    from case_service.config import get_settings
    monkeypatch.setattr(get_settings(), "mcp_writes_enabled", True)


def wcall(name: str, args: dict, key: str | None = None) -> dict:
    body = dict(args)
    body["idempotency_key"] = key or f"k-{uuid.uuid4()}"
    return call(name, body)


# ── Write profile gating ──────────────────────────────────────────────


class TestWriteProfile:
    async def test_writes_hidden_and_blocked_when_disabled(self, client, monkeypatch):
        from case_service.config import get_settings
        monkeypatch.setattr(get_settings(), "mcp_writes_enabled", False)
        listed = {t["name"] for t in
                  (await client.post(MCP, json=rpc("tools/list"))).json()["result"]["tools"]}
        assert "case_update_data" not in listed and "case_get" in listed
        # hidden write tool is not callable either (default-deny)
        resp = await client.post(MCP, json=wcall("case_set_priority",
                                 {"case_id": str(uuid.uuid4()), "priority": "high"}))
        assert resp.json()["error"]["code"] == -32602

    async def test_writes_visible_when_enabled(self, client):
        listed = {t["name"] for t in
                  (await client.post(MCP, json=rpc("tools/list"))).json()["result"]["tools"]}
        assert {"case_update_data", "case_set_priority", "case_link"} <= listed


# ── The writes themselves ─────────────────────────────────────────────


class TestWrites:
    async def test_update_data_applies_and_audits(self, client):
        ct = await deploy_case_type(client, name="MCP W Update CT")
        case = await create_case(client, ct["id"], data={"amount": 1})
        out = tool_payload((await client.post(MCP, json=wcall(
            "case_update_data", {"case_id": case["id"], "data": {"amount": 9, "note": "hi"}}))).json())
        assert out["data"]["amount"] == 9 and out["data"]["note"] == "hi"
        # persisted (read back through a fresh read tool)
        got = tool_payload((await client.post(
            MCP, json=call("case_get", {"case_id": case["id"]}))).json())
        assert got["data"]["amount"] == 9

    async def test_set_priority(self, client):
        ct = await deploy_case_type(client, name="MCP W Prio CT")
        case = await create_case(client, ct["id"])
        out = tool_payload((await client.post(MCP, json=wcall(
            "case_set_priority", {"case_id": case["id"], "priority": "critical"}))).json())
        assert out["priority"] == "critical"

    async def test_set_priority_rejects_bad_value(self, client):
        ct = await deploy_case_type(client, name="MCP W BadPrio CT")
        case = await create_case(client, ct["id"])
        resp = await client.post(MCP, json=wcall(
            "case_set_priority", {"case_id": case["id"], "priority": "urgent"}))
        assert resp.json()["error"]["code"] == -32602

    async def test_case_link(self, client):
        ct = await deploy_case_type(client, name="MCP W Link CT")
        a = await create_case(client, ct["id"])
        b = await create_case(client, ct["id"])
        out = tool_payload((await client.post(MCP, json=wcall(
            "case_link", {"case_id": a["id"], "target_case_id": b["id"],
                          "relationship_type": "duplicate"}))).json())
        assert out["relationship_type"] == "duplicate"

    async def test_missing_idempotency_key_rejected(self, client):
        ct = await deploy_case_type(client, name="MCP W NoKey CT")
        case = await create_case(client, ct["id"])
        # call() without a key
        resp = await client.post(MCP, json=call(
            "case_set_priority", {"case_id": case["id"], "priority": "high"}))
        assert resp.json()["error"]["code"] == -32602
        assert "idempotency_key" in resp.json()["error"]["message"]


# ── Idempotency ───────────────────────────────────────────────────────


class TestIdempotency:
    async def test_same_key_replays_not_reapplies(self, client):
        ct = await deploy_case_type(client, name="MCP Idem Replay CT")
        case = await create_case(client, ct["id"], data={"n": 0})
        key = f"k-{uuid.uuid4()}"
        first = tool_payload((await client.post(MCP, json=call(
            "case_update_data",
            {"case_id": case["id"], "data": {"n": 5}, "idempotency_key": key}))).json())
        assert first["data"]["n"] == 5
        # a concurrent overwrite via a DIFFERENT key
        await client.post(MCP, json=wcall(
            "case_update_data", {"case_id": case["id"], "data": {"n": 99}}))
        # retry with the ORIGINAL key returns the ORIGINAL stored result,
        # and does NOT re-apply n=5 over the current n=99
        replay = tool_payload((await client.post(MCP, json=call(
            "case_update_data",
            {"case_id": case["id"], "data": {"n": 5}, "idempotency_key": key}))).json())
        assert replay["data"]["n"] == 5             # stored response echoed
        now = tool_payload((await client.post(
            MCP, json=call("case_get", {"case_id": case["id"]}))).json())
        assert now["data"]["n"] == 99               # write was NOT re-applied

    async def test_same_key_different_args_conflict(self, client):
        ct = await deploy_case_type(client, name="MCP Idem Conflict CT")
        case = await create_case(client, ct["id"])
        key = f"k-{uuid.uuid4()}"
        await client.post(MCP, json=call("case_set_priority",
            {"case_id": case["id"], "priority": "high", "idempotency_key": key}))
        resp = await client.post(MCP, json=call("case_set_priority",
            {"case_id": case["id"], "priority": "low", "idempotency_key": key}))
        assert resp.json()["error"]["code"] == -32602
        assert "different arguments" in resp.json()["error"]["message"]

    async def test_failed_write_releases_key_for_retry(self, client):
        """A write that applied NOTHING must not burn its idempotency key — a
        legit retry with the SAME (user, key) has to re-run. Proven via a
        bad-args failure then a valid retry on the same key + same client, so
        this exercises release, not a fresh user namespace."""
        ct = await deploy_case_type(client, name="MCP Idem Release CT")
        case = await create_case(client, ct["id"])
        key = f"k-{uuid.uuid4()}"
        # invalid priority → ToolArgError AFTER the claim → claim released
        bad = await client.post(MCP, json=call("case_set_priority",
            {"case_id": case["id"], "priority": "urgent", "idempotency_key": key}))
        assert bad.json()["error"]["code"] == -32602
        # same user, same key, valid args: without release this would hit a
        # pending/conflict; instead it re-runs and succeeds
        ok = tool_payload((await client.post(MCP, json=call("case_set_priority",
            {"case_id": case["id"], "priority": "high", "idempotency_key": key}))).json())
        assert ok["priority"] == "high"

    async def test_denied_write_releases_key(self, client):
        """An authz-denied write (isError tool result, not a protocol error)
        also releases its key — the handler rolled back, nothing applied. A
        same-user same-key retry re-runs (still denied) rather than replaying
        a stored success or hitting an in-flight conflict."""
        from case_service.config import get_settings
        get_settings().hxguard_case_enforcement = "enforce"
        try:
            ct = await deploy_case_type(client, name="MCP Idem Denied CT")
            case = await create_case(client, ct["id"])          # owned by admin
            key = f"k-{uuid.uuid4()}"
            hdrs = _limited_headers(["user"])
            denied = await client.post(MCP, json=call("case_set_priority",
                {"case_id": case["id"], "priority": "high", "idempotency_key": key}),
                headers=hdrs)
            assert denied.json()["result"]["isError"] is True
            retry = await client.post(MCP, json=call("case_set_priority",
                {"case_id": case["id"], "priority": "high", "idempotency_key": key}),
                headers=hdrs)
            assert retry.json()["result"]["isError"] is True
            assert tool_payload(retry.json())["status"] == 404
        finally:
            get_settings().hxguard_case_enforcement = "shadow"


# ── Confused-deputy on writes ─────────────────────────────────────────


class TestWriteAuthz:
    async def test_unrelated_user_cannot_write(self, client):
        from case_service.config import get_settings
        get_settings().hxguard_case_enforcement = "enforce"
        try:
            ct = await deploy_case_type(client, name="MCP W Authz CT")
            case = await create_case(client, ct["id"])
            hdrs = _limited_headers(["user"])
            for c in (
                call("case_update_data", {"case_id": case["id"], "data": {"x": 1},
                                          "idempotency_key": f"k-{uuid.uuid4()}"}),
                call("case_set_priority", {"case_id": case["id"], "priority": "high",
                                           "idempotency_key": f"k-{uuid.uuid4()}"}),
            ):
                body = (await client.post(MCP, json=c, headers=hdrs)).json()
                assert body["result"]["isError"] is True
                assert tool_payload(body)["status"] == 404      # anti-oracle
        finally:
            get_settings().hxguard_case_enforcement = "shadow"
