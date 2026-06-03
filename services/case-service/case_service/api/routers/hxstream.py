"""HxStream — Live Execution & Interaction Stream (P46).

Endpoints:
  POST /hxstream/event          — frontend UI interaction events
  GET  /hxstream/events         — paginated history (role-scoped)
  GET  /hxstream/replay/{case}  — ordered event log for one case
  WS   /hxstream/ws             — live broadcast (role-scoped, token-auth)
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone

from fastapi import (
    APIRouter, Depends, HTTPException, Query,
    Request, WebSocket, WebSocketDisconnect,
)
from pydantic import BaseModel
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser
from case_service.db.session import get_session
from case_service.db.models import TraceEventModel
from case_service.hxstream.emitter import (
    emit_trace,
    _register_subscriber,
    _unregister_subscriber,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/hxstream", tags=["hxstream"])

# Portal type → numeric access level (higher = broader visibility)
_PORTAL_RANK = {
    "customer": 0,
    "viewer":   1,
    "staff":    2,
    "manager":  3,
    "admin":    4,
}

# Minimum rank required to see other users' events in HxStream
_ADMIN_RANK = 4   # only full admin sees everything
_MANAGER_RANK = 3 # managers see their own + reports


def _actor_scope(user: AuthenticatedUser) -> str | None:
    """Return the user_id to restrict events to, or None if unrestricted.

    - admin portal (or is_admin) → None (see everything)
    - manager portal → only their own events for now (report tree is complex)
    - all others → their own events only
    """
    if user.is_admin:
        return None
    ag = user.active_access_group
    if ag:
        rank = _PORTAL_RANK.get(ag.portal_type, 0)
        if rank >= _ADMIN_RANK:
            return None
    # non-admin: restrict to own events
    return user.user_id


def _require_hxstream_access(user: AuthenticatedUser) -> None:
    """HxStream requires admin or staff-level portal (not customer/viewer)."""
    if user.is_admin:
        return
    ag = user.active_access_group
    if ag and _PORTAL_RANK.get(ag.portal_type, 0) >= _PORTAL_RANK["staff"]:
        return
    raise HTTPException(403, "HxStream access requires at least staff-level portal.")


# ── POST /hxstream/event ─────────────────────────────────────────────────────

class UIEventIn(BaseModel):
    event_type: str = "ui_interaction"
    case_id: str | None = None
    payload: dict = {}
    session_id: str | None = None


@router.post("/event", status_code=202)
async def post_ui_event(
    body: UIEventIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Record a frontend user interaction (fire-and-forget from the UI)."""
    client_ip = request.client.host if request.client else None
    tenant_id = (
        str(user.active_access_group.id)
        if user.active_access_group else "default"
    )
    await emit_trace(
        body.event_type, body.payload,
        case_id=body.case_id, tenant_id=tenant_id,
        actor_user_id=user.user_id, actor_ip=client_ip,
        session_id=body.session_id, session=session,
    )
    return {"status": "accepted"}


# ── GET /hxstream/events ─────────────────────────────────────────────────────

@router.get("/events")
async def list_events(
    case_id: str | None = Query(None),
    event_type: str | None = Query(None),
    actor_user_id: str | None = Query(None),
    limit: int = Query(100, le=500),
    offset: int = Query(0),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Paginated history.  Non-admin users are automatically scoped to their
    own events — they cannot query other users' activity."""
    _require_hxstream_access(user)

    # Determine visibility scope
    own_id = _actor_scope(user)

    stmt = select(TraceEventModel).order_by(desc(TraceEventModel.occurred_at))

    # __system__ actor = superadmin actions — hidden from all regular views
    stmt = stmt.where(TraceEventModel.actor_user_id != "__system__")

    # If scoped (non-admin), enforce own-events restriction
    if own_id is not None:
        stmt = stmt.where(TraceEventModel.actor_user_id == own_id)
        if actor_user_id and actor_user_id != own_id:
            raise HTTPException(403, "You can only view your own events.")
        actor_user_id = own_id
    elif actor_user_id:
        stmt = stmt.where(TraceEventModel.actor_user_id == actor_user_id)

    if case_id:
        try:
            stmt = stmt.where(TraceEventModel.case_id == uuid.UUID(case_id))
        except ValueError:
            pass
    if event_type:
        stmt = stmt.where(TraceEventModel.event_type == event_type)

    stmt = stmt.offset(offset).limit(limit)
    rows = (await session.execute(stmt)).scalars().all()
    return {"events": [_serialize(r) for r in rows], "total": len(rows)}


# ── GET /hxstream/replay/{case_id} ──────────────────────────────────────────

@router.get("/replay/{case_id}")
async def replay_case(
    case_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_hxstream_access(user)
    stmt = (
        select(TraceEventModel)
        .where(TraceEventModel.case_id == case_id)
        .order_by(TraceEventModel.occurred_at)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return {"case_id": str(case_id), "events": [_serialize(r) for r in rows]}


# ── GET /hxstream/actors ─────────────────────────────────────────────────────

@router.get("/actors")
async def list_actors(
    q: str | None = Query(None),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Return distinct actor_user_ids that appear in trace_events.

    Non-admins only see themselves.  Admins get a searchable list.
    """
    _require_hxstream_access(user)
    own_id = _actor_scope(user)

    if own_id is not None:
        # non-admin: only their own ID
        return {"actors": [{"user_id": own_id, "display_name": user.user_id}]}

    # Admin: distinct actors from trace_events, enriched with user directory
    from sqlalchemy import func, distinct
    from case_service.db.models import UserDirectoryModel

    # Distinct actor IDs from trace_events (last 30 days for performance)
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    actor_stmt = (
        select(distinct(TraceEventModel.actor_user_id))
        .where(
            TraceEventModel.actor_user_id.isnot(None),
            TraceEventModel.actor_user_id != "__system__",  # superadmin actions hidden
            TraceEventModel.occurred_at >= cutoff,
        )
        .limit(200)
    )
    actor_ids = [r[0] for r in (await session.execute(actor_stmt)).all()]

    # Enrich with display names from user directory
    dir_stmt = select(UserDirectoryModel).where(
        UserDirectoryModel.user_id.in_(actor_ids)
    )
    if q:
        pattern = f"%{q}%"
        dir_stmt = dir_stmt.where(
            (UserDirectoryModel.user_id.ilike(pattern))
            | (UserDirectoryModel.display_name.ilike(pattern))
            | (UserDirectoryModel.email.ilike(pattern))
        )
    dir_rows = (await session.execute(dir_stmt)).scalars().all()
    dir_map = {u.user_id: u for u in dir_rows}

    actors = []
    for aid in actor_ids:
        u = dir_map.get(aid)
        label = f"{u.display_name or aid} ({u.email or ''})" if u else aid
        if q and q.lower() not in label.lower() and q.lower() not in aid.lower():
            continue
        actors.append({
            "user_id": aid,
            "display_name": u.display_name if u else None,
            "email": u.email if u else None,
            "label": label,
        })

    return {"actors": actors}


# ── WS /hxstream/ws ──────────────────────────────────────────────────────────

@router.websocket("/ws")
async def hxstream_ws(
    websocket: WebSocket,
    token: str | None = Query(None),
    case_id: str | None = Query(None),
    event_types: str | None = Query(None),
):
    """Live HxStream WebSocket — role-scoped.

    Auth: pass ?token=<bearer_token> in the query string (browsers can't set
    Authorization headers on WebSocket connections).

    Visibility rules (mirroring the REST endpoint):
      - admin → all events
      - staff/manager → own events only
      - no/invalid token → rejected
    """
    await websocket.accept()

    # ── Authenticate ────────────────────────────────────────────────
    ws_user_id: str | None = None
    is_admin = False

    if token:
        try:
            from case_service.auth.jwt_handler import decode_jwt_token
            from case_service.config import get_settings
            claims = decode_jwt_token(token, secret=get_settings().auth_secret)
            ws_user_id = str(claims.get("sub") or claims.get("preferred_username") or "")
            roles = []
            realm = claims.get("realm_access", {})
            if isinstance(realm, dict):
                roles = realm.get("roles", [])
            if not roles:
                roles = claims.get("roles", [])
            is_admin = "admin" in roles
        except Exception:
            await websocket.send_json({"type": "error", "message": "Invalid or expired token"})
            await websocket.close(code=4001)
            return
    else:
        await websocket.send_json({"type": "error", "message": "Authentication required"})
        await websocket.close(code=4001)
        return

    # Determine actor_scope: None = see all, str = see only own
    actor_scope: str | None = None if is_admin else ws_user_id

    # ── Register subscriber ──────────────────────────────────────────
    sub_id = str(uuid.uuid4())
    q = _register_subscriber(sub_id)

    allowed_types: set[str] | None = None
    if event_types:
        allowed_types = {t.strip() for t in event_types.split(",")}

    try:
        await websocket.send_json({"type": "connected", "sub_id": sub_id, "scope": "admin" if is_admin else "own"})

        async def _pump():
            while True:
                event = await q.get()
                # Role-based visibility filter
                if actor_scope and event.get("actor_user_id") != actor_scope:
                    continue
                # Optional client-specified filters
                if case_id and event.get("case_id") != case_id:
                    continue
                if allowed_types and event.get("event_type") not in allowed_types:
                    continue
                await websocket.send_json(event)

        pump_task = asyncio.create_task(_pump())

        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=30)
                msg = json.loads(raw)
                if msg.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "heartbeat"})

    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("HxStream WS error for sub %s", sub_id)
    finally:
        pump_task.cancel()
        _unregister_subscriber(sub_id)


# ── helpers ───────────────────────────────────────────────────────────────────

def _serialize(row: TraceEventModel) -> dict:
    return {
        "id": str(row.id),
        "event_type": row.event_type,
        "case_id": str(row.case_id) if row.case_id else None,
        "tenant_id": row.tenant_id,
        "actor_user_id": row.actor_user_id,
        "actor_ip": row.actor_ip,
        "payload": row.payload,
        "occurred_at": row.occurred_at.isoformat() if row.occurred_at else None,
        "session_id": row.session_id,
        "latency_ms": row.latency_ms,
    }
