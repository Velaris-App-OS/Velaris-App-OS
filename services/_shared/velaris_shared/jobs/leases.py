"""One lease per background job — so a job never runs in two processes (R5e step 9).

case-service starts its background jobs in every process, and almost none of
them guard against a second copy. Each such job now runs under `run_singleton`:
a supervisor that takes the job's row in `job_leases` (migration 221) when it is
free or expired, starts the job, renews the row while it runs, and stops the job
the moment the row is lost. However many processes run the platform — `--workers`,
Kubernetes replicas, the worker service beside case-service, two versions side
by side during an update — one of them runs each job.

Plain rows and conditional UPDATEs: a transaction-pooling PgBouncer does not
carry session advisory locks, and the same code runs on MySQL and SQLite.

**What it costs.** A process killed without stopping (SIGKILL, power) leaves its
row until it expires: another process takes the job within `ttl` seconds. A
process that stalls past `ttl` (a frozen VM) can overlap with its successor for
at most one renew interval before it notices and stops — the jobs this guards are
sweeps whose next cycle is idempotent, not a ledger that may never repeat a write.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import os
import socket
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

log = logging.getLogger(__name__)

#: Seconds a lease lasts without a renewal, and how often its holder renews it.
DEFAULT_TTL = 60
DEFAULT_RENEW = 20

#: This process, as it appears in `job_leases.holder`.
HOLDER = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"[:200]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _factory():
    from velaris_shared.db.session import get_session_factory
    return get_session_factory()


async def acquire(job: str, *, ttl: int = DEFAULT_TTL, holder: str = HOLDER,
                  factory=None) -> bool:
    """Take or renew `job`'s lease. True when this holder has it until now + ttl.

    Its own short transaction, so the answer is visible to every other process
    the moment it commits. Never raises: a database it cannot reach means "not
    held", and the caller stops the job rather than running it unguarded.
    """
    from velaris_shared.db.models import JobLeaseModel

    now = _now()
    until = now + timedelta(seconds=ttl)
    try:
        async with (factory or _factory())() as session:
            # Renew our own, or take one whose holder let it lapse.
            res = await session.execute(
                update(JobLeaseModel)
                .where(JobLeaseModel.job == job,
                       (JobLeaseModel.holder == holder) | (JobLeaseModel.expires_at < now))
                .values(holder=holder, expires_at=until, renewed_at=now)
                .execution_options(synchronize_session=False))
            if res.rowcount == 1:
                await session.commit()
                return True
            # No row yet: the first process to insert it holds it.
            exists = (await session.execute(
                select(JobLeaseModel.job).where(JobLeaseModel.job == job))).first()
            if exists is not None:
                await session.rollback()
                return False
            session.add(JobLeaseModel(job=job, holder=holder, expires_at=until,
                                      acquired_at=now, renewed_at=now))
            try:
                await session.commit()
                return True
            except IntegrityError:          # another process inserted it first
                await session.rollback()
                return False
    except Exception as exc:                # noqa: BLE001 — "not held" is the safe answer
        if _lost_race(exc):
            # Two processes inserting the same new row: MySQL and MariaDB answer
            # the losers with a deadlock, not a duplicate key (seen on both,
            # 2026-10-21). Someone else holds it — not a database fault.
            log.debug("job lease %s: another process took it first", job)
            return False
        log.warning("job lease %s: could not reach the lease table (%s) — not running it",
                    job, type(exc).__name__)
        return False


#: Deadlock / lock-wait timeout (MySQL, MariaDB) and deadlock / serialization
#: failure (PostgreSQL): the transaction lost to a concurrent one.
_RACE_CODES = {1213, 1205, "40P01", "40001"}


def _lost_race(exc: BaseException) -> bool:
    orig = getattr(exc, "orig", None)
    if orig is None:
        return False
    code = getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", None)
    if code is None and getattr(orig, "args", None):
        code = orig.args[0]
    return code in _RACE_CODES


async def release(job: str, *, holder: str = HOLDER, factory=None) -> None:
    """Give the lease up now (on a clean stop), so the next process need not wait
    for it to expire. Only ever this holder's own row."""
    from velaris_shared.db.models import JobLeaseModel

    try:
        async with (factory or _factory())() as session:
            await session.execute(
                delete(JobLeaseModel).where(JobLeaseModel.job == job,
                                            JobLeaseModel.holder == holder)
                .execution_options(synchronize_session=False))
            await session.commit()
    except Exception as exc:                # noqa: BLE001
        log.warning("job lease %s: release failed (%s) — it expires on its own", job, exc)


async def _call(fn: Callable[[], Any] | None) -> Any:
    if fn is None:
        return None
    out = fn()
    # A Task is awaitable too, but it is the running job: awaiting it here would
    # park the supervisor until the job ends, and its lease would never renew.
    if inspect.isawaitable(out) and not isinstance(out, asyncio.Future):
        out = await out
    return out


async def run_singleton(job: str, start: Callable[[], Any], stop: Callable[[], Any] | None = None,
                        *, ttl: int = DEFAULT_TTL, renew: int = DEFAULT_RENEW,
                        holder: str = HOLDER, factory=None) -> None:
    """Run `job` in this process only while this process holds its lease.

    `start()` starts it (sync or async; it may return an asyncio task, which is
    then what `stop` cancels when no `stop` is given). `stop()` stops it. Runs
    until cancelled; on cancellation stops the job and releases the lease.
    """
    running = False
    task: asyncio.Task | None = None

    async def _stop() -> None:
        nonlocal running, task
        try:
            if stop is not None:
                await _call(stop)
            elif task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):   # noqa: BLE001
                    pass
        except Exception as exc:            # noqa: BLE001
            log.warning("job %s: stop failed: %s", job, exc)
        running, task = False, None

    try:
        while True:
            held = await acquire(job, ttl=ttl, holder=holder, factory=factory)
            if held and not running:
                try:
                    out = await _call(start)
                    task = out if isinstance(out, asyncio.Task) else None
                    running = True
                    log.info("job %s: running here (%s)", job, holder)
                except Exception as exc:    # noqa: BLE001 — the next cycle retries
                    log.warning("job %s: start failed: %s", job, exc)
            elif running and not held:
                log.warning("job %s: lease lost — stopping it here", job)
                await _stop()
            elif running and task is not None and task.done():
                # Ended by itself: a disabled feature returns at once — nothing to
                # run, so keep the lease and leave it. A CRASH is different: this
                # process holds the lease, so no other process may run the job,
                # and a sealer that died here would stay dead with nothing saying
                # so. Log it and start it again next cycle (paced by `renew`).
                exc = None if task.cancelled() else task.exception()
                if exc is not None:
                    log.error("job %s: crashed (%s: %s) — restarting it",
                              job, type(exc).__name__, exc)
                    running, task = False, None
            await asyncio.sleep(renew)
    except asyncio.CancelledError:
        if running:
            await _stop()
        await release(job, holder=holder, factory=factory)
        raise


def coroutine_job(make: Callable[[], Awaitable[Any]]) -> Callable[[], asyncio.Task]:
    """`start` for a job that is one long coroutine (`async def x_cron(): while True: …`)."""
    def _start() -> asyncio.Task:
        return asyncio.create_task(make())
    return _start
