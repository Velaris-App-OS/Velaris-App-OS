"""case-service's background jobs — started once per platform, not once per process (R5e step 9).

Moved out of `main.py`'s lifespan so the same set is started by case-service and
by the worker service (`case_service.worker.main`). Two kinds:

* **singletons** — run under `velaris_shared.jobs.leases.run_singleton`, so one
  process runs each however many processes start them: the scheduler, the email
  poll, the sweeps, sealers and watchers. Each would do its work twice in two
  processes (fire a timed action twice, ingest a mailbox twice, notify twice).
* **parallel** — the bot runner, the outbox relay and the event-bus relay. They
  claim their rows (`FOR UPDATE SKIP LOCKED` / a conditional UPDATE), so copies
  share the work instead of repeating it; they need no lease.

The loops that keep THIS process's memory current (generations, HxGuard, role
store, HxStream relay, HxVault resync, feature flags) are not here: every
process needs its own, so they stay in each service's lifespan.

Each job keeps its own try/except, deliberately: they are unrelated, and a
failed import in one must not silently cancel the scheduling of another.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import logging

from velaris_shared.config import get_settings
from velaris_shared.jobs.leases import coroutine_job, run_singleton

logger = logging.getLogger("case_service.jobs")


class JobSet:
    """What `start_jobs` started; `stop()` stops it all and releases the leases."""

    def __init__(self) -> None:
        self.supervisors: list[asyncio.Task] = []
        self.parallel: list[tuple[str, object]] = []

    def singleton(self, job: str, start, stop=None) -> None:
        self.supervisors.append(asyncio.create_task(run_singleton(job, start, stop),
                                                    name=f"lease:{job}"))

    async def stop(self) -> None:
        for t in self.supervisors:
            t.cancel()
        for t in self.supervisors:
            try:
                await t
            except (asyncio.CancelledError, Exception):   # noqa: BLE001
                pass
        for name, obj in self.parallel:
            try:
                out = obj.stop()
                if asyncio.iscoroutine(out):
                    await out
            except Exception as exc:                      # noqa: BLE001
                logger.warning("%s: stop failed: %s", name, exc)


async def start_jobs() -> JobSet:
    js = JobSet()
    settings = get_settings()

    # >>> P25 email poll loop
    try:
        from case_service.mail.worker import get_poll_loop
        _email = get_poll_loop()
        js.singleton("email-poll", _email.start, _email.stop)
    except Exception as _e:
        logger.warning("Email poll loop unavailable: %s", _e)

    try:
        from case_service.security.credential_monitor import credential_expiry_monitor
        js.singleton("credential-expiry-monitor", coroutine_job(credential_expiry_monitor))
        logger.info("Credential expiry monitor scheduled")
    except Exception as _e:
        logger.warning("Credential expiry monitor unavailable: %s", _e)

    try:
        from case_service.hxevolve.pipeline import hxevolve_cron
        js.singleton("hxevolve", coroutine_job(hxevolve_cron))
        logger.info("HxEvolve cron scheduled")
    except Exception as _e:
        logger.warning("HxEvolve cron unavailable: %s", _e)

    # SAML IdP metadata refresh: user-service's own job now (R5e step 10), same lease.

    # >>> HxSentinel periodic signal extraction (P1)
    try:
        from case_service.sentinel.extraction import signal_extraction_cron
        js.singleton("sentinel-signal-extraction", coroutine_job(signal_extraction_cron))
        logger.info("HxSentinel signal extraction sweep scheduled")
    except Exception as _e:
        logger.warning("HxSentinel signal extraction unavailable: %s", _e)

    # >>> Group I: daily RFC-3161 anchoring of the audit chain
    try:
        if settings.audit_anchor_enabled:
            from case_service.compliance.audit_anchor import audit_anchor_loop
            js.singleton("audit-anchor", coroutine_job(audit_anchor_loop))
            logger.info("Audit anchor loop scheduled (TSA: %s)", settings.audit_tsa_url)
    except Exception as _e:
        logger.warning("Audit anchor loop unavailable: %s", _e)

    # >>> HxArbiter: seal the decision log so it is evidence, not just rows.
    # Unconditional and safe: seal_once resumes from the chain tip, so a sealer
    # that was down seals late rather than losing anything. Two sealers racing
    # on one chain tip is exactly what the lease prevents.
    try:
        from case_service.hxarbiter.chain import sealer_loop
        js.singleton("hxarbiter-sealer", coroutine_job(sealer_loop))
        logger.info("HxArbiter decision sealer scheduled")
    except Exception as _e:
        logger.warning("HxArbiter decision sealer unavailable: %s", _e)

    # >>> F-01: seal the AI decision ledger — a separate loop, separate chain.
    try:
        from case_service.ledger.chain import sealer_loop as _ledger_sealer
        js.singleton("ai-ledger-sealer", coroutine_job(_ledger_sealer))
        logger.info("AI ledger sealer scheduled")
    except Exception as _e:
        logger.warning("AI ledger sealer unavailable: %s", _e)

    # >>> F-05 / F-06 / F-08: off by default; each loop reports "OFF" and returns.
    for job, mod, fn in (("agent-scorecard", "case_service.agents.workforce", "scorecard_cron"),
                         ("ambient-capture", "case_service.capture.service", "auto_cron"),
                         ("self-healing-watch", "case_service.healing.service", "watch_cron")):
        try:
            import importlib
            js.singleton(job, coroutine_job(getattr(importlib.import_module(mod), fn)))
        except Exception as _e:
            logger.warning("%s sweep unavailable: %s", job, _e)

    # >>> Oct 18a: the content rollout sweep (VELARIS_ROLLOUT_SWEEP=off stops it).
    try:
        from case_service.rollouts.sweep import cron as _rollout_cron
        js.singleton("rollout-sweep", coroutine_job(_rollout_cron))
    except Exception as _e:
        logger.warning("Rollout sweep unavailable: %s", _e)

    # >>> 2026-11-04: website demo tenants expire and are deleted.
    try:
        from case_service.tenancy.demo_expiry import cron as _demo_expiry_cron
        js.singleton("demo-tenant-expiry", coroutine_job(_demo_expiry_cron))
    except Exception as _e:
        logger.warning("Demo-tenant expiry unavailable: %s", _e)

    # >>> R5e step 7: the commerce order sweep.
    try:
        from case_service.internal_api.sweeps import cron as _order_sweep_cron
        js.singleton("commerce-order-sweep", coroutine_job(_order_sweep_cron))
    except Exception as _e:
        logger.warning("Commerce order sweep unavailable: %s", _e)

    # >>> RPA bot runner — parallel: `lease_next` claims each job with a
    # conditional UPDATE, so two runners share the queue. Default OFF; its own
    # `start()` checks the flag and logs when disabled.
    try:
        from case_service.bots.runner import runner as _bot_runner
        _bot_runner.start()
        js.parallel.append(("bot runner", _bot_runner))
    except Exception as _e:
        logger.warning("Bot runner unavailable: %s", _e)

    # >>> Scheduler (timed actions) — ON by default; a singleton: two copies
    # would fire every due action twice.
    try:
        from case_service.scheduler.runner import loop as _scheduler_loop
        js.singleton("scheduler", _scheduler_loop.start, _scheduler_loop.stop)
    except Exception as _e:
        logger.warning("Scheduler unavailable: %s", _e)

    # >>> C1 outbox relay — parallel (FOR UPDATE SKIP LOCKED).
    try:
        from case_service.integrations.outbox_relay import OutboxRelay
        from velaris_shared.db.session import get_session_factory as _gsf
        _outbox_relay = OutboxRelay(_gsf())
        _outbox_relay.start()
        js.parallel.append(("outbox relay", _outbox_relay))
    except Exception as _e:
        logger.warning("OutboxRelay unavailable: %s", _e)

    # >>> §8.8 health sampler — the producer HxStatus needs.
    try:
        if settings.health_sampler_enabled:
            from case_service.integrations.health_sampler import HealthSampler
            from velaris_shared.db.session import get_session_factory as _gsf3
            _hs = HealthSampler(_gsf3(), interval_seconds=settings.health_sampler_interval_seconds,
                                retention_days=settings.health_sampler_retention_days)
            js.singleton("health-sampler", _hs.start, _hs.stop)
    except Exception as _e:
        logger.warning("HealthSampler unavailable: %s", _e)

    # >>> §8.10 scheduled PII scan (idempotent; never overwrites a reviewer's verdict).
    try:
        if settings.pii_scanner_enabled:
            from case_service.integrations.pii_scanner import PIIScanner
            from velaris_shared.db.session import get_session_factory as _gsf4
            _ps = PIIScanner(_gsf4(), interval_seconds=settings.pii_scanner_interval_seconds)
            js.singleton("pii-scanner", _ps.start, _ps.stop)
    except Exception as _e:
        logger.warning("PIIScanner unavailable: %s", _e)

    # >>> retention scheduler (opt-in twice — it deletes).
    try:
        if settings.retention_scheduler_enabled:
            from case_service.integrations.retention_runner import RetentionRunner
            from velaris_shared.db.session import get_session_factory as _gsf5
            _rr = RetentionRunner(_gsf5(), interval_seconds=settings.retention_scheduler_interval_seconds,
                                  apply=settings.retention_scheduler_apply)
            js.singleton("retention", _rr.start, _rr.stop)
    except Exception as _e:
        logger.warning("RetentionRunner unavailable: %s", _e)

    # >>> §2.1 durable event log (opt-in) — parallel (FOR UPDATE SKIP LOCKED).
    try:
        if settings.event_bus_enabled:
            from case_service.integrations.event_bus import EventBusRelay
            from velaris_shared.db.session import get_session_factory as _gsf2
            _eb = EventBusRelay(_gsf2(), brokers=settings.event_bus_brokers,
                                topic_prefix=settings.event_bus_topic_prefix,
                                username=settings.event_bus_username,
                                password=settings.event_bus_password,
                                tls_ca=settings.event_bus_tls_ca)
            _eb.start()
            js.parallel.append(("event bus relay", _eb))
    except Exception as _e:
        logger.warning("EventBusRelay unavailable: %s", _e)

    # >>> PUO Phase 2: platform update watcher (admin notifications).
    try:
        from case_service.integrations.platform_update_watcher import PlatformUpdateWatcher
        from velaris_shared.db.session import get_analytics_session_factory as _gasf
        _puo = PlatformUpdateWatcher(_gasf())
        js.singleton("platform-update-watcher", _puo.start, _puo.stop)
    except Exception as _e:
        logger.warning("PlatformUpdateWatcher unavailable: %s", _e)

    # >>> HxDR backup watcher (R5a step c): failure is loud.
    try:
        from case_service.compliance.dr_watch import BackupWatcher
        from velaris_shared.db.session import get_analytics_session_factory as _gasf3
        _bw = BackupWatcher(_gasf3())
        js.singleton("backup-watcher", _bw.start, _bw.stop)
    except Exception as _e:
        logger.warning("BackupWatcher unavailable: %s", _e)

    # >>> PUO Phase 3: rollout plan engine (rings, soak, supersede).
    try:
        from case_service.integrations.platform_update_plan_engine import PlatformUpdatePlanEngine
        from velaris_shared.db.session import get_analytics_session_factory as _gasf2
        _pe = PlatformUpdatePlanEngine(_gasf2())
        js.singleton("platform-update-plan-engine", _pe.start, _pe.stop)
    except Exception as _e:
        logger.warning("PlatformUpdatePlanEngine unavailable: %s", _e)

    return js
