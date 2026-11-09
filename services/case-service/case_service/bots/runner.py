"""The bot runner loop — an asyncio loop in the shape of the others.

Same shape as `OutboxRelay`, `RetentionRunner` and `EmailPollLoop`, started from
`main.py` alongside them, so there is one answer to "where do background loops
live in this service".

**Default off.** This loop starts containers on the host, so it is opt-in per
deployment (`VELARIS_CASE_BOT_RUNNER_ENABLED`). A deployment without Docker
access must see a clear refusal, never a traceback.

The cycle:

    lease a queued job         (conditional UPDATE — two runners are safe)
    resolve credential leases  (per job, TTL-bounded, revoked at teardown)
    mint a single-use result token
    provision the container    (marketplace/runtime.py, unchanged posture)
    wait for the result POST, up to timeout_seconds
    stop the container         (always — including on every failure path)
    on failure: the connector path's backoff, then a visible failure

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from case_service.bots import credentials, service
from velaris_shared.db.models import BotDefinitionModel, BotJobModel

log = logging.getLogger(__name__)

#: How often to look for work when the queue was empty.
IDLE_SLEEP_SECONDS = 5.0

#: How often to check whether a running job has posted its result.
POLL_INTERVAL_SECONDS = 2.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class BotRunner:
    """Leases and executes bot jobs until stopped.

    **A pool, not a loop.** One worker at a time meant a night's queue drained
    serially — a thousand two-minute jobs is thirty-three hours that way, which
    is not a resource problem but a throughput one, and nothing said so. Each
    worker holds one container, so `bot_runner_workers` **is** this host's
    concurrency ceiling; the per-bot and per-tenant limits sit underneath it.

    Sizing is arithmetic: `concurrency = jobs x average runtime / window`, then
    multiply by the resource profile's memory. A thousand two-minute jobs across
    an eight-hour night needs about five workers; the same thousand at five
    minutes each needs eleven.
    """

    def __init__(self) -> None:
        self._workers: list[asyncio.Task] = []
        self._stopping = asyncio.Event()
        #: Leasing is serialised across this process's workers. The claim itself
        #: is already atomic, but the CAPACITY CHECK in front of it is not: two
        #: workers can both read "one slot free" before either takes it, and a
        #: `max_concurrency=1` bot would then be driving somebody's website
        #: twice. The lock costs microseconds; the container it prevents does
        #: not. Across processes, the row lock in `lease_next` does the same job.
        self._lease_lock = asyncio.Lock()

    def start(self) -> None:
        from velaris_shared.config import get_settings

        settings = get_settings()
        if not getattr(settings, "bot_runner_enabled", False):
            log.info("bot runner disabled (VELARIS_CASE_BOT_RUNNER_ENABLED)")
            return
        if self._workers:
            return
        count = max(1, int(getattr(settings, "bot_runner_workers", 2)))
        self._workers = [
            asyncio.create_task(self._worker(i), name=f"bot-runner-{i}")
            for i in range(count)
        ]
        log.info("bot runner started with %d worker(s)", count)

    async def stop(self) -> None:
        self._stopping.set()
        workers, self._workers = self._workers, []
        for task in workers:
            task.cancel()
        for task in workers:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _worker(self, index: int) -> None:
        while not self._stopping.is_set():
            try:
                ran = await self._cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                # One bad job must not stop this worker, let alone the others.
                log.exception("bot runner worker %d cycle failed", index)
                ran = False
            if not ran:
                await asyncio.sleep(IDLE_SLEEP_SECONDS)

    async def _loop(self) -> None:            # kept for callers that drive one pass
        await self._worker(0)

    async def _cycle(self) -> bool:
        from velaris_shared.db.session import get_session_factory

        async with get_session_factory()() as session:
            # Serialised: see `_lease_lock`. Execution below is NOT — that is the
            # whole point of the pool.
            async with self._lease_lock:
                job = await service.lease_next(session)
                if job is None:
                    await session.commit()
                    return False
                # Committed inside the lock so the next worker's capacity check
                # sees this claim rather than racing it.
                await session.commit()
                job_id = job.id

            bot = await session.get(BotDefinitionModel, job.bot_id)
            if bot is None or bot.status != "active":
                await service.fail(
                    session, job,
                    error="The bot definition is missing or no longer active.")
                await session.commit()
                return True

            # No limit check here. It used to live at this point, *after* the
            # lease, and counted the job it had just leased — so at the default
            # `max_concurrency=1` every job was declared over the limit and put
            # straight back, forever. The ceilings (per definition and per
            # tenant) are now checked inside `lease_next`, before the claim, so a
            # full bot costs a job neither a lease nor an attempt.
            await session.commit()

        await self._execute(job_id)
        return True

    async def _execute(self, job_id) -> None:
        from velaris_shared.db.session import get_session_factory

        container_id: str | None = None
        secrets_used: list[str] = []
        #: The container's egress identity, if it was granted any domains. Held
        #: here so that EVERY exit — success, failure, timeout, crash — can cut
        #: it: an allowlist entry that outlives its container is a standing
        #: permission to reach a supplier's site with nobody watching.
        egress_user: str | None = None

        async with get_session_factory()() as session:
            job = await session.get(BotJobModel, job_id)
            bot = await session.get(BotDefinitionModel, job.bot_id) if job else None
            if job is None or bot is None:
                return

            try:
                env = dict(bot.declared_env or {})
                leased = await credentials.resolve_leases(
                    session, job=job, refs=list(bot.credential_refs or []),
                    ttl_seconds=bot.timeout_seconds + 60,
                )
                secrets_used = list(leased.values())
                env.update(leased)

                token = await service.mint_result_token(session, job)
                env["VELARIS_JOB_URL"] = self._job_url(job.id)
                env["VELARIS_JOB_TOKEN"] = token
                env["VELARIS_JOB_ID"] = str(job.id)

                # Egress, per job. Without this the bot's `granted_domains` were
                # passed to the runtime with no credential to enforce them, and
                # `provision_app_container` refuses that combination — so **every
                # bot that named a domain failed to start**, which is the whole
                # point of an RPA bot. Fail-closed, but inert.
                egress_env, egress_user = self._mint_egress(bot, job)
                env.update(egress_env)

                job.status = "running"
                job.started_at = _utcnow()
                await service.record_event(session, job.id, "started", {})
                await session.commit()

                container_id = await asyncio.to_thread(
                    self._provision, bot, job, env, egress_env,
                )
            except credentials.CredentialError as exc:
                self._cut_egress(egress_user)
                await service.fail(session, job, error=str(exc))
                await session.commit()
                return
            except Exception as exc:
                self._cut_egress(egress_user)
                message = credentials.redact(str(exc), secrets_used)
                if type(exc).__name__ == "Layer2Error":
                    # A refusal about the deployment — egress disabled, gateway
                    # down, no docker. Retrying that three times over half an
                    # hour changes nothing and buries the reason.
                    log.error("bot job %s cannot start: %s", job_id, message)
                    await service.fail(session, job, error=message)
                else:
                    log.exception("bot job %s could not start", job_id)
                    await service.requeue_or_fail(session, job, error=message)
                await credentials.revoke_leases(session, job_id=job.id)
                await session.commit()
                return

            job.container_id = container_id
            await session.commit()

        timed_out = await self._await_result(job_id, bot.timeout_seconds)

        if container_id:
            await asyncio.to_thread(self._teardown, container_id)
        # After the container is gone, not before: cutting egress first would
        # leave a live browser making requests that suddenly fail, and the bot
        # would report a confusing error instead of its result.
        self._cut_egress(egress_user)

        async with get_session_factory()() as session:
            job = await session.get(BotJobModel, job_id)
            if job is None:
                return
            await credentials.revoke_leases(session, job_id=job.id)

            if timed_out and job.status not in service.TERMINAL_STATUSES:
                await service.record_event(session, job.id, "timeout", {})
                await service.requeue_or_fail(
                    session, job, error="The bot did not return within its timeout.")
            elif job.status == "succeeded":
                await service.apply_to_case(session, job)
            await session.commit()

    def _job_url(self, job_id) -> str:
        from velaris_shared.config import get_settings

        base = (getattr(get_settings(), "bot_result_base_url", "") or "").rstrip("/")
        return f"{base}/api/v1/bots/jobs/{job_id}/result"

    def _mint_egress(self, bot: BotDefinitionModel, job: BotJobModel) -> tuple[dict, str | None]:
        """A per-job egress identity for the domains this bot was approved for.

        Same posture as a Layer-2 grant's: an opaque credential, hash-only at
        rest, injected once, and rotated every run — the identity is the job, so
        the gateway's audit log names the run that made each request. No domains
        means no credential and no route out, which is the substrate's default.
        """
        domains = [str(d) for d in (bot.granted_domains or []) if str(d).strip()]
        if not domains:
            return {}, None

        from velaris_shared.apps import egress

        if not egress.egress_enabled():
            # Let `provision_app_container` raise the explicit refusal rather
            # than starting a container whose grant could not be enforced.
            return {}, None

        user = f"bot-{job.id}"
        raw, token_hash = egress.mint_credentials()
        # `job_id`, NOT `grant_id`: the latter keys the marketplace's capability
        # grants, so a job id there made every one of this bot's egress lines
        # unstorable — and the ingest that discovered that had already deleted
        # the log it read them from.
        egress.upsert_app(
            user, token_hash=token_hash, job_id=str(job.id),
            package_id=f"bot:{bot.name}", tenant_id=bot.tenant_id,
            domains=domains)
        return egress.proxy_env(user, raw), user

    def _cut_egress(self, user: str | None) -> None:
        """Remove the job's egress identity. Best-effort and idempotent — called
        on every exit path, including the ones that already failed."""
        if not user:
            return
        try:
            from velaris_shared.apps import egress

            egress.remove_app(user)
        except Exception:
            log.warning("could not remove egress entry %s", user, exc_info=True)

    def _provision(self, bot: BotDefinitionModel, job: BotJobModel, env: dict,
                   egress_env: dict | None = None) -> str:
        """Start the container. Runs in a thread — the docker SDK is blocking."""
        from velaris_shared.apps.runtime import provision_app_container

        profile = service.RESOURCE_PROFILES.get(
            bot.resource_profile, service.RESOURCE_PROFILES["light"])
        mem_limit = service.profile_mem_limit(bot.resource_profile)
        return provision_app_container(
            row_id=str(job.id),
            tenant_id=bot.tenant_id,
            package_id=f"bot:{bot.name}",
            image=bot.image,
            digest=bot.digest,
            granted_domains=list(bot.granted_domains or []),
            egress_env=egress_env or None,
            declared_env=env,
            declared_command=list(bot.declared_command or []) or None,
            pids_limit=profile["pids_limit"],
            tmpfs=profile["tmpfs"],
            mem_limit=mem_limit,
            labels={"velaris.bot": bot.name, "velaris.job": str(job.id)},
        )

    def _teardown(self, container_id: str) -> None:
        from velaris_shared.apps.runtime import stop_app_container

        stop_app_container(container_id)

    async def _await_result(self, job_id, timeout_seconds: int) -> bool:
        """Poll for the bot's result POST. Returns True if it timed out."""
        from velaris_shared.db.session import get_session_factory

        deadline = asyncio.get_running_loop().time() + max(1, timeout_seconds)
        while asyncio.get_running_loop().time() < deadline:
            if self._stopping.is_set():
                return False
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            async with get_session_factory()() as session:
                job = await session.get(BotJobModel, job_id)
                if job is None or job.status in service.TERMINAL_STATUSES:
                    return False
        return True


#: The process-wide instance, started from `main.py` beside the other loops.
runner = BotRunner()


async def sweep_expired_leases(session) -> int:
    """Revoke credential leases whose job has finished or whose TTL has passed.

    A lease that outlives its job is a live credential nobody is watching, and a
    crashed runner is exactly when that happens.
    """
    from velaris_shared.db.models import BotCredentialLeaseModel

    now = _utcnow()
    rows = (await session.execute(
        select(BotCredentialLeaseModel).where(
            BotCredentialLeaseModel.revoked_at.is_(None))
    )).scalars().all()

    revoked = 0
    for lease in rows:
        expires = lease.expires_at
        if expires is not None and expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        job = await session.get(BotJobModel, lease.job_id)
        if (expires is not None and expires <= now) or (
                job is None or job.status in service.TERMINAL_STATUSES):
            lease.revoked_at = now
            revoked += 1
    await session.flush()
    return revoked
