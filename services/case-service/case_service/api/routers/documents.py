"""Document management API router."""
from __future__ import annotations
import uuid
from typing import Optional

from fastapi import (
    APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile,
)
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser
from case_service.db.models import CaseInstanceModel
from case_service.db.session import get_session
from case_service.documents import DocumentService, generate_preview

router = APIRouter(prefix="/documents", tags=["documents"])
service = DocumentService()


class DocumentResponse(BaseModel):
    id: uuid.UUID
    case_id: uuid.UUID
    filename: str
    content_type: str
    current_version: int
    uploaded_by: Optional[str] = None
    tenant_id: Optional[str] = None
    is_deleted: bool
    created_at: str
    updated_at: str


class DocumentVersionResponse(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    version: int
    size_bytes: int
    sha256: str
    uploaded_by: Optional[str] = None
    created_at: str


def _doc_to_response(doc) -> DocumentResponse:
    return DocumentResponse(
        id=doc.id, case_id=doc.case_id, filename=doc.filename,
        content_type=doc.content_type, current_version=doc.current_version,
        uploaded_by=doc.uploaded_by, tenant_id=doc.tenant_id,
        is_deleted=doc.is_deleted,
        created_at=doc.created_at.isoformat() if doc.created_at else "",
        updated_at=doc.updated_at.isoformat() if doc.updated_at else "",
    )


def _ver_to_response(v) -> DocumentVersionResponse:
    return DocumentVersionResponse(
        id=v.id, document_id=v.document_id, version=v.version,
        size_bytes=v.size_bytes, sha256=v.sha256, uploaded_by=v.uploaded_by,
        created_at=v.created_at.isoformat() if v.created_at else "",
    )


@router.post("/upload", status_code=201, response_model=DocumentResponse)
async def upload_document(
    case_id: uuid.UUID = Form(...),
    file: UploadFile = File(...),
    uploaded_by: Optional[str] = Form(None),
    tenant_id: Optional[str] = Form(None),
    session: AsyncSession = Depends(get_session),
):
    from case_service.middleware.file_security import validate_upload_filename, safe_filename, ALLOWED_DOCUMENT_EXTENSIONS
    raw_name = file.filename or "unnamed"
    ok, reason = validate_upload_filename(raw_name, allowed_extensions=ALLOWED_DOCUMENT_EXTENSIONS)
    if not ok:
        raise HTTPException(400, f"File rejected: {reason}")
    safe_name = safe_filename(raw_name)
    data = await file.read()
    try:
        doc = await service.upload(
            session, case_id=case_id, filename=safe_name,
            data=data, content_type=file.content_type, uploaded_by=uploaded_by,
            tenant_id=tenant_id,
        )
    except ValueError as e:
        raise HTTPException(404, str(e))
    return _doc_to_response(doc)


@router.get("/{document_id}", response_model=DocumentResponse)
async def get_document(
    document_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    from case_service.db.models import DocumentModel
    doc = await session.get(DocumentModel, document_id)
    if doc is None or doc.is_deleted:
        raise HTTPException(404, "Document not found")
    return _doc_to_response(doc)


@router.get("/{document_id}/download")
async def download_document(
    document_id: uuid.UUID,
    version: Optional[int] = Query(None),
    session: AsyncSession = Depends(get_session),
):
    try:
        data, filename, content_type = await service.download(session, document_id, version)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return Response(
        content=data, media_type=content_type,
        headers={"content-disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{document_id}/preview")
async def preview_document(
    document_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    try:
        data, _, content_type = await service.download(session, document_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    png = generate_preview(data, content_type)
    if png is None:
        raise HTTPException(415, "Preview not available for this content type")
    return Response(content=png, media_type="image/png")


@router.delete("/{document_id}", status_code=204)
async def delete_document(
    document_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    from case_service.db.models import DocumentModel
    doc = await session.get(DocumentModel, document_id)
    if doc is None or doc.is_deleted:
        raise HTTPException(404, "Document not found")

    case = await session.get(CaseInstanceModel, doc.case_id)
    is_admin = user.is_admin or user.has_role("admin")
    is_case_owner = case is not None and case.created_by == user.user_id
    if not (is_admin or is_case_owner):
        raise HTTPException(
            403,
            "Only the case owner or an admin can delete this document.",
        )

    await service.delete(session, document_id, deleted_by=user.user_id)
    return Response(status_code=204)


@router.post("/{document_id}/versions", status_code=201, response_model=DocumentVersionResponse)
async def upload_new_version(
    document_id: uuid.UUID,
    file: UploadFile = File(...),
    uploaded_by: Optional[str] = Form(None),
    session: AsyncSession = Depends(get_session),
):
    data = await file.read()
    try:
        ver = await service.upload_version(
            session, document_id=document_id, data=data,
            filename=file.filename, uploaded_by=uploaded_by,
        )
    except ValueError as e:
        raise HTTPException(404, str(e))
    return _ver_to_response(ver)



@router.delete("/{document_id}/versions/{version}", status_code=200, response_model=DocumentResponse)
async def delete_document_version(
    document_id: uuid.UUID,
    version: int,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    from case_service.db.models import DocumentModel
    doc = await session.get(DocumentModel, document_id)
    if doc is None or doc.is_deleted:
        raise HTTPException(404, "Document not found")

    case = await session.get(CaseInstanceModel, doc.case_id)
    is_admin = user.is_admin or user.has_role("admin")
    is_case_owner = case is not None and case.created_by == user.user_id
    if not (is_admin or is_case_owner):
        raise HTTPException(
            403,
            "Only the case owner or an admin can delete versions.",
        )

    try:
        updated = await service.delete_version(session, document_id, version)
    except ValueError as e:
        msg = str(e)
        if "only remaining" in msg:
            raise HTTPException(409, msg)
        raise HTTPException(404, msg)
    return _doc_to_response(updated)


@router.get("/{document_id}/versions", response_model=list[DocumentVersionResponse])
async def list_versions(
    document_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    versions = await service.list_versions(session, document_id)
    return [_ver_to_response(v) for v in versions]


@router.get("/by-case/{case_id}", response_model=list[DocumentResponse])
async def list_case_documents(
    case_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    docs = await service.list_by_case(session, case_id)
    return [_doc_to_response(d) for d in docs]


# ── P39b: Staff toggle portal visibility ─────────────────────────

class PortalVisibilityUpdate(BaseModel):
    portal_visible: bool


@router.patch("/{document_id}/portal-visibility")
async def set_portal_visibility(
    document_id: uuid.UUID,
    body: PortalVisibilityUpdate,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Staff toggle — share or un-share a document with the customer portal."""
    from case_service.db.models import DocumentModel
    doc = await session.get(DocumentModel, document_id)
    if doc is None or doc.is_deleted:
        raise HTTPException(404, "Document not found")
    doc.portal_visible = body.portal_visible
    if body.portal_visible and not doc.portal_source:
        doc.portal_source = "staff"
    await session.commit()
    return {
        "id": str(doc.id),
        "filename": doc.filename,
        "portal_visible": doc.portal_visible,
        "portal_source": doc.portal_source,
    }
