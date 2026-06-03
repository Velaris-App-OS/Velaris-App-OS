"""Data Models API router.

CRUD for data model definitions.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.api.schemas.forms import (
    DataModelCreate,
    DataModelListResponse,
    DataModelResponse,
    DataModelUpdate,
)
from case_service.db import repository as repo
from case_service.auth.dependencies import require_role
from case_service.db.session import get_session

router = APIRouter(prefix="/data-models", tags=["data-models"], dependencies=[Depends(require_role("designer", "admin"))])


@router.post("", response_model=DataModelResponse, status_code=201)
async def create_data_model(
    body: DataModelCreate,
    session: AsyncSession = Depends(get_session),
):
    existing = await repo.get_data_model_by_name(
        session, body.name, body.version
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Data model '{body.name}' v{body.version} already exists",
        )

    dm = await repo.create_data_model(
        session,
        data={
            "name": body.name,
            "version": body.version,
            "definition_json": body.definition_json,
        },
    )
    return dm


@router.get("", response_model=DataModelListResponse)
async def list_data_models(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
):
    items, total = await repo.list_data_models(
        session,
        offset=(page - 1) * page_size,
        limit=page_size,
    )
    return DataModelListResponse(
        items=items, total=total, page=page, page_size=page_size
    )


@router.get("/{model_id}", response_model=DataModelResponse)
async def get_data_model(
    model_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    dm = await repo.get_data_model(session, model_id)
    if dm is None:
        raise HTTPException(
            status_code=404, detail="Data model not found"
        )
    return dm


@router.patch("/{model_id}", response_model=DataModelResponse)
async def update_data_model(
    model_id: uuid.UUID,
    body: DataModelUpdate,
    session: AsyncSession = Depends(get_session),
):
    dm = await repo.get_data_model(session, model_id)
    if dm is None:
        raise HTTPException(
            status_code=404, detail="Data model not found"
        )

    values = {}
    if body.definition_json is not None:
        values["definition_json"] = body.definition_json

    if values:
        await repo.update_data_model(session, model_id, values=values)

    return await repo.get_data_model(session, model_id)


@router.delete("/{model_id}", status_code=204)
async def delete_data_model(
    model_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    deleted = await repo.delete_data_model(session, model_id)
    if not deleted:
        raise HTTPException(
            status_code=404, detail="Data model not found"
        )
