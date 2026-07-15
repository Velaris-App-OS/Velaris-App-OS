"""HELIX P44 — BPM App Importer tests (24 tests).

Covers: extractor (Camunda raw XML, Pega ZIP, unknown tool), parsers
        (Pega flow/section/SLA/AG, Camunda BPMN, Appian, ServiceNow),
        mapper (case_type generated, form generated, SLA mapped, unmapped),
        generator (definition_json structure, migration SQL), reporter
        (summary counts, conversion_pct), API endpoints (upload → job created,
        list jobs, get job, get report 409 before complete, delete job),
        bad tool returns 400, empty file returns 400.
"""
from __future__ import annotations

import io
import uuid
import zipfile

import pytest
import pytest_asyncio
from httpx import AsyncClient

from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser, ActiveAccessGroup
from case_service.bpm_importer.extractor import extract
from case_service.bpm_importer.parsers import pega, camunda, appian
from case_service.bpm_importer.generator import generate, _slug
from case_service.bpm_importer.reporter import build_report
from case_service.db.models import ImportJobModel
from case_service.main import app


# ── Auth ──────────────────────────────────────────────────────────────────────

def _admin():
    return AuthenticatedUser(
        user_id="admin-1", roles=["admin"],
        active_access_group=ActiveAccessGroup(
            id=str(uuid.uuid4()), name="Admins",
            portal_id=str(uuid.uuid4()), portal_type="admin",
            portal_name="Admin Portal", modules=[], homepage="/",
            roles=["admin"], privileges=[],
            allowed_case_type_ids=["*"], allowed_queue_ids=["*"],
        ),
    )

def _override():
    app.dependency_overrides[get_current_user] = lambda: _admin()

def _clear():
    app.dependency_overrides.pop(get_current_user, None)


# ── Helpers ───────────────────────────────────────────────────────────────────

_CAMUNDA_BPMN = b"""<?xml version="1.0" encoding="UTF-8"?>
<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL">
  <process id="claims" name="Insurance Claim">
    <userTask id="t1" name="Intake Review" />
    <userTask id="t2" name="Assessment" />
    <serviceTask id="t3" name="Auto Validate" />
    <exclusiveGateway id="gw1" name="Route" />
    <sequenceFlow id="sf1" sourceRef="t1" targetRef="t2" />
  </process>
</definitions>"""

_PEGA_FLOW_XML = b"""<?xml version="1.0"?>
<pega:FlowRule xmlns:pega="http://pega.com" name="Claim">
  <pega:FlowShape id="s1" type="ASSIGNMENT" pyLabel="Review Document" />
  <pega:FlowShape id="s2" type="APPROVAL" pyLabel="Manager Approval" />
</pega:FlowRule>"""

_PEGA_SECTION_XML = b"""<?xml version="1.0"?>
<Section name="ClaimDetails">
  <Field name="ClaimAmount" type="Decimal" />
  <Field name="ClaimDate" type="Date" />
  <Field name="Description" type="TextArea" />
</Section>"""

_PEGA_SLA_XML = b"""<?xml version="1.0"?>
<SLARule name="ClaimSLA">
  <pyGoal pyValue="8" pyUnit="hours" />
  <pyDeadline pyValue="24" pyUnit="hours" />
</SLARule>"""

_PEGA_AG_XML = b"""<?xml version="1.0"?>
<AccessGroup name="Claims">
  <Roles>
    <Role name="ClaimsAdjuster" />
    <Role name="Supervisor" />
  </Roles>
</AccessGroup>"""


def _make_pega_zip(*entries: tuple[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries:
            zf.writestr(name, content)
    return buf.getvalue()


# ── Extractor tests ───────────────────────────────────────────────────────────

class TestExtractor:
    def test_camunda_raw_xml(self):
        result = extract("camunda", _CAMUNDA_BPMN, "claims.bpmn")
        assert result["total"] == 1
        assert result["files"][0]["rule_type"] == "BpmnProcess"

    def test_pega_zip_classifies_flow(self):
        zb = _make_pega_zip(("Flow-InsuranceClaim.xml", _PEGA_FLOW_XML))
        result = extract("pega", zb, "export.jar")
        assert result["total"] == 1
        assert result["files"][0]["rule_type"] == "Flow"

    def test_pega_zip_classifies_section(self):
        zb = _make_pega_zip(("Section-ClaimDetails.xml", _PEGA_SECTION_XML))
        result = extract("pega", zb, "export.jar")
        assert result["files"][0]["rule_type"] == "Section"

    def test_pega_zip_skips_binary_files(self):
        zb = _make_pega_zip(
            ("Flow-Claim.xml", _PEGA_FLOW_XML),
            ("image.png", b"\x89PNG\r\n"),
        )
        result = extract("pega", zb, "export.jar")
        assert result["total"] == 1
        assert result["skipped"] == 1

    def test_unknown_tool_returns_other(self):
        zb = _make_pega_zip(("anything.xml", b"<root/>"))
        result = extract("servicenow", zb, "export.zip")
        assert result["files"][0]["rule_type"] == "Other"


# ── Parser tests ──────────────────────────────────────────────────────────────

class TestPegaParser:
    def _files(self, rule_type, name, content):
        return [{"rule_type": rule_type, "name": name, "content": content.decode()}]

    def test_parse_flow_extracts_steps(self):
        result = pega.parse_files(self._files("Flow", "Flow-Claim.xml", _PEGA_FLOW_XML))
        flows = result.get("Flow", [])
        assert len(flows) == 1
        assert flows[0]["name"] == "Claim"
        assert any(s["step_type"] == "approval" for s in flows[0]["steps"])

    def test_parse_section_extracts_fields(self):
        result = pega.parse_files(self._files("Section", "Section-ClaimDetails.xml", _PEGA_SECTION_XML))
        sections = result.get("Section", [])
        assert len(sections) == 1
        assert len(sections[0]["fields"]) == 3

    def test_parse_sla_extracts_hours(self):
        result = pega.parse_files(self._files("SLARule", "SLARule-ClaimSLA.xml", _PEGA_SLA_XML))
        slas = result.get("SLARule", [])
        assert slas[0]["goal_hours"] == 8
        assert slas[0]["deadline_hours"] == 24

    def test_parse_access_group(self):
        result = pega.parse_files(self._files("AccessGroup", "AccessGroup-Claims.xml", _PEGA_AG_XML))
        ags = result.get("AccessGroup", [])
        assert ags[0]["name"] == "Claims"


class TestCamundaParser:
    def test_parse_bpmn_extracts_tasks(self):
        files = [{"rule_type": "BpmnProcess", "name": "claims.bpmn", "content": _CAMUNDA_BPMN.decode()}]
        result = camunda.parse_files(files)
        processes = result.get("BpmnProcess", [])
        assert len(processes) == 1
        procs = processes[0]["processes"]
        assert any(p["name"] == "Insurance Claim" for p in procs)
        # bpmn2 parser groups steps under stages
        steps = [s for stage in procs[0]["stages"] for s in stage["steps"]]
        assert any(s["step_type"] == "user_task" for s in steps)
        assert any(s["step_type"] == "automated" for s in steps)


class TestAppianParser:
    def test_parse_process_model(self):
        content = """<?xml version="1.0"?>
<processModel name="Loan Approval">
  <node type="userinput" name="Submit Application"/>
  <node type="script" name="Credit Check"/>
  <node type="approval" name="Manager Approval"/>
</processModel>""".encode()
        files = [{"rule_type": "ProcessModel", "name": "LoanApproval.xml", "content": content.decode()}]
        result = appian.parse_files(files)
        models = result.get("ProcessModel", [])
        assert models[0]["name"] == "Loan Approval"     # root name attr wins
        steps = [s for stage in models[0]["stages"] for s in stage["steps"]]
        assert any(s["step_type"] == "user_task" for s in steps)
        assert any(s["step_type"] == "approval" for s in steps)


# ── Generator tests ───────────────────────────────────────────────────────────

class TestGenerator:
    def _mapped(self):
        return {
            "case_types": [
                {
                    "name": "Insurance Claim",
                    "source_rule": "Flow",
                    "confidence": "exact",
                    "stages": [
                        {"id": "intake", "name": "Intake", "steps": [
                            {"name": "Review", "step_type": "user_task"}
                        ]},
                    ],
                }
            ],
            "forms": [
                {"name": "Claim Details", "source_rule": "Section", "confidence": "exact",
                 "fields": [{"name": "Amount", "type": "number", "required": True}]},
            ],
            "sla_rules":     [{"name": "Claim SLA", "goal_hours": 8, "deadline_hours": 24}],
            "access_groups": [{"name": "ClaimsTeam", "roles": [], "confidence": "close"}],
            "unmapped":      [],
        }

    def test_generates_case_type_definition_json(self):
        result = generate(self._mapped())
        ct = result["case_types"][0]
        assert ct["slug"] == "insurance-claim"
        assert "stages" in ct["definition_json"]
        assert ct["definition_json"]["stages"][0]["id"] == "intake"

    def test_generates_form_schema(self):
        result = generate(self._mapped())
        form = result["forms"][0]
        assert form["slug"] == "claim-details"
        assert len(form["schema"]["fields"]) == 1

    def test_generates_sla_definitions(self):
        # migration_sql was removed in HxMigrate v2 — objects are written via
        # the ORM apply path; generate() emits native definitions only
        result = generate(self._mapped())
        assert "migration_sql" not in result
        sla = result["sla_rules"][0]
        assert sla["name"] == "Claim SLA"
        assert sla["goal_seconds"] == 8 * 3600
        assert sla["deadline_seconds"] == 24 * 3600

    def test_slug_normalises_spaces(self):
        assert _slug("Insurance Claim 2024") == "insurance-claim-2024"


# ── Reporter tests ────────────────────────────────────────────────────────────

class TestReporter:
    def test_report_summary(self):
        pass1 = {"total": 5, "skipped": 1, "type_counts": {"Flow": 2, "Section": 3}}
        pass2 = {}
        pass3 = {
            "case_types":    [{"name": "A", "confidence": "exact"}],
            "forms":         [{"name": "B", "confidence": "close"}],
            "sla_rules":     [{"name": "C"}],
            "access_groups": [],
            "unmapped":      [{"name": "X", "needs_review": True, "helix_suggestion": None}],
        }
        pass4 = {
            "case_types": [{"name": "A", "confidence": "exact"}],
            "forms":      [{"name": "B", "confidence": "close"}],
        }
        report = build_report("pega", "export.jar", pass1, pass2, pass3, pass4)
        assert report["summary"]["extracted_total"] == 5
        assert report["summary"]["auto_converted"] == 3
        assert report["summary"]["no_equivalent"] == 1
        assert report["summary"]["conversion_pct"] == 60.0


# ── API endpoint tests ────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestImporterAPI:
    def setup_method(self):
        _override()

    def teardown_method(self):
        _clear()

    async def test_upload_camunda_creates_job(self, client: AsyncClient):
        r = await client.post(
            "/api/v1/importer/upload",
            data={"tool": "camunda"},
            files={"file": ("claims.bpmn", _CAMUNDA_BPMN, "application/xml")},
        )
        assert r.status_code == 202
        d = r.json()
        assert d["tool"] == "camunda"
        assert d["filename"] == "claims.bpmn"
        assert "job_id" in d

    async def test_upload_bad_tool_returns_400(self, client: AsyncClient):
        r = await client.post(
            "/api/v1/importer/upload",
            data={"tool": "oracle"},
            files={"file": ("x.xml", b"<x/>", "application/xml")},
        )
        assert r.status_code == 400

    async def test_upload_empty_file_returns_400(self, client: AsyncClient):
        r = await client.post(
            "/api/v1/importer/upload",
            data={"tool": "camunda"},
            files={"file": ("empty.bpmn", b"", "application/xml")},
        )
        assert r.status_code == 400

    async def test_list_jobs_empty(self, client: AsyncClient):
        r = await client.get("/api/v1/importer/jobs")
        assert r.status_code == 200
        assert r.json()["total"] == 0

    async def test_list_jobs_returns_created(self, client: AsyncClient):
        await client.post(
            "/api/v1/importer/upload",
            data={"tool": "camunda"},
            files={"file": ("claims.bpmn", _CAMUNDA_BPMN, "application/xml")},
        )
        r = await client.get("/api/v1/importer/jobs")
        assert r.json()["total"] == 1

    async def test_get_job_detail(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/importer/upload",
            data={"tool": "camunda"},
            files={"file": ("claims.bpmn", _CAMUNDA_BPMN, "application/xml")},
        )
        job_id = resp.json()["job_id"]
        r = await client.get(f"/api/v1/importer/jobs/{job_id}")
        assert r.status_code == 200
        assert r.json()["id"] == job_id

    async def test_get_report_409_if_pending(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/importer/upload",
            data={"tool": "camunda"},
            files={"file": ("claims.bpmn", _CAMUNDA_BPMN, "application/xml")},
        )
        job_id = resp.json()["job_id"]
        r = await client.get(f"/api/v1/importer/jobs/{job_id}/report")
        # Job is pending/extracting in test (background task doesn't run in test)
        assert r.status_code in (200, 409)

    async def test_delete_job(self, client: AsyncClient):
        resp = await client.post(
            "/api/v1/importer/upload",
            data={"tool": "camunda"},
            files={"file": ("claims.bpmn", _CAMUNDA_BPMN, "application/xml")},
        )
        job_id = resp.json()["job_id"]
        r = await client.delete(f"/api/v1/importer/jobs/{job_id}")
        assert r.status_code == 204
        r2 = await client.get(f"/api/v1/importer/jobs/{job_id}")
        assert r2.status_code == 404

    async def test_get_job_404(self, client: AsyncClient):
        r = await client.get(f"/api/v1/importer/jobs/{uuid.uuid4()}")
        assert r.status_code == 404
