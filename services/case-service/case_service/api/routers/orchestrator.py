"""Migration Orchestrator API.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import io
import json
import uuid
import zipfile
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.config import get_settings
from case_service.db.models import (
    MigrationProjectModel, MigrationTaskModel,
)
from case_service.auth.dependencies import require_role
from case_service.db.session import get_session
from case_service.orchestrator import orchestrator

router = APIRouter(prefix="/orchestrator", tags=["orchestrator"], dependencies=[Depends(require_role("devops", "admin"))])


class CreateProjectRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    scan_id: str


class ProjectResponse(BaseModel):
    id: str
    name: str
    source_platform: str | None
    scan_id: str | None
    status: str
    total_artifacts: int
    analyzed_count: int
    generated_count: int
    ported_count: int
    created_at: datetime


@router.post("/projects", response_model=ProjectResponse, status_code=201)
async def create_project(
    body: CreateProjectRequest,
    session: AsyncSession = Depends(get_session),
):
    """Create a migration project from a Scout scan."""
    try:
        scan_uuid = uuid.UUID(body.scan_id)
    except ValueError:
        raise HTTPException(400, "Invalid scan_id")

    try:
        project_id = await orchestrator.create_project_from_scan(
            session, scan_id=scan_uuid, name=body.name,
        )
        await session.commit()
    except ValueError as e:
        raise HTTPException(404, str(e))

    project = await session.get(MigrationProjectModel, project_id)
    return _project_response(project)


@router.get("/projects")
async def list_projects(
    status: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    """List all migration projects."""
    stmt = select(MigrationProjectModel).order_by(MigrationProjectModel.created_at.desc())
    if status:
        stmt = stmt.where(MigrationProjectModel.status == status)

    result = await session.execute(stmt)
    return [_project_response(p).model_dump() for p in result.scalars().all()]


@router.get("/projects/{project_id}")
async def get_project(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    project = await session.get(MigrationProjectModel, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return _project_response(project).model_dump()


@router.delete("/projects/{project_id}", status_code=204)
async def delete_project(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    project = await session.get(MigrationProjectModel, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    await session.delete(project)
    await session.commit()


@router.get("/projects/{project_id}/tasks")
async def list_project_tasks(
    project_id: uuid.UUID,
    status: str | None = None,
    phase: int | None = None,
    session: AsyncSession = Depends(get_session),
):
    """List all tasks in a project, ordered by phase+sequence."""
    stmt = select(MigrationTaskModel).where(
        MigrationTaskModel.project_id == project_id,
    ).order_by(MigrationTaskModel.phase, MigrationTaskModel.sequence)

    if status:
        stmt = stmt.where(MigrationTaskModel.status == status)
    if phase:
        stmt = stmt.where(MigrationTaskModel.phase == phase)

    result = await session.execute(stmt)
    return [
        {
            "id": str(t.id),
            "project_id": str(t.project_id),
            "artifact_id": t.artifact_id,
            "artifact_type": t.artifact_type,
            "artifact_name": t.artifact_name,
            "phase": t.phase,
            "sequence": t.sequence,
            "status": t.status,
            "depends_on": t.depends_on or [],
            "complexity": t.complexity,
            "estimated_hours": t.estimated_hours,
            "actual_hours": t.actual_hours,
            "generated_code": t.generated_code,
            "notes": t.notes,
            "started_at": t.started_at.isoformat() if t.started_at else None,
            "completed_at": t.completed_at.isoformat() if t.completed_at else None,
        }
        for t in result.scalars().all()
    ]


@router.get("/tasks/{task_id}")
async def get_task(
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    task = await session.get(MigrationTaskModel, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return {
        "id": str(task.id),
        "project_id": str(task.project_id),
        "artifact_id": task.artifact_id,
        "artifact_type": task.artifact_type,
        "artifact_name": task.artifact_name,
        "phase": task.phase,
        "sequence": task.sequence,
        "status": task.status,
        "depends_on": task.depends_on or [],
        "analysis_id": str(task.analysis_id) if task.analysis_id else None,
        "generated_code": task.generated_code,
        "complexity": task.complexity,
        "estimated_hours": task.estimated_hours,
        "actual_hours": task.actual_hours,
        "notes": task.notes,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
    }


@router.post("/tasks/{task_id}/analyze")
async def run_analyze(
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    """Run deep AI analysis on a single task."""
    settings = get_settings()
    try:
        result = await orchestrator.analyze_task(
            session, task_id,
            ollama_url=settings.ai_ollama_url,
            ollama_model=settings.ai_ollama_model,
        )
        await session.commit()
        return result
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.post("/tasks/{task_id}/generate")
async def run_generate(
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    """Generate HELIX code for a task."""
    settings = get_settings()
    try:
        code = await orchestrator.generate_code_for_task(
            session, task_id,
            ollama_url=settings.ai_ollama_url,
            ollama_model=settings.ai_ollama_model,
        )
        await session.commit()
        return {"task_id": str(task_id), "generated_code": code}
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/tasks/{task_id}/mark-ported")
async def mark_ported(
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    """Mark a task as successfully ported (human-verified)."""
    task = await session.get(MigrationTaskModel, task_id)
    if not task:
        raise HTTPException(404, "Task not found")

    task.status = "ported"
    task.completed_at = datetime.utcnow()

    # Update project counter
    project = await session.get(MigrationProjectModel, task.project_id)
    if project:
        project.ported_count = (project.ported_count or 0) + 1

    await session.commit()
    return {"status": "ported"}


@router.post("/projects/{project_id}/run-all")
async def run_full_pipeline(
    project_id: uuid.UUID,
    max_tasks: int = Query(50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
):
    """Run analyze + generate for all pending tasks in project."""
    settings = get_settings()
    try:
        result = await orchestrator.run_full_pipeline(
            session, project_id,
            ollama_url=settings.ai_ollama_url,
            ollama_model=settings.ai_ollama_model,
            max_tasks=max_tasks,
        )
        await session.commit()
        return result
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.get("/projects/{project_id}/roadmap")
async def get_roadmap(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    """Get the full roadmap for a project — tasks grouped by phase."""
    project = await session.get(MigrationProjectModel, project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    stmt = select(MigrationTaskModel).where(
        MigrationTaskModel.project_id == project_id,
    ).order_by(MigrationTaskModel.phase, MigrationTaskModel.sequence)
    result = await session.execute(stmt)
    tasks = list(result.scalars().all())

    phases: dict[int, dict] = {}
    phase_names = {
        1: "Quick Wins (Full Compatibility)",
        2: "High-Compatibility Migration",
        3: "Redesign Phase",
        4: "Complex Migration",
    }

    for t in tasks:
        if t.phase not in phases:
            phases[t.phase] = {
                "phase": t.phase,
                "name": phase_names.get(t.phase, f"Phase {t.phase}"),
                "tasks": [],
                "total_hours": 0.0,
            }
        phases[t.phase]["tasks"].append({
            "id": str(t.id),
            "artifact_name": t.artifact_name,
            "artifact_type": t.artifact_type,
            "status": t.status,
            "complexity": t.complexity,
            "estimated_hours": t.estimated_hours,
            "depends_on": t.depends_on or [],
        })
        phases[t.phase]["total_hours"] += t.estimated_hours or 0

    return {
        "project_id": str(project_id),
        "project_name": project.name,
        "source_platform": project.source_platform,
        "total_artifacts": project.total_artifacts,
        "analyzed_count": project.analyzed_count,
        "generated_count": project.generated_count,
        "ported_count": project.ported_count,
        "phases": list(phases.values()),
        "dependencies": project.dependencies or {},
    }


@router.get("/projects/{project_id}/export")
async def export_project_zip(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    """Download the complete migration package (roadmap + all generated code)."""
    project = await session.get(MigrationProjectModel, project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    stmt = select(MigrationTaskModel).where(
        MigrationTaskModel.project_id == project_id,
    ).order_by(MigrationTaskModel.phase, MigrationTaskModel.sequence)
    result = await session.execute(stmt)
    tasks = list(result.scalars().all())

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # README
        readme = f"""# Migration Project: {project.name}

Source platform: {project.source_platform or 'unknown'}
Total artifacts: {project.total_artifacts}
Analyzed: {project.analyzed_count} / Generated: {project.generated_count} / Ported: {project.ported_count}

Generated by HELIX Migration Orchestrator (Phase 21)

## Structure

- `roadmap.json` — phased migration plan with dependencies
- `phase_1/` through `phase_4/` — generated Python code grouped by phase
- `_meta/` — task metadata per artifact

## Phases

1. **Quick Wins** — Full-compatibility artifacts, migrate first
2. **High-Compatibility** — Small adjustments needed
3. **Redesign** — Significant rework required
4. **Complex** — Major redesign; review carefully

## Usage

Review each generated file, adjust as needed for your HELIX setup,
then incorporate into your HELIX application.
"""
        zf.writestr(f"{project.name}/README.md", readme)

        # Roadmap JSON
        phases_dict: dict[int, list] = {}
        for t in tasks:
            phases_dict.setdefault(t.phase, []).append({
                "artifact_id": t.artifact_id,
                "artifact_name": t.artifact_name,
                "artifact_type": t.artifact_type,
                "complexity": t.complexity,
                "depends_on": t.depends_on or [],
                "status": t.status,
            })

        roadmap_data = {
            "project": project.name,
            "source_platform": project.source_platform,
            "phases": phases_dict,
        }
        zf.writestr(f"{project.name}/roadmap.json",
                    json.dumps(roadmap_data, indent=2, default=str))

        # Code files per phase
        for t in tasks:
            phase_dir = f"phase_{t.phase}"
            safe_name = (t.artifact_name or t.artifact_id).replace("/", "_").replace(" ", "_")[:100]
            if t.generated_code:
                zf.writestr(
                    f"{project.name}/{phase_dir}/{safe_name}.py",
                    f"# {t.artifact_name}\n# From: {t.artifact_id}\n# Complexity: {t.complexity}\n\n{t.generated_code}",
                )

            # Metadata for every task
            meta = {
                "artifact_id": t.artifact_id,
                "artifact_name": t.artifact_name,
                "artifact_type": t.artifact_type,
                "complexity": t.complexity,
                "status": t.status,
                "depends_on": t.depends_on or [],
                "estimated_hours": t.estimated_hours,
                "notes": t.notes,
            }
            zf.writestr(
                f"{project.name}/_meta/{safe_name}.json",
                json.dumps(meta, indent=2, default=str),
            )

    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{project.name.replace(" ", "_")}_migration.zip"',
        },
    )


def _project_response(p: MigrationProjectModel) -> ProjectResponse:
    return ProjectResponse(
        id=str(p.id),
        name=p.name,
        source_platform=p.source_platform,
        scan_id=str(p.scan_id) if p.scan_id else None,
        status=p.status,
        total_artifacts=p.total_artifacts,
        analyzed_count=p.analyzed_count,
        generated_count=p.generated_count,
        ported_count=p.ported_count,
        created_at=p.created_at,
    )
