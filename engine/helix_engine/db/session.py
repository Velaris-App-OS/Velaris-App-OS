"""
Database Session
=================

Manages the async SQLAlchemy engine and session factory.

The engine is created once during FastAPI startup and shared.
Sessions are created per-request (or per-operation in background tasks).

Connection string comes from:
  1. ``DATABASE_URL`` environment variable, or
  2. ``helix.yaml`` database config, or
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

    url = database_url or os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)

    logger.info("database_connecting", url=_mask_password(url))

    _engine = create_async_engine(
        url,
        echo=False,           # Set True for SQL debugging
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,   # Verify connections before use
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
