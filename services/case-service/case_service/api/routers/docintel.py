"""P52 Document Intelligence & Storage router."""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser
from case_service.db.session import get_session
from case_service.docintel import service

router = APIRouter(prefix="/docintel", tags=["docintel"])


def _tenant(user: AuthenticatedUser) -> str:
    return getattr(user, "tenant_id", None) or "default"


def _actor(user: AuthenticatedUser) -> str:
    return (getattr(user, "username", None) or getattr(user, "email", None) or getattr(user, "user_id", None) or "system")


# ── Document Extraction ───────────────────────────────────────────────────────

class ExtractRequest(BaseModel):
    step_id:       str
    source_url:    str | None = None
    document_name: str | None = None
    connector_id:  uuid.UUID | None = None


class ExtractionOut(BaseModel):
    id:               uuid.UUID
    status:           str
    provider:         str
    document_name:    str | None
    extracted_fields: dict
    confidence:       float | None
    error:            str | None

    model_config = {"from_attributes": True}


@router.get("/cases/{case_id}/extractions", response_model=list[ExtractionOut])
async def list_extractions(
    case_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    return await service.list_extractions(session, case_id)


@router.post("/cases/{case_id}/extract", response_model=ExtractionOut)
async def extract_document(
    case_id: uuid.UUID,
    body:    ExtractRequest,
    session: AsyncSession = Depends(get_session),
    user:    AuthenticatedUser = Depends(get_current_user),
):
    try:
        row = await service.extract_document(
            session, case_id, body.step_id,
            _tenant(user), _actor(user),
            source_url=body.source_url,
            document_name=body.document_name,
            connector_id=body.connector_id,
        )
        await session.commit()
        return row
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, str(exc))


# ── Cloud Storage ─────────────────────────────────────────────────────────────

class StorageRouteRequest(BaseModel):
    step_id:       str
    document_name: str
    content_type:  str = "application/octet-stream"
    size_bytes:    int | None = None
    connector_id:  uuid.UUID | None = None


class StorageRouteOut(BaseModel):
    id:            uuid.UUID
    status:        str
    provider:      str
    document_name: str
    bucket:        str | None
    object_key:    str | None
    storage_url:   str | None
    presigned_url: str | None
    error:         str | None

    model_config = {"from_attributes": True}


@router.get("/cases/{case_id}/storage", response_model=list[StorageRouteOut])
async def list_storage_routes(
    case_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    return await service.list_storage_routes(session, case_id)


@router.post("/cases/{case_id}/store", response_model=StorageRouteOut)
async def route_to_storage(
    case_id: uuid.UUID,
    body:    StorageRouteRequest,
    session: AsyncSession = Depends(get_session),
    user:    AuthenticatedUser = Depends(get_current_user),
):
    try:
        row = await service.route_to_storage(
            session, case_id, body.step_id,
            body.document_name, _tenant(user), _actor(user),
            connector_id=body.connector_id,
            content_type=body.content_type,
            size_bytes=body.size_bytes,
        )
        await session.commit()
        return row
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(502, str(exc))


# ── Connectors list ───────────────────────────────────────────────────────────

@router.get("/connectors")
async def list_docintel_connectors(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    from sqlalchemy import select
    from case_service.db.models import ConnectorRegistryModel
    rows = (await session.execute(
        select(ConnectorRegistryModel).where(
            ConnectorRegistryModel.connector_type.in_(["docling", "s3"]),
            ConnectorRegistryModel.tenant_id == _tenant(user),
        )
    )).scalars().all()
    return [{"id": str(r.id), "name": r.name, "type": r.connector_type, "enabled": r.enabled} for r in rows]


@router.get("/extractions", response_model=list[ExtractionOut])
async def list_all_extractions(
    status: str | None = None,
    limit: int = 100,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    from sqlalchemy import select
    from case_service.db.models import DocExtractionJobModel
    q = select(DocExtractionJobModel).order_by(DocExtractionJobModel.created_at.desc()).limit(limit)
    if status:
        q = q.where(DocExtractionJobModel.status == status)
    rows = (await session.execute(q)).scalars().all()
    return rows


@router.get("/storage", response_model=list[StorageRouteOut])
async def list_all_storage_routes(
    status: str | None = None,
    limit: int = 100,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    from sqlalchemy import select
    from case_service.db.models import DocStorageRouteModel
    q = select(DocStorageRouteModel).order_by(DocStorageRouteModel.created_at.desc()).limit(limit)
    if status:
        q = q.where(DocStorageRouteModel.status == status)
    rows = (await session.execute(q)).scalars().all()
    return rows
