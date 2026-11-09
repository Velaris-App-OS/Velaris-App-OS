"""Async SQLAlchemy session factories for case-service.

Three separate connection pools prevent slow analytics or compliance queries
from starving auth or case-CRUD operations:

  operations  (get_session)          — case CRUD, forms, general endpoints
  auth        (get_auth_session)     — login, refresh, token validation
  analytics   (get_analytics_session)— analytics, compliance, sync, sitemap

Each pool is independent: exhausting one cannot block another.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from fastapi import Request

from velaris_shared.config import get_settings
from velaris_shared.db.commit_route import register_session


def _make_engine(pool_size: int, max_overflow: int, url: str | None = None,
                 pool_timeout: float | None = None):
    settings = get_settings()
    # url is passed only for the explicit read-replica (db_analytics_url); all primary
    # pools resolve through the first-party backend allowlist (DB SDK Phase 0). The backend
    # also supplies connect-time correctness options (e.g. MySQL READ COMMITTED); PostgreSQL
    # returns {} so this path stays byte-identical for the default backend.
    from velaris_shared.db.backends import get_backend, resolve_runtime_url
    from velaris_shared.db.tls import split_tls
    backend = get_backend(settings.database_backend)
    # A database on another server names its TLS in the URL (sslmode=…,
    # sslrootcert=…); split_tls hands those to the driver. A URL without them —
    # the bundled database — passes through unchanged, with no connect_args.
    # resolve_runtime_url: through the pooler when VELARIS_CASE_DATABASE_POOLER_URL
    # is set (R5d 6a), otherwise direct — unchanged.
    url, tls_args = split_tls(url or resolve_runtime_url(settings))
    return create_async_engine(
        url,
        echo=settings.db_echo,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=True,  # recycle stale connections before use
        **({"pool_timeout": pool_timeout} if pool_timeout is not None else {}),
        **({"connect_args": tls_args} if tls_args else {}),
        **backend.engine_options(),
    )


# ── Engines (module-level singletons) ────────────────────────────────────────

_engine              = None
_auth_engine         = None
_analytics_engine    = None
_replica_engine      = None

_session_factory:            async_sessionmaker[AsyncSession] | None = None
_auth_session_factory:       async_sessionmaker[AsyncSession] | None = None
_analytics_session_factory:  async_sessionmaker[AsyncSession] | None = None
_replica_session_factory:    async_sessionmaker[AsyncSession] | None = None


def get_engine():
    global _engine  # noqa: PLW0603
    if _engine is None:
        s = get_settings()
        _engine = _make_engine(s.db_pool_size, s.db_max_overflow)
    return _engine


def get_auth_engine():
    global _auth_engine  # noqa: PLW0603
    if _auth_engine is None:
        s = get_settings()
        _auth_engine = _make_engine(s.db_auth_pool_size, 2)
    return _auth_engine


def get_analytics_engine():
    global _analytics_engine  # noqa: PLW0603
    if _analytics_engine is None:
        s = get_settings()
        _analytics_engine = _make_engine(s.db_analytics_pool_size, 2)
    return _analytics_engine


def get_replica_engine():
    """Read-only engine for heavy analytics queries (Group I).

    Backed by db_analytics_url when set; otherwise a pool of its own on the
    main database (until 2026-11-02 it was the analytics engine itself — see
    below why that deadlocked). Deliberately separate from the analytics pool:
    that pool also serves compliance seals, hxsync, and PUO — all of which
    WRITE and must stay on the primary. Only endpoints with zero writes may
    use this engine, or they break the moment a real standby is configured.
    """
    global _replica_engine  # noqa: PLW0603
    if _replica_engine is None:
        s = get_settings()
        url = replica_url()
        if not url:
            # Its OWN pool on the main database, never the analytics engine
            # itself (R6 soak, 2026-11-02). An analytics request holds a session
            # from each — the analytics session (where its sign-in check runs)
            # and this read session — so sharing one pool meant two connections
            # per request from the same 5: five requests each held one and
            # waited for another, nothing moved until the 30 s pool timeout,
            # and every analytics page answered 503 (VEL-AUTH-CHECK-BUSY) in
            # 31-second waves. Two pools: at most one connection from each per
            # request, so that wait cannot close on itself.
            _replica_engine = _make_engine(s.db_analytics_pool_size, 2)
        else:
            _replica_engine = _make_engine(s.db_analytics_pool_size, 2, url=url)
    return _replica_engine


def replica_url() -> str:
    """The configured replica: db_analytics_url (as before), else the R5d 11b
    database_read_url. Empty = no replica."""
    s = get_settings()
    return s.db_analytics_url or s.database_read_url


# ── Session factories ─────────────────────────────────────────────────────────

def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory  # noqa: PLW0603
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _session_factory


def get_auth_session_factory() -> async_sessionmaker[AsyncSession]:
    global _auth_session_factory  # noqa: PLW0603
    if _auth_session_factory is None:
        _auth_session_factory = async_sessionmaker(
            get_auth_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _auth_session_factory


def get_analytics_session_factory() -> async_sessionmaker[AsyncSession]:
    global _analytics_session_factory  # noqa: PLW0603
    if _analytics_session_factory is None:
        _analytics_session_factory = async_sessionmaker(
            get_analytics_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _analytics_session_factory


def get_replica_session_factory() -> async_sessionmaker[AsyncSession]:
    global _replica_session_factory  # noqa: PLW0603
    if _replica_session_factory is None:
        _replica_session_factory = async_sessionmaker(
            get_replica_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _replica_session_factory


# ── FastAPI dependencies ──────────────────────────────────────────────────────

async def _rollback_quietly(session: AsyncSession) -> None:
    """Roll back after a failed request without hiding why it failed.

    When the database or pooler dies mid-request, the rollback fails too, with a
    raw driver error (asyncpg InternalClientError) that replaced the real one and
    answered 500 instead of the 503 the error handlers give a lost connection
    (R6 failure test, 2026-11-03). The original exception is what propagates.
    """
    try:
        await session.rollback()
    except Exception:  # noqa: BLE001 — the connection is already gone
        pass


async def get_session(request: Request = None) -> AsyncGenerator[AsyncSession, None]:
    """Operations pool — case CRUD, forms, general endpoints."""
    factory = get_session_factory()
    async with factory() as session:
        # Registered so CommitBeforeResponseRoute can commit this session
        # before the response is sent; the commit below then finds nothing
        # pending. Outside an HTTP request `request` is None and nothing
        # changes. See velaris_shared/db/commit_route.py.
        register_session(request, session)
        try:
            yield session
            await session.commit()
        except Exception:
            await _rollback_quietly(session)
            raise


async def get_auth_session(request: Request = None) -> AsyncGenerator[AsyncSession, None]:
    """Dedicated auth pool — login, refresh, token checks.

    Isolated from the operations pool so a spike in case queries
    cannot delay authentication responses.
    """
    factory = get_auth_session_factory()
    async with factory() as session:
        # Registered so CommitBeforeResponseRoute can commit this session
        # before the response is sent; the commit below then finds nothing
        # pending. Outside an HTTP request `request` is None and nothing
        # changes. See velaris_shared/db/commit_route.py.
        register_session(request, session)
        try:
            yield session
            await session.commit()
        except Exception:
            await _rollback_quietly(session)
            raise


async def get_analytics_session(request: Request = None) -> AsyncGenerator[AsyncSession, None]:
    """Analytics pool — reporting, compliance seals, sync, sitemap.

    Long-running queries stay in this pool and cannot starve auth or
    case-CRUD connections.
    """
    factory = get_analytics_session_factory()
    async with factory() as session:
        # Registered so CommitBeforeResponseRoute can commit this session
        # before the response is sent; the commit below then finds nothing
        # pending. Outside an HTTP request `request` is None and nothing
        # changes. See velaris_shared/db/commit_route.py.
        register_session(request, session)
        try:
            yield session
            await session.commit()
        except Exception:
            await _rollback_quietly(session)
            raise


async def get_replica_session(request: Request = None) -> AsyncGenerator[AsyncSession, None]:
    """Read-only replica session — heavy analytics queries ONLY.

    Since R5d 11b this is the routed read session (velaris_shared/db/read_routing.py):
    the replica when one is configured and usable, the main otherwise; read your
    own writes; READ ONLY transactions. No commit: endpoints using this dependency
    must not write — now refused by the database, not only by convention.
    """
    from velaris_shared.db.read_routing import read_session
    async with read_session(request, analytics_pool=True) as session:
        yield session
