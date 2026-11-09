"""Durable event log — the CDC fan-out of the outbox (report §2.1).

The outbox has been atomic with domain writes since Group C, so nothing here
is about durability. It is about **distribution**: the only sink was HTTP
webhook delivery, so analytics, HxShield and event sourcing could not read the
same stream.

The properties worth pinning are the ones that would be silently wrong:
publishing must not disturb webhook delivery, a case's events must stay in
order, a failed publish must retry rather than vanish, and the whole thing must
be inert when switched off or when the client library is absent.

No broker is required — the producer is faked, because these tests are about
the relay's contract, not aiokafka's.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from velaris_shared.db.models import OutboxEventModel
from case_service.integrations import event_bus

pytestmark = pytest.mark.asyncio


@pytest.fixture
def session_factory():
    """The suite's own factory — the relay opens its own sessions, as in prod."""
    from tests.conftest import TestSessionFactory

    return TestSessionFactory


class _FakeProducer:
    """Records what would have been sent; can be told to fail."""

    def __init__(self, fail_on: set[str] | None = None) -> None:
        self.sent: list[tuple[str, bytes, bytes]] = []
        self._fail_on = fail_on or set()
        self.stopped = False

    async def send_and_wait(self, topic, value, key=None):
        decoded = json.loads(value)
        if decoded["event_type"] in self._fail_on:
            raise RuntimeError("broker rejected")
        self.sent.append((topic, value, key))

    async def stop(self):
        self.stopped = True


def _relay(session_factory, producer, *, topics_exist=True):
    relay = event_bus.EventBusRelay(session_factory, brokers="unused:9092")
    relay._producer = producer          # skip the real connection
    if topics_exist:
        # Mark topics as already ensured. `_ensure_topic` talks to a real
        # broker admin API; these tests are about the claim/publish/mark
        # contract, and its own behaviour is covered separately below.
        relay._ensured.update({
            event_bus.topic_for(t, "velaris") for t in ("case", "authz", "unknown")
        })
    return relay


async def _outbox_row(session, event_type="case.created", payload=None, **kw):
    row = OutboxEventModel(
        event_type=event_type,
        payload=payload if payload is not None else {"case_id": str(uuid.uuid4())},
        **kw,
    )
    session.add(row)
    await session.flush()
    return row


class TestTopicAndKey:
    async def test_topic_is_per_event_family(self):
        assert event_bus.topic_for("case.created", "velaris") == "velaris.case"
        assert event_bus.topic_for("case.data_updated", "velaris") == "velaris.case"
        assert event_bus.topic_for("authz.tuple_changed", "velaris") == "velaris.authz"

    async def test_topic_name_is_sanitised(self):
        """A publisher-influenced event type must not shape the topic name."""
        assert event_bus.topic_for("we!rd/type.x", "velaris") == "velaris.we_rd_type"

    async def test_missing_event_type_still_yields_a_topic(self):
        assert event_bus.topic_for("", "velaris") == "velaris.unknown"

    async def test_key_is_the_case_so_a_case_stays_ordered(self):
        """Same case → same key → same partition → in-order consumption."""
        cid = str(uuid.uuid4())
        assert event_bus.partition_key({"case_id": cid}) == cid.encode()

    async def test_keyless_events_do_not_all_share_one_partition(self):
        a = event_bus.partition_key({})
        b = event_bus.partition_key({})
        assert a != b     # a constant key would make one partition the bottleneck


class TestEnvelope:
    async def test_carries_outbox_id_for_deduplication(self):
        """Delivery is at-least-once; a consumer must be able to spot a replay."""
        rid = uuid.uuid4()
        body = json.loads(event_bus.envelope(
            rid, "case.created", {"case_id": "c1"}, None, datetime.now(timezone.utc),
        ))
        assert body["outbox_id"] == str(rid)
        assert body["event_type"] == "case.created"
        assert body["payload"] == {"case_id": "c1"}


class TestDrain:
    async def test_publishes_and_marks_rows(self, session, session_factory):
        row = await _outbox_row(session)
        await session.commit()

        producer = _FakeProducer()
        published = await _relay(session_factory, producer).drain_once()

        assert published == 1
        assert len(producer.sent) == 1
        await session.refresh(row)
        assert row.published_at is not None

    async def test_second_drain_does_not_republish(self, session, session_factory):
        await _outbox_row(session)
        await session.commit()

        producer = _FakeProducer()
        relay = _relay(session_factory, producer)
        assert await relay.drain_once() == 1
        assert await relay.drain_once() == 0
        assert len(producer.sent) == 1

    async def test_failed_publish_is_retried_not_lost(self, session, session_factory):
        """A failed publish keeps the row pending and backs off for the claim TTL.

        The claim is deliberately NOT released on failure: a down broker would
        otherwise be hammered every poll cycle. The row is retried once the
        claim goes stale, which is the same recovery path a crashed worker
        takes — one mechanism, not two.
        """
        row = await _outbox_row(session, event_type="case.doomed")
        await session.commit()

        failing = _FakeProducer(fail_on={"case.doomed"})
        assert await _relay(session_factory, failing).drain_once() == 0
        await session.refresh(row)
        assert row.published_at is None          # still pending, not dropped
        assert row.publish_claimed_at is not None

        # Still claimed → an immediate retry is correctly skipped.
        assert await _relay(session_factory, _FakeProducer()).drain_once() == 0

        # Once the claim goes stale it is picked up and published.
        row.publish_claimed_at = (
            datetime.now(timezone.utc) - timedelta(seconds=event_bus._CLAIM_TTL + 30)
        )
        await session.commit()
        assert await _relay(session_factory, _FakeProducer()).drain_once() == 1
        await session.refresh(row)
        assert row.published_at is not None

    async def test_publishing_does_not_touch_webhook_delivery(self, session, session_factory):
        """The two sinks share rows but not cursors — that is the whole design."""
        row = await _outbox_row(session)
        await session.commit()

        await _relay(session_factory, _FakeProducer()).drain_once()
        await session.refresh(row)
        assert row.published_at is not None
        assert row.delivered_at is None
        assert row.attempts == 0                 # webhook bookkeeping untouched

    async def test_already_delivered_rows_are_still_published(self, session, session_factory):
        """A webhook subscriber is not the only consumer — the log gets it too."""
        row = await _outbox_row(session, delivered_at=datetime.now(timezone.utc))
        await session.commit()

        assert await _relay(session_factory, _FakeProducer()).drain_once() == 1
        await session.refresh(row)
        assert row.published_at is not None

    async def test_stale_claim_is_reclaimed(self, session, session_factory):
        """A worker that died mid-publish must not park the row forever."""
        stale = datetime.now(timezone.utc) - timedelta(seconds=event_bus._CLAIM_TTL + 30)
        row = await _outbox_row(session, publish_claimed_at=stale)
        await session.commit()

        assert await _relay(session_factory, _FakeProducer()).drain_once() == 1
        await session.refresh(row)
        assert row.published_at is not None


class TestTopicCreation:
    """The broker does not auto-create topics (bundled Kafka has it switched off).

    Without this the relay produced against a missing topic, got
    UnknownTopicOrPartitionError, and retried forever against a broker that
    was working perfectly — found by publishing for real, not by unit tests.
    """

    async def test_drain_ensures_the_topic_before_publishing(self, session, session_factory):
        await _outbox_row(session)
        await session.commit()

        relay = _relay(session_factory, _FakeProducer(), topics_exist=False)
        ensured: list[str] = []

        async def fake_ensure(topic: str) -> None:
            ensured.append(topic)
            relay._ensured.add(topic)

        relay._ensure_topic = fake_ensure       # type: ignore[assignment]
        assert await relay.drain_once() == 1
        assert ensured == ["velaris.case"]

    async def test_ensure_is_cached_per_topic(self, session, session_factory):
        for _ in range(3):
            await _outbox_row(session)
        await session.commit()

        relay = _relay(session_factory, _FakeProducer(), topics_exist=False)
        calls: list[str] = []

        async def fake_ensure(topic: str) -> None:
            if topic in relay._ensured:
                return
            calls.append(topic)
            relay._ensured.add(topic)

        relay._ensure_topic = fake_ensure       # type: ignore[assignment]
        await relay.drain_once()
        assert calls == ["velaris.case"]        # three events, one create


class TestBacklog:
    async def test_reports_unpublished_count_and_oldest(self, session):
        before = await event_bus.backlog(session)
        await _outbox_row(session)
        await _outbox_row(session)
        await session.commit()

        after = await event_bus.backlog(session)
        assert after["unpublished"] == before["unpublished"] + 2
        assert after["oldest_unpublished_at"] is not None


class TestDisabledPosture:
    async def test_off_by_default(self):
        """The bus adds distribution, not durability — it should be opt-in."""
        from velaris_shared.config import get_settings

        assert get_settings().event_bus_enabled is False

    async def test_missing_client_library_raises_the_one_expected_error(self, monkeypatch):
        """A missing wheel disables the relay; it must not break startup."""
        import builtins

        real_import = builtins.__import__

        def _no_aiokafka(name, *a, **kw):
            if name == "aiokafka":
                raise ImportError("no aiokafka")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", _no_aiokafka)
        with pytest.raises(event_bus.EventBusUnavailable, match="aiokafka"):
            event_bus._load_producer_class()

    async def test_backlog_endpoint_says_whether_it_is_on(self, client):
        """Zero backlog must never be mistaken for 'working' when it is off."""
        resp = await client.get("/api/v1/hxstream/bus/backlog")
        assert resp.status_code == 200, resp.text
        assert resp.json()["enabled"] is False


class TestBrokerAuthentication:
    """The bundled broker requires SASL/PLAIN (November 2026). Without a
    password the relay must connect exactly as before (an operator's own broker
    may run without authentication); with one, every client it opens — the
    producer AND the topic admin — must carry it."""

    async def test_no_password_means_no_sasl(self):
        assert event_bus._auth_kwargs("velaris", "") == {}
        assert event_bus._auth_kwargs("", "") == {}

    async def test_password_turns_on_sasl_plain(self):
        kw = event_bus._auth_kwargs("velaris", "s3cret")
        assert kw == {
            "security_protocol": "SASL_PLAINTEXT",
            "sasl_mechanism": "PLAIN",
            "sasl_plain_username": "velaris",
            "sasl_plain_password": "s3cret",
        }

    async def test_empty_username_falls_back_to_the_default_user(self):
        assert event_bus._auth_kwargs("", "s3cret")["sasl_plain_username"] == "velaris"

    async def test_producer_is_built_with_the_credentials(self, session_factory, monkeypatch):
        built = {}

        class _Recording:
            def __init__(self, **kw):
                built.update(kw)

            async def start(self):
                return None

        monkeypatch.setattr(event_bus, "_load_producer_class", lambda: _Recording)
        relay = event_bus.EventBusRelay(
            session_factory, brokers="127.0.0.1:19092", username="velaris", password="pw",
        )
        await relay._ensure_producer()
        assert built["sasl_plain_password"] == "pw"
        assert built["security_protocol"] == "SASL_PLAINTEXT"

    async def test_password_is_not_in_the_settings_repr(self, monkeypatch):
        from velaris_shared.config import Settings

        monkeypatch.setenv("VELARIS_CASE_EVENT_BUS_PASSWORD", "do-not-print-me")
        s = Settings()
        assert s.event_bus_password == "do-not-print-me"
        assert "do-not-print-me" not in repr(s)


class TestLoop:
    """A full batch is followed at once; the pause is for a drained queue (R6 2026-11-06)."""

    async def test_full_batch_is_followed_at_once(self, monkeypatch):
        relay = event_bus.EventBusRelay(None, brokers="unused:9092")
        outcomes = iter([event_bus._BATCH_SIZE, 7])
        sleeps: list[float] = []

        async def fake_drain():
            return next(outcomes)

        async def fake_sleep(s):
            sleeps.append(s)
            if len(sleeps) == 2:
                relay._running = False

        monkeypatch.setattr(relay, "drain_once", fake_drain)
        monkeypatch.setattr(event_bus.asyncio, "sleep", fake_sleep)
        relay._running = True
        await relay._run()
        assert sleeps == [0, event_bus._POLL_INTERVAL]
