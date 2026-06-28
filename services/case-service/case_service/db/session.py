"""Async SQLAlchemy session factories for case-service.

Three separate connection pools prevent slow analytics or compliance queries
from starving auth or case-CRUD operations:

  operations  (get_session)          — case CRUD, forms, general endpoints
  auth        (get_auth_session)     — login, refresh, token validation
  analytics   (get_analytics_session)— analytics, compliance, sync, sitemap

Each pool is independent: exhausting one cannot block another.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from case_service.config import get_settings


def _make_engine(pool_size: int, max_overflow: int, url: str | None = None):
    settings = get_settings()
    # url is passed only for the explicit read-replica (db_analytics_url); all primary
    # pools resolve through the first-party backend allowlist (DB SDK Phase 0). The backend
    # also supplies connect-time correctness options (e.g. MySQL READ COMMITTED); PostgreSQL
    # returns {} so this path stays byte-identical for the default backend.
    from case_service.db.backends import get_backend, resolve_async_url
    backend = get_backend(settings.database_backend)
    return create_async_engine(
        url or resolve_async_url(settings),
        echo=settings.db_echo,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=True,  # recycle stale connections before use
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

    Backed by db_analytics_url when set; otherwise this IS the analytics
    engine (no extra pool). Deliberately separate from the analytics pool:
    that pool also serves compliance seals, hxsync, and PUO — all of which
    WRITE and must stay on the primary. Only endpoints with zero writes may
    use this engine, or they break the moment a real standby is configured.
    """
    global _replica_engine  # noqa: PLW0603
    if _replica_engine is None:
        s = get_settings()
        if not s.db_analytics_url:
            _replica_engine = get_analytics_engine()
        else:
            _replica_engine = _make_engine(
                s.db_analytics_pool_size, 2, url=s.db_analytics_url,
            )
    return _replica_engine


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

async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Operations pool — case CRUD, forms, general endpoints."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_auth_session() -> AsyncGenerator[AsyncSession, None]:
    """Dedicated auth pool — login, refresh, token checks.

    Isolated from the operations pool so a spike in case queries
    cannot delay authentication responses.
    """
    factory = get_auth_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_analytics_session() -> AsyncGenerator[AsyncSession, None]:
    """Analytics pool — reporting, compliance seals, sync, sitemap.

    Long-running queries stay in this pool and cannot starve auth or
    case-CRUD connections.
    """
    factory = get_analytics_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_replica_session() -> AsyncGenerator[AsyncSession, None]:
    """Read-only replica session — heavy analytics queries ONLY.

    No commit on success: endpoints using this dependency must not write
    (a read-only standby would reject the write; the missing commit makes
    that contract explicit even when no replica is configured).
    """
    factory = get_replica_session_factory()
    async with factory() as session:
        try:
            yield session
        finally:
            await session.rollback()
