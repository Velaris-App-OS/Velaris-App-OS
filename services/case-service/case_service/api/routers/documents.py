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


# ═══ HxMeet P4b — document verification (the document-first KYC gate) ═══
# Automated checks (local, no connector dependency) + the worker's checklist
# verdict, recorded per document. AI/automation never passes a document alone:
# `status` comes from the worker, the automated checks are evidence attached
# to that verdict. The video-KYC step stays locked by stage ordering until a
# passing record exists.

import io as _io
import uuid as _uuid
from datetime import date as _date

from case_service import hxguard as _hxguard
from case_service.db.models import DocumentModel as _DocModel, DocumentVerificationModel as _DocVerif
from sqlalchemy import select as _select


_MRZ_WEIGHTS = (7, 3, 1)


def _mrz_char_value(c: str) -> int:
    if c.isdigit():
        return int(c)
    if c == "<":
        return 0
    return ord(c.upper()) - 55  # A=10 … Z=35


def _mrz_check_digit(field: str) -> int:
    return sum(_mrz_char_value(c) * _MRZ_WEIGHTS[i % 3] for i, c in enumerate(field)) % 10


def _run_automated_checks(data: bytes, content_type: str, mrz: str | None,
                          expiry: str | None) -> list[dict]:
    """Local checks only. Each returns pass/fail/skipped with a human detail —
    a 'skipped' check is honest absence of signal, never counted as a pass."""
    checks: list[dict] = []

    # 1. File integrity: declared type must match magic bytes.
    magic_ok = None
    ct = (content_type or "").lower()
    if ct == "application/pdf":
        magic_ok = data[:5] == b"%PDF-"
    elif ct in ("image/png",):
        magic_ok = data[:8] == b"\x89PNG\r\n\x1a\n"
    elif ct in ("image/jpeg", "image/jpg"):
        magic_ok = data[:3] == b"\xff\xd8\xff"
    elif ct == "image/webp":
        magic_ok = data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    checks.append({
        "name": "file_integrity",
        "result": "skipped" if magic_ok is None else ("pass" if magic_ok else "fail"),
        "detail": f"declared {ct or 'unknown'}; magic bytes "
                  + ("match" if magic_ok else "do NOT match" if magic_ok is False else "not checked for this type"),
    })

    # 2. Image quality: too-small images can't be inspected for tampering.
    if ct.startswith("image/"):
        try:
            from PIL import Image
            img = Image.open(_io.BytesIO(data))
            w, h = img.size
            ok = w >= 600 and h >= 400
            checks.append({"name": "image_quality", "result": "pass" if ok else "fail",
                           "detail": f"{w}x{h}px" + ("" if ok else " — below 600x400 minimum for inspection")})
        except Exception:
            checks.append({"name": "image_quality", "result": "fail",
                           "detail": "image could not be decoded"})
    else:
        checks.append({"name": "image_quality", "result": "skipped", "detail": "not an image"})

    # 3. MRZ check digits (worker pastes or extraction supplies the MRZ line 2).
    if mrz:
        line = mrz.strip().replace(" ", "").upper()
        if len(line) >= 28:
            try:
                doc_no_ok = _mrz_check_digit(line[0:9]) == int(line[9])
                dob_ok    = _mrz_check_digit(line[13:19]) == int(line[19])
                exp_ok    = _mrz_check_digit(line[21:27]) == int(line[27])
                ok = doc_no_ok and dob_ok and exp_ok
                checks.append({"name": "mrz_check_digits", "result": "pass" if ok else "fail",
                               "detail": f"document={'ok' if doc_no_ok else 'BAD'} "
                                         f"birthdate={'ok' if dob_ok else 'BAD'} "
                                         f"expiry={'ok' if exp_ok else 'BAD'}"})
            except (ValueError, IndexError):
                checks.append({"name": "mrz_check_digits", "result": "fail",
                               "detail": "MRZ line is malformed"})
        else:
            checks.append({"name": "mrz_check_digits", "result": "fail",
                           "detail": "MRZ line too short (need the 28+ char second line)"})
    else:
        checks.append({"name": "mrz_check_digits", "result": "skipped", "detail": "no MRZ provided"})

    # 4. Expiry (ISO date supplied by worker or extraction).
    if expiry:
        try:
            exp = _date.fromisoformat(expiry.strip())
            ok = exp >= _date.today()
            checks.append({"name": "not_expired", "result": "pass" if ok else "fail",
                           "detail": f"expires {exp.isoformat()}" + ("" if ok else " — EXPIRED")})
        except ValueError:
            checks.append({"name": "not_expired", "result": "fail", "detail": "unparseable expiry date"})
    else:
        checks.append({"name": "not_expired", "result": "skipped", "detail": "no expiry provided"})

    return checks


class DocVerifyBody(BaseModel):
    # Optional inputs for the automated checks
    mrz_line2: str | None = None      # machine-readable zone, second line
    expiry_date: str | None = None    # ISO yyyy-mm-dd
    # The worker's verdict — REQUIRED; automation alone never passes a document
    status: str                       # passed | failed | review
    checklist: dict[str, bool] = {}   # e.g. {"photo_matches": true, "no_visible_tampering": true}
    notes: str | None = None


@router.post("/{document_id}/verify", status_code=201)
async def verify_document(
    document_id: _uuid.UUID,
    body: DocVerifyBody,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Record a document verification: automated checks + worker verdict."""
    if body.status not in ("passed", "failed", "review"):
        raise HTTPException(400, "status must be 'passed', 'failed', or 'review'")

    doc = await session.get(_DocModel, document_id)
    if doc is None or doc.is_deleted or doc.case_id is None:
        raise HTTPException(404, "Document not found")
    await _hxguard.require_case(session, user, "docs.verify", doc.case_id)

    data, _name, content_type = await service.download(session, document_id)
    checks = _run_automated_checks(data, content_type or doc.content_type,
                                   body.mrz_line2, body.expiry_date)
    checks.extend({"name": f"worker_checklist:{k}",
                   "result": "pass" if v else "fail",
                   "detail": "confirmed by worker" if v else "worker flagged"}
                  for k, v in (body.checklist or {}).items())

    # A worker cannot record "passed" over a failing automated check —
    # they must resolve it (re-upload) or record "review"/"failed".
    failed = [c["name"] for c in checks if c["result"] == "fail"]
    if body.status == "passed" and failed:
        raise HTTPException(409, f"Cannot record 'passed' with failing checks: {failed}")

    row = _DocVerif(case_id=doc.case_id, document_id=document_id,
                    status=body.status, checks=checks,
                    verified_by=user.user_id, notes=(body.notes or "").strip() or None)
    session.add(row)
    await session.flush()
    from case_service.api.routers.cases import _audit
    await _audit(session, doc.case_id, "document_verified", actor_id=user.user_id,
                 details={"document_id": str(document_id), "status": body.status,
                          "failed_checks": failed})
    await session.commit()
    return {"id": str(row.id), "status": row.status, "checks": checks}


@router.get("/{document_id}/verifications")
async def list_document_verifications(
    document_id: _uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    doc = await session.get(_DocModel, document_id)
    if doc is None or doc.is_deleted or doc.case_id is None:
        raise HTTPException(404, "Document not found")
    await _hxguard.require_case(session, user, "case.read", doc.case_id)
    rows = (await session.execute(
        _select(_DocVerif).where(_DocVerif.document_id == document_id)
        .order_by(_DocVerif.created_at.desc())
    )).scalars().all()
    return {"verifications": [
        {"id": str(r.id), "status": r.status, "checks": r.checks,
         "verified_by": r.verified_by, "notes": r.notes,
         "created_at": r.created_at.isoformat() if r.created_at else None}
        for r in rows
    ]}
