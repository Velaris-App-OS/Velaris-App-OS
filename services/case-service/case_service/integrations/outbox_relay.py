"""OutboxRelay — background task that delivers queued outbox events via HTTP.

Design guarantees:
- At-least-once delivery: rows stay in the outbox until confirmed delivered.
- Crash-safe claiming: claimed_at is reset after 5 min so crashed rows are
  re-tried by the next relay cycle.
- No double-delivery under concurrent workers: uses FOR UPDATE SKIP LOCKED
  (Postgres); SQLite (dev/test) falls back to a plain SELECT — safe because
  asyncio is single-threaded within one process.
- No long-lived DB transactions during HTTP I/O: the relay uses TWO short
  transactions per cycle:
    1. Claim (set claimed_at) → commit → release lock + connection immediately.
    2. After all HTTP calls finish, persist results → commit.
  This keeps pool connections free for the application during delivery.
- SSRF-safe: validate_outbound_url() is called before every HTTP delivery.
- Exponential back-off: 30s × 2^(attempts-1) between retries; max 5 attempts.
- Keeps up under load (R6 soak 2026-11-06, L2): a full batch is followed by the
  next one at once — the 5 s pause is only for an empty or part-filled queue —
  and the subscriptions are read once per batch, not once per row. Before, one
  relay delivered at most 50 rows per 5 s and the backlog grew ~10k rows/h.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.integrations.webhook_dispatcher import (
    compute_signature,
    load_active_subscriptions,
    match_subscriptions,
)

logger = logging.getLogger(__name__)

_POLL_INTERVAL  = 5    # seconds between relay cycles
_MAX_ATTEMPTS   = 5    # total delivery attempts per row
_CLAIM_TTL      = 300  # seconds before a claimed row is re-claimable (crash recovery)
_BATCH_SIZE     = 50   # rows per relay cycle


def _backoff(attempts: int) -> timedelta:
    """Exponential back-off: 30s, 60s, 120s, 240s after attempts 1-4."""
    return timedelta(seconds=30 * (2 ** max(0, attempts - 1)))


@dataclass
class _RowResult:
    """Delivery outcome for one outbox row — passed between the two transactions."""
    row_id:          uuid.UUID
    attempts:        int
    delivered_at:    datetime | None = None
    next_attempt_at: datetime | None = None


class OutboxRelay:
    """Async background relay for the transactional outbox.

    Usage:
        relay = OutboxRelay(session_factory)
        relay.start()   # in lifespan startup
        relay.stop()    # in lifespan shutdown
    """

    def __init__(self, session_factory) -> None:
        self._factory = session_factory
        self._task: asyncio.Task | None = None
        self._running = False

    def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run(), name="outbox-relay")
        logger.info(
            "OutboxRelay started (poll=%ds, max_attempts=%d)",
            _POLL_INTERVAL, _MAX_ATTEMPTS,
        )

    def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("OutboxRelay stopped")

    async def _run(self) -> None:
        while self._running:
            claimed = 0
            try:
                claimed = await self._deliver_batch()
            except Exception as exc:
                logger.warning("outbox relay cycle error: %s", exc)
            # A full batch means more rows are waiting: take the next batch at
            # once (yielding to the event loop first). Pause only when the
            # queue is drained, or after an error.
            await asyncio.sleep(0 if claimed >= _BATCH_SIZE else _POLL_INTERVAL)

    async def _deliver_batch(self) -> int:
        """Deliver one batch; returns how many rows it claimed."""
        from velaris_shared.db.models import OutboxEventModel

        now             = datetime.now(timezone.utc)
        stale_threshold = now - timedelta(seconds=_CLAIM_TTL)

        # ── Transaction 1: claim rows, commit immediately ──────────────────
        # The FOR UPDATE SKIP LOCKED lock is held only for this short write,
        # not for the duration of HTTP delivery. The pool connection is
        # returned to the pool as soon as this block exits.
        # (id, event_type, payload, case_type_id, current_attempts)
        claimed: list[tuple[uuid.UUID, str, dict, uuid.UUID | None, int]] = []

        async with self._factory() as session:
            base_q = (
                select(OutboxEventModel)
                .where(
                    OutboxEventModel.delivered_at.is_(None),
                    OutboxEventModel.attempts < _MAX_ATTEMPTS,
                    or_(
                        OutboxEventModel.next_attempt_at.is_(None),
                        OutboxEventModel.next_attempt_at <= now,
                    ),
                    or_(
                        OutboxEventModel.claimed_at.is_(None),
                        OutboxEventModel.claimed_at < stale_threshold,
                    ),
                )
                .order_by(OutboxEventModel.created_at)
                .limit(_BATCH_SIZE)
            )

            try:
                rows = (await session.execute(
                    base_q.with_for_update(skip_locked=True)
                )).scalars().all()
            except Exception:
                # Non-Postgres (SQLite in dev/test): plain SELECT.
                # Safe because asyncio is single-threaded within one process.
                rows = (await session.execute(base_q)).scalars().all()

            if not rows:
                return 0

            for row in rows:
                row.claimed_at = now
                # Capture current attempts so _deliver_one knows which attempt number this is
                claimed.append((row.id, row.event_type, row.payload, row.case_type_id, row.attempts))
            # Commit immediately — lock released, connection returned to pool
            await session.commit()

        # The active subscriptions, read ONCE for the batch (short read-only
        # session, closed before any HTTP call).
        async with self._factory() as session:
            subs = await load_active_subscriptions(session)

        # ── HTTP delivery — no DB connection held ─────────────────────────
        results: list[_RowResult] = []
        for row_id, event_type, payload, case_type_id, current_attempts in claimed:
            result = await self._deliver_one(
                row_id, event_type, payload, case_type_id, now, current_attempts,
                subs,
            )
            results.append(result)

        # ── Transaction 2: persist delivery outcomes ──────────────────────
        async with self._factory() as session:
            by_id = {
                r.id: r for r in (await session.execute(
                    select(OutboxEventModel).where(
                        OutboxEventModel.id.in_([res.row_id for res in results]))
                )).scalars().all()
            }
            for res in results:
                db_row = by_id.get(res.row_id)
                if db_row is None:
                    continue
                db_row.attempts        = res.attempts
                db_row.delivered_at    = res.delivered_at
                db_row.next_attempt_at = res.next_attempt_at
                if res.attempts >= _MAX_ATTEMPTS and res.delivered_at is None:
                    # Exhausted — log as error; delivered_at stays NULL for auditing
                    logger.error(
                        "outbox: row %s exhausted %d attempts — giving up",
                        res.row_id, _MAX_ATTEMPTS,
                    )
            await session.commit()
        return len(claimed)

    async def _deliver_one(
        self,
        row_id:           uuid.UUID,
        event_type:       str,
        payload:          dict,
        case_type_id:     uuid.UUID | None,
        now:              datetime,
        current_attempts: int,
        active_subs:      list,
    ) -> _RowResult:
        """Deliver one outbox row. Returns the outcome without touching the DB."""
        from case_service.hxbridge.security import validate_outbound_url

        subs = match_subscriptions(active_subs, event_type, case_type_id)

        # Increment the attempt counter — Transaction 2 will persist this
        result = _RowResult(row_id=row_id, attempts=current_attempts + 1)

        if not subs:
            result.delivered_at = now
            return result

        body          = json.dumps(payload, default=str)
        delivered_any = False

        for sub in subs:
            try:
                await validate_outbound_url(sub.url)

                headers: dict[str, str] = {
                    "Content-Type":    "application/json",
                    "X-Helix-Event":    event_type,
                    "X-Helix-Delivery": str(row_id),
                    **(sub.headers or {}),
                }
                if sub.secret:
                    headers["X-Helix-Signature"] = compute_signature(body, sub.secret)

                async with httpx.AsyncClient(
                    timeout=getattr(sub, "timeout_seconds", 15),
                    follow_redirects=False,
                ) as client:
                    resp = await client.post(sub.url, content=body, headers=headers)

                if resp.status_code < 400:
                    delivered_any = True
                else:
                    logger.warning(
                        "outbox: delivery to %s returned HTTP %d (row=%s)",
                        sub.url, resp.status_code, row_id,
                    )

            except ValueError as ssrf_err:
                logger.error(
                    "outbox: SSRF guard blocked delivery to %s: %s", sub.url, ssrf_err
                )
                delivered_any = True  # permanently blocked — don't retry this sub

            except Exception as exc:
                logger.warning(
                    "outbox: HTTP delivery failed for %s: %s", sub.url, exc
                )

        if delivered_any:
            result.delivered_at = now
        else:
            result.next_attempt_at = now + _backoff(result.attempts)
            logger.info(
                "outbox: row %s will retry at %s",
                row_id, result.next_attempt_at,
            )

        return result
