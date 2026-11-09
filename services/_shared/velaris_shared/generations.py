"""Change counters for caches that must agree between processes (R5e step 5).

The same shape as HxGuard's authorization generation (`hxguard/generation.py`,
migration 173), for every other cache that is only correct while one process
serves the platform. One row per TOPIC in `platform_generations` (migration
219):

    flags       feature-flag versions        api/routers/releases.py
    revocation  revoked sessions             auth/dependencies.py
    vault       tenant data keys (DEKs)      hxvault/keyring.py
    tenants     tenant / actor lookups       tenancy/identity.py

A change calls `bump(session, topic)` with its own session, so the bump commits
with the change and a rolled-back change leaves nothing behind. The caller
still reloads its OWN cache at once — it should not wait for a poll to see its
own change. Every process runs `generations_cron`, which reads all topics in
one query every POLL_INTERVAL_SECONDS and runs the reloads registered with
`on_change` for each topic whose counter moved.

Works with the cache (Valkey) switched off, which is every existing install.
Fails open: if the table cannot be read the caches keep their own timers (the
behaviour before this file), and a bump that fails never fails the change.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import PlatformGenerationModel

log = logging.getLogger(__name__)

TOPICS = frozenset({"flags", "revocation", "vault", "tenants"})

#: Well inside every cache's own TTL, so this bounds staleness, not the TTLs.
POLL_INTERVAL_SECONDS = 5

Reload = Callable[[AsyncSession], Awaitable[None]]
_handlers: dict[str, list[Reload]] = {}

#: Last counter seen per topic. Empty until the first successful read, which is
#: a baseline, not a change — or every process would reload everything at boot.
_seen: dict[str, int] = {}
_read_failing = False


def on_change(topic: str, reload: Reload) -> None:
    """Run `reload(session)` in this process whenever `topic` moves elsewhere."""
    if topic not in TOPICS:
        raise ValueError(f"unknown generation topic {topic!r}")
    lst = _handlers.setdefault(topic, [])
    if reload not in lst:
        lst.append(reload)


async def bump(session: AsyncSession, topic: str, *, reason: str = "") -> None:
    """Record, in the caller's transaction, that `topic` changed. Never raises."""
    if topic not in TOPICS:
        raise ValueError(f"unknown generation topic {topic!r}")
    try:
        res = await session.execute(
            update(PlatformGenerationModel)
            .where(PlatformGenerationModel.topic == topic)
            .values(generation=PlatformGenerationModel.generation + 1, reason=reason[:100] or None)
        )
        if res.rowcount == 0:
            # Row missing (a database built without the seed). Inside a savepoint:
            # a concurrent insert must not abort the caller's transaction.
            async with session.begin_nested():
                await session.execute(insert(PlatformGenerationModel).values(
                    topic=topic, generation=1, reason=reason[:100] or None))
    except Exception as exc:  # noqa: BLE001
        # The change itself is still correct where it was made; the other
        # processes fall back to their own reload timers.
        log.warning("generation bump failed for %s (%s): %s", topic, reason, exc)


async def poll_once(session: AsyncSession) -> list[str]:
    """Read every counter; run the reloads of the topics that moved. Returns them."""
    global _read_failing
    try:
        rows = (await session.execute(
            select(PlatformGenerationModel.topic, PlatformGenerationModel.generation)
        )).all()
    except Exception as exc:  # noqa: BLE001
        if not _read_failing:
            log.warning("platform generations unreadable (caches keep their own timers): %s", exc)
        _read_failing = True
        return []
    if _read_failing:
        log.info("platform generations readable again")
    _read_failing = False
    current = {t: int(g) for t, g in rows}
    first = not _seen
    moved: list[str] = []
    for topic in TOPICS:
        now = current.get(topic, 0)
        if not first and now != _seen.get(topic, 0):
            moved.append(topic)
        _seen[topic] = now
    for topic in moved:
        for reload in list(_handlers.get(topic, ())):
            try:
                await reload(session)
            except Exception as exc:  # noqa: BLE001 — one cache must not stop the others
                log.warning("reload after %s change failed: %s", topic, exc)
        log.info("platform generation %s changed — %d cache(s) reloaded",
                 topic, len(_handlers.get(topic, ())))
    return moved


async def generations_cron() -> None:
    """Poll forever (started beside the HxGuard generation poll in every service)."""
    from velaris_shared.db.session import get_session_factory

    while True:
        try:
            async with get_session_factory()() as session:
                await poll_once(session)
        except Exception as exc:  # noqa: BLE001 — this loop must outlive a bad moment
            log.warning("generations poll failed: %s", exc)
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
