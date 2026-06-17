"""Compliance API — audit verification, evidence packs, lineage."""
from __future__ import annotations
import io
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.auth.dependencies import get_current_user, require_role
from case_service.auth.models import AuthenticatedUser
from case_service.compliance import (
    chain_status, verify_chain, seal_new_entries,
    anchor_chain_tip, list_anchors,
    generate_evidence_pack, FRAMEWORKS,
    get_case_lineage,
)
from case_service.db.models import ComplianceReportModel
from case_service.db.session import get_analytics_session as get_session

router = APIRouter(prefix="/compliance", tags=["compliance"])


# ── Audit chain ──────────────────────────────────────────────────────

@router.get("/audit/status")
async def get_audit_status(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    return await chain_status(session)


@router.post("/audit/seal")
async def seal_chain(
    max_rows: int = Query(10000, ge=1, le=100000),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(require_role("admin")),
):
    return await seal_new_entries(session, max_rows=max_rows)


@router.get("/audit/verify")
async def verify_audit_chain(
    limit: Optional[int] = Query(None, ge=1, le=1_000_000),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    return await verify_chain(session, limit=limit)


# ── External anchoring (Group I — RFC-3161) ──────────────────────────

@router.get("/audit/anchors")
async def get_audit_anchors(
    limit: int = Query(50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    return await list_anchors(session, limit=limit)


@router.post("/audit/anchor")
async def trigger_audit_anchor(
    force: bool = Query(False, description="Anchor even if the tip is unchanged"),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(require_role("admin")),
):
    try:
        return await anchor_chain_tip(session, force=force)
    except Exception as e:
        raise HTTPException(502, f"Anchoring failed: {e}")


# ── Evidence packs ───────────────────────────────────────────────────

class GenerateReportBody(BaseModel):
    framework: str
    period_days: int = 30
    period_start: Optional[str] = None
    period_end: Optional[str] = None


@router.get("/frameworks")
async def list_frameworks():
    return {k: v["name"] for k, v in FRAMEWORKS.items()}


@router.post("/reports/generate")
async def generate_report(
    body: GenerateReportBody,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(require_role("admin")),
):
    if body.framework not in FRAMEWORKS:
        raise HTTPException(400, f"Unknown framework: {body.framework}")

    end = datetime.fromisoformat(body.period_end) if body.period_end else datetime.now(timezone.utc)
    if end.tzinfo is None: end = end.replace(tzinfo=timezone.utc)
    start = (
        datetime.fromisoformat(body.period_start)
        if body.period_start
        else end - timedelta(days=body.period_days)
    )
    if start.tzinfo is None: start = start.replace(tzinfo=timezone.utc)

    # Seal anything pending so the report covers all writes up to now
    await seal_new_entries(session)

    result = await generate_evidence_pack(
        session, framework=body.framework,
        period_start=start, period_end=end,
        generated_by=user.user_id, cadence="on_demand",
    )

    # Persist report metadata. Storage of bytes via MinIO is optional
    # (P24 storage backend can be wired in via tenant config); we store
    # the JSON inline in summary if small, otherwise store separately.
    report = ComplianceReportModel(
        id=uuid.uuid4(),
        framework=body.framework,
        period_start=start, period_end=end,
        generated_by=user.user_id,
        summary=result["summary"],
        chain_verified=result["chain_verified"],
        cadence="on_demand",
    )
    session.add(report)
    await session.flush()

    return {
        "report_id": str(report.id),
        "framework": body.framework,
        "summary": result["summary"],
        "chain_verified": result["chain_verified"],
        "json_size_bytes": len(result["json_bytes"]),
        "pdf_size_bytes": len(result["pdf_bytes"]),
        # Bytes are NOT returned in JSON — fetch them via the download endpoints
    }


@router.get("/reports")
async def list_reports(
    framework: Optional[str] = None,
    limit: int = Query(50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    q = select(ComplianceReportModel).order_by(ComplianceReportModel.generated_at.desc()).limit(limit)
    if framework:
        q = q.where(ComplianceReportModel.framework == framework)
    res = await session.execute(q)
    return [
        {
            "id": str(r.id),
            "framework": r.framework,
            "period_start": r.period_start.isoformat() if r.period_start else None,
            "period_end": r.period_end.isoformat() if r.period_end else None,
            "generated_by": r.generated_by,
            "generated_at": r.generated_at.isoformat() if r.generated_at else None,
            "summary": r.summary,
            "chain_verified": r.chain_verified,
            "cadence": r.cadence,
        }
        for r in res.scalars().all()
    ]


@router.get("/reports/{report_id}/download")
async def download_report(
    report_id: uuid.UUID,
    fmt: str = Query("json", pattern=r"^(json|pdf)$"),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    report = await session.get(ComplianceReportModel, report_id)
    if report is None:
        raise HTTPException(404, "Report not found")

    # Re-generate fresh bytes for the requested format. Reports are
    # deterministic given period + chain state, so re-generation is safe;
    # this avoids double-storing in DB and works without MinIO.
    result = await generate_evidence_pack(
        session, framework=report.framework,
        period_start=report.period_start, period_end=report.period_end,
        generated_by=report.generated_by, cadence=report.cadence,
    )
    fname = f"helix-{report.framework}-{report.period_start.date()}-to-{report.period_end.date()}.{fmt}"
    if fmt == "json":
        return Response(
            content=result["json_bytes"], media_type="application/json",
            headers={"content-disposition": f'attachment; filename="{fname}"'},
        )
    else:
        return Response(
            content=result["pdf_bytes"], media_type="application/pdf",
            headers={"content-disposition": f'attachment; filename="{fname}"'},
        )


# ── Lineage ──────────────────────────────────────────────────────────

@router.get("/lineage/{case_id}")
async def get_lineage(
    case_id: uuid.UUID,
    limit: int = Query(500, ge=1, le=5000),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    timeline = await get_case_lineage(session, case_id, limit=limit)
    return {"case_id": str(case_id), "events": timeline, "count": len(timeline)}
