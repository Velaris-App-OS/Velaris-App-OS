"""BPM App Importer API — P44.

Endpoints:
  POST /importer/upload            upload a BPM export → starts background pipeline
  GET  /importer/jobs              list all import jobs
  GET  /importer/jobs/{id}         job status + progress
  GET  /importer/jobs/{id}/report  full import report JSON
  GET  /importer/jobs/{id}/result  generated Helix objects (case_types, forms, SQL)
  DELETE /importer/jobs/{id}       delete a job record
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser
from case_service.db.models import ImportJobModel
from case_service.db.session import get_session, get_session_factory
from case_service.bpm_importer.pipeline import run_pipeline

router = APIRouter(prefix="/importer", tags=["bpm-importer"])

SUPPORTED_TOOLS = {"pega", "camunda", "appian", "servicenow"}


# ── Upload & start ────────────────────────────────────────────────────────────

@router.post("/upload", status_code=202)
async def upload_bpm_export(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    tool: str = Form(...),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Upload a BPM export file and start the five-pass import pipeline."""
    if tool.lower() not in SUPPORTED_TOOLS:
        raise HTTPException(400, f"tool must be one of: {', '.join(sorted(SUPPORTED_TOOLS))}")

    from case_service.middleware.file_security import validate_upload_filename, safe_filename, ALLOWED_BPM_IMPORT_EXTENSIONS
    filename = file.filename or "upload"
    ok, reason = validate_upload_filename(filename, allowed_extensions=ALLOWED_BPM_IMPORT_EXTENSIONS)
    if not ok:
        raise HTTPException(400, f"File rejected: {reason}")
    filename = safe_filename(filename)
    file_bytes = await file.read()

    if len(file_bytes) == 0:
        raise HTTPException(400, "Uploaded file is empty")

    job = ImportJobModel(
        tool=tool.lower(),
        filename=filename,
        status="pending",
        created_by=user.user_id,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)

    session_factory = get_session_factory()
    background_tasks.add_task(
        run_pipeline,
        job_id=job.id,
        tool=tool.lower(),
        filename=filename,
        file_bytes=file_bytes,
        session_factory=session_factory,
    )

    return {
        "job_id":   str(job.id),
        "tool":     job.tool,
        "filename": job.filename,
        "status":   job.status,
        "message":  "Import pipeline started. Poll /importer/jobs/{job_id} for progress.",
    }


# ── List jobs ─────────────────────────────────────────────────────────────────

@router.get("/jobs")
async def list_jobs(
    tool:   Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    stmt = select(ImportJobModel).order_by(desc(ImportJobModel.created_at))
    if tool:
        stmt = stmt.where(ImportJobModel.tool == tool.lower())
    if status:
        stmt = stmt.where(ImportJobModel.status == status)
    jobs = (await session.execute(stmt)).scalars().all()
    return {"jobs": [_job_summary(j) for j in jobs], "total": len(jobs)}


# ── Job detail ────────────────────────────────────────────────────────────────

@router.get("/jobs/{job_id}")
async def get_job(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    job = await _get_or_404(session, job_id)
    result = _job_summary(job)
    # Include pass1 manifest (file counts) — lightweight
    result["manifest"] = {
        "total_files":   job.pass1_result.get("total", 0),
        "skipped_files": job.pass1_result.get("skipped", 0),
        "type_counts":   job.pass1_result.get("type_counts", {}),
    }
    if job.error:
        result["error"] = job.error
    return result


# ── Report ────────────────────────────────────────────────────────────────────

@router.get("/jobs/{job_id}/report")
async def get_report(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    job = await _get_or_404(session, job_id)
    if job.status not in ("complete", "failed"):
        raise HTTPException(409, f"Job not complete yet (status: {job.status})")
    return job.report or {}


# ── Generated Helix objects ───────────────────────────────────────────────────

@router.get("/jobs/{job_id}/result")
async def get_result(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    """Return the generated Helix objects (case_types, forms, migration SQL)."""
    job = await _get_or_404(session, job_id)
    if job.status != "complete":
        raise HTTPException(409, f"Job not complete (status: {job.status})")
    pass4 = job.pass4_result or {}
    return {
        "job_id":        str(job.id),
        "tool":          job.tool,
        "filename":      job.filename,
        "case_types":    pass4.get("case_types", []),
        "forms":         pass4.get("forms", []),
        "migration_sql": pass4.get("migration_sql", ""),
        "sla_sql":       pass4.get("sla_sql", []),
        "access_group_sql": pass4.get("access_group_sql", []),
    }


# ── Delete ────────────────────────────────────────────────────────────────────

@router.delete("/jobs/{job_id}", status_code=204)
async def delete_job(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(get_current_user),
):
    job = await _get_or_404(session, job_id)
    await session.delete(job)
    await session.commit()


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _get_or_404(session: AsyncSession, job_id: uuid.UUID) -> ImportJobModel:
    job = await session.get(ImportJobModel, job_id)
    if not job:
        raise HTTPException(404, f"Job {job_id} not found")
    return job


def _job_summary(job: ImportJobModel) -> dict:
    return {
        "id":           str(job.id),
        "tool":         job.tool,
        "filename":     job.filename,
        "status":       job.status,
        "created_by":   job.created_by,
        "created_at":   job.created_at.isoformat() if job.created_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }
