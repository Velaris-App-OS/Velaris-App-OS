"""Test Suite (#27) Phase C — structural conformance checks + the submit gate.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from case_service.testsuite import conformance
from case_service.db.models import MarketplaceWorkspaceModel


def _clean_package() -> dict:
    return {
        "manifest": {"name": "Acme App", "version": "1.0.0"},
        "case_types": [{"name": "Claim", "definition_json": {"stages": [
            {"id": "intake", "name": "Intake", "transitions": ["review"]},
            {"id": "review", "name": "Review", "transitions": ["closed"]},
            {"id": "closed", "name": "Closed", "terminal": True, "transitions": []},
        ]}}],
        "forms": [{"name": "Claim Form", "fields": [
            {"key": "policy_number", "type": "text", "required": True},
            {"key": "amount", "type": "currency"},
        ], "field_refs": ["policy_number"]}],
        "rules": [{"id": "r1", "name": "escalate", "depends_on": []},
                  {"id": "r2", "name": "notify", "depends_on": ["r1"]}],
    }


# ── Structural checks ─────────────────────────────────────────────────────────

def test_clean_package_passes():
    res = conformance.run_structural(_clean_package())
    assert res["passed"], [c for c in res["checks"] if not c["ok"]]


def test_dangling_stage_transition_fails():
    pkg = _clean_package()
    pkg["case_types"][0]["definition_json"]["stages"][0]["transitions"] = ["nowhere"]
    res = conformance.run_structural(pkg)
    assert not res["passed"]
    assert any(c["check"].startswith("stage_transitions") and not c["ok"] for c in res["checks"])


def test_bad_form_field_type_fails():
    pkg = _clean_package()
    pkg["forms"][0]["fields"][0]["type"] = "quantum"
    res = conformance.run_structural(pkg)
    assert not res["passed"]
    assert any(c["check"].startswith("form_field_types") and not c["ok"] for c in res["checks"])


def test_circular_rule_dependency_fails():
    pkg = _clean_package()
    pkg["rules"][0]["depends_on"] = ["r2"]  # r1->r2->r1 cycle
    res = conformance.run_structural(pkg)
    assert not res["passed"]
    assert any(c["check"] == "rules_acyclic" and not c["ok"] for c in res["checks"])


def test_hardcoded_id_fails():
    pkg = _clean_package()
    pkg["case_types"][0]["definition_json"]["stages"][0]["assignee"] = str(uuid.uuid4())
    res = conformance.run_structural(pkg)
    assert not res["passed"]
    assert any(c["check"] == "no_hardcoded_ids" and not c["ok"] for c in res["checks"])


@pytest.mark.asyncio
async def test_record_conformance_run_persists(session):
    run = await conformance.record_conformance_run(session, _clean_package(), triggered_by="t")
    assert run.status == "passed" and run.failed == 0


# ── The submit gate (end-to-end) ──────────────────────────────────────────────

async def _make_workspace(session, status="active", conformance_status="none") -> uuid.UUID:
    ws = MarketplaceWorkspaceModel(
        tenant_id="default", name="WS", status=status, created_by="anyone",
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        conformance_status=conformance_status,
    )
    session.add(ws)
    await session.commit()
    return ws.id


@pytest.mark.asyncio
async def test_submit_blocked_without_conformance(session, client):
    ws_id = await _make_workspace(session)
    r = await client.post(f"/api/v1/marketplace/workspaces/{ws_id}/submit", json={})
    assert r.status_code == 400, r.text
    assert "Conformance" in r.text


@pytest.mark.asyncio
async def test_submit_allowed_after_conformance_pass(session, client):
    ws_id = await _make_workspace(session)
    # run conformance (clean package) attached to the workspace → structural_passed
    rc = await client.post("/api/v1/testsuite/conformance",
                           json={"package": _clean_package(), "workspace_id": str(ws_id)})
    assert rc.status_code == 200 and rc.json()["passed"], rc.text
    r = await client.post(f"/api/v1/marketplace/workspaces/{ws_id}/submit", json={})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "submitted"


@pytest.mark.asyncio
async def test_submit_blocked_after_conformance_fail(session, client):
    ws_id = await _make_workspace(session)
    bad = _clean_package()
    bad["rules"][0]["depends_on"] = ["r2"]  # cycle → fails
    rc = await client.post("/api/v1/testsuite/conformance",
                           json={"package": bad, "workspace_id": str(ws_id)})
    assert rc.status_code == 200 and not rc.json()["passed"]
    r = await client.post(f"/api/v1/marketplace/workspaces/{ws_id}/submit", json={})
    assert r.status_code == 400
