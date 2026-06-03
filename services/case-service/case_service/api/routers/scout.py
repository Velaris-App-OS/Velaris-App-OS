"""Scout Migration Scanner API.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import MigrationScanModel
from case_service.auth.dependencies import require_role
from case_service.db.session import get_session
from case_service.scout import scanner
from case_service.scout.migration_planner import build_migration_plan

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/scout", tags=["scout"], dependencies=[Depends(require_role("devops", "admin"))])


class ScanRequest(BaseModel):
    name: str
    content: str
    source_platform: str = ""  # empty = auto-detect
    filename: str = ""


class ScanResponse(BaseModel):
    id: str
    name: str
    source_platform: str
    source_version: str
    status: str
    compatibility_score: float | None
    effort_weeks: int | None
    artifacts_found: dict[str, Any]


@router.get("/platforms")
async def list_supported_platforms():
    """List platforms Scout can scan."""
    return {
        "platforms": [
            {"id": "pega", "name": "Pega PRPC", "file_types": [".rap", ".xml", ".zip"]},
            {"id": "camunda", "name": "Camunda BPM", "file_types": [".bpmn", ".xml"]},
            {"id": "appian", "name": "Appian", "file_types": [".zip", ".xml"]},
        ]
    }


@router.post("/scan", response_model=ScanResponse)
async def scan_content(
    body: ScanRequest,
    session: AsyncSession = Depends(get_session),
):
    """Scan source platform content and save the analysis."""
    # Run scan
    result = scanner.scan(body.content, body.source_platform, body.filename)

    # Save to DB
    model = MigrationScanModel(
        name=body.name,
        source_platform=result.source_platform,
        source_version=result.source_version,
        filename=body.filename,
        status="completed" if not result.errors else "failed",
        compatibility_score=result.compatibility_score,
        effort_weeks=result.effort_weeks,
        artifacts_found=result.counts_by_type(),
        scan_report=result.to_dict(),
        error_message="; ".join(result.errors) if result.errors else None,
        completed_at=datetime.now(timezone.utc),
    )
    session.add(model)
    await session.flush()
    await session.commit()

    return ScanResponse(
        id=str(model.id),
        name=model.name,
        source_platform=model.source_platform,
        source_version=model.source_version or "",
        status=model.status,
        compatibility_score=model.compatibility_score,
        effort_weeks=model.effort_weeks,
        artifacts_found=model.artifacts_found or {},
    )


@router.post("/scan-upload", response_model=ScanResponse)
async def scan_file_upload(
    name: str = Query(...),
    source_platform: str = Query(""),
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
):
    """Upload a file for scanning."""
    raw = await file.read()
    try:
        content = raw.decode("utf-8", errors="replace")
    except Exception as e:
        raise HTTPException(400, f"Could not decode file: {e}")

    # Use scan logic
    result = scanner.scan(content, source_platform, file.filename or "")

    model = MigrationScanModel(
        name=name,
        source_platform=result.source_platform,
        source_version=result.source_version,
        filename=file.filename,
        status="completed" if not result.errors else "failed",
        compatibility_score=result.compatibility_score,
        effort_weeks=result.effort_weeks,
        artifacts_found=result.counts_by_type(),
        scan_report=result.to_dict(),
        error_message="; ".join(result.errors) if result.errors else None,
        completed_at=datetime.now(timezone.utc),
    )
    session.add(model)
    await session.flush()
    await session.commit()

    return ScanResponse(
        id=str(model.id),
        name=model.name,
        source_platform=model.source_platform,
        source_version=model.source_version or "",
        status=model.status,
        compatibility_score=model.compatibility_score,
        effort_weeks=model.effort_weeks,
        artifacts_found=model.artifacts_found or {},
    )


@router.get("/scans")
async def list_scans(
    session: AsyncSession = Depends(get_session),
):
    """List all migration scans."""
    stmt = select(MigrationScanModel).order_by(MigrationScanModel.created_at.desc())
    result = await session.execute(stmt)
    return [
        {
            "id": str(m.id),
            "name": m.name,
            "source_platform": m.source_platform,
            "source_version": m.source_version,
            "status": m.status,
            "compatibility_score": m.compatibility_score,
            "effort_weeks": m.effort_weeks,
            "artifacts_found": m.artifacts_found,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        }
        for m in result.scalars().all()
    ]


@router.get("/scans/{scan_id}")
async def get_scan(
    scan_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    """Get full scan report."""
    model = await session.get(MigrationScanModel, scan_id)
    if not model:
        raise HTTPException(404, "Scan not found")
    return {
        "id": str(model.id),
        "name": model.name,
        "source_platform": model.source_platform,
        "source_version": model.source_version,
        "filename": model.filename,
        "status": model.status,
        "compatibility_score": model.compatibility_score,
        "effort_weeks": model.effort_weeks,
        "artifacts_found": model.artifacts_found,
        "scan_report": model.scan_report,
        "error_message": model.error_message,
        "created_at": model.created_at.isoformat() if model.created_at else None,
        "completed_at": model.completed_at.isoformat() if model.completed_at else None,
    }


@router.get("/scans/{scan_id}/plan")
async def get_migration_plan(
    scan_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    """Get phased migration plan for a scan."""
    model = await session.get(MigrationScanModel, scan_id)
    if not model:
        raise HTTPException(404, "Scan not found")

    # Rebuild ScanResult from stored report
    from case_service.scout.base import (
        ScanResult, ScannedArtifact, ArtifactType, CompatibilityLevel,
    )

    report = model.scan_report or {}
    result = ScanResult(
        source_platform=report.get("source_platform", model.source_platform),
        source_version=report.get("source_version", model.source_version or ""),
    )
    for a in report.get("artifacts", []):
        try:
            result.artifacts.append(ScannedArtifact(
                artifact_type=ArtifactType(a["type"]),
                name=a["name"],
                identifier=a["identifier"],
                compatibility=CompatibilityLevel(a["compatibility"]),
                mapped_to=a.get("mapped_to"),
                effort_hours=a.get("effort_hours", 0.0),
                issues=a.get("issues", []),
            ))
        except (KeyError, ValueError):
            continue

    return build_migration_plan(result)


@router.delete("/scans/{scan_id}", status_code=204)
async def delete_scan(
    scan_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    model = await session.get(MigrationScanModel, scan_id)
    if not model:
        raise HTTPException(404, "Scan not found")
    await session.delete(model)
    await session.commit()
