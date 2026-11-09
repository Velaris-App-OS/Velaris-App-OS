"""The four defects a security audit of the scheduler and the bot editor found.

Every one of them was reachable on a running service, and none was visible from
the happy path — the feature worked perfectly while each of these held.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from case_service.bots import service as bot_service
from velaris_shared.db.models import (
    BotDefinitionModel,
    BotJobModel,
    ScheduleModel,
    TenantModel,
)
from case_service.scheduler import service
from case_service.tenancy.identity import clear_cache

UTC = timezone.utc
DIGEST = "sha256:" + "a" * 64


@dataclass
class _User:
    tenant_id: str | None
    username: str = "someone"


async def _tenant(session, prefix: str) -> TenantModel:
    row = TenantModel(slug=f"{prefix}-{uuid.uuid4().hex[:6]}", name=prefix)
    session.add(row)
    await session.commit()
    clear_cache()
    return row


async def _bot(session, **kw) -> BotDefinitionModel:
    row = BotDefinitionModel(
        tenant_id=kw.pop("tenant_id", "default"), name=f"b-{uuid.uuid4().hex[:8]}",
        description="", image="registry.test/good", digest=DIGEST,
        declared_command=[], declared_env={}, credential_refs=[],
        input_schema={}, output_schema={}, granted_domains=[],
        status=kw.pop("status", "draft"), created_by=kw.pop("created_by", "author"),
        **kw)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def _schedule(session, **kw) -> ScheduleModel:
    row = ScheduleModel(
        tenant_id=kw.pop("tenant_id", "default"), name=f"s-{uuid.uuid4().hex[:8]}",
        description="", cron=kw.pop("cron", "0 2 * * *"), timezone="UTC",
        action_kind=kw.pop("action_kind", "sync"),
        action_config=kw.pop("action_config", {"graph": True}),
        status=kw.pop("status", "draft"), created_by=kw.pop("created_by", "author"),
        **kw)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


class TestTheReviewerCannotApproveTheirOwnEdit:
    """**Defect 1, proven live.** Approval compared the approver against
    `created_by`, and an edit never changes `created_by`. So the reviewer could
    approve a bot, repoint it at `registry.test/EVIL` with `granted_domains:
    ["*"]`, and approve their own change — the author's name was still on the
    row. One admin, two ordinary calls, any container they like on the host."""

    @pytest.mark.asyncio
    async def test_a_bot_edited_by_its_approver_needs_a_third_pair_of_eyes(self, session):
        bot = await _bot(session, created_by="alice")
        await bot_service.approve(session, bot, approver="bob")
        assert bot.status == "active"

        # Bob now rewrites what Bob approved.
        bot.status, bot.approved_by, bot.last_edited_by = "draft", None, "bob"
        bot.image, bot.granted_domains = "registry.test/EVIL", ["*"]
        await session.commit()

        with pytest.raises(bot_service.BotError, match="last changed it"):
            await bot_service.approve(session, bot, approver="bob")
        # Alice is refused too, and deliberately: she wrote the definition this
        # change sits on top of. The rule is "not the author, not the editor",
        # which in a two-person team means the reviewer does not edit — they ask
        # the author to. Exactly how code review works.
        with pytest.raises(bot_service.BotError, match="its author"):
            await bot_service.approve(session, bot, approver="alice")
        await bot_service.approve(session, bot, approver="carol")
        assert bot.status == "active" and bot.approved_by == "carol"

    @pytest.mark.asyncio
    async def test_the_same_holds_for_a_schedule(self, session):
        row = await _schedule(session, created_by="alice")
        await service.approve(session, row, approver="bob")

        row.status, row.approved_by, row.last_edited_by = "draft", None, "bob"
        await session.commit()

        with pytest.raises(service.ScheduleError, match="last changed it"):
            await service.approve(session, row, approver="bob")

    @pytest.mark.asyncio
    async def test_the_editor_records_who_edited(self, client, session):
        """The rule above is only as good as the column behind it."""
        created = (await client.post("/api/v1/scheduler", json={
            "name": f"n-{uuid.uuid4().hex[:6]}", "cron": "0 2 * * *",
            "action_kind": "sync", "action_config": {"graph": True},
        })).json()
        await client.put(f"/api/v1/scheduler/{created['id']}", json={
            "name": created["name"], "cron": "0 3 * * *",
            "action_kind": "sync", "action_config": {"graph": True},
        })
        row = await session.get(ScheduleModel, uuid.UUID(created["id"]))
        await session.refresh(row)
        assert row.last_edited_by == "test-admin"


class TestCrossTenantActPaths:
    """**Defect 2, proven live**: one tenant's admin could read, run, disable and
    **delete** another tenant's schedules — a 204 on somebody else's timetable —
    and could cancel their jobs or accept a bot's proposal onto their case."""

    @pytest.mark.asyncio
    async def test_another_tenants_schedule_is_not_loadable(self, session):
        from case_service.api.routers.scheduler import _load_or_404

        mine = await _tenant(session, "mine")
        theirs = await _tenant(session, "theirs")
        row = await _schedule(session, tenant_id=theirs.slug)

        with pytest.raises(HTTPException) as raised:
            await _load_or_404(session, _User(mine.slug), row.id)
        assert raised.value.status_code == 404

        # …and their own tenant still reaches it.
        assert await _load_or_404(session, _User(theirs.slug), row.id) is not None

    @pytest.mark.asyncio
    async def test_another_tenants_job_cannot_be_cancelled_or_accepted(self, session):
        from case_service.api.routers.bots import _job_mine_or_404

        mine = await _tenant(session, "mine")
        theirs = await _tenant(session, "theirs")
        bot = await _bot(session, tenant_id=theirs.slug, status="active")
        job = BotJobModel(tenant_id=theirs.slug, bot_id=bot.id, trigger="api",
                          input={}, artifacts=[], status="running")
        session.add(job)
        await session.commit()

        with pytest.raises(HTTPException) as raised:
            await _job_mine_or_404(session, _User(mine.slug), job)
        assert raised.value.status_code == 404

    @pytest.mark.asyncio
    async def test_a_token_with_no_tenant_is_not_a_superuser(self, session):
        """"Unset" must not read as "all tenants" — that is how a missing claim
        becomes an authorization bypass. A tenantless token is the
        platform-default tenant, which is what `same_tenant` has always said."""
        from case_service.api.routers.scheduler import _load_or_404

        theirs = await _tenant(session, "theirs")
        row = await _schedule(session, tenant_id=theirs.slug)
        with pytest.raises(HTTPException):
            await _load_or_404(session, _User(None), row.id)

    @pytest.mark.asyncio
    async def test_superadmin_crosses_deliberately(self, session):
        """Crossing a tenant boundary is a role somebody was given, not a side
        effect of an absent claim."""
        from case_service.api.routers.scheduler import _load_or_404

        @dataclass
        class _Super:
            tenant_id: str | None = None
            username: str = "root"
            roles: tuple = ("admin", "superadmin")

        theirs = await _tenant(session, "theirs")
        row = await _schedule(session, tenant_id=theirs.slug)
        assert await _load_or_404(session, _Super(), row.id) is not None


class TestAHangingActionCannotStallTheTimetable:
    """**Defect 3**: actions run one after another with no bound, so a connector
    against a supplier that holds the connection open would stall every other
    schedule for as long as it hung — silently, and with no run record to say
    so."""

    @pytest.mark.asyncio
    async def test_a_hanging_action_is_abandoned_and_recorded(self, session, monkeypatch):
        from velaris_shared.db.models import ScheduleRunModel
        from case_service.scheduler import actions
        from case_service.scheduler.runner import loop
        from sqlalchemy import select

        class _Hangs:
            KIND = "sync"
            LABEL = "hangs"

            async def validate(self, *a, **k):
                return None

            async def run(self, *a, **k):
                await asyncio.sleep(3600)

        monkeypatch.setattr(actions, "_ACTIONS", {**actions._ACTIONS, "sync": _Hangs()})
        monkeypatch.setattr(
            "velaris_shared.config.get_settings",
            lambda: type("S", (), {"scheduler_action_timeout_seconds": 5})())

        row = await _schedule(session, created_by="alice", status="active")
        row.next_run_at = datetime.now(UTC) - timedelta(seconds=5)
        await session.commit()

        await asyncio.wait_for(loop.tick(), timeout=30)

        runs = (await session.execute(
            select(ScheduleRunModel).where(ScheduleRunModel.schedule_id == row.id)
        )).scalars().all()
        assert len(runs) == 1 and runs[0].status == "error"
        assert "did not finish" in runs[0].detail["error"]
        await session.refresh(row)
        # The timetable stands: a standing instruction is not abandoned because
        # one night went wrong.
        assert row.status == "active" and row.next_run_at is not None


class TestScheduledWorkIsAttributed:
    """**Defect 4**: a case opened by a schedule had `created_by = NULL`, which
    reads as a person nobody recorded rather than as the platform."""

    @pytest.mark.asyncio
    async def test_a_scheduled_case_names_its_schedule(self, session):
        from velaris_shared.db.models import CaseInstanceModel, CaseTypeModel
        from case_service.scheduler import actions

        case_type = CaseTypeModel(name=f"ct-{uuid.uuid4().hex[:6]}", version=1,
                                  definition_json={}, tenant_id=None)
        session.add(case_type)
        await session.commit()

        row = await _schedule(session, action_kind="case", action_config={
            "case_type_id": str(case_type.id), "data": {"opened_by": "cron"}})
        detail = await actions.get("case").run(session, row, row.action_config)
        await session.commit()

        case = await session.get(CaseInstanceModel, uuid.UUID(detail["case_id"]))
        assert case.created_by == f"schedule:{row.name}"


class TestBotEgressIsActuallyWired:
    """**Defect 5**: the runner passed a bot's `granted_domains` to the runtime
    but never minted the egress credential that enforces them — and
    `provision_app_container` refuses that combination outright. So **every bot
    with an egress allowlist failed to start**, which is every bot that has a
    supplier portal to reach. Fail-closed, and inert."""

    def test_a_domain_grant_mints_a_per_job_identity(self, monkeypatch, tmp_path):
        from case_service.bots.runner import BotRunner
        from velaris_shared.apps import egress

        monkeypatch.setattr(egress, "egress_enabled", lambda: True)
        monkeypatch.setattr(egress, "_egress_dir", lambda: tmp_path)

        bot = BotDefinitionModel(
            tenant_id="default", name="b", description="", image="i",
            digest=DIGEST, declared_command=[], declared_env={},
            credential_refs=[], input_schema={}, output_schema={},
            granted_domains=["supplier.test"], status="active", created_by="a")
        job = BotJobModel(id=uuid.uuid4(), tenant_id="default", bot_id=uuid.uuid4(),
                          trigger="api", input={}, artifacts=[], status="running")

        env, user = BotRunner()._mint_egress(bot, job)
        assert user == f"bot-{job.id}"
        # Standard proxy variables — every HTTP stack honours them, so the bot
        # needs no Velaris-specific code to be confined.
        assert env["HTTPS_PROXY"].startswith("http://") and user in env["HTTPS_PROXY"]
        assert egress._read_allowlist()[user]["domains"] == ["supplier.test"]

        # …and the identity dies with the job, on every exit path.
        BotRunner()._cut_egress(user)
        assert user not in egress._read_allowlist()

    def test_no_domains_means_no_credential(self, monkeypatch, tmp_path):
        from case_service.bots.runner import BotRunner
        from velaris_shared.apps import egress

        monkeypatch.setattr(egress, "egress_enabled", lambda: True)
        monkeypatch.setattr(egress, "_egress_dir", lambda: tmp_path)

        bot = BotDefinitionModel(
            tenant_id="default", name="b", description="", image="i",
            digest=DIGEST, declared_command=[], declared_env={},
            credential_refs=[], input_schema={}, output_schema={},
            granted_domains=[], status="active", created_by="a")
        job = BotJobModel(id=uuid.uuid4(), tenant_id="default", bot_id=uuid.uuid4(),
                          trigger="api", input={}, artifacts=[], status="running")
        assert BotRunner()._mint_egress(bot, job) == ({}, None)

    def test_a_wildcard_grants_nothing_at_the_gateway(self):
        """`granted_domains: ["*"]` is refused *with credentials* by the approval
        gate — and it is not a wildcard at the enforcement layer either: the
        gateway matches an exact host or a subdomain, so "*" matches nothing at
        all rather than everything."""
        import importlib.util
        from pathlib import Path

        proxy_path = (Path(__file__).resolve().parents[4]
                      / "deploy" / "docker-compose" / "egress-gw" / "proxy.py")
        spec = importlib.util.spec_from_file_location("egress_proxy", proxy_path)
        proxy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(proxy)

        assert proxy.host_allowed("supplier.test", ["supplier.test"]) is True
        assert proxy.host_allowed("api.supplier.test", ["supplier.test"]) is True
        assert proxy.host_allowed("evil.test", ["*"]) is False
        assert proxy.host_allowed("notsupplier.test", ["supplier.test"]) is False


class TestBulkIsScopedInSql:
    """**Defect 6**: the bulk action filtered by tenant *after* the LIMIT, so
    another tenant's matching cases consumed the budget and the schedule quietly
    acted on fewer of its own cases than it should — every night, looking
    perfectly healthy."""

    @pytest.mark.asyncio
    async def test_another_tenants_cases_do_not_consume_the_budget(self, session):
        from velaris_shared.db.models import CaseInstanceModel, CaseTypeModel
        from case_service.scheduler import actions

        mine = await _tenant(session, "mine")
        theirs = await _tenant(session, "theirs")
        case_type = CaseTypeModel(name=f"ct-{uuid.uuid4().hex[:6]}", version=1,
                                  definition_json={}, tenant_id=None)
        session.add(case_type)
        await session.flush()

        # Their cases are created FIRST, so an unscoped query returns them first.
        for tenant, count in ((theirs.id, 5), (mine.id, 2)):
            for _ in range(count):
                session.add(CaseInstanceModel(
                    case_type_id=case_type.id, case_type_version=1, status="new",
                    priority="normal", data={}, tenant_id=tenant))
        await session.commit()

        row = await _schedule(session, tenant_id=mine.slug, action_kind="case_bulk",
                              action_config={
                                  "match": {"status": "new",
                                            "case_type_id": str(case_type.id)},
                                  "apply": {"set_variables": {"chased": True}},
                                  "max_cases": 3})
        detail = await actions.get("case_bulk").run(session, row, row.action_config)
        await session.commit()

        # Its own two, and neither of theirs.
        assert detail["matched"] == 2 and detail["changed"] == 2

    @pytest.mark.asyncio
    async def test_an_unresolvable_tenant_reaches_only_shared_rows(self, session):
        """"Not a tenant" must never widen to "every tenant"."""
        from velaris_shared.db.models import CaseInstanceModel, CaseTypeModel
        from case_service.scheduler import actions

        theirs = await _tenant(session, "theirs")
        case_type = CaseTypeModel(name=f"ct-{uuid.uuid4().hex[:6]}", version=1,
                                  definition_json={}, tenant_id=None)
        session.add(case_type)
        await session.flush()
        session.add(CaseInstanceModel(
            case_type_id=case_type.id, case_type_version=1, status="new",
            priority="normal", data={}, tenant_id=theirs.id))
        await session.commit()

        row = await _schedule(session, tenant_id="not-a-real-tenant",
                              action_kind="case_bulk", action_config={
                                  "match": {"case_type_id": str(case_type.id)},
                                  "apply": {"set_variables": {"x": 1}},
                                  "max_cases": 10})
        detail = await actions.get("case_bulk").run(session, row, row.action_config)
        assert detail["matched"] == 0
