"""HxSync — sync pipeline.

run_sync(destination_id, session) does the following:
  1. Load destination + its field mappings + redaction rules
  2. Extract CaseInstance rows since last watermark (CDC-style incremental)
  3. Apply field mappings (rename + transform)
  4. Apply redaction rules (hash/drop/mask PII)
  5. Ensure schema on destination
  6. Push rows
  7. Update SyncDestination.last_synced_at + SyncRun record
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import (
    CaseInstanceModel,
    SyncDestinationModel,
    SyncFieldMappingModel,
    SyncRedactionRuleModel,
    SyncRunModel,
)
from case_service.hxsync import destinations as _dest_mod  # noqa: F401 — registers adapters
from case_service.hxsync.protocol import get_destination
from case_service.hxsync.redaction import apply_redaction, apply_transforms

_TABLE = "helix_cases"

_DEFAULT_COLUMNS = [
    {"name": "id",              "type": "VARCHAR"},
    {"name": "case_number",     "type": "VARCHAR"},
    {"name": "case_type_id",    "type": "VARCHAR"},
    {"name": "status",          "type": "VARCHAR"},
    {"name": "priority",        "type": "VARCHAR"},
    {"name": "created_by",      "type": "VARCHAR"},
    {"name": "created_at",      "type": "TIMESTAMPTZ"},
    {"name": "updated_at",      "type": "TIMESTAMPTZ"},
    {"name": "resolved_at",     "type": "TIMESTAMPTZ"},
    {"name": "current_stage_id","type": "VARCHAR"},
]


def _case_to_row(case: CaseInstanceModel) -> dict:
    return {
        "id":               str(case.id),
        "case_number":      case.case_number,
        "case_type_id":     str(case.case_type_id) if case.case_type_id else None,
        "status":           case.status,
        "priority":         case.priority,
        "created_by":       case.created_by,
        "created_at":       case.created_at.isoformat() if case.created_at else None,
        "updated_at":       case.updated_at.isoformat() if case.updated_at else None,
        "resolved_at":      case.resolved_at.isoformat() if case.resolved_at else None,
        "current_stage_id": str(case.current_stage_id) if case.current_stage_id else None,
    }


async def run_sync(destination_id: uuid.UUID, session: AsyncSession) -> dict:
    """Execute a full incremental sync. Returns a summary dict."""
    dest = await session.get(SyncDestinationModel, destination_id)
    if dest is None:
        raise ValueError(f"Destination {destination_id} not found")

    run = SyncRunModel(destination_id=destination_id, watermark_from=dest.last_synced_at)
    session.add(run)
    await session.flush()

    try:
        # Load mappings + redaction rules
        mappings_rows = (await session.execute(
            select(SyncFieldMappingModel).where(SyncFieldMappingModel.destination_id == destination_id)
        )).scalars().all()
        redaction_rows = (await session.execute(
            select(SyncRedactionRuleModel).where(SyncRedactionRuleModel.destination_id == destination_id)
        )).scalars().all()

        mappings = [{"source_field": m.source_field, "dest_column": m.dest_column, "transform": m.transform} for m in mappings_rows]
        redactions = [{"field_path": r.field_path, "action": r.action} for r in redaction_rows]

        # Extract cases (incremental by updated_at watermark)
        q = select(CaseInstanceModel).order_by(CaseInstanceModel.updated_at)
        if dest.last_synced_at:
            q = q.where(CaseInstanceModel.updated_at > dest.last_synced_at)
        cases = (await session.execute(q)).scalars().all()

        now = datetime.now(timezone.utc)
        rows = []
        for case in cases:
            row = _case_to_row(case)
            if redactions:
                row = apply_redaction(row, redactions)
            if mappings:
                row = apply_transforms(row, mappings)
            rows.append(row)

        # Determine columns for schema ensure
        if mappings:
            columns = [{"name": m["dest_column"], "type": "VARCHAR"} for m in mappings]
        else:
            columns = _DEFAULT_COLUMNS

        # Push to destination
        adapter = get_destination(dest.dest_type, dest.connection_config)
        adapter.ensure_schema(_TABLE, columns)
        pushed = adapter.push_rows(_TABLE, rows)

        # Update destination watermark
        dest.last_synced_at = now
        dest.last_sync_status = "success"
        dest.updated_at = now

        run.status = "success"
        run.rows_synced = pushed
        run.watermark_to = now
        run.finished_at = now

        await session.commit()
        return {"status": "success", "rows_synced": pushed, "run_id": str(run.id)}

    except Exception as exc:
        run.status = "error"
        run.error_msg = str(exc)
        run.finished_at = datetime.now(timezone.utc)
        dest.last_sync_status = "error"
        await session.commit()
        return {"status": "error", "error": str(exc), "run_id": str(run.id)}
