"""FastAPI auth dependencies.

Provides get_current_user and require_role dependencies for routes.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import hashlib
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Callable

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.auth.models import AuthenticatedUser, ActiveAccessGroup
from velaris_shared.auth.jwt_handler import decode_jwt_token, extract_user_from_claims
from velaris_shared.config import get_settings

logger = logging.getLogger(__name__)

# ── Revoked-session in-memory cache (30s TTL) ─────────────────────────────────
# Avoids a DB round-trip on every request in the common (non-revoked) case.
_REVOKED_HASHES: set[str] = set()
_REVOKED_CACHE_AT: float  = 0.0
_REVOKED_CACHE_TTL: float = 30.0


async def _refresh_revoked_cache(session: AsyncSession) -> None:
    global _REVOKED_CACHE_AT
    from velaris_shared.db.models import RevokedSessionModel
    now_utc = datetime.now(timezone.utc)
    rows = (await session.execute(
        select(RevokedSessionModel.token_hash).where(
            RevokedSessionModel.expires_at > now_utc
        )
    )).scalars().all()
    _REVOKED_HASHES.clear()
    _REVOKED_HASHES.update(rows)
    _REVOKED_CACHE_AT = time.monotonic()


def _is_token_revoked_cached(raw_token: str) -> bool:
    return hashlib.sha256(raw_token.encode()).hexdigest() in _REVOKED_HASHES


def note_revoked(token_hash: str) -> None:
    """A session revoked in THIS process is refused here at once.

    Until R5e step 5 a logout waited for the next 30 s reload even in the
    process that recorded it. Other processes reload when the `revocation`
    generation moves (velaris_shared/generations.py) — the revoking code bumps
    it in the same transaction as the revocation row.
    """
    _REVOKED_HASHES.add(token_hash)


# Another process revoked a session: reload the set now, not at the 30 s mark.
from velaris_shared import generations as _generations  # noqa: E402

_generations.on_change("revocation", _refresh_revoked_cache)

_bearer_scheme = HTTPBearer(auto_error=False)


async def _tenant_open_check(user: AuthenticatedUser, session: AsyncSession) -> None:
    """An archived (offboarded) tenant's people are refused everywhere (R7,
    2026-10-30) — archiving was a label nothing read. After enrichment, so the
    tenant is the directory's. superadmin passes (break-glass, and the one who
    can restore the tenant)."""
    from velaris_shared.tenancy.identity import is_superadmin, tenant_closed
    if not is_superadmin(user) and await tenant_closed(session, getattr(user, "tenant_id", None)):
        raise HTTPException(status_code=403,
                            detail="This organisation's access to Velaris has been closed.")


async def _enrich_with_access_group(
    user: AuthenticatedUser,
    session: AsyncSession,
) -> AuthenticatedUser:
    """Load the operator's active access group from the DB and enrich the user.

    Falls back gracefully — if no access groups are assigned, the user object is
    returned unchanged. Since Phase 4 step 1 it is returned unchanged in one
    further respect: `user.roles` is never written here, so an access group can
    add context but cannot add platform authority.
    """
    from velaris_shared.db.models import (
        UserDirectoryModel, AccessGroupModel, AccessRoleModel,
        OperatorAccessGroupModel, PortalModel,
    )

    # ── Identity resolution (fixed 2026-08-15) ────────────────────
    #
    # These two tables key on a free-text operator id, and what lands in it
    # depends on who wrote the row: the SSO/LDAP sync and the admin CRUD screens
    # write a **username or email**, while `auth_real._make_tokens` mints tokens
    # with `sub = str(user.id)`, a **UUID**. So matching only on `user.user_id`
    # found nothing for a normally-logged-in operator: no access group ever
    # loaded, and `tenant_id` was never populated from the directory. P37 was
    # inert for every staff login, silently, since it shipped.
    #
    # Resolution is by PRECEDENCE, not by `IN (...)`. Merging the three
    # identifiers into one query would conflate two accounts the moment one
    # user's username equals another's email — rare, but the failure mode is
    # handing someone else's access group to the wrong person, so it is not a
    # risk worth taking for one fewer round trip.
    identifiers = [i for i in (user.user_id, user.username, user.email) if i]

    dir_row = None
    for ident in identifiers:
        dir_row = (await session.execute(
            select(UserDirectoryModel).where(UserDirectoryModel.user_id == ident)
        )).scalar_one_or_none()
        if dir_row is not None:
            break

    oag_rows = []
    for ident in identifiers:
        oag_rows = (await session.execute(
            select(OperatorAccessGroupModel).where(
                OperatorAccessGroupModel.operator_id == ident
            )
        )).scalars().all()
        if oag_rows:
            break

    # Always populate tenant_id from user_directory regardless of access groups
    if dir_row and dir_row.tenant_id:
        user.tenant_id = dir_row.tenant_id

    # A group owned by another tenant is never applied (2026-11-09): a person
    # carries their OWN tenant's groups only. Written memberships are checked
    # at every write path; this catches any made before those checks.
    if oag_rows and "superadmin" not in (user.roles or []):
        from velaris_shared.tenancy.identity import DEFAULT_TENANT, same_tenant
        owners = dict((await session.execute(
            select(AccessGroupModel.id, AccessGroupModel.tenant_id).where(
                AccessGroupModel.id.in_([r.access_group_id for r in oag_rows]))
        )).all())
        home = getattr(user, "tenant_id", None) or DEFAULT_TENANT
        oag_rows = [r for r in oag_rows
                    if owners.get(r.access_group_id) is None
                    or await same_tenant(session, owners[r.access_group_id], home)]

    if not oag_rows:
        return user  # no P37 groups assigned yet — legacy flat roles still work

    # Determine active group: persisted current > primary > first
    active_group_id: uuid.UUID | None = None
    if dir_row and dir_row.current_access_group_id:
        # Verify it's still a valid membership
        ids = {str(r.access_group_id) for r in oag_rows}
        if str(dir_row.current_access_group_id) in ids:
            active_group_id = dir_row.current_access_group_id

    if active_group_id is None:
        primary = next((r for r in oag_rows if r.is_primary), None) or oag_rows[0]
        active_group_id = primary.access_group_id

    # Load the active group + its portal
    ag = (await session.execute(
        select(AccessGroupModel).where(AccessGroupModel.id == active_group_id)
    )).scalar_one_or_none()

    if ag is None:
        return user

    portal = (await session.execute(
        select(PortalModel).where(PortalModel.id == ag.portal_id)
    )).scalar_one_or_none()

    # Resolve role names from role_ids
    role_names: list[str] = []
    all_privileges: list[dict] = []
    if ag.role_ids:
        role_uuids = [uuid.UUID(r) if isinstance(r, str) else r for r in ag.role_ids]
        role_rows = (await session.execute(
            select(AccessRoleModel).where(AccessRoleModel.id.in_(role_uuids))
        )).scalars().all()
        for rr in role_rows:
            role_names.append(rr.name)
            all_privileges.extend(rr.privileges or [])

    # Build available_access_groups summary
    all_ag_ids = [r.access_group_id for r in oag_rows]
    all_ags = (await session.execute(
        select(AccessGroupModel).where(AccessGroupModel.id.in_(all_ag_ids))
    )).scalars().all()
    available = [
        {
            "id": str(a.id),
            "name": a.name,
            "is_primary": any(r.is_primary and r.access_group_id == a.id for r in oag_rows),
        }
        for a in all_ags
    ]

    active_ag = ActiveAccessGroup(
        id=str(ag.id),
        name=ag.name,
        portal_id=str(portal.id) if portal else "",
        portal_type=portal.portal_type if portal else "staff",
        portal_name=portal.name if portal else "",
        modules=portal.modules if portal else [],
        homepage=portal.homepage if portal else "/work-center",
        roles=role_names,
        privileges=all_privileges,
        allowed_case_type_ids=ag.allowed_case_type_ids or ["*"],
        allowed_queue_ids=ag.allowed_queue_ids or ["*"],
    )

    # Phase 4 step 1 (2026-08-15): access-role NAMES are no longer merged into
    # `user.roles`. They were, from P37 until now, so that `require_role("admin")`
    # kept working for operators whose access role happened to be named `admin` —
    # which also meant *naming* an access role `admin` conferred platform
    # administration. `require_role` has had zero call sites since Phase 3, so the
    # bridge has nothing left to carry.
    #
    # The names are still published on `active_access_group.roles` (UI, and the
    # case-level vocabulary in `core/access_control.py`); what they no longer do
    # is answer a platform authorization question. An access group reaches a
    # platform action only through a privilege entry naming the dotted namespace
    # — see `hxguard/privileges.py::_crosses_into_platform_namespace`, which is
    # load-bearing from this commit onward rather than merely defensive.
    user.active_access_group = active_ag
    user.available_access_groups = available
    return user


# R6 soak 2026-11-02 (S1): every request carrying an expired token wrote a
# WARNING — ~110 lines/s, +260 MB/h of journal, when 400 clients' tokens had
# expired. An expired token is the normal end of a session (the client
# refreshes), so it is DEBUG. Any other failure (bad signature, wrong issuer,
# garbage) stays a WARNING, but at most one line per reason per minute, with
# how many were folded into it — a flood of forged tokens is still seen, once.
_TOKEN_LOG_WINDOW = 60.0
_token_log: dict[str, list] = {}   # reason -> [window start, folded count]


def _log_token_failure(e: Exception) -> None:
    import time

    try:
        import jwt as _pyjwt
        if isinstance(e, _pyjwt.ExpiredSignatureError):
            logger.debug("JWT validation failed: %s", e)
            return
    except ImportError:
        pass
    reason = type(e).__name__
    now = time.monotonic()
    slot = _token_log.get(reason)
    if slot is None or now - slot[0] >= _TOKEN_LOG_WINDOW:
        folded = slot[1] if slot else 0
        _token_log[reason] = [now, 0]
        if folded:
            logger.warning("JWT validation failed: %s (+%d more %s in the last minute)",
                           e, folded, reason)
        else:
            logger.warning("JWT validation failed: %s", e)
    else:
        slot[1] += 1


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> AuthenticatedUser:
    """Extract and validate the current user from the request.

    In dev mode, returns a mock admin user.
    In jwt/oidc mode, validates the Bearer token.

    P37: enriches the user with active access group context when available.
    Falls back gracefully so all pre-P37 routes are unaffected.
    """
    settings = get_settings()

    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        claims = decode_jwt_token(
            credentials.credentials,
            secret=settings.auth_secret,
            issuer=settings.auth_issuer,
            audience=settings.auth_audience,
            public_key=settings.auth_rsa_public_key or "",
        )
        user = AuthenticatedUser(
            token=credentials.credentials,
            **extract_user_from_claims(claims),
        )
    except Exception as e:
        _log_token_failure(e)
        raise HTTPException(
            status_code=401,
            detail=f"Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Purpose-built tokens are single-purpose: each is accepted only by the one
    # dependency written for it — MCP scoped tokens by the MCP transport
    # (`token_use="mcp"`, P4), agent tokens by the agent caller dependency
    # (`token_use="agent"`, F-00). Everywhere else — REST, proposals
    # confirm/reject, token minting — they fail closed, so a leaked non-human
    # token cannot touch any other surface.
    #
    # The check is "any token_use at all", not a list of known values. A session
    # token has never carried this claim, so nothing existing is affected, and
    # the next purpose-built token kind is refused here by default instead of
    # being admitted until somebody remembers to add it. Fail closed on the
    # unknown case is the whole point.
    token_use = claims.get("token_use")
    if token_use:
        raise HTTPException(
            status_code=401,
            detail=f"A {token_use} token is not valid on this endpoint",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # P37 enrichment + security checks (is_active + revoked session)
    try:
        from velaris_shared.db.session import get_session_factory
        from velaris_shared.db.models import HelixUserModel

        from velaris_shared.db.commit_route import request_session

        # The request's own session when it has one — a second one from the
        # same pool deadlocks under load (see request_session).
        # The security check first, and outside the "skipped" path below: an
        # unexpected error while looking up access groups must never let a
        # deleted, renamed or suspended account through (11b audit, 2026-10-23).
        session: AsyncSession | None = request_session(request)
        if session is None:
            async with get_session_factory()() as session:
                await _security_check(user, credentials.credentials, session)
                user = await _enrich_with_access_group(user, session)
                await _tenant_open_check(user, session)
        else:
            await _security_check(user, credentials.credentials, session)
            user = await _enrich_with_access_group(user, session)
            await _tenant_open_check(user, session)
    except HTTPException:
        raise
    except Exception as exc:
        # The database could not be reached (pool wait ran out, connection
        # refused or dropped). Until 2026-10-19 this was skipped too, so under
        # overload a revoked token or deactivated account was accepted and the
        # caller's access group was lost. Refuse instead: nothing reaches a
        # route unchecked, and the request never ran, so the client may retry.
        raise_if_db_unavailable(exc)
        # Anything else is not an outage — the behaviour before 2026-10-19,
        # now logged where an operator can see it.
        logger.warning("P37 enrichment / security check skipped", exc_info=True)

    # R5d 11b: who this request is, for "read your own writes"
    # (velaris_shared/middleware/read_routing.py).
    request.state.velaris_user_id = user.user_id
    # 2026-11-04: the request's TRUSTED tenant — from the signed-in account's
    # directory row, never a header. Logs, metrics, traces and AI-call records
    # are stamped with it, so each tenant's admin can be shown its own slice.
    # No tenant is the default tenant's, as everywhere.
    trusted = str(getattr(user, "tenant_id", None) or "default")
    request.state.velaris_tenant = trusted
    from velaris_shared.observability.context import tenant_id_var
    tenant_id_var.set(trusted)
    return user


#: Retry-After on that 503 — about how long an overloaded pool takes to drain.
AUTH_CHECK_RETRY_AFTER_SECONDS = 5


def raise_if_db_unavailable(exc: BaseException) -> None:
    """Refuse with VEL-AUTH-CHECK-BUSY-503 when `exc` means the database was
    unreachable during a sign-in check. Returns for any other exception.

    Every sign-in check uses this (session tokens here, scoped MCP tokens), so
    none of them lets a request through unchecked during an outage.
    """
    if not _db_unavailable(exc):
        return
    logger.warning("sign-in check: database unavailable (%s) — 503", type(exc).__name__)
    from velaris_shared.core.error_codes import AUTH_CHECK_BUSY_503, coded

    raise coded(
        503, AUTH_CHECK_BUSY_503,
        "The server is busy. Please try again in a moment.",
        headers={"Retry-After": str(AUTH_CHECK_RETRY_AFTER_SECONDS)},
    ) from exc


def is_auth_check_busy(exc: BaseException) -> bool:
    """True for the 503 above — callers that turn a refused sign-in check into
    "no caller" must re-raise this one instead: skipping a check because the
    database was briefly away is exactly what the 503 exists to prevent."""
    from velaris_shared.core.error_codes import AUTH_CHECK_BUSY_503

    return getattr(exc, "code", None) == AUTH_CHECK_BUSY_503


# ── WebSockets ────────────────────────────────────────────────────────────────
#
# Until 2026-10-19 the sockets checked only the token's signature: a revoked
# token or a suspended account kept a live socket, and an agent / MCP token —
# refused by every HTTP route — was accepted. They now run the same check as
# get_current_user, when the socket opens and again every
# WS_RECHECK_SECONDS while it stays open.

#: Close codes. 4001 = sign-in refused (the client reconnects with a fresh
#: token); 1013 = "try again later", the WebSocket form of the busy 503.
WS_CLOSE_UNAUTHORIZED = 4001
WS_CLOSE_TRY_AGAIN = 1013
#: How often an open socket's sign-in is checked again. A revocation or a
#: suspension takes effect within this long.
WS_RECHECK_SECONDS = 60.0


async def authenticate_websocket(websocket, token: str | None) -> AuthenticatedUser:
    """The full sign-in check for a socket's `?token=` — or HTTPException
    (401/403 refused, VEL-AUTH-CHECK-BUSY-503 busy)."""
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token) if token else None
    return await get_current_user(websocket, creds)


def websocket_close_code(exc: HTTPException) -> int:
    """The close code for a refused socket sign-in."""
    return WS_CLOSE_TRY_AGAIN if is_auth_check_busy(exc) else WS_CLOSE_UNAUTHORIZED


async def watch_websocket_auth(websocket, token: str, interval: float | None = None) -> None:
    """Re-check an open socket's sign-in every `interval` seconds; close it
    when the check refuses. Run as a task beside the socket's own loop.

    A busy database is tolerated once (the socket stays open, as the HTTP side
    would retry); a second busy answer in a row closes with 1013 so the client
    reconnects later. Ends when the socket closes or the task is cancelled.
    """
    import asyncio

    busy_in_a_row = 0
    while True:
        await asyncio.sleep(interval if interval is not None else WS_RECHECK_SECONDS)
        try:
            await authenticate_websocket(websocket, token)
            busy_in_a_row = 0
        except HTTPException as exc:
            if is_auth_check_busy(exc):
                busy_in_a_row += 1
                if busy_in_a_row < 2:
                    continue
            reason = "Server busy" if is_auth_check_busy(exc) else "Session ended"
            try:
                await websocket.close(code=websocket_close_code(exc), reason=reason)
            except Exception:                                   # noqa: BLE001
                pass      # already closed
            return
        except Exception:                                       # noqa: BLE001
            logger.warning("websocket sign-in re-check failed", exc_info=True)


def _db_unavailable(exc: BaseException) -> bool:
    """The database could not be reached, as opposed to a query that failed."""
    from sqlalchemy import exc as sa_exc

    if isinstance(exc, (sa_exc.TimeoutError, sa_exc.DisconnectionError,
                        sa_exc.InterfaceError, TimeoutError, ConnectionError, OSError)):
        return True
    if isinstance(exc, sa_exc.DBAPIError) and exc.connection_invalidated:
        return True
    # PostgreSQL reports "too many clients", "shutting down" and refused
    # connections as OperationalError. SQLite also uses it for a missing table,
    # which is not an outage.
    return isinstance(exc, sa_exc.OperationalError) and "no such table" not in str(exc)


async def _security_check(
    user: AuthenticatedUser,
    raw_token: str,
    session: AsyncSession,
) -> None:
    """Verify the token has not been revoked and the account is still active."""
    # Refresh revoked-token cache if stale
    if time.monotonic() - _REVOKED_CACHE_AT > _REVOKED_CACHE_TTL:
        try:
            await _refresh_revoked_cache(session)
        except Exception as exc:
            # A database that cannot be reached refuses (503): the cache may be
            # missing revocations made since it was last read. Any other failure
            # keeps the cache, as before.
            raise_if_db_unavailable(exc)

    if _is_token_revoked_cached(raw_token):
        raise HTTPException(401, "Session has been revoked",
                            headers={"WWW-Authenticate": "Bearer"})

    await _account_gate(user, session)


async def _account_gate(user: AuthenticatedUser, session: AsyncSession) -> None:
    """The account behind the token must still exist, carry the token's username
    and be active. Until 2026-10-23 only an EXISTING inactive row was refused: a
    deleted user's token kept working until it expired, and — access groups being
    looked up by the token's username — once a new user was given that name, the
    old token resolved to the NEW account's access. A rename ends the old sessions
    for the same reason. A `sub` that is not an account id was never issued by
    Velaris (session tokens carry `str(user.id)`): refused. Read on the main
    database — sign-in never uses a read replica."""
    from velaris_shared.db.models import HelixUserModel

    try:
        account_id = uuid.UUID(str(user.user_id))
    except ValueError:
        raise HTTPException(401, "Invalid session.", headers={"WWW-Authenticate": "Bearer"}) from None
    try:
        hu = await session.get(HelixUserModel, account_id)
    except Exception as exc:
        # Unreachable database: refuse (503). Anything else: refuse too — an
        # account that cannot be looked up is not let through.
        raise_if_db_unavailable(exc)
        logger.error("account check failed — refusing the request", exc_info=True)
        raise HTTPException(401, "Session could not be verified.",
                            headers={"WWW-Authenticate": "Bearer"}) from exc
    # Deleted (mig 223, kept for its grace period) is gone as far as sign-in goes;
    # so is an account the deletion ledger names (a restore may have brought the
    # row back before the replay purges it again).
    from velaris_shared.identity.ledger import purged_set
    if hu is None or getattr(hu, "deleted_at", None) is not None or str(hu.id) in purged_set():
        raise HTTPException(401, "This account no longer exists.",
                            headers={"WWW-Authenticate": "Bearer"})
    if (user.username or "") != (hu.username or ""):
        raise HTTPException(401, "This session belongs to a renamed account — sign in again.",
                            headers={"WWW-Authenticate": "Bearer"})
    if not hu.is_active:
        raise HTTPException(401, "Account is suspended. Contact your administrator.",
                            headers={"WWW-Authenticate": "Bearer"})
    cutoff = getattr(hu, "tokens_valid_after", None)
    if cutoff is not None:
        iat, precise = _issued_at(getattr(user, "token", "") or "")
        cut = cutoff.replace(tzinfo=cutoff.tzinfo or timezone.utc).timestamp()
        # Our tokens carry `iat_ms`: issued after the cutoff to the millisecond
        # (a sign-in right after an undo works at once). A token with whole
        # seconds only: the cutoff rounds UP, so one from the delete's own
        # second cannot slip through (a sign-in in that second is repeated).
        import math
        if iat is None or (iat <= cut if precise else iat < math.ceil(cut)):
            raise HTTPException(401, "This session was ended — sign in again.",
                                headers={"WWW-Authenticate": "Bearer"})


def _issued_at(token: str) -> tuple[float | None, bool]:
    """When the token was issued, and whether to the millisecond (`iat_ms`) or
    whole seconds (`iat`). Its signature was verified by the caller."""
    import base64
    import json as _json
    try:
        seg = token.split(".")[1]
        claims = _json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))
        if isinstance(claims.get("iat_ms"), int) and not isinstance(claims["iat_ms"], bool):
            return claims["iat_ms"] / 1000, True
        return float(claims["iat"]), False
    except Exception:  # noqa: BLE001
        return None, False


def require_role(*roles: str) -> Callable:
    """**RETIRED (Phase 4 step 2). Raises at import-of-use, deliberately.**

    This was the platform's authorization gate: it passed for `superadmin`, for
    anyone whose `is_admin` was true, or for any of the named roles — where
    `has_role()` itself returned True for every role once admin was present.
    Phase 3 replaced all 110 of its call sites with `hxguard.guard(action)`, and
    Phase 3.5 did the same for the 56 that hid behind the `require_admin()`
    alias.

    It is kept as a loud failure rather than deleted outright because the
    *symbol* is still imported (unused) by 25 routers, and a NameError at import
    time in an unrelated deploy is a worse first signal than this message. A
    reintroduced gate now fails when the route is built, not silently at request
    time with role-name semantics nobody expects any more.
    """
    raise RuntimeError(
        "require_role() was retired in Phase 4 of privilege-based authorization. "
        f"Use hxguard.guard('<action>') instead of require_role({roles!r}) — see "
        "case_service/hxguard/actions.py for the registry, and "
        "docs/Future/Done/privilege-based-authorization.md for why."
    )


async def _shadow_compare(request, user, roles, allowed: bool) -> None:
    """Phase-2 observation. Never raises, never alters the decision.

    Nothing calls this now that `require_role` is retired. It stays, with
    `hxguard/shadow.py`, because it is the harness a *future* migration reuses:
    run the new decider alongside the old one and count disagreements before
    changing any answer. Deleting it would mean rebuilding it.
    """
    try:
        from velaris_shared.config import get_settings
        if not getattr(get_settings(), "hxguard_privilege_shadow", True):
            return
        from velaris_shared.hxguard import shadow
        await shadow.compare(request, user, roles, allowed)
    except Exception:                       # observation must never break auth
        pass


def require_admin() -> Callable:
    """**RETIRED (Phase 4 step 2)** — the one-line alias that hid 56 gates from
    the Phase-0 inventory. See `require_role`."""
    return require_role("admin")
