"""
Tests for the Process API
==========================

End-to-end tests using FastAPI's test client:
  Deploy XML → Start instance → Check status → List → Cancel

Run with:  uv run python -m pytest engine/tests/integration/test_api.py -v
"""

from __future__ import annotations

import asyncio
import pytest
from fastapi.testclient import TestClient

from helix_engine.compiler import BPMNCompiler
from helix_engine.db.store import ProcessStore
from helix_engine.api.routers.process import init_router
from helix_engine.api.schemas.process import InstanceStatus
from helix_engine.main import app


# ── Fixtures ──────────────────────────────────────────────────────────

SIMPLE_BPMN = """\
<?xml version="1.0" encoding="UTF-8"?>
<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL">
  <process id="test_process" name="Test Process" isExecutable="true">
    <startEvent id="start" name="Begin"/>
    <sequenceFlow id="f1" sourceRef="start" targetRef="task1"/>
    <serviceTask id="task1" name="Do Work" implementation="helix://test/work"/>
    <sequenceFlow id="f2" sourceRef="task1" targetRef="end"/>
    <endEvent id="end" name="Finish"/>
  </process>
</definitions>
"""

INVALID_BPMN = """\
<?xml version="1.0" encoding="UTF-8"?>
<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL">
  <process id="broken" name="Broken" isExecutable="true">
    <serviceTask id="orphan" name="No start event"/>
  </process>
</definitions>
"""


@pytest.fixture(autouse=True)
def _setup_router():
    """Initialise the router with fresh store and compiler before each test."""
    store = ProcessStore()
    compiler = BPMNCompiler()
    init_router(store=store, compiler=compiler)
    app.state.store = store
    app.state.compiler = compiler
    yield


@pytest.fixture
def client():
    return TestClient(app)


# ── Deploy tests ──────────────────────────────────────────────────────

class TestDeploy:

    def test_deploy_valid_process(self, client: TestClient):
        resp = client.post("/processes/deploy", json={"bpmn_xml": SIMPLE_BPMN})
        assert resp.status_code == 201
        data = resp.json()
        assert data["process_id"] == "test_process"
        assert data["version"] == 1
        assert data["status"] == "active"
        assert data["element_count"] == 3
        assert data["flow_count"] == 2

    def test_deploy_with_name_override(self, client: TestClient):
        resp = client.post("/processes/deploy", json={
            "bpmn_xml": SIMPLE_BPMN,
            "name": "My Custom Name",
        })
        assert resp.status_code == 201
        assert resp.json()["name"] == "My Custom Name"

    def test_deploy_with_tags(self, client: TestClient):
        resp = client.post("/processes/deploy", json={
            "bpmn_xml": SIMPLE_BPMN,
            "tags": {"env": "test", "team": "platform"},
        })
        assert resp.status_code == 201

    def test_deploy_invalid_bpmn_returns_422(self, client: TestClient):
        resp = client.post("/processes/deploy", json={"bpmn_xml": INVALID_BPMN})
        assert resp.status_code == 422

    def test_deploy_empty_xml_returns_422(self, client: TestClient):
        resp = client.post("/processes/deploy", json={"bpmn_xml": ""})
        assert resp.status_code == 422

    def test_deploy_versioning(self, client: TestClient):
        """Deploying the same process twice creates version 2."""
        resp1 = client.post("/processes/deploy", json={"bpmn_xml": SIMPLE_BPMN})
        assert resp1.json()["version"] == 1

        resp2 = client.post("/processes/deploy", json={"bpmn_xml": SIMPLE_BPMN})
        assert resp2.json()["version"] == 2


# ── List / Get tests ─────────────────────────────────────────────────

class TestListAndGet:

    def test_list_empty(self, client: TestClient):
        resp = client.get("/processes")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_list_after_deploy(self, client: TestClient):
        client.post("/processes/deploy", json={"bpmn_xml": SIMPLE_BPMN})
        resp = client.get("/processes")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["processes"][0]["process_id"] == "test_process"

    def test_get_process(self, client: TestClient):
        client.post("/processes/deploy", json={"bpmn_xml": SIMPLE_BPMN})
        resp = client.get("/processes/test_process")
        assert resp.status_code == 200
        assert resp.json()["process_id"] == "test_process"

    def test_get_nonexistent_returns_404(self, client: TestClient):
        resp = client.get("/processes/nonexistent")
        assert resp.status_code == 404


# ── Start instance tests ─────────────────────────────────────────────

class TestStartInstance:

    def test_start_instance(self, client: TestClient):
        client.post("/processes/deploy", json={"bpmn_xml": SIMPLE_BPMN})
        resp = client.post("/processes/test_process/start", json={
            "variables": {"order_id": "12345"},
            "business_key": "ORD-12345",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["process_id"] == "test_process"
        assert data["status"] == "running"
        assert data["business_key"] == "ORD-12345"
        assert "instance_id" in data

    def test_start_nonexistent_process_returns_404(self, client: TestClient):
        resp = client.post("/processes/nonexistent/start", json={})
        assert resp.status_code == 404

    def test_start_with_empty_variables(self, client: TestClient):
        client.post("/processes/deploy", json={"bpmn_xml": SIMPLE_BPMN})
        resp = client.post("/processes/test_process/start", json={})
        assert resp.status_code == 201


# ── Instance status tests ────────────────────────────────────────────

class TestInstanceStatus:

    def test_get_instance_status(self, client: TestClient):
        client.post("/processes/deploy", json={"bpmn_xml": SIMPLE_BPMN})
        start_resp = client.post("/processes/test_process/start", json={
            "variables": {"x": 1},
        })
        instance_id = start_resp.json()["instance_id"]

        resp = client.get(f"/processes/test_process/instances/{instance_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["instance_id"] == instance_id
        assert data["process_id"] == "test_process"

    def test_get_nonexistent_instance_returns_404(self, client: TestClient):
        client.post("/processes/deploy", json={"bpmn_xml": SIMPLE_BPMN})
        resp = client.get("/processes/test_process/instances/fake-id")
        assert resp.status_code == 404

    def test_list_instances(self, client: TestClient):
        client.post("/processes/deploy", json={"bpmn_xml": SIMPLE_BPMN})
        client.post("/processes/test_process/start", json={})
        client.post("/processes/test_process/start", json={})

        resp = client.get("/processes/test_process/instances")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2


# ── Cancel tests ─────────────────────────────────────────────────────

class TestCancelInstance:

    def test_cancel_running_instance(self, client: TestClient):
        client.post("/processes/deploy", json={"bpmn_xml": SIMPLE_BPMN})
        start_resp = client.post("/processes/test_process/start", json={})
        instance_id = start_resp.json()["instance_id"]

        # Manually set status to running (process may complete instantly)
        client.app.state.store.update_instance(instance_id, status=InstanceStatus.RUNNING)

        resp = client.post(
            f"/processes/test_process/instances/{instance_id}/cancel"
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"

    def test_cancel_nonexistent_returns_404(self, client: TestClient):
        client.post("/processes/deploy", json={"bpmn_xml": SIMPLE_BPMN})
        resp = client.post("/processes/test_process/instances/fake/cancel")
        assert resp.status_code == 404
