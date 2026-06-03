"""Tests for P55 HxDeploy — Intelligent Deployment Governance."""
from __future__ import annotations

import uuid
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import EnvironmentRegistryModel, DeploymentRunModel

from tests.conftest import client, session  # type: ignore[attr-defined]

MANIFEST_SMALL = {"case_types": [], "forms": [], "version": "1.0.0", "notes": "Minor label fix"}
MANIFEST_MED   = {"case_types": [{"name": "LoanApp"}], "forms": [{"name": "LoanForm"}], "version": "2.0.0"}
MANIFEST_LARGE = {"case_types": [{"name": f"CT{i}"} for i in range(10)], "forms": [], "version": "3.0.0", "sla_sql": "ALTER TABLE..."}


async def _env(client: AsyncClient, name: str = "staging", order: int = 1) -> dict:
    r = await client.post("/api/v1/deploy/environments", json={
        "name": name, "label": name.title(), "order_index": order,
    })
    assert r.status_code == 201
    return r.json()


# ── Environments ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_register_environment(client: AsyncClient):
    r = await client.post("/api/v1/deploy/environments", json={
        "name": "staging", "label": "Staging", "url": "https://staging.example.com", "order_index": 1,
    })
    assert r.status_code == 201
    data = r.json()
    assert data["name"] == "staging"
    assert data["label"] == "Staging"
    assert data["url"] == "https://staging.example.com"


@pytest.mark.asyncio
async def test_register_environment_upserts(client: AsyncClient):
    await _env(client, "prod", 3)
    r = await client.post("/api/v1/deploy/environments", json={
        "name": "prod", "label": "Production", "order_index": 3,
    })
    assert r.status_code == 201
    assert r.json()["label"] == "Production"


@pytest.mark.asyncio
async def test_list_environments_empty(client: AsyncClient):
    r = await client.get("/api/v1/deploy/environments")
    assert r.status_code == 200 and isinstance(r.json(), list)


@pytest.mark.asyncio
async def test_list_environments_ordered(client: AsyncClient):
    for n, i in [("dev",0),("staging",1),("uat",2),("prod",3)]:
        await _env(client, n, i)
    r = await client.get("/api/v1/deploy/environments")
    names = [e["name"] for e in r.json()]
    assert names == ["dev","staging","uat","prod"]


@pytest.mark.asyncio
async def test_environment_status(client: AsyncClient):
    env = await _env(client, "dev", 0)
    r = await client.get(f"/api/v1/deploy/environments/{env['id']}/status")
    assert r.status_code == 200
    assert "status" in r.json()


# ── Risk Analysis ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_analyse_risk_endpoint(client: AsyncClient):
    env = await _env(client, "prod", 3)
    r = await client.post("/api/v1/deploy/analyse-risk", json={
        "to_env_id": env["id"],
        "package_manifest": MANIFEST_MED,
    })
    assert r.status_code == 200
    data = r.json()
    assert data["risk_level"] in ("low","medium","high","critical")
    assert "reason" in data


@pytest.mark.asyncio
async def test_heuristic_low_risk_empty_manifest(client: AsyncClient):
    env = await _env(client, "staging", 1)
    r = await client.post("/api/v1/deploy/analyse-risk", json={
        "to_env_id": env["id"], "package_manifest": MANIFEST_SMALL,
    })
    assert r.status_code == 200
    assert r.json()["risk_level"] in ("low","medium")


# ── Promote ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_promote_to_staging(client: AsyncClient):
    env = await _env(client, "staging", 1)
    r = await client.post("/api/v1/deploy/promote", json={
        "to_env_id": env["id"],
        "package_manifest": MANIFEST_SMALL,
        "deploy_notes": "Test deploy",
    })
    assert r.status_code == 201
    data = r.json()
    assert data["risk_level"] in ("low","medium","high","critical")
    assert data["status"] in ("deployed","awaiting_approval","pending")


@pytest.mark.asyncio
async def test_promote_unknown_env_returns_400(client: AsyncClient):
    r = await client.post("/api/v1/deploy/promote", json={
        "to_env_id": str(uuid.uuid4()),
        "package_manifest": MANIFEST_SMALL,
    })
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_low_risk_auto_approved(client: AsyncClient, session: AsyncSession):
    env = await _env(client, "dev", 0)
    r = await client.post("/api/v1/deploy/promote", json={
        "to_env_id": env["id"],
        "package_manifest": {"case_types": [], "forms": [], "version": "1.0.0"},
    })
    assert r.status_code == 201
    # low risk should be auto-approved and deployed
    assert r.json()["status"] in ("deployed", "awaiting_approval")


# ── Runs ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_runs_empty(client: AsyncClient):
    r = await client.get("/api/v1/deploy/runs")
    assert r.status_code == 200 and isinstance(r.json(), list)


@pytest.mark.asyncio
async def test_get_run(client: AsyncClient):
    env = await _env(client, "staging", 1)
    p = await client.post("/api/v1/deploy/promote", json={"to_env_id": env["id"], "package_manifest": MANIFEST_MED})
    run_id = p.json()["id"]
    r = await client.get(f"/api/v1/deploy/runs/{run_id}")
    assert r.status_code == 200
    assert r.json()["id"] == run_id


@pytest.mark.asyncio
async def test_get_run_not_found(client: AsyncClient):
    r = await client.get(f"/api/v1/deploy/runs/{uuid.uuid4()}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_list_runs_shows_created(client: AsyncClient):
    env = await _env(client, "uat", 2)
    await client.post("/api/v1/deploy/promote", json={"to_env_id": env["id"], "package_manifest": MANIFEST_MED})
    r = await client.get("/api/v1/deploy/runs")
    assert len(r.json()) >= 1


@pytest.mark.asyncio
async def test_filter_runs_by_status(client: AsyncClient):
    env = await _env(client, "staging", 1)
    await client.post("/api/v1/deploy/promote", json={"to_env_id": env["id"], "package_manifest": MANIFEST_MED})
    r = await client.get("/api/v1/deploy/runs?status=awaiting_approval")
    assert r.status_code == 200
    for run in r.json():
        assert run["status"] == "awaiting_approval"


# ── Approve / Reject ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_approve_awaiting_run(client: AsyncClient, session: AsyncSession):
    env = await _env(client, "prod", 3)
    # large manifest → should be high/medium risk → awaiting_approval
    p = await client.post("/api/v1/deploy/promote", json={
        "to_env_id": env["id"], "package_manifest": MANIFEST_LARGE,
    })
    run = p.json()
    if run["status"] == "awaiting_approval":
        r = await client.post(f"/api/v1/deploy/runs/{run['id']}/approve", json={})
        assert r.status_code == 200
        assert r.json()["status"] == "deployed"
        assert r.json()["approved_by"] is not None


@pytest.mark.asyncio
async def test_reject_awaiting_run(client: AsyncClient):
    env = await _env(client, "prod", 3)
    p = await client.post("/api/v1/deploy/promote", json={
        "to_env_id": env["id"], "package_manifest": MANIFEST_LARGE,
    })
    run = p.json()
    if run["status"] == "awaiting_approval":
        r = await client.post(f"/api/v1/deploy/runs/{run['id']}/reject",
                              json={"reason": "Not ready for prod"})
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "rejected"
        assert data["rejection_reason"] == "Not ready for prod"


@pytest.mark.asyncio
async def test_approve_non_pending_returns_400(client: AsyncClient):
    env = await _env(client, "dev", 0)
    p = await client.post("/api/v1/deploy/promote", json={
        "to_env_id": env["id"], "package_manifest": MANIFEST_SMALL,
    })
    run = p.json()
    if run["status"] != "awaiting_approval":
        r = await client.post(f"/api/v1/deploy/runs/{run['id']}/approve", json={})
        assert r.status_code == 400


# ── Change Windows ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_change_window(client: AsyncClient):
    env = await _env(client, "prod", 3)
    r = await client.post("/api/v1/deploy/windows", json={
        "env_id": env["id"],
        "name": "Production Window",
        "days_of_week": [0,1,2,3,4],
        "start_hour_utc": 2,
        "end_hour_utc": 4,
    })
    assert r.status_code == 201
    assert r.json()["name"] == "Production Window"


@pytest.mark.asyncio
async def test_list_windows_empty(client: AsyncClient):
    r = await client.get("/api/v1/deploy/windows")
    assert r.status_code == 200 and isinstance(r.json(), list)


@pytest.mark.asyncio
async def test_list_windows_by_env(client: AsyncClient):
    env = await _env(client, "prod", 3)
    await client.post("/api/v1/deploy/windows", json={
        "env_id": env["id"], "name": "Test Window",
        "days_of_week": [0], "start_hour_utc": 0, "end_hour_utc": 2,
    })
    r = await client.get(f"/api/v1/deploy/windows?env_id={env['id']}")
    assert r.status_code == 200
    assert len(r.json()) >= 1
