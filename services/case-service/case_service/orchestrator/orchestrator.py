"""Migration orchestrator — ties Scout + Scout AI into an end-to-end pipeline.

Given a migration scan, produces a complete project with tasks
ordered by dependencies, and drives each through the full pipeline:
  analyze → generate → mark ported.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import (
    MigrationScanModel, MigrationProjectModel, MigrationTaskModel,
    ArtifactAnalysisModel,
)
from case_service.orchestrator.dependency_graph import (
    build_dependencies, topological_sort, phase_for_compatibility,
)

logger = logging.getLogger(__name__)


async def create_project_from_scan(
    session: AsyncSession,
    *,
    scan_id: uuid.UUID,
    name: str,
) -> uuid.UUID:
    """Create a migration project from a completed Scout scan.

    Builds tasks for every artifact, determines dependencies,
    and assigns phases + sequence numbers.
    """
    # Load the scan
    scan = await session.get(MigrationScanModel, scan_id)
    if not scan:
        raise ValueError("Scan not found")

    report = scan.scan_report or {}
    artifacts = report.get("artifacts", [])

    # Build dependency graph
    deps = build_dependencies(artifacts)
    ordered_ids = topological_sort(artifacts, deps)

    # Create project
    project = MigrationProjectModel(
        name=name,
        source_platform=scan.source_platform,
        scan_id=scan_id,
        status="ready",
        total_artifacts=len(artifacts),
        roadmap={"ordered_artifacts": ordered_ids},
        dependencies=deps,
    )
    session.add(project)
    await session.flush()

    # Create tasks in dependency order
    artifact_index = {a["identifier"]: a for a in artifacts}
    for seq, aid in enumerate(ordered_ids):
        if aid not in artifact_index:
            continue
        a = artifact_index[aid]
        phase = phase_for_compatibility(a.get("compatibility", "medium"))

        task = MigrationTaskModel(
            project_id=project.id,
            artifact_id=aid,
            artifact_type=a.get("type"),
            artifact_name=a.get("name", aid),
            phase=phase,
            sequence=seq,
            status="pending",
            depends_on=deps.get(aid, []),
            complexity=a.get("compatibility", "medium"),
            estimated_hours=a.get("effort_hours", 0.0),
        )
        session.add(task)

    await session.flush()
    return project.id


async def analyze_task(
    session: AsyncSession,
    task_id: uuid.UUID,
    *,
    ollama_url: str = "http://localhost:11434",
    ollama_model: str = "qwen2.5-coder:7b",
) -> dict[str, Any]:
    """Run deep AI analysis on a single task's artifact."""
    task = await session.get(MigrationTaskModel, task_id)
    if not task:
        raise ValueError("Task not found")

    # Mark analyzing
    task.status = "analyzing"
    task.started_at = datetime.now(timezone.utc)
    await session.flush()

    # Load source code from scan
    project = await session.get(MigrationProjectModel, task.project_id)
    scan = await session.get(MigrationScanModel, project.scan_id) if project.scan_id else None

    source_code = _find_artifact_source(scan, task.artifact_id) if scan else f"// {task.artifact_id}"

    # Run Scout AI analysis
    from case_service.scout.ai import analyzer as ai_analyzer

    analysis = await ai_analyzer.analyze_artifact(
        code=source_code,
        artifact_type=task.artifact_type or "activity",
        source_platform=project.source_platform or "unknown",
        identifier=task.artifact_id,
        model=ollama_model,
        ollama_url=ollama_url,
    )

    # Save analysis record
    analysis_model = ArtifactAnalysisModel(
        scan_id=project.scan_id,
        artifact_identifier=task.artifact_id,
        artifact_type=task.artifact_type,
        source_code=source_code[:10000],
        summary=analysis.summary,
        business_logic=analysis.business_logic,
        complexity=analysis.complexity,
        external_calls=analysis.external_calls,
        data_reads=analysis.data_reads,
        data_writes=analysis.data_writes,
        side_effects=analysis.side_effects,
        helix_mapping=analysis.helix_mapping,
        confidence=analysis.confidence,
        ai_model=ollama_model if analysis.source == "llm" else "heuristic",
    )
    session.add(analysis_model)
    await session.flush()

    task.analysis_id = analysis_model.id
    task.status = "ready"

    # Update project counter
    project.analyzed_count = (project.analyzed_count or 0) + 1
    await session.flush()

    return {
        "task_id": str(task_id),
        "analysis_id": str(analysis_model.id),
        "summary": analysis.summary,
        "complexity": analysis.complexity,
        "source": analysis.source,
    }


async def generate_code_for_task(
    session: AsyncSession,
    task_id: uuid.UUID,
    *,
    ollama_url: str = "http://localhost:11434",
    ollama_model: str = "qwen2.5-coder:7b",
) -> str:
    """Generate HELIX code for a task using its analysis."""
    task = await session.get(MigrationTaskModel, task_id)
    if not task:
        raise ValueError("Task not found")
    if not task.analysis_id:
        raise ValueError("Task not yet analyzed")

    task.status = "generating"
    await session.flush()

    analysis_model = await session.get(ArtifactAnalysisModel, task.analysis_id)
    project = await session.get(MigrationProjectModel, task.project_id)
    scan = await session.get(MigrationScanModel, project.scan_id) if project.scan_id else None
    source_code = _find_artifact_source(scan, task.artifact_id) if scan else ""

    from case_service.scout.ai import analyzer as ai_analyzer
    from case_service.scout.ai.analyzer import ArtifactAnalysis

    analysis_obj = ArtifactAnalysis(
        summary=analysis_model.summary or "",
        business_logic=analysis_model.business_logic or "",
        complexity=analysis_model.complexity or "low",
        external_calls=analysis_model.external_calls or [],
        data_reads=analysis_model.data_reads or [],
        data_writes=analysis_model.data_writes or [],
        side_effects=analysis_model.side_effects or [],
        helix_mapping=analysis_model.helix_mapping or {},
    )

    code = await ai_analyzer.generate_helix_code(
        analysis=analysis_obj,
        original_code=source_code,
        artifact_type=task.artifact_type or "activity",
        source_platform=project.source_platform or "unknown",
        model=ollama_model,
        ollama_url=ollama_url,
    )

    task.generated_code = code
    task.status = "generated"
    task.completed_at = datetime.now(timezone.utc)

    project.generated_count = (project.generated_count or 0) + 1
    await session.flush()

    return code


async def run_full_pipeline(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    ollama_url: str = "http://localhost:11434",
    ollama_model: str = "qwen2.5-coder:7b",
    max_tasks: int = 100,
) -> dict[str, Any]:
    """Run analyze + generate for every task in a project.

    Processes tasks in dependency order. Returns summary.
    """
    project = await session.get(MigrationProjectModel, project_id)
    if not project:
        raise ValueError("Project not found")

    project.status = "in_progress"
    await session.flush()

    # Get all pending/ready tasks in order
    stmt = select(MigrationTaskModel).where(
        MigrationTaskModel.project_id == project_id,
        MigrationTaskModel.status.in_(["pending", "ready"]),
    ).order_by(
        MigrationTaskModel.phase, MigrationTaskModel.sequence,
    ).limit(max_tasks)
    result = await session.execute(stmt)
    tasks = list(result.scalars().all())

    analyzed = 0
    generated = 0
    errors = []

    for task in tasks:
        try:
            # Analyze if not yet
            if task.status == "pending":
                await analyze_task(
                    session, task.id,
                    ollama_url=ollama_url, ollama_model=ollama_model,
                )
                analyzed += 1
            # Generate
            if task.status == "ready":
                await generate_code_for_task(
                    session, task.id,
                    ollama_url=ollama_url, ollama_model=ollama_model,
                )
                generated += 1
        except Exception as e:
            logger.warning("Task %s failed: %s", task.id, e)
            task.status = "failed"
            task.notes = str(e)
            errors.append({"task_id": str(task.id), "error": str(e)})

    # Update project status
    total = len(tasks)
    if errors and len(errors) == total:
        project.status = "failed"
    elif generated == total:
        project.status = "completed"
    else:
        project.status = "in_progress"

    await session.flush()

    return {
        "project_id": str(project_id),
        "total_tasks": total,
        "analyzed": analyzed,
        "generated": generated,
        "errors": errors,
        "status": project.status,
    }


def _find_artifact_source(scan, artifact_id: str) -> str:
    """Try to find source code for a specific artifact from the scan report."""
    if not scan or not scan.scan_report:
        return f"// {artifact_id}"

    for a in scan.scan_report.get("artifacts", []):
        if a.get("identifier") == artifact_id:
            # If scan stored raw source code, return it
            md = a.get("metadata", {})
            if "source_code" in md:
                return md["source_code"]
            # Otherwise synthesize a stub
            return f"// {a.get('name', artifact_id)}\n// type: {a.get('type')}\n// From {scan.source_platform}"

    return f"// {artifact_id}"
