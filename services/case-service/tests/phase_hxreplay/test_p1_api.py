"""HxReplay Phase B — /api/v1/hxreplay endpoints (single-case runs)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from tests.conftest import create_case, deploy_case_type


async def _make_case(client) -> tuple[str, str]:
    ct = await deploy_case_type(client, name=f"Replay CT {uuid.uuid4().hex[:6]}")
    case = await create_case(client, ct["id"])
    return ct["id"], case["id"]


_CANDIDATE = {"rules": [{
    "id": "replay-auto", "name": "replay-auto", "rule_type": "when", "enabled": True,
    "definition_json": {
        "conditions": [{"field_path": "claim.amount", "operator": "lt", "value": 500}],
        "actions": [{"action_type": "auto_approve"}],
    },
}]}


@pytest.mark.asyncio
async def test_create_single_run_and_fetch(client):
    _, case_id = await _make_case(client)
    resp = await client.post("/api/v1/hxreplay/runs",
                             json={"kind": "single", "case_id": case_id,
                                   "candidate": _CANDIDATE})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "complete" and body["kind"] == "single"
    # the added rule reads claim.amount, absent on this case → deterministic
    # None → never fires → no divergence (or indeterminate is NEVER silent)
    assert body["result"]["determinacy"] in ("determinate", "indeterminate")
    assert body["summary"]["assumption"].startswith("exogenous")

    run_id = body["id"]
    assert (await client.get(f"/api/v1/hxreplay/runs/{run_id}")).status_code == 200
    results = (await client.get(f"/api/v1/hxreplay/runs/{run_id}/results")).json()["results"]
    assert len(results) == 1 and results[0]["case_id"] == case_id
    detail = await client.get(f"/api/v1/hxreplay/runs/{run_id}/results/{case_id}")
    assert detail.status_code == 200 and "trace" in detail.json()
    listed = (await client.get("/api/v1/hxreplay/runs")).json()["runs"]
    assert any(r["id"] == run_id for r in listed)


@pytest.mark.asyncio
async def test_cohort_kind_rejected_in_p1(client):
    resp = await client.post("/api/v1/hxreplay/runs",
                             json={"kind": "cohort", "case_id": str(uuid.uuid4())})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_missing_case_and_bad_ids_404(client):
    r = await client.post("/api/v1/hxreplay/runs",
                          json={"kind": "single", "case_id": str(uuid.uuid4())})
    assert r.status_code == 404
    r = await client.post("/api/v1/hxreplay/runs",
                          json={"kind": "single", "case_id": "not-a-uuid"})
    assert r.status_code == 404
    assert (await client.get("/api/v1/hxreplay/runs/not-a-uuid")).status_code == 404
    assert (await client.get(f"/api/v1/hxreplay/runs/{uuid.uuid4()}")).status_code == 404


@pytest.mark.asyncio
async def test_bad_candidate_shape_fails_run(client):
    _, case_id = await _make_case(client)
    resp = await client.post("/api/v1/hxreplay/runs",
                             json={"kind": "single", "case_id": case_id,
                                   "candidate": {"rules": [{"rule_type": "when"}]}})
    assert resp.status_code == 400
    assert "'id' or 'name'" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_missing_branch_fails_run(client):
    _, case_id = await _make_case(client)
    resp = await client.post("/api/v1/hxreplay/runs",
                             json={"kind": "single", "case_id": case_id,
                                   "branch_id": str(uuid.uuid4())})
    assert resp.status_code == 400
    assert "Branch not found" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_delete_run_cascades(client):
    _, case_id = await _make_case(client)
    run_id = (await client.post("/api/v1/hxreplay/runs",
                                json={"kind": "single", "case_id": case_id,
                                      "candidate": _CANDIDATE})).json()["id"]
    assert (await client.delete(f"/api/v1/hxreplay/runs/{run_id}")).status_code == 204
    assert (await client.get(f"/api/v1/hxreplay/runs/{run_id}")).status_code == 404


@pytest.mark.asyncio
async def test_anonymous_rejected(anon_client):
    resp = await anon_client.get("/api/v1/hxreplay/runs")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_candidate_rule_caps(client):
    _, case_id = await _make_case(client)
    too_many = {"rules": [{"id": f"r{i}", "rule_type": "when",
                           "definition_json": {"conditions": [], "actions": []}}
                          for i in range(51)]}
    r = await client.post("/api/v1/hxreplay/runs",
                          json={"kind": "single", "case_id": case_id, "candidate": too_many})
    assert r.status_code == 400 and "Too many candidate rules" in r.json()["detail"]
    fat = {"rules": [{"id": "r", "rule_type": "when", "definition_json": {
        "conditions": [{"field_path": "x", "operator": "eq", "value": 1}] * 51, "actions": []}}]}
    r = await client.post("/api/v1/hxreplay/runs",
                          json={"kind": "single", "case_id": case_id, "candidate": fat})
    assert r.status_code == 400 and "too large" in r.json()["detail"]
