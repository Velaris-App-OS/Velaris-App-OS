"""HxDBMigrate P7 — the Compliance Migration Certificate (signed, board-presentable).

Assembles everything the platform actually recorded about a source's migration —
discovery findings, the PII actions genuinely taken per run, full run history,
table → case-type lineage with live case counts, and the cutover record — into a
canonical JSON artefact, then signs its SHA-256 with the platform auth key
(RS256 when the RSA keypair is configured, HS256 fallback otherwise; signed-off
decision 2026-07-05). A reportlab PDF renders the same content for humans.

Nothing in the certificate is invented: every figure is read back from the
analyses/runs/links tables. Raw source values never appear (the P2 invariant —
classifications and MASKED examples only — carries through).

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import hashlib
import io
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.config import get_settings
from case_service.db.models import (
    CaseTypeModel,
    HxDBMigrateAnalysisModel,
    HxDBMigrateMigrationRunModel,
    HxDBMigrateRowLinkModel,
    HxDBMigrateSourceModel,
)

CERTIFICATE_VERSION = "1.0"


# ── assembly ────────────────────────────────────────────────────────────────────

async def build_certificate(session: AsyncSession,
                            source: HxDBMigrateSourceModel) -> dict[str, Any]:
    """Canonical certificate JSON + detached signature block."""
    analyses = (await session.execute(
        select(HxDBMigrateAnalysisModel)
        .where(HxDBMigrateAnalysisModel.source_id == source.id)
        .order_by(desc(HxDBMigrateAnalysisModel.created_at))
    )).scalars().all()
    latest = next((a for a in analyses if a.status == "complete"), None)

    runs = (await session.execute(
        select(HxDBMigrateMigrationRunModel)
        .where(HxDBMigrateMigrationRunModel.source_id == source.id)
        .order_by(HxDBMigrateMigrationRunModel.created_at)
    )).scalars().all()

    lineage_rows = (await session.execute(
        select(HxDBMigrateRowLinkModel.table_name,
               HxDBMigrateRowLinkModel.case_type_id,
               func.count(HxDBMigrateRowLinkModel.id))
        .where(HxDBMigrateRowLinkModel.source_id == source.id)
        .group_by(HxDBMigrateRowLinkModel.table_name,
                  HxDBMigrateRowLinkModel.case_type_id)
    )).all()
    lineage = []
    for table_name, ct_id, case_count in lineage_rows:
        ct = await session.get(CaseTypeModel, ct_id) if ct_id else None
        lineage.append({
            "source_table": table_name,
            "case_type_id": str(ct_id) if ct_id else None,
            "case_type_name": ct.name if ct else None,
            "case_count": case_count,
        })

    compliance = (latest.report or {}).get("compliance") if latest else None
    payload: dict[str, Any] = {
        "certificate_version": CERTIFICATE_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "id": str(source.id),
            "name": source.name,
            "engine": source.source_type,
            "host": source.host,
            "database": source.database,
            "ssl_mode": source.ssl_mode,
            "read_only_access": True,      # by construction — SET TRANSACTION READ ONLY
            "status": source.status,
            "cutover_at": source.cutover_at.isoformat() if source.cutover_at else None,
            "rollback_window_hours": source.rollback_window_hours,
        },
        "discovery": {
            "analyses_run": len(analyses),
            "latest_quality_score": latest.quality_score if latest else None,
            "latest_pii_count": latest.pii_count if latest else None,
            "compliance_findings": compliance,   # classifications + MASKED examples only
        },
        "runs": [{
            "id": str(r.id), "kind": r.kind, "table": r.table_name,
            "status": r.status, "pii_mode": r.pii_mode, "dry_run": r.dry_run,
            "rows_read": r.rows_read, "rows_migrated": r.rows_migrated,
            "rows_updated": r.rows_updated,
            "excluded_columns": r.excluded_columns or [],
            "at": r.created_at.isoformat() if r.created_at else None,
        } for r in runs],
        "lineage": lineage,
        "statement": (
            "Data was read from the source under a read-only transaction and was "
            "never written back. Columns classified for tokenisation (payment "
            "card / national identifier patterns) were excluded from migration "
            "under the 'safe' PII mode wherever that mode was used; the "
            "excluded_columns of each run above are the authoritative record. "
            "Source-row-to-case lineage is retained in hxdbmigrate_row_links."
        ),
    }
    return _sign(payload)


# ── signing ─────────────────────────────────────────────────────────────────────

def _sign(payload: dict[str, Any]) -> dict[str, Any]:
    """SHA-256 over canonical JSON, signed as a compact JWS with the platform key."""
    import jwt

    canonical = json.dumps(payload, sort_keys=True, default=str)
    digest = hashlib.sha256(canonical.encode()).hexdigest()

    s = get_settings()
    if s.auth_rsa_private_key:
        algorithm, key, key_id = "RS256", s.auth_rsa_private_key, "platform-rsa"
    else:
        algorithm, key, key_id = "HS256", s.auth_secret, "platform-secret"
    signature = jwt.encode(
        {"sha256": digest, "v": CERTIFICATE_VERSION,
         "source_id": payload["source"]["id"],
         "generated_at": payload["generated_at"]},
        key, algorithm=algorithm, headers={"kid": key_id},
    )
    return {"certificate": payload,
            "sha256": digest,
            "signature": signature,
            "signature_algorithm": algorithm,
            "key_id": key_id}


# ── PDF rendering (reportlab — mirrors compliance/reports.py) ───────────────────

def render_pdf(signed: dict[str, Any]) -> bytes:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    cert = signed["certificate"]
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    width, height = letter
    y = height - 54

    def line(txt: str, size: int = 9, bold: bool = False, indent: int = 54) -> None:
        nonlocal y
        if y < 60:
            c.showPage()
            y = height - 54
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        c.drawString(indent, y, txt[:118])
        y -= size + 5

    line("Compliance Migration Certificate", 16, bold=True)
    line(f"Velaris HxDBMigrate · certificate v{cert['certificate_version']} · "
         f"generated {cert['generated_at']}", 8)
    y -= 8

    src = cert["source"]
    line("Source", 12, bold=True)
    line(f"{src['name']} — {src['engine']} @ {src['host']}/{src['database']} "
         f"(ssl: {src['ssl_mode']}, read-only access: yes)")
    line(f"Lifecycle: {src['status']}"
         + (f" · cutover {src['cutover_at']}" if src["cutover_at"] else "")
         + f" · rollback window {src['rollback_window_hours']}h")
    y -= 6

    disc = cert["discovery"]
    line("Discovery", 12, bold=True)
    line(f"Analyses run: {disc['analyses_run']} · latest quality score: "
         f"{disc['latest_quality_score']} · PII columns classified: "
         f"{disc['latest_pii_count']}")
    for f in ((disc.get("compliance_findings") or {}).get("findings") or [])[:25]:
        line(f"  {f.get('table')}.{f.get('column')} — {f.get('category')} → "
             f"{f.get('recommended_action')}", 8)
    y -= 6

    line("Migration & Sync Runs", 12, bold=True)
    for r in cert["runs"][:60]:
        excl = ",".join(r["excluded_columns"]) or "-"
        line(f"  {r['at']} · {r['kind']} {r['table']} · {r['status']}"
             f"{' (dry-run)' if r['dry_run'] else ''} · read {r['rows_read']}"
             f" migrated {r['rows_migrated']} updated {r['rows_updated']}"
             f" · pii={r['pii_mode']} · excluded: {excl}", 8)
    y -= 6

    line("Lineage (source table > case type)", 12, bold=True)
    for entry in cert["lineage"]:
        line(f"  {entry['source_table']} > {entry['case_type_name'] or '?'} "
             f"({entry['case_count']} cases)", 9)
    y -= 6

    line("Statement", 12, bold=True)
    words = cert["statement"].split()
    row = ""
    for w in words:
        if len(row) + len(w) > 100:
            line(f"  {row}", 8)
            row = w
        else:
            row = f"{row} {w}".strip()
    if row:
        line(f"  {row}", 8)
    y -= 6

    line("Signature", 12, bold=True)
    line(f"SHA-256: {signed['sha256']}", 8)
    line(f"Algorithm: {signed['signature_algorithm']} · key id: {signed['key_id']}", 8)
    sig = signed["signature"]
    for i in range(0, len(sig), 100):
        line(f"  {sig[i:i + 100]}", 7)

    c.showPage()
    c.save()
    return buf.getvalue()
