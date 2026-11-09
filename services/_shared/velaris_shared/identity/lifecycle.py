"""Deleting a user: refused and signed out at once, recoverable for a grace
period, then purged — everything that lets them in goes; their history stays.

Agreed with the user 2026-10-23 (Nov_TODO "User deletion v2"). Until then
`DELETE /auth/real/users/{id}` removed only the `velaris_users` row. Their tokens
kept working (the sign-in check refused only an existing inactive row) and
their access-group memberships stayed behind — keyed by *username*: a new user
given the same name inherited them, and the deleted user's old token resolved
to the NEW account's access.

* `mark_deleted` — the delete an admin asks for. The row STAYS, so the username
  stays reserved and no access row can be inherited; `deleted_at` makes every
  sign-in path refuse it (velaris_shared.auth.dependencies._account_gate and the
  login, SSO, SCIM and reset paths). Every session ends now: credentials are
  destroyed (refresh tokens, devices, OTP codes, passkeys and challenges, push
  tokens, MCP grants, idempotency keys, pending MCP proposals) and agent tokens
  they issued are revoked. Open case assignments go back to their queue. The
  directory entry is hidden from pickers (its state kept for an undo).
* `undo_delete` — within the grace period: the mark goes, the directory entry
  comes back. The user signs in again; old sessions and devices stay dead.
* `purge_user` — after the grace period (`purge_due`, the worker's job) or at
  once by choice: everything that grants access — access-group and tenant
  memberships, ReBAC tuples naming the user, delegations to them (cleared), their
  absences, calendar/notification/report subscriptions, work patterns, the
  directory entry (manager links cleared), personal HxNexus conversations — and
  then the row.

**Kept** (audit, legal hold, the audit chain): case history, audit logs,
security events, `created_by`-style columns — by id; they read as a deleted
user. Tables store a user by account id, username or email depending on age,
so every match uses all three.

Replicas receive each step through replication. Sign-in reads only the main
database, never a replica.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import Table, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db import models as M

log = logging.getLogger(__name__)

# Credentials and sessions — destroyed when the account is MARKED deleted (an
# undo does not bring them back: the user signs in again).
CREDENTIALS = (
    ("refresh_tokens", "user_id"),
    ("auth_devices", "user_id"),
    ("auth_otp", "user_id"),
    ("webauthn_credentials", "user_id"),
    ("webauthn_challenges", "user_id"),
    ("push_device_tokens", "user_id"),
    ("mcp_token_grants", "user_id"),
    ("mcp_idempotency_keys", "user_id"),
)
# Access — removed at the final PURGE (until then the reserved username keeps
# them from being inherited, and an undo needs them).
ACCESS = (
    ("operator_access_groups", "operator_id"),
    ("tenant_memberships", "user_id"),
    ("calendar_subscriptions", "user_id"),
    ("notification_preferences", "user_id"),
    ("report_subscriptions", "created_by"),
    ("user_work_patterns", "user_id"),
    ("user_absences", "user_id"),
    ("user_directory", "user_id"),
)
# (table, column) — the link is cleared at the purge; the row belongs to someone else.
CLEAR = (
    ("user_absences", "delegate_user_id"),
    ("user_directory", "manager_user_id"),
)


class DeleteRefused(Exception):
    """A delete that must not happen; the message says why."""


def _metadata():
    for v in vars(M).values():
        if hasattr(v, "metadata") and hasattr(v, "__tablename__"):
            return v.metadata
    raise RuntimeError("no model metadata")


def _t(name: str) -> Table:
    return _metadata().tables[name]


def names_of(user: Any) -> list[str]:
    """Every spelling a table may use for this user."""
    return [v for v in {str(user.id), user.username or "", (user.email or "").lower(), user.email or ""} if v]


def _match(column, user: Any) -> list:
    """The values to match `column` against: a UUID column takes only the
    account id (a username there is not a UUID — PostgreSQL refuses the whole
    statement, SQLite does not: found live 2026-10-23); a text column every
    spelling."""
    kind = type(column.type).__name__.upper()
    if "GUID" in kind or "UUID" in kind:
        return [user.id]
    return names_of(user)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _delete_rows(session: AsyncSession, pairs, user: Any, counts: dict) -> None:
    for table, column in pairs:
        tb = _t(table)
        n = (await session.execute(delete(tb).where(tb.c[column].in_(_match(tb.c[column], user))))).rowcount or 0
        counts[table] = counts.get(table, 0) + n


def _uuid_or_none(value: Any):
    import uuid as _uuid
    try:
        return _uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


async def _end_sessions(session: AsyncSession, user: Any, actor: str, counts: dict,
                        actor_id: str | None = None) -> None:
    await _delete_rows(session, CREDENTIALS, user, counts)
    props = _t("mcp_action_proposals")
    counts["mcp_action_proposals"] = (await session.execute(
        delete(props).where(props.c.user_id.in_(_match(props.c.user_id, user)),
                            props.c.status == "pending"))).rowcount or 0
    ag = _t("agent_token_grants")
    counts["agent_token_grants_revoked"] = (await session.execute(
        update(ag).where(ag.c.issued_by.in_(_match(ag.c.issued_by, user)), ag.c.revoked.is_(False))
        # revoked_by is the account id (a UUID column); the job has none.
        .values(revoked=True, revoked_at=_now(), revoked_by=_uuid_or_none(actor_id)))).rowcount or 0


async def _release_assignments(session: AsyncSession, user: Any, actor: str, counts: dict) -> None:
    """Open work back to its queue, each step audited on its case."""
    asg = _t("case_assignments")
    rows = (await session.execute(
        select(asg.c.id, asg.c.case_id).where(asg.c.assignee_type == "user",
                                               asg.c.assignee_id.in_(_match(asg.c.assignee_id, user)),
                                               asg.c.status == "active"))).all()
    if rows:
        await session.execute(update(asg).where(asg.c.id.in_([r.id for r in rows]))
                              .values(assignee_type="queue", claimed_at=None))
        from velaris_shared.db import repository as repo
        for r in rows:
            await repo.append_audit_entry(session, data={
                "case_id": r.case_id, "action": "assignment_released", "actor_id": actor,
                "details": {"assignment_id": str(r.id), "reason": "assignee deleted"}})
    counts["case_assignments_released"] = len(rows)


async def _work_left_behind(session: AsyncSession, user: Any, actor: str, counts: dict) -> dict:
    """What the user set in motion (Nov_TODO "User deletion v2", 7–10). Requests
    they raised that still wait are withdrawn (nobody approves a deleted
    person's request later); jobs they queued are cancelled (their output would
    be delivered to nobody); report deliveries they set up are paused (an undo
    turns them back on). Schedules and bots they authored run under their own
    approved identity, not as the user — they keep running and are COUNTED so
    the administrator can review them. Returns what an undo puts back."""
    now = _now()
    note = "requester deleted"

    async def _upd(table, where_col, extra_where, values) -> int:
        tb = _t(table)
        q = update(tb).where(tb.c[where_col].in_(_match(tb.c[where_col], user)), *extra_where(tb))
        return (await session.execute(q.values(**values))).rowcount or 0

    counts["legal_hold_requests_withdrawn"] = await _upd(
        "legal_hold_release_requests", "requested_by", lambda t: [t.c.status == "pending"],
        {"status": "withdrawn", "pending_key": None, "decided_at": now, "decision_note": note})
    counts["marketplace_requests_cancelled"] = await _upd(
        "marketplace_release_requests", "requested_by", lambda t: [t.c.status == "pending"],
        {"status": "cancelled"})
    counts["tenant_transfers_cancelled"] = await _upd(
        "case_tenant_transfers", "requested_by", lambda t: [t.c.state.in_(("requested", "pending"))],
        {"state": "cancelled", "active_key": None, "cancelled_by": actor[:255], "decided_at": now})
    counts["bot_jobs_cancelled"] = await _upd(
        "bot_jobs", "requested_by", lambda t: [t.c.status == "queued"],
        {"status": "cancelled", "error": note, "ended_at": now})
    counts["capture_runs_cancelled"] = await _upd(
        "capture_runs", "requested_by", lambda t: [t.c.status == "pending"],
        {"status": "cancelled", "error": note, "completed_at": now})
    rs = _t("report_subscriptions")
    paused = [str(r) for r in (await session.execute(select(rs.c.id).where(
        rs.c.created_by.in_(_match(rs.c.created_by, user)), rs.c.enabled.is_(True)))).scalars().all()]
    if paused:
        await session.execute(update(rs).where(rs.c.id.in_(paused)).values(enabled=False))
    counts["report_subscriptions_paused"] = len(paused)
    for table, key in (("schedules", "authored_schedules"), ("bot_definitions", "authored_bots")):
        tb = _t(table)
        counts[key] = (await session.execute(select(func.count()).select_from(tb).where(
            tb.c.created_by.in_(_match(tb.c.created_by, user))))).scalar() or 0
    cr = _t("content_rollouts")
    counts["rollout_requests_open"] = (await session.execute(select(func.count()).select_from(cr).where(
        cr.c.requested_by.in_(_match(cr.c.requested_by, user)), cr.c.state == "requested"))).scalar() or 0
    return {"reports_paused": paused}


async def check_delete_allowed(session: AsyncSession, actor: Any, user: Any) -> None:
    """Refuse a delete that would lock a tenant out or looks like an attack.

    * nobody deletes their own account;
    * the last active admin of a tenant is not deleted;
    * one admin deleting many accounts in an hour: refused past
      `user_delete_bulk_limit` (superadmin excepted) — the alert past
      `user_delete_bulk_alert` is the caller's (security event)."""
    from velaris_shared.config import get_settings
    if str(user.id) == str(getattr(actor, "user_id", "")) or user.username == getattr(actor, "username", None):
        raise DeleteRefused("You cannot delete your own account.")
    if "admin" in (user.roles or []) and user.is_active and user.deleted_at is None:
        others = (await session.execute(
            select(M.HelixUserModel).where(M.HelixUserModel.id != user.id,
                                           M.HelixUserModel.is_active.is_(True),
                                           M.HelixUserModel.deleted_at.is_(None)))).scalars().all()
        tenant = await tenant_of(session, user)
        same = [u for u in others if "admin" in (u.roles or []) and await tenant_of(session, u) == tenant]
        if not same:
            raise DeleteRefused("This is the last active admin of the tenant — make another admin first.")
    if "superadmin" not in (getattr(actor, "roles", None) or []):
        if await deletes_by(session, actor, hours=1) >= get_settings().user_delete_bulk_limit:
            raise DeleteRefused("Too many accounts deleted in the last hour — ask a superadmin, "
                                "or wait.")


async def tenant_of(session: AsyncSession, user: Any) -> str:
    """The account's tenant: its directory row's (by id or username); none = default."""
    from velaris_shared.tenancy.identity import DEFAULT_TENANT
    row = (await session.execute(select(M.UserDirectoryModel.tenant_id).where(
        M.UserDirectoryModel.user_id.in_([str(user.id), user.username])))).scalars().first()
    return str(row or DEFAULT_TENANT)


async def deletes_by(session: AsyncSession, actor: Any, *, hours: int) -> int:
    ev = _t("security_events")
    since = _now() - timedelta(hours=hours)
    return (await session.execute(select(func.count()).select_from(ev).where(
        ev.c.event_type == "user_deleted", ev.c.user_id == str(getattr(actor, "user_id", "")),
        ev.c.timestamp >= since))).scalar() or 0


async def mark_deleted(session: AsyncSession, user: Any, *, actor: str, actor_id: str | None = None,
                       grace_days: int | None = None) -> dict[str, int]:
    """The delete an admin asks for: refused everywhere and signed out at once,
    kept (username reserved) until `purge_after`. In the caller's transaction."""
    from velaris_shared.config import get_settings
    days = get_settings().user_delete_grace_days if grace_days is None else grace_days
    counts: dict[str, int] = {}
    await _end_sessions(session, user, actor, counts, actor_id)
    await _release_assignments(session, user, actor, counts)
    undo = await _work_left_behind(session, user, actor, counts)
    # Hidden from pickers and lists; what it was is kept for an undo.
    d = _t("user_directory")
    was = {str(r.id): bool(r.is_active) for r in (await session.execute(
        select(d.c.id, d.c.is_active).where(d.c.user_id.in_(_match(d.c.user_id, user))))).all()}
    if was:
        await session.execute(update(d).where(d.c.id.in_(list(was))).values(is_active=False))
    now = _now()
    # Every token issued so far is refused for good — also after an undo.
    user.tokens_valid_after = now
    user.deleted_at, user.deleted_by = now, actor
    user.purge_after = now + timedelta(days=max(0, days))
    user.deleted_meta = json.dumps({"directory_active": was, **undo})
    counts["directory_hidden"] = len(was)
    log.info("user %s marked deleted by %s (purge after %s) — %s", user.id, actor,
             user.purge_after.isoformat(), {k: v for k, v in counts.items() if v})
    return counts


async def undo_delete(session: AsyncSession, user: Any, *, actor: str) -> None:
    """Within the grace period: the account comes back as it was (the user signs
    in again — sessions and devices ended by the delete stay ended)."""
    if user.deleted_at is None:
        raise DeleteRefused("This account is not deleted.")
    meta = json.loads(user.deleted_meta or "{}")
    d = _t("user_directory")
    for row_id, active in (meta.get("directory_active") or {}).items():
        await session.execute(update(d).where(d.c.id == row_id).values(is_active=active))
    # Report deliveries the delete paused come back; withdrawn requests and
    # cancelled jobs do not (they are raised again if still wanted).
    if meta.get("reports_paused"):
        rs = _t("report_subscriptions")
        await session.execute(update(rs).where(rs.c.id.in_(meta["reports_paused"])).values(enabled=True))
    user.deleted_at = user.deleted_by = user.purge_after = user.deleted_meta = None
    log.info("user %s delete undone by %s", user.id, actor)


async def purge_user(session: AsyncSession, user: Any, *, actor: str,
                     actor_id: str | None = None) -> dict[str, int]:
    """Final: everything that grants access, then the row. In the caller's
    transaction. Sessions are ended here too (a purge "at once" skips the mark)."""
    counts: dict[str, int] = {}
    if user.deleted_at is None:
        await _end_sessions(session, user, actor, counts, actor_id)
        await _release_assignments(session, user, actor, counts)
        await _work_left_behind(session, user, actor, counts)
    # A legal hold freezes a CASE's evidence (Nov_TODO "User deletion v2", 11):
    # the user's AI conversations about a held case, and absences tied to one,
    # stay; everything personal and unheld goes.
    holds = _t("legal_holds")
    held_cases = select(holds.c.case_id).where(holds.c.status == "active")
    conv, msgs = _t("hxnexus_conversations"), _t("hxnexus_messages")
    mine = conv.c.user_id.in_(_match(conv.c.user_id, user))
    unheld = (conv.c.case_id.is_(None)) | (conv.c.case_id.not_in(held_cases))
    conv_ids = select(conv.c.id).where(mine, unheld).scalar_subquery()
    counts["hxnexus_messages"] = (await session.execute(
        delete(msgs).where(msgs.c.conversation_id.in_(conv_ids)))).rowcount or 0
    counts["hxnexus_conversations"] = (await session.execute(delete(conv).where(mine, unheld))).rowcount or 0
    counts["hxnexus_conversations_kept_for_hold"] = (await session.execute(
        select(func.count()).select_from(conv).where(mine))).scalar() or 0
    for table, column in CLEAR:
        tb = _t(table)
        counts[f"{table}.{column}"] = (await session.execute(
            update(tb).where(tb.c[column].in_(_match(tb.c[column], user))).values({column: None}))).rowcount or 0
    ab = _t("user_absences")
    counts["user_absences_kept_for_hold"] = (await session.execute(
        select(func.count()).select_from(ab).where(ab.c.user_id.in_(_match(ab.c.user_id, user)),
                                                   ab.c.case_id.in_(held_cases)))).scalar() or 0
    await _delete_rows(session, CREDENTIALS + tuple(x for x in ACCESS if x[0] != "user_absences"),
                       user, counts)
    counts["user_absences"] = (await session.execute(delete(ab).where(
        ab.c.user_id.in_(_match(ab.c.user_id, user)),
        (ab.c.case_id.is_(None)) | (ab.c.case_id.not_in(held_cases))))).rowcount or 0
    tup = _t("hxguard_tuples")
    counts["hxguard_tuples"] = (await session.execute(
        delete(tup).where(tup.c.subject_type == "user",
                          tup.c.subject_id.in_(_match(tup.c.subject_id, user))))).rowcount or 0
    # The ledger record, in this same transaction: a purge never lacks its
    # record, a record never names an account that was not purged.
    from velaris_shared.identity import ledger
    await ledger.enqueue(session, user, actor=actor)
    await session.delete(user)
    log.info("user %s purged by %s — removed %s", user.id, actor, {k: v for k, v in counts.items() if v})
    return counts


async def purge_due(session: AsyncSession, *, now: datetime | None = None) -> list[str]:
    """The worker's job: purge every account whose grace period has passed.
    One commit per account, so one failure does not hold the rest back."""
    from velaris_shared.db.security_events import log_security_event
    now = now or _now()
    due = (await session.execute(select(M.HelixUserModel).where(
        M.HelixUserModel.deleted_at.is_not(None), M.HelixUserModel.purge_after <= now))).scalars().all()
    done = []
    for user in due:
        uid, name = str(user.id), user.username
        try:
            removed = await purge_user(session, user, actor="system:purge")
            await log_security_event(
                session, event_type="user_purged", severity="info", user_id=None,
                resource_type="user", resource_id=uid, action="purge", outcome="success",
                details={"username": name, "removed": {k: v for k, v in removed.items() if v}})
            await session.commit()
            done.append(uid)
        except Exception:  # noqa: BLE001
            await session.rollback()
            log.exception("purging deleted user %s failed — retried at the next run", uid)
    return done


PURGE_INTERVAL_SECONDS = 3600
PURGE_STARTUP_DELAY_SECONDS = 120


async def purge_due_cron() -> None:
    """user-service's job (one at a time across copies: the `user-purge` lease):
    every hour, purge the accounts whose grace period has passed. Survives a bad
    cycle, like the other schedulers; an account that fails is tried next run."""
    import asyncio

    from velaris_shared.db.session import get_session_factory
    from velaris_shared.identity import ledger

    # At start, before anything else: an account the ledger names that the
    # database still holds came back with a restore — purge it again now.
    try:
        async with get_session_factory()() as session:
            await ledger.replay_local(session)
    except Exception as exc:  # noqa: BLE001
        log.warning("deletion ledger replay at start failed (retried hourly): %s", exc)
    await asyncio.sleep(PURGE_STARTUP_DELAY_SECONDS)
    while True:
        try:
            async with get_session_factory()() as session:
                await ledger.replay_local(session)
                done = await purge_due(session)
                # Write every queued ledger record (and retry any that failed).
                written, failed = await ledger.flush(session)
            if done:
                log.info("purged %d deleted account(s) past their grace period", len(done))
            if failed:
                log.error("deletion ledger: %d record(s) not written — retried next run", failed)
        except Exception as exc:  # noqa: BLE001
            log.warning("deleted-account purge failed (retried next run): %s", exc)
        await asyncio.sleep(PURGE_INTERVAL_SECONDS)
