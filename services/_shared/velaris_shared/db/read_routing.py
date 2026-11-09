"""R5d 11b — reads on a replica, chosen per path (agreed 2026-10-22, Nov_TODO R5d).

A path opts in by taking `get_read_session` (a dashboard, a report, a search)
or, for a point-in-time output, `read_session_at_cutoff`. Every other path —
case and task views, sign-in, every write — keeps its own session on the main
database, exactly as before. Of 578 GET handlers, 19 write in their own body;
routing by HTTP method would break them on a read-only replica.

The rules, in the order they decide:

* **No replica configured** (`VELARIS_CASE_DB_ANALYTICS_URL` and
  `VELARIS_CASE_DATABASE_READ_URL` empty — every existing install): the read
  runs where it always did, on the main.
* **Read your own writes.** A user who wrote in the last
  `read_own_writes_seconds` reads from the main, whatever the path — nobody
  sees their own change missing. The mark is shared state (Valkey when the
  cache is on: a write in one service is seen by a read in another).
* **Behind is not down.** A replica that is merely behind keeps its reads (at a
  peak, moving them to the main would put the heavy load on the main exactly
  when it is busiest, and flap back and forth). The response carries the time
  its data is as of (`X-Velaris-Data-As-Of`).
* **Down is down.** A replica that does not answer, or answers as a server that
  is not this install's database, is skipped: its reads go to the main through
  a pool of `read_fallback_pool_size` connections — capped, so they can never
  crowd out the main's own work — and come back only after the replica has been
  healthy for `read_recovery_seconds` (no flapping). A replica lost since the
  last health look fails only the request that finds it, with a 503 to retry.
* **Read only** wherever a session of its own is handed out (replica, capped
  fallback, the analytics pool): its transactions begin with `SET TRANSACTION
  READ ONLY`, so a write that slips onto a read path fails at once.
* **No connection is held while dependencies resolve.** READ ONLY is applied
  when the session's first transaction begins, in the route body, and with no
  replica `get_read_session` / `get_cutoff_session` hand out the request's own
  operations session (the one its guard and sign-in check use). Holding a
  second connection from the same pool while waiting for the first
  deadlocked under load (proven on real PostgreSQL in the 11b audit,
  2026-10-23: 8 requests on a 2-connection pool all failed after 30 s).

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, Request
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as SATimeout
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from velaris_shared import shared_state
from velaris_shared.config import get_settings
from velaris_shared.db import session as dbs

log = logging.getLogger(__name__)

OWN_WRITES_NS = "read-own-writes"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
ROUTES = ("replica", "main_no_replica", "main_own_writes", "main_replica_down", "main_barrier")


def _postgres() -> bool:
    return (get_settings().database_backend or "postgresql").lower() in ("postgresql", "postgres")


# ── the capped fallback pool on the main ─────────────────────────────────────

_fallback_factory: async_sessionmaker[AsyncSession] | None = None


def fallback_factory() -> async_sessionmaker[AsyncSession]:
    global _fallback_factory  # noqa: PLW0603
    if _fallback_factory is None:
        s = get_settings()
        # A short wait: when the capped pool is full the caller gets a quick 503
        # with Retry-After, not a 30 s hang (get_read_session).
        engine = dbs._make_engine(max(1, s.read_fallback_pool_size), 0, pool_timeout=5)   # noqa: SLF001
        _fallback_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return _fallback_factory


def _main_connect():
    # Small looks at the main (its identity, its WAL position) go through the
    # capped pool, never the operations pool: the request asking may already
    # hold an operations connection (its guard's), and waiting for a second
    # one from the same pool is how the pool deadlocks.
    bind = fallback_factory().kw["bind"]
    return bind.connect()


# ── the replica's health, per process ────────────────────────────────────────

class ReplicaHealth:
    """Looked at most every `read_health_seconds`; on demand, no background task."""

    def __init__(self) -> None:
        self.checked_at = 0.0
        self.healthy_since: float | None = None
        self.was_down = False
        self.as_of: datetime | None = None
        self.reason = ""
        self._lock: asyncio.Lock | None = None

    def usable(self, now: float | None = None) -> bool:
        if self.healthy_since is None:
            return False
        if not self.was_down:
            return True                       # never failed in this process: no delay
        now = time.monotonic() if now is None else now
        return now - self.healthy_since >= get_settings().read_recovery_seconds

    def mark_down(self, why: object) -> None:
        if self.healthy_since is not None or not self.reason:
            log.warning("read replica unusable — reads go to the main (capped pool): %s", why)
        self.healthy_since = None
        self.was_down = True
        self.as_of = None
        self.reason = str(why)[:200]

    def _mark_up(self, as_of: datetime | None) -> None:
        if self.healthy_since is None:
            self.healthy_since = time.monotonic()
            if self.was_down:
                log.warning("read replica answering again — back in use after %.0f s healthy",
                            get_settings().read_recovery_seconds)
        self.as_of = as_of
        self.reason = ""

    async def refresh(self, force: bool = False) -> None:
        s = get_settings()
        if not force and time.monotonic() - self.checked_at < s.read_health_seconds:
            return
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if not force and time.monotonic() - self.checked_at < s.read_health_seconds:
                return
            self.checked_at = time.monotonic()
            try:
                as_of = await asyncio.wait_for(_probe(), timeout=5)
            except Exception as exc:  # noqa: BLE001
                self.mark_down(exc)
                return
            self._mark_up(as_of)


health = ReplicaHealth()
_main_sysid: str | None = None


async def _main_identity(sql: str) -> str:
    global _main_sysid  # noqa: PLW0603
    if _main_sysid is None:
        async with _main_connect() as m:
            _main_sysid = str((await m.execute(text(sql))).scalar())
    return _main_sysid


async def _probe() -> datetime | None:
    """How current the replica is; raises when it must not be used — above all
    when it is not THIS install's database (a wrong URL would serve someone
    else's data)."""
    if not _postgres():
        return await _probe_mysql()
    now = datetime.now(timezone.utc)
    async with dbs.get_replica_engine().connect() as c:
        in_recovery, replay_ts, caught_up, sysid = (await c.execute(text(
            "SELECT pg_is_in_recovery(), pg_last_xact_replay_timestamp(), "
            "coalesce(pg_last_wal_receive_lsn() = pg_last_wal_replay_lsn(), false), "
            "(SELECT system_identifier::text FROM pg_control_system())"))).one()
        # A physical replica carries its primary's system identifier: checked for
        # replicas too, not only for writable servers (a standby of another
        # cluster is in recovery as well).
        if str(sysid) != await _main_identity(
                "SELECT system_identifier::text FROM pg_control_system()"):
            raise RuntimeError("the read URL is not this install's database "
                               "(different system identifier)")
        if in_recovery:
            streaming = False
            try:
                streaming = (await c.execute(text(
                    "SELECT status = 'streaming' FROM pg_stat_wal_receiver"))).scalar() or False
            except Exception:  # noqa: BLE001 — not allowed to read it: be conservative
                streaming = False
            # Idle and caught up = current; otherwise the last replayed commit.
            return now if (streaming and caught_up) else replay_ts
    return now          # the main itself (or its pooler): current by definition


async def _probe_mysql() -> datetime | None:
    """MySQL / MariaDB: the same server as the main (server_uuid), or a replica
    whose source is the main; lag from Seconds_Behind_*. Anything that cannot
    be shown to be this install's database is refused."""
    now = datetime.now(timezone.utc)
    main_uuid = await _main_identity("SELECT @@server_uuid")
    async with dbs.get_replica_engine().connect() as c:
        if str((await c.execute(text("SELECT @@server_uuid"))).scalar()) == main_uuid:
            return now
        rows = None
        for stmt in ("SHOW REPLICA STATUS", "SHOW SLAVE STATUS"):
            try:
                rows = (await c.execute(text(stmt))).mappings().all()
                break
            except Exception:  # noqa: BLE001
                continue
    if not rows:
        raise RuntimeError("the read URL is another server and its replication status cannot be "
                           "read (REPLICATION CLIENT) — cannot show it is this install's database")
    st = dict(rows[0])
    source = st.get("Source_UUID") or st.get("Master_UUID")
    if source != main_uuid:
        async with _main_connect() as m:
            main_id = str((await m.execute(text("SELECT @@server_id"))).scalar())
        if str(st.get("Master_Server_Id") or st.get("Source_Server_Id") or "") != main_id:
            raise RuntimeError("the read URL replicates from another server, not this install's database")
    behind = st.get("Seconds_Behind_Source", st.get("Seconds_Behind_Master"))
    if behind is None:
        raise RuntimeError("the replica's SQL thread is not running")
    from datetime import timedelta
    return now - timedelta(seconds=float(behind))


# ── read your own writes ─────────────────────────────────────────────────────

def user_of(request: Request | None) -> str | None:
    """The signed-in user: set by get_current_user, else the token's subject —
    VERIFIED. The read session can be resolved before the route's own sign-in
    check; an unverified subject would let anyone ask "did user X just write?"
    by naming X in a forged token (found in the 11b audit)."""
    if request is None:
        return None
    uid = getattr(request.state, "velaris_user_id", None)
    if uid:
        return str(uid)
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    try:
        from velaris_shared.auth.jwt_handler import decode_jwt_token
        s = get_settings()
        claims = decode_jwt_token(auth[7:].strip(), secret=s.auth_secret, issuer=s.auth_issuer,
                                  audience=s.auth_audience, public_key=s.auth_rsa_public_key or "")
    except Exception:  # noqa: BLE001
        return None
    if claims.get("token_use"):          # purpose-built tokens are not session users
        return None
    return str(claims.get("sub") or "") or None


_warned_cache_off = False


def _note_cache_off() -> None:
    """Read your own writes across services needs the shared cache; without it
    the mark lives in the process that took the write."""
    global _warned_cache_off  # noqa: PLW0603
    if not _warned_cache_off and not getattr(get_settings(), "redis_enabled", False):
        _warned_cache_off = True
        log.warning("read replica configured with the cache off: 'read your own writes' holds only "
                    "inside one process — turn the cache on (VELARIS_CASE_REDIS_ENABLED)")


async def mark_wrote(user_id: str | None) -> None:
    s = get_settings()
    if user_id and s.read_own_writes_seconds > 0 and dbs.replica_url():
        await shared_state.put(OWN_WRITES_NS, user_id, "1", s.read_own_writes_seconds)


async def wrote_recently(user_id: str | None) -> bool:
    if not user_id:
        return False
    return await shared_state.get(OWN_WRITES_NS, user_id) is not None


# ── choosing, and the sessions ───────────────────────────────────────────────

def _count(route: str) -> None:
    try:
        from velaris_shared.observability.metrics import db_reads_total
        db_reads_total.labels(route=route).inc()
    except Exception:  # noqa: BLE001
        pass


def _replica_for(analytics_pool: bool) -> str:
    """The replica this path may use. The paths that were on get_replica_session
    before 11b follow DB_ANALYTICS_URL as they always did (else the read URL);
    the paths 11b routed only an operator's DATABASE_READ_URL — an install that
    already had DB_ANALYTICS_URL must not have them moved onto it by an update
    (11b audit, 2026-10-23)."""
    return dbs.replica_url() if analytics_pool else (get_settings().database_read_url or "")


async def choose(request: Request | None, *, analytics_pool: bool = False,
                 ) -> tuple[async_sessionmaker[AsyncSession], str, datetime | None]:
    if not _replica_for(analytics_pool):
        # No replica: each path keeps the pool it always used — the analytics
        # pool for the old get_replica_session paths, the operations pool for
        # everything 11b routed (found 2026-10-22: they had moved onto the
        # 3-connection analytics pool on every install).
        factory = dbs.get_replica_session_factory() if analytics_pool else dbs.get_session_factory()
        return factory, "main_no_replica", None
    _note_cache_off()
    if await wrote_recently(user_of(request)):
        # The path's own pool on the main, as with no replica — not the capped
        # pool: at a peak most active users have just written, and 3
        # connections would turn their reads into 503s (11b audit).
        factory = dbs.get_analytics_session_factory() if analytics_pool else dbs.get_session_factory()
        return factory, "main_own_writes", None
    await health.refresh()
    if health.usable():
        return dbs.get_replica_session_factory(), "replica", health.as_of
    return fallback_factory(), "main_replica_down", None


def _read_only(session: AsyncSession, timeout_ms: int = 0) -> None:
    """Every transaction of this session begins READ ONLY — applied when it
    begins (the first query, in the route body), so no connection is taken
    while the request's dependencies are still resolving.

    PostgreSQL, MySQL, MariaDB: the transaction refuses writes. SQLite (the
    unit-test database only, never an install) has no per-transaction form;
    its per-connection `query_only` is set here and cleared by _release.

    timeout_ms > 0 (PostgreSQL only): each transaction also gets
    `SET LOCAL statement_timeout` — transaction-scoped, so nothing leaks to the
    next user of the connection (PgBouncer transaction mode included)."""
    sqlite = _sqlite(session)
    pg_timeout = int(timeout_ms) if timeout_ms and timeout_ms > 0 and not sqlite and _postgres() else 0

    def _begin(sess, _transaction, connection) -> None:
        if sqlite:
            connection.exec_driver_sql("PRAGMA query_only = ON")
            sess.info["velaris_query_only"] = True
        else:
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            if pg_timeout:
                connection.exec_driver_sql(f"SET LOCAL statement_timeout = {pg_timeout}")

    event.listen(session.sync_session, "after_begin", _begin)
    session.info["velaris_read_only_hook"] = _begin


def _sqlite(session: AsyncSession) -> bool:
    return session.bind is not None and session.bind.dialect.name == "sqlite"


def _shared(main: object) -> AsyncSession | None:
    """The request's own operations session (`Depends(get_session)` — the same
    object its guard and sign-in check use), or None when called directly."""
    from fastapi import params
    return None if main is None or isinstance(main, params.Depends) else main  # type: ignore[return-value]


async def _release(session: AsyncSession) -> None:
    try:
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001 — connection already gone; keep the request's own error
            pass
        hook = session.info.pop("velaris_read_only_hook", None)
        if hook is not None:
            event.remove(session.sync_session, "after_begin", hook)
        if session.info.pop("velaris_query_only", False):
            await session.execute(text("PRAGMA query_only = OFF"))
    finally:
        await session.close()


def _note(request: Request | None, route: str, as_of: datetime | None) -> None:
    _count(route)
    if request is not None:
        request.state.velaris_read_route = route
        request.state.velaris_read_as_of = as_of


async def get_read_session(request: Request = None,  # type: ignore[assignment]
                           main: AsyncSession = Depends(dbs.get_session),
                           ) -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency for an opted-in read path. Never commits. With no
    replica: the request's own operations session, exactly as before 11b."""
    async with read_session(request, main=_shared(main)) as session:
        yield session


async def get_analytics_read_session(request: Request = None) -> AsyncGenerator[AsyncSession, None]:  # type: ignore[assignment]
    """The same, for the paths that were on the analytics pool before 11b
    (get_replica_session) — with no replica they stay there."""
    async with read_session(request, analytics_pool=True) as session:
        yield session


def _busy(exc: BaseException):
    from velaris_shared.core.error_codes import DB_UNAVAILABLE_503, coded
    return coded(503, DB_UNAVAILABLE_503, "The server is busy. Please try again in a moment.",
                 headers={"Retry-After": "5"})


@asynccontextmanager
async def read_session(request: Request | None = None, *, analytics_pool: bool = False,
                       main: AsyncSession | None = None) -> AsyncIterator[AsyncSession]:
    """The routed read session. A context manager, not a generator: the
    dependencies wrap it with `async with`, so an exception from the route body
    reaches the handlers below and the session is released when the request
    ends (found in the 11b audit: wrapped with `async for`, neither happened —
    the full-pool 503 never fired and the release waited for the garbage
    collector)."""
    factory, route, as_of = await choose(request, analytics_pool=analytics_pool)
    if route in ("main_no_replica", "main_own_writes") and main is not None:
        _note(request, route, as_of)
        yield main                      # the request's session: its owner commits / closes
        return
    session = factory()
    _read_only(session, get_settings().db_analytics_read_timeout_ms if analytics_pool else 0)
    _note(request, route, as_of)
    try:
        yield session
    except DBAPIError as exc:
        # statement_timeout fired (SQLSTATE 57014): the report ran past
        # db_analytics_read_timeout_ms — an honest "busy", not a 500.
        if getattr(getattr(exc, "orig", None), "sqlstate", None) == "57014" or \
                "canceling statement due to statement timeout" in str(exc):
            raise _busy(exc) from exc
        if not isinstance(exc, (OperationalError, InterfaceError)) or route != "replica":
            raise
        await health.refresh(force=True)
        raise _busy(exc) from exc
    except SATimeout as exc:
        # The capped main pool is full (the replica is down at a peak): a quick,
        # honest "busy" — never a hang, never more connections on the main.
        raise _busy(exc) from exc
    except (OperationalError, InterfaceError, OSError) as exc:
        if route != "replica":
            raise
        # The replica went away between the health look and this request's
        # first query (at most `read_health_seconds`): look again now, so the
        # next request goes to the main, and tell this caller to retry.
        await health.refresh(force=True)
        raise _busy(exc) from exc
    finally:
        await _release(session)


# The position is sent as text and cast in SQL: `CAST(:l AS pg_lsn)` alone makes
# asyncpg type the parameter pg_lsn, whose binary codec wants an integer — every
# barrier failed and took the replica out (clean box 2026-10-23).
_BARRIER_SQL = ("SELECT coalesce(pg_wal_lsn_diff(pg_last_wal_replay_lsn(), "
                "CAST(CAST(:l AS text) AS pg_lsn)) >= 0, true)")


@asynccontextmanager
async def read_session_at_cutoff(request: Request | None = None, *,
                                analytics_pool: bool = False,
                                main: AsyncSession | None = None) -> AsyncIterator[AsyncSession]:
    """For point-in-time outputs (scheduled reports, exports, snapshots): every
    change committed before this call is in what it reads. The main's WAL
    position now is the cutoff; the replica is used only once it has replayed
    past it (normally milliseconds, at most `read_barrier_max_seconds`),
    otherwise the main. The response's as-of is the cutoff."""
    cutoff_at = datetime.now(timezone.utc)
    factory, route = fallback_factory(), "main_barrier"
    if not _replica_for(analytics_pool):
        # The paths on the cutoff were on the operations pool before 11b
        # (analytics export: its analytics pool — the scoped wrapper passes it).
        factory, route = (dbs.get_replica_session_factory() if analytics_pool
                          else dbs.get_session_factory()), "main_no_replica"
    elif _postgres():
        await health.refresh()
        if health.usable():
            try:
                async with _main_connect() as m:
                    lsn = (await m.execute(text("SELECT pg_current_wal_lsn()::text"))).scalar()
                deadline = time.monotonic() + get_settings().read_barrier_max_seconds
                async with dbs.get_replica_engine().connect() as c:
                    while True:
                        # NULL replay position = not in recovery = the main itself (probe checked).
                        passed = (await c.execute(text(_BARRIER_SQL), {"l": lsn})).scalar()
                        if passed:
                            factory, route = dbs.get_replica_session_factory(), "replica"
                            break
                        if time.monotonic() >= deadline:
                            break
                        await asyncio.sleep(0.1)
            except Exception as exc:  # noqa: BLE001
                health.mark_down(exc)
    _note(request, route, cutoff_at)
    if route == "main_no_replica" and main is not None:
        yield main                      # the request's session, as before 11b
        return
    session = factory()
    _read_only(session)
    try:
        yield session
    except SATimeout as exc:
        raise _busy(exc) from exc
    finally:
        await _release(session)


def reset_for_tests() -> None:
    global _fallback_factory, _main_sysid, health, _warned_cache_off  # noqa: PLW0603
    _fallback_factory = None
    _main_sysid = None
    _warned_cache_off = False
    health = ReplicaHealth()


async def get_cutoff_session(request: Request = None,  # type: ignore[assignment]
                             main: AsyncSession = Depends(dbs.get_session),
                             ) -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency for a point-in-time output (an export, a ledger
    balance): `read_session_at_cutoff` — everything committed before the
    request is in what it reads. With no replica: the request's own session."""
    async with read_session_at_cutoff(request, main=_shared(main)) as session:
        yield session


async def get_analytics_cutoff_session(request: Request = None) -> AsyncGenerator[AsyncSession, None]:  # type: ignore[assignment]
    async with read_session_at_cutoff(request, analytics_pool=True) as session:
        yield session
