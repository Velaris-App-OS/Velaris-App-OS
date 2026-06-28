"""First-party MySQL / MariaDB backend (DB SDK Phase 1).

MySQL 8+ / MariaDB 10.6+ via aiomysql (async) / pymysql (sync migrations). Implements
the `helix_sdk.protocols.database.DatabaseBackend` contract.

Connect-time hardening (`engine_options`): MySQL's default isolation is REPEATABLE READ,
but case-service is written against READ COMMITTED semantics — so it is forced here. utf8mb4
is enforced at schema-creation time (database/table charset), not per connection.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from typing import Any
from urllib.parse import quote


class MysqlBackend:
    """MySQL 8+ / MariaDB 10.6+ via aiomysql (async) / pymysql (sync)."""

    ASYNC_DRIVER = "mysql+aiomysql"
    SYNC_DRIVER = "mysql+pymysql"
    DEFAULT_PORT = 3306

    def name(self) -> str:
        return "mysql"

    def async_driver(self) -> str:
        return self.ASYNC_DRIVER

    def sync_driver(self) -> str:
        return self.SYNC_DRIVER

    def engine_options(self) -> dict:
        # case-service assumes READ COMMITTED; MySQL defaults to REPEATABLE READ.
        return {"isolation_level": "READ COMMITTED"}

    def migration_dialect(self) -> str:
        return "mysql"

    def driver_packages(self) -> list[str]:
        return ["aiomysql", "pymysql"]

    async def next_case_seq(self, session: Any) -> int:
        # MySQL has no SEQUENCE. Emulate with a self-seeding atomic counter row:
        # the INSERT…ON DUPLICATE KEY UPDATE bumps the value (creating the row on
        # first call), and LAST_INSERT_ID(expr) makes the new value readable
        # connection-locally — no race, no separate seed step. The row lock is held
        # until the request transaction commits, so concurrent creates serialize and
        # the increment rolls back on abort (differs from PG's non-transactional
        # nextval — see DB SDK open questions). Gap-free uniqueness still holds.
        from sqlalchemy import text
        await session.execute(text(
            "INSERT INTO velaris_sequences (name, value) VALUES ('case', LAST_INSERT_ID(1)) "
            "ON DUPLICATE KEY UPDATE value = LAST_INSERT_ID(value + 1)"
        ))
        return (await session.execute(text("SELECT LAST_INSERT_ID()"))).scalar()

    def async_url(self, config: Any) -> str:
        return self._build(self.ASYNC_DRIVER, config)

    def sync_url(self, config: Any) -> str:
        return self._build(self.SYNC_DRIVER, config)

    def _build(self, driver: str, config: Any) -> str:
        host = getattr(config, "db_host", "") or "localhost"
        port = getattr(config, "db_port", None) or self.DEFAULT_PORT
        name = getattr(config, "db_name", "") or "velaris"
        user = getattr(config, "db_user", "") or "velaris"
        password = getattr(config, "db_password", "") or ""
        userinfo = quote(str(user), safe="")
        if password:
            userinfo = f"{userinfo}:{quote(str(password), safe='')}"
        return f"{driver}://{userinfo}@{host}:{port}/{name}"

    async def health_check(self) -> bool:
        return True

    async def initialize(self, config: Any) -> None:
        return None

    async def shutdown(self) -> None:
        return None
