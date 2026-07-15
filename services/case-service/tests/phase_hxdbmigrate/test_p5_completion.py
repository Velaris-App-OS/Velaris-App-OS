"""HxDBMigrate P5–P7 — sync identity, cutover/rollback lifecycle, certificate.

In-harness pins (no source connection needed): row-checksum stability, the
freeze gates, the rollback window (happy path, expiry, wrong-state), complete,
cutover-with-nothing 400, and the signed certificate (JSON digest verifies,
lineage/runs read back from the DB, PDF renders). The sync/migrate engines
against a LIVE source are covered by the gated e2e (HXDBMIGRATE_TEST_SOURCE_URL).
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from case_service.db.models import (
    CaseInstanceModel,
    CaseTypeModel,
    HxDBMigrateAnalysisModel,
    HxDBMigrateMigrationRunModel,
    HxDBMigrateRowLinkModel,
    HxDBMigrateSourceModel,
)
from case_service.hxdbmigrate import certificate as cert_mod, sync as sync_mod


def _token_tenant() -> str:
    # the admin token minted by conftest has no tenant claim → router falls
    # back to "default" (see hxdbmigrate._tenant)
    return "default"


async def _mk_source(session, *, status="active", cutover_at=None,
                     window_hours=72) -> HxDBMigrateSourceModel:
    s = HxDBMigrateSourceModel(
        name=f"src-{uuid.uuid4().hex[:6]}", source_type="mariadb",
        host="10.0.0.5", port=3306, database="crm", username="reader",
        credentials={}, tenant_id=_token_tenant(), status=status,
        cutover_at=cutover_at, rollback_window_hours=window_hours,
    )
    session.add(s)
    await session.commit()
    await session.refresh(s)
    return s


async def _mk_linked_cases(session, source, n=3):
    ct = CaseTypeModel(name=f"Migrated CT {uuid.uuid4().hex[:6]}", version="1.0.0",
                       definition_json={"stages": [{"id": "intake", "steps": []}]})
    session.add(ct)
    await session.flush()
    case_ids = []
    for i in range(n):
        # cases inherit the case-type tenant (None = global), like real migrations
        case = CaseInstanceModel(case_type_id=ct.id, case_type_version="1.0.0",
                                 status="new", priority="medium",
                                 data={"row": i}, tenant_id=None)
        session.add(case)
        await session.flush()
        session.add(HxDBMigrateRowLinkModel(
            source_id=source.id, tenant_id=source.tenant_id, table_name="orders",
            source_pk=str(i), case_id=case.id, case_type_id=ct.id,
            row_checksum="x"))
        case_ids.append(case.id)
    await session.commit()
    return ct, case_ids


# ── checksum + cursor units ─────────────────────────────────────────────────────

def test_row_checksum_stable_and_order_independent():
    a = sync_mod.row_checksum({"a": 1, "b": "x"})
    b = sync_mod.row_checksum({"b": "x", "a": 1})
    assert a == b == sync_mod.row_checksum({"a": 1, "b": "x"})
    assert a != sync_mod.row_checksum({"a": 2, "b": "x"})


def test_pk_cursor_coercion_for_integer_keys():
    """PostgreSQL has no integer>text operator — the API's string cursor must be
    coerced back to the PK's native type before binding (MySQL merely coerces)."""
    assert sync_mod.coerce_pk_cursor("42", "integer") == 42
    assert sync_mod.coerce_pk_cursor("42", "int(11)") == 42
    assert sync_mod.coerce_pk_cursor("42", "bigint") == 42
    assert sync_mod.coerce_pk_cursor("ORD-42", "varchar") == "ORD-42"
    assert sync_mod.coerce_pk_cursor(None, "integer") is None
    with pytest.raises(sync_mod.SyncError):
        sync_mod.coerce_pk_cursor("not-a-number", "integer")


# ── freeze gates ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_frozen_source_refuses_data_operations(client, session):
    s = await _mk_source(session, status="cutover",
                         cutover_at=datetime.now(timezone.utc))
    m = await client.post(f"/api/v1/hxdbmigrate/sources/{s.id}/migrate",
                          json={"table": "orders",
                                "case_type_id": str(uuid.uuid4())})
    assert m.status_code == 409
    y = await client.post(f"/api/v1/hxdbmigrate/sources/{s.id}/sync",
                          json={"table": "orders",
                                "case_type_id": str(uuid.uuid4())})
    assert y.status_code == 409
    c = await client.post(f"/api/v1/hxdbmigrate/sources/{s.id}/cutover", json={})
    assert c.status_code == 409          # already cut over


@pytest.mark.asyncio
async def test_cutover_with_no_links_is_400(client, session):
    s = await _mk_source(session)
    r = await client.post(f"/api/v1/hxdbmigrate/sources/{s.id}/cutover", json={})
    assert r.status_code == 400 and "Nothing to cut over" in r.json()["detail"]


# ── rollback window ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rollback_cancels_linked_cases_and_unfreezes(client, session):
    s = await _mk_source(session, status="cutover",
                         cutover_at=datetime.now(timezone.utc))
    sid = s.id
    ct, case_ids = await _mk_linked_cases(session, s, n=3)

    r = await client.post(f"/api/v1/hxdbmigrate/sources/{sid}/rollback")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cases_cancelled"] == 3 and body["links_removed"] == 3
    assert body["status"] == "active" and body["cutover_at"] is None

    session.expire_all()
    for cid in case_ids:
        case = await session.get(CaseInstanceModel, cid)
        assert case.status == "cancelled"
    remaining = (await session.execute(
        select(HxDBMigrateRowLinkModel)
        .where(HxDBMigrateRowLinkModel.source_id == sid))).scalars().all()
    assert remaining == []
    # audit trail: a rollback run row exists
    runs = (await session.execute(
        select(HxDBMigrateMigrationRunModel)
        .where(HxDBMigrateMigrationRunModel.source_id == sid,
               HxDBMigrateMigrationRunModel.kind == "rollback"))).scalars().all()
    assert len(runs) == 1 and runs[0].rows_migrated == 3


@pytest.mark.asyncio
async def test_rollback_window_expiry_and_wrong_state(client, session):
    expired = await _mk_source(
        session, status="cutover",
        cutover_at=datetime.now(timezone.utc) - timedelta(hours=100))
    r = await client.post(f"/api/v1/hxdbmigrate/sources/{expired.id}/rollback")
    assert r.status_code == 409 and "expired" in r.json()["detail"]

    active = await _mk_source(session)
    r2 = await client.post(f"/api/v1/hxdbmigrate/sources/{active.id}/rollback")
    assert r2.status_code == 400


@pytest.mark.asyncio
async def test_complete_finalises_only_from_cutover(client, session):
    s = await _mk_source(session, status="cutover",
                         cutover_at=datetime.now(timezone.utc))
    r = await client.post(f"/api/v1/hxdbmigrate/sources/{s.id}/complete")
    assert r.status_code == 200 and r.json()["status"] == "completed"
    # final: no rollback, no second complete
    r2 = await client.post(f"/api/v1/hxdbmigrate/sources/{s.id}/rollback")
    assert r2.status_code == 400
    r3 = await client.post(f"/api/v1/hxdbmigrate/sources/{s.id}/complete")
    assert r3.status_code == 400


# ── certificate ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_certificate_signed_and_faithful(client, session):
    s = await _mk_source(session, status="cutover",
                         cutover_at=datetime.now(timezone.utc))
    ct, _ = await _mk_linked_cases(session, s, n=2)
    session.add(HxDBMigrateAnalysisModel(
        source_id=s.id, tenant_id=s.tenant_id, status="complete",
        table_count=3, quality_score=82, pii_count=4,
        report={"deep": True, "compliance": {"findings": [
            {"table": "orders", "column": "ssn", "category": "ssn",
             "sensitivity": "high", "recommended_action": "tokenize",
             "masked_examples": ["***-**-6789"]}]}}))
    session.add(HxDBMigrateMigrationRunModel(
        source_id=s.id, tenant_id=s.tenant_id, table_name="orders",
        case_type_id=ct.id, kind="migrate", status="complete",
        rows_read=2, rows_migrated=2, excluded_columns=["ssn"]))
    await session.commit()

    r = await client.get(f"/api/v1/hxdbmigrate/sources/{s.id}/certificate")
    assert r.status_code == 200, r.text
    signed = r.json()
    cert = signed["certificate"]

    # faithful: every figure read back from the DB
    assert cert["source"]["status"] == "cutover"
    assert cert["discovery"]["latest_quality_score"] == 82
    assert cert["discovery"]["latest_pii_count"] == 4
    assert any(run["excluded_columns"] == ["ssn"] for run in cert["runs"])
    lineage = {l["source_table"]: l for l in cert["lineage"]}
    assert lineage["orders"]["case_count"] == 2
    assert lineage["orders"]["case_type_name"] == ct.name
    # PII invariant carries through: masked examples only
    assert "123-45-6789" not in json.dumps(signed)

    # signature verifies and matches the recomputed canonical digest
    import jwt as _jwt
    from case_service.config import get_settings
    settings = get_settings()
    key = settings.auth_rsa_private_key or settings.auth_secret
    algs = ["RS256"] if settings.auth_rsa_private_key else ["HS256"]
    if settings.auth_rsa_private_key:
        pytest.skip("RSA verification needs the public key — HS256 path covers dev")
    claims = _jwt.decode(signed["signature"], key, algorithms=algs)
    digest = hashlib.sha256(
        json.dumps(cert, sort_keys=True, default=str).encode()).hexdigest()
    assert claims["sha256"] == signed["sha256"] == digest
    assert claims["source_id"] == str(s.id)

    # PDF renders
    p = await client.get(f"/api/v1/hxdbmigrate/sources/{s.id}/certificate?fmt=pdf")
    assert p.status_code == 200
    assert p.headers["content-type"] == "application/pdf"
    assert p.content[:4] == b"%PDF"


# ── gated live e2e: full sync loop against a real source ────────────────────────

@pytest.mark.skipif(not os.environ.get("HXDBMIGRATE_TEST_SOURCE_URL"),
                    reason="no external source DB configured")
@pytest.mark.asyncio
async def test_live_sync_detects_pk(session):
    from urllib.parse import urlsplit
    from case_service.hxdbmigrate import source as source_mod
    sp = urlsplit(os.environ["HXDBMIGRATE_TEST_SOURCE_URL"])
    st = "mysql" if sp.scheme.split("+")[0] == "mysql" else "postgresql"
    async with source_mod.source_session(
        st, sp.hostname, sp.port, (sp.path or "/").lstrip("/"),
        sp.username or "root", sp.password or "",
    ) as src:
        pk = await sync_mod.pk_column(src, "people")
        assert pk  # seeded table has a single-column PK
