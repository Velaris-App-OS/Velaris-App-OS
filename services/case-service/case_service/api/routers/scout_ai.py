"""Scout AI API — deep code analysis endpoints.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.config import get_settings
from case_service.db.models import ArtifactAnalysisModel
from case_service.auth.dependencies import require_role
from case_service.db.session import get_session
from case_service.hxnexus.factory import check_ai_available, get_ai_info
from case_service.scout.ai import analyzer as ai_analyzer

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/scout-ai", tags=["scout-ai"], dependencies=[Depends(require_role("devops", "admin"))])


class AnalyzeRequest(BaseModel):
    code: str = Field(..., min_length=1, max_length=50000)
    artifact_type: str = "activity"
    source_platform: str = "pega"
    identifier: str = ""
    save: bool = False
    scan_id: str | None = None


class AnalyzeResponse(BaseModel):
    identifier: str
    summary: str
    business_logic: str
    complexity: str
    external_calls: list[str]
    data_reads: list[str]
    data_writes: list[str]
    side_effects: list[str]
    helix_mapping: dict[str, Any]
    confidence: float
    source: str
    saved_id: str | None = None


class GenerateCodeRequest(BaseModel):
    code: str = Field(..., min_length=1, max_length=50000)
    artifact_type: str = "activity"
    source_platform: str = "pega"


class StatusResponse(BaseModel):
    ollama_available: bool
    ollama_model: str
    features: list[str]


@router.get("/status", response_model=StatusResponse)
async def get_status():
    settings = get_settings()
    available = await check_ai_available()
    info = get_ai_info()
    return StatusResponse(
        ollama_available=available,
        ollama_model=f"{info['backend']}:{settings.ai_ollama_model}",
        features=[
            "Deep code analysis",
            "Business logic extraction",
            "Integration detection",
            "Data flow tracking",
            "Auto code generation",
        ],
    )


@router.post("/analyze", response_model=AnalyzeResponse)
async def analyze_artifact(
    body: AnalyzeRequest,
    session: AsyncSession = Depends(get_session),
):
    settings = get_settings()

    analysis = await ai_analyzer.analyze_artifact(
        code=body.code,
        artifact_type=body.artifact_type,
        source_platform=body.source_platform,
        identifier=body.identifier or "unknown",
        model=settings.ai_ollama_model,
        ollama_url=settings.ai_ollama_url,
    )

    saved_id = None
    if body.save:
        scan_uuid = uuid.UUID(body.scan_id) if body.scan_id else None
        model = ArtifactAnalysisModel(
            scan_id=scan_uuid,
            artifact_identifier=body.identifier or "unknown",
            artifact_type=body.artifact_type,
            source_code=body.code[:10000],
            summary=analysis.summary,
            business_logic=analysis.business_logic,
            complexity=analysis.complexity,
            external_calls=analysis.external_calls,
            data_reads=analysis.data_reads,
            data_writes=analysis.data_writes,
            side_effects=analysis.side_effects,
            helix_mapping=analysis.helix_mapping,
            confidence=analysis.confidence,
            ai_model=settings.ai_ollama_model if analysis.source == "llm" else "heuristic",
        )
        session.add(model)
        await session.flush()
        await session.commit()
        saved_id = str(model.id)

    return AnalyzeResponse(
        identifier=body.identifier or "unknown",
        summary=analysis.summary,
        business_logic=analysis.business_logic,
        complexity=analysis.complexity,
        external_calls=analysis.external_calls,
        data_reads=analysis.data_reads,
        data_writes=analysis.data_writes,
        side_effects=analysis.side_effects,
        helix_mapping=analysis.helix_mapping,
        confidence=analysis.confidence,
        source=analysis.source,
        saved_id=saved_id,
    )


@router.post("/generate-code")
async def generate_code(body: GenerateCodeRequest):
    settings = get_settings()

    analysis = await ai_analyzer.analyze_artifact(
        code=body.code,
        artifact_type=body.artifact_type,
        source_platform=body.source_platform,
        model=settings.ai_ollama_model,
        ollama_url=settings.ai_ollama_url,
    )

    code = await ai_analyzer.generate_helix_code(
        analysis=analysis,
        original_code=body.code,
        artifact_type=body.artifact_type,
        source_platform=body.source_platform,
        model=settings.ai_ollama_model,
        ollama_url=settings.ai_ollama_url,
    )

    return {
        "original_code": body.code,
        "generated_code": code,
        "analysis_summary": analysis.summary,
        "source": analysis.source,
    }


@router.get("/analyses")
async def list_analyses(
    scan_id: uuid.UUID | None = None,
    limit: int = 100,
    session: AsyncSession = Depends(get_session),
):
    stmt = select(ArtifactAnalysisModel).order_by(
        ArtifactAnalysisModel.created_at.desc()
    ).limit(limit)
    if scan_id:
        stmt = stmt.where(ArtifactAnalysisModel.scan_id == scan_id)

    result = await session.execute(stmt)
    return [
        {
            "id": str(a.id),
            "scan_id": str(a.scan_id) if a.scan_id else None,
            "artifact_identifier": a.artifact_identifier,
            "artifact_type": a.artifact_type,
            "summary": a.summary,
            "complexity": a.complexity,
            "confidence": a.confidence,
            "external_calls": a.external_calls or [],
            "side_effects": a.side_effects or [],
            "helix_mapping": a.helix_mapping or {},
            "ai_model": a.ai_model,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in result.scalars().all()
    ]


@router.get("/analyses/{analysis_id}")
async def get_analysis(
    analysis_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    model = await session.get(ArtifactAnalysisModel, analysis_id)
    if not model:
        raise HTTPException(404, "Analysis not found")
    return {
        "id": str(model.id),
        "scan_id": str(model.scan_id) if model.scan_id else None,
        "artifact_identifier": model.artifact_identifier,
        "artifact_type": model.artifact_type,
        "source_code": model.source_code,
        "summary": model.summary,
        "business_logic": model.business_logic,
        "complexity": model.complexity,
        "external_calls": model.external_calls or [],
        "data_reads": model.data_reads or [],
        "data_writes": model.data_writes or [],
        "side_effects": model.side_effects or [],
        "helix_mapping": model.helix_mapping or {},
        "generated_code": model.generated_code,
        "confidence": model.confidence,
        "ai_model": model.ai_model,
        "created_at": model.created_at.isoformat() if model.created_at else None,
    }


@router.delete("/analyses/{analysis_id}", status_code=204)
async def delete_analysis(
    analysis_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    stmt = delete(ArtifactAnalysisModel).where(ArtifactAnalysisModel.id == analysis_id)
    result = await session.execute(stmt)
    if result.rowcount == 0:
        raise HTTPException(404, "Analysis not found")
    await session.commit()
