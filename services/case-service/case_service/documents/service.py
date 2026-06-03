"""Document service — business logic for upload, versioning, retrieval."""
from __future__ import annotations
import hashlib
import mimetypes
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import (
    CaseInstanceModel, DocumentModel, DocumentVersionModel,
)
from case_service.storage import get_storage_backend
from case_service.storage.backend import StorageBackend, ObjectNotFound


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _guess_content_type(filename: str, fallback: str = "application/octet-stream") -> str:
    ctype, _ = mimetypes.guess_type(filename)
    return ctype or fallback


class DocumentService:
    """Handles document CRUD + versioning. Storage backend is pluggable."""

    def __init__(self, backend: Optional[StorageBackend] = None):
        self._backend = backend  # if None, resolved lazily per-call

    def _be(self) -> StorageBackend:
        return self._backend or get_storage_backend()

    # -- upload / create new document ----------------------------------------
    async def upload(
        self,
        session: AsyncSession,
        case_id: uuid.UUID,
        filename: str,
        data: bytes,
        content_type: Optional[str] = None,
        uploaded_by: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> DocumentModel:
        # Case must exist
        case = await session.get(CaseInstanceModel, case_id)
        if case is None:
            raise ValueError(f"case {case_id} not found")

        ct = content_type or _guess_content_type(filename)
        doc_id = uuid.uuid4()
        version_id = uuid.uuid4()
        key = f"cases/{case_id}/{doc_id}/v1/{filename}"

        be = self._be()
        await be.put(key, data, content_type=ct)

        doc = DocumentModel(
            id=doc_id,
            case_id=case_id,
            filename=filename,
            content_type=ct,
            current_version=1,
            uploaded_by=uploaded_by,
            tenant_id=tenant_id,
        )
        ver = DocumentVersionModel(
            id=version_id,
            document_id=doc_id,
            version=1,
            storage_key=key,
            size_bytes=len(data),
            sha256=_sha256(data),
            uploaded_by=uploaded_by,
        )
        session.add(doc)
        session.add(ver)
        await session.flush()
        return doc

    # -- upload a new version of an existing document ------------------------
    async def upload_version(
        self,
        session: AsyncSession,
        document_id: uuid.UUID,
        data: bytes,
        filename: Optional[str] = None,
        uploaded_by: Optional[str] = None,
    ) -> DocumentVersionModel:
        doc = await session.get(DocumentModel, document_id)
        if doc is None or doc.is_deleted:
            raise ValueError(f"document {document_id} not found")

        new_version = doc.current_version + 1
        effective_filename = filename or doc.filename
        key = f"cases/{doc.case_id}/{doc.id}/v{new_version}/{effective_filename}"

        be = self._be()
        await be.put(key, data, content_type=doc.content_type)

        ver = DocumentVersionModel(
            id=uuid.uuid4(),
            document_id=doc.id,
            version=new_version,
            storage_key=key,
            size_bytes=len(data),
            sha256=_sha256(data),
            uploaded_by=uploaded_by,
        )
        session.add(ver)
        doc.current_version = new_version
        doc.updated_at = _utcnow()
        if filename:
            doc.filename = filename
        await session.flush()
        return ver

    # -- overwrite current version (simple mode) -----------------------------
    async def overwrite(
        self,
        session: AsyncSession,
        document_id: uuid.UUID,
        data: bytes,
        filename: Optional[str] = None,
        uploaded_by: Optional[str] = None,
    ) -> DocumentVersionModel:
        doc = await session.get(DocumentModel, document_id)
        if doc is None or doc.is_deleted:
            raise ValueError(f"document {document_id} not found")

        cur_ver = await self._get_version(session, document_id, doc.current_version)
        if cur_ver is None:
            # Fall back to creating version 1
            return await self.upload_version(session, document_id, data, filename, uploaded_by)

        be = self._be()
        # Overwrite at same key
        await be.put(cur_ver.storage_key, data, content_type=doc.content_type)
        cur_ver.size_bytes = len(data)
        cur_ver.sha256 = _sha256(data)
        cur_ver.uploaded_by = uploaded_by or cur_ver.uploaded_by
        doc.updated_at = _utcnow()
        if filename:
            doc.filename = filename
        await session.flush()
        return cur_ver

    # -- download ------------------------------------------------------------
    async def download(
        self,
        session: AsyncSession,
        document_id: uuid.UUID,
        version: Optional[int] = None,
    ) -> tuple[bytes, str, str]:
        """Return (data, filename, content_type)."""
        doc = await session.get(DocumentModel, document_id)
        if doc is None or doc.is_deleted:
            raise ValueError(f"document {document_id} not found")
        v = version or doc.current_version
        ver = await self._get_version(session, document_id, v)
        if ver is None:
            raise ValueError(f"version {v} not found")
        be = self._be()
        data = await be.get(ver.storage_key)
        return data, doc.filename, doc.content_type

    # -- delete (soft) -------------------------------------------------------
    async def delete(
        self,
        session: AsyncSession,
        document_id: uuid.UUID,
        deleted_by: Optional[str] = None,
    ) -> None:
        doc = await session.get(DocumentModel, document_id)
        if doc is None or doc.is_deleted:
            return
        doc.is_deleted = True
        doc.deleted_at = _utcnow()
        doc.deleted_by = deleted_by
        await session.flush()

    # -- list ---------------------------------------------------------------
    async def list_by_case(
        self, session: AsyncSession, case_id: uuid.UUID,
    ) -> list[DocumentModel]:
        q = (
            select(DocumentModel)
            .where(DocumentModel.case_id == case_id)
            .where(DocumentModel.is_deleted.is_(False))
            .order_by(DocumentModel.created_at.desc())
        )
        res = await session.execute(q)
        return list(res.scalars().all())

    async def list_versions(
        self, session: AsyncSession, document_id: uuid.UUID,
    ) -> list[DocumentVersionModel]:
        q = (
            select(DocumentVersionModel)
            .where(DocumentVersionModel.document_id == document_id)
            .order_by(DocumentVersionModel.version.desc())
        )
        res = await session.execute(q)
        return list(res.scalars().all())

    # -- internals ----------------------------------------------------------
    async def _get_version(
        self, session: AsyncSession, document_id: uuid.UUID, version: int,
    ) -> Optional[DocumentVersionModel]:
        q = (
            select(DocumentVersionModel)
            .where(DocumentVersionModel.document_id == document_id)
            .where(DocumentVersionModel.version == version)
        )
        res = await session.execute(q)
        return res.scalar_one_or_none()
    # -- delete a specific version ------------------------------------------
    async def delete_version(
        self,
        session: AsyncSession,
        document_id: uuid.UUID,
        version: int,
    ) -> DocumentModel:
        """Delete one version. Blocks if it's the only remaining version.

        Returns the updated document (so caller can see the new current_version).
        Raises ValueError with a clear message on not-found or last-version.
        """
        doc = await session.get(DocumentModel, document_id)
        if doc is None or doc.is_deleted:
            raise ValueError(f"document {document_id} not found")

        versions = await self.list_versions(session, document_id)
        if len(versions) <= 1:
            raise ValueError(
                "cannot delete the only remaining version; delete the document instead"
            )

        target = next((v for v in versions if v.version == version), None)
        if target is None:
            raise ValueError(f"version {version} not found")

        # Remove from storage (ignore missing object)
        be = self._be()
        try:
            await be.delete(target.storage_key)
        except Exception:
            pass

        await session.delete(target)
        await session.flush()

        # If we deleted the current version, point current_version at the newest remaining
        if doc.current_version == version:
            remaining = [v for v in versions if v.version != version]
            doc.current_version = max(v.version for v in remaining)
            doc.updated_at = _utcnow()
            await session.flush()
        return doc

