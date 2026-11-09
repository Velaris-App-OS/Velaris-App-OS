"""OutboxRelay keeps up under load (R6 soak 2026-11-06, L2).

The relay paused 5 s after every batch of 50, full or not, and read the
webhook subscriptions once per row — one relay delivered at most ~10 rows/s
and the backlog grew ~10k rows an hour. Now a full batch is followed at once,
the subscriptions are read once per batch, and delivery outcomes, retries and
crash-recovery claims are as before.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from velaris_shared.db.models import OutboxEventModel, WebhookSubscriptionModel
from case_service.integrations import outbox_relay as relay_mod
from case_service.integrations.outbox_relay import OutboxRelay

pytestmark = pytest.mark.asyncio


@pytest.fixture
def session_factory():
    """The suite's own factory — the relay opens its own sessions, as in prod."""
    from tests.conftest import TestSessionFactory

    return TestSessionFactory


async def _rows(session, n, event_type="case.created", **kw):
    rows = [OutboxEventModel(event_type=event_type,
                             payload={"case_id": str(uuid.uuid4())}, **kw)
            for _ in range(n)]
    session.add_all(rows)
    await session.commit()
    return rows


class TestLoop:
    """The pause is only for a drained queue (or after an error)."""

    async def _run_with(self, monkeypatch, outcomes):
        relay = OutboxRelay(session_factory=None)
        sleeps: list[float] = []
        seq = iter(outcomes)

        async def fake_batch():
            out = next(seq)
            if isinstance(out, Exception):
                raise out
            return out

        async def fake_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == len(outcomes):
                relay._running = False

        monkeypatch.setattr(relay, "_deliver_batch", fake_batch)
        monkeypatch.setattr(relay_mod.asyncio, "sleep", fake_sleep)
        relay._running = True
        await relay._run()
        return sleeps

    async def test_full_batch_is_followed_at_once(self, monkeypatch):
        full = relay_mod._BATCH_SIZE
        sleeps = await self._run_with(monkeypatch, [full, full, 3])
        assert sleeps == [0, 0, relay_mod._POLL_INTERVAL]

    async def test_empty_queue_still_pauses(self, monkeypatch):
        sleeps = await self._run_with(monkeypatch, [0, 0])
        assert sleeps == [relay_mod._POLL_INTERVAL] * 2

    async def test_error_still_pauses(self, monkeypatch):
        sleeps = await self._run_with(monkeypatch, [RuntimeError("db down")])
        assert sleeps == [relay_mod._POLL_INTERVAL]


class TestBatch:
    async def test_rows_without_subscribers_are_delivered(self, session, session_factory):
        rows = await _rows(session, 3)
        n = await OutboxRelay(session_factory)._deliver_batch()
        assert n == 3
        for r in rows:
            await session.refresh(r)
            assert r.delivered_at is not None and r.attempts == 1

    async def test_a_full_batch_returns_the_batch_size(self, session, session_factory):
        await _rows(session, relay_mod._BATCH_SIZE + 5)
        relay = OutboxRelay(session_factory)
        assert await relay._deliver_batch() == relay_mod._BATCH_SIZE
        assert await relay._deliver_batch() == 5
        assert await relay._deliver_batch() == 0

    async def test_subscriptions_are_read_once_per_batch(self, session, session_factory,
                                                         monkeypatch):
        await _rows(session, 7)
        calls = 0
        real = relay_mod.load_active_subscriptions

        async def counting(s):
            nonlocal calls
            calls += 1
            return await real(s)

        monkeypatch.setattr(relay_mod, "load_active_subscriptions", counting)
        assert await OutboxRelay(session_factory)._deliver_batch() == 7
        assert calls == 1

    async def test_a_claimed_row_is_left_alone(self, session, session_factory):
        now = datetime.now(timezone.utc)
        (fresh,) = await _rows(session, 1, claimed_at=now)
        (stale,) = await _rows(session, 1, claimed_at=now - timedelta(seconds=600))
        assert await OutboxRelay(session_factory)._deliver_batch() == 1
        await session.refresh(fresh)
        await session.refresh(stale)
        assert fresh.delivered_at is None
        assert stale.delivered_at is not None


class TestDelivery:
    """With a matching subscriber, success and retry are as before."""

    async def _sub(self, session, **kw):
        sub = WebhookSubscriptionModel(name="hook", url="https://hook.example/in",
                                       events=["case.created"], **kw)
        session.add(sub)
        await session.commit()
        return sub

    @pytest.fixture(autouse=True)
    def _no_dns(self, monkeypatch):
        import case_service.hxbridge.security as sec

        async def ok(url, *a, **kw):
            return url
        monkeypatch.setattr(sec, "validate_outbound_url", ok)

    async def test_delivered_on_2xx(self, session, session_factory, monkeypatch):
        sub = await self._sub(session)
        (row,) = await _rows(session, 1)
        posted: list[str] = []

        async def post(self, url, **kw):
            posted.append(url)
            return type("R", (), {"status_code": 204})()
        monkeypatch.setattr(relay_mod.httpx.AsyncClient, "post", post)

        try:
            assert await OutboxRelay(session_factory)._deliver_batch() == 1
            await session.refresh(row)
            assert posted == [sub.url]
            assert row.delivered_at is not None
        finally:
            await session.delete(sub)
            await session.commit()

    async def test_failure_is_retried_later(self, session, session_factory, monkeypatch):
        sub = await self._sub(session)
        (row,) = await _rows(session, 1)

        async def post(self, url, **kw):
            raise RuntimeError("connection refused")
        monkeypatch.setattr(relay_mod.httpx.AsyncClient, "post", post)

        try:
            assert await OutboxRelay(session_factory)._deliver_batch() == 1
            await session.refresh(row)
            assert row.delivered_at is None
            assert row.attempts == 1
            assert row.next_attempt_at is not None
            # Not due yet: the next batch does not take it.
            assert await OutboxRelay(session_factory)._deliver_batch() == 0
        finally:
            await session.delete(sub)
            await session.commit()

    async def test_other_event_types_do_not_reach_the_subscriber(
            self, session, session_factory, monkeypatch):
        sub = await self._sub(session)
        (row,) = await _rows(session, 1, event_type="case.resolved")
        posted: list[str] = []

        async def post(self, url, **kw):
            posted.append(url)
            return type("R", (), {"status_code": 200})()
        monkeypatch.setattr(relay_mod.httpx.AsyncClient, "post", post)

        try:
            assert await OutboxRelay(session_factory)._deliver_batch() == 1
            await session.refresh(row)
            assert posted == []
            assert row.delivered_at is not None
        finally:
            await session.delete(sub)
            await session.commit()
