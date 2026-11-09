"""What a schedule can fire, and the bounds each one runs inside.

A scheduler is a way to make something happen with **nobody watching and nobody
holding the authority for it**, so every handler here is written to the same
three rules:

1. **It cannot reach outside its schedule's tenant.** A schedule fires objects —
   a bot, a connector, a case type, a sync destination — and each is checked with
   `same_tenant` before it is touched. Without that, one tenant's schedule is a
   way to run another tenant's integrations;
2. **It cannot do more unattended than a person could do at the keyboard.** The
   bulk action sets case *variables*, priority and assignment; it cannot move a
   case's status, because a status transition is the workflow's decision and
   driving it from a cron line would route around the process the customer
   designed. This is the same line `apply_to_case` draws for bots;
3. **It is bounded.** Every action that touches a collection has a ceiling that
   is refused rather than truncated — quietly doing the first 50 of 4,000 and
   reporting success is worse than refusing to start.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import ScheduleModel
from case_service.tenancy.identity import same_tenant

log = logging.getLogger(__name__)

#: The most cases one bulk firing may touch. A schedule that would exceed it is
#: refused, not truncated.
BULK_CEILING = 500

#: The most rows of a report that get inlined into an email. Beyond this the run
#: record holds the data and the mail says so — a 40 MB body helps nobody.
REPORT_EMAIL_ROWS = 200


class ScheduleActionError(Exception):
    """A configuration that cannot run, or a run that failed."""


def _uuid(value: Any, field: str) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ScheduleActionError(f"'{field}' must be an id.") from exc


async def _require_same_tenant(session, row_tenant, schedule: ScheduleModel, what: str):
    if not await same_tenant(session, row_tenant, schedule.tenant_id):
        # 404-shaped wording on purpose: "exists but is not yours" is an answer
        # a schedule author does not need.
        raise ScheduleActionError(f"{what} not found.")


class Action(Protocol):
    KIND: str
    LABEL: str

    async def validate(self, session: AsyncSession, schedule: ScheduleModel,
                       cfg: dict) -> None: ...

    async def run(self, session: AsyncSession, schedule: ScheduleModel,
                  cfg: dict) -> dict: ...


# ── bot ───────────────────────────────────────────────────────────────────

class BotAction:
    KIND = "bot"
    LABEL = "Run a bot"

    async def _bot(self, session, schedule, cfg):
        from velaris_shared.db.models import BotDefinitionModel

        bot = await session.get(BotDefinitionModel, _uuid(cfg.get("bot_id"), "bot_id"))
        if bot is None:
            raise ScheduleActionError("Bot not found.")
        await _require_same_tenant(session, bot.tenant_id, schedule, "Bot")
        return bot

    async def validate(self, session, schedule, cfg):
        await self._bot(session, schedule, cfg)
        if not isinstance(cfg.get("input") or {}, dict):
            raise ScheduleActionError("'input' must be an object.")

    async def run(self, session, schedule, cfg):
        from case_service.bots import service as bot_service

        bot = await self._bot(session, schedule, cfg)
        # A draft bot stays unrunnable. An approved schedule is not a second way
        # through the bot approval gate.
        jobs = await bot_service.enqueue_run(
            session, bot=bot, input_data=dict(cfg.get("input") or {}),
            trigger="schedule", requested_by=f"schedule:{schedule.name}")
        return {"bot": bot.name, "jobs": [str(j.id) for j in jobs],
                "batch_id": str(jobs[0].batch_id) if jobs[0].batch_id else None}


# ── email ─────────────────────────────────────────────────────────────────

class EmailAction:
    KIND = "email"
    LABEL = "Send an email"

    async def _account(self, session, schedule, cfg):
        from velaris_shared.db.models import EmailAccountModel

        if cfg.get("account_id"):
            account = await session.get(
                EmailAccountModel, _uuid(cfg["account_id"], "account_id"))
        else:
            # The schedule's tenant's default mailbox — not whichever tenant's
            # default the database returned first (2026-09-17).
            from case_service.mail import tenancy as mail_tenancy
            account = await mail_tenancy.default_outbound(
                session, await mail_tenancy.scope_for_token(session, schedule.tenant_id))
        if account is None:
            raise ScheduleActionError("No outbound email account configured.")
        await _require_same_tenant(session, account.tenant_id, schedule, "Email account")
        return account

    async def validate(self, session, schedule, cfg):
        # The config's own shape first, the account second: "no outbound account
        # is configured" is a fact about the deployment, and reporting it in
        # place of "this recipient list is malformed" sends the author looking in
        # the wrong place.
        to = cfg.get("to") or []
        if not isinstance(to, list) or not to:
            raise ScheduleActionError("'to' must be a non-empty list of addresses.")
        if len(to) > 100:
            raise ScheduleActionError(
                "A scheduled send is capped at 100 recipients. A bulk mailing "
                "belongs in a campaign with an unsubscribe path, not a cron line.")
        if not (cfg.get("template_id") or cfg.get("subject")):
            raise ScheduleActionError("Give a subject, or a template_id.")
        await self._account(session, schedule, cfg)

    async def run(self, session, schedule, cfg):
        from case_service.mail.service import ConsentRefused, EmailService

        account = await self._account(session, schedule, cfg)
        try:
            message = await EmailService().send(
                session, case_id=None, account=account,
                to_addresses=list(cfg.get("to") or []),
                cc_addresses=list(cfg.get("cc") or []),
                template_id=(_uuid(cfg["template_id"], "template_id")
                             if cfg.get("template_id") else None),
                subject=cfg.get("subject"),
                body_text=cfg.get("body_text"), body_html=cfg.get("body_html"),
                ctx=dict(cfg.get("ctx") or {}),
                # The consent gate is the mailer's, unchanged: a scheduled
                # marketing send is filtered exactly as an interactive one is.
                purpose=cfg.get("purpose"),
                tenant_id=schedule.tenant_id,
            )
        except ConsentRefused as exc:
            return {"sent": False, "refused": "consent", "purpose": exc.purpose,
                    "dropped": len(exc.dropped)}
        return {"sent": True, "message_id": str(getattr(message, "id", "")) or None,
                "to": list(cfg.get("to") or [])}


# ── case ──────────────────────────────────────────────────────────────────

class CaseAction:
    KIND = "case"
    LABEL = "Open a case"

    async def _case_type(self, session, schedule, cfg):
        from velaris_shared.db.models import CaseTypeModel

        row = await session.get(
            CaseTypeModel, _uuid(cfg.get("case_type_id"), "case_type_id"))
        if row is None:
            raise ScheduleActionError("Case type not found.")
        await _require_same_tenant(session, row.tenant_id, schedule, "Case type")
        return row

    async def validate(self, session, schedule, cfg):
        await self._case_type(session, schedule, cfg)
        if not isinstance(cfg.get("data") or {}, dict):
            raise ScheduleActionError("'data' must be an object.")

    async def run(self, session, schedule, cfg):
        from velaris_shared.db import repository as repo

        case_type = await self._case_type(session, schedule, cfg)
        tenant_id = (str(case_type.tenant_id) if case_type.tenant_id is not None
                     else None)
        if tenant_id is None and getattr(schedule, "tenant_id", None):
            # A shared case type made the scheduled case tenantless — readable
            # by every tenant — until 2026-09-30. The schedule knows its owner.
            from velaris_shared.tenancy.identity import resolve_tenant
            resolved = await resolve_tenant(session, str(schedule.tenant_id))
            if resolved is not None:
                tenant_id = str(resolved[0])
        from case_service.rollouts import service as _rollouts
        case_type, _rollout = await _rollouts.resolve_case_type(
            session, case_type=case_type, tenant=tenant_id,
            subject=_rollouts.subject_of((cfg.get("data") or {}).get("customer_id")),
            data=dict(cfg.get("data") or {}))
        case = await repo.create_case_instance(session, data={
            "case_type_id": case_type.id,
            "case_type_version": case_type.version,
            "status": "new",
            "priority": cfg.get("priority") or case_type.default_priority,
            "data": dict(cfg.get("data") or {}),
            # Attributed, not anonymous: "who opened this case at 2am?" has to be
            # answerable, and a NULL there reads as a person nobody recorded.
            "created_by": f"schedule:{schedule.name}"[:255],
            "tenant_id": tenant_id,
        })
        await _rollouts.record(session, case_id=case.id, res=_rollout, tenant=tenant_id,
                               via="schedule")

        # SLAs are the synchronous path's job and must start with the case,
        # scheduled or not — a case with no clock is the failure people notice a
        # week later.
        try:
            from case_service.core import case_lifecycle

            await case_lifecycle.on_case_created(
                session, case_id=case.id,
                case_type_def=case_type.definition_json or {},
                case_type_id=case.case_type_id, tenant_id=tenant_id)
        except Exception:
            log.warning("scheduled case %s: SLA start failed", case.id, exc_info=True)

        started_workflow = False
        try:
            from case_service.temporal.worker import get_client

            client = get_client()
            if client is not None:
                from case_service.temporal.workflows.case_lifecycle_workflow import (
                    CaseLifecycleWorkflow,
                )

                await client.start_workflow(
                    CaseLifecycleWorkflow.run,
                    {"case_id": str(case.id),
                     "case_type_id": str(case.case_type_id)},
                    id=f"helix-case-{case.id}", task_queue="helix-case-service")
                started_workflow = True
        except Exception:
            # Same posture as the interactive route: the case exists either way,
            # and Temporal being absent is a logged warning, never a failed run.
            log.warning("scheduled case %s: workflow not started", case.id,
                        exc_info=True)

        return {"case_id": str(case.id), "case_type": case_type.name,
                "workflow_started": started_workflow}


# ── connector ─────────────────────────────────────────────────────────────

class ConnectorAction:
    KIND = "connector"
    LABEL = "Call a connector"

    async def _connector(self, session, schedule, cfg):
        from velaris_shared.db.models import ConnectorRegistryModel

        row = await session.get(
            ConnectorRegistryModel, _uuid(cfg.get("connector_id"), "connector_id"))
        # Its own tenant's, or — for the default tenant alone — the operator's
        # (2026-11-04: a tenant-less connector passed for every tenant).
        from velaris_shared.hxbridge.security import usable_by
        if row is None or not await usable_by(session, row, schedule.tenant_id):
            raise ScheduleActionError("Connector not found.")
        return row

    async def validate(self, session, schedule, cfg):
        row = await self._connector(session, schedule, cfg)
        if not row.enabled:
            raise ScheduleActionError(
                f"Connector '{row.name}' is disabled. Enable it, or point the "
                f"schedule somewhere else — a schedule that fires a disabled "
                f"connector fails silently every night.")
        if not isinstance(cfg.get("input") or {}, dict):
            raise ScheduleActionError("'input' must be an object.")
        op = cfg.get("operation")
        if op is not None:
            from velaris_shared.hxbridge.operations import OperationError, validate_operations
            try:
                keys = {o["key"] for o in validate_operations(row.connector_type, row.config)}
            except OperationError as exc:
                raise ScheduleActionError(str(exc))
            if not isinstance(op, str) or op not in keys:
                raise ScheduleActionError(
                    f"Connector '{row.name}' has no operation {op!r}"
                    + (f" — it has: {', '.join(sorted(keys))}." if keys else "."))

    async def run(self, session, schedule, cfg):
        from case_service.hxbridge.executor import execute_connector

        connector = await self._connector(session, schedule, cfg)
        if not connector.enabled:
            raise ScheduleActionError(f"Connector '{connector.name}' is disabled.")
        op = cfg.get("operation") if isinstance(cfg.get("operation"), str) else None
        response = await execute_connector(
            session, connector, dict(cfg.get("input") or {}), operation=op)
        return {"connector": connector.name,
                "status": (response or {}).get("status"),
                "ok": bool(response)}


# ── case_bulk ─────────────────────────────────────────────────────────────

class CaseBulkAction:
    KIND = "case_bulk"
    LABEL = "Act on matching cases"

    #: What an unattended run may change.
    #:
    #: `status` is deliberately absent: a transition is the workflow's decision,
    #: and driving it from a cron line routes around the process the customer
    #: designed. **Assignment is absent too, and for a duller reason** — an
    #: assignment is a row in `case_assignments` with a non-null `step_id`, so
    #: "assign these cases to Sam" has no meaning without naming the step. A
    #: bulk action that guessed one would write assignments nobody can complete.
    APPLICABLE = ("set_variables", "priority")

    async def validate(self, session, schedule, cfg):
        apply = cfg.get("apply") or {}
        if not isinstance(apply, dict) or not apply:
            raise ScheduleActionError(
                "'apply' must say what to change: "
                f"{', '.join(self.APPLICABLE)}.")
        unknown = [k for k in apply if k not in self.APPLICABLE]
        if unknown:
            raise ScheduleActionError(
                f"A scheduled bulk action cannot change {', '.join(unknown)}. A "
                f"status transition belongs to the workflow; an assignment needs "
                f"a step, so it belongs to the assignment API. Allowed: "
                f"{', '.join(self.APPLICABLE)}.")
        if not isinstance(apply.get("set_variables", {}), dict):
            raise ScheduleActionError("'set_variables' must be an object.")

        limit = int(cfg.get("max_cases") or 100)
        if limit < 1 or limit > BULK_CEILING:
            raise ScheduleActionError(
                f"'max_cases' must be between 1 and {BULK_CEILING}.")
        if not isinstance(cfg.get("match") or {}, dict):
            raise ScheduleActionError("'match' must be an object.")

    async def _stmt(self, session, schedule, match: dict, limit: int):
        from velaris_shared.db.models import CaseInstanceModel
        from case_service.tenancy.identity import resolve_tenant
        from sqlalchemy import or_

        # A soft-deleted case is not eligible for an unattended sweep — it is
        # hidden from the operator who would have to answer for the change.
        stmt = select(CaseInstanceModel).where(
            CaseInstanceModel.is_deleted.is_(False))
        # Tenant-scoped in SQL, not only afterwards in Python. Filtering after a
        # `LIMIT` means another tenant's matching cases consume the budget and
        # this schedule silently skips its OWN cases — the query would look
        # correct and quietly do less every night.
        resolved = await resolve_tenant(session, schedule.tenant_id)
        if resolved is not None:
            stmt = stmt.where(or_(CaseInstanceModel.tenant_id.is_(None),
                                  CaseInstanceModel.tenant_id == resolved[0]))
        else:
            # An unregistered tenant string resolves to nothing, and "nothing"
            # must mean the platform-shared rows only — never every row.
            stmt = stmt.where(CaseInstanceModel.tenant_id.is_(None))
        if match.get("status"):
            stmt = stmt.where(CaseInstanceModel.status == str(match["status"]))
        if match.get("priority"):
            stmt = stmt.where(CaseInstanceModel.priority == str(match["priority"]))
        if match.get("case_type_id"):
            stmt = stmt.where(CaseInstanceModel.case_type_id
                              == _uuid(match["case_type_id"], "case_type_id"))
        if match.get("older_than_days"):
            cutoff = datetime.now(timezone.utc) - timedelta(
                days=int(match["older_than_days"]))
            stmt = stmt.where(CaseInstanceModel.created_at <= cutoff)
        return stmt.limit(limit + 1)

    async def run(self, session, schedule, cfg):
        from velaris_shared.db.models import CaseInstanceModel

        await self.validate(session, schedule, cfg)
        match = dict(cfg.get("match") or {})
        limit = int(cfg.get("max_cases") or 100)
        apply = dict(cfg.get("apply") or {})

        rows = (await session.execute(
            await self._stmt(session, schedule, match, limit))).scalars().all()

        # Checked again per row: the SQL filter above resolves the tenant once,
        # and this catches the case where it could not be resolved at all.
        mine = []
        for case in rows:
            if await same_tenant(session, case.tenant_id, schedule.tenant_id):
                mine.append(case)

        if len(mine) > limit:
            raise ScheduleActionError(
                f"This run matches more than {limit} cases. Raise 'max_cases' "
                f"deliberately, or narrow the filter — silently acting on the "
                f"first {limit} and reporting success is the worse answer.")

        touched = []
        for case in mine:
            if "set_variables" in apply:
                data = dict(case.data or {})
                for key, value in (apply["set_variables"] or {}).items():
                    if str(key).startswith("_"):
                        continue          # platform-internal keys are ours
                    data[str(key)] = value
                case.data = data
            if apply.get("priority"):
                case.priority = str(apply["priority"])
            touched.append(str(case.id))

        await session.flush()
        return {"matched": len(mine), "changed": len(touched),
                "cases": touched[:50], "applied": sorted(apply)}


# ── report ────────────────────────────────────────────────────────────────

class ReportAction:
    KIND = "report"
    LABEL = "Run a report"

    async def validate(self, session, schedule, cfg):
        query = cfg.get("query")
        if not isinstance(query, dict) or not query.get("metric"):
            raise ScheduleActionError("'query' must be an object with a 'metric'.")
        if cfg.get("format") not in (None, "json", "csv"):
            raise ScheduleActionError("'format' must be 'json' or 'csv'.")
        if cfg.get("email_to"):
            await EmailAction().validate(
                session, schedule,
                {**cfg, "to": cfg["email_to"],
                 "subject": cfg.get("subject") or f"Report: {schedule.name}"})

    async def run(self, session, schedule, cfg):
        from velaris_shared.analytics.exporter import to_csv
        from velaris_shared.analytics.query_engine import run_structured

        result = await run_structured(dict(cfg.get("query") or {}), session)
        series = result.get("series") or []
        body = (to_csv(series) if (cfg.get("format") or "csv") == "csv"
                else str(result.get("data")))

        detail: dict[str, Any] = {"title": result.get("title"),
                                  "rows": len(series), "emailed": False}
        if cfg.get("email_to"):
            truncated = len(series) > REPORT_EMAIL_ROWS
            if truncated:
                shown = to_csv(series[:REPORT_EMAIL_ROWS])
                body = (f"{shown}\n\n[{len(series)} rows in total; the first "
                        f"{REPORT_EMAIL_ROWS} are shown. The full result is on "
                        f"the schedule's run record.]")
            sent = await EmailAction().run(session, schedule, {
                **cfg, "to": cfg["email_to"],
                "subject": cfg.get("subject") or f"Report: {schedule.name}",
                "body_text": body, "body_html": None, "template_id": None,
            })
            detail.update({"emailed": sent.get("sent", False),
                           "truncated_in_email": truncated})
        detail["data"] = series[:REPORT_EMAIL_ROWS]
        return detail


# ── sync ──────────────────────────────────────────────────────────────────

class SyncAction:
    KIND = "sync"
    LABEL = "Sync the warehouse / graph"

    async def _destinations(self, session, schedule, cfg):
        from velaris_shared.db.models import SyncDestinationModel

        wanted = cfg.get("destination_ids")
        stmt = select(SyncDestinationModel).where(
            SyncDestinationModel.enabled.is_(True))
        rows = (await session.execute(stmt)).scalars().all()
        rows = [r for r in rows
                if await same_tenant(session, r.tenant_id, schedule.tenant_id)]
        if wanted:
            ids = {str(_uuid(w, "destination_ids")) for w in wanted}
            found = [r for r in rows if str(r.id) in ids]
            missing = ids - {str(r.id) for r in found}
            if missing:
                raise ScheduleActionError(
                    f"{len(missing)} sync destination(s) not found, disabled, or "
                    f"not this tenant's.")
            return found
        return rows

    async def validate(self, session, schedule, cfg):
        if not cfg.get("graph") and not cfg.get("destination_ids") \
                and not cfg.get("all_destinations"):
            raise ScheduleActionError(
                "Say what to sync: 'graph', 'destination_ids', or "
                "'all_destinations'.")
        if cfg.get("destination_ids"):
            await self._destinations(session, schedule, cfg)

    async def run(self, session, schedule, cfg):
        detail: dict[str, Any] = {"destinations": [], "graph": None}

        if cfg.get("destination_ids") or cfg.get("all_destinations"):
            from case_service.hxsync.pipeline import run_sync

            for dest in await self._destinations(session, schedule, cfg):
                try:
                    summary = await run_sync(dest.id, session)
                    detail["destinations"].append(
                        {"id": str(dest.id), "name": dest.name, "ok": True,
                         "rows": (summary or {}).get("rows_synced")})
                except Exception as exc:                      # noqa: BLE001
                    # One destination failing must not hide the others: the run
                    # is recorded per destination, not abandoned at the first.
                    detail["destinations"].append(
                        {"id": str(dest.id), "name": dest.name, "ok": False,
                         "error": str(exc)[:500]})

        if cfg.get("graph"):
            from velaris_shared.hxgraph.sync import sync_graph

            detail["graph"] = await sync_graph(session)
        return detail


# ── calendar feeds ────────────────────────────────────────────────────────

class CalendarFeedAction:
    """Re-read the external calendars this tenant subscribes to.

    A subscription is worth having only if something re-reads it, and this
    platform already has one thing that runs work on a timetable with approval,
    a tenant pin and a timeout. A second always-on poller would be a second
    thing to configure, a second thing to forget, and a second place for a
    stalled fetch to hide.
    """

    KIND = "calendar_feed"
    LABEL = "Refresh subscribed calendars"

    async def _subscriptions(self, session, schedule, cfg):
        from velaris_shared.db.models import CalendarSubscriptionModel

        rows = (await session.execute(
            select(CalendarSubscriptionModel)
            .where(CalendarSubscriptionModel.enabled.is_(True))
        )).scalars().all()
        rows = [r for r in rows
                if await same_tenant(session, r.tenant_id, schedule.tenant_id)]
        wanted = cfg.get("subscription_ids")
        if wanted:
            ids = {str(_uuid(w, "subscription_ids")) for w in wanted}
            found = [r for r in rows if str(r.id) in ids]
            missing = ids - {str(r.id) for r in found}
            if missing:
                raise ScheduleActionError(
                    f"{len(missing)} calendar subscription(s) not found, "
                    f"disabled, or not this tenant's.")
            return found
        return rows

    async def validate(self, session, schedule, cfg):
        if cfg.get("subscription_ids"):
            await self._subscriptions(session, schedule, cfg)

    async def run(self, session, schedule, cfg):
        from case_service.calendar import feeds

        detail: dict[str, Any] = {"refreshed": [], "skipped_not_due": 0}
        for row in await self._subscriptions(session, schedule, cfg):
            # `due` is what lets this be scheduled every five minutes while each
            # calendar is still only fetched at its own interval — the schedule
            # sets the granularity, the subscription sets the rate.
            if not cfg.get("force") and not feeds.due(row):
                detail["skipped_not_due"] += 1
                continue
            outcome = await feeds.refresh(session, row, actor="scheduler")
            detail["refreshed"].append(
                {"id": str(row.id), "name": row.name, **outcome})
        return detail


class AuditStreamAction:
    """Forward security events to every enabled SIEM target (market gap A3).

    A scheduler action rather than an always-on loop, for the same reason
    `calendar_feed` is one: a second background poller is a second thing to
    configure, a second thing to forget, and a second place for a stall to hide.
    It also means the forwarding interval is an operator's setting, visible on
    the schedules screen, rather than a constant in a file.

    **Platform-wide.** Targets are not tenant-scoped — `security_events` has no
    tenant column — so this runs the same work whichever tenant's schedule
    triggers it. That is stated here rather than filtered silently.
    """

    KIND = "audit_stream"
    LABEL = "Forward security events to the SIEM"

    async def validate(self, session, schedule, cfg):
        # Nothing to validate: a schedule created before the target is normal,
        # and refusing it would force the operator to do the two steps in one
        # order. The "no target yet" observation belongs in `run`, whose result
        # is RECORDED — `validate`'s return value is discarded by the caller
        # (`scheduler/service.py`), so a warning returned here reaches nobody.
        return None

    async def run(self, session, schedule, cfg):
        from case_service.audit_stream import run_due_targets

        results = await run_due_targets(session)
        if not results:
            return {"targets": [], "sent": 0, "failed": [],
                    "note": "No audit stream target is enabled, so nothing was "
                            "forwarded. Create one and enable it."}
        return {
            "targets": results,
            "sent": sum(r.get("sent", 0) for r in results),
            "failed": [r["name"] for r in results if r.get("error")],
        }


class DocumentIndexAction:
    """Drain the document index queue (migration 203).

    A scheduler action for the third time, and for the third time the same
    reason as `calendar_feed` and `audit_stream`: a second always-on loop is a
    second thing to configure, a second thing to forget, and a second place for
    a stall to hide. It also puts the interval on a screen rather than in a
    constant.

    **Nothing enqueues itself.** A row exists only because a caller asked for
    `background: true`, so a schedule that finds an empty queue is the normal
    state of an install that never asked for one.

    **Tenant-scoped by the schedule that runs it**, unlike `audit_stream`: a job
    carries the tenant of the case it came from, so a tenant's schedule drains
    that tenant's work — plus the untenanted rows, which have always meant
    platform-shared here and would otherwise wait forever.
    """

    KIND = "document_index"
    LABEL = "Index queued documents"

    async def validate(self, session, schedule, cfg):
        for key in ("max_jobs", "budget_seconds"):
            if key in cfg and cfg[key] is not None:
                try:
                    value = float(cfg[key])
                except (TypeError, ValueError):
                    raise ScheduleActionError(f"{key} must be a number.")
                if value <= 0:
                    raise ScheduleActionError(f"{key} must be greater than zero.")

    async def run(self, session, schedule, cfg):
        from case_service.hxnexus import index_queue

        result = await index_queue.run_due_jobs(
            session,
            max_jobs=int(cfg.get("max_jobs") or index_queue.DEFAULT_MAX_JOBS),
            budget_seconds=float(
                cfg.get("budget_seconds") or index_queue.DEFAULT_BUDGET_SECONDS),
            tenant_id=schedule.tenant_id,
        )
        if not result["claimed"]:
            # Said out loud, because "0 indexed" and "nothing was waiting" look
            # identical on a schedule history screen.
            result["note"] = ("No document was waiting to be indexed. Documents "
                              "reach this queue only when indexing is requested "
                              "with background: true.")
        return result


_ACTIONS: dict[str, Any] = {
    a.KIND: a() for a in (
        BotAction, EmailAction, CaseAction, ConnectorAction,
        CaseBulkAction, ReportAction, SyncAction, CalendarFeedAction,
        AuditStreamAction, DocumentIndexAction,
    )
}


def get(kind: str):
    return _ACTIONS.get((kind or "").strip())


def kinds() -> list[str]:
    return sorted(_ACTIONS)


def catalogue() -> list[dict]:
    """What the editor offers, generated from the handlers rather than typed
    into the UI a second time."""
    return [{"kind": a.KIND, "label": a.LABEL} for a in _ACTIONS.values()]
