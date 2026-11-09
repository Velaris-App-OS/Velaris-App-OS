"""Read `security_events` on a cursor, push them to the target, advance.

**Scope is the platform, not a tenant — and that is a decision, not an
oversight.** `security_events` carries no tenant column: the two chokepoints
that write it (the audit middleware and HxGuard's deny path) know the actor,
not the actor's tenant, and the tenant lives in `user_directory` rather than in
the token. So a target that claimed to be tenant-scoped would either forward
every tenant's events under one tenant's name — the exact shape of the leaks
found in September — or filter on a column that does not exist.

Refusing it is also the right model for what this platform is. A Velaris
install belongs to ONE organisation; its tenants are that organisation's own
business units, and its SIEM is that organisation's SOC. The case this does not
serve is a service provider running several customers on one install and
splitting the audit stream between their separate SOCs. That needs a tenant on
the event, and is named as unbuilt rather than half-built.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
import logging
import socket
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import and_, or_, select

from case_service.audit_stream.format import (
    SEVERITY_ORDER, format_batch, severity_rank,
)

logger = logging.getLogger(__name__)

#: How many events one push carries. Splunk's HEC accepts far more; the ceiling
#: here is about how much work is lost when a push fails, since a failed batch
#: is retried whole.
BATCH_SIZE = 500

#: A push that hangs holds the scheduler's slot. Short enough that a dead SIEM
#: is reported as an error this run rather than as a stall nobody sees.
PUSH_TIMEOUT_SECONDS = 15.0


class AuditStreamError(Exception):
    """The batch was not accepted. The cursor does not move."""


def _host() -> str:
    """What the SIEM shows as the originating host.

    The machine's own name, resolved once per push. A SOC correlating an alert
    needs to know which box it came from, and this is the only field that says.
    """
    try:
        return socket.gethostname() or "velaris"
    except Exception:  # noqa: BLE001
        return "velaris"


def _event_types(target) -> list[str]:
    raw = (target.event_types or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(t).strip() for t in parsed if str(t).strip()]
    except (ValueError, TypeError):
        pass
    return [t.strip() for t in raw.split(",") if t.strip()]


async def _read_batch(session, target, *, limit: int = BATCH_SIZE) -> list[dict[str, Any]]:
    """Events after the cursor, oldest first.

    The cursor is `(timestamp, id)` rather than `timestamp` alone: two events
    can share a microsecond, and a cursor on the timestamp alone either replays
    them forever or skips one of them.
    """
    from velaris_shared.db.models import SecurityEventModel as SE

    stmt = select(SE).order_by(SE.timestamp.asc(), SE.id.asc()).limit(limit)

    if target.cursor_timestamp is not None:
        cursor_time = target.cursor_timestamp
        if cursor_time.tzinfo is None:
            cursor_time = cursor_time.replace(tzinfo=timezone.utc)
        if target.cursor_event_id is not None:
            stmt = stmt.where(or_(
                SE.timestamp > cursor_time,
                and_(SE.timestamp == cursor_time, SE.id > target.cursor_event_id),
            ))
        else:
            stmt = stmt.where(SE.timestamp > cursor_time)

    wanted_types = _event_types(target)
    if wanted_types:
        stmt = stmt.where(SE.event_type.in_(wanted_types))
    # A tenant's target carries its own tenant's events only (2026-11-04,
    # migration 230); the platform operator's target (no tenant) every event.
    if getattr(target, "tenant_id", None):
        from case_service.tenancy.identity import resolve_tenant
        spellings = {str(target.tenant_id)}
        hit = await resolve_tenant(session, str(target.tenant_id))
        if hit is not None:
            spellings |= {str(hit[0]), str(hit[1])}
        stmt = stmt.where(SE.tenant_id.in_(sorted(spellings)))

    rows = (await session.execute(stmt)).scalars().all()

    # Severity is filtered in Python, not SQL: the ladder is an ORDER, and
    # expressing "at least warning" in SQL would mean either a CASE per dialect
    # or a numeric column that then has to stay in step with the names.
    floor = severity_rank(target.min_severity) if target.min_severity else -1
    events = []
    for row in rows:
        if floor >= 0 and severity_rank(row.severity) < floor:
            # Filtered out — but its position still counts, or the cursor would
            # stick on a run of low-severity events and replay them forever.
            events.append({"__skipped__": True, "id": row.id, "timestamp": row.timestamp})
            continue
        events.append({
            "id": row.id,
            "event_type": row.event_type,
            "severity": row.severity,
            "user_id": row.user_id,
            "resource_type": row.resource_type,
            "resource_id": row.resource_id,
            "ip_address": row.ip_address,
            "user_agent": row.user_agent,
            "action": row.action,
            "outcome": row.outcome,
            "details": row.details or {},
            "timestamp": row.timestamp,
            # >1 = a summary of that many refused repeats (migration 231).
            "repeat_count": getattr(row, "repeat_count", 1) or 1,
        })
    return events


def _decrypt_token(target) -> str | None:
    if not target.token_encrypted:
        return None
    from case_service.hxbridge.encryption import decrypt_credentials
    try:
        stored = json.loads(target.token_encrypted)
    except (ValueError, TypeError):
        return None
    try:
        return decrypt_credentials(stored).get("token")
    except Exception:  # noqa: BLE001
        logger.warning("audit stream target %s: token could not be decrypted", target.id)
        return None


async def push(target, events: list[dict[str, Any]]) -> None:
    """Send one batch. Raises `AuditStreamError` if it was not accepted."""
    if not events:
        return
    token = _decrypt_token(target)
    if not token:
        raise AuditStreamError(
            "No HEC token is stored for this target. Splunk refuses an "
            "unauthenticated push, so nothing was sent.")

    body = format_batch(events, host=_host())
    headers = {
        "Authorization": f"Splunk {token}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(
            timeout=PUSH_TIMEOUT_SECONDS,
            verify=bool(target.verify_tls),
            # A redirect is how an endpoint hands your audit — and your token —
            # to somewhere else. Refused, never followed, exactly as the
            # calendar feed refuses one on the way in.
            follow_redirects=False,
        ) as client:
            response = await client.post(target.endpoint_url, content=body, headers=headers)
    except httpx.HTTPError as exc:
        raise AuditStreamError(f"{type(exc).__name__}: {exc}") from exc

    if response.is_redirect:
        raise AuditStreamError(
            f"The endpoint redirected to {response.headers.get('location', '?')}. "
            "A redirect is refused rather than followed — an audit stream that "
            "follows one is an audit stream that can be pointed elsewhere.")
    if response.status_code >= 400:
        # HEC answers with a JSON `text`/`code` pair; keep it, it is the whole
        # diagnosis ("Invalid token", "Incorrect index", …).
        detail = (response.text or "")[:300]
        raise AuditStreamError(f"HTTP {response.status_code}: {detail}")


async def forward_target(session, target, *, max_batches: int = 20) -> dict[str, Any]:
    """Drain what is waiting for one target, up to a bounded number of batches.

    Bounded because a target enabled for the first time on a busy install has a
    backlog measured in hundreds of thousands, and one scheduler run must not
    try to ship all of it. The cursor makes the next run continue rather than
    start again.
    """
    sent = 0
    skipped = 0
    batches = 0
    while batches < max_batches:
        events = await _read_batch(session, target)
        if not events:
            break
        batches += 1
        real = [e for e in events if not e.get("__skipped__")]
        try:
            await push(target, real)
        except AuditStreamError as exc:
            # The cursor is NOT advanced. Everything in this batch is read again
            # next run, which is what makes the delivery at-least-once.
            target.last_error = str(exc)[:1000]
            target.last_error_at = datetime.now(timezone.utc)
            await session.flush()
            raise

        last = events[-1]
        target.cursor_timestamp = last["timestamp"]
        target.cursor_event_id = last["id"]
        target.forwarded_count = (target.forwarded_count or 0) + len(real)
        target.last_success_at = datetime.now(timezone.utc)
        target.last_error = None
        target.last_error_at = None
        await session.flush()

        sent += len(real)
        skipped += len(events) - len(real)
        if len(events) < BATCH_SIZE:
            break

    return {
        "target_id": str(target.id),
        "name": target.name,
        "sent": sent,
        "filtered_out": skipped,
        "batches": batches,
        "caught_up": batches < max_batches,
    }


async def run_due_targets(session, *, tenant_id: str | None = None) -> list[dict[str, Any]]:
    """Forward for every enabled target. One failure does not stop the others.

    A single unreachable SIEM must not stop a second one from receiving events —
    that would turn one team's outage into everybody's blind spot.
    """
    from velaris_shared.db.models import AuditStreamTargetModel

    stmt = select(AuditStreamTargetModel).where(
        AuditStreamTargetModel.enabled.is_(True))
    targets = (await session.execute(stmt)).scalars().all()

    results = []
    for target in targets:
        try:
            results.append(await forward_target(session, target))
        except AuditStreamError as exc:
            results.append({
                "target_id": str(target.id),
                "name": target.name,
                "sent": 0,
                "error": str(exc)[:300],
            })
        except Exception as exc:  # noqa: BLE001
            logger.exception("audit stream target %s failed", target.id)
            results.append({
                "target_id": str(target.id), "name": target.name,
                "sent": 0, "error": f"{type(exc).__name__}",
            })
    return results


__all__ = [
    "AuditStreamError", "BATCH_SIZE", "SEVERITY_ORDER",
    "forward_target", "push", "run_due_targets",
]
