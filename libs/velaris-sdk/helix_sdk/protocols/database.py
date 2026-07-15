"""Protocol interface for database backends.

SECURITY — FIRST-PARTY ONLY. Unlike the other SDK protocols, database backends are
**never** discovered via entry_points / `pip install`. A DB backend receives connection
credentials and executes SQL at the highest privilege in the system, so it is the last
place untrusted code may run. Concrete backends are first-party, shipped in-image, and
selected from a baked-in fail-closed allowlist (see
`case_service/db/backends/__init__.py`). This module defines only the *contract*; do not
register implementations through `helix_sdk.plugin.PluginRegistry`.

A backend is a stateless URL/driver descriptor plus optional connect-time hardening.
"""

from __future__ import annotations
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class DatabaseBackend(Protocol):
    """Database backend contract. First-party implementations only."""

    def name(self) -> str:
        """Canonical dialect name: 'postgresql' | 'mysql' | 'sqlserver' | 'oracle'."""
        ...

    def async_driver(self) -> str:
        """SQLAlchemy async driver prefix, e.g. 'postgresql+asyncpg', 'mysql+aiomysql'."""
        ...

    def sync_driver(self) -> str:
        """SQLAlchemy sync driver prefix, e.g. 'postgresql+psycopg2', 'mysql+pymysql'."""
        ...

    def engine_options(self) -> dict:
        """Extra create_engine kwargs for connect-time correctness (e.g. isolation_level).
        Empty for backends that need none."""
        ...

    def async_url(self, config: Any) -> str:
        """SQLAlchemy async connection URL for the running service.

        Built from typed config fields (host/port/name/user + secret from env).
        Never accepts a raw `+driver://` passthrough from untrusted input.
        e.g. 'postgresql+asyncpg://user:pass@host:5432/db'
        """
        ...

    def sync_url(self, config: Any) -> str:
        """SQLAlchemy sync URL for migrations and admin scripts."""
        ...

    def migration_dialect(self) -> str:
        """Which subfolder of migrations/ to use: 'postgresql', 'mysql', etc."""
        ...

    def driver_packages(self) -> list[str]:
        """Fixed pip packages required for this backend (e.g. ['asyncpg'])."""
        ...

    async def next_case_seq(self, session: Any) -> int:
        """Next monotonic case-number value, using each dialect's native mechanism.

        PostgreSQL uses the `helix_case_seq` SEQUENCE; backends without sequences
        (e.g. MySQL) emulate it with an atomic counter row. Runs on the caller's
        session/transaction.
        """
        ...

    async def health_check(self) -> bool:
        """Return True if the backend is reachable and healthy."""
        ...

    async def initialize(self, config: Any) -> None:
        """Connect-time hardening / one-time setup (TLS, isolation level, timeouts)."""
        ...

    async def shutdown(self) -> None:
        """Gracefully shut down the backend."""
        ...
