"""HxEvolve Phase B — the API loop and the P2 staging path.

Pins: admin gate + tenant scoping, the scan pipeline recording discarded-gate
insights without surfacing them, dismiss/stage state transitions, and that
staging opens an HxBranch whose merge (a HUMAN approval) is the only way the
proposal reaches production config.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from case_service.db.models import (
    ArtifactBranchModel,
    CaseTypeModel,
    HxEvolveInsightModel,
    RuleDefinitionModel,
)
from case_service.hxevolve import proposer

from tests.phase_hxdraft.test_pD_p2_api import _viewer_headers

_DEF = {
    "stages": [
        {"id": "intake", "name": "Intake", "stage_type": "linear", "order": 0,
         "steps": [{"id": "s1", "name": "Collect"}, {"id": "s2", "name": "Verify"}]},
    ],
    "sla_policies": [{"id": "res_sla", "name": "Resolution SLA", "scope": "case",
                      "goal_duration": "PT4H", "deadline_duration": "PT8H"}],
}


async def _mk_ct(session) -> CaseTypeModel:
    ct = CaseTypeModel(name=f"Evolve CT {uuid.uuid4().hex[:6]}", version="1.0.0",
                       definition_json=_DEF)
    session.add(ct)
    await session.commit()
    await session.refresh(ct)
    return ct


async def _mk_insight(session, ct, *, kind="sla_duration", status="surfaced",
                      proposal=None) -> HxEvolveInsightModel:
    i = HxEvolveInsightModel(
        tenant_id="default", case_type_id=ct.id,
        signal={"kind": "bottleneck", "target": "s2", "window_days": 30},
        proposal=proposal if proposal is not None else {
            "policy": {"id": "res_sla", "name": "Resolution SLA", "scope": "case",
                       "goal_duration": "PT8H", "deadline_duration": "PT24H",
                       "description": "Proposed by HxEvolve"},
            "replaces_policy_id": "res_sla",
            "before_policy": _DEF["sla_policies"][0]},
        proposal_kind=kind, evidence_kind="descriptive",
        evidence={"note": "test"}, rationale="loosen the SLA",
        status=status,
    )
    session.add(i)
    await session.commit()
    await session.refresh(i)
    return i


# ── auth + validation edges ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_admin_gate_and_404s(client, anon_client, session):
    r = await anon_client.post("/api/v1/hxevolve/scan",
                               json={"case_type_id": str(uuid.uuid4())})
    assert r.status_code in (401, 403)
    r = await client.post("/api/v1/hxevolve/scan",
                          json={"case_type_id": str(uuid.uuid4())},
                          headers=_viewer_headers())
    assert r.status_code == 403
    r = await client.post("/api/v1/hxevolve/scan",
                          json={"case_type_id": str(uuid.uuid4())})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_scan_with_no_events_is_honest(client, session):
    ct = await _mk_ct(session)
    r = await client.post("/api/v1/hxevolve/scan",
                          json={"case_type_id": str(ct.id)})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["insights"] == [] and "hint" in body


@pytest.mark.asyncio
async def test_scan_records_gate_discards_without_surfacing(client, session,
                                                            monkeypatch):
    ct = await _mk_ct(session)
    ct_id = ct.id

    # a bottleneck candidate exists…
    from case_service.hxevolve import detector
    async def fake_detect(sess, ctid, **kw):
        return [{"kind": "bottleneck", "target": "s2",
                 "magnitude_seconds": 9000, "occurrences": 30,
                 "severity": "high", "window_days": 30}]
    monkeypatch.setattr(detector, "detect_candidates", fake_detect)

    # …and the LLM proposes something hostile → gate-discarded, never surfaced
    async def hostile(prompt, system="", **kw):
        return {"kind": "rule_add", "rationale": "x",
                "rule": {"name": "Backdoor",
                         "conditions": [{"field_path": "a", "operator": "eq",
                                         "value": 1}],
                         "actions": [{"action_type": "set_value",
                                      "target": "case.security_level",
                                      "value": 0}]}}
    monkeypatch.setattr(proposer, "_ai_generate_json", hostile)

    r = await client.post("/api/v1/hxevolve/scan",
                          json={"case_type_id": str(ct_id)})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["insights"] == []           # nothing surfaced
    assert body["recorded"] == 1            # …but provenance recorded

    # not in the default list…
    lst = await client.get(f"/api/v1/hxevolve/insights?case_type_id={ct_id}")
    assert lst.json()["insights"] == []
    # …but auditable on request
    aud = await client.get(
        f"/api/v1/hxevolve/insights?case_type_id={ct_id}&include_discarded=true")
    rows = aud.json()["insights"]
    assert len(rows) == 1 and rows[0]["status"] == "discarded_gate"


@pytest.mark.asyncio
async def test_dismiss_transitions(client, session):
    ct = await _mk_ct(session)
    i = await _mk_insight(session, ct)
    r = await client.post(f"/api/v1/hxevolve/insights/{i.id}/dismiss")
    assert r.status_code == 200 and r.json()["status"] == "dismissed"
    r2 = await client.post(f"/api/v1/hxevolve/insights/{i.id}/dismiss")
    assert r2.status_code == 400


# ── sweep: cohort concurrency parity ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_prover_respects_tenant_cohort_cap(session):
    """Parity with the manual cohort endpoint: a busy tenant gets an honest
    insufficient-evidence verdict, never a second concurrent cohort."""
    from case_service.db.models import ReplayRunModel
    from case_service.hxevolve import prover

    session.add(ReplayRunModel(tenant_id="default", kind="cohort",
                               status="running", cohort_filter={},
                               candidate={}, created_by="other"))
    await session.commit()

    out = await prover._prove_by_replay(
        session, {"id": "x", "name": "x"}, uuid.uuid4(), "default", "tester")
    assert out["verdict"] == "insufficient_evidence"
    assert "already running" in out["evidence"]["error"]
    assert out["replay_run_id"] is None


# ── P3: config + frequency cap ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_config_defaults_and_upsert(client, session):
    ct = await _mk_ct(session)
    r = await client.get(f"/api/v1/hxevolve/config/{ct.id}")
    assert r.status_code == 200
    assert r.json()["is_default"] is True and r.json()["scan_enabled"] is False

    r2 = await client.put(f"/api/v1/hxevolve/config/{ct.id}", json={
        "min_improvement": 0.25, "max_auto_ratio_rise": 0.05,
        "min_coverage": 0.9, "min_determinate": 100,
        "scan_frequency_hours": 12, "scan_enabled": True})
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["min_improvement"] == 0.25 and body["scan_enabled"] is True
    assert body["is_default"] is False

    # config actually tightens the vetoes
    from case_service.hxevolve import prover
    v, reasons = prover._apply_vetoes(
        {"determinate": 100, "coverage_ratio": 0.95,
         "baseline": {"cycle_time": {"mean": 1000}, "auto_ratio": 0.2},
         "counterfactual": {"cycle_time": {"mean": 800}, "auto_ratio": 0.2},
         "cost": None},
        config=body)
    assert v == "discarded_guardrail"      # 20% < the configured 25% minimum


@pytest.mark.asyncio
async def test_scan_frequency_cap(client, session):
    ct = await _mk_ct(session)
    await _mk_insight(session, ct)          # a recent scan artefact exists

    r = await client.post("/api/v1/hxevolve/scan",
                          json={"case_type_id": str(ct.id)})
    assert r.status_code == 429             # capped

    r2 = await client.post("/api/v1/hxevolve/scan",
                           json={"case_type_id": str(ct.id), "force": True})
    assert r2.status_code == 201            # explicit admin override


# ── P2: staging opens the human-approved path ───────────────────────────────────

@pytest.mark.asyncio
async def test_stage_sla_insight_opens_case_type_branch(client, session):
    ct = await _mk_ct(session)
    ct_id, ct_name = ct.id, ct.name
    i = await _mk_insight(session, ct)
    iid = i.id

    r = await client.post(f"/api/v1/hxevolve/insights/{iid}/stage")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "staged" and body["branch_id"]

    branch = await session.get(ArtifactBranchModel, uuid.UUID(body["branch_id"]))
    assert branch.artifact_type == "case_type"
    assert branch.artifact_id == str(ct_id)
    # the branch content carries the patched definition; base is the live one
    assert branch.base_snapshot["definition_json"]["sla_policies"][0]["goal_duration"] == "PT4H"
    assert branch.content_snapshot["definition_json"]["sla_policies"][0]["goal_duration"] == "PT8H"
    assert "HxEvolve" in branch.description
    # the LIVE case type is untouched — only a human merge changes it
    session.expire_all()
    live = await session.get(CaseTypeModel, ct_id)
    assert live.definition_json["sla_policies"][0]["goal_duration"] == "PT4H"

    # staging twice is refused
    r2 = await client.post(f"/api/v1/hxevolve/insights/{iid}/stage")
    assert r2.status_code == 400


@pytest.mark.asyncio
async def test_stage_rule_add_creates_disabled_rule_behind_branch(client, session):
    ct = await _mk_ct(session)
    proposal = {
        "name": f"Auto approve small {uuid.uuid4().hex[:6]}", "version": "1.0.0",
        "rule_type": "when", "scope": "case_type", "scope_target_id": str(ct.id),
        "enabled": True, "priority": 0,
        "definition_json": {
            "conditions": [{"field_path": "claim.amount", "operator": "lt",
                            "value": 200}],
            "actions": [{"action_type": "auto_approve"}]},
        "description": "small claims", "id": "auto_approve_small",
    }
    i = await _mk_insight(session, ct, kind="rule_add", proposal=proposal)

    r = await client.post(f"/api/v1/hxevolve/insights/{i.id}/stage")
    assert r.status_code == 201, r.text
    body = r.json()

    rows = (await session.execute(
        select(RuleDefinitionModel).where(
            RuleDefinitionModel.name == proposal["name"]))).scalars().all()
    assert len(rows) == 1 and rows[0].enabled is False   # disabled until merge
    branch = await session.get(ArtifactBranchModel, uuid.UUID(body["branch_id"]))
    assert branch.artifact_type == "rule"
    assert branch.base_snapshot["enabled"] is False
    assert branch.content_snapshot["enabled"] is True    # the merge IS the enabling


@pytest.mark.asyncio
async def test_stage_revalidates_stale_proposal(client, session):
    ct = await _mk_ct(session)
    i = await _mk_insight(session, ct)
    iid = i.id
    # the SLA policy disappears between scan and stage
    ct.definition_json = {**_DEF, "sla_policies": []}
    await session.commit()
    r = await client.post(f"/api/v1/hxevolve/insights/{iid}/stage")
    assert r.status_code == 409

    session.expire_all()
    i2 = await session.get(HxEvolveInsightModel, iid)
    assert i2.status == "surfaced"          # untouched — can be dismissed/re-scanned
