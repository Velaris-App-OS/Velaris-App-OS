"""RPA bots — registry, queue, results, and the attended review gate.

Two kinds of caller, authenticated differently on purpose:

* **operators and admins** use a normal Velaris JWT, gated by HxGuard;
* **the bot container** holds a **per-job, single-use token** and can do exactly
  one thing with it: POST that job's result. It has no other authority, which is
  what let the scoped broker stay read-only instead of gaining write scopes that
  every container on the apps network would then carry, permanently, to serve
  this one feature.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from case_service.core.tenant_path_guard import platform_operator_only as _platform_operator_only
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared import hxguard
from velaris_shared.auth.models import AuthenticatedUser
from case_service.bots import credentials as bot_credentials
from case_service.bots import service
from velaris_shared.db.models import (
    BotCredentialModel,
    BotDefinitionModel,
    BotJobModel,
    BotRunEventModel,
)
from case_service.core.timestamps import iso_utc
from velaris_shared.db.session import get_session
from velaris_shared.middleware.endpoint_rate_limit import rate_limit
from case_service.tenancy.identity import caller_tenant_or_refuse, may_reach_tenant

log = logging.getLogger(__name__)

router = APIRouter(prefix="/bots", tags=["bots"])

#: The result and progress endpoints are reachable from the apps network and
#: authenticate on a per-job token, so they get their own bound.
_result_rl = rate_limit(max_calls=120, window_seconds=60, name="bot-result")


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _mine_or_404(session, user, bot: BotDefinitionModel) -> None:
    """A bot from another tenant is not yours to approve, disable or run.

    404 rather than 403: "exists but is not yours" is an answer nobody outside
    that tenant needs. Without this, a tenant's own admin could approve a
    definition somebody else registered against their tenant — the approval gate
    reviewing the wrong person's container.
    """
    if not await may_reach_tenant(session, user, bot.tenant_id):
        raise HTTPException(404, "Bot not found.")


async def _job_mine_or_404(session, user, job: BotJobModel) -> None:
    """Another tenant's job is not yours to read, cancel or accept.

    The same rule as `_mine_or_404`, at the job level: without it, cancelling a
    running bot, or **accepting an attended bot's proposed changes onto a case**,
    was reachable by id alone from any tenant.
    """
    if not await may_reach_tenant(session, user, job.tenant_id):
        raise HTTPException(404, "Job not found.")


def _bot_dict(b: BotDefinitionModel) -> dict:
    return {
        "id": str(b.id), "tenant_id": b.tenant_id, "name": b.name,
        "description": b.description, "image": b.image, "digest": b.digest,
        "signature_verified": b.signature_verified,
        "declared_command": b.declared_command or [],
        "declared_env": b.declared_env or {},
        "credential_refs": b.credential_refs or [],
        "input_schema": b.input_schema or {},
        "output_schema": b.output_schema or {},
        "granted_domains": b.granted_domains or [],
        "timeout_seconds": b.timeout_seconds,
        "max_concurrency": b.max_concurrency,
        "resource_profile": b.resource_profile,
        "fanout_input_key": b.fanout_input_key,
        "fanout_max": b.fanout_max,
        "status": b.status, "created_by": b.created_by,
        "last_edited_by": b.last_edited_by,
        "approved_by": b.approved_by,
        "approved_at": iso_utc(b.approved_at),
    }


def _job_dict(j: BotJobModel) -> dict:
    return {
        "id": str(j.id), "bot_id": str(j.bot_id), "tenant_id": j.tenant_id,
        "case_id": str(j.case_id) if j.case_id else None,
        "step_id": j.step_id, "node_id": j.node_id,
        "process_instance_id": str(j.process_instance_id) if j.process_instance_id else None,
        "trigger": j.trigger, "requested_by": j.requested_by,
        "input": j.input or {}, "output": j.output,
        "artifacts": j.artifacts or [],
        "status": j.status, "attempt": j.attempt, "error": j.error,
        "batch_id": str(j.batch_id) if j.batch_id else None,
        "batch_index": j.batch_index, "batch_size": j.batch_size,
        "run_after": iso_utc(j.run_after),
        "queued_at": iso_utc(j.queued_at),
        "started_at": iso_utc(j.started_at),
        "ended_at": iso_utc(j.ended_at),
        # The token hash is never returned in any shape — it is the credential.
    }


# ── definitions ───────────────────────────────────────────────────────────

class BotIn(BaseModel):
    tenant_id: str = "default"
    name: str = Field(..., max_length=255)
    description: str = ""
    image: str = Field(..., max_length=512)
    digest: str = Field(..., max_length=80)
    declared_command: list[str] = []
    declared_env: dict = {}
    credential_refs: list[str] = []
    input_schema: dict = {}
    output_schema: dict = {}
    granted_domains: list[str] = []
    timeout_seconds: int = Field(300, ge=1, le=3600)
    max_concurrency: int = Field(1, ge=1, le=32)
    resource_profile: str = "light"
    #: NULL/empty = no fan-out. On the definition, not the run request, so the
    #: approver can see that one call may start `fanout_max` containers.
    fanout_input_key: Optional[str] = Field(None, max_length=255)
    fanout_max: int = Field(25, ge=1, le=500)


@router.get("")
async def list_bots(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("bots.definitions.read")),
):
    rows = (await session.execute(
        select(BotDefinitionModel).order_by(BotDefinitionModel.name)
    )).scalars().all()
    # Filtered in Python: a tenant token is legitimately a slug OR a UUID, so
    # `same_tenant` is the only comparison that holds on both conventions.
    mine = [b for b in rows if await may_reach_tenant(session, user, b.tenant_id)]
    return {"bots": [_bot_dict(b) for b in mine]}


@router.post("", status_code=201)
async def create_bot(
    body: BotIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("bots.definitions.write")),
):
    """Register a bot. It is created **draft** and cannot run until approved."""
    try:
        tenant_id = await caller_tenant_or_refuse(session, user, body.tenant_id)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc

    bot = BotDefinitionModel(
        tenant_id=tenant_id, name=body.name, description=body.description,
        image=body.image, digest=body.digest,
        declared_command=body.declared_command, declared_env=body.declared_env,
        credential_refs=body.credential_refs,
        input_schema=body.input_schema, output_schema=body.output_schema,
        granted_domains=body.granted_domains,
        timeout_seconds=body.timeout_seconds,
        max_concurrency=body.max_concurrency,
        resource_profile=body.resource_profile,
        fanout_input_key=(body.fanout_input_key or None),
        fanout_max=body.fanout_max,
        status="draft", created_by=user.username,
    )
    try:
        service.validate_definition(bot)
    except service.BotError as exc:
        raise HTTPException(exc.status, str(exc)) from exc

    session.add(bot)
    await session.commit()
    await session.refresh(bot)
    return _bot_dict(bot)


@router.post("/{bot_id}/approve")
async def approve_bot(
    bot_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("bots.definitions.approve")),
):
    """Put a bot into production. Deliberately separate from create, and never
    by its author — approval *is* the review."""
    bot = await session.get(BotDefinitionModel, bot_id)
    if bot is None:
        raise HTTPException(404, "Bot not found.")
    await _mine_or_404(session, user, bot)
    try:
        await service.approve(session, bot, approver=user.username)
    except service.BotError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    await session.commit()

    from velaris_shared.db.security_events import log_security_event
    await log_security_event(
        session, event_type="bot_approved", severity="high",
        user_id=user.user_id, resource_type="bot_definition", resource_id=str(bot.id),
        action="approve", outcome="success",
        details={"name": bot.name, "image": bot.image, "digest": bot.digest,
                 "granted_domains": bot.granted_domains or [],
                 "credential_refs": bot.credential_refs or []},
    )
    await session.commit()
    await session.refresh(bot)
    return _bot_dict(bot)


@router.post("/{bot_id}/disable")
async def disable_bot(
    bot_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("bots.definitions.write")),
):
    """The kill switch: disable, then cancel everything in flight.

    Identical in spirit to revoking a Layer-2 grant — a bot that has started
    misbehaving must stop now, not at the end of its queue.
    """
    bot = await session.get(BotDefinitionModel, bot_id)
    if bot is None:
        raise HTTPException(404, "Bot not found.")
    await _mine_or_404(session, user, bot)
    bot.status = "disabled"

    live = (await session.execute(
        select(BotJobModel).where(
            BotJobModel.bot_id == bot.id,
            BotJobModel.status.in_(("queued", "leased", "running")),
        )
    )).scalars().all()
    for job in live:
        await service.fail(session, job, error="Bot disabled.", status="cancelled")
        if job.container_id:
            try:
                from velaris_shared.apps.runtime import stop_app_container

                stop_app_container(job.container_id)
            except Exception:
                log.exception("could not stop container for cancelled job %s", job.id)
    await session.commit()
    return {"disabled": True, "cancelled_jobs": len(live)}


# ── credentials ───────────────────────────────────────────────────────────

class CredentialIn(BaseModel):
    tenant_id: str = "default"
    ref: str = Field(..., max_length=512)
    secret: str = Field(..., max_length=4096)


@router.get("/credentials")
async def list_credentials(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("bots.credentials.write")),
):
    """Refs and metadata only. The value is never returned, to anyone."""
    rows = (await session.execute(
        select(BotCredentialModel).order_by(BotCredentialModel.ref)
    )).scalars().all()
    rows = [r for r in rows if await may_reach_tenant(session, user, r.tenant_id)]
    return {"credentials": [{
        "id": str(r.id), "tenant_id": r.tenant_id, "ref": r.ref,
        "version": r.version, "created_by": r.created_by,
        "env_name": bot_credentials.env_name_for(r.ref),
        "updated_at": iso_utc(r.updated_at),
    } for r in rows]}


@router.put("/credentials")
async def upsert_credential(
    body: CredentialIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("bots.credentials.write")),
):
    """Store or rotate a bot credential.

    Encrypted under the tenant's HxVault DEK, so a database dump yields nothing
    and crypto-shredding the tenant destroys it with everything else of theirs.
    Rotating bumps `version`, which is what makes outstanding leases dead rather
    than merely stale.
    """
    from case_service.hxbridge.encryption import encrypt_credentials
    from case_service.hxvault.keyring import ensure_dek
    from case_service.tenancy.identity import resolve_tenant

    env_name = bot_credentials.env_name_for(body.ref)
    if env_name in bot_credentials.RESERVED_ENV:
        raise HTTPException(
            400, f"'{body.ref}' maps to the reserved environment variable "
                 f"'{env_name}'. Rename the ref.")

    try:
        body.tenant_id = await caller_tenant_or_refuse(session, user, body.tenant_id)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc

    resolved = await resolve_tenant(session, body.tenant_id)
    vault = False
    if resolved is not None:
        try:
            await ensure_dek(session, resolved[0])
            vault = True
        except Exception:
            # A tenant without a usable DEK falls back to the platform key
            # rather than storing plaintext.
            log.warning("no HxVault DEK for %s — using the platform key", body.tenant_id)

    row = (await session.execute(
        select(BotCredentialModel).where(
            BotCredentialModel.tenant_id == body.tenant_id,
            BotCredentialModel.ref == body.ref,
        )
    )).scalar_one_or_none()

    value_enc = encrypt_credentials(
        {"secret": body.secret},
        tenant_id=resolved[0] if resolved else None, vault=vault,
    )
    if row is None:
        row = BotCredentialModel(
            tenant_id=body.tenant_id, ref=body.ref, value_enc=value_enc,
            created_by=user.username, version=1,
        )
        session.add(row)
    else:
        row.value_enc = value_enc
        row.version = (row.version or 1) + 1

    await session.commit()
    await session.refresh(row)
    return {"ref": row.ref, "version": row.version,
            "env_name": bot_credentials.env_name_for(row.ref)}


# ── jobs ──────────────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    input: dict = {}
    case_id: Optional[uuid.UUID] = None
    step_id: Optional[str] = None


@router.post("/{bot_id}/run", status_code=202)
async def run_bot(
    bot_id: uuid.UUID,
    body: RunRequest,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("bots.jobs.run")),
):
    """Queue a job — or a fan-out batch, if the definition asks for one.

    202, because the runner leases it: this does not block. A fan-out answers
    with every job and the batch id, not just the first, so the caller can see
    what it actually started.
    """
    bot = await session.get(BotDefinitionModel, bot_id)
    if bot is None:
        raise HTTPException(404, "Bot not found.")
    await _mine_or_404(session, user, bot)
    try:
        jobs = await service.enqueue_run(
            session, bot=bot, input_data=body.input, trigger="agent",
            requested_by=user.username, case_id=body.case_id, step_id=body.step_id,
        )
    except service.BotError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    await session.commit()

    if len(jobs) == 1 and jobs[0].batch_id is None:
        return _job_dict(jobs[0])
    return {"batch_id": str(jobs[0].batch_id), "size": len(jobs),
            "jobs": [_job_dict(j) for j in jobs]}


@router.get("/batches/{batch_id}")
async def get_batch(
    batch_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("bots.jobs.read")),
):
    """How a fan-out batch is doing, and whether every child has finished."""
    try:
        summary = await service.batch_summary(session, batch_id)
    except service.BotError as exc:
        raise HTTPException(exc.status, str(exc)) from exc

    first = await session.get(BotJobModel, uuid.UUID(summary["jobs"][0]))
    if first is not None:
        await _job_mine_or_404(session, user, first)
    return summary


class TenantLimitIn(BaseModel):
    tenant_id: str = "default"
    max_concurrent_jobs: int = Field(..., ge=1, le=200)


@router.get("/limits")
async def list_tenant_limits(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("bots.definitions.read")),
):
    """Every tenant's ceiling, and the platform default behind it."""
    from velaris_shared.config import get_settings
    from velaris_shared.db.models import BotTenantLimitModel

    # "Every tenant's ceiling" is the platform operator's view; a tenant's admin
    # sees their own (8d, 2026-09-29).
    from case_service.tenancy.identity import is_platform_operator, scope_to_caller

    stmt = select(BotTenantLimitModel).order_by(BotTenantLimitModel.tenant_id)
    if not await is_platform_operator(session, user):
        stmt = await scope_to_caller(stmt, BotTenantLimitModel, session, user)
    rows = (await session.execute(stmt)).scalars().all()
    return {
        "default": int(getattr(get_settings(), "bot_tenant_max_concurrent", 4)),
        "limits": [{"tenant_id": r.tenant_id,
                    "max_concurrent_jobs": r.max_concurrent_jobs,
                    "updated_by": r.updated_by,
                    "updated_at": iso_utc(r.updated_at)}
                   for r in rows],
    }


@router.put("/limits", dependencies=[Depends(_platform_operator_only)])
async def set_tenant_limit(
    body: TenantLimitIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("bots.definitions.write")),
):
    """Override a tenant's concurrent-job ceiling.

    An override, never the switch that turns the cap on: with no row the
    platform default applies, so deleting one restores a bound rather than
    removing it.
    """
    from velaris_shared.db.models import BotTenantLimitModel

    row = await session.get(BotTenantLimitModel, body.tenant_id)
    if row is None:
        row = BotTenantLimitModel(
            tenant_id=body.tenant_id,
            max_concurrent_jobs=body.max_concurrent_jobs,
            updated_by=user.username)
        session.add(row)
    else:
        row.max_concurrent_jobs = body.max_concurrent_jobs
        row.updated_by = user.username
    await session.commit()
    return {"tenant_id": body.tenant_id,
            "max_concurrent_jobs": body.max_concurrent_jobs}


@router.get("/jobs")
async def list_jobs(
    status: Optional[str] = Query(None),
    case_id: Optional[uuid.UUID] = Query(None),
    bot_id: Optional[uuid.UUID] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("bots.jobs.read")),
):
    stmt = select(BotJobModel)
    if status:
        stmt = stmt.where(BotJobModel.status == status)
    if case_id:
        stmt = stmt.where(BotJobModel.case_id == case_id)
    if bot_id:
        stmt = stmt.where(BotJobModel.bot_id == bot_id)
    rows = (await session.execute(
        stmt.order_by(desc(BotJobModel.queued_at)).limit(limit)
    )).scalars().all()
    mine = [j for j in rows if await may_reach_tenant(session, user, j.tenant_id)]
    return {"jobs": [_job_dict(j) for j in mine]}


@router.get("/jobs/{job_id}")
async def get_job(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("bots.jobs.read")),
):
    job = await session.get(BotJobModel, job_id)
    if job is None:
        raise HTTPException(404, "Job not found.")
    await _job_mine_or_404(session, user, job)
    events = (await session.execute(
        select(BotRunEventModel).where(BotRunEventModel.job_id == job.id)
        .order_by(BotRunEventModel.created_at)
    )).scalars().all()

    # "Which bot did what" needs three records together, not one: what the job
    # did, which credentials it held while doing it, and every host it tried to
    # reach. They are written in three places by three subsystems, and a person
    # answering that question should not have to know that.
    from velaris_shared.db.models import BotCredentialLeaseModel

    leases = (await session.execute(
        select(BotCredentialLeaseModel)
        .where(BotCredentialLeaseModel.job_id == job.id)
        .order_by(BotCredentialLeaseModel.leased_at)
    )).scalars().all()

    network: list[dict] = []
    try:
        from velaris_shared.db.models import MarketplaceNetworkLogModel
        from velaris_shared.apps import egress as mkt_egress

        if mkt_egress.egress_enabled():
            # Fold in anything the gateway has written since, so the view is
            # current rather than as-of-whenever-somebody-last-looked.
            await mkt_egress.ingest_access_log(session)
            await session.commit()
        rows = (await session.execute(
            select(MarketplaceNetworkLogModel)
            .where(MarketplaceNetworkLogModel.job_id == job.id)
            .order_by(desc(MarketplaceNetworkLogModel.created_at)).limit(200)
        )).scalars().all()
        network = [{"destination": r.destination_url, "ip": r.destination_ip,
                    "status": r.status, "declared": r.is_declared,
                    "bytes_out": r.bytes_sent, "bytes_in": r.bytes_received,
                    "at": iso_utc(r.created_at)}
                   for r in rows]
    except Exception:
        # The audit view must not fail because the gateway is absent on this
        # deployment — an empty list is the honest answer there. But it is NOT
        # the honest answer to a broken write, and this handler hid one for a
        # day: every bot's egress row failed its foreign key, the commit raised
        # here, and the view reported "no hosts reached". A refusal to fail is
        # not a licence to stay quiet, so this is a warning with a traceback,
        # and the session is rolled back rather than left mid-failure.
        log.warning("egress log unavailable for job %s", job_id, exc_info=True)
        try:
            await session.rollback()
        except Exception:
            pass

    return {
        "job": _job_dict(job),
        "events": [{"kind": e.kind, "detail": e.detail or {},
                    "at": iso_utc(e.created_at)}
                   for e in events],
        # The ref and the timestamps — never the value, which nothing can read
        # back at all.
        "credentials": [{"ref": l.vault_ref,
                         "leased_at": iso_utc(l.leased_at),
                         "expires_at": iso_utc(l.expires_at),
                         "revoked_at": iso_utc(l.revoked_at)}
                        for l in leases],
        "network": network,
    }


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("bots.jobs.run")),
):
    job = await session.get(BotJobModel, job_id)
    if job is None:
        raise HTTPException(404, "Job not found.")
    await _job_mine_or_404(session, user, job)
    if job.status in service.TERMINAL_STATUSES:
        raise HTTPException(409, f"This job already {job.status}.")

    await service.fail(session, job, error="Cancelled by an operator.",
                       status="cancelled")
    if job.container_id:
        try:
            from velaris_shared.apps.runtime import stop_app_container

            stop_app_container(job.container_id)
        except Exception:
            log.exception("could not stop container for cancelled job %s", job.id)
    await session.commit()
    return _job_dict(job)


# ── the result path (per-job token) ───────────────────────────────────────

def _presented_token(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    return request.headers.get("x-velaris-job-token")


class ResultIn(BaseModel):
    output: Any = None
    artifacts: list = []


@router.post("/jobs/{job_id}/result", dependencies=[Depends(_result_rl)])
async def submit_result(
    job_id: uuid.UUID,
    body: ResultIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """The bot returns its work. Once.

    Authenticated by the per-job token and nothing else — this is called from a
    container that holds no session and no user. Every refusal is the same 404:
    a distinct "already used" would tell whoever holds a stolen token that it
    was genuine.
    """
    raw = await request.body()
    if len(raw) > service.MAX_RESULT_BYTES:
        raise HTTPException(413, "Result too large.")

    try:
        job = await service.redeem_result(
            session, job_id=job_id, presented_token=_presented_token(request),
            output=body.output, artifacts=body.artifacts,
        )
    except service.BotError as exc:
        await session.commit()          # keep the burned token and the event
        raise HTTPException(exc.status, str(exc)) from exc

    # An unattended job's output is applied by the platform, under the
    # workflow's authority. An attended one is a proposal the operator accepts.
    await service.apply_to_case(session, job)
    await session.commit()
    return {"accepted": True, "job_id": str(job.id)}


class ProgressIn(BaseModel):
    message: str = Field("", max_length=1000)
    percent: Optional[int] = Field(None, ge=0, le=100)


@router.post("/jobs/{job_id}/progress", dependencies=[Depends(_result_rl)])
async def submit_progress(
    job_id: uuid.UUID,
    body: ProgressIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Live progress, so a 90-second bot is not a frozen spinner.

    The **same** per-job token, but NOT single-use — progress is many, results
    are one. It cannot complete the job, so replaying it achieves nothing.
    """
    from case_service.bots import tokens

    job = await session.get(BotJobModel, job_id)
    if job is None or not tokens.matches(_presented_token(request), job.result_token_hash):
        raise HTTPException(404, "Not found.")
    if job.status in service.TERMINAL_STATUSES:
        raise HTTPException(404, "Not found.")

    await service.record_event(session, job.id, "progress",
                               {"message": body.message, "percent": body.percent})
    await session.commit()

    try:
        from case_service.realtime.publisher import publish_case_event

        if job.case_id:
            await publish_case_event(job.case_id, "bot_progress", {
                "job_id": str(job.id), "message": body.message,
                "percent": body.percent,
            })
    except Exception:
        log.debug("bot progress publish failed", exc_info=True)
    return {"recorded": True}


# ── attended: propose, then accept ────────────────────────────────────────

class AcceptRequest(BaseModel):
    #: The case's data checksum as the operator saw it. A mismatch is a 409
    #: rather than a silent overwrite — the same rule HxDraft already applies.
    expected_checksum: Optional[str] = None
    fields: Optional[list[str]] = None


def _checksum(data: dict) -> str:
    import hashlib
    import json

    return hashlib.sha256(
        json.dumps(data or {}, sort_keys=True, default=str).encode()).hexdigest()


@router.get("/jobs/{job_id}/proposal")
async def job_proposal(
    job_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("bots.jobs.read")),
):
    """What an attended job wants to change, as a diff against the case.

    The human is the commit point. That is the whole difference between an
    attended bot and an unattended one, and it is why the output is shown rather
    than applied.
    """
    from velaris_shared.db.models import CaseInstanceModel

    job = await session.get(BotJobModel, job_id)
    if job is None or job.status != "succeeded":
        raise HTTPException(404, "No completed job here.")
    await _job_mine_or_404(session, user, job)
    case = await session.get(CaseInstanceModel, job.case_id) if job.case_id else None
    current = dict(case.data or {}) if case else {}

    changes = [
        {"field": key, "current": current.get(key), "proposed": value}
        for key, value in (job.output or {}).items()
        if not str(key).startswith("_") and current.get(key) != value
    ]
    return {"job_id": str(job.id), "case_id": str(job.case_id) if job.case_id else None,
            "checksum": _checksum(current), "changes": changes}


@router.post("/jobs/{job_id}/accept")
async def accept_proposal(
    job_id: uuid.UUID,
    body: AcceptRequest,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("bots.jobs.run")),
):
    """Apply an attended job's output, under the operator's authority."""
    from velaris_shared.db.models import CaseInstanceModel

    job = await session.get(BotJobModel, job_id)
    if job is None or job.status != "succeeded" or job.case_id is None:
        raise HTTPException(404, "No completed job here.")
    # The write path: accepting applies a container's output to a case under the
    # operator's authority, so it is the last place a cross-tenant job id may be
    # honoured.
    await _job_mine_or_404(session, user, job)

    case = await session.get(CaseInstanceModel, job.case_id)
    if case is None:
        raise HTTPException(404, "Case not found.")

    current = dict(case.data or {})
    if body.expected_checksum and body.expected_checksum != _checksum(current):
        raise HTTPException(
            409, "The case changed while this proposal was open. Reload the diff "
                 "and review it again.")

    wanted = set(body.fields) if body.fields else None
    applied = []
    for key, value in (job.output or {}).items():
        if str(key).startswith("_") or (wanted is not None and key not in wanted):
            continue
        current[key] = value
        applied.append(key)
    case.data = current

    await service.record_event(session, job.id, "accepted",
                               {"by": user.username, "fields": applied})
    await session.commit()
    return {"applied": applied}


# ── the editor ────────────────────────────────────────────────────────────
#
# Registered **last on purpose**. `PUT /bots/{bot_id}` takes a UUID path
# parameter, and FastAPI matches in declaration order: declared above, it
# swallows `PUT /bots/credentials` and `PUT /bots/limits`, which then answer 422
# on a UUID that was never meant to be one. A literal path must be declared
# before the parameterised one that would eat it.

#: The fields an edit may change. `status`, `created_by`, `approved_by` and
#: `approved_at` are deliberately absent: an editor that could set them would be
#: a way around the approval gate, which is the entire control here.
EDITABLE_FIELDS = (
    "name", "description", "image", "digest", "declared_command", "declared_env",
    "credential_refs", "input_schema", "output_schema", "granted_domains",
    "timeout_seconds", "max_concurrency", "resource_profile",
    "fanout_input_key", "fanout_max",
)


@router.put("/{bot_id}")
async def update_bot(
    bot_id: uuid.UUID,
    body: BotIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("bots.definitions.write")),
):
    """Edit a definition. **Any change returns it to `draft`.**

    One rule, not a taxonomy of safe and unsafe fields. A classification is a
    thing to get wrong, and getting it wrong here means an approved bot whose
    image, egress or credential set changed without a second person looking —
    which is exactly the review the approval gate exists to force. So an edit is
    an edit: the bot stops being runnable until somebody who is not its author
    approves it again.

    Editing nothing changes nothing, including the approval: a no-op save must
    not de-approve a live bot, or the editor becomes a denial-of-service on any
    bot somebody merely opened.
    """
    bot = await session.get(BotDefinitionModel, bot_id)
    if bot is None:
        raise HTTPException(404, "Bot not found.")
    await _mine_or_404(session, user, bot)

    # `tenant_id` is not in EDITABLE_FIELDS: an edit may not move a definition
    # into another tenant.
    incoming = {f: getattr(body, f) for f in EDITABLE_FIELDS}
    incoming["fanout_input_key"] = incoming["fanout_input_key"] or None
    changed = [f for f in EDITABLE_FIELDS if getattr(bot, f) != incoming[f]]
    if not changed:
        return _bot_dict(bot)

    for field in changed:
        setattr(bot, field, incoming[field])

    try:
        service.validate_definition(bot)
    except service.BotError as exc:
        await session.rollback()
        raise HTTPException(exc.status, str(exc)) from exc

    was_active = bot.status == "active"
    approver_lost = bot.approved_by
    # Who made this change, so approval can refuse them: the edit-returns-to-draft
    # rule is exactly what puts a reviewer in a position to re-approve their own
    # edit under the original author's name.
    bot.last_edited_by = user.username
    bot.status = "draft"
    bot.approved_by = None
    bot.approved_at = None

    # In-flight jobs are left alone on purpose: they were started against the
    # definition as approved, and killing them would make "save" a kill switch
    # that does not look like one. `disable` is the kill switch.
    await session.commit()
    await session.refresh(bot)

    if was_active:
        from velaris_shared.db.security_events import log_security_event

        await log_security_event(
            session, event_type="bot_edited_to_draft", severity="high",
            user_id=user.user_id, resource_type="bot_definition",
            resource_id=str(bot.id), action="update", outcome="success",
            details={"name": bot.name, "fields": changed,
                     "previous_approver": approver_lost},
        )
        await session.commit()
    return _bot_dict(bot)
