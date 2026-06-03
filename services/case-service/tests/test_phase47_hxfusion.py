"""Tests for P47 HxFusion — Adaptive Execution Engine."""
from __future__ import annotations

import pytest
from httpx import AsyncClient

from tests.conftest import client  # type: ignore[attr-defined]

MINIMAL_BPMN = """<?xml version="1.0" encoding="UTF-8"?>
<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL">
  <process id="P1" name="Test Process">
    <startEvent id="start" name="Start" />
    <sequenceFlow id="f1" sourceRef="start" targetRef="task1" />
    <serviceTask id="task1" name="Do Work" />
    <sequenceFlow id="f2" sourceRef="task1" targetRef="end" />
    <endEvent id="end" name="End" />
  </process>
</definitions>"""

GATEWAY_BPMN = """<?xml version="1.0" encoding="UTF-8"?>
<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL">
  <process id="P2" name="Gateway Process">
    <startEvent id="start" />
    <sequenceFlow id="f1" sourceRef="start" targetRef="gw1" />
    <exclusiveGateway id="gw1" name="Check Amount" />
    <sequenceFlow id="f2" sourceRef="gw1" targetRef="highTask">
      <conditionExpression>${amount &gt; 100}</conditionExpression>
    </sequenceFlow>
    <sequenceFlow id="f3" sourceRef="gw1" targetRef="lowTask" />
    <serviceTask id="highTask" name="High Value Task" />
    <serviceTask id="lowTask" name="Low Value Task" />
    <sequenceFlow id="f4" sourceRef="highTask" targetRef="end" />
    <sequenceFlow id="f5" sourceRef="lowTask" targetRef="end" />
    <endEvent id="end" />
  </process>
</definitions>"""


# ── Parser unit tests ─────────────────────────────────────────────────────────

def test_parser_minimal():
    from case_service.hxfusion.parser import parse
    p = parse(MINIMAL_BPMN)
    assert p.id == "P1"
    assert len(p.nodes) == 3
    assert len(p.flows) == 2
    assert p.start_events == ["start"]


def test_parser_gateway():
    from case_service.hxfusion.parser import parse
    p = parse(GATEWAY_BPMN)
    assert "gw1" in p.nodes
    assert p.nodes["gw1"].node_type == "exclusiveGateway"
    # With amount=200 should route to highTask
    next_high = p.next_nodes("gw1", {"amount": 200})
    assert "highTask" in next_high
    # With amount=50 should fall through to lowTask
    next_low = p.next_nodes("gw1", {"amount": 50})
    assert "lowTask" in next_low


def test_parser_invalid():
    from case_service.hxfusion.parser import parse
    import pytest
    with pytest.raises(Exception):
        parse("<not>bpmn</not>")


# ── API tests ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_validate_bpmn_valid(client: AsyncClient):
    resp = await client.post("/api/v1/fusion/definitions/validate", json={"bpmn_xml": MINIMAL_BPMN})
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is True
    assert data["node_count"] == 3


@pytest.mark.asyncio
async def test_validate_bpmn_invalid(client: AsyncClient):
    resp = await client.post("/api/v1/fusion/definitions/validate", json={"bpmn_xml": "<bad/>"})
    assert resp.status_code == 200
    assert resp.json()["valid"] is False


@pytest.mark.asyncio
async def test_create_definition(client: AsyncClient):
    resp = await client.post("/api/v1/fusion/definitions", json={
        "name": "Test Process",
        "bpmn_xml": MINIMAL_BPMN,
        "description": "A test process",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Test Process"
    assert data["version"] == 1
    assert data["status"] == "active"
    return data["id"]


@pytest.mark.asyncio
async def test_version_increments(client: AsyncClient):
    for _ in range(2):
        await client.post("/api/v1/fusion/definitions", json={
            "name": "Versioned Process",
            "bpmn_xml": MINIMAL_BPMN,
        })
    resp = await client.get("/api/v1/fusion/definitions")
    versions = [d["version"] for d in resp.json() if d["name"] == "Versioned Process"]
    assert sorted(versions) == [1, 2]


@pytest.mark.asyncio
async def test_list_definitions(client: AsyncClient):
    await client.post("/api/v1/fusion/definitions", json={"name": "Listed", "bpmn_xml": MINIMAL_BPMN})
    resp = await client.get("/api/v1/fusion/definitions")
    assert resp.status_code == 200
    assert len(resp.json()) >= 1


@pytest.mark.asyncio
async def test_update_definition(client: AsyncClient):
    cr = await client.post("/api/v1/fusion/definitions", json={"name": "To Update", "bpmn_xml": MINIMAL_BPMN})
    defn_id = cr.json()["id"]
    resp = await client.patch(f"/api/v1/fusion/definitions/{defn_id}", json={"status": "inactive"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "inactive"


@pytest.mark.asyncio
async def test_start_instance(client: AsyncClient):
    cr = await client.post("/api/v1/fusion/definitions", json={"name": "Runnable", "bpmn_xml": MINIMAL_BPMN})
    defn_id = cr.json()["id"]
    resp = await client.post("/api/v1/fusion/instances", json={
        "definition_id": defn_id,
        "context": {"order_id": "ORD-001"},
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["definition_id"] == defn_id
    assert data["status"] in ("running", "completed")


@pytest.mark.asyncio
async def test_start_instance_inactive_definition(client: AsyncClient):
    cr = await client.post("/api/v1/fusion/definitions", json={"name": "Archived", "bpmn_xml": MINIMAL_BPMN})
    defn_id = cr.json()["id"]
    await client.patch(f"/api/v1/fusion/definitions/{defn_id}", json={"status": "inactive"})
    resp = await client.post("/api/v1/fusion/instances", json={"definition_id": defn_id})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_list_instances(client: AsyncClient):
    resp = await client.get("/api/v1/fusion/instances")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.asyncio
async def test_task_log(client: AsyncClient):
    cr = await client.post("/api/v1/fusion/definitions", json={"name": "Log Test", "bpmn_xml": MINIMAL_BPMN})
    inst_resp = await client.post("/api/v1/fusion/instances", json={"definition_id": cr.json()["id"]})
    inst_id = inst_resp.json()["id"]
    resp = await client.get(f"/api/v1/fusion/instances/{inst_id}/log")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_stats(client: AsyncClient):
    resp = await client.get("/api/v1/fusion/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert "total_definitions" in data
    assert "instances_by_status" in data


@pytest.mark.asyncio
async def test_list_bindings(client: AsyncClient):
    resp = await client.get("/api/v1/fusion/bindings")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
