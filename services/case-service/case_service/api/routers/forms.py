"""Forms API router.

CRUD for form definitions.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.api.schemas.forms import (
    FormCreate,
    FormListResponse,
    FormResponse,
    FormUpdate,
)
from case_service.db import repository as repo
from case_service.auth.dependencies import get_current_user
from case_service.db.session import get_session

router = APIRouter(prefix="/forms", tags=["forms"], dependencies=[Depends(get_current_user)])


@router.post("", response_model=FormResponse, status_code=201)
async def create_form(
    body: FormCreate,
    session: AsyncSession = Depends(get_session),
):
    # Validate data model reference if provided
    if body.data_model_id:
        dm = await repo.get_data_model(session, body.data_model_id)
        if dm is None:
            raise HTTPException(
                status_code=404, detail="Data model not found"
            )

    form = await repo.create_form(
        session,
        data={
            "name": body.name,
            "version": body.version,
            "data_model_id": body.data_model_id,
            "definition_json": body.definition_json,
        },
    )
    return form


@router.get("", response_model=FormListResponse)
async def list_forms(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
):
    items, total = await repo.list_forms(
        session,
        offset=(page - 1) * page_size,
        limit=page_size,
    )
    return FormListResponse(
        items=items, total=total, page=page, page_size=page_size
    )


@router.get("/{form_id}", response_model=FormResponse)
async def get_form(
    form_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    form = await repo.get_form(session, form_id)
    if form is None:
        raise HTTPException(status_code=404, detail="Form not found")
    return form


@router.patch("/{form_id}", response_model=FormResponse)
async def update_form(
    form_id: uuid.UUID,
    body: FormUpdate,
    session: AsyncSession = Depends(get_session),
):
    form = await repo.get_form(session, form_id)
    if form is None:
        raise HTTPException(status_code=404, detail="Form not found")

    values = {}
    if body.data_model_id is not None:
        values["data_model_id"] = body.data_model_id
    if body.definition_json is not None:
        values["definition_json"] = body.definition_json
    if body.version is not None:
        values["version"] = body.version

    if values:
        await repo.update_form(session, form_id, values=values)

    return await repo.get_form(session, form_id)


@router.delete("/{form_id}", status_code=204)
async def delete_form(
    form_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    deleted = await repo.delete_form(session, form_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Form not found")
