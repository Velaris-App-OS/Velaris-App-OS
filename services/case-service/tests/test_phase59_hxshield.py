"""Tests for P59 HxShield — fraud & abuse detection."""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient

from tests.conftest import client  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_create_rule(client: AsyncClient):
    resp = await client.post("/api/v1/shield/rules", json={
        "name": "DoS submission guard",
        "pattern_type": "dos_submission",
        "threshold": 5,
        "window_seconds": 60,
        "action": "block",
        "severity": "high",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["pattern_type"] == "dos_submission"
    assert data["action"] == "block"
    return data["id"]


@pytest.mark.asyncio
async def test_list_rules(client: AsyncClient):
    await client.post("/api/v1/shield/rules", json={
        "name": "Velocity guard",
        "pattern_type": "velocity_anomaly",
        "threshold": 20,
        "window_seconds": 300,
        "action": "flag",
        "severity": "medium",
    })
    resp = await client.get("/api/v1/shield/rules")
    assert resp.status_code == 200
    assert len(resp.json()) >= 1


@pytest.mark.asyncio
async def test_update_rule(client: AsyncClient):
    cr = await client.post("/api/v1/shield/rules", json={
        "name": "To Update",
        "pattern_type": "replay_attack",
        "threshold": 3,
        "window_seconds": 60,
        "action": "flag",
        "severity": "low",
    })
    rule_id = cr.json()["id"]
    resp = await client.patch(f"/api/v1/shield/rules/{rule_id}", json={"threshold": 10})
    assert resp.status_code == 200
    assert resp.json()["threshold"] == 10


@pytest.mark.asyncio
async def test_delete_rule(client: AsyncClient):
    cr = await client.post("/api/v1/shield/rules", json={
        "name": "To Delete",
        "pattern_type": "duplicate_case_flood",
        "threshold": 5,
        "window_seconds": 60,
        "action": "flag",
        "severity": "low",
    })
    rule_id = cr.json()["id"]
    resp = await client.delete(f"/api/v1/shield/rules/{rule_id}")
    assert resp.status_code == 204
    get_resp = await client.get(f"/api/v1/shield/rules/{rule_id}")
    assert get_resp.status_code == 404


@pytest.mark.asyncio
async def test_score_no_rules(client: AsyncClient):
    resp = await client.post("/api/v1/shield/score", json={
        "event_type": "case_created",
        "actor_id": "user-123",
        "context": {},
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["score"] == 0.0
    assert data["action"] == "allow"


@pytest.mark.asyncio
async def test_list_incidents_empty(client: AsyncClient):
    resp = await client.get("/api/v1/shield/incidents")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_list_events(client: AsyncClient):
    # Scoring always writes a shield event
    await client.post("/api/v1/shield/score", json={
        "event_type": "form_submit",
        "actor_id": "user-abc",
        "context": {"field": "value"},
    })
    resp = await client.get("/api/v1/shield/events")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_stats(client: AsyncClient):
    resp = await client.get("/api/v1/shield/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert "open_incidents" in data
    assert "total_incidents" in data
    assert "flagged_events" in data
