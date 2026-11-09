"""The worker pool, and the container sizing that has to hold with it.

One worker at a time made a night's queue drain serially — a thousand two-minute
jobs is thirty-three hours that way, which is a throughput failure nothing
reported. The pool fixes that, and introduces the one race a serial loop could
not have: two workers checking capacity before either takes a slot.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from case_service.bots import service
from case_service.bots.runner import BotRunner
from velaris_shared.db.models import BotDefinitionModel, BotJobModel

DIGEST = "sha256:" + "a" * 64


async def _bot(session, **kw) -> BotDefinitionModel:
    row = BotDefinitionModel(
        tenant_id=kw.pop("tenant_id", "default"), name=f"b-{uuid.uuid4().hex[:8]}",
        description="", image="registry.test/x", digest=DIGEST,
        declared_command=[], declared_env={}, credential_refs=[],
        input_schema={}, output_schema={}, granted_domains=[],
        status="active", created_by="author", **kw)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


class TestTheProfilesCannotKillThemselves:
    """A browser profile is handed 768m of tmpfs, and **tmpfs counts against the
    container's own memory cgroup** — so under the Layer-2 default of 512m a bot
    could be OOM-killed by using exactly what it was given, in a way that reads
    as a flaky website rather than as a misconfiguration."""

    @pytest.mark.parametrize("name", sorted(service.RESOURCE_PROFILES))
    def test_scratch_fits_inside_the_memory_limit(self, name):
        from velaris_shared.config import get_settings

        profile = service.RESOURCE_PROFILES[name]
        limit = service.profile_mem_limit(name) \
            or get_settings().marketplace_l2_mem_limit
        megabytes = int("".join(c for c in str(limit) if c.isdigit()))
        scratch = service.tmpfs_megabytes(profile)
        # Room to actually run in, not merely to hold its own scratch.
        assert scratch * 2 <= megabytes, (
            f"{name}: {scratch}MB of tmpfs under a {megabytes}MB memory limit")

    def test_the_browser_profile_has_its_own_ceiling(self):
        assert service.profile_mem_limit("browser") == "1536m"
        # `light` deliberately keeps the platform default rather than pinning a
        # second number that would then drift from it.
        assert service.profile_mem_limit("light") is None

    def test_provisioning_passes_the_limit_through(self, monkeypatch):
        captured = {}

        def _fake(**kwargs):
            captured.update(kwargs)
            return "container-id"

        monkeypatch.setattr(
            "velaris_shared.apps.runtime.provision_app_container", _fake)

        bot = BotDefinitionModel(
            tenant_id="default", name="b", description="", image="i",
            digest=DIGEST, declared_command=[], declared_env={},
            credential_refs=[], input_schema={}, output_schema={},
            granted_domains=[], status="active", created_by="a",
            resource_profile="browser")
        job = BotJobModel(id=uuid.uuid4(), tenant_id="default", bot_id=uuid.uuid4(),
                          trigger="api", input={}, artifacts=[], status="running")

        BotRunner()._provision(bot, job, {}, {})
        assert captured["mem_limit"] == "1536m"
        assert captured["pids_limit"] == 512


class TestThePool:
    @pytest.mark.asyncio
    async def test_workers_run_jobs_in_parallel(self, session, monkeypatch):
        """The point of the pool: two jobs in flight at once, where the serial
        loop would have run them one after the other."""
        bot = await _bot(session, max_concurrency=4)
        for _ in range(2):
            await service.enqueue(session, bot=bot, input_data={}, trigger="api")
        await session.commit()

        runner = BotRunner()
        in_flight, peak = 0, 0

        async def _execute(job_id):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.2)
            in_flight -= 1

        monkeypatch.setattr(runner, "_execute", _execute)
        await asyncio.gather(runner._cycle(), runner._cycle())
        assert peak == 2

    @pytest.mark.asyncio
    async def test_two_workers_cannot_exceed_max_concurrency(self, session, monkeypatch):
        """The race a serial loop could not have. The claim was always atomic;
        the CAPACITY CHECK in front of it was not — two workers could both read
        "one slot free" before either took it, and a `max_concurrency=1` bot
        would then be driving somebody else's website twice."""
        bot = await _bot(session, max_concurrency=1)
        for _ in range(2):
            await service.enqueue(session, bot=bot, input_data={}, trigger="api")
        await session.commit()

        runner = BotRunner()
        started: list[uuid.UUID] = []

        async def _execute(job_id):
            started.append(job_id)
            await asyncio.sleep(0.2)

        monkeypatch.setattr(runner, "_execute", _execute)
        results = await asyncio.gather(runner._cycle(), runner._cycle())

        assert started == started[:1], "a second job started past the limit"
        assert results.count(True) == 1 and results.count(False) == 1

    @pytest.mark.asyncio
    async def test_the_pool_size_is_the_host_ceiling(self, monkeypatch):
        """`bot_runner_workers` is what an operator sizes the box against, so it
        has to be the number of workers that actually start."""
        from velaris_shared import config

        real = config.get_settings()
        monkeypatch.setattr(config, "get_settings", lambda: type(
            "S", (), {"bot_runner_enabled": True, "bot_runner_workers": 3,
                      "marketplace_l2_mem_limit": real.marketplace_l2_mem_limit})())

        runner = BotRunner()
        runner.start()
        try:
            assert len(runner._workers) == 3
        finally:
            await runner.stop()
        assert runner._workers == []
