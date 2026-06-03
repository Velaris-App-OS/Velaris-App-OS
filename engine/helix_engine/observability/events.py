from __future__ import annotations
import uuid
from typing import Any
from .context import get_context


async def emit_event(session, event_type: str, payload: dict[str, Any] | None = None, severity: str = "INFO"):
    from case_service.db.models import TelemetryEventModel
    ctx = get_context()
    row = TelemetryEventModel(
        id=uuid.uuid4(),
        event_type=event_type,
        severity=severity,
        payload=payload or {},
        request_id=ctx.get("request_id"),
        trace_id=ctx.get("trace_id"),
        tenant_id=ctx.get("tenant_id"),
        user_id=ctx.get("user_id"),
    )
    session.add(row)
    await session.flush()
    return row.id
