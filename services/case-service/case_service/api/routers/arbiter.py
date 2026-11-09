"""HxArbiter — the policy API.

Everything here is gated, and the gates are not all the same on purpose:

* `decide` is asked by **services** on the hot path and never by a person.
  Sharing an action with policy reading would mean every service that enforces
  policy could also read how it is written; sharing one with writing would be
  worse.
* **Activation is separate from writing.** Appending a version is a draft;
  activating it is the moment the platform starts answering differently, and
  four-eyes has nothing to attach to if the two are one privilege.
* **Promoting a namespace is admin-only.** It is the control that turns shadow
  into enforcement across a whole class of actions at once.

`/simulate` answers what-if questions through the **same** `decide()` the real
path uses, with `record=True` and `simulated=True`. A simulator with its own
evaluation logic proves nothing about the engine that will actually run.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared import hxguard
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.db.models import (
    ArbiterDecisionModel,
    ArbiterNamespaceModel,
    ArbiterPolicyModel,
    ArbiterPolicyVersionModel,
)
from velaris_shared.db.session import get_session
from velaris_shared.middleware.endpoint_rate_limit import rate_limit

from case_service.hxarbiter import (
    CompileError,
    Effect,
    PolicyCompiler,
    PolicyMode,
    PolicyVersion,
    Rule,
    VELARIS_CATALOG,
)
from case_service.hxarbiter import (
    chain, legacy_bridge, lifecycle, parity, pdp, store, upkeep)
import hxarbiter_core as _analyzer   # analyse()/seed_requests(): deterministic, ships in the core
from case_service.hxarbiter.nlp import author as _author
from case_service.hxarbiter.nlp import provider as _provider
from case_service.hxarbiter.nlp import readback as _readback
from case_service.hxarbiter.nlp import whatif as _whatif
from case_service.hxarbiter.nlp import advisor as _advisor

# NOTE — nothing here calls `pdp.invalidate_cache()`, deliberately.
#
# The compiled-policy cache is keyed on a generation DERIVED from the rows
# (`store.policy_generation`), which every process re-reads on every decision.
# A policy written here is therefore seen by case-service, analytics-service and
# every other process on their next `decide()`, with no bump to remember and
# nothing to forget. Calling invalidate here would clear only THIS process's
# cache and read as though that were the mechanism — which is the shape of a
# defect this platform has already paid for once.
from hxarbiter_core import RequestResolver, StaticResolver
from hxarbiter_core import (
    DecisionRequest,
    Op,
    ResourceRef,
    SubjectRef,
)
from case_service.core.timestamps import iso_utc
from case_service.tenancy.identity import is_superadmin, resolve_tenant

log = logging.getLogger(__name__)

router = APIRouter(prefix="/arbiter", tags=["arbiter"])

# NOTE — every rate limiter below is declared AFTER the `hxguard.guard`
# dependency on its route, and the order is load-bearing.
#
# FastAPI resolves dependencies in declaration order, so a limiter declared
# first runs before authorization. That gave a caller who holds none of these
# actions a **429 instead of a 403** — which leaks that the route exists, and
# lets an unprivileged caller exhaust the bucket for a privileged one sharing
# an egress IP (the limiter is keyed on the client address, and an office or VPN
# is one address). Authorization first means an unentitled caller is refused
# without ever touching the bucket.
#
#: `/simulate` writes a decision row per call, so an unbounded caller can grow
#: the decision log at will — and the log is the evidence surface. Bounded.
_simulate_rl = rate_limit(max_calls=60, window_seconds=60, name="arbiter-simulate")

#: Sealing walks unsealed decisions and writes chain rows. It is idempotent, but
#: it is not free, and it is a platform-wide write.
_seal_rl = rate_limit(max_calls=6, window_seconds=60, name="arbiter-seal")

#: Drafting calls a language model, which is the most expensive thing this
#: router can be made to do — and on a `local_only` tenant it is CPU on the same
#: box serving requests. Bounded well below what a person can type.
_draft_rl = rate_limit(max_calls=20, window_seconds=60, name="arbiter-draft")

#: The upkeep report writes nothing, but it is the most expensive READ in this
#: router: it recompiles every active version, runs the analyser over each
#: namespace's whole live set, scans up to 20,000 decision rows and computes a
#: parity report per namespace. Cheap on a small install, not on a large one,
#: and every other costly route here is bounded.
_upkeep_rl = rate_limit(max_calls=30, window_seconds=60, name="arbiter-upkeep")

#: Regenerating mirrors writes policy versions. Idempotent by content address,
#: but a caller that changes the legacy setting between calls can still make it
#: append, so it is not free to hammer.
_bridge_rl = rate_limit(max_calls=10, window_seconds=60, name="arbiter-bridge")


def _tenant_token(user: AuthenticatedUser) -> str:
    return getattr(user, "tenant_id", None) or "default"


def _claimant(user: AuthenticatedUser) -> str:
    """The authenticated caller, for the decision row's `claimed_by`.

    `subject` says who the action is on behalf of, and a PEP legitimately asks
    about somebody else — the engine, a worker and a connector all do. That
    makes `subject` an ASSERTION, and an assertion nobody signed for cannot be
    attributed: without this, anyone able to call `/decide` could write any
    principal into the log and the row would read exactly like one the platform
    produced.
    """
    return str(getattr(user, "username", None)
               or getattr(user, "email", None)
               or getattr(user, "user_id", None)
               or "unknown")[:255]


async def _actor_uuid(
    session: AsyncSession, user: AuthenticatedUser,
) -> uuid.UUID | None:
    """The caller as a `GUID`, for the four arbiter attribution columns.

    This used to parse `user.user_id` and give up on anything that was not
    already a UUID. Giving up kept the route working — these columns are
    nullable — but an SSO or service principal carries a **username** there, so
    every policy such an operator authored was recorded as having no author, on
    the one table an auditor reads to find out who. `actor_uuid` resolves both
    forms against the user table (2026-09-06, Sep item 39).

    The degrade is unchanged and deliberate: a principal with no user row is
    recorded as absent rather than coerced. A made-up id on an audit row is
    worse than an empty one.
    """
    from case_service.tenancy.identity import actor_uuid

    return await actor_uuid(session, user)


def _require_platform_operator(user: AuthenticatedUser, what: str) -> None:
    """Platform scope is not a tenant admin's to take.

    Two things here reach every tenant on the box: a policy with `tenant_id
    NULL`, which every tenant is bound by and none can override (§5.3), and a
    namespace ceiling, which decides whether a whole class of actions enforces
    for everyone. `arbiter.policies.write` and `arbiter.namespaces.promote` are
    admin actions — and on a shared install "admin" is a TENANT admin. Without
    this check, one tenant's administrator could bind, or switch on enforcement
    for, every other tenant sharing the deployment.
    """
    if not is_superadmin(user):
        raise HTTPException(
            status_code=403,
            detail=f"VEL-ARB-PLATFORM-SCOPE-403: {what} affects every tenant "
                   f"and is reserved to a platform operator.",
        )


async def _require_namespace(session: AsyncSession, namespace: str) -> ArbiterNamespaceModel:
    """404 for a namespace nobody registered, rather than a 500 from the FK.

    `arbiter_policies.namespace` is a foreign key, so an unregistered value was
    caught — by PostgreSQL, as an `IntegrityError`, surfacing to the caller as
    `VEL-SYS-UNHANDLED-500`. A typo in a namespace is ordinary user input and
    must read as one; a 500 also says nothing about what was wrong, and the
    error text differs per dialect. `/drafts` had this check from ARB-F and the
    other two write paths never did.
    """
    row = (await session.execute(
        select(ArbiterNamespaceModel)
        .where(ArbiterNamespaceModel.namespace == namespace)
    )).scalars().first()
    if row is None:
        raise HTTPException(
            404, f"VEL-ARB-NAMESPACE-404: {namespace!r} is not a registered "
                 f"namespace. GET /arbiter/catalog lists them.")
    return row


async def _tenant_uuid(session: AsyncSession, user: AuthenticatedUser) -> uuid.UUID:
    resolved = await resolve_tenant(session, _tenant_token(user))
    if resolved is None:
        # An unresolvable tenant is a denial, never "any tenant".
        raise HTTPException(status_code=403, detail="VEL-ARB-TENANT-UNRESOLVED-403")
    return resolved[0]


# ── payloads ─────────────────────────────────────────────────────────────────


#: Every payload here forbids unknown fields. Silently ignoring one is how a
#: caller who misspells `context` gets a decision made on no facts at all and is
#: told it succeeded — and how a field that looks security-relevant, like
#: `claimed_by`, appears to be settable from the body when it is not.
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PolicyIn(_Strict):
    name: str = Field(min_length=1, max_length=255)
    namespace: str = Field(min_length=1, max_length=64)
    description: str = ""
    #: Platform scope is a deliberate, separate act — a tenant admin creating a
    #: policy every other tenant is bound by must have meant to.
    platform: bool = False


#: A version's rules are all evaluated on every decision in the namespace, and
#: the compiler already bounds each ONE at 128 nodes. Without a bound on the
#: COUNT, a single accepted policy could put over a million nodes in front of
#: every request — the per-rule limit alone is not a limit.
MAX_RULES_PER_VERSION = 200


class VersionIn(_Strict):
    rules: list[dict[str, Any]] = Field(min_length=1, max_length=MAX_RULES_PER_VERSION)
    mode: PolicyMode = PolicyMode.SHADOW
    nl_source: str | None = None
    nl_readback: str | None = None


class DecideIn(_Strict):
    subject: SubjectRef
    #: Bounded to the column that stores it. An over-long action would otherwise
    #: pass validation and fail at the INSERT, turning a bad request into a 500.
    action: str = Field(min_length=1, max_length=128)
    resource: ResourceRef
    #: The context is written to the decision log. Unbounded, it is a way to
    #: put arbitrary bulk into the evidence surface.
    context: dict[str, Any] = Field(default_factory=dict, max_length=64)


class SimulateIn(DecideIn):
    #: Attributes a PIP would otherwise resolve. This is what makes the endpoint
    #: a what-if: "suppose this tenant were in eu-west".
    attributes: dict[str, Any] = Field(default_factory=dict, max_length=64)


class RetentionIn(_Strict):
    #: NULL/None means keep forever, which is what every namespace does until
    #: an operator says otherwise. Sent explicitly so "unset it again" is
    #: expressible rather than a one-way door.
    decision_retention_days: int | None = Field(default=None, ge=1, le=3650)


class PromoteIn(_Strict):
    max_mode: PolicyMode
    #: Promote WITHOUT the parity evidence. Deliberately noisy to type, because
    #: it is the one switch here that can break a working install: enforcing a
    #: namespace whose engine disagrees with the control it replaces changes
    #: answers that used to be right. It exists because a brand-new namespace
    #: with no legacy control has no parity to prove, and refusing forever would
    #: make the engine unusable for anything it did not inherit.
    override_parity: bool = False
    override_reason: str = Field(default="", max_length=500)


# ── catalogue ────────────────────────────────────────────────────────────────


@router.get("/catalog")
async def get_catalog(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("arbiter.policies.read")),
) -> dict[str, Any]:
    """Every attribute a condition may name, and every namespace registered.

    This is what NL authoring is shown, so a model can only reference attributes
    that exist — and it is what a human needs before writing a rule by hand.
    """
    namespaces = (await session.execute(
        select(ArbiterNamespaceModel).order_by(ArbiterNamespaceModel.namespace)
    )).scalars().all()
    return {
        "attributes": [a.model_dump() for a in VELARIS_CATALOG.as_list()],
        "operators": [str(o) for o in Op],
        "namespaces": [
            {
                "namespace": n.namespace,
                "default_effect": n.default_effect,
                "max_mode": n.max_mode,
                "description": n.description,
                "promoted_at": n.promoted_at,
                #: None means "kept forever", which is every namespace until an
                #: operator sets one. Shown because a retention horizon nobody
                #: can see is one nobody reviews.
                "decision_retention_days": n.decision_retention_days,
            }
            for n in namespaces
        ],
    }


# ── policies ─────────────────────────────────────────────────────────────────


@router.get("/policies")
async def list_policies(
    namespace: str | None = Query(None),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("arbiter.policies.read")),
) -> list[dict[str, Any]]:
    tenant = await _tenant_uuid(session, user)
    q = select(ArbiterPolicyModel).where(
        ArbiterPolicyModel.tenant_id.is_(None)
        | (ArbiterPolicyModel.tenant_id == tenant)
    )
    if namespace:
        q = q.where(ArbiterPolicyModel.namespace == namespace)
    rows = (await session.execute(q.order_by(ArbiterPolicyModel.name))).scalars().all()
    return [
        {
            "id": str(r.id),
            "name": r.name,
            "namespace": r.namespace,
            "description": r.description,
            "scope": "platform" if r.tenant_id is None else "tenant",
            "status": r.status,
            "active_version": r.active_version,
            "origin": r.origin,
            "retired_at": r.retired_at,
        }
        for r in rows
    ]


@router.post("/policies", status_code=201)
async def create_policy(
    body: PolicyIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("arbiter.policies.write")),
) -> dict[str, Any]:
    tenant = await _tenant_uuid(session, user)
    if body.platform:
        _require_platform_operator(user, "a platform-scoped policy")
    await _require_namespace(session, body.namespace)
    row = await store.create_policy(
        session,
        tenant_id=None if body.platform else tenant,
        name=body.name,
        namespace=body.namespace,
        description=body.description,
        created_by=await _actor_uuid(session, user),
    )
    await session.commit()
    return {"id": str(row.id), "status": row.status}


@router.post("/policies/{policy_id}/versions", status_code=201)
async def add_version(
    policy_id: uuid.UUID,
    body: VersionIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("arbiter.policies.write")),
) -> dict[str, Any]:
    await _require_visible(session, user, policy_id)
    try:
        rules = [Rule.model_validate(r) for r in body.rules]
    except Exception as exc:                                   # noqa: BLE001
        raise HTTPException(422, f"VEL-ARB-RULE-INVALID-422: {exc}") from exc
    try:
        row = await store.add_version(
            session,
            policy_id=policy_id,
            rules=rules,
            compiler=PolicyCompiler(VELARIS_CATALOG),
            mode=body.mode,
            created_by=await _actor_uuid(session, user),
            nl_source=body.nl_source,
            nl_readback=body.nl_readback,
        )
    except CompileError as exc:
        # A version that cannot compile is never stored — refusing here rather
        # than at activation keeps a trap out of the database.
        raise HTTPException(422, f"VEL-ARB-COMPILE-422: {exc}") from exc
    await session.commit()
    return {"version": row.version, "signature": row.signature}


@router.post("/policies/{policy_id}/activate")
async def activate(
    policy_id: uuid.UUID,
    version: int = Query(..., ge=1),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("arbiter.policies.activate")),
) -> dict[str, Any]:
    await _require_visible(session, user, policy_id)
    exists = (await session.execute(
        select(ArbiterPolicyVersionModel).where(
            ArbiterPolicyVersionModel.policy_id == policy_id,
            ArbiterPolicyVersionModel.version == version)
    )).scalars().first()
    if exists is None:
        raise HTTPException(404, "VEL-ARB-VERSION-404")
    await store.activate_version(session, policy_id=policy_id, version=version)
    await session.commit()
    return {"active_version": version}


@router.post("/policies/{policy_id}/retire")
async def retire(
    policy_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("arbiter.policies.activate")),
) -> dict[str, Any]:
    await _require_visible(session, user, policy_id)
    await store.retire_policy(session, policy_id=policy_id)
    await session.commit()
    return {"status": "retired"}


def _parse_moment(raw: str) -> datetime:
    """Parse an ISO timestamp from a query string, tolerating a lost `+`.

    `+00:00` is a legal offset and `+` is also how a query string spells a
    space, so an un-encoded ISO timestamp arrives here as `...263527 00:00` and
    a strict parser answers 422. Repairing it is not laxity: the alternative is
    that the most obvious way to call this endpoint is the one that fails, and
    every caller learns it the hard way.
    """
    text = raw.strip()
    if len(text) > 6 and text[-6] == " " and text[-3] == ":":
        text = text[:-6] + "+" + text[-5:]
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise HTTPException(422, "VEL-ARB-MOMENT-INVALID-422") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@router.get("/policies/{policy_id}/active-at")
async def active_at(
    policy_id: uuid.UUID,
    moment: str = Query(..., description="ISO-8601 instant"),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("arbiter.policies.read")),
) -> dict[str, Any]:
    """*Which version of this rule was in force at that moment.*

    The question the product is sold on, answered from the activation interval
    rather than reconstructed from a sampled log.
    """
    at = _parse_moment(moment)
    await _require_visible(session, user, policy_id)
    row = await store.version_active_at(session, policy_id=policy_id, moment=at)
    if row is None:
        return {"active": False, "moment": at}
    return {
        "active": True,
        "moment": at,
        "version": row.version,
        "signature": row.signature,
        "mode": row.mode,
        "activated_at": row.activated_at,
        "deactivated_at": row.deactivated_at,
        "rules": row.rules,
    }


async def _require_visible(
    session: AsyncSession, user: AuthenticatedUser, policy_id: uuid.UUID,
) -> ArbiterPolicyModel:
    """404 outside the caller's tenant — never 403, which confirms it exists."""
    tenant = await _tenant_uuid(session, user)
    row = (await session.execute(
        select(ArbiterPolicyModel).where(ArbiterPolicyModel.id == policy_id)
    )).scalars().first()
    if row is None or (row.tenant_id is not None and row.tenant_id != tenant):
        raise HTTPException(404, "VEL-ARB-POLICY-404")
    return row


# ── namespaces ───────────────────────────────────────────────────────────────


@router.post("/namespaces/{namespace}/promote")
async def promote(
    namespace: str,
    body: PromoteIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("arbiter.namespaces.promote")),
) -> dict[str, Any]:
    """Raise or lower a namespace ceiling — shadow, dry_run, enforce.

    Turning something on is an operator's decision and never an update's, which
    is why this exists as an explicit endpoint and why every namespace ships at
    `shadow`.
    """
    _require_platform_operator(user, "raising a namespace ceiling")
    exists = (await session.execute(
        select(ArbiterNamespaceModel)
        .where(ArbiterNamespaceModel.namespace == namespace)
    )).scalars().first()
    if exists is None:
        raise HTTPException(404, "VEL-ARB-NAMESPACE-404")

    # ARB-G — a promotion to `enforce` must be earned (§8 step 4).
    #
    # Lowering a ceiling is always permitted and never checked: backing out must
    # never be harder than going in. Only the direction that starts changing
    # answers is gated.
    report: dict[str, Any] | None = None
    if body.max_mode is PolicyMode.ENFORCE:
        tenant = await _tenant_uuid(session, user)
        report = (await parity.parity_report(
            session, tenant_id=tenant, namespace=namespace)).to_dict()
        if not report["eligible_for_promotion"]:
            if not body.override_parity:
                raise HTTPException(
                    409,
                    detail={
                        "code": "VEL-ARB-PARITY-409",
                        "message": (
                            "This namespace has not proven parity with the "
                            "control it shadows. Promoting now would change "
                            "answers that are correct today."),
                        "reasons": report["reasons"],
                        "report": report,
                    })
            if not body.override_reason.strip():
                # An override with no reason is an unattributable decision to
                # accept risk. The reason is the whole point of allowing it.
                raise HTTPException(
                    422,
                    "VEL-ARB-OVERRIDE-REASON-422: overriding the parity gate "
                    "requires a written reason.")
            log.warning(
                "hxarbiter: PARITY GATE OVERRIDDEN for namespace %s by %s — %s "
                "(reasons: %s)",
                namespace, _claimant(user), body.override_reason.strip(),
                "; ".join(report["reasons"]))

    await store.promote_namespace(session, namespace, body.max_mode)
    await session.commit()
    return {"namespace": namespace, "max_mode": str(body.max_mode),
            "parity": report,
            "override": bool(body.max_mode is PolicyMode.ENFORCE
                             and report and not report["eligible_for_promotion"])}


# ── decide / simulate ────────────────────────────────────────────────────────


@router.post("/decide")
async def decide(
    body: DecideIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("arbiter.decisions.evaluate")),
) -> dict[str, Any]:
    """The PDP for out-of-process callers. In-process callers use `pdp.decide()`.

    `subject`, `resource` and `context` are **PEP-supplied**: a trusted caller
    describing what it is about to do. The authenticated caller is stamped onto
    the decision row so a claim made here is attributable.
    """
    req = DecisionRequest(
        tenant_id=_tenant_token(user),
        subject=body.subject,
        action=body.action,
        resource=body.resource,
        context=body.context,
    )
    decision = await pdp.decide(
        session, req,
        request_id=request.headers.get("x-request-id"),
        claimed_by=_claimant(user),
    )
    await session.commit()
    return decision.model_dump(mode="json")


@router.post("/simulate")
async def simulate(
    body: SimulateIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("arbiter.simulate.run")),
    _rl: None = Depends(_simulate_rl),
) -> dict[str, Any]:
    """What-if, through the same evaluator the real path uses.

    The supplied `attributes` stand in for what a PIP would resolve, so an
    operator can ask "suppose this tenant were in eu-west" without changing
    anything. The decision is written with `simulated=True` and enforces nothing.
    """
    req = DecisionRequest(
        tenant_id=_tenant_token(user),
        subject=body.subject,
        action=body.action,
        resource=body.resource,
        context=body.context,
    )
    resolvers = [RequestResolver()]
    for prefix in {p.split(".", 1)[0] for p in body.attributes}:
        resolvers.append(StaticResolver(prefix, body.attributes))

    decision = await pdp.decide(session, req, resolvers=resolvers,
                                simulated=True, claimed_by=_claimant(user))
    await session.commit()
    return decision.model_dump(mode="json")


# ── ARB-F: natural-language authoring ────────────────────────────────────────


class DraftIn(_Strict):
    #: What the operator wants, in their own words. Kept verbatim: the readback
    #: is compared against THIS, and paraphrasing it destroys the comparison.
    intent: str = Field(min_length=8, max_length=4000)
    namespace: str = Field(min_length=1, max_length=64)
    policy_id: uuid.UUID | None = None
    corpus_refs: list[str] = Field(default_factory=list, max_length=32)


class ApproveIn(_Strict):
    #: The approver may edit the readback before signing. What they sign is what
    #: is stored — not what the model wrote.
    readback: str = Field(min_length=1, max_length=8000)


@router.post("/drafts", status_code=201)
async def create_draft(
    body: DraftIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("arbiter.drafts.write")),
    _rl: None = Depends(_draft_rl),
) -> dict[str, Any]:
    """Describe a policy in words; get a draft, an analysis and a readback.

    The model proposes. Nothing here is active, and nothing here can become
    active without a human approving the readback (`POST /drafts/{id}/approve`)
    and then activating a version through the ordinary path.

    **No provider configured is not an error condition for the platform** — it
    is a 503 on this endpoint alone, and every other part of the engine works
    exactly as before.
    """
    from velaris_shared.db.models import ArbiterDraftModel, ArbiterNamespaceModel

    tenant = await _tenant_uuid(session, user)
    ns = (await session.execute(
        select(ArbiterNamespaceModel)
        .where(ArbiterNamespaceModel.namespace == body.namespace)
    )).scalars().first()
    if ns is None:
        raise HTTPException(404, "VEL-ARB-NAMESPACE-404")

    # `policy_id` says "this draft proposes a new version OF THAT POLICY", and it
    # is caller-supplied. Unchecked, it is a cross-tenant WRITE on the security
    # engine itself: name another tenant's policy here, have the draft approved
    # inside your own tenant, activate it, and a new version is appended to
    # theirs. Checked at creation and again at activation — the second is not
    # redundant, it is the half that actually writes.
    if body.policy_id is not None:
        await _require_visible(session, user, body.policy_id)

    try:
        provider = await _provider.resolve_governed(
            session, tenant_slug=_tenant_token(user))
    except _provider.ProviderUnavailable as exc:
        raise HTTPException(
            503,
            f"VEL-ARB-NO-PROVIDER-503: natural-language authoring needs an AI "
            f"provider, and none is available for this tenant ({exc}). Policies "
            f"can still be written by hand.") from exc

    result = await _author.draft_rules(
        provider.backend, intent=body.intent, catalog=VELARIS_CATALOG,
        namespace=body.namespace, default_effect=ns.default_effect)

    # A draft the model could not produce is not a draft. Storing one leaves a
    # row that looks like work and carries none, and it was what a mis-shaped
    # provider produced instead of the honest 503 above.
    if not result.ok and not result.rules:
        raise HTTPException(
            422,
            {"code": "VEL-ARB-DRAFT-FAILED-422",
             "message": "The model did not produce rules that compile. Nothing "
                        "was saved; the errors are below, unrepaired.",
             "errors": result.errors,
             "attempts": result.attempts})

    findings = _analyzer.analyze(result.rules) if result.ok else []
    # What this draft would have done to traffic that really happened (§7.2).
    # `arbiter_drafts.whatif_report` has existed since migration 179 and
    # nothing ever wrote to it.
    whatif = None
    if result.ok:
        try:
            whatif = await _whatif.replay(
                session, rules=result.rules, namespace=body.namespace,
                tenant_id=tenant, catalog=VELARIS_CATALOG)
        except Exception as exc:                               # noqa: BLE001
            # A replay is context for a human, never a gate. Losing it must not
            # cost the operator the draft they just waited on a model for.
            log.warning("hxarbiter: what-if replay failed: %s", exc)
    seeds = [{"rule_id": r.id, "requests": _analyzer.seed_requests(r)}
             for r in result.rules] if result.ok else []
    text, source = await _readback.readback(
        provider.backend if result.ok else None, result.rules,
        default_effect=ns.default_effect) if result.ok else ("", "deterministic")

    draft = ArbiterDraftModel(
        id=uuid.uuid4(),
        policy_id=body.policy_id,
        tenant_id=tenant,
        namespace=body.namespace,
        intent=body.intent,
        generated_rules=[r.model_dump(mode="json", by_alias=True)
                         for r in result.rules],
        compile_report={"ok": result.ok, "errors": result.errors},
        analysis_report=[f.to_dict() for f in findings],
        readback=text or None,
        readback_source=source,
        test_seeds=seeds,
        whatif_report=whatif,
        model_provider=provider.provider_class,
        model_name=provider.name,
        prompt_version="arb-f-1",
        attempts=result.attempts,
        corpus_refs=body.corpus_refs or None,
        state=str(lifecycle.DraftState.ANALYZED if result.ok
                  else lifecycle.DraftState.DRAFTED),
        created_by=await _actor_uuid(session, user),
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    session.add(draft)
    await session.commit()

    return {
        "id": str(draft.id),
        "state": draft.state,
        "compiled": result.ok,
        "errors": result.errors,
        "rules": draft.generated_rules,
        "analysis": draft.analysis_report,
        "readback": draft.readback,
        "readback_source": draft.readback_source,
        "whatif": draft.whatif_report,
        "provenance": {"provider": provider.provider_class,
                       "model": provider.name,
                       "prompt_version": "arb-f-1",
                       "attempts": result.attempts},
    }


@router.get("/drafts/{draft_id}")
async def get_draft(
    draft_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("arbiter.drafts.write")),
) -> dict[str, Any]:
    draft = await _draft_or_404(session, user, draft_id)
    return {
        "id": str(draft.id), "state": draft.state, "namespace": draft.namespace,
        "intent": draft.intent, "rules": draft.generated_rules,
        "analysis": draft.analysis_report, "readback": draft.readback,
        "readback_source": draft.readback_source, "test_seeds": draft.test_seeds,
        "whatif": draft.whatif_report,
        "provenance": {"provider": draft.model_provider,
                       "model": draft.model_name,
                       "prompt_version": draft.prompt_version,
                       "attempts": draft.attempts},
        "corpus_refs": draft.corpus_refs,
        "created_at": draft.created_at,
    }


@router.post("/drafts/{draft_id}/approve")
async def approve_draft(
    draft_id: uuid.UUID,
    body: ApproveIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("arbiter.drafts.approve")),
) -> dict[str, Any]:
    """Sign off the readback. Four-eyes applies (§7.2 step 6, OD-4).

    What the approver signs is stored verbatim as `nl_readback` on the version —
    it is what was agreed to, and it is what an auditor is shown beside the
    rules.
    """
    draft = await _draft_or_404(session, user, draft_id)
    draft.readback = body.readback
    if draft.state == str(lifecycle.DraftState.ANALYZED):
        await lifecycle.transition(
            session, draft, lifecycle.DraftState.READBACK_PENDING)
    try:
        mode = await lifecycle.transition(
            session, draft, lifecycle.DraftState.APPROVED,
            approver=await _actor_uuid(session, user))
    except lifecycle.LifecycleError as exc:
        raise HTTPException(409, f"VEL-ARB-LIFECYCLE-409: {exc}") from exc
    await session.commit()
    return {"id": str(draft.id), "state": draft.state, "approval": mode}


@router.post("/drafts/{draft_id}/activate", status_code=201)
async def activate_draft(
    draft_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("arbiter.policies.activate")),
) -> dict[str, Any]:
    """Turn an approved draft into a real policy version, in `shadow`.

    It lands at `shadow` whatever the namespace ceiling is: a rule a model
    proposed goes into the same soak as everything else, and there is no path
    from "the LLM suggested it" to "it is enforcing" that skips a human twice.
    """
    draft = await _draft_or_404(session, user, draft_id)
    if draft.state != str(lifecycle.DraftState.APPROVED):
        raise HTTPException(
            409, "VEL-ARB-LIFECYCLE-409: only an approved draft can be activated")

    tenant = await _tenant_uuid(session, user)
    policy_id = draft.policy_id
    if policy_id is not None:
        # Re-checked here, at the point of writing. A draft row outlives the
        # request that made it, and this is the call that appends a version.
        await _require_visible(session, user, policy_id)
    if policy_id is None:
        created = await store.create_policy(
            session, tenant_id=tenant,
            name=f"{draft.namespace}: {draft.intent[:80]}",
            namespace=draft.namespace,
            description="Authored from natural language (HxArbiter ARB-F).",
            created_by=await _actor_uuid(session, user))
        policy_id = created.id

    try:
        rules = [Rule.model_validate(r) for r in (draft.generated_rules or [])]
        version = await store.add_version(
            session, policy_id=policy_id, rules=rules,
            compiler=PolicyCompiler(VELARIS_CATALOG),
            mode=PolicyMode.SHADOW,
            created_by=draft.created_by,
            nl_source=draft.intent,
            nl_readback=draft.readback)
    except CompileError as exc:
        raise HTTPException(422, f"VEL-ARB-COMPILE-422: {exc}") from exc

    await store.activate_version(
        session, policy_id=policy_id, version=version.version)
    await lifecycle.transition(session, draft, lifecycle.DraftState.ACTIVATED)
    await session.commit()
    return {"policy_id": str(policy_id), "version": version.version,
            "mode": "shadow"}


async def _draft_or_404(session, user, draft_id: uuid.UUID):
    from velaris_shared.db.models import ArbiterDraftModel

    tenant = await _tenant_uuid(session, user)
    row = (await session.execute(
        select(ArbiterDraftModel).where(ArbiterDraftModel.id == draft_id)
    )).scalars().first()
    if row is None or (row.tenant_id is not None and row.tenant_id != tenant):
        raise HTTPException(404, "VEL-ARB-DRAFT-404")
    return row


# ── parity: the evidence a promotion rests on ────────────────────────────────


@router.get("/parity/{namespace}")
async def parity_for(
    namespace: str,
    window_days: int = Query(parity.DEFAULT_SOAK_DAYS, ge=1, le=365),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("arbiter.parity.read")),
) -> dict[str, Any]:
    """Agreement between the engine and the control it shadows.

    `eligible_for_promotion` is false with a reason attached whenever the
    evidence does not support enforcing — including the case operators find
    most surprising: **a namespace nobody exercised agrees perfectly**, and a
    green light earned by silence is the most dangerous report here.
    """
    tenant = await _tenant_uuid(session, user)
    report = await parity.parity_report(
        session, tenant_id=tenant, namespace=namespace, window_days=window_days)
    return report.to_dict()


@router.post("/bridge/sync")
async def bridge_sync(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("arbiter.bridge.sync")),
    _rl: None = Depends(_bridge_rl),
) -> dict[str, Any]:
    """Regenerate the read-only mirrors of the legacy controls (§8 step 1).

    Idempotent and content-addressed: a mirror whose rules have not changed
    appends no version, so running this on every boot does not fill the
    activation history — which is an audit record — with identical entries.
    """
    tenant = await _tenant_uuid(session, user)

    settings: dict[str, Any] = {}
    try:
        from velaris_shared.db.models import TenantModel
        row = (await session.execute(
            select(TenantModel).where(TenantModel.id == tenant))).scalars().first()
        settings = dict(row.settings or {}) if row else {}
    except Exception:                                          # noqa: BLE001
        settings = {}
    egress = str(((settings.get("ai") or {}).get("egress")) or "local_only").lower()

    out: dict[str, Any] = {}
    _, changed = await legacy_bridge.mirror_ai_egress(
        session, tenant_id=tenant, local_only=egress != "external_allowed")
    out["ai"] = {"local_only": egress != "external_allowed", "changed": changed}

    domains = await _installed_outbound_domains(session, tenant)
    _, changed = await legacy_bridge.mirror_connector_egress(
        session, tenant_id=tenant, domains=domains)
    out["egress"] = {"domains": domains, "changed": changed}

    await session.commit()
    return out


async def _installed_outbound_domains(
    session: AsyncSession, tenant: uuid.UUID,
) -> list[str]:
    """Union of `outbound_domains` across this tenant's installed packages.

    An empty result is meaningful and is NOT an error: the marketplace's own
    Layer-2 default is egress-DROP, so no declared host means nothing may leave,
    and the mirror says exactly that by emitting no allow rule.
    """
    try:
        from velaris_shared.db.models import MarketplaceCapabilityGrantModel

        rows = (await session.execute(
            select(MarketplaceCapabilityGrantModel.granted)
            .where(MarketplaceCapabilityGrantModel.tenant_id == str(tenant))
        )).scalars().all()
        domains: set[str] = set()
        for granted in rows:
            for d in (granted or {}).get("outbound_domains", []) or []:
                domains.add(str(d).lower())
        return sorted(domains)
    except Exception:                                          # noqa: BLE001
        return []


# ── decision log ─────────────────────────────────────────────────────────────


@router.get("/decisions")
async def list_decisions(
    namespace: str | None = Query(None),
    subject_id: str | None = Query(None),
    disagreements_only: bool = Query(False),
    limit: int = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("arbiter.decisions.read")),
) -> list[dict[str, Any]]:
    tenant = await _tenant_uuid(session, user)
    q = select(ArbiterDecisionModel).where(ArbiterDecisionModel.tenant_id == tenant)
    if namespace:
        q = q.where(ArbiterDecisionModel.namespace == namespace)
    if subject_id:
        q = q.where(ArbiterDecisionModel.subject_id == subject_id)
    if disagreements_only:
        # The whole output of a parity report: where the engine and the control
        # it is replacing did not agree.
        q = q.where(
            ArbiterDecisionModel.legacy_effect.isnot(None),
            ArbiterDecisionModel.legacy_effect != ArbiterDecisionModel.effect,
        )
    rows = (await session.execute(
        q.order_by(ArbiterDecisionModel.evaluated_at.desc()).limit(limit)
    )).scalars().all()
    return [
        {
            "id": str(r.id),
            "namespace": r.namespace,
            "action": r.action,
            "subject_kind": r.subject_kind,
            "subject_id": r.subject_id,
            "effect": r.effect,
            "mode": r.mode,
            "enforced": r.enforced,
            "default_applied": r.default_applied,
            "matched": r.matched,
            "obligations": r.obligations,
            "legacy_effect": r.legacy_effect,
            "error": r.error,
            "claimed_by": r.claimed_by,
            "simulated": r.simulated,
            "latency_us": r.latency_us,
            "sealed": r.chain_sequence is not None,
            "evaluated_at": r.evaluated_at,
        }
        for r in rows
    ]


@router.get("/decisions/verify")
async def verify(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("arbiter.decisions.read")),
) -> dict[str, Any]:
    """Recompute every link in the decision chain.

    `ok: false` means the decision log has been altered since it was sealed —
    which is the entire point of sealing it.
    """
    ok, problem = await chain.verify_chain(session)
    return {"ok": ok, "problem": problem}


@router.post("/decisions/seal")
async def seal(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("arbiter.decisions.read")),
    _rl: None = Depends(_seal_rl),
) -> dict[str, Any]:
    """Seal ready batches now, rather than waiting for the loop.

    The chain spans every tenant — it has to, or a per-tenant chain would let
    one tenant's rewrite go unnoticed by the others' verification. Sealing is
    therefore a platform-wide write and not a tenant admin's to trigger.
    """
    _require_platform_operator(user, "sealing the decision chain")
    return {"sealed": await chain.seal_once(session)}


# ── upkeep (ARB-I) ───────────────────────────────────────────────────────────


class AdviseIn(_Strict):
    #: Same look-back as the report being explained. Passed rather than implied
    #: so the advice and the findings cannot describe different windows.
    window_days: int = Field(default=upkeep.DEFAULT_WINDOW_DAYS, ge=1, le=365)
    #: Explain only these findings. Ids come from `GET /upkeep`; anything the
    #: caller invents simply matches nothing.
    finding_ids: list[str] = Field(default_factory=list, max_length=100)


@router.get("/upkeep")
async def get_upkeep(
    window_days: int = Query(upkeep.DEFAULT_WINDOW_DAYS, ge=1, le=365),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("arbiter.upkeep.read")),
    _rl: None = Depends(_upkeep_rl),
) -> dict[str, Any]:
    """What the live policy set is quietly doing wrong. Deterministic; no AI.

    Read-only in the strongest sense: it writes nothing, evaluates nothing, and
    proposes nothing. Every finding is computed from the database, so an
    install with no AI provider gets this report in full — the model in
    `/upkeep/advise` only ever narrates it.

    A platform operator's report scans **every tenant's** decisions, because a
    platform policy binds every tenant and judged from inside one it looks
    dead. A tenant admin's report says so instead of quietly guessing.
    """
    tenant = await _tenant_uuid(session, user)
    report = await upkeep.upkeep_report(
        session, tenant_id=tenant, platform_scope=is_superadmin(user),
        window_days=window_days, catalog=VELARIS_CATALOG)
    return report.to_dict()


@router.post("/upkeep/advise")
async def advise_upkeep(
    body: AdviseIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("arbiter.upkeep.read")),
    _rl: None = Depends(_draft_rl),
) -> dict[str, Any]:
    """HxNexus reading the upkeep report back. Proposes; decides nothing.

    The findings are recomputed here rather than accepted from the body. A
    caller-supplied finding list would let anyone put arbitrary text in front
    of a model that is, on a `local_only` tenant, running on this box — and it
    would let a screen show advice about a finding the database never
    produced.

    The model sees `upkeep.findings_for_advice(...)`: kinds, counts,
    operator-written policy names and rule ids. No subject, no resource, no
    decision row (§19.5). Its reply is matched back against the ids that were
    sent, so it cannot add a finding to the screen.
    """
    tenant = await _tenant_uuid(session, user)
    report = await upkeep.upkeep_report(
        session, tenant_id=tenant, platform_scope=is_superadmin(user),
        window_days=body.window_days, catalog=VELARIS_CATALOG)

    findings = report.findings
    if body.finding_ids:
        wanted = set(body.finding_ids)
        findings = [f for f in findings if f.finding_id in wanted]

    try:
        provider = await _provider.resolve_governed(
            session, tenant_slug=_tenant_token(user))
    except _provider.ProviderUnavailable as exc:
        # The feature is absent, not broken. The report above is complete
        # without it, and every other part of the engine is unaffected.
        raise HTTPException(
            503,
            f"VEL-ARB-NO-PROVIDER-503: explaining an upkeep report needs an AI "
            f"provider, and none is available for this tenant ({exc}). The "
            f"findings themselves are computed without one.") from exc

    advice = await _advisor.advise(
        provider.backend, upkeep.findings_for_advice(findings))

    return {
        "window_days": body.window_days,
        "explained": len(advice.entries),
        "of_findings": len(findings),
        **advice.to_dict(),
        "provenance": {"provider": provider.provider_class,
                       "model": provider.name,
                       "prompt_version": _advisor.PROMPT_VERSION},
    }


# ── portability: moving a rule set between installs ──────────────────────────


class ImportIn(_Strict):
    #: The document a previous `GET /export` produced.
    document: dict[str, Any]
    #: Import as PLATFORM policies. A separate, deliberate act for the same
    #: reason creating one is: a platform policy binds every tenant on the box.
    platform: bool = False
    #: Refuse rather than append when a policy of the same name already exists.
    #: Default True: silently creating a second policy called "EU lockout" is
    #: how two contradictory controls end up both active and nobody notices.
    refuse_duplicates: bool = True
    #: Activate what is imported. Off by default — an import is a delivery, and
    #: putting somebody else's rules into force is a separate decision.
    activate: bool = False


@router.get("/export")
async def export_policies(
    namespace: str | None = Query(None),
    include_retired: bool = Query(False),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("arbiter.policies.read")),
) -> dict[str, Any]:
    """The tenant's rule set as a portable document.

    There was no way to get a policy set out of an install: no backup short of
    a database dump, and no way to promote a rule set from staging to
    production or hand one to another deployment.

    What it deliberately does NOT carry:

    * **the decision log** — that is evidence about one deployment and means
      nothing in another, and it is hash-chained where it lives;
    * **`activated_at` / `deactivated_at`** — the activation interval is a fact
      about the install that ran the policy, and copying it would put a
      fabricated history into the importing install's audit answer;
    * **signatures** — a version's signature is recomputed on import from the
      rules themselves, so a doctored document produces a different signature
      rather than carrying a valid-looking one.

    Only the ACTIVE version of each policy is exported. A version history is
    the record of how this install got here; the importing one has its own.
    """
    tenant = await _tenant_uuid(session, user)
    q = select(ArbiterPolicyModel).where(
        ArbiterPolicyModel.tenant_id.is_(None)
        | (ArbiterPolicyModel.tenant_id == tenant))
    if namespace:
        q = q.where(ArbiterPolicyModel.namespace == namespace)
    if not include_retired:
        q = q.where(ArbiterPolicyModel.status == "active")
    policies = (await session.execute(
        q.order_by(ArbiterPolicyModel.name))).scalars().all()

    out: list[dict[str, Any]] = []
    for p in policies:
        if p.active_version is None:
            continue
        version = (await session.execute(
            select(ArbiterPolicyVersionModel).where(
                ArbiterPolicyVersionModel.policy_id == p.id,
                ArbiterPolicyVersionModel.version == p.active_version)
        )).scalars().first()
        if version is None:
            continue
        out.append({
            "name": p.name,
            "namespace": p.namespace,
            "description": p.description,
            "scope": "platform" if p.tenant_id is None else "tenant",
            "origin": p.origin,
            "mode": version.mode,
            "rules": version.rules,
            "nl_source": version.nl_source,
            "nl_readback": version.nl_readback,
            # Informational only: the importing install recomputes it. Carried
            # so a reviewer can diff two documents without importing either.
            "source_signature": version.signature,
        })

    namespaces = (await session.execute(
        select(ArbiterNamespaceModel).order_by(ArbiterNamespaceModel.namespace)
    )).scalars().all()
    return {
        "format": "hxarbiter-policy-export",
        "version": 1,
        "exported_at": iso_utc(datetime.now(timezone.utc)),
        # The catalogue the rules were written against. An importing install
        # with a different catalogue will fail to compile them, and that is the
        # correct outcome — but it should be able to say WHY without guessing.
        "catalog": [a.model_dump() for a in VELARIS_CATALOG.as_list()],
        # Namespace DEFAULTS travel; `max_mode` does not. A ceiling is a
        # statement about the importing deployment's rollout, and importing one
        # would let a document switch enforcement on somewhere else.
        "namespaces": [{"namespace": n.namespace,
                        "default_effect": n.default_effect} for n in namespaces],
        "policies": out,
    }


@router.post("/import", status_code=201)
async def import_policies(
    body: ImportIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("arbiter.policies.write")),
    _rl: None = Depends(_bridge_rl),
) -> dict[str, Any]:
    """Load an exported rule set into this install.

    Everything lands in **`shadow`** regardless of the mode in the document,
    and `activate` defaults to False. A document is somebody else's judgement
    about somebody else's platform; it becomes this platform's judgement when a
    person here says so, through the same soak every other policy goes through.

    A rule that does not compile against THIS install's catalogue is refused
    with the attribute named — never imported and left inert, which is how a
    control ends up existing on a screen and applying to nothing.
    """
    doc = body.document
    if doc.get("format") != "hxarbiter-policy-export":
        raise HTTPException(422, "VEL-ARB-IMPORT-FORMAT-422: not an HxArbiter "
                                 "policy export document")
    entries = doc.get("policies")
    if not isinstance(entries, list) or not entries:
        raise HTTPException(422, "VEL-ARB-IMPORT-EMPTY-422: the document "
                                 "carries no policies")
    if len(entries) > 500:
        raise HTTPException(422, "VEL-ARB-IMPORT-TOO-LARGE-422")

    if body.platform:
        _require_platform_operator(user, "importing platform-scoped policies")

    tenant = await _tenant_uuid(session, user)
    target_tenant = None if body.platform else tenant
    compiler = PolicyCompiler(VELARIS_CATALOG)

    existing = {
        p.name for p in (await session.execute(
            select(ArbiterPolicyModel).where(
                ArbiterPolicyModel.tenant_id.is_(None)
                | (ArbiterPolicyModel.tenant_id == tenant))
        )).scalars().all()
    }

    # Two passes. Everything is compiled BEFORE anything is written, so a
    # document whose fifth policy is broken does not leave four half-imported
    # rules behind and a 422 explaining why.
    known = {
        n.namespace for n in (await session.execute(
            select(ArbiterNamespaceModel))).scalars().all()
    }

    staged: list[tuple[dict[str, Any], list[Rule]]] = []
    problems: list[dict[str, Any]] = []
    for entry in entries:
        name = str(entry.get("name") or "").strip()
        if not name or not entry.get("namespace"):
            problems.append({"name": name or "<unnamed>",
                             "error": "missing name or namespace"})
            continue
        if str(entry["namespace"]) not in known:
            # Checked here, in the staging pass, so it is reported with every
            # other problem in one 422 rather than as an FK 500 partway
            # through the write.
            problems.append({
                "name": name,
                "error": f"namespace {entry['namespace']!r} is not registered "
                         f"on this install"})
            continue
        if body.refuse_duplicates and name in existing:
            problems.append({"name": name,
                             "error": "a policy of this name already exists here"})
            continue
        try:
            rules = [Rule.model_validate(r) for r in (entry.get("rules") or [])]
            if not rules:
                raise ValueError("no rules")
            report = compiler.check_version(PolicyVersion(
                policy_id=uuid.UUID(int=0), version=1, rules=rules))
            if not report.ok:
                raise ValueError("; ".join(report.errors))
        except Exception as exc:                               # noqa: BLE001
            problems.append({"name": name, "error": str(exc)})
            continue
        staged.append((entry, rules))

    if problems:
        raise HTTPException(422, {
            "code": "VEL-ARB-IMPORT-REJECTED-422",
            "message": "Nothing was imported. Every policy in a document must "
                       "be loadable, or a partial import leaves a rule set "
                       "that nobody wrote.",
            "problems": problems})

    imported: list[dict[str, Any]] = []
    for entry, rules in staged:
        policy = await store.create_policy(
            session, tenant_id=target_tenant,
            name=str(entry["name"])[:255],
            namespace=str(entry["namespace"])[:64],
            description=str(entry.get("description") or "")[:2000],
            created_by=await _actor_uuid(session, user))
        version = await store.add_version(
            session, policy_id=policy.id, rules=rules, compiler=compiler,
            # Shadow, whatever the document said. See the docstring.
            mode=PolicyMode.SHADOW,
            created_by=await _actor_uuid(session, user),
            nl_source=entry.get("nl_source"),
            nl_readback=entry.get("nl_readback"))
        if body.activate:
            await store.activate_version(
                session, policy_id=policy.id, version=version.version)
        imported.append({"id": str(policy.id), "name": policy.name,
                         "namespace": policy.namespace,
                         "version": version.version,
                         "signature": version.signature,
                         "activated": body.activate})
    await session.commit()
    return {"imported": len(imported), "mode": "shadow",
            "activated": body.activate, "policies": imported}


# ── evidence: anchoring and retention ────────────────────────────────────────


@router.post("/decisions/anchor")
async def anchor_decisions(
    force: bool = Query(False),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("arbiter.decisions.read")),
    _rl: None = Depends(_seal_rl),
) -> dict[str, Any]:
    """Obtain an RFC-3161 receipt over the decision chain's tip.

    The chain is hash-linked, which is tamper-evident against a writer. It is
    not evidence against someone who can rewrite the whole chain — and that is
    the party a sovereignty product is asked about. An external timestamp
    closes it: nothing older than the last receipt can be forged.

    Platform-wide, like sealing, and for the same reason: the chain spans every
    tenant, so anchoring it is not a tenant admin's action.
    """
    _require_platform_operator(user, "anchoring the decision chain")
    from case_service.compliance import audit_anchor

    try:
        result = await audit_anchor.anchor_chain_tip(
            session, force=force, chain=audit_anchor.CHAIN_ARBITER)
    except Exception as exc:                                   # noqa: BLE001
        # A TSA is an external service. Failing to reach it must not read like
        # a broken chain, which is what an unhandled 500 here would suggest.
        raise HTTPException(
            502, f"VEL-ARB-ANCHOR-502: the timestamp authority could not be "
                 f"used ({exc}). The chain itself is unaffected.") from exc
    await session.commit()
    return result


@router.put("/namespaces/{namespace}/retention")
async def set_retention(
    namespace: str,
    body: RetentionIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("arbiter.namespaces.promote")),
) -> dict[str, Any]:
    """How long decisions in this namespace are kept. `null` means forever.

    Platform-operator only, and gated with promotion rather than with reading:
    the namespace registry is not tenant-scoped (a ceiling a tenant could raise
    would not be one), and this decides how long the evidence about **every**
    tenant survives.

    Setting it does not delete anything. `POST /decisions/purge` does, and is a
    separate deliberate act — so an operator can declare a horizon, look at
    what it would remove, and act second.
    """
    _require_platform_operator(user, "setting a decision-retention horizon")
    ns = await _require_namespace(session, namespace)
    before = ns.decision_retention_days
    ns.decision_retention_days = body.decision_retention_days
    ns.updated_at = datetime.now(timezone.utc)
    await session.commit()
    log.warning(
        "hxarbiter: decision retention for namespace %s changed from %s to %s "
        "by %s", namespace, before, body.decision_retention_days,
        _claimant(user))
    return {"namespace": namespace,
            "decision_retention_days": ns.decision_retention_days,
            "previous": before}


@router.post("/decisions/purge")
async def purge_decisions(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("arbiter.namespaces.promote")),
    _rl: None = Depends(_seal_rl),
) -> dict[str, Any]:
    """Delete decisions past their namespace's retention horizon.

    Gated on `namespaces.promote`, not `decisions.read`: this is the only call
    in the module that destroys evidence, and it belongs with the other action
    that changes what the platform does rather than with reading.

    A namespace whose `decision_retention_days` is unset keeps everything, which
    is every namespace until an operator says otherwise. Unsealed rows are never
    touched — deleting one would be indistinguishable from tampering.
    """
    _require_platform_operator(user, "purging the decision log")
    purged = await chain.purge_expired_decisions(session)
    await session.commit()
    return {"purged": purged, "total": sum(purged.values())}
