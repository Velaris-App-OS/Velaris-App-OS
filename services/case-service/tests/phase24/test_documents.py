"""HELIX P24 — Document Management tests."""
from __future__ import annotations

import asyncio
import io
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import AsyncClient

from case_service.db.models import (
    CaseTypeModel, CaseInstanceModel,
    DocumentModel, DocumentVersionModel,
)
from case_service.documents import DocumentService, generate_preview
from case_service.documents.ocr import StubOCREngine, get_ocr_engine
from case_service.storage import LocalFSBackend
from case_service.storage.backend import ObjectNotFound, StorageError


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def tmp_backend(tmp_path):
    return LocalFSBackend(base_path=tmp_path / "docs")


@pytest.fixture
def doc_service(tmp_backend):
    return DocumentService(backend=tmp_backend)


@pytest_asyncio.fixture
async def seeded_case(session):
    ct = CaseTypeModel(
        name="TestType", version="1.0.0",
        lifecycle_process_id="lp-1",
        definition_json={"stages": []},
    )
    session.add(ct)
    await session.flush()

    case = CaseInstanceModel(
        case_type_id=ct.id, case_type_version="1.0.0",
        status="new", priority="medium", data={},
    )
    session.add(case)
    await session.flush()
    return case


# ── Storage backend tests ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_01_local_fs_put_and_get(tmp_backend):
    await tmp_backend.put("foo/bar.txt", b"hello", "text/plain")
    data = await tmp_backend.get("foo/bar.txt")
    assert data == b"hello"


@pytest.mark.asyncio
async def test_02_local_fs_exists_and_size(tmp_backend):
    await tmp_backend.put("x.bin", b"abc", "application/octet-stream")
    assert await tmp_backend.exists("x.bin")
    assert await tmp_backend.size("x.bin") == 3


@pytest.mark.asyncio
async def test_03_local_fs_missing_raises_object_not_found(tmp_backend):
    with pytest.raises(ObjectNotFound):
        await tmp_backend.get("nope.bin")


@pytest.mark.asyncio
async def test_04_local_fs_delete(tmp_backend):
    await tmp_backend.put("rm.bin", b"x", "application/octet-stream")
    assert await tmp_backend.exists("rm.bin")
    await tmp_backend.delete("rm.bin")
    assert not await tmp_backend.exists("rm.bin")


def test_05_local_fs_rejects_path_traversal(tmp_backend):
    with pytest.raises(StorageError):
        tmp_backend._path("../escape")


# ── Service layer tests ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_06_upload_creates_document_and_version(doc_service, session, seeded_case):
    doc = await doc_service.upload(
        session, case_id=seeded_case.id,
        filename="report.pdf", data=b"%PDF-fake",
        uploaded_by="alice",
    )
    assert doc.id is not None
    assert doc.filename == "report.pdf"
    assert doc.current_version == 1

    versions = await doc_service.list_versions(session, doc.id)
    assert len(versions) == 1
    assert versions[0].version == 1
    assert versions[0].size_bytes == len(b"%PDF-fake")
    assert len(versions[0].sha256) == 64


@pytest.mark.asyncio
async def test_07_upload_missing_case_raises(doc_service, session):
    with pytest.raises(ValueError):
        await doc_service.upload(
            session, case_id=uuid.uuid4(),
            filename="x.txt", data=b"x",
        )


@pytest.mark.asyncio
async def test_08_download_returns_original_bytes(doc_service, session, seeded_case):
    doc = await doc_service.upload(
        session, case_id=seeded_case.id,
        filename="hello.txt", data=b"hello world",
        content_type="text/plain",
    )
    data, fname, ct = await doc_service.download(session, doc.id)
    assert data == b"hello world"
    assert fname == "hello.txt"
    assert ct == "text/plain"


@pytest.mark.asyncio
async def test_09_upload_version_increments(doc_service, session, seeded_case):
    doc = await doc_service.upload(
        session, case_id=seeded_case.id,
        filename="f.txt", data=b"v1",
    )
    v2 = await doc_service.upload_version(
        session, document_id=doc.id, data=b"v2-content",
    )
    assert v2.version == 2
    await session.refresh(doc)
    assert doc.current_version == 2
    versions = await doc_service.list_versions(session, doc.id)
    assert len(versions) == 2
    # Download returns newest by default
    data, _, _ = await doc_service.download(session, doc.id)
    assert data == b"v2-content"


@pytest.mark.asyncio
async def test_10_download_specific_version(doc_service, session, seeded_case):
    doc = await doc_service.upload(
        session, case_id=seeded_case.id, filename="f.txt", data=b"v1",
    )
    await doc_service.upload_version(session, document_id=doc.id, data=b"v2")
    data_v1, _, _ = await doc_service.download(session, doc.id, version=1)
    assert data_v1 == b"v1"


@pytest.mark.asyncio
async def test_11_overwrite_replaces_current_version(doc_service, session, seeded_case):
    doc = await doc_service.upload(
        session, case_id=seeded_case.id, filename="f.txt", data=b"orig",
    )
    await doc_service.overwrite(session, document_id=doc.id, data=b"overwritten")
    await session.refresh(doc)
    assert doc.current_version == 1  # unchanged
    data, _, _ = await doc_service.download(session, doc.id)
    assert data == b"overwritten"


@pytest.mark.asyncio
async def test_12_soft_delete_hides_document(doc_service, session, seeded_case):
    doc = await doc_service.upload(
        session, case_id=seeded_case.id, filename="x.txt", data=b"x",
    )
    await doc_service.delete(session, doc.id, deleted_by="bob")
    await session.refresh(doc)
    assert doc.is_deleted is True
    assert doc.deleted_by == "bob"
    docs = await doc_service.list_by_case(session, seeded_case.id)
    assert all(d.id != doc.id for d in docs)


@pytest.mark.asyncio
async def test_13_list_by_case_filters_deleted(doc_service, session, seeded_case):
    d1 = await doc_service.upload(session, case_id=seeded_case.id, filename="a.txt", data=b"a")
    d2 = await doc_service.upload(session, case_id=seeded_case.id, filename="b.txt", data=b"b")
    await doc_service.delete(session, d1.id)
    docs = await doc_service.list_by_case(session, seeded_case.id)
    ids = {d.id for d in docs}
    assert d2.id in ids
    assert d1.id not in ids


# ── Preview tests ────────────────────────────────────────────────────


def test_14_preview_returns_none_for_unsupported_type():
    assert generate_preview(b"irrelevant", "application/x-custom") is None


def test_15_preview_returns_none_for_empty_pdf():
    # malformed "pdf" — should fail gracefully
    assert generate_preview(b"not a pdf", "application/pdf") is None


# ── OCR tests ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_16_stub_ocr_returns_empty_string():
    engine = StubOCREngine()
    out = await engine.extract_text(b"irrelevant", "application/pdf")
    assert out == ""


def test_17_get_ocr_engine_returns_stub_by_default():
    engine = get_ocr_engine()
    assert isinstance(engine, StubOCREngine)


# ── API tests (integration via FastAPI) ──────────────────────────────


@pytest.mark.asyncio
async def test_18_api_upload_and_get(client: AsyncClient, session, seeded_case, monkeypatch, tmp_path):
    # Force service to use LocalFS for test isolation
    from case_service.api.routers import documents as docs_router_mod
    docs_router_mod.service = DocumentService(backend=LocalFSBackend(tmp_path / "api_docs"))

    files = {"file": ("spec.txt", b"spec contents", "text/plain")}
    data = {"case_id": str(seeded_case.id), "uploaded_by": "api-tester"}
    r = await client.post("/api/v1/documents/upload", files=files, data=data)
    assert r.status_code == 201, r.text
    doc_id = r.json()["id"]

    r2 = await client.get(f"/api/v1/documents/{doc_id}")
    assert r2.status_code == 200
    assert r2.json()["filename"] == "spec.txt"


@pytest.mark.asyncio
async def test_19_api_download(client: AsyncClient, session, seeded_case, tmp_path):
    from case_service.api.routers import documents as docs_router_mod
    docs_router_mod.service = DocumentService(backend=LocalFSBackend(tmp_path / "dl_docs"))

    files = {"file": ("data.txt", b"raw-bytes", "text/plain")}  # .bin blocked by upload allowlist
    data = {"case_id": str(seeded_case.id)}
    r = await client.post("/api/v1/documents/upload", files=files, data=data)
    doc_id = r.json()["id"]

    r2 = await client.get(f"/api/v1/documents/{doc_id}/download")
    assert r2.status_code == 200
    assert r2.content == b"raw-bytes"


@pytest.mark.asyncio
async def test_20_api_list_by_case(client: AsyncClient, session, seeded_case, tmp_path):
    from case_service.api.routers import documents as docs_router_mod
    docs_router_mod.service = DocumentService(backend=LocalFSBackend(tmp_path / "list_docs"))

    for fn in ("a.txt", "b.txt", "c.txt"):
        files = {"file": (fn, b"x", "text/plain")}
        data = {"case_id": str(seeded_case.id)}
        await client.post("/api/v1/documents/upload", files=files, data=data)

    r = await client.get(f"/api/v1/documents/by-case/{seeded_case.id}")
    assert r.status_code == 200
    assert len(r.json()) == 3


@pytest.mark.asyncio
async def test_21_api_versioning(client: AsyncClient, session, seeded_case, tmp_path):
    from case_service.api.routers import documents as docs_router_mod
    docs_router_mod.service = DocumentService(backend=LocalFSBackend(tmp_path / "ver_docs"))

    files = {"file": ("doc.txt", b"v1", "text/plain")}
    data = {"case_id": str(seeded_case.id)}
    r = await client.post("/api/v1/documents/upload", files=files, data=data)
    doc_id = r.json()["id"]

    files2 = {"file": ("doc.txt", b"v2", "text/plain")}
    r2 = await client.post(f"/api/v1/documents/{doc_id}/versions", files=files2)
    assert r2.status_code == 201
    assert r2.json()["version"] == 2

    r3 = await client.get(f"/api/v1/documents/{doc_id}/versions")
    assert r3.status_code == 200
    assert len(r3.json()) == 2


@pytest.mark.asyncio
async def test_22_api_delete(client: AsyncClient, session, seeded_case, tmp_path):
    from case_service.api.routers import documents as docs_router_mod
    docs_router_mod.service = DocumentService(backend=LocalFSBackend(tmp_path / "del_docs"))

    files = {"file": ("tmp.txt", b"x", "text/plain")}
    data = {"case_id": str(seeded_case.id)}
    r = await client.post("/api/v1/documents/upload", files=files, data=data)
    doc_id = r.json()["id"]

    r2 = await client.delete(f"/api/v1/documents/{doc_id}")
    assert r2.status_code == 204

    r3 = await client.get(f"/api/v1/documents/{doc_id}")
    assert r3.status_code == 404


# ── Version-delete tests (added in v2) ──────────────────────────────

@pytest.mark.asyncio
async def test_23_delete_version_updates_current(doc_service, session, seeded_case):
    doc = await doc_service.upload(session, case_id=seeded_case.id, filename="f.txt", data=b"v1")
    await doc_service.upload_version(session, document_id=doc.id, data=b"v2")
    await doc_service.upload_version(session, document_id=doc.id, data=b"v3")
    await session.refresh(doc)
    assert doc.current_version == 3

    updated = await doc_service.delete_version(session, doc.id, version=3)
    assert updated.current_version == 2
    versions = await doc_service.list_versions(session, doc.id)
    assert {v.version for v in versions} == {1, 2}


@pytest.mark.asyncio
async def test_24_delete_last_version_blocked(doc_service, session, seeded_case):
    doc = await doc_service.upload(session, case_id=seeded_case.id, filename="f.txt", data=b"only")
    with pytest.raises(ValueError, match="only remaining"):
        await doc_service.delete_version(session, doc.id, version=1)


@pytest.mark.asyncio
async def test_25_delete_middle_version_keeps_current(doc_service, session, seeded_case):
    doc = await doc_service.upload(session, case_id=seeded_case.id, filename="f.txt", data=b"v1")
    await doc_service.upload_version(session, document_id=doc.id, data=b"v2")
    await doc_service.upload_version(session, document_id=doc.id, data=b"v3")
    updated = await doc_service.delete_version(session, doc.id, version=2)
    assert updated.current_version == 3  # unchanged because we deleted v2, not current
    versions = await doc_service.list_versions(session, doc.id)
    assert {v.version for v in versions} == {1, 3}
