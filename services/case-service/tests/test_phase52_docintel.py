"""Tests for P52 HxConnect — Document Intelligence & Storage."""
from __future__ import annotations

import uuid
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import ConnectorRegistryModel, DocExtractionJobModel, DocStorageRouteModel
from case_service.hxbridge.encryption import encrypt_credentials

from tests.conftest import client, session, deploy_case_type, create_case  # type: ignore[attr-defined]


# ── helpers ───────────────────────────────────────────────────────────────────

async def _reg_docling(session: AsyncSession, tenant_id: str = "t1") -> ConnectorRegistryModel:
    row = ConnectorRegistryModel(
        name="Docling Test", connector_type="docling",
        config={"base_url": "http://localhost:5001"},
        credentials=encrypt_credentials({}),
        tenant_id=tenant_id, enabled=True,
    )
    session.add(row); await session.flush(); return row


async def _reg_s3(session: AsyncSession, tenant_id: str = "t1") -> ConnectorRegistryModel:
    row = ConnectorRegistryModel(
        name="S3 Test", connector_type="s3",
        config={"bucket": "helix-docs", "region": "eu-west-1"},
        credentials=encrypt_credentials({"access_key_id": "AKIATEST", "secret_access_key": "secret"}),
        tenant_id=tenant_id, enabled=True,
    )
    session.add(row); await session.flush(); return row


async def _extract_case(client: AsyncClient) -> dict:
    ct = await deploy_case_type(client, name="Extraction Case", definition_json={
        "stages": [{"id": "s1", "name": "Intake", "order": 1, "steps": [
            {"id": "ext_step", "name": "Extract Document", "step_type": "doc_extract", "required": True},
        ]}]
    })
    return await create_case(client, ct["id"])


async def _storage_case(client: AsyncClient) -> dict:
    ct = await deploy_case_type(client, name="Storage Case", definition_json={
        "stages": [{"id": "s1", "name": "Store", "order": 1, "steps": [
            {"id": "store_step", "name": "Upload Doc", "step_type": "doc_store", "required": True},
        ]}]
    })
    return await create_case(client, ct["id"])


# ── extraction list ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_extractions_empty(client: AsyncClient):
    case = await _extract_case(client)
    r = await client.get(f"/api/v1/docintel/cases/{case['id']}/extractions")
    assert r.status_code == 200 and r.json() == []


@pytest.mark.asyncio
async def test_extract_no_connector_returns_400(client: AsyncClient):
    case = await _extract_case(client)
    r = await client.post(f"/api/v1/docintel/cases/{case['id']}/extract",
                          json={"step_id": "ext_step", "source_url": "https://example.com/doc.pdf"})
    assert r.status_code in (400, 502)


@pytest.mark.asyncio
async def test_extract_unknown_case_returns_400(client: AsyncClient, session: AsyncSession):
    await _reg_docling(session); await session.commit()
    r = await client.post(f"/api/v1/docintel/cases/{uuid.uuid4()}/extract",
                          json={"step_id": "ext_step", "source_url": "https://example.com/doc.pdf"})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_extraction_record_created_and_retrievable(client: AsyncClient, session: AsyncSession):
    reg  = await _reg_docling(session)
    case = await _extract_case(client); await session.commit()

    row = DocExtractionJobModel(
        tenant_id="t1", case_id=uuid.UUID(case["id"]), step_id="ext_step",
        connector_id=reg.id, provider="docling",
        document_name="passport.pdf",
        extracted_fields={"name": "Alice Smith", "dob": "1990-01-15", "doc_number": "GB123456"},
        confidence=0.97, status="completed",
    )
    session.add(row); await session.commit()

    r = await client.get(f"/api/v1/docintel/cases/{case['id']}/extractions")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["status"] == "completed"
    assert rows[0]["extracted_fields"]["name"] == "Alice Smith"
    assert rows[0]["confidence"] == pytest.approx(0.97)


@pytest.mark.asyncio
async def test_extraction_failed_shows_error(client: AsyncClient, session: AsyncSession):
    reg  = await _reg_docling(session)
    case = await _extract_case(client); await session.commit()

    row = DocExtractionJobModel(
        tenant_id="t1", case_id=uuid.UUID(case["id"]), step_id="ext_step",
        connector_id=reg.id, provider="docling",
        extracted_fields={}, status="failed", error="Docling server unreachable",
    )
    session.add(row); await session.commit()

    r = await client.get(f"/api/v1/docintel/cases/{case['id']}/extractions")
    assert r.json()[0]["error"] == "Docling server unreachable"


@pytest.mark.asyncio
async def test_extraction_fields_count(client: AsyncClient, session: AsyncSession):
    reg  = await _reg_docling(session)
    case = await _extract_case(client); await session.commit()

    fields = {f"field_{i}": f"value_{i}" for i in range(10)}
    row = DocExtractionJobModel(
        tenant_id="t1", case_id=uuid.UUID(case["id"]), step_id="ext_step",
        connector_id=reg.id, provider="docling",
        extracted_fields=fields, status="completed",
    )
    session.add(row); await session.commit()

    r = await client.get(f"/api/v1/docintel/cases/{case['id']}/extractions")
    assert len(r.json()[0]["extracted_fields"]) == 10


@pytest.mark.asyncio
async def test_multiple_extractions_same_case(client: AsyncClient, session: AsyncSession):
    reg  = await _reg_docling(session)
    case = await _extract_case(client); await session.commit()

    for i in range(3):
        session.add(DocExtractionJobModel(
            tenant_id="t1", case_id=uuid.UUID(case["id"]), step_id=f"ext_step_{i}",
            connector_id=reg.id, provider="docling",
            extracted_fields={}, status="completed",
        ))
    await session.commit()

    r = await client.get(f"/api/v1/docintel/cases/{case['id']}/extractions")
    assert r.status_code == 200 and len(r.json()) == 3


@pytest.mark.asyncio
async def test_doc_connector_type_isolation(client: AsyncClient, session: AsyncSession):
    """S3 connector not used for extraction."""
    await _reg_s3(session)
    case = await _extract_case(client); await session.commit()
    r = await client.post(f"/api/v1/docintel/cases/{case['id']}/extract",
                          json={"step_id": "ext_step", "source_url": "https://example.com/doc.pdf"})
    assert r.status_code in (400, 502)


# ── storage routes ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_storage_empty(client: AsyncClient):
    case = await _storage_case(client)
    r = await client.get(f"/api/v1/docintel/cases/{case['id']}/storage")
    assert r.status_code == 200 and r.json() == []


@pytest.mark.asyncio
async def test_storage_no_connector_returns_400(client: AsyncClient):
    case = await _storage_case(client)
    r = await client.post(f"/api/v1/docintel/cases/{case['id']}/store",
                          json={"step_id": "store_step", "document_name": "passport.pdf"})
    assert r.status_code in (400, 502)


@pytest.mark.asyncio
async def test_storage_unknown_case_returns_400(client: AsyncClient, session: AsyncSession):
    await _reg_s3(session); await session.commit()
    r = await client.post(f"/api/v1/docintel/cases/{uuid.uuid4()}/store",
                          json={"step_id": "store_step", "document_name": "doc.pdf"})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_storage_route_created_and_retrievable(client: AsyncClient, session: AsyncSession):
    reg  = await _reg_s3(session)
    case = await _storage_case(client); await session.commit()

    row = DocStorageRouteModel(
        tenant_id="t1", case_id=uuid.UUID(case["id"]), step_id="store_step",
        connector_id=reg.id, provider="s3",
        document_name="passport.pdf", bucket="helix-docs",
        object_key="helix/passport.pdf",
        storage_url="https://helix-docs.s3.eu-west-1.amazonaws.com/helix/passport.pdf",
        presigned_url="https://helix-docs.s3.eu-west-1.amazonaws.com/helix/passport.pdf?X-Amz-Signature=abc",
        status="pending",
    )
    session.add(row); await session.commit()

    r = await client.get(f"/api/v1/docintel/cases/{case['id']}/storage")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"
    assert rows[0]["bucket"] == "helix-docs"
    assert rows[0]["object_key"] == "helix/passport.pdf"
    assert "X-Amz-Signature" in rows[0]["presigned_url"]


@pytest.mark.asyncio
async def test_storage_uploaded_status(client: AsyncClient, session: AsyncSession):
    reg  = await _reg_s3(session)
    case = await _storage_case(client); await session.commit()

    row = DocStorageRouteModel(
        tenant_id="t1", case_id=uuid.UUID(case["id"]), step_id="store_step",
        connector_id=reg.id, provider="s3",
        document_name="invoice.pdf", bucket="helix-docs",
        object_key="helix/invoice.pdf", status="uploaded",
        size_bytes=204800,
    )
    session.add(row); await session.commit()

    r = await client.get(f"/api/v1/docintel/cases/{case['id']}/storage")
    assert r.json()[0]["status"] == "uploaded"


@pytest.mark.asyncio
async def test_storage_failed_shows_error(client: AsyncClient, session: AsyncSession):
    reg  = await _reg_s3(session)
    case = await _storage_case(client); await session.commit()

    row = DocStorageRouteModel(
        tenant_id="t1", case_id=uuid.UUID(case["id"]), step_id="store_step",
        connector_id=reg.id, provider="s3",
        document_name="fail.pdf", status="failed", error="Access denied",
    )
    session.add(row); await session.commit()

    r = await client.get(f"/api/v1/docintel/cases/{case['id']}/storage")
    assert r.json()[0]["error"] == "Access denied"


@pytest.mark.asyncio
async def test_multiple_storage_routes_same_case(client: AsyncClient, session: AsyncSession):
    reg  = await _reg_s3(session)
    case = await _storage_case(client); await session.commit()

    for i in range(3):
        session.add(DocStorageRouteModel(
            tenant_id="t1", case_id=uuid.UUID(case["id"]), step_id=f"store_step_{i}",
            connector_id=reg.id, provider="s3",
            document_name=f"doc_{i}.pdf", status="pending",
        ))
    await session.commit()

    r = await client.get(f"/api/v1/docintel/cases/{case['id']}/storage")
    assert r.status_code == 200 and len(r.json()) == 3


@pytest.mark.asyncio
async def test_storage_connector_type_isolation(client: AsyncClient, session: AsyncSession):
    """Docling connector not used for storage."""
    await _reg_docling(session)
    case = await _storage_case(client); await session.commit()
    r = await client.post(f"/api/v1/docintel/cases/{case['id']}/store",
                          json={"step_id": "store_step", "document_name": "doc.pdf"})
    assert r.status_code in (400, 502)


# ── connectors endpoint ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_docintel_connectors_empty(client: AsyncClient):
    r = await client.get("/api/v1/docintel/connectors")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_list_docintel_connectors_both_types(client: AsyncClient, session: AsyncSession):
    await _reg_docling(session, tenant_id="default")
    await _reg_s3(session, tenant_id="default")
    await session.commit()

    r = await client.get("/api/v1/docintel/connectors")
    assert r.status_code == 200
    types = {c["type"] for c in r.json()}
    assert "docling" in types
    assert "s3" in types
