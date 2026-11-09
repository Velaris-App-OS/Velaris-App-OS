"""Document management API router."""
from __future__ import annotations
import logging
import uuid
from typing import Optional

from fastapi import (
    Request,
    APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile,
)
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared import hxguard as _hxguard
from velaris_shared.auth.dependencies import get_current_user
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.core.error_codes import (
    DOC_DELETE_403,
    DOC_FILETYPE_400,
    DOC_CASE_404,
    DOC_ENCRYPT_503,
    DOC_LASTVERSION_409,
    DOC_NOTFOUND_404,
    DOC_PREVIEW_415,
    DOC_STORAGE_503,
    DOC_TENANT_403,
    DOC_UNREADABLE_409,
    coded,
)
from velaris_shared.db.models import CaseInstanceModel
from velaris_shared.db.session import get_session
from case_service.documents import DocumentService, generate_preview
from case_service.storage.backend import StorageError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/documents", tags=["documents"])


# ── Who may read a document (2026-08-15) ──────────────────────────
#
# Until now: nobody checked. These routes had no auth dependency and no check in
# their bodies, so every document in the deployment was readable by UUID.
#
# They cannot simply take `Depends(get_current_user)`, because the customer
# portal hands ANONYMOUS customers download links pointing here — that is why the
# hole existed. So there are exactly two ways in, and no third:
#
#   1. an authenticated caller who passes the case-level ReBAC check, or
#   2. a signed, expiring, single-document grant minted by the portal at the
#      moment it made the same decision (documents/access_tokens.py).
#
# Order matters: the grant is checked FIRST and returns immediately. A portal
# customer has no account, so falling through to `get_current_user` would 401
# them — the grant has to be able to answer on its own.

async def _authorize_document_read(
    session: AsyncSession, request: Request, document_id: uuid.UUID,
) -> None:
    """Raise 404 unless the caller may read this document.

    404 rather than 403, matching the case-action anti-oracle: a caller who may
    not read a document must not be able to confirm that the id exists.
    """
    from case_service.documents import access_tokens
    from velaris_shared.db.models import DocumentModel

    if access_tokens.verify(request.query_params.get("grant"), document_id):
        return

    user = await _optional_user(request)
    if user is None:
        raise coded(404, DOC_NOTFOUND_404, "Document not found")

    doc = await session.get(DocumentModel, document_id)
    if doc is None or doc.is_deleted:
        raise coded(404, DOC_NOTFOUND_404, "Document not found")
    if doc.case_id is not None:
        await _hxguard.require_case(session, user, "case.read", doc.case_id)


async def _optional_user(request: Request) -> AuthenticatedUser | None:
    """`get_current_user`, but absent credentials is None rather than a 401.

    Needed because these routes serve both an operator with a Bearer token and a
    portal customer with a grant; only after BOTH fail is the answer a refusal.
    """
    from velaris_shared.auth.dependencies import get_current_user as _gcu
    from fastapi.security import HTTPAuthorizationCredentials

    header = request.headers.get("authorization") or ""
    if not header.lower().startswith("bearer "):
        return None
    try:
        return await _gcu(
            request,
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=header[7:]),
        )
    except Exception as exc:
        from velaris_shared.auth.dependencies import is_auth_check_busy

        if is_auth_check_busy(exc):
            raise              # "try again", not "anonymous" (Studio retries it)
        return None            # an invalid token is not an identity


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


async def _upload_attribution(
    session: AsyncSession, user: AuthenticatedUser, case_id, tenant_id: str | None,
) -> tuple[str, str | None]:
    """Who uploaded this, and into which tenant — decided here, not by the form.

    Both fields arrived as `Form(...)` and were stored verbatim, so a caller
    named whoever it liked as the uploader and stamped whatever tenant string it
    liked on the row. No first-party client sends either (Studio's document
    manager and the form renderer both post `case_id` and `file` and nothing
    else), and the internal callers that legitimately set `uploaded_by` —
    HxMeet, the portal — reach `service.upload` directly and never come through
    this route.

    So the fields stay accepted, exactly as an install may already be sending
    them, and stop being believed:

    * **uploaded_by is the authenticated caller.** A value that disagreed with
      the token was a forged signature on somebody else's upload.
    * **tenant_id may only be the caller's own**, which is the rule
      `caller_tenant_or_refuse` already applies to every other create body. When
      it is absent the document inherits the CASE's tenant rather than staying
      NULL — nothing decides on this column today, and a document whose tenant
      is unknowable is one a later reader has to guess about.
    """
    from velaris_shared.db.models import CaseInstanceModel
    from case_service.tenancy.identity import caller_tenant_or_refuse, tenant_slug

    who = (getattr(user, "username", None) or getattr(user, "email", None)
           or getattr(user, "user_id", None) or "unknown")

    if tenant_id:
        try:
            resolved = await caller_tenant_or_refuse(session, user, tenant_id)
        except PermissionError as exc:
            raise coded(403, DOC_TENANT_403, str(exc))
        return str(who), resolved

    case = await session.get(CaseInstanceModel, case_id)
    inherited = getattr(case, "tenant_id", None) if case is not None else None
    return str(who), (await tenant_slug(session, inherited)) if inherited else None


@router.post("/upload", status_code=201, response_model=DocumentResponse)
async def upload_document(
    case_id: uuid.UUID = Form(...),
    file: UploadFile = File(...),
    uploaded_by: Optional[str] = Form(None),
    tenant_id: Optional[str] = Form(None),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    await _hxguard.require_case(session, user, "case.update", case_id)
    uploaded_by, tenant_id = await _upload_attribution(
        session, user, case_id, tenant_id)
    from velaris_shared.middleware.file_security import validate_upload_filename, safe_filename, ALLOWED_DOCUMENT_EXTENSIONS
    raw_name = file.filename or "unnamed"
    ok, reason = validate_upload_filename(raw_name, allowed_extensions=ALLOWED_DOCUMENT_EXTENSIONS)
    if not ok:
        raise coded(400, DOC_FILETYPE_400, f"File rejected: {reason}")
    safe_name = safe_filename(raw_name)
    data = await file.read()
    try:
        doc = await service.upload(
            session, case_id=case_id, filename=safe_name,
            data=data, content_type=file.content_type, uploaded_by=uploaded_by,
            tenant_id=tenant_id,
        )
    except ValueError as e:
        # `service.upload` raises ValueError when the case does not exist —
        # the catalog documents that as its own code, distinct from a missing
        # document, because the operator fix is completely different.
        raise coded(404, DOC_CASE_404, str(e))
    except (OSError, PermissionError) as e:
        # The catalog's reference incident: an unwritable storage path made
        # every upload a bare 500 with nothing to look up. Now it names the
        # code, and the operator resolution is one search away.
        raise coded(
            503, DOC_STORAGE_503,
            f"Document storage is unavailable: {e}. Check the configured "
            f"storage path is writable, or that the object store is reachable.",
        )
    except StorageError as e:
        # At-rest encryption is configured but unusable — a bad or missing
        # master key. Documented separately from a disk fault because the
        # operator fix is completely different, and the upload is NOT stored.
        #
        # Matched on the exception TYPE, not on message text: an earlier
        # version sniffed for "key" in the message, which would have reported
        # any KeyError mentioning a key as an encryption failure and sent an
        # operator chasing the wrong problem.
        raise coded(
            503, DOC_ENCRYPT_503,
            f"Document encryption or storage failed: {e}. If at-rest "
            f"encryption is enabled, check VELARIS_CASE_STORAGE_MASTER_KEY is "
            f"a valid 64-char hex key.",
        )
    return _doc_to_response(doc)


@router.get("/{document_id}", response_model=DocumentResponse)
async def get_document(
    document_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    await _authorize_document_read(session, request, document_id)
    from velaris_shared.db.models import DocumentModel
    doc = await session.get(DocumentModel, document_id)
    if doc is None or doc.is_deleted:
        raise coded(404, DOC_NOTFOUND_404, "Document not found")
    return _doc_to_response(doc)


@router.get("/{document_id}/download")
async def download_document(
    document_id: uuid.UUID,
    request: Request,
    version: Optional[int] = Query(None),
    session: AsyncSession = Depends(get_session),
):
    await _authorize_document_read(session, request, document_id)
    try:
        data, filename, content_type = await service.download(session, document_id, version)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except StorageError as e:
        raise _unreadable(document_id, e)
    return Response(
        content=data, media_type=content_type,
        headers={"content-disposition": f'attachment; filename="{filename}"'},
    )


def _unreadable(document_id: uuid.UUID, exc: Exception):
    """A stored document this install cannot decrypt: its key changed or is gone.

    A bare 500 said nothing (R3 2026-11-01, after a storage key was removed). The
    detail names the cause for the operator; the bytes are not served.
    """
    logger.error("document %s unreadable: %s", document_id, exc)
    return coded(409, DOC_UNREADABLE_409,
                 "This document cannot be read on this install: its encryption key is not the one it "
                 "was stored with (VELARIS_CASE_STORAGE_MASTER_KEY changed or was removed).")


@router.get("/{document_id}/preview")
async def preview_document(
    document_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    await _authorize_document_read(session, request, document_id)
    try:
        data, _, content_type = await service.download(session, document_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except StorageError as e:
        raise _unreadable(document_id, e)
    png = generate_preview(data, content_type)
    if png is None:
        raise coded(415, DOC_PREVIEW_415, "Preview not available for this content type")
    return Response(content=png, media_type="image/png")


@router.delete("/{document_id}", status_code=204)
async def delete_document(
    document_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    from velaris_shared.db.models import DocumentModel
    doc = await session.get(DocumentModel, document_id)
    if doc is None or doc.is_deleted:
        raise coded(404, DOC_NOTFOUND_404, "Document not found")

    # The case boundary FIRST. `docs.files.delete` is a PLATFORM privilege and
    # the check below asks only "admin, or the case's creator?" — so an admin in
    # one tenant could delete a document on another tenant's case, and did:
    # proven on 2026-09-06 on the sibling route below. `require_case` carries
    # the tenant clause since the same day and answers 404, so a document in a
    # tenant this caller cannot reach is not confirmed to exist either.
    if doc.case_id is not None:
        await _hxguard.require_case(session, user, "case.update", doc.case_id)

    case = await session.get(CaseInstanceModel, doc.case_id)
    is_admin = _hxguard.user_allows(user, "docs.files.delete")
    is_case_owner = case is not None and case.created_by == user.user_id
    if not (is_admin or is_case_owner):
        raise coded(
            403, DOC_DELETE_403,
            "Only the case owner or an admin can delete this document.",
        )

    # A document on a case under a legal hold is evidence (Oct item 13).
    from case_service.legal_hold import service as _legal_hold
    await _legal_hold.require_not_held(session, doc.case_id, "changed by deleting a document")

    await service.delete(session, document_id, deleted_by=user.user_id)
    return Response(status_code=204)


@router.post("/{document_id}/versions", status_code=201, response_model=DocumentVersionResponse)
async def upload_new_version(
    document_id: uuid.UUID,
    file: UploadFile = File(...),
    uploaded_by: Optional[str] = Form(None),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    from velaris_shared.db.models import DocumentModel
    _doc = await session.get(DocumentModel, document_id)
    if _doc is None or _doc.is_deleted:
        raise coded(404, DOC_NOTFOUND_404, "Document not found")
    if _doc.case_id is not None:
        await _hxguard.require_case(session, user, "case.update", _doc.case_id)
    # Same rule as the first upload: the version is signed by the caller, not by
    # whatever name the form carried.
    uploaded_by = (getattr(user, "username", None) or getattr(user, "email", None)
                   or getattr(user, "user_id", None) or "unknown")
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
    from velaris_shared.db.models import DocumentModel
    doc = await session.get(DocumentModel, document_id)
    if doc is None or doc.is_deleted:
        raise coded(404, DOC_NOTFOUND_404, "Document not found")

    # The case boundary FIRST. `docs.files.delete` is a PLATFORM privilege and
    # the check below asks only "admin, or the case's creator?" — so an admin in
    # one tenant could delete a document on another tenant's case, and did:
    # proven on 2026-09-06 on the sibling route below. `require_case` carries
    # the tenant clause since the same day and answers 404, so a document in a
    # tenant this caller cannot reach is not confirmed to exist either.
    if doc.case_id is not None:
        await _hxguard.require_case(session, user, "case.update", doc.case_id)

    case = await session.get(CaseInstanceModel, doc.case_id)
    is_admin = _hxguard.user_allows(user, "docs.files.delete")
    is_case_owner = case is not None and case.created_by == user.user_id
    if not (is_admin or is_case_owner):
        raise HTTPException(
            403,
            "Only the case owner or an admin can delete versions.",
        )

    # A version of a document on a held case is evidence too (Oct item 13).
    from case_service.legal_hold import service as _legal_hold
    await _legal_hold.require_not_held(
        session, doc.case_id, "changed by deleting a document version")

    try:
        updated = await service.delete_version(session, document_id, version)
    except ValueError as e:
        msg = str(e)
        if "only remaining" in msg:
            raise coded(409, DOC_LASTVERSION_409, msg)
        raise coded(404, DOC_NOTFOUND_404, msg)
    return _doc_to_response(updated)


@router.get("/{document_id}/versions", response_model=list[DocumentVersionResponse])
async def list_versions(
    document_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    await _authorize_document_read(session, request, document_id)
    versions = await service.list_versions(session, document_id)
    return [_ver_to_response(v) for v in versions]


@router.get("/by-case/{case_id}", response_model=list[DocumentResponse])
async def list_case_documents(
    case_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    # No grant path here: a grant is bound to ONE document, and this lists a
    # whole case. The portal has its own case-scoped listing endpoint.
    await _hxguard.require_case(session, user, "case.read", case_id)
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
    """Staff toggle — share or un-share a document with the customer portal.

    This route had **no authorization at all** beyond a valid token: it loaded a
    document by id and published it to a customer portal. Proven on 2026-09-06
    — a caller in tenant `dev` made a document on tenant `default`'s case
    portal-visible, which is the one flag on the row that decides whether
    somebody outside the platform can fetch it. It is a case change and is now
    gated as one.
    """
    from velaris_shared.db.models import DocumentModel
    doc = await session.get(DocumentModel, document_id)
    if doc is None or doc.is_deleted:
        raise coded(404, DOC_NOTFOUND_404, "Document not found")
    if doc.case_id is not None:
        await _hxguard.require_case(session, user, "case.update", doc.case_id)
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

from velaris_shared.db.models import DocumentModel as _DocModel, DocumentVerificationModel as _DocVerif
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
        raise coded(404, DOC_NOTFOUND_404, "Document not found")
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
        raise coded(404, DOC_NOTFOUND_404, "Document not found")
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
