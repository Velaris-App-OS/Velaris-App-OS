"""HxMigrate v2 — Unified Migration Pipeline router.

Security: SEC-7 (upload size limit), SEC-10 (input validation), SEC-6 (auth selector).
"""
from __future__ import annotations

import os
import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser
from case_service.db.session import get_session, get_session_factory as _get_session_factory
from case_service.hxmigrate.security import (
    MAX_UPLOAD_BYTES,
    SUPPORTED_PLATFORMS as _PLATFORM_ALLOWLIST,
    validate_safe_name,
)

def get_pipeline_factory():
    """Dependency: returns the async session factory for background tasks.
    Overridable in tests via app.dependency_overrides."""
    return _get_session_factory()
from case_service.hxmigrate import service

router = APIRouter(prefix="/hxmigrate", tags=["hxmigrate"])

_CREATOR_MODES  = frozenset({"generate", "create"})
_AUTH_TYPES     = frozenset({"auto", "service", "user"})

# SEC-10: exact allowlist (string list for API docs)
SUPPORTED_PLATFORMS = sorted(_PLATFORM_ALLOWLIST)


def _tenant(user: AuthenticatedUser) -> str:
    return getattr(user, "tenant_id", None) or "default"


def _get_auth_options(user: AuthenticatedUser) -> dict:
    """SEC-6: determine which auth options are available for the Creator module."""
    has_service_token = bool(os.getenv("HELIX_SERVICE_TOKEN", "").strip())
    has_user_jwt      = True  # user is already authenticated
    options = []
    if has_service_token:
        options.append({"value": "service", "label": "Service Account (HELIX_SERVICE_TOKEN)"})
    if has_user_jwt:
        options.append({"value": "user", "label": f"Current User ({getattr(user, 'email', 'me')})"})
    return {
        "requires_selection": has_service_token and has_user_jwt,
        "options": options,
        "default": "service" if has_service_token else "user",
    }


# ── Schemas ───────────────────────────────────────────────────────────────────

class StageOut(BaseModel):
    stage:      int
    stage_name: str
    status:     str
    summary:    dict
    error:      str | None
    started_at: str | None
    finished_at: str | None

    model_config = {"from_attributes": True}

    @classmethod
    def from_model(cls, s: Any) -> "StageOut":
        return cls(
            stage=s.stage, stage_name=s.stage_name, status=s.status,
            summary=s.summary or {}, error=s.error,
            started_at=s.started_at.isoformat() if s.started_at else None,
            finished_at=s.finished_at.isoformat() if s.finished_at else None,
        )


class RunOut(BaseModel):
    id:              uuid.UUID
    name:            str
    source_platform: str
    status:          str
    mode:            str
    current_stage:   int
    scan_id:         uuid.UUID | None
    import_job_id:   uuid.UUID | None
    project_id:      uuid.UUID | None
    package_id:      uuid.UUID | None
    source_filename: str | None
    error:           str | None
    created_at:      str
    completed_at:    str | None
    stages:          list[StageOut] = []

    @classmethod
    def from_model(cls, r: Any) -> "RunOut":
        return cls(
            id=r.id, name=r.name, source_platform=r.source_platform,
            status=r.status, mode=r.mode, current_stage=r.current_stage,
            scan_id=r.scan_id, import_job_id=r.import_job_id,
            project_id=r.project_id, package_id=r.package_id,
            source_filename=r.source_filename, error=r.error,
            created_at=r.created_at.isoformat(),
            completed_at=r.completed_at.isoformat() if r.completed_at else None,
            stages=[StageOut.from_model(s) for s in (r.stages or [])],
        )


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/platforms")
async def list_platforms():
    return [
        {"id": "pega",           "label": "Pega",            "accepts": [".jar", ".zip", ".xml"]},
        {"id": "camunda",        "label": "Camunda",         "accepts": [".bpmn", ".xml", ".zip"]},
        {"id": "appian",         "label": "Appian",          "accepts": [".zip", ".xml"]},
        {"id": "servicenow",     "label": "ServiceNow",      "accepts": [".xml", ".zip"]},
        {"id": "jbpm",           "label": "jBPM / Kogito",   "accepts": [".bpmn", ".zip"]},
        {"id": "flowable",       "label": "Flowable",        "accepts": [".bpmn", ".xml", ".zip"]},
        {"id": "ibm",            "label": "IBM BAW / BPM",   "accepts": [".zip", ".xml"]},
        {"id": "oracle",         "label": "Oracle BPM",      "accepts": [".zip", ".xml"]},
        {"id": "bizagi",         "label": "Bizagi",          "accepts": [".bpm", ".zip"]},
        {"id": "power_automate", "label": "Power Automate",  "accepts": [".json", ".zip"]},
        {"id": "salesforce",     "label": "Salesforce Flow", "accepts": [".xml", ".zip"]},
        {"id": "nintex",         "label": "Nintex",          "accepts": [".xml", ".nwc", ".zip"]},
    ]


@router.get("/auth-options")
async def get_auth_options(user: AuthenticatedUser = Depends(get_current_user)):
    """SEC-6: return available Creator auth options before starting a run."""
    return _get_auth_options(user)


@router.post("/run", status_code=202)
async def start_run(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    source_platform: str = Form(...),
    name: str = Form(""),
    mode: str = Form("full"),
    creator_mode: str = Form("generate"),
    creator_auth_type: str = Form("auto"),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
    pipeline_factory = Depends(get_pipeline_factory),
):
    # SEC-10: platform allowlist
    if source_platform.lower().strip() not in _PLATFORM_ALLOWLIST:
        raise HTTPException(400, f"Unsupported platform.")

    # SEC-10: mode validation
    if creator_mode not in _CREATOR_MODES:
        raise HTTPException(400, "Invalid creator_mode.")
    if creator_auth_type not in _AUTH_TYPES:
        raise HTTPException(400, "Invalid creator_auth_type.")

    # SEC-7: upload size limit
    file_bytes = await file.read()
    if len(file_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File too large. Maximum upload size is 100 MB.")

    # SEC-10: name sanitisation
    run_name = (name.strip() or file.filename or "Unnamed Run")[:200]
    if not validate_safe_name(run_name):
        run_name = "Unnamed Run"

    tenant_id = _tenant(user)

    run = await service.create_run(
        session, tenant_id=tenant_id, name=run_name,
        source_platform=source_platform.lower().strip(),
        filename=(file.filename or "upload")[:500],
        file_size=len(file_bytes), mode=mode,
    )
    await session.commit()

    background_tasks.add_task(
        service.start_pipeline,
        pipeline_factory, run.id, run_name,
        source_platform.lower().strip(),
        (file.filename or "upload")[:500],
        file_bytes, tenant_id, mode,
    )

    auth_info = _get_auth_options(user)
    return {
        "run_id":       str(run.id),
        "status":       "pending",
        "message":      "Pipeline started",
        "creator_mode": creator_mode,
        "auth_options": auth_info if auth_info["requires_selection"] else None,
    }


@router.get("/runs", response_model=list[RunOut])
async def list_runs(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    rows = await service.list_runs(session, _tenant(user))
    return [RunOut.from_model(r) for r in rows]


@router.get("/runs/{run_id}", response_model=RunOut)
async def get_run(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    run = await service.get_run(session, run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    return RunOut.from_model(run)


@router.post("/runs/{run_id}/apply", status_code=202)
async def apply_run(
    run_id: uuid.UUID,
    creator_auth_type: str = Form("auto"),
    background_tasks: BackgroundTasks = None,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
    pipeline_factory = Depends(get_pipeline_factory),
):
    """Trigger the Creator module on a completed generate-mode run.

    This is the 'Apply to Velaris' action. It calls Creator in create mode
    using the resolved auth token.
    SEC-6: token resolved server-side, never passed through request body.
    """
    if creator_auth_type not in _AUTH_TYPES:
        raise HTTPException(400, "Invalid creator_auth_type.")

    run = await service.get_run(session, run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    if run.status != "completed":
        raise HTTPException(400, f"Run must be completed before applying (status: {run.status})")

    # SEC-6: resolve auth token server-side
    auth_token = _resolve_creator_token(creator_auth_type, user)
    if not auth_token:
        raise HTTPException(400, "No valid auth token available for Creator. Set HELIX_SERVICE_TOKEN or ensure user is logged in.")

    user_id   = getattr(user, "user_id", "") or getattr(user, "id", "") or ""
    # Use username (preferred_username claim) as the human-readable display name.
    # user.email is UUID@<domain> in dev-token mode; username is the actual login name.
    display_name = (
        getattr(user, "username", "") or
        getattr(user, "preferred_username", "") or
        getattr(user, "email", "") or
        str(user_id)
    )

    if background_tasks:
        background_tasks.add_task(
            _run_creator_apply,
            pipeline_factory, run_id, auth_token,
            str(user_id), display_name,
        )
    return {"run_id": str(run_id), "status": "applying", "message": "Creator apply started"}


def _resolve_creator_token(auth_type: str, user: AuthenticatedUser) -> str:
    """SEC-6: resolve the auth token for Creator. Never log the resolved value."""
    service_token = os.getenv("HELIX_SERVICE_TOKEN", "").strip()
    user_jwt      = getattr(user, "token", "") or getattr(user, "_raw_token", "") or getattr(user, "access_token", "")

    if auth_type == "service":
        return service_token
    if auth_type == "user":
        return user_jwt
    # auto: prefer service token, fall back to user JWT
    return service_token or user_jwt


async def _run_creator_apply(
    session_factory,
    run_id: uuid.UUID,
    auth_token: str,
    imported_by_user_id: str = "",
    imported_by_email: str = "",
) -> None:
    """Background task: run Creator in create mode on the run's ValidatedPlan."""
    from case_service.hxmigrate.creator import Creator
    try:
        async with session_factory() as session:
            run = await service.get_run(session, run_id)
            if not run or not run.import_job_id:
                return

            from case_service.db.models import ImportJobModel
            job = await session.get(ImportJobModel, run.import_job_id)
            if not job or not job.report:
                return

            from case_service.hxmigrate.schemas import ValidatedPlan
            try:
                plan = ValidatedPlan.model_validate(job.report)
            except Exception:
                return

        creator = Creator(
            dry_run=False,
            auth_token=auth_token,
            imported_by_user_id=imported_by_user_id,
            imported_by_email=imported_by_email,
            run_id=str(run_id),
        )
        report = await creator.create_all(plan)

        # Store creation report in job
        async with session_factory() as session:
            async with session.begin():
                job = await session.get(ImportJobModel, run.import_job_id)
                if job:
                    existing = dict(job.report or {})
                    existing["creator_report"] = report.summary()
                    existing["creator_detail"] = {
                        "created":   report.created[:100],
                        "failed":    report.failed[:50],
                        "conflicts": report.conflicts[:50],
                    }
                    job.report = existing

    except Exception as exc:
        from case_service.hxmigrate.security import sanitize_error
        logger.error("Creator apply failed for run %s: %s", run_id, sanitize_error(str(exc)))


@router.get("/runs/{run_id}/result")
async def get_result(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    run = await service.get_run(session, run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    if run.status not in ("completed", "partial"):
        raise HTTPException(400, f"Run not complete (status: {run.status})")

    result: dict = {"run_id": str(run.id), "name": run.name, "status": run.status}
    if run.scan_id:
        result["scan_id"] = str(run.scan_id)
    if run.import_job_id:
        from case_service.db.models import ImportJobModel
        job = await session.get(ImportJobModel, run.import_job_id)
        if job:
            result["generated"] = job.report
    if run.project_id:
        result["project_id"] = str(run.project_id)
    if run.package_id:
        result["package_id"] = str(run.package_id)

    return result
