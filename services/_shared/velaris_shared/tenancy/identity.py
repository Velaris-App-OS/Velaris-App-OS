"""Canonical tenant identity — one answer to "which tenant is this caller?".

The platform grew two conventions for the same idea and they do not compare:

  * **UUID columns** — ``case_instances.tenant_id``, ``case_types.tenant_id`` and
    friends are ``GUID`` foreign keys into ``tenants.id``.
  * **Slug strings** — ``case_sessions.tenant_id`` holds a slug, and every
    settings lookup matches ``TenantModel.slug``.

``user_directory.tenant_id`` is a bare ``String(64)``, so a caller's tenant
token can legitimately be *either* form depending on how the directory was
populated. Code that assumed one form silently misbehaved on a deployment
using the other: a direct ``str(case.tenant_id) != user.tenant_id`` compare
fails closed (every tenant-scoped case 404s for its own users), while a
``TenantModel.slug == user.tenant_id`` lookup finds nothing (per-tenant
settings read back as "not enabled").

Everything here accepts BOTH forms and resolves to the tenant row, so the
convention a deployment happens to use stops being load-bearing.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import TenantModel

#: The tenantless caller's token. Matches HxSentinel's DEFAULT_TENANT.
DEFAULT_TENANT = "default"

#: Tenants change about never; a short TTL keeps the resolver off the hot path
#: without holding a stale row long enough to matter. Negative results are
#: cached too, so an unknown token cannot be used to hammer the DB.
_CACHE_TTL_SECONDS = 60.0
_cache: dict[str, tuple[float, tuple[uuid.UUID, str] | None]] = {}


def _as_uuid(token: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(token)
    except (ValueError, AttributeError, TypeError):
        return None


def clear_cache() -> None:
    """Drop the resolver cache — for tests and for tenant mutations."""
    _cache.clear()
    _closed_cache.clear()


#: Tokens (id and slug) of ARCHIVED tenants — offboarded: nobody of theirs
#: signs in, their portal answers 404. Read for every request, so cached; a
#: change of status clears it everywhere (tenants generation), and the TTL
#: bounds it if that signal is missed.
_CLOSED_TTL_SECONDS = 30.0
_closed_cache: dict[str, tuple[float, frozenset[str]]] = {}


async def tenant_closed(session: AsyncSession, token: Any) -> bool:
    """Has this tenant been archived (offboarded)?

    Archiving set `status = "archived"` and nothing read it (found 2026-10-30,
    R7): an offboarded tenant's staff kept signing in and its portal kept
    taking submissions. The default tenant can never be archived (the route
    refuses), so a token with no tenant is never closed.
    """
    key = str(token).strip() if token not in (None, "") else ""
    if not key or key == DEFAULT_TENANT:
        return False
    hit = _closed_cache.get("all")
    if hit is None or hit[0] <= time.monotonic():
        rows = (await session.execute(
            select(TenantModel.id, TenantModel.slug).where(TenantModel.status == "archived")
        )).all()
        closed = frozenset({str(r[0]) for r in rows} | {str(r[1]) for r in rows})
        _closed_cache["all"] = (time.monotonic() + _CLOSED_TTL_SECONDS, closed)
        hit = _closed_cache["all"]
    return key in hit[1]


async def tenants_changed(session: AsyncSession, *, reason: str) -> None:
    """Call after a tenant is created or renamed, in its transaction.

    Until R5e step 5 nothing did: a new tenant's slug, looked up a moment
    before it existed, stayed cached as "no such tenant" for 60 s — in every
    process, including the one that created it.
    """
    from velaris_shared import generations

    await generations.bump(session, "tenants", reason=reason)
    _cache.clear()
    _closed_cache.clear()
    _actor_cache.clear()


async def _on_tenants_change(session: AsyncSession) -> None:
    _cache.clear()
    _closed_cache.clear()
    _actor_cache.clear()


async def resolve_tenant(
    session: AsyncSession, token: str | uuid.UUID | None,
) -> tuple[uuid.UUID, str] | None:
    """Resolve a tenant slug OR a tenant UUID to ``(id, slug)``.

    A UUID *object* is accepted as well as its string form: callers reach here
    from both worlds, and the GUID columns hand back real UUIDs. Coercing is
    the whole point of this module — refusing one of the two shapes would put
    the ``str(...)`` back at every call site.

    Returns None when the token names no tenant — callers must treat that as
    "deny", never as "any tenant".
    """
    key = str(token).strip() if token is not None else DEFAULT_TENANT
    if not key:
        return None

    hit = _cache.get(key)
    if hit is not None and hit[0] > time.monotonic():
        return hit[1]

    as_uuid = _as_uuid(key)
    stmt = select(TenantModel).where(
        TenantModel.id == as_uuid if as_uuid is not None else TenantModel.slug == key
    )
    row = (await session.execute(stmt.limit(1))).scalars().first()
    resolved = (row.id, row.slug) if row is not None else None
    # Only a FOUND tenant is cached (2026-11-10). Tenants are created by
    # user-service while every other service keeps its own cache, so a cached
    # "no such tenant" outlived the tenant's creation by up to a minute: the
    # live run's trial answered "'demo-…' is not a tenant on this platform" on
    # the case list right after it was made. An unknown name is looked up again
    # each time (one indexed query).
    if resolved is not None:
        _cache[key] = (time.monotonic() + _CACHE_TTL_SECONDS, resolved)
    else:
        _cache.pop(key, None)
    return resolved


async def tenant_slug(session: AsyncSession, token: str | uuid.UUID | None) -> str:
    """The slug for a caller's token, for slug-keyed rows and settings lookups.

    Falls back to the token itself so an unregistered tenant behaves exactly as
    it did before — matching nothing rather than matching everything.
    """
    resolved = await resolve_tenant(session, token)
    return resolved[1] if resolved else (str(token) if token is not None else DEFAULT_TENANT)


async def default_tenant_tokens(session: AsyncSession) -> list[str]:
    """Every spelling a string `tenant_id` column may use for the default tenant.

    For the callers with NO tenant claim. Such a caller is the platform-default
    tenant (see `is_superadmin`), and a string tenant column holds either the
    slug or the id — so both are returned. Scoped with `IS NULL` beside it, this
    is exactly what the single-case read lets the same caller see.
    """
    out = [DEFAULT_TENANT]
    resolved = await resolve_tenant(session, DEFAULT_TENANT)
    if resolved is not None:
        out.append(str(resolved[0]))
    return out


async def caller_tenant_tokens(session: AsyncSession, user: Any) -> list[str] | None:
    """Every spelling of the caller's tenant in a string `tenant_id` column —
    for `WHERE tenant_id IN (...)` on a list — or None for break-glass.

    A string column holds whatever its writer had: the token, the slug or the
    id. Matching one spelling would hide a tenant's own rows from it.
    """
    if is_superadmin(user):
        return None
    token = getattr(user, "tenant_id", None)
    if not token:
        return await default_tenant_tokens(session)
    out = {str(token)}
    resolved = await resolve_tenant(session, str(token))
    if resolved is not None:
        out |= {str(resolved[0]), str(resolved[1])}
    return sorted(out)


async def caller_tenant_uuid(session: AsyncSession, user: Any) -> uuid.UUID | None:
    """The caller's tenant id, for writing into a GUID `tenant_id` column.

    No claim is the default tenant, as everywhere. None only when even that
    does not resolve (an install with no `default` tenant row) — the row is
    then platform-shared, which is what it was before it had an owner.
    """
    resolved = await resolve_tenant(session, str(getattr(user, "tenant_id", None) or DEFAULT_TENANT))
    return resolved[0] if resolved else None


#: Tables whose tenant-less rows are genuinely PLATFORM-SHARED CONFIGURATION —
#: defaults every tenant is meant to read (2026-11-04). Everywhere else a row
#: with no tenant is a RECORD written before it had an owner (audit, logs,
#: incidents, jobs, tokens…), which the platform reads as the default tenant's;
#: until now such rows were listed to every tenant, which every website demo
#: tenant would have read. Add a table here only if a tenant must see the
#: operator's rows of it.
SHARED_CONFIG_TABLES = frozenset({
    "case_types", "portals", "access_groups", "access_roles", "security_rules",
    "sovereignty_rules", "retention_policies", "bot_tenant_limits",
    "sso_providers",   # a platform-wide IdP is every tenant's sign-in (no secret is listed)
})


async def scope_to_caller(stmt, model, session: AsyncSession, user: Any, *, via=None):
    """Narrow a list query to the caller's tenant — plus tenant-less rows where
    they are shared configuration (`SHARED_CONFIG_TABLES`), or the caller is the
    default tenant, whose legacy records they are.

    8d (2026-09-29): some forty list routes returned every tenant's rows. This
    is the one rule they now share: a row whose `tenant_id` is the caller's
    tenant (in any spelling) or NULL. A table with no tenant column names its
    owner through a parent: `via=(Model.fk_column, ParentModel)` keeps rows
    whose parent passes the same rule, or that have no parent. superadmin is
    unscoped.
    """
    from sqlalchemy import or_, select as _select

    tokens = await caller_tenant_tokens(session, user)
    if tokens is None:
        return stmt
    # A tenant-less row: shared configuration is everybody's; a record is the
    # default tenant's, so only the default tenant (the operator) reads it.
    is_default = await same_tenant_as_default(
        session, getattr(user, "tenant_id", None) or DEFAULT_TENANT)
    shared = getattr(model, "__tablename__", "") in SHARED_CONFIG_TABLES
    null_ok = shared or is_default
    if via is not None:
        fk_col, parent = via
        pk = next(iter(parent.__table__.primary_key.columns))
        allowed = await scope_to_caller(_select(getattr(parent, pk.name)), parent,
                                        session, user)
        if not null_ok:
            # A RECORD under a shared parent (an intake event on a default case
            # type) is the default tenant's, not every tenant's (2026-11-04):
            # its parent must be the caller's own, not a shared default.
            pcol = parent.tenant_id
            allowed = allowed.where(pcol.is_not(None))
        return stmt.where(or_(fk_col.in_(allowed), fk_col.is_(None)) if null_ok
                          else fk_col.in_(allowed))
    col = model.tenant_id
    if type(col.type).__name__ == "GUID":
        values = []
        for t in tokens:
            try:
                values.append(uuid.UUID(str(t)))
            except ValueError:
                continue
    else:
        values = list(tokens)
    return stmt.where(or_(col.in_(values), col.is_(None)) if null_ok else col.in_(values))


async def is_platform_operator(session: AsyncSession, user: Any) -> bool:
    """superadmin, or an account in the platform's own (default) tenant.

    For powers that act on the whole INSTALL rather than on a tenant — the
    platform update fleet above all. `admin` alone cannot mean that on a shared
    box: every tenant has admins, and until 2026-09-29 any of them could
    approve or roll back a platform update for everyone. A single-tenant
    install is unaffected: its admins are in the default tenant.
    """
    if is_superadmin(user):
        return True
    token = getattr(user, "tenant_id", None)
    if not token:
        return True
    return await same_tenant_as_default(session, token)


async def same_tenant_as_default(session: AsyncSession, token: Any) -> bool:
    resolved = await resolve_tenant(session, str(token))
    default = await resolve_tenant(session, DEFAULT_TENANT)
    if resolved is None:
        return False
    if default is None:
        return str(token) == DEFAULT_TENANT
    return str(resolved[0]) == str(default[0])


def is_superadmin(user: Any) -> bool:
    """Break-glass, and the only way across a tenant boundary.

    A token with **no** tenant is not a superuser: `same_tenant` already reads it
    as the platform-default tenant, and treating "unset" as "all tenants" is how
    a missing claim becomes an authorization bypass. Crossing has to be a role
    somebody was deliberately given.
    """
    roles = getattr(user, "roles", None) or []
    return "superadmin" in roles


async def may_reach_tenant(
    session: AsyncSession, user: Any, row_tenant_id: Any,
) -> bool:
    """May this caller see or act on a row belonging to that tenant?"""
    if is_superadmin(user):
        return True
    return await same_tenant(session, row_tenant_id, getattr(user, "tenant_id", None))


async def caller_tenant_or_refuse(
    session: AsyncSession, user: Any, requested: str | None,
) -> str:
    """The tenant a write may be attributed to.

    A caller-supplied ``tenant_id`` on a create or update body is a tenant-choice
    the caller does not have. Where the row it writes is later used to *resolve
    other rows* — as a schedule's tenant resolves the bots, connectors, cases and
    sync destinations its action may touch — accepting the field verbatim turns
    one string into a cross-tenant reach.

    So: a caller whose token names a tenant may only write into that tenant, and
    a token with no tenant at all (a platform-level operator) keeps the value it
    asked for. Raises ``PermissionError`` on a mismatch; callers map it to 403.
    """
    token = getattr(user, "tenant_id", None)
    asked = (requested or "").strip() or DEFAULT_TENANT
    if is_superadmin(user):
        # Break-glass writes into any tenant, deliberately: this is the role the
        # installer and platform operators hold, and it is auditable.
        return asked
    if not token:
        # No tenant claim is the platform-default tenant, not "any tenant".
        return DEFAULT_TENANT if asked == DEFAULT_TENANT else _refuse()

    mine = await resolve_tenant(session, token)
    theirs = await resolve_tenant(session, asked)
    if mine is not None and theirs is not None and mine[0] == theirs[0]:
        return asked
    if mine is not None and asked == DEFAULT_TENANT:
        # "default" is the shape a UI sends when it has no opinion; attribute it
        # to the caller's own tenant rather than to the platform's.
        return str(mine[1])
    return _refuse()


async def tenant_for_write(
    session: AsyncSession, user: Any, requested: str | None,
) -> str | None:
    """The tenant an ADMIN-surface create may record, from a body that names one.

    2026-09-30 sweep: SSO providers, SCIM tokens, channel endpoints, directory
    entries and access groups all stored the body's `tenant_id` as sent. A
    tenant admin could therefore register an identity provider — or mint a SCIM
    token — for ANOTHER tenant, and provision accounts into it.

    The platform operator (superadmin, or the default tenant — the same rule as
    `is_platform_operator`) keeps the value exactly as sent, NULL included, so
    nothing an install's operator does today changes. Everybody else writes
    into their own tenant, and naming another is a `PermissionError`.
    """
    if await is_platform_operator(session, user):
        return requested
    return await caller_tenant_or_refuse(session, user, requested)


def _refuse() -> str:
    raise PermissionError(
        "This may only be written into your own tenant.")


async def same_tenant(
    session: AsyncSession, row_tenant_id: Any, token: str | uuid.UUID | None,
) -> bool:
    """Does a GUID tenant column belong to this caller's tenant?

    A NULL tenant column stays visible to everyone — platform-shared rows
    predate multi-tenancy and several features depend on that. Anything else
    must resolve, and an unresolvable token is a denial.
    """
    if row_tenant_id is None:
        return True

    key = str(token).strip() if token is not None else DEFAULT_TENANT
    # Fast path: the deployment stores UUID strings on the user, so the
    # comparison is direct and needs no query at all.
    if str(row_tenant_id) == key:
        return True

    resolved = await resolve_tenant(session, key)
    # A string tenant column may hold the slug where the caller holds the id,
    # or the reverse; both spellings name the same tenant.
    return resolved is not None and str(row_tenant_id) in (str(resolved[0]), str(resolved[1]))


# ── case visibility ──────────────────────────────────────────────────────────
#
# Split out from `same_tenant` deliberately. `same_tenant` is used by ~20 call
# sites across features that genuinely depend on platform-shared NULL rows
# (connectors, escalation trees, arbiter policies), and changing its meaning
# would change all of them at once. Case visibility is the one that had a
# proven cross-tenant read, so it gets its own function and its own switch.

#: Operator switch for the NULL-tenant branch of case reads.
#:
#: **Unset means "exactly as before"**: a case with no tenant is visible to
#: everybody, which is how every release up to now has behaved and what several
#: single-tenant installs rely on.
#:
#: It may only be turned on once `migrations/*/197_case_tenant_backfill.sql` has
#: run AND reported zero cases left without a tenant. Turning it on while rows
#: are still NULL hides those rows from everyone, their rightful owner included
#: — which is an outage, not a fix.
_STRICT_ENV = "VELARIS_CASE_TENANT_STRICT_READS"


def strict_case_reads() -> bool:
    """Is the NULL-tenant branch closed on this install? Default: no."""
    import os
    raw = os.environ.get(_STRICT_ENV, "")
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


async def case_visible_to(
    session: AsyncSession, row_tenant_id: Any, token: str | uuid.UUID | None,
    *, is_superadmin_caller: bool = False,
) -> bool:
    """May this caller see a case carrying `row_tenant_id`?

    Two rules, and only the second one moves:

      * A case **owned by another tenant is never visible**, switch or no
        switch. This was not being enforced on the single-case read at all —
        `GET /cases/{id}` loaded by primary key with no tenant clause — which is
        how tenant `dev` read tenant `default`'s case on 2026-09-05.
      * A case with **no tenant** is visible to everyone by default, because
        that is what every released version does. Only `VELARIS_CASE_TENANT_
        STRICT_READS=true` closes it, and only a backfilled install may set it.

    Superadmin keeps its break-glass, same as `is_superadmin` elsewhere: it is
    the documented way across a tenant boundary and hiding rows from it would
    just push people to the database.
    """
    if is_superadmin_caller:
        return True
    if row_tenant_id is None:
        return not strict_case_reads()
    return await same_tenant(session, row_tenant_id, token)


# ── actor identity ───────────────────────────────────────────────────────────
#
# The same two-conventions problem as tenants, one column over.
#
# Fifteen columns record *who did this* as a `GUID`: `agents.created_by`,
# `agent_capabilities.granted_by`/`revoked_by`, `agent_token_grants.issued_by`/
# `revoked_by`, `agent_status_history.changed_by`, `agent_workforce.created_by`,
# `model_rate_cards.created_by`, `compliance_proofs.generated_by`,
# `simulation_gates.created_by`, `simulation_runs.created_by` and the four
# arbiter ones. But a caller's identifier is free text: `auth_real._make_tokens`
# mints `sub = str(user.id)`, a UUID, while an SSO login, a service account and
# every row in `user_directory` carry a **username or an email**.
#
# So the write worked for a password login and refused everybody else. Proven on
# 2026-09-06: `POST /agents` and `PUT /costing/model-rates` as `utpal.bhadra`
# both answered *"...is not a valid id"* — a 422 on a request that was correct in
# every respect, because a username had reached a UUID column. HxArbiter had
# already met this and degraded to NULL, which keeps the route working but
# throws the attribution away: the operator who authored a policy is recorded as
# nobody, on the one table an auditor reads to find out who.
#
# `actor_uuid` resolves both forms to `velaris_users.id`, exactly as
# `resolve_tenant` does for tenants, so neither the refusal nor the silent loss
# happens. It never invents an id: a caller that names nobody is recorded as
# absent, which is what those nullable columns already mean.

#: Actor lookups are cached on the same terms as tenants — a username maps to a
#: user row about as often as a tenant slug maps to a tenant. Negative results
#: are cached too, so an unknown principal cannot be used to hammer the DB.
_actor_cache: dict[str, tuple[float, uuid.UUID | None]] = {}


def clear_actor_cache() -> None:
    """Drop the actor resolver cache — for tests and for user mutations."""
    _actor_cache.clear()


# A tenant changed in another process: drop the lookups now (R5e step 5).
from velaris_shared import generations as _generations  # noqa: E402

_generations.on_change("tenants", _on_tenants_change)


async def actor_uuid(session: AsyncSession, user: Any) -> uuid.UUID | None:
    """The platform user id for a caller, for the `GUID` "who did this" columns.

    Accepts a UUID token, a username or an email, because all three legitimately
    arrive in `user.user_id` depending on how the caller signed in.

    Returns None when the caller names nobody this platform knows — a service
    principal, a token minted outside the user table. **None is a real answer**:
    every one of these columns is nullable and already means "not recorded".
    Coercing or inventing an id would put a fictional operator on an audit row,
    which is worse than an empty one.

    A UUID is returned as it stands, without a query and without checking that a
    user row exists. That is deliberate: it is exactly what every one of these
    call sites did before this function existed, so an install that is working
    today keeps storing precisely the value it stores today.
    """
    from velaris_shared.db.models import HelixUserModel

    raw = getattr(user, "user_id", None)
    direct = _as_uuid(str(raw).strip()) if raw is not None else None
    if direct is not None:
        return direct

    # Precedence, not `IN (...)` — the same reason `_enrich_with_access_group`
    # gives: merging the identifiers into one query hands one person's row to
    # another the moment a username equals somebody else's email.
    candidates = [
        str(v).strip() for v in (
            raw, getattr(user, "username", None), getattr(user, "email", None),
        ) if v is not None and str(v).strip()
    ]
    if not candidates:
        return None

    key = "\x00".join(candidates)
    hit = _actor_cache.get(key)
    if hit is not None and hit[0] > time.monotonic():
        return hit[1]

    found: uuid.UUID | None = None
    for ident in candidates:
        row = (await session.execute(
            select(HelixUserModel).where(
                HelixUserModel.username == ident
            ).limit(1)
        )).scalars().first()
        if row is None:
            row = (await session.execute(
                select(HelixUserModel).where(
                    HelixUserModel.email == ident
                ).limit(1)
            )).scalars().first()
        if row is not None:
            found = row.id
            break

    _actor_cache[key] = (time.monotonic() + _CACHE_TTL_SECONDS, found)
    return found
