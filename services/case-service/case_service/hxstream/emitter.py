"""HxStream event emitter.

Call emit_trace(...) from anywhere in the request path to record an
observable event.  The write is fire-and-forget — it never raises and
never blocks the caller.

Subscribers connected via /hxstream/ws receive the event in real time
via the in-process broadcast queue.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ── In-process broadcast ──────────────────────────────────────────────────────
# Each active WebSocket subscriber registers a queue here.
# emit_trace() puts events onto every queue; the WS handler drains its own.
_subscribers: dict[str, asyncio.Queue] = {}


def _register_subscriber(sub_id: str) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=500)
    _subscribers[sub_id] = q
    return q


def _unregister_subscriber(sub_id: str) -> None:
    _subscribers.pop(sub_id, None)


def _broadcast(event: dict) -> None:
    """Put event onto every live subscriber queue (non-blocking, drops if full)."""
    for q in list(_subscribers.values()):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass  # slow consumer — drop rather than block


# ── Public emit API ───────────────────────────────────────────────────────────

async def emit_trace(
    event_type: str,
    payload: dict[str, Any],
    *,
    case_id: uuid.UUID | str | None = None,
    tenant_id: str = "default",
    actor_user_id: str | None = None,
    actor_ip: str | None = None,
    session_id: str | None = None,
    latency_ms: int | None = None,
    session=None,  # AsyncSession — if provided, write to DB in the same txn
) -> None:
    """Emit one HxStream event.

    Always broadcasts to live WebSocket subscribers.
    If *session* is supplied the event is also persisted to trace_events
    within the caller's existing transaction (caller must commit).
    If *session* is None the event is broadcast-only (no DB write).
    """
    if isinstance(case_id, str):
        try:
            case_id = uuid.UUID(case_id)
        except ValueError:
            case_id = None

    event: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "event_type": event_type,
        "case_id": str(case_id) if case_id else None,
        "tenant_id": tenant_id,
        "actor_user_id": actor_user_id,
        "actor_ip": actor_ip,
        "payload": payload,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "latency_ms": latency_ms,
    }

    # Persist to DB
    if session is not None:
        try:
            from case_service.db.models import TraceEventModel
            row = TraceEventModel(
                case_id=case_id,
                tenant_id=tenant_id,
                event_type=event_type,
                actor_user_id=actor_user_id,
                actor_ip=actor_ip,
                payload=payload,
                session_id=session_id,
                latency_ms=latency_ms,
            )
            session.add(row)
        except Exception:
            logger.exception("HxStream: failed to persist trace event")

    # Broadcast to live subscribers (never raises)
    try:
        _broadcast(event)
    except Exception:
        logger.exception("HxStream: broadcast error")
