"""NLP Process Builder API.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.config import get_settings
from case_service.auth.dependencies import require_role
from case_service.db.session import get_session
from case_service.hxnexus.factory import check_ai_available, get_ai_info
from case_service.nlp import case_type_builder

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/nlp", tags=["nlp"], dependencies=[Depends(require_role("designer", "admin"))])


class GenerateRequest(BaseModel):
    description: str = Field(..., min_length=10, max_length=5000)
    use_llm: bool = True
    deploy: bool = False  # If true, also deploy the case type
    name_override: Optional[str] = Field(None, max_length=200)  # User-edited name, applied on deploy


class GenerateResponse(BaseModel):
    name: str
    description: str
    default_priority: str
    stages: list[dict[str, Any]]
    sla_policies: list[dict[str, Any]]
    source: str
    deployed_case_type_id: str | None = None


class FullGenerateResponse(BaseModel):
    name: str
    description: str
    default_priority: str
    stages: list[dict[str, Any]]
    sla_policies: list[dict[str, Any]]
    variables: list[dict[str, Any]]
    notifications: list[dict[str, Any]]
    source: str
    deployed_case_type_id: str | None = None


class StatusResponse(BaseModel):
    ollama_available: bool   # kept for frontend compat — reflects any backend's availability
    ollama_url: str
    ollama_model: str
    nlp_enabled: bool
    fallback_enabled: bool
    ai_backend: str = "ollama"


@router.get("/status", response_model=StatusResponse)
async def get_status():
    """Check NLP service status."""
    settings = get_settings()
    available = await check_ai_available()
    info = get_ai_info()
    return StatusResponse(
        ollama_available=available,
        ollama_url=settings.ai_ollama_url,
        ollama_model=settings.ai_ollama_model,
        nlp_enabled=settings.nlp_enabled,
        fallback_enabled=settings.nlp_fallback_enabled,
        ai_backend=info["backend"],
    )


@router.post("/generate-case-type", response_model=GenerateResponse)
async def generate_case_type(
    body: GenerateRequest,
    session: AsyncSession = Depends(get_session),
):
    """Generate a case type definition from natural language."""
    settings = get_settings()

    if not settings.nlp_enabled:
        raise HTTPException(400, "NLP service is disabled")

    result = await case_type_builder.build_from_description(
        description=body.description,
        use_fallback=settings.nlp_fallback_enabled and body.use_llm,
    )

    if "error" in result:
        raise HTTPException(503, result["error"])

    source = result.pop("_source", "unknown")

    response_data = {
        "name": result["name"],
        "description": result["description"],
        "default_priority": result["default_priority"],
        "stages": result["stages"],
        "sla_policies": result["sla_policies"],
        "source": source,
        "deployed_case_type_id": None,
    }

    # Optionally deploy
    if body.deploy:
        from case_service.db import repository as repo
        import uuid as _uuid

        try:
            # Prefer user-supplied name override; fall back to AI-generated name
            deploy_name = (body.name_override or "").strip() or result["name"]
            existing = await repo.get_case_type_by_name(session, deploy_name)
            version = "1.0.0"
            if existing:
                version = f"1.0.{int(existing.version.split('.')[-1]) + 1}"

            data = {
                "name": deploy_name,
                "version": version,
                "lifecycle_process_id": None,
                "definition_json": {
                    "stages": result["stages"],
                    "sla_policies": result["sla_policies"],
                },
                "default_priority": result["default_priority"],
                "description": result["description"],
                "tags": ["nlp-generated"],
            }
            ct = await repo.create_case_type(session, data=data)
            await session.commit()
            response_data["deployed_case_type_id"] = str(ct.id)
        except Exception as e:
            logger.error("Failed to deploy NLP-generated case type: %s", e)
            await session.rollback()
            raise HTTPException(
                status_code=500,
                detail=f"Case type generated but deploy failed: {e}",
            )

    return GenerateResponse(**response_data)


@router.post("/generate-full", response_model=FullGenerateResponse)
async def generate_full(
    body: GenerateRequest,
    session: AsyncSession = Depends(get_session),
):
    """NLP Builder — Full mode.

    Generates a complete case type application shell: stages, form fields per step,
    SLA policies, data model fields, and notification triggers.
    Accessible from the NLP Builder UI via the Full mode toggle.
    """
    settings = get_settings()
    if not settings.nlp_enabled:
        raise HTTPException(400, "NLP service is disabled")

    result = await case_type_builder.build_full(
        description=body.description,
        use_fallback=settings.nlp_fallback_enabled and body.use_llm,
    )

    if "error" in result:
        raise HTTPException(503, result["error"])

    source = result.pop("_source", "unknown")
    result.pop("_full", None)

    deployed_id = None
    if body.deploy:
        from case_service.db import repository as repo
        try:
            deploy_name = (body.name_override or "").strip() or result["name"]
            existing = await repo.get_case_type_by_name(session, deploy_name)
            version = "1.0.0"
            if existing:
                version = f"1.0.{int(existing.version.split('.')[-1]) + 1}"
            ct = await repo.create_case_type(session, data={
                "name": deploy_name,
                "version": version,
                "lifecycle_process_id": None,
                "definition_json": {
                    "stages": result["stages"],
                    "sla_policies": result["sla_policies"],
                    "variables": result.get("variables", []),
                    "notifications": result.get("notifications", []),
                },
                "default_priority": result["default_priority"],
                "description": result["description"],
                "tags": ["nlp-builder-full"],
            })
            await session.commit()
            deployed_id = str(ct.id)
        except Exception as e:
            await session.rollback()
            raise HTTPException(500, f"NLP Builder full mode: deploy failed: {e}")

    return FullGenerateResponse(
        name=result["name"],
        description=result["description"],
        default_priority=result["default_priority"],
        stages=result["stages"],
        sla_policies=result["sla_policies"],
        variables=result.get("variables", []),
        notifications=result.get("notifications", []),
        source=source,
        deployed_case_type_id=deployed_id,
    )


@router.post("/preview")
async def preview_case_type(body: GenerateRequest):
    """Preview generation without saving — shorthand for generate with deploy=False."""
    body.deploy = False
    from fastapi import Request
    # Re-route to generate logic without the session dependency chain
    settings = get_settings()

    if not settings.nlp_enabled:
        raise HTTPException(400, "NLP service is disabled")

    result = await case_type_builder.build_from_description(
        description=body.description,
        use_fallback=settings.nlp_fallback_enabled and body.use_llm,
    )

    if "error" in result:
        raise HTTPException(503, result["error"])

    source = result.pop("_source", "unknown")
    return {
        **result,
        "source": source,
    }
