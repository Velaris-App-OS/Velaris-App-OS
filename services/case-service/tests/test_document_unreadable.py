"""A document this install cannot decrypt answers 409 VEL-DOC-UNREADABLE-409.

R3 upgrade rehearsal, 2026-11-01: with the storage master key removed, v3.1.2
served the raw ciphertext with 200 and the next release a bare 500. Neither said
why. Now: the bytes are never served, and the answer names the cause.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import HTTPException

from velaris_shared.core.error_codes import DOC_UNREADABLE_409, KNOWN_CODES
from velaris_shared.db.models import CaseInstanceModel, CaseTypeModel
from velaris_shared.storage.encrypted_backend import EncryptedBackend
from case_service.api.routers.documents import _unreadable
from case_service.documents import DocumentService
from case_service.storage import LocalFSBackend
from case_service.storage.backend import StorageError

KEY_A = "11" * 32
KEY_B = "22" * 32


@pytest_asyncio.fixture
async def case(session):
    ct = CaseTypeModel(name="UnreadableDoc", version="1.0.0", lifecycle_process_id="lp-1",
                       definition_json={"stages": []})
    session.add(ct)
    await session.commit()
    c = CaseInstanceModel(case_type_id=ct.id, case_type_version="1.0.0", status="new",
                          priority="medium", data={})
    session.add(c)
    await session.commit()
    return c


@pytest_asyncio.fixture
async def stored(session, case, tmp_path):
    """A document written under KEY_A; returns (doc, raw backend)."""
    raw = LocalFSBackend(base_path=tmp_path / "docs")
    doc = await DocumentService(backend=EncryptedBackend(raw, KEY_A)).upload(
        session, case_id=case.id, filename="secret.txt", data=b"the body", content_type="text/plain")
    return doc, raw


@pytest.mark.asyncio
async def test_same_key_reads(session, stored):
    doc, raw = stored
    data, _, _ = await DocumentService(backend=EncryptedBackend(raw, KEY_A)).download(session, doc.id)
    assert data == b"the body"


@pytest.mark.asyncio
async def test_no_key_never_serves_ciphertext(session, stored):
    doc, raw = stored
    with pytest.raises(StorageError, match="no storage master key"):
        await DocumentService(backend=raw).download(session, doc.id)


@pytest.mark.asyncio
async def test_other_key_refused(session, stored):
    doc, raw = stored
    with pytest.raises(StorageError):
        await DocumentService(backend=EncryptedBackend(raw, KEY_B)).download(session, doc.id)


@pytest.mark.asyncio
async def test_plain_file_still_reads_without_a_key(session, case, tmp_path):
    """Files written before encryption was on are not ciphertext — unchanged."""
    raw = LocalFSBackend(base_path=tmp_path / "plain")
    svc = DocumentService(backend=raw)
    doc = await svc.upload(session, case_id=case.id, filename="p.txt", data=b"plain", content_type="text/plain")
    data, _, _ = await svc.download(session, doc.id)
    assert data == b"plain"


def test_answer_is_a_coded_409():
    import uuid
    exc = _unreadable(uuid.uuid4(), StorageError("x"))
    assert isinstance(exc, HTTPException) and exc.status_code == 409
    assert DOC_UNREADABLE_409 in str(exc.detail) or DOC_UNREADABLE_409 in str(getattr(exc, "headers", "")) \
        or DOC_UNREADABLE_409 in repr(exc.__dict__)
    assert DOC_UNREADABLE_409 in KNOWN_CODES
