"""Durable event log — the CDC half of the outbox backbone (report §2.1).

The transactional outbox has existed since Group C: domain events are written
in the **same transaction** as the state change, so an event can never be lost
by a post-commit publish failing. What it lacked was a fan-out: the only sink
was HTTP webhook delivery, so an event reached whoever had subscribed a URL and
nobody else. Analytics, HxShield detection, the lakehouse and event sourcing all
need to read the same stream, and none of them are webhooks.

This drains the outbox onto the **durable log** §2.1 names (Apache Kafka, the
`events` service in `docker-compose`, started only when the bus is on), so any
number of consumers can read at their own pace and replay from any offset.

Three deliberate choices:

**A separate cursor, not the delivery flag.** Publishing tracks its own
`published_at` per row rather than competing with `delivered_at`. HTTP delivery
semantics are therefore completely untouched, the bus can be enabled or
disabled without disturbing webhooks, and a backlog is visible rather than
implied.

**Keyed by case.** The partition key is the case id, so every event for one
case lands on one partition and is consumed **in order**. Event sourcing (§2.2,
roadmap #30a) is sequenced behind this precisely because it needs that
guarantee; an unkeyed topic would have made it impossible without a rewrite.

**Off by default, and inert without the client.** `aiokafka` is imported
lazily: a deployment that never turns the bus on does not need the wheel, and a
missing wheel disables the relay rather than breaking startup — the same
posture `hxsandbox` takes with `wasmtime`.

Events published here are the **thin** events Group I introduced (ids, status
and context — never full case data), so adding a consumer does not create a new
copy of PII that redaction and crypto-shredding do not reach.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 2.0      # seconds between drain cycles
_BATCH_SIZE = 200
_CLAIM_TTL = 60           # seconds before a stalled claim is retryable


class EventBusUnavailable(RuntimeError):
    """The broker or its client library is not usable."""


def _auth_kwargs(username: str, password: str, tls_ca: str = "") -> dict:
    """Client settings for the broker's SASL/PLAIN listener.

    No password means no SASL: right for an operator's own broker that runs
    without authentication, and the only behaviour before November 2026. The
    bundled broker always requires one, so against it an empty password fails
    to connect — the relay reports that as unavailable and the outbox keeps
    every event until it is fixed.

    A CA file (VELARIS_CASE_EVENT_BUS_TLS_CA — a broker on another server)
    turns on TLS with hostname checking: SASL_SSL, or plain SSL without a
    password. Empty = no TLS, as before.
    """
    tls: dict = {}
    if tls_ca:
        from aiokafka.helpers import create_ssl_context

        tls = {"ssl_context": create_ssl_context(cafile=tls_ca)}
    if not password:
        return {"security_protocol": "SSL", **tls} if tls else {}
    return {
        "security_protocol": "SASL_SSL" if tls else "SASL_PLAINTEXT",
        "sasl_mechanism": "PLAIN",
        "sasl_plain_username": username or "velaris",
        "sasl_plain_password": password,
        **tls,
    }


def _load_producer_class():
    """Import aiokafka lazily.

    Raises :class:`EventBusUnavailable` rather than ImportError so callers have
    one failure type to handle whether the wheel is missing or the broker is
    down — from the relay's point of view those are the same condition.
    """
    try:
        from aiokafka import AIOKafkaProducer
    except Exception as exc:                      # pragma: no cover — env-dependent
        raise EventBusUnavailable(
            "aiokafka is not installed — install the 'events' extra to enable "
            "the durable event log"
        ) from exc
    return AIOKafkaProducer


def topic_for(event_type: str, prefix: str) -> str:
    """Topic name for an event type.

    One topic per event *family* (the part before the first dot), so a consumer
    can subscribe to `velaris.case` without also receiving marketplace or authz
    traffic, and without needing a topic per individual event type.
    """
    family = (event_type or "unknown").split(".", 1)[0] or "unknown"
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in family)
    return f"{prefix}.{safe}"


def partition_key(payload: dict[str, Any]) -> bytes:
    """Case id when there is one, so a case's events stay ordered.

    Falls back to a random key rather than a constant: a constant would funnel
    every keyless event onto one partition and make it the bottleneck.
    """
    for field in ("case_id", "caseId", "id"):
        value = (payload or {}).get(field)
        if value:
            return str(value).encode()
    return uuid.uuid4().bytes


def envelope(row_id, event_type: str, payload: dict, case_type_id, created_at) -> bytes:
    """Serialise one event.

    `outbox_id` is included so a consumer can deduplicate: delivery is
    at-least-once, and a consumer that cares must be able to tell a redelivery
    from a new event.
    """
    return json.dumps({
        "outbox_id": str(row_id),
        "event_type": event_type,
        "case_type_id": str(case_type_id) if case_type_id else None,
        "occurred_at": created_at.isoformat() if created_at else None,
        "payload": payload or {},
    }, default=str).encode()


class EventBusRelay:
    """Drains unpublished outbox rows onto the durable log.

    Mirrors `OutboxRelay`'s two-transaction shape: claim under
    `FOR UPDATE SKIP LOCKED`, commit, then publish without holding a pool
    connection. Publishing failures leave `published_at` NULL, so the next
    cycle retries — at-least-once, same as HTTP delivery.
    """

    def __init__(self, session_factory, *, brokers: str, topic_prefix: str = "velaris",
                 username: str = "", password: str = "", tls_ca: str = "") -> None:
        self._factory = session_factory
        self._brokers = brokers
        self._auth = _auth_kwargs(username, password, tls_ca)
        self._prefix = topic_prefix
        self._producer = None
        self._task: asyncio.Task | None = None
        self._running = False
        #: Topics already known to exist. The bundled broker has topic
        #: auto-creation switched off, and a producer against a missing topic fails with
        #: UnknownTopicOrPartitionError — which would leave the relay retrying
        #: forever against a broker that was working fine.
        self._ensured: set[str] = set()

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run())
        logger.info("event bus relay started (brokers=%s)", self._brokers)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
        await self._close_producer()

    async def _close_producer(self) -> None:
        if self._producer is not None:
            try:
                await self._producer.stop()
            except Exception:                     # pragma: no cover
                logger.debug("event bus producer close failed", exc_info=True)
            self._producer = None

    async def _ensure_producer(self):
        if self._producer is not None:
            return self._producer
        producer_cls = _load_producer_class()
        producer = producer_cls(bootstrap_servers=self._brokers, acks="all", **self._auth)
        try:
            await producer.start()
        except Exception as exc:
            raise EventBusUnavailable(f"broker unreachable at {self._brokers}: {exc}") from exc
        self._producer = producer
        return producer

    async def _ensure_topic(self, topic: str) -> None:
        """Create the topic if the broker does not have it.

        Idempotent and cached per process: `TopicAlreadyExistsError` is the
        expected outcome whenever another worker got there first, so it is
        treated as success rather than an error.
        """
        if topic in self._ensured:
            return
        try:
            from aiokafka.admin import AIOKafkaAdminClient, NewTopic
        except Exception as exc:                  # pragma: no cover
            raise EventBusUnavailable(f"aiokafka admin unavailable: {exc}") from exc

        admin = AIOKafkaAdminClient(bootstrap_servers=self._brokers, **self._auth)
        try:
            await admin.start()
            try:
                await admin.create_topics(
                    [NewTopic(name=topic, num_partitions=3, replication_factor=1)]
                )
            except Exception as exc:
                # Already exists is success. Anything else is worth surfacing,
                # but must not stop the batch — the send below will report it.
                if "already exists" not in str(exc).lower():
                    logger.info("topic %s not created: %s", topic, exc)
        finally:
            try:
                await admin.close()
            except Exception:                     # pragma: no cover
                pass
        self._ensured.add(topic)

    async def _run(self) -> None:
        while self._running:
            published = 0
            try:
                published = await self.drain_once()
            except EventBusUnavailable as exc:
                # Expected when the wheel is absent or the broker is down. Log
                # once per cycle at info: the outbox is still safe on disk, so
                # this is a delay, never a loss.
                logger.info("event bus unavailable, will retry: %s", exc)
                await self._close_producer()
            except Exception as exc:
                logger.warning("event bus relay cycle error: %s", exc)
            # A full batch means more rows are waiting: take the next at once
            # (as the webhook relay, R6 soak 2026-11-06 L2); pause only when
            # the queue is drained or the broker failed.
            await asyncio.sleep(0 if published >= _BATCH_SIZE else _POLL_INTERVAL)

    async def drain_once(self) -> int:
        """Publish one batch. Returns how many events were published."""
        from velaris_shared.db.models import OutboxEventModel

        now = datetime.now(timezone.utc)
        stale = now - timedelta(seconds=_CLAIM_TTL)
        claimed: list[tuple] = []

        async with self._factory() as session:
            base_q = (
                select(OutboxEventModel)
                .where(
                    OutboxEventModel.published_at.is_(None),
                    or_(
                        OutboxEventModel.publish_claimed_at.is_(None),
                        OutboxEventModel.publish_claimed_at < stale,
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
                # SQLite in dev/test has no SKIP LOCKED; a plain select is safe
                # because asyncio is single-threaded within one process.
                rows = (await session.execute(base_q)).scalars().all()

            if not rows:
                return 0
            for row in rows:
                row.publish_claimed_at = now
                claimed.append((row.id, row.event_type, row.payload,
                                row.case_type_id, row.created_at))
            await session.commit()

        producer = await self._ensure_producer()

        published: list[uuid.UUID] = []
        for row_id, event_type, payload, case_type_id, created_at in claimed:
            try:
                topic = topic_for(event_type, self._prefix)
                await self._ensure_topic(topic)
                await producer.send_and_wait(
                    topic,
                    value=envelope(row_id, event_type, payload, case_type_id, created_at),
                    key=partition_key(payload),
                )
                published.append(row_id)
            except Exception as exc:
                # Leave published_at NULL — the next cycle retries this row.
                logger.warning("event %s not published: %s", row_id, exc)

        if published:
            async with self._factory() as session:
                rows = (await session.execute(
                    select(OutboxEventModel).where(OutboxEventModel.id.in_(published))
                )).scalars().all()
                for row in rows:
                    row.published_at = datetime.now(timezone.utc)
                await session.commit()

        return len(published)


async def backlog(session) -> dict[str, Any]:
    """How far behind the bus is — the number an operator actually wants."""
    from sqlalchemy import func

    from velaris_shared.db.models import OutboxEventModel

    unpublished = (await session.execute(
        select(func.count()).where(OutboxEventModel.published_at.is_(None))
    )).scalar_one()
    oldest = (await session.execute(
        select(func.min(OutboxEventModel.created_at))
        .where(OutboxEventModel.published_at.is_(None))
    )).scalar_one()
    return {
        "unpublished": int(unpublished or 0),
        "oldest_unpublished_at": oldest.isoformat() if oldest else None,
    }
