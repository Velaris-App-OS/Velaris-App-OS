"""First-party PostgreSQL backend (DB SDK Phase 0).

The default and, in Phase 0, only allowlisted backend. Implements the
`helix_sdk.protocols.database.DatabaseBackend` contract as a stateless URL/driver
descriptor. PostgreSQL needs no connect-time hardening for the things case-service
assumes (its default isolation is already READ COMMITTED), so `initialize` is a no-op.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from typing import Any
from urllib.parse import quote


class PostgresBackend:
    """PostgreSQL 14+ via asyncpg (async) / psycopg2 (sync migrations)."""

    ASYNC_DRIVER = "postgresql+asyncpg"
    SYNC_DRIVER = "postgresql+psycopg2"
    DEFAULT_PORT = 5432

    def name(self) -> str:
        return "postgresql"

    def async_driver(self) -> str:
        return self.ASYNC_DRIVER

    def sync_driver(self) -> str:
        return self.SYNC_DRIVER

    def engine_options(self) -> dict:
        # PostgreSQL default isolation is already READ COMMITTED — nothing to add.
        return {}

    def migration_dialect(self) -> str:
        return "postgresql"

    def driver_packages(self) -> list[str]:
        return ["asyncpg", "psycopg2-binary"]

    async def next_case_seq(self, session: Any) -> int:
        # Native, non-transactional sequence — unchanged from the original call site.
        from sqlalchemy import text
        return (await session.execute(text("SELECT nextval('helix_case_seq')"))).scalar()

    def async_url(self, config: Any) -> str:
        return self._build(self.ASYNC_DRIVER, config)

    def sync_url(self, config: Any) -> str:
        return self._build(self.SYNC_DRIVER, config)

    def _build(self, driver: str, config: Any) -> str:
        host = getattr(config, "db_host", "") or "localhost"
        port = getattr(config, "db_port", None) or self.DEFAULT_PORT
        name = getattr(config, "db_name", "") or "helix"
        user = getattr(config, "db_user", "") or "helix"
        password = getattr(config, "db_password", "") or ""
        # URL-encode credential parts so passwords with @ : / ? # etc. don't corrupt the URL.
        userinfo = quote(str(user), safe="")
        if password:
            userinfo = f"{userinfo}:{quote(str(password), safe='')}"
        return f"{driver}://{userinfo}@{host}:{port}/{name}"

    async def health_check(self) -> bool:  # liveness probe wired in a later phase
        return True

    async def initialize(self, config: Any) -> None:  # PG default isolation already READ COMMITTED
        return None

    async def shutdown(self) -> None:
        return None
