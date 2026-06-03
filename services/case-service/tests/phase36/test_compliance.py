"""HELIX P36 — Audit chain, lineage, evidence reports."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient

from case_service.compliance.audit_chain import (
    seal_new_entries, verify_chain, chain_status, compute_row_hash, GENESIS_HASH,
)
from case_service.compliance.lineage import record_lineage_event, get_case_lineage
from case_service.compliance.reports import generate_evidence_pack, FRAMEWORKS
from case_service.db import repository as repo
from case_service.db.models import (
    CaseTypeModel, CaseInstanceModel, CaseAuditLogModel, AuditChainModel,
    DataLineageEventModel,
)


# ── Fixtures ─────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def case(session):
    ct = CaseTypeModel(
        name="P36-Type", version="1.0.0",
        lifecycle_process_id="lp-p36",
        definition_json={"stages": []},
    )
    session.add(ct); await session.flush()
    c = CaseInstanceModel(
        case_type_id=ct.id, case_type_version="1.0.0",
        status="new", priority="medium", data={},
    )
    session.add(c); await session.flush()
    return c


async def _emit_audit(session, case_id, action="test_action", actor="alice", details=None):
    await repo.append_audit_entry(session, data={
        "case_id": case_id, "action": action, "actor_id": actor,
        "actor_type": "user", "details": details or {},
    })


# ── Hash chain ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_01_genesis_seal_starts_at_zero(session, case):
    await _emit_audit(session, case.id, action="a1")
    result = await seal_new_entries(session)
    assert result["sealed"] == 1
    assert result["tip_sequence"] == 1


@pytest.mark.asyncio
async def test_02_seal_is_idempotent(session, case):
    await _emit_audit(session, case.id, action="a1")
    r1 = await seal_new_entries(session)
    r2 = await seal_new_entries(session)
    assert r1["sealed"] == 1
    assert r2["sealed"] == 0
    assert r2["tip_sequence"] == r1["tip_sequence"]


@pytest.mark.asyncio
async def test_03_chain_links_correctly(session, case):
    for i in range(5):
        await _emit_audit(session, case.id, action=f"act-{i}")
    await seal_new_entries(session)

    from sqlalchemy import select
    rows = (await session.execute(
        select(AuditChainModel).order_by(AuditChainModel.sequence.asc())
    )).scalars().all()
    assert len(rows) == 5
    assert rows[0].prev_hash == GENESIS_HASH
    for prev, curr in zip(rows, rows[1:]):
        assert curr.prev_hash == prev.content_hash


@pytest.mark.asyncio
async def test_04_verify_clean_chain(session, case):
    for i in range(3):
        await _emit_audit(session, case.id, action=f"a-{i}")
    await seal_new_entries(session)
    res = await verify_chain(session)
    assert res["verified"] is True
    assert res["chain_length"] == 3
    assert res["breaks"] == []


@pytest.mark.asyncio
async def test_05_tampered_audit_row_detected(session, case):
    await _emit_audit(session, case.id, action="original_action")
    await seal_new_entries(session)

    # Tamper: change action on the audit row directly
    from sqlalchemy import select
    audit = (await session.execute(select(CaseAuditLogModel))).scalar_one()
    audit.action = "tampered_action"
    await session.flush()

    res = await verify_chain(session)
    assert res["verified"] is False
    assert any(b["type"] == "content_tampered" for b in res["breaks"])


@pytest.mark.asyncio
async def test_06_broken_link_detected(session, case):
    await _emit_audit(session, case.id, action="a1")
    await _emit_audit(session, case.id, action="a2")
    await seal_new_entries(session)

    # Tamper: corrupt prev_hash on second chain row
    from sqlalchemy import select
    chains = (await session.execute(
        select(AuditChainModel).order_by(AuditChainModel.sequence.asc())
    )).scalars().all()
    chains[1].prev_hash = "deadbeef" * 8  # 64 chars
    await session.flush()

    res = await verify_chain(session)
    assert res["verified"] is False
    assert any(b["type"] == "broken_link" for b in res["breaks"])


@pytest.mark.asyncio
async def test_07_compute_row_hash_deterministic(session, case):
    await _emit_audit(session, case.id, action="hash_me", actor="bob")
    from sqlalchemy import select
    audit = (await session.execute(select(CaseAuditLogModel))).scalar_one()
    h1 = compute_row_hash(audit, GENESIS_HASH)
    h2 = compute_row_hash(audit, GENESIS_HASH)
    assert h1 == h2
    assert len(h1) == 64
    h3 = compute_row_hash(audit, "0" * 32 + "1" * 32)
    assert h3 != h1  # depends on prev_hash


@pytest.mark.asyncio
async def test_08_chain_status(session, case):
    for i in range(3):
        await _emit_audit(session, case.id, action=f"a{i}")
    s1 = await chain_status(session)
    assert s1["audit_rows"] == 3
    assert s1["sealed_rows"] == 0
    assert s1["unsealed_rows"] == 3

    await seal_new_entries(session)
    s2 = await chain_status(session)
    assert s2["unsealed_rows"] == 0
    assert s2["tip_sequence"] == 3


@pytest.mark.asyncio
async def test_09_max_rows_limits_seal_batch(session, case):
    for i in range(10):
        await _emit_audit(session, case.id, action=f"a{i}")
    r1 = await seal_new_entries(session, max_rows=4)
    assert r1["sealed"] == 4
    r2 = await seal_new_entries(session, max_rows=4)
    assert r2["sealed"] == 4
    r3 = await seal_new_entries(session, max_rows=4)
    assert r3["sealed"] == 2
    assert r3["tip_sequence"] == 10


# ── Lineage ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_10_record_and_read_lineage_event(session, case):
    await record_lineage_event(
        session, case_id=case.id, kind="data_update",
        field_path="customer.name", before_value="Alice", after_value="Alicia",
        actor_id="bob",
    )
    timeline = await get_case_lineage(session, case.id)
    den = [e for e in timeline if e["source_table"] == "data_lineage_events"]
    assert len(den) == 1
    assert den[0]["field_path"] == "customer.name"
    assert den[0]["before_value"] == {"value": "Alice"}
    assert den[0]["after_value"] == {"value": "Alicia"}


@pytest.mark.asyncio
async def test_11_lineage_merges_audit_log(session, case):
    await _emit_audit(session, case.id, action="case_created")
    await _emit_audit(session, case.id, action="status_changed")
    await record_lineage_event(
        session, case_id=case.id, kind="data_update",
        field_path="x", after_value=1, actor_id="alice",
    )
    timeline = await get_case_lineage(session, case.id)
    sources = {e["source_table"] for e in timeline}
    assert "data_lineage_events" in sources
    assert "case_audit_log" in sources
    assert len(timeline) == 3


@pytest.mark.asyncio
async def test_12_lineage_sorted_desc(session, case):
    import asyncio
    await record_lineage_event(session, case_id=case.id, kind="early")
    await asyncio.sleep(0.01)
    await record_lineage_event(session, case_id=case.id, kind="late")
    timeline = await get_case_lineage(session, case.id)
    den = [e for e in timeline if e["source_table"] == "data_lineage_events"]
    # Newest first
    assert den[0]["kind"] == "late"
    assert den[1]["kind"] == "early"


@pytest.mark.asyncio
async def test_13_lineage_empty_for_unknown_case(session):
    timeline = await get_case_lineage(session, uuid.uuid4())
    assert timeline == []


# ── Reports ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_14_frameworks_enumerated():
    assert "soc2" in FRAMEWORKS
    assert "iso27001" in FRAMEWORKS
    assert "controls" in FRAMEWORKS["soc2"]
    assert FRAMEWORKS["soc2"]["controls"]


@pytest.mark.asyncio
async def test_15_generate_soc2_pack_basic(session, case):
    await _emit_audit(session, case.id, action="a1")
    await seal_new_entries(session)

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=30)
    result = await generate_evidence_pack(
        session, framework="soc2",
        period_start=start, period_end=end,
        generated_by="alice",
    )
    assert result["chain_verified"] is True
    assert result["summary"]["chain_length"] == 1
    assert b"%PDF" in result["pdf_bytes"][:8]
    assert b'"framework": "soc2"' in result["json_bytes"]


@pytest.mark.asyncio
async def test_16_generate_iso27001_pack(session, case):
    await _emit_audit(session, case.id, action="x")
    await seal_new_entries(session)
    result = await generate_evidence_pack(
        session, framework="iso27001",
        period_start=datetime.now(timezone.utc) - timedelta(days=1),
        period_end=datetime.now(timezone.utc),
    )
    assert "ISO" in result["pack"]["framework_name"]
    assert "A.5.15 Access control" in result["pack"]["controls_covered"][0]


@pytest.mark.asyncio
async def test_17_unknown_framework_rejected(session):
    with pytest.raises(ValueError):
        await generate_evidence_pack(
            session, framework="hipaa",  # not implemented
            period_start=datetime.now(timezone.utc), period_end=datetime.now(timezone.utc),
        )


@pytest.mark.asyncio
async def test_18_pack_summary_counts_audit_actions(session, case):
    for i in range(5):
        await _emit_audit(session, case.id, action="case_created")
    await seal_new_entries(session)

    result = await generate_evidence_pack(
        session, framework="soc2",
        period_start=datetime.now(timezone.utc) - timedelta(days=1),
        period_end=datetime.now(timezone.utc) + timedelta(days=1),
    )
    audit_section = result["pack"]["evidence"]["audit_summary"]
    assert audit_section["by_action"].get("case_created") == 5


@pytest.mark.asyncio
async def test_19_pack_detects_unverified_chain(session, case):
    await _emit_audit(session, case.id, action="a1")
    await seal_new_entries(session)
    # Tamper
    from sqlalchemy import select
    audit = (await session.execute(select(CaseAuditLogModel))).scalar_one()
    audit.action = "altered"
    await session.flush()

    result = await generate_evidence_pack(
        session, framework="soc2",
        period_start=datetime.now(timezone.utc) - timedelta(days=1),
        period_end=datetime.now(timezone.utc),
    )
    assert result["chain_verified"] is False
    integrity = result["pack"]["evidence"]["audit_integrity"]
    assert integrity["verification"]["verified"] is False


# ── API ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_20_api_audit_status(client: AsyncClient):
    r = await client.get("/api/v1/compliance/audit/status")
    assert r.status_code == 200
    body = r.json()
    for k in ("audit_rows", "sealed_rows", "unsealed_rows", "tip_sequence"):
        assert k in body


@pytest.mark.asyncio
async def test_21_api_seal_then_verify(client: AsyncClient, case, session):
    await _emit_audit(session, case.id, action="api_test")
    await session.commit()

    r1 = await client.post("/api/v1/compliance/audit/seal")
    assert r1.status_code == 200
    assert r1.json()["sealed"] >= 1

    r2 = await client.get("/api/v1/compliance/audit/verify")
    assert r2.status_code == 200
    assert r2.json()["verified"] is True


@pytest.mark.asyncio
async def test_22_api_list_frameworks(client: AsyncClient):
    r = await client.get("/api/v1/compliance/frameworks")
    assert r.status_code == 200
    body = r.json()
    assert "soc2" in body and "iso27001" in body


@pytest.mark.asyncio
async def test_23_api_generate_and_list_report(client: AsyncClient, case, session):
    await _emit_audit(session, case.id, action="case_action")
    await session.commit()

    r = await client.post("/api/v1/compliance/reports/generate", json={
        "framework": "soc2", "period_days": 7,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["framework"] == "soc2"
    assert "report_id" in body

    r2 = await client.get("/api/v1/compliance/reports")
    assert r2.status_code == 200
    assert any(rep["id"] == body["report_id"] for rep in r2.json())


@pytest.mark.asyncio
async def test_24_api_download_report_pdf(client: AsyncClient, case, session):
    await _emit_audit(session, case.id, action="x")
    await session.commit()

    r = await client.post("/api/v1/compliance/reports/generate", json={
        "framework": "soc2", "period_days": 7,
    })
    rid = r.json()["report_id"]

    r2 = await client.get(f"/api/v1/compliance/reports/{rid}/download?fmt=pdf")
    assert r2.status_code == 200
    assert r2.headers["content-type"] == "application/pdf"
    assert r2.content[:4] == b"%PDF"

    r3 = await client.get(f"/api/v1/compliance/reports/{rid}/download?fmt=json")
    assert r3.status_code == 200
    assert b'"framework"' in r3.content


@pytest.mark.asyncio
async def test_25_api_lineage(client: AsyncClient, case, session):
    await record_lineage_event(
        session, case_id=case.id, kind="data_update",
        field_path="x", after_value=1, actor_id="alice",
    )
    await _emit_audit(session, case.id, action="audit_event")
    await session.commit()

    r = await client.get(f"/api/v1/compliance/lineage/{case.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] >= 2
    sources = {e["source_table"] for e in body["events"]}
    assert "data_lineage_events" in sources
    assert "case_audit_log" in sources
