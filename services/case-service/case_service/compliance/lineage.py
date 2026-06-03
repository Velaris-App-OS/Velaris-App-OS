"""Hybrid data lineage.

Hot path (denormalized): case-data field changes go to data_lineage_events.
Derived path: assignments, documents, escalations are read from existing
case_audit_log on demand and merged into the lineage view.
"""
from __future__ import annotations
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import (
    CaseAuditLogModel, DataLineageEventModel,
)

log = logging.getLogger(__name__)


# ── Denormalized hot path ────────────────────────────────────────────

async def record_lineage_event(
    session: AsyncSession,
    *,
    case_id: uuid.UUID,
    kind: str,
    field_path: str | None = None,
    before_value: Any = None,
    after_value: Any = None,
    actor_id: str | None = None,
    source: str = "api",
    tenant_id: str | None = None,
) -> uuid.UUID:
    """Write a lineage event. Call from case-data update flow."""
    # Wrap non-dict primitives so PortableJSON can round-trip them across
    # SQLite (TEXT) and Postgres (JSONB). Bare strings/ints would fail json.loads.
    def _wrap(v):
        if v is None or isinstance(v, (dict, list)):
            return v
        return {"value": v}

    row = DataLineageEventModel(
        id=uuid.uuid4(),
        case_id=case_id,
        kind=kind,
        field_path=field_path,
        before_value=_wrap(before_value),
        after_value=_wrap(after_value),
        actor_id=actor_id,
        source=source,
        tenant_id=tenant_id,
    )
    session.add(row)
    await session.flush()
    return row.id


# ── Hybrid query ─────────────────────────────────────────────────────

async def get_case_lineage(
    session: AsyncSession, case_id: uuid.UUID, limit: int = 500,
) -> list[dict]:
    """Return merged lineage timeline for a case.

    Combines:
      - data_lineage_events (denormalized writes)
      - case_audit_log entries for the case (derived)
    Sorted descending by timestamp.
    """
    # Denormalized
    q1 = (
        select(DataLineageEventModel)
        .where(DataLineageEventModel.case_id == case_id)
        .order_by(DataLineageEventModel.at.desc())
        .limit(limit)
    )
    den_rows = (await session.execute(q1)).scalars().all()
    denorm = [{
        "source_table": "data_lineage_events",
        "kind": r.kind,
        "field_path": r.field_path,
        "before_value": r.before_value,
        "after_value": r.after_value,
        "actor_id": r.actor_id,
        "at": r.at.isoformat() if r.at else None,
    } for r in den_rows]

    # Derived from audit log
    q2 = (
        select(CaseAuditLogModel)
        .where(CaseAuditLogModel.case_id == case_id)
        .order_by(CaseAuditLogModel.timestamp.desc())
        .limit(limit)
    )
    audit_rows = (await session.execute(q2)).scalars().all()
    derived = [{
        "source_table": "case_audit_log",
        "kind": r.action,
        "field_path": None,
        "before_value": None,
        "after_value": None,
        "actor_id": r.actor_id,
        "at": r.timestamp.isoformat() if r.timestamp else None,
        "details": r.details or {},
    } for r in audit_rows]

    merged = denorm + derived
    # Sort merged desc by `at`; rows with None timestamps go to the end
    merged.sort(key=lambda x: x.get("at") or "", reverse=True)
    return merged[:limit]
