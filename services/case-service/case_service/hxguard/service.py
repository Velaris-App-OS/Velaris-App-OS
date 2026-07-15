"""HxGuard Phase A — the policy decision point.

Design rules (report §3.2, all three SPOF mitigations are requirements):
  - FAIL-CLOSED: unknown action, unknown subject kind, or any evaluator
    exception → deny. There is no permissive default anywhere in this module.
  - Short-TTL decision cache (in-process) with explicit invalidation on
    role/group/grant mutations — availability mitigation + per-request cost.
  - Every DENY is a SecurityEvent (HxShield-visible); allows are counted,
    not logged.

The backend is a swappable protocol: Phase A ships RbacBackend (wrapping
require_role semantics + namespace_grants); Phase C may swap in OpenFGA
behind the same check() signature.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser
from case_service.db.session import get_session

log = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 30.0


@dataclass(frozen=True)
class Subject:
    """Who is asking. kind mirrors case_vars.CallerContext kinds plus 'user'."""
    kind: str                      # user | connector | devconn | rules | module
    id: str
    roles: tuple[str, ...] = ()
    is_admin: bool = False


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str
    source: str                    # which backend/rule decided
    cached: bool = False


class AuthzBackend(Protocol):
    name: str
    async def evaluate(
        self, session: AsyncSession, subject: Subject,
        action: str, resource: dict[str, Any] | None,
    ) -> Decision: ...


# ── RBAC backend (Phase A) ───────────────────────────────────────────

#: action → roles that may perform it. Mirrors the require_role() semantics
#: it replaces on the pilot surfaces: superadmin and is_admin always pass,
#: empty set = any *authenticated* subject of kind "user".
#: An action ABSENT from this registry (and not grant-backed) is DENIED.
ACTION_ROLES: dict[str, set[str]] = {
    "variables.namespace.admin":     {"admin"},
    "variables.scan":                {"admin", "designer"},
    "variables.promote":             {"admin"},
    "connector.namespace.register":  {"admin", "integration"},
    "devconn.namespace.register":    {"admin", "integration", "developer"},
    "case.rule.apply":               set(),     # any authenticated user
    "rules.write":                   {"admin", "designer"},  # rule CRUD gates automation behavior
    "replay.run":                    {"admin"},  # HxReplay cohort replay = bulk case read
    "costing.rates":                 {"admin"},  # rate cards are commercially sensitive
    "mcp.tokens.mint":               {"admin", "designer"},  # P4 scoped external-agent tokens
}

#: grant-backed actions: resolved against namespace_grants, not roles.
GRANT_ACTIONS = {
    "namespace.write": "write",
    "namespace.read":  "read",
}


class RbacBackend:
    name = "rbac"

    async def evaluate(
        self, session: AsyncSession, subject: Subject,
        action: str, resource: dict[str, Any] | None,
    ) -> Decision:
        # grant-backed actions (namespace capabilities) — any subject kind
        if action in GRANT_ACTIONS:
            return await self._evaluate_grant(session, subject, action, resource)

        # role-backed actions — user subjects only
        roles = ACTION_ROLES.get(action)
        if roles is None:
            return Decision(False, f"unknown action '{action}' (fail-closed)", self.name)
        if subject.kind != "user":
            return Decision(False, f"action '{action}' requires a user subject, got {subject.kind}", self.name)
        if "superadmin" in subject.roles or subject.is_admin:
            return Decision(True, "admin bypass", self.name)
        if not roles:
            return Decision(True, "any authenticated user", self.name)
        matched = roles & set(subject.roles)
        if matched:
            return Decision(True, f"role {sorted(matched)[0]}", self.name)
        return Decision(False, f"requires one of {sorted(roles)}", self.name)

    async def _evaluate_grant(
        self, session: AsyncSession, subject: Subject,
        action: str, resource: dict[str, Any] | None,
    ) -> Decision:
        namespace_id = (resource or {}).get("namespace_id")
        if namespace_id is None:
            return Decision(False, f"'{action}' requires resource.namespace_id (fail-closed)", self.name)
        from sqlalchemy import select
        from case_service.db.models import NamespaceGrantModel
        row = (await session.execute(
            select(NamespaceGrantModel.id)
            .where(NamespaceGrantModel.namespace_id == namespace_id)
            .where(NamespaceGrantModel.grantee_type == subject.kind)
            .where(NamespaceGrantModel.grantee_ref.in_((subject.id, "*")))
            .where(NamespaceGrantModel.capability == GRANT_ACTIONS[action])
            .limit(1)
        )).scalar_one_or_none()
        if row is not None:
            return Decision(True, f"namespace grant ({GRANT_ACTIONS[action]})", self.name)
        return Decision(
            False,
            f"no {GRANT_ACTIONS[action]} grant for {subject.kind} '{subject.id}' on namespace {namespace_id}",
            self.name,
        )


# ── ReBAC backend (Phase B — case relationships) ─────────────────────

#: action → relations that satisfy it. owner (created_by) and admin/manager
#: roles are derived at decision time, never stored as tuples.
CASE_ACTIONS: dict[str, set[str]] = {
    "case.read":   {"assignee", "editor", "viewer"},
    "case.update": {"assignee", "editor"},
    "case.share":  {"editor"},
    "meet.start":  {"assignee", "editor"},   # HxMeet: start/end a live session = case work
    "meet.join":   {"assignee", "editor", "viewer"},  # HxMeet P2: join an embedded session in-tab
    "meet.recording.view": {"assignee", "editor"},    # HxMeet P3: download/verify a sealed recording
    "messages.read":  {"assignee", "editor", "viewer"},  # Portal v2 P4: read the case thread
    "messages.write": {"assignee", "editor"},            # Portal v2 P4: post to the case thread
    "meet.intelligence.run": {"assignee", "editor"},     # HxMeet P4a: analyze a sealed recording
    "docs.verify":    {"assignee", "editor"},            # HxMeet P4b: record a document verification
    "cases.ask":      {"assignee", "editor", "viewer"},  # HxNexus case Q&A: read-derived
}


class RebacBackend:
    name = "rebac"

    async def evaluate(
        self, session: AsyncSession, subject: Subject,
        action: str, resource: dict[str, Any] | None,
    ) -> Decision:
        relations = CASE_ACTIONS.get(action)
        if relations is None:
            return Decision(False, f"unknown case action '{action}' (fail-closed)", self.name)
        case_id = (resource or {}).get("case_id")
        if case_id is None:
            return Decision(False, f"'{action}' requires resource.case_id (fail-closed)", self.name)
        if subject.kind != "user":
            return Decision(False, f"case actions require a user subject, got {subject.kind}", self.name)
        if "superadmin" in subject.roles or subject.is_admin or "manager" in subject.roles:
            return Decision(True, "admin/manager bypass", self.name)

        from sqlalchemy import select
        from case_service.db.models import CaseInstanceModel
        row = (await session.execute(
            select(CaseInstanceModel.created_by, CaseInstanceModel.case_type_id)
            .where(CaseInstanceModel.id == case_id)
        )).first()
        if row is None:
            return Decision(False, "case not found (fail-closed)", self.name)
        created_by, case_type_id = row

        if created_by and str(created_by) == subject.id:
            return Decision(True, "owner (created_by)", self.name)

        from .tuples import has_tuple
        rel = await has_tuple(
            session, object_type="case", object_id=case_id,
            relations=relations, subject_type="user", subject_id=subject.id,
        )
        if rel:
            return Decision(True, f"relationship: {rel}", self.name)

        # Access-group parity (cutover safety, user-approved): a user whose
        # active access group allows this case TYPE keeps read/update access.
        # NEVER applies to case.share — type-scoped operators must not be
        # able to spread access to users outside their group (approved
        # design: share = owner / editor / admin only).
        if action != "case.share" and await self._access_group_allows(
            session, subject.id, case_type_id,
        ):
            return Decision(True, "access-group case-type scope", self.name)

        return Decision(
            False,
            f"no relationship ({sorted(relations)}), not owner, "
            "and no access-group scope for this case type",
            self.name,
        )

    async def _access_group_allows(
        self, session: AsyncSession, user_id: str, case_type_id,
    ) -> bool:
        from sqlalchemy import select
        from case_service.db.models import AccessGroupModel, OperatorAccessGroupModel
        rows = (await session.execute(
            select(AccessGroupModel.allowed_case_type_ids)
            .join(OperatorAccessGroupModel,
                  OperatorAccessGroupModel.access_group_id == AccessGroupModel.id)
            .where(OperatorAccessGroupModel.operator_id == user_id)
            .where(AccessGroupModel.is_active == True)  # noqa: E712
        )).scalars().all()
        for allowed in rows:
            if "*" in (allowed or []) or str(case_type_id) in (allowed or []):
                return True
        return False


_backend: AuthzBackend = RbacBackend()
_rebac_backend: AuthzBackend = RebacBackend()


# ── Decision cache (availability mitigation) ─────────────────────────

_cache: dict[tuple, tuple[Decision, float]] = {}
_cache_generation = 0   # bumped on invalidation — keys embed it
MAX_CACHE_ENTRIES = 5000   # per-resource keys (e.g. case ids) must not balloon memory

counters = {"allow": 0, "deny": 0, "error": 0, "cache_hit": 0,
            "deny_log_suppressed": 0}

# deny-audit throttle: first deny per (subject, action) is logged, repeats
# within the window are counted but not persisted — a hammered 403 route
# must not become a SecurityEvent / commit flood.
DENY_LOG_WINDOW_SECONDS = 60.0
_deny_logged: dict[tuple, float] = {}


def invalidate_cache() -> None:
    """Call on any role / access-group / namespace-grant mutation."""
    global _cache_generation
    _cache_generation += 1
    _cache.clear()


def _cache_key(subject: Subject, action: str, resource: dict | None) -> tuple:
    res_key = tuple(sorted((k, str(v)) for k, v in (resource or {}).items()))
    return (_cache_generation, subject, action, res_key)


# ── Entry points ─────────────────────────────────────────────────────


async def check(
    session: AsyncSession,
    subject: Subject,
    action: str,
    resource: dict[str, Any] | None = None,
) -> Decision:
    """The single authorization question. NEVER raises — returns a Decision;
    every internal failure is a deny."""
    key = _cache_key(subject, action, resource)
    hit = _cache.get(key)
    now = time.monotonic()
    if hit and hit[1] > now:
        counters["cache_hit"] += 1
        d = hit[0]
        return Decision(d.allow, d.reason, d.source, cached=True)

    backend = _rebac_backend if action in CASE_ACTIONS else _backend
    try:
        decision = await backend.evaluate(session, subject, action, resource)
    except Exception as exc:                      # fail-closed on ANY error
        counters["error"] += 1
        decision = Decision(False, f"authz evaluation error: {exc}", "error")
        await _maybe_log_deny(session, subject, action, resource, decision, "authz_error")
        return decision

    if len(_cache) >= MAX_CACHE_ENTRIES:
        # sweep expired first; if a hostile resource-id spread still fills
        # the cache, drop it — correctness never depends on the cache
        for k in [k for k, (_, exp) in _cache.items() if exp <= now]:
            _cache.pop(k, None)
        if len(_cache) >= MAX_CACHE_ENTRIES:
            _cache.clear()
    _cache[key] = (decision, now + CACHE_TTL_SECONDS)
    if decision.allow:
        counters["allow"] += 1
    else:
        counters["deny"] += 1
        await _maybe_log_deny(session, subject, action, resource, decision, "authz_denied")
    return decision


async def _maybe_log_deny(
    session: AsyncSession, subject: Subject, action: str,
    resource: dict | None, decision: Decision, event_type: str,
) -> None:
    """Throttled deny audit: first deny per (subject, action) in the window
    is persisted; repeats are counted, not written."""
    now = time.monotonic()
    tkey = (subject.kind, subject.id, action, event_type)
    last = _deny_logged.get(tkey, 0.0)
    if now - last < DENY_LOG_WINDOW_SECONDS:
        counters["deny_log_suppressed"] += 1
        return
    if len(_deny_logged) > 10000:    # same memory discipline as the cache
        _deny_logged.clear()
    _deny_logged[tkey] = now
    await _log_security_event(session, subject, action, resource, decision, event_type)


async def require(
    session: AsyncSession,
    subject: Subject,
    action: str,
    resource: dict[str, Any] | None = None,
) -> None:
    """check() that raises HTTPException 403 on deny — for router code."""
    decision = await check(session, subject, action, resource)
    if not decision.allow:
        raise HTTPException(403, f"Not authorized: {decision.reason}")


async def require_case(
    session: AsyncSession,
    user: AuthenticatedUser,
    action: str,
    case_id,
) -> None:
    """Case-level ReBAC check honoring HELIX_CASE_HXGUARD_CASE_ENFORCEMENT.

    off     → skipped entirely
    shadow  → evaluated; would-be denials audited (mode=shadow), request passes
    enforce → denials raise 403
    """
    from case_service.config import get_settings
    mode = (get_settings().hxguard_case_enforcement or "shadow").lower()
    if mode == "off":
        return
    subject = subject_from_user(user)
    decision = await check(session, subject, action, {"case_id": case_id})
    if decision.allow:
        return
    if mode == "enforce":
        # Anti-oracle (enforce cutover): a denied caller learns NOTHING —
        # 404 for every case action, so 403-vs-404 can't confirm that a
        # guessed case id exists. The reason stays in the authz_denied
        # SecurityEvent for operators, never in the response.
        raise HTTPException(404, "Case not found")
    counters["shadow_would_deny"] = counters.get("shadow_would_deny", 0) + 1
    log.info("hxguard shadow would-deny: %s %s case=%s (%s)",
             subject.id, action, case_id, decision.reason)


def subject_from_user(user: AuthenticatedUser) -> Subject:
    return Subject(
        kind="user", id=user.user_id,
        roles=tuple(user.roles or ()), is_admin=bool(user.is_admin),
    )


def guard(action: str):
    """FastAPI dependency: authenticate, authorize, return the user.

    Drop-in replacement for require_role() on routes that map to a single
    HxGuard action."""
    async def _dep(
        user: AuthenticatedUser = Depends(get_current_user),
        session: AsyncSession = Depends(get_session),
    ) -> AuthenticatedUser:
        await require(session, subject_from_user(user), action)
        return user
    return _dep


async def _log_security_event(
    session: AsyncSession, subject: Subject, action: str,
    resource: dict | None, decision: Decision, event_type: str,
) -> None:
    """Best-effort — a logging failure must never turn into an allow OR
    mask the deny.

    Uses its OWN session + commit: a deny raised as HTTP 403 rolls back the
    request transaction, and the audit record must survive that rollback
    (otherwise HTTP denials would never reach HxShield)."""
    try:
        from case_service.db.session import get_session_factory
        from case_service.enterprise.security_events import log_security_event
        async with get_session_factory()() as audit_session:
            await log_security_event(
                audit_session, event_type=event_type,
                severity="warning" if event_type == "authz_error" else "info",
                user_id=f"{subject.kind}:{subject.id}",
                action=action, outcome="denied",
                resource_type="authz",
                resource_id=str((resource or {}).get("namespace_id") or (resource or {}).get("id") or action),
                details={"reason": decision.reason, "source": decision.source,
                         "roles": list(subject.roles), "resource": {k: str(v) for k, v in (resource or {}).items()}},
            )
            await audit_session.commit()
    except Exception as exc:
        log.warning("hxguard: security event logging failed: %s", exc)
