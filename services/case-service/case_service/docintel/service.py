"""P52 Document Intelligence & Storage service."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import (
    CaseInstanceModel,
    CaseStepCompletionModel,
    ConnectorRegistryModel,
    DocExtractionJobModel,
    DocStorageRouteModel,
)
from case_service.hxbridge.encryption import decrypt_credentials

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _emit(case_id: uuid.UUID, event_type: str, data: dict) -> None:
    try:
        from case_service.hxstream.emitter import emit_event
        await emit_event(str(case_id), event_type, data)
    except Exception as exc:
        logger.warning("HxStream emit failed (%s): %s", event_type, exc)


async def _get_connector(session: AsyncSession, connector_type: str, connector_id: uuid.UUID | None, tenant_id: str):
    q = select(ConnectorRegistryModel).where(
        ConnectorRegistryModel.connector_type == connector_type,
        ConnectorRegistryModel.enabled == True,  # noqa: E712
    )
    if connector_id:
        q = q.where(ConnectorRegistryModel.id == connector_id)
    else:
        q = q.where(ConnectorRegistryModel.tenant_id == tenant_id)
    row = (await session.execute(q.limit(1))).scalar_one_or_none()
    if row is None:
        raise ValueError(f"No enabled {connector_type} connector found")
    return row, decrypt_credentials(row.credentials)


async def _complete_step(session: AsyncSession, case_id: uuid.UUID, step_id: str, step_type: str, actor: str, data: dict) -> None:
    now = _utcnow()
    try:
        async with session.begin_nested():
            case     = (await session.execute(select(CaseInstanceModel).where(CaseInstanceModel.id == case_id))).scalar_one_or_none()
            stage_id = (case.current_stage_id if case else None) or "unknown"
            existing = (await session.execute(
                select(CaseStepCompletionModel).where(
                    CaseStepCompletionModel.case_id == case_id,
                    CaseStepCompletionModel.step_id == step_id,
                )
            )).scalar_one_or_none()
            if existing:
                existing.status = "completed"; existing.completed_at = now; existing.data = data
            else:
                session.add(CaseStepCompletionModel(
                    case_id=case_id, step_id=step_id, stage_id=stage_id,
                    step_type=step_type, status="completed", data=data,
                    completed_by=actor, completed_at=now,
                ))
    except Exception as exc:
        logger.warning("Step completion failed (non-fatal): %s", exc)


async def _auto_advance(session: AsyncSession, case_id: uuid.UUID) -> None:
    try:
        async with session.begin_nested():
            from case_service.db.models import CaseTypeModel
            case = (await session.execute(
                select(CaseInstanceModel).where(CaseInstanceModel.id == case_id)
            )).scalar_one_or_none()
            if case and case.case_type_id:
                ct = (await session.execute(
                    select(CaseTypeModel).where(CaseTypeModel.id == case.case_type_id)
                )).scalar_one_or_none()
                if ct:
                    from case_service.api.routers.cases import _auto_advance_if_complete
                    await _auto_advance_if_complete(session, case, ct.definition_json or {})
    except Exception as exc:
        logger.warning("Auto-advance failed (non-fatal): %s", exc)


# ── Document Extraction ───────────────────────────────────────────────────────

async def extract_document(
    session: AsyncSession,
    case_id: uuid.UUID,
    step_id: str,
    tenant_id: str,
    actor: str,
    source_url: str | None = None,
    document_name: str | None = None,
    connector_id: uuid.UUID | None = None,
) -> DocExtractionJobModel:
    case = (await session.execute(select(CaseInstanceModel).where(CaseInstanceModel.id == case_id))).scalar_one_or_none()
    if case is None:
        raise ValueError(f"Case {case_id} not found")

    conn_row, creds = await _get_connector(session, "docling", connector_id, tenant_id)

    row = DocExtractionJobModel(
        tenant_id=tenant_id, case_id=case_id, step_id=step_id,
        connector_id=conn_row.id, provider="docling",
        source_url=source_url, document_name=document_name,
        status="processing",
    )
    session.add(row)
    await session.flush()

    try:
        from case_service.hxbridge.connectors.docling_connector import DoclingConnector
        connector = DoclingConnector(conn_row.config or {}, creds)
        result    = await connector.execute({"source_url": source_url})
        row.extracted_fields = result.get("extracted_fields", {})
        row.raw_text         = result.get("raw_text")
        row.confidence       = result.get("confidence")
        row.status           = "completed"
        row.completed_at     = _utcnow()
    except Exception as exc:
        row.status = "failed"
        row.error  = str(exc)[:500]

    await session.flush()

    if row.status == "completed":
        await _complete_step(session, case_id, step_id, "doc_extract", actor,
                             {"fields_count": len(row.extracted_fields)})
        await _auto_advance(session, case_id)

    await _emit(case_id, "doc.extracted", {"status": row.status, "job_id": str(row.id)})
    return row


async def list_extractions(session: AsyncSession, case_id: uuid.UUID) -> list[DocExtractionJobModel]:
    rows = (await session.execute(
        select(DocExtractionJobModel).where(DocExtractionJobModel.case_id == case_id)
        .order_by(DocExtractionJobModel.created_at)
    )).scalars().all()
    return list(rows)


# ── Cloud Storage ─────────────────────────────────────────────────────────────

async def route_to_storage(
    session: AsyncSession,
    case_id: uuid.UUID,
    step_id: str,
    document_name: str,
    tenant_id: str,
    actor: str,
    connector_id: uuid.UUID | None = None,
    content_type: str = "application/octet-stream",
    size_bytes: int | None = None,
) -> DocStorageRouteModel:
    case = (await session.execute(select(CaseInstanceModel).where(CaseInstanceModel.id == case_id))).scalar_one_or_none()
    if case is None:
        raise ValueError(f"Case {case_id} not found")

    conn_row, creds = await _get_connector(session, "s3", connector_id, tenant_id)

    row = DocStorageRouteModel(
        tenant_id=tenant_id, case_id=case_id, step_id=step_id,
        connector_id=conn_row.id, provider="s3",
        document_name=document_name, content_type=content_type,
        size_bytes=size_bytes, status="pending",
    )
    session.add(row)
    await session.flush()

    try:
        from case_service.hxbridge.connectors.s3_connector import S3Connector
        connector  = S3Connector(conn_row.config or {}, creds)
        result     = await connector.execute({
            "document_name": document_name,
            "content_type":  content_type,
        })
        row.object_key    = result.get("object_key")
        row.bucket        = result.get("bucket")
        row.storage_url   = result.get("storage_url")
        row.presigned_url = result.get("presigned_url")
        row.status        = result.get("status", "pending")
        if row.status == "uploaded":
            row.uploaded_at = _utcnow()
    except Exception as exc:
        row.status = "failed"
        row.error  = str(exc)[:500]

    await session.flush()

    if row.status in ("pending", "uploaded"):
        await _complete_step(session, case_id, step_id, "doc_store", actor,
                             {"presigned_url": row.presigned_url, "object_key": row.object_key})
        await _auto_advance(session, case_id)

    await _emit(case_id, "doc.storage_routed", {"status": row.status})
    return row


async def list_storage_routes(session: AsyncSession, case_id: uuid.UUID) -> list[DocStorageRouteModel]:
    rows = (await session.execute(
        select(DocStorageRouteModel).where(DocStorageRouteModel.case_id == case_id)
        .order_by(DocStorageRouteModel.created_at)
    )).scalars().all()
    return list(rows)
