"""Tests for P54 HxMigrate — Unified Migration Intelligence Pipeline."""
from __future__ import annotations

import uuid
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import MigrationPipelineRunModel, PipelineStageEventModel
from case_service.api.routers.hxmigrate import get_pipeline_factory
from case_service.main import app

from tests.conftest import client, session, TestSessionFactory  # type: ignore[attr-defined]

# Override the pipeline factory so background tasks use the same SQLite DB as tests
app.dependency_overrides[get_pipeline_factory] = lambda: TestSessionFactory

PEGA_XML = b"""<?xml version="1.0"?>
<pega:ruleSet xmlns:pega="http://www.pega.com">
  <pega:rule type="FlowRule" name="ProcessClaim">
    <pega:description>Main claim processing flow</pega:description>
  </pega:rule>
  <pega:rule type="SectionRule" name="ClaimForm">
    <pega:description>Claim intake form</pega:description>
  </pega:rule>
</pega:ruleSet>"""

CAMUNDA_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL">
  <process id="claimProcess" name="Claim Process" isExecutable="true">
    <startEvent id="start"/>
    <userTask id="review" name="Review Claim"/>
    <endEvent id="end"/>
  </process>
</definitions>"""


# ── Platform list ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_platforms(client: AsyncClient):
    r = await client.get("/api/v1/hxmigrate/platforms")
    assert r.status_code == 200
    platforms = r.json()
    ids = [p["id"] for p in platforms]
    assert "pega" in ids and "camunda" in ids and "appian" in ids and "servicenow" in ids


# ── Run creation ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_start_run_pega(client: AsyncClient):
    r = await client.post("/api/v1/hxmigrate/run", data={
        "source_platform": "pega", "name": "Test Pega Run", "mode": "full",
    }, files={"file": ("test.xml", PEGA_XML, "application/xml")})
    assert r.status_code == 202
    assert "run_id" in r.json()


@pytest.mark.asyncio
async def test_start_run_camunda(client: AsyncClient):
    r = await client.post("/api/v1/hxmigrate/run", data={
        "source_platform": "camunda", "name": "Camunda Run",
    }, files={"file": ("process.bpmn", CAMUNDA_XML, "application/xml")})
    assert r.status_code == 202


@pytest.mark.asyncio
async def test_start_run_appian(client: AsyncClient):
    r = await client.post("/api/v1/hxmigrate/run", data={
        "source_platform": "appian",
    }, files={"file": ("app.xml", b"<appian/>", "application/xml")})
    assert r.status_code == 202


@pytest.mark.asyncio
async def test_start_run_servicenow(client: AsyncClient):
    r = await client.post("/api/v1/hxmigrate/run", data={
        "source_platform": "servicenow",
    }, files={"file": ("sn.xml", b"<sn/>", "application/xml")})
    assert r.status_code == 202


@pytest.mark.asyncio
async def test_start_run_invalid_platform(client: AsyncClient):
    r = await client.post("/api/v1/hxmigrate/run", data={
        "source_platform": "oracle",
    }, files={"file": ("f.xml", b"<x/>", "application/xml")})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_run_creates_5_stage_events(client: AsyncClient, session: AsyncSession):
    r = await client.post("/api/v1/hxmigrate/run", data={
        "source_platform": "pega", "name": "Stage Event Test",
    }, files={"file": ("t.xml", PEGA_XML, "application/xml")})
    run_id = uuid.UUID(r.json()["run_id"])

    from sqlalchemy import select
    events = (await session.execute(
        select(PipelineStageEventModel).where(PipelineStageEventModel.run_id == run_id)
    )).scalars().all()
    assert len(events) == 5
    assert sorted(e.stage for e in events) == [1, 2, 3, 4, 5]


# ── Get run ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_run(client: AsyncClient):
    r = await client.post("/api/v1/hxmigrate/run", data={
        "source_platform": "pega", "name": "Get Test",
    }, files={"file": ("t.xml", PEGA_XML, "application/xml")})
    run_id = r.json()["run_id"]

    r2 = await client.get(f"/api/v1/hxmigrate/runs/{run_id}")
    assert r2.status_code == 200
    data = r2.json()
    assert data["id"] == run_id
    assert data["source_platform"] == "pega"
    assert data["name"] == "Get Test"
    assert len(data["stages"]) == 5


@pytest.mark.asyncio
async def test_get_run_not_found(client: AsyncClient):
    r = await client.get(f"/api/v1/hxmigrate/runs/{uuid.uuid4()}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_run_has_mode(client: AsyncClient):
    r = await client.post("/api/v1/hxmigrate/run", data={
        "source_platform": "camunda", "mode": "step_by_step",
    }, files={"file": ("t.bpmn", CAMUNDA_XML, "application/xml")})
    run_id = r.json()["run_id"]
    r2 = await client.get(f"/api/v1/hxmigrate/runs/{run_id}")
    assert r2.json()["mode"] == "step_by_step"


@pytest.mark.asyncio
async def test_run_stage_names_correct(client: AsyncClient):
    r = await client.post("/api/v1/hxmigrate/run", data={
        "source_platform": "pega",
    }, files={"file": ("t.xml", PEGA_XML, "application/xml")})
    r2 = await client.get(f"/api/v1/hxmigrate/runs/{r.json()['run_id']}")
    names = [s["stage_name"] for s in r2.json()["stages"]]
    assert "Scout Assessment" in names
    assert "Scout AI Analysis" in names
    assert "BPM Generation" in names
    assert "Orchestrator Project" in names
    assert "App Registry Package" in names


# ── List runs ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_runs(client: AsyncClient):
    r = await client.get("/api/v1/hxmigrate/runs")
    assert r.status_code == 200 and isinstance(r.json(), list)


@pytest.mark.asyncio
async def test_list_runs_shows_created(client: AsyncClient):
    await client.post("/api/v1/hxmigrate/run", data={
        "source_platform": "pega", "name": "Listed Run",
    }, files={"file": ("t.xml", PEGA_XML, "application/xml")})
    r = await client.get("/api/v1/hxmigrate/runs")
    assert any(run["name"] == "Listed Run" for run in r.json())


# ── Result endpoint ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_result_unavailable_while_pending(client: AsyncClient):
    r = await client.post("/api/v1/hxmigrate/run", data={
        "source_platform": "pega",
    }, files={"file": ("t.xml", PEGA_XML, "application/xml")})
    run_id = r.json()["run_id"]
    # Get the run — it might be pending or running
    r2 = await client.get(f"/api/v1/hxmigrate/runs/{run_id}")
    if r2.json()["status"] not in ("completed", "partial"):
        r3 = await client.get(f"/api/v1/hxmigrate/runs/{run_id}/result")
        assert r3.status_code == 400
