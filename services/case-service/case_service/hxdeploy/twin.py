"""F-04 second slice — the digital twin.

Design: docs/Future/Done/AI-ERA-ROADMAP.md §F-04. Migration 186 built the gate and
was explicit that the twin was NOT built; every run it produced said
`synthetic_load: false`. This module is the other half.

**What the twin is, concretely.** Not a mode of this process — a *place*:

  · **A database of its own** (`VELARIS_CASE_TWIN_DATABASE_URL`). This is the
    isolation that matters and the one this module can guarantee, because every
    write a rehearsal makes goes through a session bound to it.
  · **A task queue of its own** (`VELARIS_CASE_TWIN_TASK_QUEUE`), needed only
    for case types that declare `orchestration: temporal`. Those must be driven
    by a real worker or not at all — driving a Temporal-orchestrated case type
    inline would be rehearsing a *different execution model* from the one the
    promotion is about, which is worse than not rehearsing it.

**Nothing here starts a worker.** A platform that spawned processes to rehearse
a promotion would have a blast radius equal to a bug in its own process
management, and it could not be made safe on an install that takes an update
unattended. The operator runs a second case-service worker; this module finds
out whether one is there, and says so plainly when it is not.

**The isolation check is the point of the whole module.** "The twin was actually
production" is the one failure here that is both catastrophic and silent:
synthetic cases written into real data, and a promotion cleared by a rehearsal
that damaged the thing it was rehearsing. It is checked on **(host, port,
database)** and on the queue name — never on the whole connection string, since
two DSNs differing only by password point at the same database.

**It orchestrates; it does not re-implement.** The candidate is provisioned
through `packager.apply_bundle` — the same import a real promotion runs — and
cases advance through `_auto_advance_if_complete`, the same function the
synchronous API path calls. A twin with its own idea of either would eventually
disagree with production about what the candidate does, which is the one thing
a rehearsal may never do.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import hashlib
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from velaris_shared.config import get_settings
from velaris_shared.db.models import (
    CaseInstanceModel,
    CaseStepCompletionModel,
    CaseTypeModel,
    TwinRunModel,
)

log = logging.getLogger(__name__)

#: Written into every synthetic case so teardown can find exactly what this
#: rehearsal made and nothing else. A twin database swept by "delete recent
#: cases" would eventually delete somebody's deliberate fixture.
TWIN_MARKER = "__twin_run__"

#: What a rehearsal is, in one word, on every row. `batch` is a bounded set of
#: synthetic cases driven through the candidate; it is NOT a load profile with
#: ramped concurrency and think time, and the column exists so that the day one
#: is built an old row still says which question it answered.
LOAD_PROFILE = "batch"

REFUSE_NO_TWIN = "no_twin_configured"
REFUSE_NOT_ISOLATED = "twin_is_not_isolated"
REFUSE_UNREACHABLE = "twin_unreachable"
REFUSE_NOTHING_TO_RUN = "no_case_types_in_package"

_twin_engine = None
_twin_sessions: async_sessionmaker[AsyncSession] | None = None


class TwinError(RuntimeError):
    """The twin cannot be used as asked. The message is for an operator."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── configuration and isolation ──────────────────────────────────────────────


def _async_url(raw: str) -> str:
    """Accept a plain DSN and drive it asynchronously.

    An operator setting a twin URL should not have to know which async driver
    this platform happens to use, and a sync driver here fails at connect time
    with an error that says nothing about the real mistake.
    """
    url = make_url(raw)
    if url.drivername in ("postgresql", "postgres"):
        url = url.set(drivername="postgresql+asyncpg")
    elif url.drivername == "mysql":
        url = url.set(drivername="mysql+aiomysql")
    elif url.drivername == "sqlite":
        url = url.set(drivername="sqlite+aiosqlite")
    return url.render_as_string(hide_password=False)


def _identity(raw: str) -> tuple[str, int | None, str | None]:
    """The three things that decide whether two DSNs are the same database.

    Deliberately NOT the username or the password: a second account on the same
    database is the same database, and treating it as a twin is exactly the
    mistake this exists to catch.
    """
    url = make_url(_async_url(raw))
    # A UNIX socket is named in the query (`?host=/dir&port=6432`): that folder
    # and port ARE the address — read as one, or a twin given production's
    # socket would pass as another database.
    q = {k: (v[-1] if isinstance(v, tuple) else v) for k, v in url.query.items()}
    host = url.host or q.get("host") or "localhost"
    port = url.port or (int(q["port"]) if str(q.get("port") or "").isdigit() else None)
    return (str(host), port, url.database)


def fingerprint(raw: str) -> str:
    """A name for a database that carries no secret.

    The row this lands on is readable by anybody who may read a simulation, and
    a connection string carries a password.
    """
    host, port, db = _identity(raw)
    return hashlib.sha256(f"{host}:{port or ''}/{db or ''}".encode()).hexdigest()


def configured() -> bool:
    return bool((get_settings().twin_database_url or "").strip())


def isolation(settings: Any = None) -> tuple[bool, dict[str, Any]]:
    """`(ok, detail)` — is the configured twin genuinely somewhere else?

    Both halves are checked, and the database half is fatal on its own: a twin
    sharing production's database is not a twin whatever its queue is called.
    """
    s = settings or get_settings()
    twin_url = (s.twin_database_url or "").strip()
    detail: dict[str, Any] = {"checked": ["database", "task_queue"]}
    if not twin_url:
        return False, {**detail, "reason": "no twin database configured"}

    # Production is reachable at several addresses once the pooler is on (R5d 6a):
    # direct, through PgBouncer, and through commerce's, portal's and user's own
    # pools on it (R5e steps 7, 8, 10 — more database names for the same database). A twin at any of
    # them is production all the same, and must be refused like one.
    pooler_urls = [(getattr(s, k, "") or "").strip()
                   for k in ("database_pooler_url", "commerce_database_pooler_url",
                             "portal_database_pooler_url", "user_database_pooler_url")]
    try:
        prod = {_identity(s.database_url)}
        prod.update(_identity(u) for u in pooler_urls if u)
        # …and through the pooler's UNIX socket (VELARIS_CASE_DATABASE_POOLER_SOCKET),
        # whether or not this install uses it: the folder is in every release.
        from velaris_shared.db.backends import pooler_socket_dir
        prod.update((pooler_socket_dir(), 6432, _identity(u)[2]) for u in pooler_urls if u)
        twin = _identity(twin_url)
    except Exception as exc:                                     # noqa: BLE001
        return False, {**detail, "reason": f"a twin URL that cannot be parsed is not a twin: {exc}"}

    same_db = twin in prod
    detail["database"] = {
        "production_fingerprint": fingerprint(s.database_url),
        "twin_fingerprint": fingerprint(twin_url),
        "same": same_db,
    }
    if same_db:
        return False, {
            **detail,
            "reason": (
                "the twin database is production — same host, port and database "
                "name (directly or through the connection pooler). A rehearsal there would write synthetic cases into real "
                "data and clear the promotion it damaged."),
        }

    twin_queue = (s.twin_task_queue or "").strip()
    prod_queues = {str(s.temporal_task_queue or "").strip(), "helix-case-service"}
    detail["task_queue"] = {"twin": twin_queue or None,
                            "production": sorted(q for q in prod_queues if q),
                            "same": bool(twin_queue) and twin_queue in prod_queues}
    if twin_queue and twin_queue in prod_queues:
        return False, {
            **detail,
            "reason": (
                f"the twin task queue {twin_queue!r} is one production workers "
                f"poll, so rehearsal work would be executed by production."),
        }
    return True, {**detail, "reason": "twin is a separate database"
                            + (" and a separate task queue" if twin_queue
                               else "; no twin queue configured, so "
                                    "Temporal-orchestrated case types are not run")}


def twin_sessions() -> async_sessionmaker[AsyncSession]:
    """A session factory bound to the twin database.

    Its own engine and its own pool, never the production one — sharing a pool
    would mean a rehearsal could starve the API that requested it.
    """
    global _twin_engine, _twin_sessions                          # noqa: PLW0603
    if _twin_sessions is None:
        s = get_settings()
        if not configured():
            raise TwinError("no twin database is configured")
        ok, detail = isolation(s)
        if not ok:
            raise TwinError(detail.get("reason") or "the twin is not isolated")
        _twin_engine = create_async_engine(
            _async_url(s.twin_database_url), echo=False,
            pool_size=3, max_overflow=2, pool_pre_ping=True)
        _twin_sessions = async_sessionmaker(
            _twin_engine, class_=AsyncSession, expire_on_commit=False)
    return _twin_sessions


_schema_ready = False


async def ensure_schema() -> None:
    """Create the twin's tables from the ORM metadata, once per process.

    **Not from the migration files, deliberately.** A twin is a throwaway
    environment created fresh, so what it must match is *this build's models* —
    and replaying the migration history to get there would be both slow and
    wrong: measured on this platform on 2026-09-04, running the whole
    `migrations/postgresql` set into an empty database produces a schema
    **missing 92 tables and 586 columns** that the running install has, so a
    twin built that way fails on the first query against a column the models
    know about. (That gap is a real platform issue and is recorded as one; it is
    not something a rehearsal environment should have to work around.)

    `create_all` only adds what is missing, and the isolation check is asserted
    again here rather than trusted from two functions away — creating tables in
    production would be harmless, but the habit of re-checking at the point of
    the write is what keeps this safe when somebody edits `twin_sessions`.
    """
    global _schema_ready                                          # noqa: PLW0603
    if _schema_ready:
        return
    ok, detail = isolation()
    if not ok:
        raise TwinError(detail.get("reason") or "the twin is not isolated")
    from velaris_shared.db.models import Base

    twin_sessions()                       # builds the engine, re-checks isolation
    async with _twin_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    _schema_ready = True


def reset_twin_engine() -> None:
    """Drop the cached engine. Tests, and a settings change at runtime."""
    global _twin_engine, _twin_sessions, _schema_ready           # noqa: PLW0603
    _twin_engine = None
    _twin_sessions = None
    _schema_ready = False


# ── is a twin worker there? ──────────────────────────────────────────────────


async def worker_pollers(client: Any, queue: str) -> int | None:
    """How many workers poll the twin queue. `None` when we could not ask.

    `None` is not zero. "Temporal did not answer" and "nobody is listening" lead
    to the same refusal but read very differently to whoever has to fix it.
    """
    if client is None or not queue:
        return None
    try:
        desc = await client.workflow_service.describe_task_queue(
            {"namespace": getattr(client, "namespace", "default"),
             "task_queue": {"name": queue}})
        return len(getattr(desc, "pollers", []) or [])
    except Exception as exc:                                     # noqa: BLE001
        log.info("twin: could not describe task queue %s: %s", queue, exc)
        return None


# ── running a rehearsal ──────────────────────────────────────────────────────


def _ordered_stages(definition: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted((definition or {}).get("stages", []) or [],
                  key=lambda st: st.get("order", 0))


def _required_steps(stage: dict[str, Any]) -> list[str]:
    """The steps a person would have to complete. Automated ones are excluded
    for the same reason `_auto_advance_if_complete` excludes them."""
    return [st["id"] for st in (stage.get("steps") or [])
            if st.get("required", True) and st.get("step_type") != "automated"]


def _pct(values: list[int], q: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return int(ordered[idx])


async def _drive_case(session: AsyncSession, *, case: CaseInstanceModel,
                      definition: dict[str, Any]) -> dict[str, Any]:
    """Walk one synthetic case through the candidate definition.

    Advancement goes through `_auto_advance_if_complete` — the function the
    synchronous API path calls — so what this measures is the candidate's real
    behaviour and not a second reading of its stage list.
    """
    from case_service.api.routers.cases import _auto_advance_if_complete

    stages = _ordered_stages(definition)
    if not stages:
        return {"outcome": "failed", "reason": "the candidate defines no stages"}
    last_stage_id = stages[-1]["id"]
    visited: list[str] = []
    started = time.perf_counter()

    # Bounded by the stage count: a definition that advanced in a cycle would
    # otherwise spin here, and a rehearsal that hangs blocks the promotion it
    # exists to inform.
    for _ in range(len(stages) + 1):
        current = case.current_stage_id or stages[0]["id"]
        visited.append(current)
        if current == last_stage_id:
            break
        stage = next((st for st in stages if st["id"] == current), None)
        if stage is None:
            return {"outcome": "failed", "visited": visited,
                    "reason": f"the case reached stage {current!r}, which the "
                              f"candidate does not define"}
        required = _required_steps(stage)
        for step_id in required:
            session.add(CaseStepCompletionModel(
                id=uuid.uuid4(), case_id=case.id, stage_id=current,
                step_id=step_id, status="completed", completed_by="twin"))
        await session.flush()
        new_stage, held = await _auto_advance_if_complete(
            session, case.id, current, definition)
        if new_stage is None:
            elapsed = int((time.perf_counter() - started) * 1000)
            # Three different problems, and an author fixes each one
            # differently. "It stalled" alone sends them looking in the wrong
            # place — the commonest cause by far is a stage with nothing on it
            # a person could ever complete, which reads as a workflow bug and
            # is really a step-type mistake.
            if held:
                why = "an open risk hold stopped the advance"
            elif not required:
                why = (f"stage {current!r} has no required step a person can "
                       f"complete — every step on it is optional or automated, "
                       f"so nothing will ever advance it")
            else:
                why = (f"stage {current!r} did not advance with all "
                       f"{len(required)} of its required steps complete")
            return {"outcome": "stuck", "visited": visited, "ms": elapsed,
                    "stage": current, "reason": why}
        await session.refresh(case)

    return {"outcome": "terminal", "visited": visited,
            "ms": int((time.perf_counter() - started) * 1000)}


async def _synthetic_case(session: AsyncSession, *, case_type: CaseTypeModel,
                          tenant_uuid: uuid.UUID, run_id: uuid.UUID,
                          index: int) -> CaseInstanceModel:
    stages = _ordered_stages(case_type.definition_json or {})
    case = CaseInstanceModel(
        id=uuid.uuid4(), case_type_id=case_type.id,
        case_type_version=case_type.version, status="new", priority="medium",
        current_stage_id=stages[0]["id"] if stages else None,
        tenant_id=tenant_uuid,
        # The marker is how teardown finds exactly what this rehearsal made.
        data={TWIN_MARKER: str(run_id), "synthetic_index": index})
    session.add(case)
    await session.flush()
    return case


async def _ensure_tenant(session: AsyncSession, tenant_uuid: uuid.UUID) -> None:
    """The twin needs the tenant row a synthetic case points at.

    `case_instances.tenant_id` is a foreign key, so a rehearsal in a fresh twin
    fails on the very first insert without this — and it fails deep inside a
    flush, which poisons the session and makes the next case look broken too.
    Only the id, the slug and a name are copied: a twin has no business holding
    a tenant's settings, and copying them would make the rehearsal behave like
    production in ways nobody asked it to.
    """
    from velaris_shared.db.models import TenantModel

    if await session.get(TenantModel, tenant_uuid) is not None:
        return
    session.add(TenantModel(
        id=tenant_uuid, slug=f"twin-{str(tenant_uuid)[:8]}",
        name="Twin rehearsal tenant", description="Created by the F-04 twin.",
        status="active", settings={}))
    await session.flush()


async def _teardown(session: AsyncSession, run_id: uuid.UUID) -> int:
    """Delete exactly what this run made. Nothing else.

    The marker is matched in Python rather than with a JSON operator because
    `data` is portable JSON across three dialects and the operator differs on
    each. Sweeping by "recent cases" instead would eventually delete somebody's
    deliberate fixture out of their own twin.
    """
    rows = list((await session.execute(select(CaseInstanceModel))).scalars().all())
    mine = [c.id for c in rows
            if isinstance(c.data, dict) and str(c.data.get(TWIN_MARKER)) == str(run_id)]
    if not mine:
        return 0

    # Every child whose foreign key does NOT cascade has to go first. Two do:
    # `case_audit_log` and `documents` are deliberately `NO ACTION` in this
    # schema, and advancing a case writes audit rows — so without this the very
    # first rehearsal fails to clean up and the twin starts accumulating.
    #
    # Deleting from an audit log is normally the mistake migration 180 taught,
    # and it is not one here for a reason worth stating: these rows are about
    # synthetic cases that never existed, in a database no person reads. The
    # rule is about a PRODUCTION audit log, and this teardown can only ever run
    # against a database that passed the isolation check.
    from velaris_shared.db.models import CaseAuditLogModel, DocumentModel

    for model, column in ((CaseAuditLogModel, CaseAuditLogModel.case_id),
                          (DocumentModel, DocumentModel.case_id),
                          (CaseStepCompletionModel, CaseStepCompletionModel.case_id)):
        await session.execute(delete(model).where(column.in_(mine)))
    await session.execute(
        delete(CaseInstanceModel).where(CaseInstanceModel.id.in_(mine)))
    await session.commit()
    return len(mine)


async def run(
    session: AsyncSession,
    *,
    tenant_uuid: uuid.UUID,
    bundle: dict[str, Any] | None,
    run_row: Any = None,
    cases_per_type: int = 5,
    created_by: str | None = None,
    temporal_client: Any = None,
) -> TwinRunModel:
    """Provision the candidate into the twin, drive synthetic cases, report.

    The returned row is stored on THIS session (the production one) — the twin
    holds the rehearsal, production holds the record of it. Everything the
    rehearsal writes goes through the twin session and nothing else.
    """
    s = get_settings()
    row = TwinRunModel(
        id=uuid.uuid4(), tenant_id=tenant_uuid,
        deployment_run_id=getattr(run_row, "id", None),
        package_checksum=getattr(run_row, "package_checksum", None),
        task_queue=(s.twin_task_queue or None),
        load_profile=LOAD_PROFILE, created_by=created_by, status="pending")

    if not configured():
        row.status = "refused"
        row.refusal_reason = REFUSE_NO_TWIN
        row.error = ("No twin is configured on this install, so nothing was "
                     "rehearsed. Set VELARIS_CASE_TWIN_DATABASE_URL and run a "
                     "second case-service worker against it.")
        session.add(row)
        await session.flush()
        return row

    row.database_fingerprint = fingerprint(s.twin_database_url)
    ok, detail = isolation(s)
    row.isolation_verified = ok
    row.isolation_detail = detail
    if not ok:
        row.status = "refused"
        row.refusal_reason = REFUSE_NOT_ISOLATED
        row.error = detail.get("reason")
        session.add(row)
        await session.flush()
        return row

    case_types = list((bundle or {}).get("case_types") or [])
    if not case_types:
        row.status = "refused"
        row.refusal_reason = REFUSE_NOTHING_TO_RUN
        row.error = ("The package contains no case types, so there is nothing "
                     "for a twin to execute. This is not a fault in the "
                     "package.")
        session.add(row)
        await session.flush()
        return row

    row.status = "running"
    session.add(row)
    await session.flush()

    try:
        result = await _execute(
            row=row, tenant_uuid=tenant_uuid, bundle=bundle or {},
            cases_per_type=max(1, cases_per_type), settings=s,
            temporal_client=temporal_client)
    except TwinError as exc:
        row.status = "refused"
        row.refusal_reason = REFUSE_UNREACHABLE
        row.error = str(exc)
        row.completed_at = _utcnow()
        await session.flush()
        return row
    except Exception as exc:                                     # noqa: BLE001
        log.exception("twin: rehearsal failed")
        row.status = "failed"
        row.error = f"{type(exc).__name__}: {exc}"
        row.completed_at = _utcnow()
        await session.flush()
        return row

    for key, value in result.items():
        setattr(row, key, value)
    row.status = "complete"
    row.completed_at = _utcnow()
    await session.flush()
    return row


async def _execute(*, row: TwinRunModel, tenant_uuid: uuid.UUID, bundle: dict,
                   cases_per_type: int, settings: Any,
                   temporal_client: Any) -> dict[str, Any]:
    from case_service.hxdeploy import packager

    await ensure_schema()
    factory = twin_sessions()
    deadline = time.monotonic() + max(30, int(settings.twin_deadline_seconds))
    cap = max(1, int(settings.twin_max_cases))
    twin_queue = (settings.twin_task_queue or "").strip()
    pollers = await worker_pollers(temporal_client, twin_queue)

    durations: list[int] = []
    stage_profile: dict[str, int] = {}
    failures: list[dict[str, Any]] = []
    requested = started = terminal = failed = stuck = 0
    provisioned: dict[str, Any] = {}

    async with factory() as twin:
        try:
            await _ensure_tenant(twin, tenant_uuid)
            provisioned = await packager.apply_bundle(
                twin, bundle, str(tenant_uuid), imported_by="velaris-twin")
            await twin.commit()
        except Exception as exc:                                 # noqa: BLE001
            raise TwinError(
                f"the candidate could not be provisioned into the twin: {exc}"
            ) from exc

        wanted_names = {ct.get("name") for ct in bundle.get("case_types") or []}
        rows = list((await twin.execute(
            select(CaseTypeModel).where(CaseTypeModel.name.in_(wanted_names))
        )).scalars().all())

        for case_type in rows:
            definition = case_type.definition_json or {}
            orchestration = str(definition.get("orchestration") or "").lower()
            if orchestration == "temporal" and not (twin_queue and pollers):
                # NOT driven inline. A Temporal-orchestrated case type run
                # synchronously would be a rehearsal of a different execution
                # model from the one being promoted — a more misleading answer
                # than no answer.
                failures.append({
                    "case_type": case_type.name,
                    "outcome": "not_dispatched",
                    "reason": (
                        "this case type is Temporal-orchestrated and no twin "
                        "worker is polling "
                        + (f"{twin_queue!r}" if twin_queue
                           else "(no twin task queue is configured)")
                        + ". Running it inline would rehearse a different "
                          "execution model, so it was not run."),
                })
                continue

            for index in range(cases_per_type):
                if requested >= cap or time.monotonic() > deadline:
                    break
                requested += 1
                try:
                    case = await _synthetic_case(
                        twin, case_type=case_type, tenant_uuid=tenant_uuid,
                        run_id=row.id, index=index)
                    started += 1
                    outcome = await _drive_case(twin, case=case, definition=definition)
                    await twin.commit()
                except Exception as exc:                         # noqa: BLE001
                    await twin.rollback()
                    failed += 1
                    failures.append({"case_type": case_type.name,
                                     "outcome": "error",
                                     "reason": f"{type(exc).__name__}: {exc}"})
                    continue

                for visited in outcome.get("visited") or []:
                    stage_profile[visited] = stage_profile.get(visited, 0) + 1
                if outcome["outcome"] == "terminal":
                    terminal += 1
                    durations.append(int(outcome.get("ms") or 0))
                elif outcome["outcome"] == "stuck":
                    stuck += 1
                    failures.append({"case_type": case_type.name,
                                     "outcome": "stuck",
                                     "stage": outcome.get("stage"),
                                     "reason": outcome.get("reason")})
                else:
                    failed += 1
                    failures.append({"case_type": case_type.name,
                                     "outcome": "failed",
                                     "reason": outcome.get("reason")})

        torn_down = 0
        teardown_error = None
        try:
            torn_down = await _teardown(twin, row.id)
        except Exception as exc:                                 # noqa: BLE001
            # The verdict stands; somebody has to be told the twin needs a sweep.
            teardown_error = f"{type(exc).__name__}: {exc}"
            log.warning("twin: teardown failed for run %s: %s", row.id, exc)

    return {
        "provisioned": {**(provisioned or {}), "pollers": pollers,
                        "case_types": sorted(n for n in wanted_names if n)},
        "requested_cases": requested,
        "started_cases": started,
        "terminal_cases": terminal,
        "failed_cases": failed,
        "stuck_cases": stuck,
        "p50_ms": _pct(durations, 0.50),
        "p95_ms": _pct(durations, 0.95),
        "max_ms": max(durations) if durations else None,
        "stage_profile": stage_profile,
        "failures": failures[:50],
        "torn_down_at": _utcnow(),
        "torn_down_rows": torn_down,
        "teardown_error": teardown_error,
    }
