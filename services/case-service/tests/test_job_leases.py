"""Job leases — a background job runs in one process however many start it (R5e step 9).

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
import asyncio
import pathlib
import re
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from tests.conftest import TestSessionFactory
from velaris_shared.db.models import JobLeaseModel
from velaris_shared.jobs import leases

ROOT = pathlib.Path(__file__).resolve().parents[3]


async def _row(job):
    async with TestSessionFactory() as db:
        return (await db.execute(select(JobLeaseModel).where(JobLeaseModel.job == job))).scalar_one_or_none()


async def _expire(job):
    async with TestSessionFactory() as db:
        await db.execute(update(JobLeaseModel).where(JobLeaseModel.job == job)
                         .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
        await db.commit()


# ── the lease ─────────────────────────────────────────────────────────────────

async def test_first_holder_takes_a_free_lease():
    assert await leases.acquire("t-free", holder="A", factory=TestSessionFactory) is True
    assert (await _row("t-free")).holder == "A"


async def test_a_second_holder_is_refused_while_it_is_held():
    assert await leases.acquire("t-held", holder="A", factory=TestSessionFactory)
    assert await leases.acquire("t-held", holder="B", factory=TestSessionFactory) is False
    assert (await _row("t-held")).holder == "A"


async def test_the_holder_renews_its_own():
    assert await leases.acquire("t-renew", holder="A", ttl=60, factory=TestSessionFactory)
    first = (await _row("t-renew")).expires_at
    await asyncio.sleep(0.01)
    assert await leases.acquire("t-renew", holder="A", ttl=60, factory=TestSessionFactory)
    assert (await _row("t-renew")).expires_at >= first


async def test_an_expired_lease_is_taken_over():
    assert await leases.acquire("t-exp", holder="A", factory=TestSessionFactory)
    await _expire("t-exp")
    assert await leases.acquire("t-exp", holder="B", factory=TestSessionFactory) is True
    assert (await _row("t-exp")).holder == "B"
    assert await leases.acquire("t-exp", holder="A", factory=TestSessionFactory) is False


async def test_release_frees_it_at_once_and_only_for_its_holder():
    assert await leases.acquire("t-rel", holder="A", factory=TestSessionFactory)
    await leases.release("t-rel", holder="B", factory=TestSessionFactory)      # not B's
    assert (await _row("t-rel")).holder == "A"
    await leases.release("t-rel", holder="A", factory=TestSessionFactory)
    assert await _row("t-rel") is None
    assert await leases.acquire("t-rel", holder="B", factory=TestSessionFactory) is True


async def test_an_unreachable_database_means_not_held():
    def broken():
        raise ConnectionError("no database")
    assert await leases.acquire("t-down", holder="A", factory=broken) is False


# ── the supervisor ────────────────────────────────────────────────────────────

async def test_two_supervisors_run_the_job_once_and_hand_it_over():
    started, stopped = [], []

    def make(name):
        def start():
            started.append(name)
        def stop():
            stopped.append(name)
        return start, stop

    sa, ta = make("A")
    sb, tb = make("B")
    a = asyncio.create_task(leases.run_singleton("t-two", sa, ta, ttl=1, renew=0.05,
                                                 holder="A", factory=TestSessionFactory))
    await asyncio.sleep(0.2)
    b = asyncio.create_task(leases.run_singleton("t-two", sb, tb, ttl=1, renew=0.05,
                                                 holder="B", factory=TestSessionFactory))
    await asyncio.sleep(0.5)
    assert started == ["A"]                       # B never started while A held it
    a.cancel()                                    # a clean stop releases the lease
    with pytest.raises(asyncio.CancelledError):
        await a
    assert stopped == ["A"]
    await asyncio.sleep(0.3)
    assert started == ["A", "B"]                  # B took it at once, not after the TTL
    b.cancel()
    with pytest.raises(asyncio.CancelledError):
        await b
    assert await _row("t-two") is None


async def test_a_lost_lease_stops_the_job():
    started, stopped = [], []
    s = asyncio.create_task(leases.run_singleton(
        "t-lost", lambda: started.append(1), lambda: stopped.append(1),
        ttl=5, renew=0.05, holder="A", factory=TestSessionFactory))
    await asyncio.sleep(0.2)
    assert started == [1]
    async with TestSessionFactory() as db:        # another process takes it
        await db.execute(update(JobLeaseModel).where(JobLeaseModel.job == "t-lost")
                         .values(holder="Z", expires_at=datetime.now(timezone.utc) + timedelta(seconds=60)))
        await db.commit()
    await asyncio.sleep(0.3)
    assert stopped == [1]
    s.cancel()
    with pytest.raises(asyncio.CancelledError):
        await s
    assert (await _row("t-lost")).holder == "Z"   # never releases someone else's


async def test_a_coroutine_job_is_cancelled_with_its_lease():
    ran = asyncio.Event()

    async def cron():
        ran.set()
        await asyncio.sleep(3600)

    s = asyncio.create_task(leases.run_singleton("t-coro", leases.coroutine_job(cron),
                                                 ttl=5, renew=0.05, holder="A",
                                                 factory=TestSessionFactory))
    await asyncio.wait_for(ran.wait(), 2)
    s.cancel()
    with pytest.raises(asyncio.CancelledError):
        await s
    assert await _row("t-coro") is None


async def test_a_running_coroutine_job_keeps_renewing_its_lease():
    """Found live: `start()` returns the job's Task, and awaiting it parked the
    supervisor, so the lease lapsed and a second process ran the job too."""
    async def cron():
        await asyncio.sleep(3600)

    s = asyncio.create_task(leases.run_singleton("t-renewing", leases.coroutine_job(cron),
                                                 ttl=5, renew=0.05, holder="A",
                                                 factory=TestSessionFactory))
    await asyncio.sleep(0.2)
    first = (await _row("t-renewing")).renewed_at
    await asyncio.sleep(0.3)
    assert (await _row("t-renewing")).renewed_at > first
    s.cancel()
    with pytest.raises(asyncio.CancelledError):
        await s


# ── what runs under a lease ───────────────────────────────────────────────────

SINGLETONS = {
    "email-poll", "credential-expiry-monitor", "hxevolve",
    "sentinel-signal-extraction", "audit-anchor", "demo-tenant-expiry", "hxarbiter-sealer", "ai-ledger-sealer",
    "agent-scorecard", "ambient-capture", "self-healing-watch", "rollout-sweep",
    "commerce-order-sweep", "scheduler", "health-sampler", "pii-scanner", "retention",
    "platform-update-watcher", "backup-watcher", "platform-update-plan-engine",
}


def test_every_singleton_job_runs_under_a_lease():
    src = (ROOT / "services/case-service/case_service/jobs.py").read_text()
    named = set(re.findall(r'js\.singleton\("([a-z-]+)"', src))
    named |= set(re.findall(r'\("([a-z-]+)", "case_service\.[a-z_.]+", "[a-z_]+"\)', src))
    assert named == SINGLETONS


def test_saml_metadata_is_user_services_job_under_the_same_lease():
    """R5e step 10: the identity code moved, its job with it — same lease name, so
    an install mid-update (old case-service, new user-service) runs it once."""
    src = (ROOT / "services/user-service/user_service/main.py").read_text()
    assert 'run_singleton("saml-metadata", coroutine_job(saml_metadata_cron))' in src
    assert "saml" not in (ROOT / "services/case-service/case_service/jobs.py").read_text().replace(
        "SAML IdP metadata refresh: user-service's", "")


def test_main_starts_no_singleton_job_itself():
    src = (ROOT / "services/case-service/case_service/main.py").read_text()
    for needle in ("credential_expiry_monitor()", "hxevolve_cron()", "saml_metadata_cron()",
                   "signal_extraction_cron()", "audit_anchor_loop()", "sealer_loop()",
                   "_scheduler_loop.start()", "get_poll_loop()", "BackupWatcher(",
                   "PlatformUpdateWatcher(", "PlatformUpdatePlanEngine(", "RetentionRunner("):
        assert needle not in src, needle
    assert "if get_settings().run_jobs:" in src and "await start_jobs()" in src


def test_the_per_process_loops_stay_in_main():
    src = (ROOT / "services/case-service/case_service/main.py").read_text()
    for needle in ("release_cron()", "role_privileges_cron()", "generation_cron()",
                   "generations_cron()", "relay_from_other_processes()", "_vault_resync_loop()"):
        assert needle in src, needle


def test_run_jobs_defaults_on():
    from velaris_shared.config import Settings
    assert Settings().run_jobs is True


def test_migration_is_additive_on_both_databases():
    for d in ("postgresql", "mysql"):
        sql = (ROOT / f"migrations/{d}/221_job_leases.sql").read_text()
        assert "CREATE TABLE IF NOT EXISTS job_leases" in sql
        assert not re.search(r"\b(DROP|ALTER|DELETE|UPDATE)\b", re.sub(r"--.*", "", sql))


def test_scheduler_restarts_after_a_stop():
    """A lease taken back starts the loop again; it must not exit at once."""
    from case_service.scheduler.runner import SchedulerLoop

    async def go():
        loop = SchedulerLoop()
        loop.tick = lambda: asyncio.sleep(0)                      # type: ignore[method-assign]
        loop.start(); await asyncio.sleep(0.05); await loop.stop()
        loop.start(); await asyncio.sleep(0.05)
        alive = loop._task is not None and not loop._task.done()
        await loop.stop()
        return alive

    assert asyncio.run(go()) is True


def test_a_lost_insert_race_is_not_reported_as_an_outage():
    """MySQL and MariaDB answer the losers of a race on a new lease row with a
    deadlock (1213), not a duplicate key (seen on both, 2026-10-21)."""
    from sqlalchemy.exc import OperationalError

    class _My(Exception):
        pass

    class _Pg(Exception):
        pgcode = "40P01"

    lost = lambda orig: leases._lost_race(OperationalError("x", {}, orig))   # noqa: E731
    assert lost(_My(1213, "Deadlock found")) and lost(_My(1205, "Lock wait timeout"))
    assert lost(_Pg())
    assert not lost(_My(2003, "Can't connect to MySQL server"))
    assert not leases._lost_race(ConnectionError("no database"))


async def test_a_crashed_job_is_restarted_but_a_finished_one_is_not():
    """It holds the lease, so nobody else may run it: a crash must not leave it dead."""
    runs = {"crash": 0, "done": 0}

    async def crashes():
        runs["crash"] += 1
        raise RuntimeError("boom")

    async def finishes():
        runs["done"] += 1                       # a disabled feature: returns at once

    tasks = [asyncio.create_task(leases.run_singleton(j, leases.coroutine_job(f), ttl=5, renew=0.05,
                                                      holder="A", factory=TestSessionFactory))
             for j, f in (("t-crash", crashes), ("t-done", finishes))]
    await asyncio.sleep(0.6)
    assert runs["crash"] >= 3 and runs["done"] == 1
    assert (await _row("t-crash")).holder == "A"            # restarted under the same lease
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
