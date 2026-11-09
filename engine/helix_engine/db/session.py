"""
Database Session
=================

Manages the async SQLAlchemy engine and session factory.

The engine is created once during FastAPI startup and shared.
Sessions are created per-request (or per-operation in background tasks).

Connection string comes from:
  1. ``DATABASE_URL`` environment variable, or
  2. ``VELARIS_CASE_DATABASE_URL`` — the one the installer actually writes, or
  3. Default: ``postgresql+asyncpg://helix:helix_dev_password@localhost:5432/helix``

Usage::

    from helix_engine.db.session import init_db, get_session

    # During startup:
    await init_db()

    # In a route or background task:
    async with get_session() as session:
        result = await session.execute(select(ProcessDefinition))
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

import structlog
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from helix_engine.db.models import Base

logger = structlog.get_logger()

# Default connection string matching docker-compose.yml
DEFAULT_DATABASE_URL = "postgresql+asyncpg://helix:helix_dev_password@localhost:5432/helix"

# Module-level singleton
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


# Async driver for a URL that names none. `VELARIS_CASE_DATABASE_URL` is written
# by the installer in async form, but a hand-edited .env may not be, and
# create_async_engine refuses a sync driver with an error that reads like a
# missing package rather than a wrong scheme.
_ASYNC_DRIVER = {"postgresql": "asyncpg", "postgres": "asyncpg", "mysql": "aiomysql", "mariadb": "aiomysql"}


def _database_url_from_env() -> str:
    """The connection string, in the order the platform actually supplies it.

    ``DATABASE_URL`` still wins, so an install that sets it sees no change.

    The fallback exists because **nothing has ever set DATABASE_URL**:
    ``setup-velaris.sh`` writes ``VELARIS_CASE_DATABASE_URL`` and the engine's
    unit file passes ``.env`` straight through, so the engine reached the line
    below, found nothing, and used its compiled-in default — ``helix@localhost/
    helix``, which no install has been called since the velaris rename. It did
    not fail on that: ``init_db`` catches the refusal, falls back to an
    in-memory store and reports the engine ready, so a fresh install persisted
    no process definitions or instances from day one and nothing said so.
    """
    url = os.environ.get("DATABASE_URL")
    if url:
        return url
    url = os.environ.get("VELARIS_CASE_DATABASE_URL")
    if not url:
        return DEFAULT_DATABASE_URL
    # R5d 6a: through the pooler when one is set and the database is PostgreSQL
    # (PgBouncer only speaks PostgreSQL). Empty = direct, as before.
    pooler = (os.environ.get("VELARIS_CASE_DATABASE_POOLER_URL") or "").strip()
    if pooler and url.split("://", 1)[0].split("+", 1)[0].lower() in ("postgresql", "postgres"):
        url = pooler
        # Through PgBouncer's UNIX socket when the install says so (same rule
        # as the platform: velaris_shared.db.backends.pooler_socket_url).
        if (os.environ.get("VELARIS_CASE_DATABASE_POOLER_SOCKET") or "").strip().lower() in ("1", "true", "yes", "on"):
            from velaris_shared.db.backends import pooler_socket_url
            url = pooler_socket_url(url)
    scheme = url.split("://", 1)[0]
    if "+" in scheme:
        return url
    driver = _ASYNC_DRIVER.get(scheme.lower())
    return f"{scheme}+{driver}{url[len(scheme):]}" if driver else url


async def init_db(database_url: str | None = None) -> AsyncEngine:
    """
    Initialise the database engine and create tables.

    Args:
        database_url: SQLAlchemy async connection string.
                      Defaults to DATABASE_URL env var or the docker-compose default.

    Returns:
        The SQLAlchemy async engine.
    """
    global _engine, _session_factory

    url = database_url or _database_url_from_env()

    logger.info("database_connecting", url=_mask_password(url))

    # TLS keys in the URL (a database on another server) become driver
    # arguments; a URL without them is used unchanged.
    from velaris_shared.db.tls import split_tls
    url, tls_args = split_tls(url)
    _engine = create_async_engine(
        url,
        echo=False,           # Set True for SQL debugging
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,   # Verify connections before use
        **({"connect_args": tls_args} if tls_args else {}),
    )

    _session_factory = async_sessionmaker(
        _engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    # Create tables if they don't exist
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    logger.info("database_connected", tables=list(Base.metadata.tables.keys()))
    return _engine


async def close_db() -> None:
    """Close the database engine."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        logger.info("database_closed")
    _engine = None
    _session_factory = None


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Get an async database session.

    Usage::

        async with get_session() as session:
            result = await session.execute(query)
    """
    if _session_factory is None:
        raise RuntimeError("Database not initialised. Call init_db() first.")

    async with _session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def is_db_connected() -> bool:
    """Check if the database engine has been initialised."""
    return _engine is not None


def _mask_password(url: str) -> str:
    """Mask the password in a connection string for logging."""
    if "@" in url and ":" in url.split("@")[0]:
        parts = url.split("@")
        credentials = parts[0]
        # Find the password portion
        pre_password = credentials.rsplit(":", 1)[0]
        return f"{pre_password}:****@{parts[1]}"
    return url
