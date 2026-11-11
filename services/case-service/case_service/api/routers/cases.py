"""Case instance API router.

Handles creation, retrieval, status changes, stage transitions,
and lifecycle operations for case instances.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

# `CaseAction`, `CasePriorityChange`, `CaseResolve` and `CaseStatusChange` are no
# longer imported here: the six routes that used them became the action
# dispatcher, whose body is `ActionRequest`. The schemas themselves are KEPT in
# `api/schemas/cases.py` — they are part of the published schema surface, and
# deleting them would break any generated client that still references the type
# even though the route is gone.
from case_service.api.schemas.cases import (
    AssignmentResponse,
    AuditEntryResponse,
    CaseCreate,
    CaseDataUpdate,
    CaseListResponse,
    CaseResponse,
    CaseStageTransition,
    RelationshipCreate,
    RelationshipResponse,
    StepCompleteBody,
    StepCompletionResponse,
)
from case_service.core import case_lifecycle
from case_service.core.outbound_rules import fire_outbound_rules
from velaris_shared.db import repository as repo
from case_service.integrations.webhook_dispatcher import (
    dispatch_event as _dispatch_webhook,
    build_case_event_payload as _webhook_payload,
)
from velaris_shared.process_mining import event_logger
from case_service.realtime.publisher import publish_case_event, publish_assignment_event
from velaris_shared.db.session import get_session
from velaris_shared import hxguard
from velaris_shared.auth.dependencies import get_current_user
from velaris_shared.auth.models import AuthenticatedUser
from case_service.tenancy.identity import (
    DEFAULT_TENANT,
    case_visible_to,
    is_superadmin,
    resolve_tenant,
    strict_case_reads,
)
from velaris_shared.hxstream.emitter import emit_trace
from case_service.case_vars import service as case_vars
from sqlalchemy import select as sa_select
from velaris_shared.core.error_codes import (
    CASE_AI_ACTION_400,
    CASE_GUARD_403,
    CASE_NOTFOUND_404,
    CASE_PROCDEF_400,
    CASE_RULE_DISABLED_409,
    CASE_RULE_SCOPE_403,
    CASE_SUBPROCESS_500,
    CASE_TENANT_403,
    CASE_VAR_FILTER_400,
    coded,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/cases", tags=["cases"])


# ── 198 ──────────────────────────────────────────────────────────────────────
# `/cases/actions/catalogue` is declared HERE, above `GET /cases/{case_id}`,
# and not beside the rest of the dispatcher at the foot of this file. FastAPI
# matches in registration order, so declared later it is shadowed: "actions" is
# offered to the `{case_id}` matcher, fails to parse as a UUID, and the endpoint
# answers 422 to every caller while looking perfectly correct in the source.
# Route shadowing of exactly this shape has bitten this platform before (F-05).

@router.get("/actions/catalogue")
async def action_catalogue(
    case_type_id: uuid.UUID | None = None,
    case_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Every action and status in force for the caller, with the tenant's overrides applied.

    This is what a UI renders its action bar from, so an action a tenant has
    disabled is ABSENT here — not present and greyed out. A control you may not
    use is absent, not decoration.

    **`case_id` is optional and narrows further.** Given one, the caller's own
    tenant clause applies (another tenant's id is a 404, as everywhere else),
    the case's type fills in `case_type_id`, and the case's STAGE removes the
    actions that stage does not offer. Without it the answer is the tenant-wide
    one, exactly as before — an action bar rendered from that would offer a
    button the dispatcher then refuses, which is why the case screen passes it.
    """
    from case_service.case_actions import dispatcher as _disp
    from case_service.case_actions import registry as _reg
    from case_service.case_actions import stages as _stg
    from case_service.case_actions import statuses as _st

    tenant_uuid = await _action_context(session, user)

    stage = None
    if case_id is not None:
        # The RELATIONSHIP check, not only the tenant one.
        #
        # Without it this endpoint answers "which stage is case X in" to any
        # caller in the tenant holding a valid token, for any id they can guess
        # or enumerate — a case-scoped fact the tenant-wide catalogue never
        # carried before `case_id` existed. `require_case` carries the tenant
        # clause itself since 2026-09-06, so this is the whole gate: 404 for
        # another tenant's case, 404 for one this operator has no relationship
        # to, and never a 403 that would confirm the id is real.
        await hxguard.require_case(session, user, "case.read", case_id)
        case = await _get_case_or_404(session, case_id, user)
        case_type_id = case_type_id or case.case_type_id
        stage = await _stg.stage_def(
            session, case_type_id=case.case_type_id,
            stage_id=case.current_stage_id)

    cfg = await _disp.resolve_config(
        session, tenant_id=tenant_uuid, case_type_id=case_type_id)
    status_set = await _st.resolve_set(
        session, tenant_id=tenant_uuid, case_type_id=case_type_id)

    actions = []
    for name in _reg.names():
        action = _reg.ACTIONS[name]
        row = cfg.get(name)
        if row is not None and not row.enabled:
            continue
        # Absent, not greyed out — the same rule the tenant's own switch gets.
        # This only applies when the caller named a case; without one there is
        # no stage to ask, and guessing would empty the bar on every screen that
        # renders the tenant-wide catalogue.
        if case_id is not None and not _stg.action_offered(stage, name):
            continue
        entry = action.describe()
        # Both privileges, because a tenant's override is an ADDITIONAL
        # requirement rather than a replacement — reporting only the override
        # would understate what a caller actually needs.
        required = list(_disp.required_privileges(action, row))
        if row is not None:
            entry["label"] = row.label or entry["label"]
            entry["allowed_from"] = list(row.allowed_from or [])
            entry["sort_order"] = row.sort_order
        else:
            entry["allowed_from"] = []
            entry["sort_order"] = 0
        entry["privilege"] = action.privilege
        entry["required_privileges"] = required

        # `permitted` answers "may this operator EVER do this", and only the
        # PLATFORM half of the gate can answer that here.
        #
        # `user_allows` is a synchronous privilege test and says so: a platform
        # decision is a pure function of the subject, with nothing to await. The
        # ReBAC half — `case.update` — is a question about a relationship to a
        # PARTICULAR case, and this endpoint is not case-scoped, so asking it
        # here would return False for everybody including admin and empty the
        # action bar. The relationship is checked at action time, per case,
        # which is the only place it means anything.
        from velaris_shared.hxguard.service import PLATFORM_ACTIONS
        platform_half = [p for p in required if p in PLATFORM_ACTIONS]
        entry["permitted"] = all(hxguard.user_allows(user, p) for p in platform_half)
        actions.append(entry)

    actions.sort(key=lambda a: (a["sort_order"], a["name"]))
    return {
        "actions": actions,
        "statuses": _st.describe(status_set),
        "bulk_ceiling": _disp.BULK_CEILING,
        # Whether the bulk bar may offer "Move to stage". Its own privilege, so
        # a screen that inferred it from the action list would offer a button
        # that 403s. Additive: a client that ignores it is unchanged.
        "bulk_stage_permitted": hxguard.user_allows(user, "case.actions.bulk_stage"),
        # Named so a screen can say WHY a button it expected is not there. A
        # missing control with no explanation reads as a bug.
        "stage": ({"id": stage.get("id"), "name": stage.get("name"),
                   "actions_allowed": list(_stg.allowed_actions(stage) or ())
                   if _stg.allowed_actions(stage) is not None else None,
                   "on_enter_status": _stg.on_enter_status(stage)}
                  if stage is not None else None),
    }



# ── SD-4: Role-gated PII field visibility ────────────────────────────────────

async def _get_pii_fields(session: AsyncSession, case_type_id: uuid.UUID) -> set[str]:
    """Return set of field names marked pii=True for a case type.

    FormFieldModel (the planned per-field PII registry) is not yet migrated.
    Until migration 042 adds that table, we return an empty set so callers
    get the permissive default (no redaction) rather than a 500.
    """
    return set()


def _has_sensitive_access(user: AuthenticatedUser) -> bool:
    """True if the user may see PII/sensitive case fields unredacted.

    Phase 4 step 2: was `is_admin or "finance" in roles or "admin" in roles`.
    Sync and boolean by design — the answer redacts a field, it does not refuse
    a request, so it must not write an `authz_denied` event (one per field per
    row would bury the real denials HxShield watches for).
    """
    return hxguard.user_allows(user, "finance.details.read")


async def _redact_data(
    data: dict | None,
    case_type_id: uuid.UUID | None,
    user: AuthenticatedUser,
    session: AsyncSession,
) -> dict | None:
    """Return case data with PII fields replaced by '***' for non-privileged callers."""
    if data is None or not data:
        return data
    if _has_sensitive_access(user) or case_type_id is None:
        return data
    pii_fields = await _get_pii_fields(session, case_type_id)
    if not pii_fields:
        return data
    return {k: "***" if k in pii_fields else v for k, v in data.items()}


# ─── Schemas for new endpoints ────────────────────────────────────────


LOCK_TTL_SECONDS = 1800  # 30 minutes


class InternalAssignmentCreate(BaseModel):
    step_id: str
    assignee_type: str = "queue"
    assignee_id: str = "default-queue"


class MyTaskResponse(BaseModel):
    assignment_id: str
    case_id: str
    case_number: str | None
    case_description: str | None
    case_priority: str | None
    stage_id: str
    step_id: str
    step_def: dict
    form_id: str | None
    completion: dict | None
    locked_by: str | None
    lock_expires_at: str | None
    is_locked_by_me: bool


# ─── Helpers ──────────────────────────────────────────────────────────


async def _get_case_or_404(
    session: AsyncSession, case_id: uuid.UUID, user: AuthenticatedUser | None = None,
    *, include_deleted: bool = False,
):
    """Load a case, refusing one that belongs to somebody else's tenant.

    `repo.get_case_instance` selects on the primary key and nothing else, so
    before this check the single-case read had **no tenant clause at all** —
    weaker than the list endpoint beside it, which at least filtered on
    `tenant_id = mine OR IS NULL`. Proven on 2026-09-05: tenant `dev` read
    tenant `default`'s case and its private field through this path.

    `user` is optional so existing internal callers keep working unchanged; when
    it is supplied the caller's tenant is enforced. A refusal is **404, not
    403** — 403 would confirm the case exists to somebody with no right to know
    that.

    A case with no tenant stays visible to everybody, exactly as in every
    released version, until an operator backfills and sets
    `VELARIS_CASE_TENANT_STRICT_READS`. See `tenancy.identity.case_visible_to`.

    **A soft-deleted case is a 404 too** (199), and for the same reason the
    tenant refusal is one: a deleted case that answers 403, or answers 200 with
    a `deleted` flag for the client to respect, is a case the platform is still
    showing. `include_deleted` is for the dispatcher and the restore path, which
    have to see it in order to act on it.
    """
    case = await repo.get_case_instance(session, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")
    if not include_deleted and bool(getattr(case, "is_deleted", False)):
        raise HTTPException(status_code=404, detail="Case not found")
    if user is not None:
        visible = await case_visible_to(
            session, case.tenant_id, getattr(user, "tenant_id", None),
            is_superadmin_caller=is_superadmin(user))
        if not visible:
            raise HTTPException(status_code=404, detail="Case not found")
    return case


async def _audit(
    session: AsyncSession,
    case_id: uuid.UUID,
    action: str,
    actor_id: str | None = None,
    **kwargs,
):
    await repo.append_audit_entry(
        session,
        data={
            "case_id": case_id,
            "action": action,
            "actor_id": actor_id,
            "actor_type": kwargs.pop("actor_type", "user"),
            "details": kwargs.pop("details", {}),
            "previous_value": kwargs.pop("previous_value", None),
            "new_value": kwargs.pop("new_value", None),
        },
    )


async def _signal_lifecycle(
    request: Request,
    case_id: uuid.UUID,
    signal: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Best-effort nudge to the case's Temporal workflows.

    Fans out to BOTH the SLA companion (`helix-case-…`) and the
    `definition_json` interpreter (`helix-interpreter-…`, #21). Fanning out
    here rather than adding a second call at every site is deliberate: there
    are six signal sites and growing, and a new one that forgot the
    interpreter would leave a case advancing only on the poll interval — a
    slow, intermittent bug that looks like nothing at all.

    Both workflows rescan/poll regardless, so a failed or skipped signal only
    delays; routes never fail because of Temporal.
    """
    client = getattr(request.app.state, "temporal_client", None)
    if client is None:
        return
    for workflow_id in (f"helix-case-{case_id}", f"helix-interpreter-{case_id}"):
        try:
            handle = client.get_workflow_handle(workflow_id)
            await handle.signal(signal, payload or {})
        except Exception as e:
            # Not every case has an interpreter running — a missing workflow
            # is the normal case, not an error.
            logger.debug(
                "Signal %s skipped for %s: %s", signal, workflow_id, e
            )


# ─── CRUD ─────────────────────────────────────────────────────────────


@router.post("", response_model=CaseResponse, status_code=201)
async def create_case(
    body: CaseCreate,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Create a new case instance and start its lifecycle workflow."""
    # Verify case type exists
    case_type = await repo.get_case_type(session, body.case_type_id)
    if case_type is None:
        raise HTTPException(status_code=404, detail="Case type not found")

    # Verify the case type belongs to this user's tenant or is global.
    # Prevents a user in tenant A from creating cases of tenant B's case types.
    # A tenant token may be the slug or the id; a literal string compare refused
    # slug-token users their OWN case types (2026-09-29).
    from case_service.tenancy.identity import same_tenant as _same_tenant
    if case_type.tenant_id is not None and user.tenant_id:
        if not await _same_tenant(session, case_type.tenant_id, user.tenant_id):
            raise HTTPException(
                status_code=403,
                detail="This case type does not belong to your tenant.",
            )

    # Who may create a case is decided by the access roles defined in Admin
    # Studio (2026-10-18). Until then any signed-in user could, a viewer
    # included. MCP's case_create comes through here too.
    from velaris_shared.hxguard.case_create import require_case_create
    await require_case_create(session, user, case_type.id)

    # Derive the case's tenant (case_instances.tenant_id is a nullable UUID):
    #   1. the acting user's tenant, when they have one;
    #   2. else inherit the case type's tenant — covers user-less creation
    #      (e.g. HxFusion: an external system opens a case after a process);
    #   3. else None — global / single-tenant cases are stored with NULL tenant.
    #
    # `user.tenant_id` comes from `user_directory`, where it is a **slug**
    # ("default", "dev"), not a UUID. `str(...)` therefore produced a non-UUID
    # string and the insert died in the GUID bind processor — an unhandled 500
    # on every case creation by any user who actually had a tenant. The comment
    # here already said it "must never be a non-UUID string"; nothing enforced
    # it. `resolve_tenant` is the module that exists for exactly this and takes
    # either shape.
    #
    # This matters beyond the crash: a case that falls through to NULL is read
    # by EVERY tenant, because the read filter is `tenant_id == mine OR
    # tenant_id IS NULL`. So failing to resolve a tenant does not merely lose a
    # label, it publishes the case platform-wide.
    effective_tenant_id: str | None = None
    if user.tenant_id:
        resolved = await resolve_tenant(session, user.tenant_id)
        if resolved is None:
            # Named a tenant that does not exist. Refuse rather than store the
            # case globally — deny is the safe reading of an unknown tenant.
            raise HTTPException(
                403, f"VEL-CASE-TENANT-403: {user.tenant_id!r} is not a tenant "
                     f"on this platform")
        effective_tenant_id = str(resolved[0])
    elif case_type.tenant_id is not None:
        effective_tenant_id = str(case_type.tenant_id)
    else:
        # A caller with no tenant claim IS the default tenant (`same_tenant`
        # reads it that way on every read). Until 2026-09-30 their case on a
        # shared case type was stored with NO tenant — readable by every tenant
        # on the install; 187 of 196 cases on the dev box are that. Where the
        # install has no default tenant row at all, NULL stays, as before.
        default = await resolve_tenant(session, DEFAULT_TENANT)
        if default is not None:
            effective_tenant_id = str(default[0])

    # Under strict reads a NULL-tenant case is visible to NOBODY — not even the
    # person who just created it. Writing one would be manufacturing a case that
    # instantly disappears, so the create is refused instead, naming the fix.
    # With strict off (the default, and every released version) this cannot fire.
    if effective_tenant_id is None and strict_case_reads():
        raise coded(
            403, CASE_TENANT_403,
            "This case would be stored with no tenant, and strict tenant reads "
            "are on, so nobody would be able to read it back. Give the acting "
            "user a tenant in the user directory, or give the case type an "
            "owner, before creating cases here.")

    # A tenant's `max_cases` (NULL = unlimited, every tenant before 2026-11-04).
    from velaris_shared.tenancy.limits import TenantLimitError, require_case_room
    try:
        await require_case_room(session, effective_tenant_id)
    except TenantLimitError as exc:
        raise HTTPException(403, str(exc)) from exc

    # Oct item 18a: an open rollout chooses the version for a NEW case —
    # sticky per customer, or per requester when there is no customer.
    from case_service.rollouts import service as _rollouts
    case_type, _rollout = await _rollouts.resolve_case_type(
        session, case_type=case_type, tenant=effective_tenant_id,
        subject=_rollouts.subject_of((body.data or {}).get("customer_id"),
                                     body.created_by or user.user_id),
        data=body.data)

    case = await repo.create_case_instance(
        session,
        data={
            "case_type_id": case_type.id,
            "case_type_version": case_type.version,
            "status": "new",
            "priority": body.priority or case_type.default_priority,
            "data": body.data,
            "parent_case_id": body.parent_case_id,
            "created_by": body.created_by or user.user_id,
            "tenant_id": effective_tenant_id,
        },
    )
    await _rollouts.record(session, case_id=case.id, res=_rollout,
                           tenant=effective_tenant_id, via="api")

    await _audit(
        session,
        case.id,
        "created",
        actor_id=body.created_by,
        details={"case_type": case_type.name, "priority": case.priority},
    )

    # Start case-level SLAs + initial urgency (synchronous path owns
    # SLA state; the Temporal companion only adds durable timers).
    try:
        await case_lifecycle.on_case_created(
            session,
            case_id=case.id,
            case_type_def=case_type.definition_json or {},
            case_type_id=case.case_type_id,
            tenant_id=effective_tenant_id,
        )
    except Exception as e:
        logger.warning("SLA start on case creation failed for %s: %s", case.id, e)

    # Start CaseLifecycleWorkflow via Temporal
    temporal_client = getattr(request.app.state, "temporal_client", None)
    if temporal_client is not None:
        try:
            from case_service.temporal.workflows.case_lifecycle_workflow import (
                CaseLifecycleWorkflow,
            )

            workflow_id = f"helix-case-{case.id}"
            await temporal_client.start_workflow(
                CaseLifecycleWorkflow.run,
                {
                    "case_id": str(case.id),
                    "case_type_id": str(case.case_type_id),
                },
                id=workflow_id,
                task_queue="helix-case-service",
            )

            await repo.update_case_instance(
                session,
                case.id,
                values={"process_instance_id": workflow_id},
            )

            logger.info(
                "Started lifecycle workflow %s for case %s",
                workflow_id, case.id,
            )

            # #21 interpreter — opt-in twice: platform flag AND the case type
            # declaring `orchestration: temporal`. Started alongside the
            # companion so a case is driven from birth; adopting a case
            # mid-flight also works, but starting late means the stages it
            # already passed were driven by the synchronous path instead.
            from velaris_shared.config import get_settings as _gs

            if (
                _gs().temporal_interpreter_enabled
                and str((case_type.definition_json or {}).get("orchestration") or "").lower()
                == "temporal"
            ):
                from case_service.temporal.workflows.case_interpreter_workflow import (
                    CaseInterpreterWorkflow,
                )

                await temporal_client.start_workflow(
                    CaseInterpreterWorkflow.run,
                    {"case_id": str(case.id)},
                    id=f"helix-interpreter-{case.id}",
                    task_queue="helix-case-service",
                )
                logger.info("Started interpreter workflow for case %s", case.id)
        except Exception as e:
            logger.warning(
                "Failed to start lifecycle workflow for case %s: %s",
                case.id, e,
            )
    else:
        logger.info(
            "Temporal not available — case %s created without "
            "auto-execution (manual lifecycle only)",
            case.id,
        )

    # Generate human-readable case number: HLX-{TYPE}-{NNNNNN}
    try:
        from velaris_shared.config import get_settings
        from velaris_shared.db.backends import get_backend
        # Per-dialect monotonic counter (PG sequence / MySQL atomic counter row).
        seq_val = await get_backend(get_settings().database_backend).next_case_seq(session)
        prefix = re.sub(r"[^A-Za-z]", "", case_type.name).upper()[:3].ljust(3, "X")
        case_number = f"HLX-{prefix}-{seq_val:06d}"
        await repo.update_case_instance(session, case.id, values={"case_number": case_number})
    except Exception as e:
        logger.warning("Case number generation failed for %s: %s", case.id, e)

    # Process mining event log (Phase 14)
    try:
        await event_logger.log_case_created(
            session, case_id=case.id, case_type_id=case.case_type_id,
            actor_id=body.created_by,
        )
    except Exception as e:
        logger.warning("Event log failed for case %s: %s", case.id, e)

    # Fire outbound connector rules for case_created
    await fire_outbound_rules(
        session,
        trigger_event="case_created",
        case_id=case.id,
        case_type_id=case.case_type_id,
        case_data=body.data or {},
        tenant_id="default",
    )

    # Queue webhook delivery via transactional outbox
    try:
        wh_payload = await _webhook_payload(session, case.id)
        await _dispatch_webhook(session, "case.created", wh_payload, case.case_type_id)
    except Exception as _e:
        logger.warning("outbox dispatch failed for case %s: %s", case.id, _e)

    # HxShield producer (roadmap #25). Three of its patterns —
    # duplicate_case_flood, dos_submission and field_value_anomaly — match on
    # `case_created` and nothing had ever written it, so they could not fire
    # however many rules were enabled. The detector hook on log_security_event
    # scores this; see hxshield/detector.py.
    try:
        from velaris_shared.db.security_events import log_security_event
        await log_security_event(
            session, event_type="case_created", severity="info",
            user_id=user.user_id, resource_type="case", resource_id=str(case.id),
            action="create", outcome="success",
            ip_address=request.client.host if request.client else None,
            details={"case_type_id": str(case.case_type_id),
                     "tenant_id": user.tenant_id},
        )
    except Exception as _e:  # noqa: BLE001
        # A detection producer must never cost a case its creation.
        logger.warning("security event for case %s not recorded: %s", case.id, _e)

    return await repo.get_case_instance(session, case.id)


@router.get("", response_model=CaseListResponse)
async def list_cases(
    status: str | None = None,
    priority: str | None = None,
    case_type_id: uuid.UUID | None = None,
    var: list[str] | None = Query(None, description=
        "Variable filter 'namespace.name:value' — repeatable, AND semantics. "
        "Only variables flagged indexed in the Case Designer are filterable."),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Search / list case instances. Auth added with HxGuard Phase B — this
    route previously returned every case (data blobs included) without a
    token. Per-user relationship FILTERING of the list is a Phase B
    follow-up (enforce-mode cutover)."""
    filters: dict = {}
    if status:
        filters["status"] = status
    if priority:
        filters["priority"] = priority
    if case_type_id:
        filters["case_type_id"] = case_type_id

    # Case Variables Phase 3: indexed variable filters
    variable_filters: list[tuple[str, str]] = []
    if var:
        from velaris_shared.db.models import CaseTypeVariableModel, VariableNamespaceModel
        from case_service.case_vars.service import NAME_RE, _effective_sensitivity
        for raw_filter in var[:10]:                      # sane cap
            full_key, sep, value = raw_filter.partition(":")
            ns_name, dot, var_name = full_key.partition(".")
            if not sep or not dot or not NAME_RE.match(ns_name) or not NAME_RE.match(var_name):
                raise coded(400, CASE_VAR_FILTER_400, f"var filter must be 'namespace.name:value', got {raw_filter!r}")
            idx_q = sa_select(CaseTypeVariableModel).where(
                CaseTypeVariableModel.full_key == full_key,
                CaseTypeVariableModel.indexed == True,  # noqa: E712
            )
            if case_type_id:
                idx_q = idx_q.where(CaseTypeVariableModel.case_type_id == case_type_id)
            indexed_def = (await session.execute(idx_q.limit(1))).scalar_one_or_none()
            if indexed_def is None:
                raise HTTPException(400, f"'{full_key}' is not an indexed variable — flag it in the Case Designer Variables panel first")
            # Equality filtering is a value oracle: it confirms a guessed
            # value even though reads come back masked. pii/secret variables
            # are therefore never filterable, indexed or not.
            ns_row = (await session.execute(
                sa_select(VariableNamespaceModel)
                .where(VariableNamespaceModel.name == ns_name).limit(1)
            )).scalar_one_or_none()
            if ns_row is not None and _effective_sensitivity(ns_row, indexed_def) in ("pii", "secret"):
                raise HTTPException(400, f"'{full_key}' is {_effective_sensitivity(ns_row, indexed_def)}-classified — sensitive variables are not filterable")
            variable_filters.append((full_key, value))

    # HxGuard enforce cutover: in enforce mode the list shows only cases the
    # user has a relationship with (owner / tuple / allowed case type).
    # Admin, manager, and "*"-scoped access groups see everything.
    accessible_to_user = None
    from velaris_shared.config import get_settings
    if (get_settings().hxguard_case_enforcement or "enforce").lower() == "enforce" \
            and not (hxguard.user_allows(user, "casework.sensitive.read")
                     or "superadmin" in (user.roles or [])
                     or "manager" in (user.roles or [])):
        from velaris_shared.db.models import AccessGroupModel, OperatorAccessGroupModel
        from velaris_shared.tenancy.home import foreign_group_ids
        _foreign = await foreign_group_ids(session, str(user.user_id))
        allowed_lists = [lst for gid, lst in (await session.execute(
            sa_select(AccessGroupModel.id, AccessGroupModel.allowed_case_type_ids)
            .join(OperatorAccessGroupModel,
                  OperatorAccessGroupModel.access_group_id == AccessGroupModel.id)
            .where(OperatorAccessGroupModel.operator_id == user.user_id)
            .where(AccessGroupModel.is_active == True)  # noqa: E712
        )).all() if gid not in _foreign]
        allowed_ids: list = []
        unrestricted = False
        for lst in allowed_lists:
            if "*" in (lst or []):
                unrestricted = True
                break
            allowed_ids.extend(uuid.UUID(x) for x in (lst or []) if x)
        if not unrestricted:
            accessible_to_user = (user.user_id, allowed_ids)

    # Tenant scope. This list had NO tenant clause at all, so every caller saw
    # every tenant's cases — the same hole as the single-case read, one page
    # wider. Superadmin keeps its documented break-glass; everybody else is
    # scoped to their own tenant, with NULL-tenant rows still included exactly
    # as before unless an operator has closed that branch.
    #
    # A caller with NO tenant claim is the platform-default tenant — never "every
    # tenant". Until 2026-09-17 the clause below was skipped for such a caller,
    # so on a multi-tenant install a user with no directory row listed every
    # tenant's cases, while the single-case read beside it (`same_tenant`)
    # already treated them as `default` and answered 404 on the same rows. The
    # list now follows the read. Where no `default` tenant exists, such a caller
    # sees only untenanted cases — again exactly what the read allows.
    scope_tenant = None
    if not is_superadmin(user):
        claim = getattr(user, "tenant_id", None)
        resolved_scope = await resolve_tenant(session, claim or DEFAULT_TENANT)
        if resolved_scope is None:
            if claim:
                # An unknown tenant matches nothing rather than everything.
                raise coded(403, CASE_TENANT_403,
                            f"{claim!r} is not a tenant on this platform")
            # No claim and no default tenant: no owned case is reachable.
            scope_tenant = _NO_TENANT
        else:
            scope_tenant = resolved_scope[0]

    cases, total = await repo.search_case_instances(
        session,
        filters=filters,
        offset=(page - 1) * page_size,
        limit=page_size,
        variable_filters=variable_filters or None,
        accessible_to_user=accessible_to_user,
        tenant_id=scope_tenant,
        include_untenanted=not strict_case_reads(),
    )
    return CaseListResponse(
        items=cases, total=total, page=page, page_size=page_size
    )


@router.get("/{case_id}", response_model=CaseResponse)
async def get_case(
    case_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    case = await _get_case_or_404(session, case_id, user)
    # HxGuard Phase B pilot (shadow by default — see hxguard_case_enforcement)
    from velaris_shared import hxguard
    await hxguard.require_case(session, user, "case.read", case_id)
    # case_vars façade read: typed variables (sensitivity-redacted in the
    # service) merged over the case.data blob. SD-4 redaction maps still
    # apply to the blob keys afterwards.
    ctx = case_vars.CallerContext(
        kind="platform", actor_id=user.user_id,
        privileged=_has_sensitive_access(user),
    )
    data = await case_vars.get_all(session, ctx, case_id)
    redacted = await _redact_data(data, case.case_type_id, user, session)
    response = CaseResponse.model_validate(case).model_dump()
    response["data"] = redacted
    return response


@router.patch("/{case_id}", response_model=CaseResponse)
async def update_case_data(
    case_id: uuid.UUID,
    body: CaseDataUpdate,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Merge-update the case's data payload."""
    case = await _get_case_or_404(session, case_id, user)

    # Sealed cases are inert — one shared rule in `case_actions.seal`,
    # because a rule enforced at each call site is one that is missing
    # at the call site somebody adds next year.
    from case_service.case_actions import seal as _seal
    _seal.assert_writable(case)
    # HxGuard Phase B pilot — also closes the pre-existing unauthenticated-
    # mutation gap (route previously had NO auth dependency)
    from velaris_shared import hxguard
    await hxguard.require_case(session, user, "case.update", case_id)
    # Phase 4 guard: a promoted key's blob value is no longer served by the
    # façade — writing it here would vanish silently. Reject with a pointer.
    if body.data:
        from velaris_shared.db.models import CaseTypeVariableModel
        promoted = (await session.execute(
            sa_select(CaseTypeVariableModel.promoted_source, CaseTypeVariableModel.full_key)
            .where(CaseTypeVariableModel.case_type_id == case.case_type_id)
            .where(CaseTypeVariableModel.promoted_source.in_(list(body.data.keys())))
        )).all()
        if promoted:
            detail = ", ".join(f"'{s}' is promoted to {fk}" for s, fk in promoted)
            raise HTTPException(400, f"{detail} — these keys are typed variables now; write them through the owning integration or the variables API")
    merged = {**case.data, **body.data}
    await repo.update_case_instance(
        session, case_id, values={"data": merged}
    )
    await _audit(
        session,
        case_id,
        "data_updated",
        actor_id=body.updated_by,
        previous_value=case.data,
        new_value=merged,
    )

    # Phase 22: Real-time broadcast
    try:
        await publish_case_event(
            case_id, "data_updated",
            data={"updated_fields": list(body.data.keys())},
            actor_id=body.updated_by,
        )
    except Exception:
        pass

    # Queue webhook delivery via transactional outbox — variable NAMES only,
    # values never leave the API boundary in a webhook
    try:
        wh_payload = await _webhook_payload(session, case_id,
            extra={"changed_variables": sorted(body.data.keys())})
        await _dispatch_webhook(session, "case.data_updated", wh_payload,
            case.case_type_id)
    except Exception as _e:
        logger.warning("outbox dispatch failed for data update %s: %s", case_id, _e)

    return await repo.get_case_instance(session, case_id)


# ─── Status Changes ──────────────────────────────────────────────────


async def apply_status_change(
    session: AsyncSession,
    case_id: uuid.UUID,
    new_status: str,
    actor_id: str | None,
    reason: str | None = None,
    audit_action: str = "status_changed",
) -> None:
    """The FULL status-change machinery — audit, SLA lifecycle, realtime,
    outbox webhook. Used by the status endpoint AND rule-action application
    (velaris.status writes route here; state never changes silently)."""
    case = await _get_case_or_404(session, case_id)
    prev = case.status
    await repo.update_case_instance(
        session, case_id, values={"status": new_status}
    )
    # `audit_action` exists so the dispatcher can record the verb its action
    # has always recorded — `/close` wrote `closed`, `/resolve` wrote
    # `resolved` — WITHOUT a second audit row being written on top of this one.
    # An action that produced two entries would double every status change in
    # the log an auditor reads, and `change_status` would have produced two
    # identical ones.
    await _audit(
        session,
        case_id,
        audit_action,
        actor_id=actor_id,
        previous_value={"status": prev},
        new_value={"status": new_status},
        details={"reason": reason},
    )
    await case_lifecycle.on_status_changed(
        session, case_id=case_id, old_status=prev, new_status=new_status
    )

    # HxSentinel: a closing case is the moment its data and document
    # verifications are final, so it is when its signals are worth extracting.
    # No-ops with zero DB work when HxSentinel is off, and fails open.
    from case_service.sentinel import extraction as _risk_extraction
    await _risk_extraction.extract_on_close(session, case_id, new_status)

    # Phase 22: Real-time broadcast
    try:
        await publish_case_event(
            case_id, "status_changed",
            data={"old_status": prev, "new_status": new_status},
            actor_id=actor_id,
        )
    except Exception:
        pass

    # Queue webhook delivery via transactional outbox
    try:
        case_obj = await repo.get_case_instance(session, case_id)
        wh_payload = await _webhook_payload(session, case_id,
            extra={"old_status": prev, "new_status": new_status})
        await _dispatch_webhook(session, "case.status_changed", wh_payload,
            case_obj.case_type_id if case_obj else None)
    except Exception as _e:
        logger.warning("outbox dispatch failed for status change %s: %s", case_id, _e)


# `POST /{case_id}/status` was here. It is now the `change_status` action on the
# dispatcher below — and the difference is not the URL, it is that the target is
# validated against the configured status set instead of any string being
# accepted. `apply_status_change` above is unchanged and is still what runs.


async def apply_priority_change(
    session: AsyncSession,
    case_id: uuid.UUID,
    new_priority: str,
    actor_id: str | None,
    audit_action: str = "priority_changed",
) -> None:
    """Full priority-change machinery — shared by the endpoint and
    rule-action application (velaris.priority writes route here)."""
    case = await _get_case_or_404(session, case_id)
    prev = case.priority
    await repo.update_case_instance(
        session, case_id, values={"priority": new_priority}
    )
    await _audit(
        session,
        case_id,
        audit_action,
        actor_id=actor_id,
        previous_value={"priority": prev},
        new_value={"priority": new_priority},
    )
    await case_lifecycle.on_priority_changed(
        session, case_id=case_id, new_priority=new_priority
    )

    # Phase 22: Real-time broadcast
    try:
        await publish_case_event(
            case_id, "priority_changed",
            data={"old_priority": prev, "new_priority": new_priority},
            actor_id=actor_id,
        )
    except Exception:
        pass

    # Queue webhook delivery via transactional outbox
    try:
        case_obj = await repo.get_case_instance(session, case_id)
        wh_payload = await _webhook_payload(session, case_id,
            extra={"old_priority": prev, "new_priority": new_priority})
        await _dispatch_webhook(session, "case.priority_changed", wh_payload,
            case_obj.case_type_id if case_obj else None)
    except Exception as _e:
        logger.warning("outbox dispatch failed for priority change %s: %s", case_id, _e)


# `POST /{case_id}/priority` was here — now the `change_priority` action.
# `apply_priority_change` above is unchanged and is still what runs.


#: A tenant id no tenant has: scoping a search to it leaves only untenanted
#: rows (and none at all under strict reads).
_NO_TENANT = uuid.UUID(int=0)


class StageMoveRefused(Exception):
    """A case that may not be moved, with the code a bulk result reports.

    The single-case route turns it back into exactly the response it always
    gave; the bulk route reports it against the one case and carries on.
    """

    def __init__(self, status_code: int, code: str, detail: str,
                 original: HTTPException | None = None) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail
        #: The exception a shared check raised, so the single-case route can
        #: re-raise it unchanged rather than rebuild an approximation of it.
        self.original = original


#: "The caller did not resolve the tenant" — distinct from None, which is a
#: real answer (a caller in no tenant).
_UNSET_TENANT = object()


async def _stage_move_gate(
    session: AsyncSession,
    user: AuthenticatedUser,
    case_id: uuid.UUID,
    target_stage_id: str,
    *,
    consult_policy: bool = True,
):
    """Everything that decides whether THIS caller may move THIS case. Writes nothing
    — except the HxArbiter decision log, which is why `preview` passes
    `consult_policy=False`: a preview that logged a policy decision for a move
    nobody made would be a false record.

    Shared by `POST /{case_id}/stage` and the bulk route, so a gate added here
    holds for both. That is the point: the bulk path is a loop over this, not a
    second copy of it.
    """
    try:
        case = await _get_case_or_404(session, case_id, user)
        # same case-level check the MCP case_advance_stage tool applies
        from velaris_shared import hxguard
        await hxguard.require_case(session, user, "case.update", case_id)
    except HTTPException as exc:
        raise StageMoveRefused(
            exc.status_code,
            getattr(exc, "code", None) or ("VEL-CASE-NOTFOUND-404" if exc.status_code == 404
                                           else "VEL-CASE-ACTION-DENIED-403"),
            str(exc.detail), original=exc)
    # Sealed cases are inert. Shared rule, not a local copy —
    # `case_actions.seal`. This route wrote happily to an archived
    # case until 2026-09-08.
    from case_service.case_actions import seal as _seal
    sealed = _seal.sealed_reason(case)
    if sealed:
        raise StageMoveRefused(409, _seal.CODE, sealed)

    # HxSentinel verify gate (opt-in per tenant): refuse a manual advance while
    # an open risk hold exists. 409, not a silent no-op, so the operator sees
    # WHY. Fails OPEN (off/fault -> not blocked); a human clears the hold first.
    from case_service.sentinel import engine as _risk_engine
    if await _risk_engine.advance_blocked(session, case_id):
        raise StageMoveRefused(
            409, "VEL-CASE-STAGE-HELD-409",
            "Stage advance is held by an open HxSentinel risk verification. "
            "Clear the hold on the case's risk panel first.")

    if not consult_policy:
        return case

    # ── HxArbiter PEP (ARB-D) ────────────────────────────────────────────────
    # HxGuard has already decided whether this CALLER may update the case. The
    # engine answers a different question — whether this TRANSITION is permitted
    # for this case in this tenant — which is the thing a policy can express and
    # a role cannot. NG1 holds: it can only further restrict what HxGuard
    # allowed, never grant, because it is asked after the gate above and never
    # instead of it. The `case` namespace defaults to ALLOW, so an install with
    # no policy sees no change at all, and the namespace is clamped to shadow on
    # top of that.
    try:
        from case_service.hxarbiter import pep as _arb_pep

        allowed = await _arb_pep.check(
            session,
            tenant_id=str(getattr(case, "tenant_id", None) or "default"),
            action=_arb_pep.ACTION_STAGE_TRANSITION,
            subject_id=str(user.user_id),
            subject_kind=_arb_pep.SubjectKind.USER,
            resource_kind="case",
            resource_id=str(case_id),
            resource_attrs={"case_type": str(case.case_type_id)},
            context={"from_stage": str(case.current_stage_id or ""),
                     "to_stage": str(target_stage_id),
                     "purpose": "stage_transition"},
            legacy_allowed=True,
            claimed_by=str(getattr(user, "username", None) or user.user_id)[:255],
        )
    except Exception as _arb_exc:                               # noqa: BLE001
        logger.warning("cases: HxArbiter stage PEP unavailable: %s", _arb_exc)
        allowed = True
    if not allowed:
        raise StageMoveRefused(
            403, "VEL-ARB-STAGE-DENIED-403",
            "VEL-ARB-STAGE-DENIED-403: policy refuses this stage "
            "transition for this case.")
    return case


async def _stage_move_apply(
    request: Request,
    session: AsyncSession,
    user: AuthenticatedUser,
    case,
    target_stage_id: str,
    *,
    ct_def: dict | None = None,
    tenant_uuid: Any = _UNSET_TENANT,
    signal: bool = True,
    audit_details: dict | None = None,
) -> dict | None:
    """Move the case and fire what entering the stage fires. Returns the stage
    definition entered, or None when the case type does not define it.

    `ct_def` and `tenant_uuid` let a bulk run resolve each once rather than once
    per case; `signal=False` lets it nudge Temporal after the loop, under a
    budget, instead of two round trips per case inside a transaction.
    """
    case_id = case.id
    prev = case.current_stage_id
    await repo.update_case_instance(
        session, case_id, values={"current_stage_id": target_stage_id}
    )
    await _audit(
        session,
        case_id,
        "stage_transitioned",
        actor_id=user.user_id,
        previous_value={"stage_id": prev},
        new_value={"stage_id": target_stage_id},
        # Absent on the single-case route, exactly as before. A bulk run names
        # itself, so one move can be told apart from five hundred.
        **({"details": audit_details} if audit_details else {}),
    )

    # Start the new stage's SLA (idempotent) and nudge the SLA companion.
    # Bound before the try because the block below reads it: a failure loading
    # the case type must leave `stage` a real answer, not unbound.
    stage = None
    try:
        if ct_def is None:
            ct = await repo.get_case_type(session, case.case_type_id)
            ct_def = (ct.definition_json or {}) if ct else {}
        stage = next(
            (s for s in ct_def.get("stages", []) if s["id"] == target_stage_id),
            None,
        )
        if stage is not None:
            await case_lifecycle.start_stage_slas(
                session,
                case_id=case_id,
                case_type_id=case.case_type_id,
                stage=stage,
                case_type_def=ct_def,
                tenant_id=getattr(case, "tenant_id", None),
            )
    except Exception as e:
        logger.warning("Stage SLA start failed for case %s: %s", case_id, e)
    # The stage's own `on_enter_status`, applied AFTER the stage pointer has
    # moved and the stage's SLAs have started — so the status change the
    # designer configured is a consequence of being in the stage, in that order,
    # and never a reason the stage move failed.
    try:
        from case_service.case_actions import stages as _stages

        if stage is not None:
            await _stages.apply_on_enter_status(
                session, case_id=case_id, stage=stage,
                actor=str(user.user_id),
                tenant_uuid=(await _action_context(session, user)
                             if tenant_uuid is _UNSET_TENANT else tenant_uuid))
    except Exception as e:                                       # noqa: BLE001
        logger.warning("stage on_enter_status failed for case %s: %s", case_id, e)

    if signal:
        await _signal_lifecycle(
            request, case_id, "stage_entered", {"stage_id": target_stage_id}
        )
    await emit_trace(
        "stage_transition",
        {"from_stage": prev, "to_stage": target_stage_id},
        case_id=case_id,
        tenant_id=getattr(user, "tenant_id", None) or "default",  # 8k
        actor_user_id=user.user_id,
        session=session,
    )
    return stage


# ── bulk STAGE transition (2026-09-17) ───────────────────────────────────────
#
# Registered BEFORE `/{case_id}/stage`: Starlette matches paths in order, and
# `/{case_id}/stage` would otherwise claim `/bulk/stage` and 422 on the UUID.
#
# Why this is two routes and a token rather than one route with a flag: see
# `case_actions/bulk_stage.py`. In one line — a stage move fires things per case
# that moving back does not undo, so what the operator confirmed is checked by
# the server, not by a dialog.

class BulkStageRequest(BaseModel):
    case_ids: list[uuid.UUID]
    target_stage_id: str = Field(min_length=1, max_length=255)
    #: Execute only — the token `preview` returned for exactly these cases.
    confirmation_token: str | None = Field(default=None, max_length=512)
    #: Recorded on every case's audit row. Bounded: it is written 500 times.
    reason: str | None = Field(default=None, max_length=2000)


async def _bulk_stage_prepare(
    body: BulkStageRequest, session: AsyncSession, user: AuthenticatedUser,
) -> list[uuid.UUID]:
    """What is wrong with the REQUEST, before any case is read."""
    from case_service.case_actions import bulk_stage as _bs
    from case_service.case_actions import dispatcher as _disp

    case_ids: list[uuid.UUID] = list(dict.fromkeys(body.case_ids))
    if len(case_ids) > _disp.BULK_CEILING:
        raise coded(400, "VEL-CASE-BULK-CEILING-400",
                    f"This request names {len(case_ids)} cases; the ceiling is "
                    f"{_disp.BULK_CEILING}. Narrow it or split it — acting on the "
                    f"first {_disp.BULK_CEILING} and reporting success is the "
                    f"worse answer.")
    if not case_ids:
        raise coded(400, "VEL-CASE-BULK-EMPTY-400", "No cases named.")
    # Once, not per case: it is a question about the operator, and asking it
    # before any case is read means a caller without it learns nothing about
    # the ids it sent — not even which exist.
    try:
        await hxguard.require_user(session, user, _bs.PRIVILEGE)
    except HTTPException:
        raise coded(403, "VEL-CASE-BULK-STAGE-DENIED-403",
                    f"Moving cases through a stage in bulk needs {_bs.PRIVILEGE}.")
    return case_ids


def _case_type_memo(session: AsyncSession):
    """Case type definitions, read once per case type across a run."""
    cache: dict[Any, tuple[dict, str | None]] = {}

    async def get(case_type_id) -> tuple[dict, str | None]:
        if case_type_id not in cache:
            ct = await repo.get_case_type(session, case_type_id)
            cache[case_type_id] = (((ct.definition_json or {}) if ct else {}),
                                   (ct.name if ct else None))
        return cache[case_type_id]

    return get


async def _bulk_stage_evaluate(
    session: AsyncSession, user: AuthenticatedUser, case_id: uuid.UUID,
    target_stage_id: str, ct_for, *, consult_policy: bool,
):
    """(case, case type definition, stage, unchanged) or StageMoveRefused."""
    case = await _stage_move_gate(session, user, case_id, target_stage_id,
                                  consult_policy=consult_policy)
    ct_def, _ = await ct_for(case.case_type_id)
    stage = next((s for s in ct_def.get("stages", []) or []
                  if isinstance(s, dict) and str(s.get("id")) == str(target_stage_id)),
                 None)
    if stage is None:
        # Stricter than the single-case route, which writes any string. A bulk
        # run spans case types, and a stage id that means something in one and
        # nothing in another would strand those cases in a stage with no steps.
        raise StageMoveRefused(
            409, "VEL-CASE-BULK-STAGE-UNKNOWN-409",
            f"'{target_stage_id}' is not a stage of this case's case type.")
    unchanged = str(case.current_stage_id or "") == str(target_stage_id)
    return case, ct_def, stage, unchanged


def _refused(case_id: uuid.UUID, exc: StageMoveRefused) -> dict:
    return {"case_id": str(case_id), "ok": False, "code": exc.code, "error": exc.detail}


@router.post("/bulk/stage/preview")
async def preview_bulk_stage(
    body: BulkStageRequest,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """What moving these cases into a stage WOULD do. Changes nothing.

    Returns, per case, whether it would move; per case type, what entering the
    stage fires (the SLA it starts, the status it sets); and a confirmation
    token for exactly the cases that would move. HxArbiter is consulted at
    execution only — see `_stage_move_gate`.
    """
    from case_service.case_actions import bulk_stage as _bs
    from case_service.case_actions import stages as _stg

    case_ids = await _bulk_stage_prepare(body, session, user)
    ct_for = _case_type_memo(session)

    results: list[dict] = []
    eligible: list[uuid.UUID] = []
    by_type: dict[Any, dict] = {}
    for case_id in case_ids:
        try:
            case, ct_def, stage, unchanged = await _bulk_stage_evaluate(
                session, user, case_id, body.target_stage_id, ct_for,
                consult_policy=False)
        except StageMoveRefused as exc:
            results.append(_refused(case_id, exc))
            continue
        results.append({"case_id": str(case_id), "ok": True,
                        "from_stage": case.current_stage_id,
                        "unchanged": unchanged})
        if unchanged:
            continue
        eligible.append(case_id)
        key = case.case_type_id
        if key not in by_type:
            _, ct_name = await ct_for(key)
            sla_id = stage.get("sla_policy_id")
            sla = next((p for p in ct_def.get("sla_policies", []) or []
                        if isinstance(p, dict) and p.get("id") == sla_id), None)
            by_type[key] = {
                "case_type_id": str(key), "case_type_name": ct_name,
                "stage_name": stage.get("name") or body.target_stage_id,
                "cases": 0,
                # What entering the stage FIRES, named so the confirm screen can
                # say it rather than an operator having to know the designer.
                "starts_sla": ((sla or {}).get("name") or sla_id) if sla_id else None,
                "on_enter_status": _stg.on_enter_status(stage),
                "signals_workflows": True,
            }
        by_type[key]["cases"] += 1

    token, expires_at = (_bs.mint(str(user.user_id), body.target_stage_id, eligible)
                         if eligible else (None, None))
    return {
        "target_stage_id": body.target_stage_id,
        "requested": len(case_ids),
        "would_move": len(eligible),
        "unchanged": sum(1 for r in results if r.get("unchanged")),
        "refused": sum(1 for r in results if not r["ok"]),
        "effects": list(by_type.values()),
        "results": results,
        # Confirms `eligible_case_ids` and nothing else: execute must send
        # exactly that set back.
        "eligible_case_ids": [str(c) for c in eligible],
        "confirmation_token": token,
        "confirm_expires_at": expires_at,
    }


async def _flush_stage_signals(
    request: Request, entries: list[tuple[uuid.UUID, str]],
) -> None:
    """`_flush_signals`' budget and concurrency, for `stage_entered`. Skipping a
    signal only delays: both workflows rescan on their own."""
    if not entries or getattr(request.app.state, "temporal_client", None) is None:
        return

    import asyncio

    sem = asyncio.Semaphore(_SIGNAL_CONCURRENCY)

    async def one(case_id: uuid.UUID, stage_id: str) -> None:
        async with sem:
            await _signal_lifecycle(request, case_id, "stage_entered",
                                    {"stage_id": stage_id})

    try:
        await asyncio.wait_for(
            asyncio.gather(*(one(c, s) for c, s in entries), return_exceptions=True),
            timeout=_SIGNAL_BUDGET_SECONDS)
    except asyncio.TimeoutError:
        logger.warning(
            "bulk stage: workflow nudges exceeded %.0fs for %d cases — the moves "
            "are committed; the workflows pick them up on their own rescan.",
            _SIGNAL_BUDGET_SECONDS, len(entries))


@router.post("/bulk/stage")
async def bulk_stage_transition(
    body: BulkStageRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Move the cases a preview confirmed into a stage.

    Refused as a whole — before any case is touched — without a confirmation
    token for exactly this operator, stage and set of cases. Then every case is
    checked again, because the preview was a moment ago: a case that was held,
    sealed or re-shared since is refused individually, and partial success is
    reported per case, never hidden.
    """
    from case_service.case_actions import bulk_stage as _bs

    case_ids = await _bulk_stage_prepare(body, session, user)
    why = _bs.verify(body.confirmation_token, str(user.user_id),
                     body.target_stage_id, case_ids)
    if why is not None:
        raise coded(409, "VEL-CASE-BULK-STAGE-CONFIRM-409", why)

    ct_for = _case_type_memo(session)
    tenant_uuid = await _action_context(session, user)
    run_id = str(uuid.uuid4())
    audit_details = {"bulk_run_id": run_id, "requested": len(case_ids)}
    if body.reason:
        audit_details["reason"] = body.reason

    results: list[dict] = []
    to_signal: list[tuple[uuid.UUID, str]] = []
    for case_id in case_ids:
        try:
            case, ct_def, stage, unchanged = await _bulk_stage_evaluate(
                session, user, case_id, body.target_stage_id, ct_for,
                consult_policy=True)
        except StageMoveRefused as exc:
            results.append(_refused(case_id, exc))
            continue
        if unchanged:
            # Already there — entering again would restart nothing useful and
            # write a second audit row. Reported, not counted as moved.
            results.append({"case_id": str(case_id), "ok": True,
                            "detail": {"unchanged": True}})
            continue
        prev = case.current_stage_id
        try:
            # A savepoint per case: a database error part way through one case's
            # move rolls back THAT case — its pointer, audit row and SLA — and
            # leaves the transaction usable for the rest. Without it one failure
            # poisons the session, every later case fails, and the commit at the
            # end can take the successful moves down with it while the response
            # already said they moved.
            async with session.begin_nested():
                await _stage_move_apply(
                    request, session, user, case, body.target_stage_id,
                    ct_def=ct_def, tenant_uuid=tenant_uuid, signal=False,
                    audit_details=audit_details)
        except Exception as exc:                                 # noqa: BLE001
            logger.exception("bulk stage move failed on %s", case_id)
            results.append({"case_id": str(case_id), "ok": False,
                            "code": "VEL-CASE-ACTION-FAILED-500", "error": str(exc)})
            continue
        results.append({"case_id": str(case_id), "ok": True,
                        "detail": {"from_stage": prev, "to_stage": body.target_stage_id}})
        to_signal.append((case_id, body.target_stage_id))

    await _flush_stage_signals(request, to_signal)

    moved = len(to_signal)
    failed = sum(1 for r in results if not r["ok"])
    return {
        "target_stage_id": body.target_stage_id,
        "bulk_run_id": run_id,
        "requested": len(case_ids),
        "moved": moved,
        "unchanged": len(results) - moved - failed,
        "failed": failed,
        "results": results,
    }


@router.post("/{case_id}/stage", response_model=CaseResponse)
async def transition_stage(
    case_id: uuid.UUID,
    body: CaseStageTransition,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    try:
        case = await _stage_move_gate(session, user, case_id, body.target_stage_id)
    except StageMoveRefused as exc:
        if exc.original is not None:
            # The exact response this route gave before the gate was shared.
            raise exc.original
        raise coded(exc.status_code, exc.code, exc.detail)

    await _stage_move_apply(request, session, user, case, body.target_stage_id)
    return await repo.get_case_instance(session, case_id)


# `POST /{case_id}/resolve`, `/close`, `/reopen` and `/cancel` were here.
# They are now the `resolve`, `close`, `reopen` and `cancel` actions on the
# dispatcher below. Each did the same four things — set a status, stamp a
# timestamp, write an audit row, signal the workflow — with four slightly
# different spellings, and `/reopen` alone remembered to restart the lifecycle
# workflow that exits on a terminal status. That restart now happens once, in
# the dispatcher, for every action that leaves a terminal status.


# ─── Assignments (for workflow activities) ────────────────────────────


@router.post(
    "/{case_id}/assignments",
    response_model=AssignmentResponse,
    status_code=201,
)
async def create_assignment(
    case_id: uuid.UUID,
    body: InternalAssignmentCreate,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Create a work-item assignment for a case step.

    In-process stage execution uses core/assignment_router directly; this
    HTTP surface is for operators and requires case.update on the case.
    """
    case = await _get_case_or_404(session, case_id, user)
    from velaris_shared import hxguard
    await hxguard.require_case(session, user, "case.update", case_id)
    # Sealed cases are inert. Shared rule, not a local copy —
    # `case_actions.seal`. This route wrote happily to an archived
    # case until 2026-09-08.
    from case_service.case_actions import seal as _seal
    _seal.assert_writable(case)


    assignment = await repo.create_assignment(
        session,
        data={
            "case_id": case_id,
            "step_id": body.step_id,
            "assignee_type": body.assignee_type,
            "assignee_id": body.assignee_id,
        },
    )

    await _audit(
        session,
        case_id,
        "assignment_created",
        actor_type="system",
        details={
            "step_id": body.step_id,
            "assignee_type": body.assignee_type,
            "assignee_id": body.assignee_id,
        },
    )

    return assignment


@router.get(
    "/{case_id}/assignments",
    response_model=list[AssignmentResponse],
)
async def list_case_assignments(
    case_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """List all assignments for a case."""
    await _get_case_or_404(session, case_id, user)
    from velaris_shared import hxguard
    await hxguard.require_case(session, user, "case.read", case_id)
    return await repo.get_assignments_for_case(session, case_id)


# ─── History ──────────────────────────────────────────────────────────


@router.get(
    "/{case_id}/history", response_model=list[AuditEntryResponse]
)
async def get_case_history(
    case_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    await _get_case_or_404(session, case_id, user)
    # same posture as get_case — a timeline reveals as much as the case itself
    from velaris_shared import hxguard
    await hxguard.require_case(session, user, "case.read", case_id)
    return await repo.get_audit_log(session, case_id)


# ─── Relationships ────────────────────────────────────────────────────


@router.post(
    "/{case_id}/relationships",
    response_model=RelationshipResponse,
    status_code=201,
)
async def add_relationship(
    case_id: uuid.UUID,
    body: RelationshipCreate,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    case = await _get_case_or_404(session, case_id, user)
    await _get_case_or_404(session, body.target_case_id, user)
    # mirror of the MCP case_link tool: update on source, read on target
    from velaris_shared import hxguard
    await hxguard.require_case(session, user, "case.update", case_id)
    # Sealed cases are inert. Shared rule, not a local copy —
    # `case_actions.seal`. This route wrote happily to an archived
    # case until 2026-09-08.
    from case_service.case_actions import seal as _seal
    _seal.assert_writable(case)

    await hxguard.require_case(session, user, "case.read", body.target_case_id)
    rel = await repo.create_relationship(
        session,
        data={
            "source_case_id": case_id,
            "target_case_id": body.target_case_id,
            "relationship_type": body.relationship_type,
            "propagate_status": body.propagate_status,
            "propagate_priority": body.propagate_priority,
            "required": body.required,
        },
    )
    await _audit(
        session,
        case_id,
        "relationship_added",
        details={
            "target_case_id": str(body.target_case_id),
            "type": body.relationship_type,
        },
    )
    return rel


@router.get(
    "/{case_id}/relationships",
    response_model=list[RelationshipResponse],
)
async def list_relationships(
    case_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    await _get_case_or_404(session, case_id, user)
    from velaris_shared import hxguard
    await hxguard.require_case(session, user, "case.read", case_id)
    return await repo.get_relationships(session, case_id)


# ─── Child Cases ──────────────────────────────────────────────────────


@router.post(
    "/{case_id}/children", response_model=CaseResponse, status_code=201
)
async def create_child_case(
    case_id: uuid.UUID,
    body: CaseCreate,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Create a child case linked to the parent."""
    parent = await _get_case_or_404(session, case_id, user)
    from velaris_shared import hxguard
    await hxguard.require_case(session, user, "case.update", case_id)
    # Sealed cases are inert. Shared rule, not a local copy —
    # `case_actions.seal`. This route wrote happily to an archived
    # case until 2026-09-08.
    from case_service.case_actions import seal as _seal
    _seal.assert_writable(parent)

    body.parent_case_id = case_id
    case_type = await repo.get_case_type(session, body.case_type_id)
    if case_type is None:
        raise HTTPException(status_code=404, detail="Case type not found")
    # Found 2026-09-29 (item 18a): this path checked neither. Another tenant's
    # case type was usable here, and the child was stored with NO tenant —
    # which every tenant can read. The child lives where its parent lives.
    from case_service.tenancy.identity import same_tenant as _same_tenant
    if case_type.tenant_id is not None and user.tenant_id and \
            not await _same_tenant(session, case_type.tenant_id, user.tenant_id):
        raise HTTPException(status_code=404, detail="Case type not found")
    # A child is a new case: the same access-group rule as POST /cases.
    from velaris_shared.hxguard.case_create import require_case_create
    await require_case_create(session, user, case_type.id)

    from case_service.rollouts import service as _rollouts
    case_type, _rollout = await _rollouts.resolve_case_type(
        session, case_type=case_type, tenant=parent.tenant_id,
        subject=_rollouts.subject_of((body.data or {}).get("customer_id"),
                                     (parent.data or {}).get("customer_id")
                                     if isinstance(parent.data, dict) else None,
                                     parent.created_by, body.created_by),
        data=body.data)

    child = await repo.create_case_instance(
        session,
        data={
            "case_type_id": case_type.id,
            "case_type_version": case_type.version,
            "status": "new",
            "priority": body.priority or case_type.default_priority,
            "data": body.data,
            "parent_case_id": case_id,
            "created_by": body.created_by or user.user_id,
            "tenant_id": parent.tenant_id,
        },
    )
    await _rollouts.record(session, case_id=child.id, res=_rollout,
                           tenant=parent.tenant_id, via="child")

    await repo.create_relationship(
        session,
        data={
            "source_case_id": case_id,
            "target_case_id": child.id,
            "relationship_type": "child",
            "propagate_status": True,
        },
    )

    await _audit(
        session,
        case_id,
        "child_case_created",
        actor_id=body.created_by,
        details={"child_case_id": str(child.id)},
    )

    return child


@router.get(
    "/{case_id}/children", response_model=list[CaseResponse]
)
async def list_child_cases(
    case_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    await _get_case_or_404(session, case_id, user)
    from velaris_shared import hxguard
    await hxguard.require_case(session, user, "case.read", case_id)
    children, _ = await repo.search_case_instances(
        session,
        filters={"parent_case_id": case_id},
        limit=200,
    )
    return children


# ── P38: Step Completions ─────────────────────────────────────────────────────

async def _auto_advance_if_complete(
    session: AsyncSession,
    case_id: uuid.UUID,
    stage_id: str,
    case_type_def: dict,
) -> tuple[str | None, bool]:
    """Check if all required steps in stage_id are complete; if so advance to next stage.

    Returns ``(new_stage_id, held_by_risk)``: the new stage id if it advanced,
    else None; and whether an open HxSentinel hold is what stopped it. The
    caller needs those apart — "held" and "steps outstanding" are both a
    non-advance, but only one of them is something a human must act on.
    """
    from sqlalchemy import select
    from velaris_shared.db.models import CaseStepCompletionModel

    stages = sorted(
        case_type_def.get("stages", []),
        key=lambda s: s.get("order", 0),
    )
    current_stage = next((s for s in stages if s["id"] == stage_id), None)
    if current_stage is None:
        return None, False

    required_step_ids = {
        s["id"] for s in current_stage.get("steps", [])
        if s.get("required", True) and s.get("step_type") != "automated"
    }
    if not required_step_ids:
        return None, False  # no required steps → don't auto-advance (avoid infinite loops)

    completed = (await session.execute(
        select(CaseStepCompletionModel).where(
            CaseStepCompletionModel.case_id == case_id,
            CaseStepCompletionModel.stage_id == stage_id,
            CaseStepCompletionModel.status == "completed",
        )
    )).scalars().all()

    completed_ids = {c.step_id for c in completed}
    if not required_step_ids.issubset(completed_ids):
        return None, False  # not all required steps done yet

    # HxSentinel verify gate (opt-in per tenant): an open risk hold blocks the
    # advance until a human clears it. Fails OPEN — a HxSentinel fault never
    # wedges the workflow, and it short-circuits with no DB work when off.
    from case_service.sentinel import engine as _risk_engine
    if await _risk_engine.advance_blocked(session, case_id):
        await _audit(session, case_id, "stage_advance_held_by_risk",
                     new_value={"stage_id": stage_id})
        return None, True

    # Find the next stage
    current_idx = next((i for i, s in enumerate(stages) if s["id"] == stage_id), -1)
    if current_idx < 0 or current_idx >= len(stages) - 1:
        return None, False  # already at last stage

    next_stage_id = stages[current_idx + 1]["id"]
    await repo.update_case_instance(session, case_id, values={"current_stage_id": next_stage_id})
    await _audit(
        session, case_id, "stage_auto_advanced",
        previous_value={"stage_id": stage_id},
        new_value={"stage_id": next_stage_id},
    )

    # Start the new stage's SLA here so every auto-advance caller
    # (step completion, payments, kyc, crm, ...) gets it.
    try:
        await case_lifecycle.start_stage_slas(
            session,
            case_id=case_id,
            case_type_id=None,
            stage=stages[current_idx + 1],
            case_type_def=case_type_def,
        )
    except Exception as e:
        logger.warning("Stage SLA start failed on auto-advance %s: %s", case_id, e)

    # The stage's `on_enter_status`. An auto-advance is still a stage entry, and
    # a designer who says "entering Review means under_review" means it however
    # the case got there — a rule that only held when a human clicked would be
    # the more surprising behaviour, not the safer one.
    try:
        from case_service.case_actions import stages as _stages

        await _stages.apply_on_enter_status(
            session, case_id=case_id, stage=stages[current_idx + 1],
            actor=None)
    except Exception as e:                                       # noqa: BLE001
        logger.warning("stage on_enter_status failed on auto-advance %s: %s", case_id, e)

    # Fetch case for outbound rule context
    case_row = await repo.get_case_instance(session, case_id)
    if case_row:
        await fire_outbound_rules(
            session,
            trigger_event="stage_exit",
            case_id=case_id,
            case_type_id=case_row.case_type_id,
            case_data=case_row.data or {},
            tenant_id="default",
        )
        await fire_outbound_rules(
            session,
            trigger_event="stage_enter",
            case_id=case_id,
            case_type_id=case_row.case_type_id,
            case_data=case_row.data or {},
            tenant_id="default",
        )

    return next_stage_id, False


@router.post("/{case_id}/steps/{step_id}/complete", response_model=StepCompletionResponse)
async def complete_step(
    case_id: uuid.UUID,
    step_id: str,
    body: StepCompleteBody,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Record a step completion (or rejection). Upserts on re-submit.

    After recording, checks whether all required steps in the stage are complete
    and auto-advances the case to the next stage if so.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy import select
    from velaris_shared.db.models import CaseStepCompletionModel, CaseTypeModel

    case = await _get_case_or_404(session, case_id, user)

    # Sealed cases are inert — one shared rule in `case_actions.seal`,
    # because a rule enforced at each call site is one that is missing
    # at the call site somebody adds next year.
    from case_service.case_actions import seal as _seal
    _seal.assert_writable(case)

    # Only the assignee, their access group or their N+1 may complete the
    # step (2026-10-28); an unassigned step, a group covering the case type.
    from velaris_shared.db.models import CaseAssignmentModel as _Asg
    from velaris_shared.hxguard.step_submit import require_step_submit
    _assignment = (await session.execute(
        select(_Asg).where(_Asg.case_id == case_id, _Asg.step_id == step_id, _Asg.status == "active")
        .order_by(_Asg.assigned_at.desc()).limit(1)
    )).scalar_one_or_none()
    await require_step_submit(session, user, case, _assignment)
    # `actor_id` names someone else as the completer; only superadmin may
    # (2026-10-28) — for anyone else it forged the record and the audit.
    from velaris_shared.hxguard.step_submit import acts_as as _acts_as
    if not _acts_as(user, body.actor_id):
        raise HTTPException(status_code=403, detail="You can only complete a step in your own name.")
    _actor = body.actor_id or user.user_id

    # Load case type definition for auto-advance check
    ct = (await session.execute(
        select(CaseTypeModel).where(CaseTypeModel.id == case.case_type_id)
    )).scalar_one_or_none()
    ct_def = ct.definition_json if ct else {}

    # A step with a Form Builder form is checked against it, as a form
    # submission is (2026-10-28) — until then this path accepted anything.
    # A rejection carries a reason, not the form; a form that no longer
    # exists is not checked, as before. Any status but an exact "rejected"
    # is checked — "completed " or "done" must not slip past.
    if body.status != "rejected":
        step_form_id = next((st.get("form_id") for sg in ct_def.get("stages", [])
                             for st in sg.get("steps", []) if st.get("id") == step_id), None)
        if step_form_id:
            from velaris_shared.db import repository as _repo
            from velaris_shared.forms import validate as _validate_form
            try:
                step_form = await _repo.get_form(session, uuid.UUID(str(step_form_id)))
            except ValueError:
                step_form = None
            if step_form is not None:
                # The version this case uses (§5 step 6).
                from velaris_shared.forms.versions import form_for_case as _form_for_case
                _step_def = await _form_for_case(session, step_form, case_id)
                # Field-level security (§5 step 7), as on a form submission.
                from velaris_shared.forms.security import caller_roles as _roles, secure_definition as _secure
                _step_def = _secure(_step_def, await _roles(session, user, case), for_browser=False)
                from velaris_shared.forms.translate import for_caller as _for_caller
                _step_def = _for_caller(_step_def, request.headers.get("accept-language"))
                from case_service.api.routers.form_submissions import case_context as _case_context
                from case_service.api.routers.form_submissions import form_references as _form_refs
                from case_service.api.routers.form_submissions import reference_tenant as _ref_tenant
                from velaris_shared.forms.pickers import picker_lookups as _picker_lookups
                clean, field_errors = _validate_form(
                    _step_def, body.data,
                    context=await _case_context(session, user, case_id),
                    references=await _form_refs(session, _step_def,
                                                await _ref_tenant(session, user, case)),
                    # Pickers (batch M): only ids this caller may choose, for this case.
                    lookups=await _picker_lookups(session, _step_def, body.data,
                                                  user=user, case_id=case_id))
                from velaris_shared.forms import form_keys as _form_keys
                _keys = _form_keys(_step_def)
                clean = {k: v for k, v in clean.items() if k in _keys}
                if field_errors:
                    raise HTTPException(status_code=422, detail={
                        "message": "; ".join(e.message for e in field_errors),
                        "validation_errors": [e.message for e in field_errors],
                        "field_errors": [e.as_dict() for e in field_errors],
                    })
                body.data = clean
                # The completer's draft of this form is done with (§5 step 7).
                from case_service.api.routers.form_submissions import drop_draft as _drop_draft
                await _drop_draft(session, case_id, step_form.id, user)

    # Validate lock ownership — if a live lock exists it must belong to this operator
    from velaris_shared.db.models import CaseAssignmentModel
    now = datetime.now(timezone.utc)
    active_assignment = (await session.execute(
        select(CaseAssignmentModel).where(
            CaseAssignmentModel.case_id == case_id,
            CaseAssignmentModel.step_id == step_id,
            CaseAssignmentModel.status == "active",
        ).order_by(CaseAssignmentModel.assigned_at.desc()).limit(1)
    )).scalar_one_or_none()

    if active_assignment is not None and active_assignment.locked_by is not None:
        exp = active_assignment.lock_expires_at
        if exp is not None and exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        lock_valid = exp is not None and exp > now
        if lock_valid and active_assignment.locked_by != user.user_id:
            raise HTTPException(
                status_code=409,
                detail=f"Step is locked by operator '{active_assignment.locked_by}'. Try again after the lock expires.",
            )

    # Upsert step completion
    existing = (await session.execute(
        select(CaseStepCompletionModel).where(
            CaseStepCompletionModel.case_id == case_id,
            CaseStepCompletionModel.step_id == step_id,
        )
    )).scalar_one_or_none()

    if existing:
        existing.stage_id = body.stage_id
        existing.step_type = body.step_type
        existing.status = body.status
        existing.data = body.data
        existing.completed_by = _actor
        existing.completed_at = now
        completion = existing
    else:
        completion = CaseStepCompletionModel(
            case_id=case_id,
            stage_id=body.stage_id,
            step_id=step_id,
            step_type=body.step_type,
            status=body.status,
            data=body.data,
            completed_by=_actor,
            completed_at=now,
        )
        session.add(completion)

    # Close assignment and release lock after successful submission
    if active_assignment is not None:
        active_assignment.status = "completed"
        active_assignment.completed_at = now
        active_assignment.locked_by = None
        active_assignment.locked_at = None
        active_assignment.lock_expires_at = None

    await session.flush()

    await _audit(
        session, case_id, "step_completed",
        actor_id=body.actor_id or user.user_id,
        details={"step_id": step_id, "stage_id": body.stage_id, "status": body.status},
    )
    await emit_trace(
        "step_complete",
        {"step_id": step_id, "stage_id": body.stage_id, "status": body.status,
         "step_type": body.step_type},
        case_id=case_id,
        tenant_id=getattr(user, "tenant_id", None) or "default",  # 8k
        actor_user_id=user.user_id,
        session=session,
    )

    # Fire outbound connector rules for step_complete
    if body.status == "completed":
        await fire_outbound_rules(
            session,
            trigger_event="step_complete",
            case_id=case_id,
            case_type_id=case.case_type_id,
            case_data=await case_vars.get_all(
                session,
                case_vars.CallerContext(kind="rules", actor_id="outbound-rules"),
                case_id,
            ),
            tenant_id="default",
        )

    # A step advanced, so the case is being worked — §7, answered 2026-09-08.
    #
    # Before the advance, and against the CURRENT stage: if that stage declares
    # its own `on_enter_status` the automatic rule stands down entirely, because
    # a designer's visible configuration must not be undone by a rule nobody can
    # see on any screen.
    auto_status = None
    if body.status == "completed":
        try:
            from case_service.case_actions import stages as _stages

            auto_status = await _stages.apply_auto_in_progress(
                session, case_id=case_id,
                stage=await _stages.stage_def(
                    session, case_type_id=case.case_type_id,
                    stage_id=body.stage_id),
                actor=str(user.user_id),
                tenant_uuid=await _action_context(session, user))
        except Exception as e:                                   # noqa: BLE001
            logger.warning("auto in_progress failed on case %s: %s", case_id, e)

    # Auto-advance if all required steps are done.
    #
    # The gate asks the STATUS SET whether work may proceed — not a literal
    # `["open", "new", "reopened"]`, which is what stood here until 2026-09-08.
    # That list was wrong in two directions at once: a tenant's own active
    # status was missing from it, and so was `in_progress` — so the moment the
    # rule above began setting `in_progress`, the second step completed on any
    # case would have stopped advancing.
    auto_advanced = False
    advance_held_by_risk = False
    if body.status == "completed":
        from case_service.case_actions import statuses as _st

        fresh = await repo.get_case_instance(session, case_id)
        if await _st.is_workable_for_case(session, fresh or case):
            new_stage, advance_held_by_risk = await _auto_advance_if_complete(
                session, case_id, body.stage_id, ct_def
            )
            auto_advanced = new_stage is not None

    # Nudge the SLA companion (new stage may have started an SLA)
    await _signal_lifecycle(
        request, case_id,
        "stage_entered" if auto_advanced else "step_completed",
        {"step_id": step_id},
    )

    return StepCompletionResponse(
        id=str(completion.id),
        case_id=str(completion.case_id),
        stage_id=completion.stage_id,
        step_id=completion.step_id,
        step_type=completion.step_type,
        status=completion.status,
        data=completion.data,
        completed_by=completion.completed_by,
        completed_at=completion.completed_at.isoformat(),
        auto_advanced=auto_advanced,
        advance_held_by_risk=advance_held_by_risk,
    )


@router.post("/{case_id}/steps/{step_id}/activate")
async def activate_step(
    case_id: uuid.UUID,
    step_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Activate a step — behaviour depends on step_type defined in the case type.

    user_task   → no-op (worker picks it up manually, existing flow)
    service_task → auto-execute linked connector, then auto-complete the step
    subprocess  → start linked HxFusion process definition, bind to this case+step
    approval    → create an approval request record (future: notify approver)
    script_task → evaluate inline rule expression, auto-complete
    """
    from sqlalchemy import select
    from velaris_shared.db.models import CaseTypeModel, CaseStepCompletionModel

    case = await _get_case_or_404(session, case_id, user)

    # Sealed cases are inert — one shared rule in `case_actions.seal`,
    # because a rule enforced at each call site is one that is missing
    # at the call site somebody adds next year.
    from case_service.case_actions import seal as _seal
    _seal.assert_writable(case)
    # Activating can auto-complete the step, so it takes the same rule as
    # completing it (2026-10-28).
    from velaris_shared.db.models import CaseAssignmentModel as _Asg
    from velaris_shared.hxguard.step_submit import require_step_submit
    _assignment = (await session.execute(
        select(_Asg).where(_Asg.case_id == case_id, _Asg.step_id == step_id, _Asg.status == "active")
        .order_by(_Asg.assigned_at.desc()).limit(1)
    )).scalar_one_or_none()
    await require_step_submit(session, user, case, _assignment)
    ct = (await session.execute(
        select(CaseTypeModel).where(CaseTypeModel.id == case.case_type_id)
    )).scalar_one_or_none()
    if not ct:
        raise coded(404, CASE_NOTFOUND_404, "Case type not found")

    # Find step definition in case type JSON
    ct_def = ct.definition_json or {}
    step_def: dict | None = None
    found_stage_id: str = ""
    for stage in ct_def.get("stages", []):
        for step in stage.get("steps", []):
            if step.get("id") == step_id:
                step_def = step
                found_stage_id = stage.get("id", "")
                break
        if step_def:
            break

    if not step_def:
        raise coded(404, CASE_NOTFOUND_404, f"Step {step_id} not found in case type definition")

    step_type = step_def.get("step_type", "user_task")
    now = datetime.now(timezone.utc)
    result: dict = {"step_id": step_id, "step_type": step_type, "action": "none"}

    if step_type in ("service_task", "connector_call", "send_task"):
        # All connector-backed automated steps use connector_config
        connector_config = step_def.get("connector_config", {})
        connector_id = connector_config.get("connector_id")
        if not connector_id:
            raise HTTPException(400, "service_task has no connector_id configured")

        # Execute connector via HxBridge
        connector_result: dict = {}
        # The connector reads and writes case variables AS itself: it sees
        # its own secret variables unmasked, everyone else's pii/secret
        # redacted, and its writes land in its registered namespace.
        try:
            _conn_ref = uuid.UUID(str(connector_id))
        except ValueError:
            _conn_ref = None
        vars_ctx = case_vars.CallerContext(
            kind="connector", ref=_conn_ref, actor_id="service-task",
        )
        try:
            # Through HxBridge's executor (2026-11-04): recorded, target-checked,
            # and able to run the step's named operation. Before, this called
            # the connector TYPE factory with an id and failed every time.
            from case_service.hxbridge.executor import run_connector_by_id

            input_mapping: dict = connector_config.get("input_mapping", {})
            case_data = await case_vars.get_all(session, vars_ctx, case_id)
            payload = {k: case_data.get(v, v) for k, v in input_mapping.items()} if input_mapping else dict(case_data)
            op = connector_config.get("operation") if isinstance(connector_config.get("operation"), str) else None
            connector_result = await run_connector_by_id(
                session, connector_id, payload, owner_tenant=case.tenant_id,
                case_id=case_id, step_id=step_id, operation=op) or {}
        except Exception as e:
            connector_result = {"_error": str(e)}

        # Write result back as case variables in the connector's registered
        # namespace (Phase 2). Connectors without a namespace fall back to
        # the legacy case.data blob so existing case types keep working.
        output_mapping: dict = connector_config.get("output_mapping", {})
        if output_mapping and isinstance(connector_result, dict):
            new_data: dict | None = None
            for src, dest in output_mapping.items():
                if src not in connector_result:
                    continue
                try:
                    await case_vars.set_variable(
                        session, vars_ctx, case_id, dest, connector_result[src],
                    )
                except case_vars.VariableError as ve:
                    if new_data is None:
                        logger.warning(
                            "service_task %s: case_vars write rejected (%s) — "
                            "falling back to case.data blob", connector_id, ve,
                        )
                        new_data = dict(case.data or {})
                    new_data[dest] = connector_result[src]
            if new_data is not None:
                case.data = new_data

        # Auto-complete the step
        completion = CaseStepCompletionModel(
            case_id=case_id,
            stage_id=found_stage_id,
            step_id=step_id,
            step_type=step_type,
            status="completed",
            data={"connector_result": connector_result},
            completed_by="system",
            completed_at=now,
        )
        session.add(completion)
        result = {"step_id": step_id, "step_type": step_type, "action": "executed", "connector_result": connector_result}

    elif step_type == "subprocess":
        # process_definition_id lives inside subprocess_config (as stored by PropertyPanel)
        subprocess_config = step_def.get("subprocess_config", {})
        process_def_id = subprocess_config.get("process_definition_id") or step_def.get("process_definition_id")
        if not process_def_id:
            raise HTTPException(400, "subprocess step has no process_definition_id configured")
        try:
            from case_service.hxfusion.engine import start_instance as _start_fusion
            from velaris_shared.db.models import ProcessDefinitionModel
            defn = await session.get(ProcessDefinitionModel, uuid.UUID(process_def_id))
            if not defn or defn.status != "active":
                raise coded(400, CASE_PROCDEF_400, f"Process definition {process_def_id} not found or inactive")

            # Map case fields to process context using context_mapping
            context_mapping: dict = subprocess_config.get("context_mapping", {})
            case_data = await case_vars.get_all(
                session,
                case_vars.CallerContext(kind="platform", actor_id="hxfusion"),
                case_id,
            )
            context = dict(case_data)
            if context_mapping:
                try:
                    import json as _json
                    if isinstance(context_mapping, str):
                        context_mapping = _json.loads(context_mapping)
                    context = {k: case_data.get(v, v) for k, v in context_mapping.items()}
                except Exception:
                    pass

            proc = await _start_fusion(
                definition_id=defn.id,
                case_id=case_id,
                context={**context, "step_id": step_id},
                tenant_id=str(ct.tenant_id) if ct.tenant_id else None,
                stage_id=found_stage_id,
                step_id=step_id,
                session=session,
            )
            result = {"step_id": step_id, "step_type": "subprocess", "action": "process_started", "process_instance_id": str(proc.id)}
        except HTTPException:
            raise
        except Exception as e:
            raise coded(500, CASE_SUBPROCESS_500, f"Failed to start subprocess: {e}")

    elif step_type == "ai_action":
        # Roadmap #33 / §5.1 — AI as a workflow participant. The model
        # proposes; a human accepts, edits or rejects. Nothing reaches case
        # data until then unless the step explicitly opted out.
        from case_service.core import ai_action

        try:
            result = await ai_action.execute(
                session, case_id=case_id, step=step_def,
                stage_id=found_stage_id, actor_id=user.user_id,
            )
        except ai_action.AIActionError as exc:
            raise coded(400, CASE_AI_ACTION_400, f"ai_action step misconfigured: {exc}")

    elif step_type in ("user_task", "manual_task", "approval"):
        result = {"step_id": step_id, "step_type": step_type, "action": "awaiting_human"}

    else:
        result = {"step_id": step_id, "step_type": step_type, "action": "no_op"}

    await session.commit()
    return result


@router.get("/{case_id}/detail")
async def get_case_detail(
    case_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Everything the case detail view needs, in one request.

    Replaces five client calls — case, history, SLA, case type, my-task — of
    which one was a genuine waterfall: the case type could not be requested
    until the case had returned its `case_type_id`.

    **This is not a GraphQL substitute and does not pretend to be.** It exists
    because the measured problem was one waterfall on one page, not the
    "chronic over-fetching" a federation layer solves. Federated GraphQL earns
    its cost when there are many services to federate; there was one.

    **Federation (#36) was declined on 2026-10-11**, after service extraction
    (#13) had happened: integrators consume REST or MCP, and a federation
    gateway is a second API surface to keep in sync with every route. Revisit
    only if a named customer's integration team blocks on it.

    ## Authorization — the part that matters when aggregating

    The five underlying endpoints do **not** share one rule, so this composes
    them rather than flattening them:

    * `case` and `history` require `case.read` — enforced once, here.
    * `sla` today has **no** case-level check of its own. Reading it through
      this endpoint is therefore *stricter* than the standalone route, never
      looser. (The standalone gap is pre-existing and left alone deliberately:
      silently tightening a live route from inside an aggregate is how callers
      break without explanation.)
    * `my_task` is **self-scoped** — it returns only the caller's own
      assignment, never the case's other assignees. That property is preserved
      exactly; aggregating must not widen it into "everyone's tasks".
    * `case_type` carries a tenant-visibility rule (a tenant-owned type is
      invisible outside its tenant). Reproduced here, degrading to `null`
      rather than 404-ing the whole response — the case is still readable even
      when its type is not.
    """
    from sqlalchemy import select as _select

    from velaris_shared import hxguard
    from velaris_shared.db.models import CaseAssignmentModel

    case = await _get_case_or_404(session, case_id, user)
    await hxguard.require_case(session, user, "case.read", case_id)

    history = await repo.get_audit_log(session, case_id)
    slas = await repo.get_sla_instances(session, case_id)

    # Case type — same visibility rule as GET /case-types/{id}. Null, not 404:
    # losing sight of the type must not make the case unreadable.
    ct = await repo.get_case_type(session, case.case_type_id)
    from case_service.tenancy.identity import same_tenant as _same_tenant
    if ct is not None and ct.tenant_id is not None and user.tenant_id \
            and not await _same_tenant(session, ct.tenant_id, user.tenant_id):
        ct = None

    # My task — self-scoped, exactly as the standalone route.
    my_task = (await session.execute(
        _select(CaseAssignmentModel).where(
            CaseAssignmentModel.case_id == case_id,
            CaseAssignmentModel.assignee_id == user.user_id,
            CaseAssignmentModel.status == "active",
        ).limit(1)
    )).scalar_one_or_none()

    return {
        "case": case,
        "history": history,
        "slas": slas,
        "case_type": ct,
        "my_task": my_task,
    }


@router.get("/{case_id}/interpreter")
async def get_interpreter_state(
    case_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Whether a Temporal interpreter is driving this case, and where it is (#21).

    Reports `running: false` rather than erroring when no interpreter exists —
    which is the normal state for every case type that has not opted in, and
    must not read as a fault.
    """
    from velaris_shared import hxguard
    await hxguard.require_case(session, user, "case.read", case_id)

    client = getattr(request.app.state, "temporal_client", None)
    if client is None:
        return {"running": False, "reason": "Temporal is not connected"}
    try:
        handle = client.get_workflow_handle(f"helix-interpreter-{case_id}")
        state = await handle.query("get_interpreter_state")
        return {"running": True, **(state or {})}
    except Exception as exc:
        return {"running": False, "reason": f"no interpreter for this case ({type(exc).__name__})"}


@router.get("/{case_id}/events")
async def get_case_event_stream(
    case_id: uuid.UUID,
    after_seq: int = 0,
    limit: int = 500,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """The case's append-only event stream (#30a, §2.2).

    Empty for any case created before the event-sourcing cutover — those are
    deliberately not backfilled (§7.5), and `event_sourced` says which kind of
    case this is so an empty list is never mistaken for a case that did nothing.
    """
    from case_service.core import event_store

    from velaris_shared import hxguard
    await hxguard.require_case(session, user, "case.read", case_id)

    case = await repo.get_case_instance(session, case_id)
    if case is None:
        raise coded(404, CASE_NOTFOUND_404, "Case not found")

    events = await event_store.stream(
        session, case_id, after_seq=after_seq, limit=max(1, min(limit, 1000)),
    )
    return {
        "case_id": str(case_id),
        "event_sourced": case.event_sourced,
        "events": [{
            "seq": e.seq,
            "event_type": e.event_type,
            "data": e.data,
            "actor_id": e.actor_id,
            "actor_type": e.actor_type,
            "occurred_at": e.occurred_at.isoformat() if e.occurred_at else None,
        } for e in events],
    }


@router.get("/{case_id}/events/verify")
async def verify_case_event_stream(
    case_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Replay the stream and compare it against the relational row (#30a).

    Divergence means something wrote case state without going through the
    lifecycle chokepoints — a bug class that is otherwise invisible until
    someone notices a case in an impossible state. Sequence gaps are reported
    too: `seq` is contiguous by construction, so a hole means an event was
    deleted, which nothing is supposed to be able to do.
    """
    from case_service.core import event_store

    from velaris_shared import hxguard
    await hxguard.require_case(session, user, "case.read", case_id)

    try:
        return await event_store.verify(session, case_id)
    except event_store.EventStoreError as exc:
        raise HTTPException(404, str(exc))


@router.post("/{case_id}/variables/sync")
async def sync_offline_variable_edits(
    case_id: uuid.UUID,
    body: dict,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Converge a batch of offline variable edits (#38, §2.7).

    Body: ``{"edits": [{"key", "value", "lamport", "origin"}, ...]}``.

    Each edit merges as an LWW-Register on `(lamport, origin)` — never on wall
    time, which offline clients cannot agree on. The response reports every
    edit as `applied`, `ignored` (a newer edit already won), `duplicate` or
    `rejected`, so a client knows exactly what to drop from its queue instead
    of resending forever.
    """
    from case_service.core import crdt

    case = await repo.get_case_instance(session, case_id)
    if case is None:
        raise coded(404, CASE_NOTFOUND_404, "Case not found")

    # Offline sync WRITES case variables, so it needs the same relationship
    # check every other case-mutating route makes. Without it any
    # authenticated user could converge edits onto any case.
    from velaris_shared import hxguard
    await hxguard.require_case(session, user, "case.update", case_id)

    edits = body.get("edits")
    if not isinstance(edits, list):
        raise HTTPException(400, "edits must be a list")
    if len(edits) > 500:
        raise HTTPException(400, "at most 500 edits per sync batch")

    out = await crdt.sync_case_variables(
        session, case_id, edits, actor_id=user.user_id,
    )
    await session.commit()
    return out


@router.get("/{case_id}/ai-actions")
async def list_ai_action_proposals(
    case_id: uuid.UUID,
    status: str | None = None,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Proposals an `ai_action` step has produced for this case (#33)."""
    from case_service.core import ai_action
    from velaris_shared import hxguard

    # Proposals carry model output derived from this case's data — a read of
    # the case by another name.
    await hxguard.require_case(session, user, "case.read", case_id)

    rows = await ai_action.list_for_case(session, case_id, status=status)
    return {"case_id": str(case_id), "proposals": [{
        "id": str(p.id),
        "step_id": p.step_id,
        "stage_id": p.stage_id,
        "status": p.status,
        "proposed": p.proposed,
        "final_output": p.final_output,
        "model": p.model,
        "provider": p.provider,
        "prompt_sha256": p.prompt_sha256,
        "error": p.error,
        "decided_by": p.decided_by,
        "decided_at": p.decided_at.isoformat() if p.decided_at else None,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    } for p in rows]}


@router.post("/{case_id}/ai-actions/{proposal_id}/decide")
async def decide_ai_action_proposal(
    case_id: uuid.UUID,
    proposal_id: uuid.UUID,
    body: dict,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Accept, reject or override an AI proposal — the human half of §5.1.

    `override` keeps both what the model proposed and what the human decided,
    which is what makes the model's real accuracy measurable later.
    """
    from case_service.core import ai_action
    from velaris_shared import hxguard
    from velaris_shared.db.models import AIActionProposalModel

    # Accepting or overriding writes a value into case variables — a case
    # update, and the human half of the §5.1 control. It must not be reachable
    # by anyone who merely holds a token.
    await hxguard.require_case(session, user, "case.update", case_id)

    decision = str(body.get("decision") or "").strip()
    proposal = await session.get(AIActionProposalModel, proposal_id)
    if proposal is None or proposal.case_id != case_id:
        raise coded(404, CASE_NOTFOUND_404, "Proposal not found for this case")

    step_def = None
    if decision != "reject":
        case = await repo.get_case_instance(session, case_id)
        ct = await repo.get_case_type(session, case.case_type_id) if case else None
        for stage in ((ct.definition_json or {}).get("stages", []) if ct else []):
            for st in stage.get("steps", []):
                if st.get("id") == proposal.step_id:
                    step_def = st
                    break
            if step_def:
                break
        if step_def is None:
            raise HTTPException(
                400, f"Step {proposal.step_id} no longer exists in the case type")

    try:
        out = await ai_action.decide(
            session, proposal_id=proposal_id, decision=decision,
            actor_id=user.user_id, step=step_def,
            override_value=body.get("value"),
        )
    except ai_action.AIActionError as exc:
        raise HTTPException(400, str(exc))

    await session.commit()
    return out


# ─── Rule application (Case Variables Phase 3) ───────────────────────


async def apply_rule_actions(
    session: AsyncSession,
    case_id: uuid.UUID,
    action_results: list[dict],
    actor_id: str,
    rule_id: str,
) -> list[dict]:
    """Apply a matched rule's set_value actions to case storage.

    Routing per target:
      velaris.status / velaris.priority  → full lifecycle machinery
      other velaris.*                    → rejected (virtual, read-only)
      namespace.name                     → grant-checked case_vars write
                                           (rules own no namespace — needs a
                                           namespace_grants row, ref = rule id
                                           or the "*" wildcard)
      bare names                         → context-only, not persisted
    """
    applied: list[dict] = []
    # actor_id is the bare rule UUID — namespace_grants rows for rules use
    # grantee_ref = <rule uuid> (or "*"), and written_by records "<ns>:<uuid>"
    rules_ctx = case_vars.CallerContext(kind="rules", actor_id=str(rule_id))
    for ar in action_results or []:
        if ar.get("action") != "set_value":
            continue
        target, value = ar.get("target") or "", ar.get("value")
        entry: dict = {"target": target, "value": value}
        try:
            if target == "velaris.status":
                await apply_status_change(
                    session, case_id, str(value), actor_id,
                    reason=f"rule:{rule_id}",
                )
                entry["applied"] = "lifecycle:status"
            elif target == "velaris.priority":
                await apply_priority_change(session, case_id, str(value), actor_id)
                entry["applied"] = "lifecycle:priority"
            elif target.startswith("velaris."):
                raise case_vars.VariableError(
                    f"'{target}' is a virtual projection — only velaris.status "
                    "and velaris.priority are writable, via the lifecycle."
                )
            elif "." in target:
                result = await case_vars.set_granted(
                    session, rules_ctx, case_id, target, value,
                )
                entry["applied"] = f"variable:{result['full_key']}"
            else:
                entry["applied"] = "context-only"
        except case_vars.VariableError as ve:
            entry["error"] = str(ve)
        applied.append(entry)
    return applied


@router.post("/{case_id}/rules/{rule_id}/apply")
async def apply_rule_to_case(
    case_id: uuid.UUID,
    rule_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Evaluate a stored rule against this case and apply its actions.

    Context is built from the case_vars façade (rules context, redacted),
    so conditions reference variables as ``crm.account_status`` or
    ``case.data.crm.account_status``; velaris.* projections are readable.
    """
    from case_service.core.rules_evaluator import evaluate_rule as _eval_rule
    from velaris_shared import hxguard

    # HxGuard pilot: any authenticated user today, but the decision (and any
    # future tightening) lives in the PDP, not in this route.
    await hxguard.require(
        session, hxguard.subject_from_user(user), "case.rule.apply",
        resource={"id": str(case_id)},
    )
    # Phase B: applying a rule mutates the case — case.update in shadow mode
    await hxguard.require_case(session, user, "case.update", case_id)

    case = await _get_case_or_404(session, case_id, user)

    # Sealed cases are inert — one shared rule in `case_actions.seal`,
    # because a rule enforced at each call site is one that is missing
    # at the call site somebody adds next year.
    from case_service.case_actions import seal as _seal
    _seal.assert_writable(case)
    rule = await repo.get_rule(session, rule_id)
    if rule is None:
        raise coded(404, CASE_NOTFOUND_404, "Rule not found")
    if not rule.enabled:
        raise coded(409, CASE_RULE_DISABLED_409, "Rule is disabled")
    # scope enforcement: a case_type-scoped rule only applies to its own type
    if rule.scope == "case_type" and rule.scope_target_id \
            and rule.scope_target_id != str(case.case_type_id):
        raise coded(403, CASE_RULE_SCOPE_403, "Rule is scoped to a different case type")

    rules_ctx = case_vars.CallerContext(kind="rules", actor_id=f"rule:{rule_id}")
    vars_dict = await case_vars.get_all(session, rules_ctx, case_id)
    # both shapes resolve: bare "crm.x" and "case.data.crm.x" / "case.status"
    context = {
        **vars_dict,
        "case": {
            "id": str(case.id),
            "status": case.status,
            "priority": case.priority,
            "current_stage_id": case.current_stage_id,
            "data": vars_dict,
        },
    }

    rule_dict = {
        "id": str(rule.id), "name": rule.name,
        "rule_type": rule.rule_type, "priority": rule.priority,
        **(rule.definition_json or {}),
    }
    result = _eval_rule(rule_dict, context)

    applied: list[dict] = []
    if result.get("matched"):
        applied = await apply_rule_actions(
            session, case_id, result.get("action_results") or [],
            actor_id=user.user_id, rule_id=str(rule.id),
        )

    return {
        "case_id": str(case_id),
        "rule_id": str(rule.id),
        "matched": result.get("matched", False),
        "action_results": result.get("action_results"),
        "applied": applied,
    }


# ─── Case sharing (HxGuard Phase B) ──────────────────────────────────


class ShareCreate(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=255)
    relation: str = "viewer"     # viewer | editor


async def _resolve_share_user(session, value: str):
    """Resolve a share target to a real user — accepts a user UUID, username,
    or email. Tuples must store the canonical user id (the JWT `sub` is the
    user UUID); a free-text subject would 'share' to nobody."""
    from sqlalchemy import func, or_
    from velaris_shared.db.models import HelixUserModel
    v = value.strip()
    target = None
    try:
        target = (await session.execute(
            sa_select(HelixUserModel).where(HelixUserModel.id == uuid.UUID(v))
        )).scalar_one_or_none()
    except ValueError:
        pass
    if target is None:
        target = (await session.execute(
            sa_select(HelixUserModel).where(or_(
                func.lower(HelixUserModel.email) == v.lower(),
                func.lower(HelixUserModel.username) == v.lower(),
            ))
        )).scalar_one_or_none()
    return target


async def _require_share_access(session, user, case_id: uuid.UUID, action: str) -> None:
    """Sharing endpoints are NEW surfaces — always enforced, regardless of
    the shadow-mode setting that protects pre-existing case routes.

    Anti-oracle: a caller who cannot READ the case gets 404 (existence is
    not confirmed); 403 is reserved for callers who can read but lack the
    requested capability."""
    from velaris_shared import hxguard
    subject = hxguard.subject_from_user(user)
    decision = await hxguard.check(session, subject, action, {"case_id": case_id})
    if decision.allow:
        return
    can_read = (await hxguard.check(
        session, subject, "case.read", {"case_id": case_id},
    )).allow
    if not can_read:
        raise coded(404, CASE_NOTFOUND_404, "Case not found")
    raise coded(403, CASE_GUARD_403, f"Not authorized: {decision.reason}")


@router.post("/{case_id}/shares", status_code=201)
async def share_case(
    case_id: uuid.UUID,
    body: ShareCreate,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    if body.relation not in ("viewer", "editor"):
        raise HTTPException(400, "relation must be viewer | editor")
    case = await _get_case_or_404(session, case_id, user)
    await _require_share_access(session, user, case_id, "case.share")
    # Sealed: sharing a case is a change to who may reach it, which is exactly
    # the kind of "further change" archiving is meant to stop.
    from case_service.case_actions import seal as _seal
    _seal.assert_writable(case)
    target = await _resolve_share_user(session, body.user_id)
    if target is None or not target.is_active:
        raise HTTPException(400, f"No user found for '{body.user_id}' — use a user id, username, or email")
    from velaris_shared.hxguard import tuples as hxg_tuples
    await hxg_tuples.write_tuple(
        session, object_type="case", object_id=case_id,
        relation=body.relation, subject_type="user",
        subject_id=str(target.id), created_by=user.user_id,
    )
    await _audit(session, case_id, "case_shared", actor_id=user.user_id,
                 details={"user_id": str(target.id), "given": body.user_id,
                          "username": target.username, "relation": body.relation})
    return {"case_id": str(case_id), "user_id": str(target.id),
            "username": target.username, "relation": body.relation}


@router.get("/{case_id}/shares")
async def list_case_shares(
    case_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    await _get_case_or_404(session, case_id, user)
    await _require_share_access(session, user, case_id, "case.read")
    from velaris_shared.hxguard import tuples as hxg_tuples
    rows = await hxg_tuples.list_tuples(
        session, object_type="case", object_id=case_id,
        relations={"viewer", "editor", "assignee"},
    )
    # Resolve subject UUIDs to usernames so the Sharing tab shows people,
    # not identifiers. Legacy/free-text subjects simply resolve to nothing.
    from velaris_shared.db.models import HelixUserModel
    subject_uuids = []
    for t in rows:
        try:
            subject_uuids.append(uuid.UUID(t.subject_id))
        except ValueError:
            pass
    names: dict[str, HelixUserModel] = {}
    if subject_uuids:
        for u in (await session.execute(
            sa_select(HelixUserModel).where(HelixUserModel.id.in_(subject_uuids))
        )).scalars():
            names[str(u.id)] = u
    return [
        {"user_id": t.subject_id, "relation": t.relation,
         "username": names[t.subject_id].username if t.subject_id in names else None,
         "display_name": names[t.subject_id].display_name if t.subject_id in names else None,
         "created_by": t.created_by,
         "created_at": t.created_at.isoformat() if t.created_at else None}
        for t in rows
    ]


@router.delete("/{case_id}/shares", status_code=204)
async def unshare_case(
    case_id: uuid.UUID,
    user_id: str,
    relation: str = Query(..., pattern="^(viewer|editor)$"),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    case = await _get_case_or_404(session, case_id, user)

    # Sealed cases are inert — one shared rule in `case_actions.seal`,
    # because a rule enforced at each call site is one that is missing
    # at the call site somebody adds next year.
    from case_service.case_actions import seal as _seal
    _seal.assert_writable(case)
    await _require_share_access(session, user, case_id, "case.share")
    from velaris_shared.hxguard import tuples as hxg_tuples
    # Accept id/username/email like share; fall back to the raw value so
    # legacy free-text tuples stay removable.
    target = await _resolve_share_user(session, user_id)
    subject_id = str(target.id) if target is not None else user_id
    removed = await hxg_tuples.remove_tuple(
        session, object_type="case", object_id=case_id,
        relation=relation, subject_type="user", subject_id=subject_id,
    )
    if not removed and subject_id != user_id:
        removed = await hxg_tuples.remove_tuple(
            session, object_type="case", object_id=case_id,
            relation=relation, subject_type="user", subject_id=user_id,
        )
    if not removed:
        raise coded(404, CASE_NOTFOUND_404, "Share not found")
    await _audit(session, case_id, "case_unshared", actor_id=user.user_id,
                 details={"user_id": subject_id, "given": user_id, "relation": relation})


@router.get("/{case_id}/step-completions", response_model=list[StepCompletionResponse])
async def list_step_completions(
    case_id: uuid.UUID,
    stage_id: str | None = Query(None),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Return all step completions for a case, optionally filtered by stage."""
    from sqlalchemy import select
    from velaris_shared.db.models import CaseStepCompletionModel

    await _get_case_or_404(session, case_id, user)
    from velaris_shared import hxguard
    await hxguard.require_case(session, user, "case.read", case_id)
    stmt = select(CaseStepCompletionModel).where(
        CaseStepCompletionModel.case_id == case_id
    )
    if stage_id:
        stmt = stmt.where(CaseStepCompletionModel.stage_id == stage_id)
    rows = (await session.execute(stmt.order_by(CaseStepCompletionModel.completed_at))).scalars().all()
    return [
        StepCompletionResponse(
            id=str(r.id), case_id=str(r.case_id),
            stage_id=r.stage_id, step_id=r.step_id,
            step_type=r.step_type, status=r.status,
            data=r.data, completed_by=r.completed_by,
            completed_at=r.completed_at.isoformat(),
        )
        for r in rows
    ]


@router.get("/{case_id}/my-task", response_model=MyTaskResponse | None)
async def get_my_task(
    case_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Return the operator's current open assignment on this case, acquiring a lock.

    Pega semantics: opening a task locks it to this operator for LOCK_TTL_SECONDS.
    Other operators can see the lock but cannot submit the step while it is held.
    Returns null (204 body) when no active assignment exists for this operator.
    """
    from sqlalchemy import select
    from velaris_shared.db.models import (
        CaseAssignmentModel, CaseTypeModel, CaseStepCompletionModel
    )

    case = await _get_case_or_404(session, case_id, user)
    now = datetime.now(timezone.utc)

    # Find most recent active assignment for this operator
    assignment = (await session.execute(
        select(CaseAssignmentModel).where(
            CaseAssignmentModel.case_id == case_id,
            CaseAssignmentModel.assignee_id == user.user_id,
            CaseAssignmentModel.status == "active",
        ).order_by(CaseAssignmentModel.assigned_at.desc()).limit(1)
    )).scalar_one_or_none()

    if assignment is None:
        return None

    # Acquire / refresh lock
    lock_expires = now + timedelta(seconds=LOCK_TTL_SECONDS)
    assignment.locked_by = user.user_id
    assignment.locked_at = now
    assignment.lock_expires_at = lock_expires
    await session.flush()
    await emit_trace(
        "lock_acquire",
        {"step_id": assignment.step_id, "lock_expires_at": lock_expires.isoformat()},
        case_id=case_id,
        tenant_id=getattr(user, "tenant_id", None) or "default",  # 8k
        actor_user_id=user.user_id,
        session=session,
    )

    # Resolve step definition from case type
    ct = (await session.execute(
        select(CaseTypeModel).where(CaseTypeModel.id == case.case_type_id)
    )).scalar_one_or_none()
    ct_def = ct.definition_json if ct else {}

    stage_id = case.current_stage_id or ""
    step_def: dict = {}
    form_id: str | None = None
    for stage in ct_def.get("stages", []):
        for step in stage.get("steps", []):
            if step.get("id") == assignment.step_id:
                step_def = step
                form_id = step.get("form_id")
                if not stage_id:
                    stage_id = stage.get("id", "")
                break

    # Load existing completion if any
    completion_row = (await session.execute(
        select(CaseStepCompletionModel).where(
            CaseStepCompletionModel.case_id == case_id,
            CaseStepCompletionModel.step_id == assignment.step_id,
        )
    )).scalar_one_or_none()

    return MyTaskResponse(
        assignment_id=str(assignment.id),
        case_id=str(case.id),
        case_number=case.case_number,
        case_description=(await case_vars.get_all(
            session,
            case_vars.CallerContext(kind="platform", actor_id=user.user_id,
                                    privileged=_has_sensitive_access(user)),
            case.id,
        )).get("description"),
        case_priority=case.priority,
        stage_id=stage_id,
        step_id=assignment.step_id,
        step_def=step_def,
        form_id=form_id,
        completion=completion_row.data if completion_row else None,
        locked_by=assignment.locked_by,
        lock_expires_at=lock_expires.isoformat(),
        is_locked_by_me=True,
    )


@router.post("/{case_id}/steps/{step_id}/unlock", status_code=204)
async def unlock_step(
    case_id: uuid.UUID,
    step_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Explicitly release a step lock held by the calling operator.

    Called when the operator navigates away or clicks Cancel — prevents
    the lock from blocking colleagues for the full TTL.
    """
    from sqlalchemy import select
    from velaris_shared.db.models import CaseAssignmentModel

    assignment = (await session.execute(
        select(CaseAssignmentModel).where(
            CaseAssignmentModel.case_id == case_id,
            CaseAssignmentModel.step_id == step_id,
            CaseAssignmentModel.status == "active",
            CaseAssignmentModel.locked_by == user.user_id,
        ).limit(1)
    )).scalar_one_or_none()

    if assignment is not None:
        assignment.locked_by = None
        assignment.locked_at = None
        assignment.lock_expires_at = None
        await session.flush()
        await emit_trace(
            "lock_release",
            {"step_id": step_id},
            case_id=case_id,
            tenant_id=getattr(user, "tenant_id", None) or "default",  # 8k
            actor_user_id=user.user_id,
            session=session,
        )


# ── 198: the action dispatcher ───────────────────────────────────────────────
#
# Design: docs/Future/Done/CASE-ACTIONS-STATUS-DESIGN.md
#
# One endpoint replaces six named routes, and `POST /cases/bulk` is the same
# function in a loop rather than a second implementation. That is the whole
# reason B3 (interactive bulk case actions) closes here: every named route had
# to remember the tenant clause, the per-case ReBAC check, the privilege, the
# audit row, the lifecycle hook and the realtime broadcast, and seventeen routes
# would have been seventeen chances to forget one. Nineteen handlers gating on
# `require_case` alone were readable across the tenant boundary until
# 2026-09-06; the fix there was to move the clause INTO `require_case` so the
# next feature inherits it. Same lesson, one level up.

class ActionRequest(BaseModel):
    """Parameters are per action; `precheck` refuses a missing one up front."""
    params: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = None
    idempotency_key: str | None = None


class BulkActionRequest(BaseModel):
    case_ids: list[uuid.UUID]
    action: str
    params: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = None
    idempotency_key: str | None = None


async def _action_context(session: AsyncSession, user: AuthenticatedUser):
    """The caller's tenant, resolved once per request rather than per case."""
    resolved = await resolve_tenant(session, getattr(user, "tenant_id", None))
    return resolved[0] if resolved else None


async def _restart_lifecycle_if_needed(
    request: Request, session: AsyncSession, case_id: uuid.UUID, result,
) -> None:
    """Restart the companion workflow when a case leaves a terminal status.

    `/reopen` was the only one of the four removed routes that remembered this,
    and it mattered: the lifecycle workflow exits on a terminal status, so a
    case moved out of one by any other route sat with no workflow behind it and
    advanced only on the poll interval. Doing it here means every action that
    leaves a terminal status gets it, including a tenant's own.
    """
    if not result.ok or not isinstance(result.detail, dict):
        return
    if result.detail.get("terminal") is not False:
        return
    client = getattr(request.app.state, "temporal_client", None)
    if client is None:
        return
    case = await repo.get_case_instance(session, case_id)
    if case is None:
        return
    try:
        from case_service.temporal.workflows.case_lifecycle_workflow import (
            CaseLifecycleWorkflow,
        )
        await client.start_workflow(
            CaseLifecycleWorkflow.run,
            {"case_id": str(case_id), "case_type_id": str(case.case_type_id)},
            id=f"helix-case-{case_id}",
            task_queue="helix-case-service",
        )
    except Exception:
        pass  # already running — the signal below is enough


#: How many workflow nudges a bulk run may have in flight, and how long it may
#: spend on them in total. Both are ceilings on damage, not tuning knobs.
_SIGNAL_CONCURRENCY = 16
_SIGNAL_BUDGET_SECONDS = 10.0


async def _flush_signals(
    request: Request, session: AsyncSession,
    entries: list[tuple[uuid.UUID, dict]],
) -> None:
    """Nudge Temporal for a batch, concurrently and time-boxed.

    Returns when the work is done OR the budget expires, whichever is first —
    and an expired budget is not an error. The workflows rescan on their own, so
    an unsent signal costs latency, never correctness.
    """
    if not entries or getattr(request.app.state, "temporal_client", None) is None:
        return

    import asyncio

    sem = asyncio.Semaphore(_SIGNAL_CONCURRENCY)

    async def one(case_id: uuid.UUID, payload: dict) -> None:
        async with sem:
            # The lifecycle restart is in here too: it is the same Temporal
            # round trip shape and belongs under the same budget.
            await _restart_lifecycle_if_needed(
                request, session, case_id,
                _disp_result_shim(payload))
            await _signal_lifecycle(request, case_id, "status_changed", payload)

    try:
        await asyncio.wait_for(
            asyncio.gather(*(one(cid, p) for cid, p in entries),
                           return_exceptions=True),
            timeout=_SIGNAL_BUDGET_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "bulk: workflow nudges exceeded %.0fs for %d cases — the cases are "
            "updated and committed; their workflows will pick the change up on "
            "their own rescan.", _SIGNAL_BUDGET_SECONDS, len(entries))


class _disp_result_shim:
    """`_restart_lifecycle_if_needed` wants a result object; this is the two
    fields it reads, so the batch path can reuse it without rebuilding one."""

    def __init__(self, payload: dict) -> None:
        self.ok = True
        self.detail = payload


@router.post("/{case_id}/actions/{action}")
async def perform_action(
    case_id: uuid.UUID,
    action: str,
    body: ActionRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Perform one action on one case."""
    from case_service.case_actions import dispatcher as _disp

    params = dict(body.params or {})
    if body.reason and "reason" not in params:
        params["reason"] = body.reason

    try:
        _disp.precheck(action, params, case_count=1)
    except _disp.ActionError as exc:
        raise coded(exc.status_code, exc.code, str(exc))

    tenant_uuid = await _action_context(session, user)
    # `restore` is the one action whose target is, by definition, hidden.
    case = await _get_case_or_404(
        session, case_id, user, include_deleted=(action == "restore"))
    cfg = await _disp.resolve_config(
        session, tenant_id=tenant_uuid, case_type_id=case.case_type_id)

    result = await _disp.perform(
        session, case_id=case_id, action_name=action, params=params,
        user=user, tenant_uuid=tenant_uuid, config=cfg, audit=_audit,
    )

    # A single action reports its failure as a status code, because there is
    # exactly one outcome to report. A bulk run cannot — see below.
    if not result.ok:
        code = result.code or "VEL-CASE-ACTION-FAILED-500"
        status_code = (404 if code.endswith("-404")
                       else 403 if code.endswith("-403")
                       else 409 if code.endswith("-409")
                       else 500 if code.endswith("-500")
                       else 400)
        raise coded(status_code, code, result.error)

    effects = set(_disp.registry.ACTIONS[action].effects)
    await _restart_lifecycle_if_needed(request, session, case_id, result)
    if "status" in effects:
        # `terminal` travels WITH the signal. A workflow may not query the
        # database — it has to stay deterministic on replay — so it cannot look
        # up whether a tenant's own status ends a case. The dispatcher already
        # resolved it, so it says so.
        await _signal_lifecycle(request, case_id, "status_changed", {
            "status": (result.detail or {}).get("to"),
            "terminal": (result.detail or {}).get("terminal"),
        })

    # The updated case comes back, as every named route this replaces did. A
    # client that closes a case wants the new state, and making it re-fetch is
    # both a second round trip and a chance to read a stale row.
    updated = await repo.get_case_instance(session, case_id)
    return {"case_id": str(case_id), "action": action,
            **result.as_dict(),
            "case": CaseResponse.model_validate(updated, from_attributes=True)
                    if updated is not None else None}


@router.post("/bulk")
async def bulk_action(
    body: BulkActionRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Perform one action across many cases.

    **Partial success is the normal outcome and is reported, never hidden.** A
    200 whose body says nothing about the twelve cases that refused is how a
    bulk feature loses an operator's trust in its first week.

    The permission check is **per case, inside the loop** — never once for the
    batch — because the answer genuinely differs per case. And a request naming
    more ids than the ceiling is **refused, not truncated**: silently acting on
    the first N and reporting success is the worse answer, which is the posture
    `scheduler/actions.py::CaseBulkAction` already takes.
    """
    from case_service.case_actions import dispatcher as _disp

    params = dict(body.params or {})
    if body.reason and "reason" not in params:
        params["reason"] = body.reason

    # Deduplicated first, so a caller who repeats an id does not get two audit
    # rows and two notes on one case.
    case_ids: list[uuid.UUID] = list(dict.fromkeys(body.case_ids))

    try:
        action_def = _disp.precheck(
            body.action, params, case_count=len(case_ids))
    except _disp.ActionError as exc:
        raise coded(exc.status_code, exc.code, str(exc))

    tenant_uuid = await _action_context(session, user)

    # Configuration is resolved PER CASE TYPE, memoised across the batch.
    #
    # Resolving once at tenant scope would have been one query instead of a
    # handful — and it would have silently skipped a tenant's per-case-type
    # narrowing. An operator who restricted `close` to `resolved` on one case
    # type would find that restriction enforced on the single-case route and
    # bypassed by selecting the same cases and using bulk, which is worse than
    # not offering bulk at all. A selection made on one screen usually spans one
    # or two case types, so the memo makes this a query or two, not N.
    cfg_cache: dict[uuid.UUID | None, dict] = {}

    async def _cfg_for(case_type_id):
        if case_type_id not in cfg_cache:
            cfg_cache[case_type_id] = await _disp.resolve_config(
                session, tenant_id=tenant_uuid, case_type_id=case_type_id)
        return cfg_cache[case_type_id]

    # The stage rule is memoised on the same principle, keyed by case type AND
    # stage: a selection is usually a handful of stages, and the alternative is
    # one `case_types` read per case — 500 of them for the same few answers.
    from case_service.case_actions import stages as _stg

    stage_cache: dict[tuple, Any] = {}

    async def _stage_for(case_type_id, stage_id):
        key = (case_type_id, stage_id)
        if key not in stage_cache:
            stage_cache[key] = await _stg.stage_def(
                session, case_type_id=case_type_id, stage_id=stage_id)
        return stage_cache[key]

    results = []
    to_signal: list[tuple[uuid.UUID, dict]] = []
    for case_id in case_ids:
        from velaris_shared.db.models import CaseInstanceModel as _CI
        case_row = (await session.execute(
            sa_select(_CI.case_type_id, _CI.current_stage_id).where(_CI.id == case_id)
        )).first()
        case_type_of, stage_of = (case_row if case_row is not None else (None, None))
        result = await _disp.perform(
            session, case_id=case_id, action_name=body.action, params=params,
            user=user, tenant_uuid=tenant_uuid,
            config=await _cfg_for(case_type_of),
            stage_def=await _stage_for(case_type_of, stage_of), audit=_audit,
        )
        results.append(result)
        if result.ok and "status" in action_def.effects:
            to_signal.append((case_id, {
                "status": (result.detail or {}).get("to"),
                "terminal": (result.detail or {}).get("terminal"),
            }))

    # Temporal is nudged AFTER the loop, concurrently, and under a hard time
    # budget.
    #
    # Signalling inside the loop was 2 round trips per case — **1000 sequential
    # network calls for a 500-case run**, awaited one at a time while this
    # request holds a database transaction open. At 20ms each that is twenty
    # seconds of a pool connection; with Temporal slow or unreachable it is the
    # per-call timeout multiplied by a thousand, and the request dies at the
    # gateway with the transaction still open. One authorised operator clicking
    # one button could exhaust the pool.
    #
    # Skipping a signal is SAFE by construction and `_signal_lifecycle` says so:
    # both workflows rescan and poll regardless, so a signal that never lands
    # only delays. That is what makes a time budget the right answer rather than
    # a compromise — the worst case is a case advancing on its poll interval
    # instead of instantly.
    await _flush_signals(request, session, to_signal)

    succeeded = sum(1 for r in results if r.ok)
    return {
        "action": body.action,
        "requested": len(case_ids),
        "succeeded": succeeded,
        "failed": len(results) - succeeded,
        "results": [r.as_dict() for r in results],
    }
