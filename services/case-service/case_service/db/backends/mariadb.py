"""First-party MariaDB backend (DB SDK — MariaDB support).

MariaDB 10.6+ / 11.x. MariaDB is wire- and dialect-compatible with MySQL for
everything case-service needs, so this backend **reuses `MysqlBackend` wholesale**
(same `aiomysql` / `pymysql` drivers, same utf8mb4 consolidated baseline under
`migrations/mysql/`, same self-seeding case-number counter) and differs only in
identity:

  * ``name() -> "mariadb"`` so ``database: mariadb`` is a first-class, selectable
    backend, distinct from ``mysql`` in config / Studio footer / logs;
  * the SQLAlchemy URL scheme deliberately stays ``mysql+aiomysql`` / ``mysql+pymysql``:
    those drivers connect to a MariaDB server and SQLAlchemy auto-detects it
    (``dialect._is_mariadb = True``). There is no separate async MariaDB driver worth
    the dependency, and the scheme pin in ``resolve_async_url`` matches on the URL
    *scheme* (``mysql``), not the backend name, so this stays consistent.
  * ``migration_dialect()`` is inherited as ``"mysql"`` on purpose — MariaDB ships from
    the same ``migrations/mysql/`` baseline; there is no separate track to maintain.

The one behavioural MySQL/MariaDB divergence that matters — the statement-timeout
system variable (MySQL ``max_execution_time`` ms/SELECT-only vs MariaDB
``max_statement_time`` seconds/all-statements) — lives at the introspection layer
(`MariadbIntrospector`), dispatched on the live bind's ``_is_mariadb`` flag, not here.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from case_service.db.backends.mysql import MysqlBackend


class MariadbBackend(MysqlBackend):
    """MariaDB 10.6+ / 11.x — MysqlBackend with a distinct backend identity."""

    def name(self) -> str:
        return "mariadb"
