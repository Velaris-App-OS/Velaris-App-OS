"""First-party database backends — multi-dialect support (DB SDK Phase 0).

SECURITY: backends are FIRST-PARTY ONLY and chosen from `ALLOWED_BACKENDS`, a baked-in
fail-closed allowlist. This deliberately does NOT use `helix_sdk.plugin.PluginRegistry`
(entry_points auto-discovery) — a DB backend gets credentials and runs SQL at top
privilege, so it must never be loadable from an arbitrary installed package. A code-level
dict is more tamper-resistant than a data file (nothing to read, nothing to swap on disk).
An unknown backend name aborts startup; there is no permissive fallback.

Phase 0 ships PostgreSQL only and is behaviour-preserving: `resolve_async_url` returns the
existing `settings.database_url` verbatim when set (trusted operator / OpenBao config). The
component-built URL path and a scheme/driver pin arrive with the second dialect (Phase 1).

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from case_service.db.backends.mariadb import MariadbBackend
from case_service.db.backends.mysql import MysqlBackend
from case_service.db.backends.postgres import PostgresBackend

if TYPE_CHECKING:
    # `DatabaseBackend` is a structural (Protocol) contract. It is imported for typing
    # ONLY — never at runtime — so the service does not load the helix_sdk.protocols
    # package (an unrelated stub whose __init__ is currently broken). Backends conform
    # structurally; no runtime dependency on the SDK is created here.
    from helix_sdk.protocols.database import DatabaseBackend

# Baked-in allowlist. The ONLY backends Velaris may run on. Adding a dialect is a
# platform-release change here — never a runtime / package operation.
ALLOWED_BACKENDS: "dict[str, type[DatabaseBackend]]" = {
    "postgresql": PostgresBackend,
    "mysql": MysqlBackend,
    "mariadb": MariadbBackend,
}


def get_backend(name: str) -> "DatabaseBackend":
    """Return the first-party backend for ``name``, or abort.

    Fail-closed: an unknown / unsupported name raises SystemExit so the service refuses
    to start rather than guessing a default.
    """
    cls = ALLOWED_BACKENDS.get((name or "").strip().lower())
    if cls is None:
        raise SystemExit(
            f"Unsupported database backend {name!r}. "
            f"Allowed (first-party only): {sorted(ALLOWED_BACKENDS)}."
        )
    return cls()


def resolve_async_url(settings: Any) -> str:
    """Async SQLAlchemy URL for the running service.

    Validates the backend *name* through the allowlist, then returns the configured
    full URL verbatim when present (Phase 0 = byte-identical to legacy behaviour), else
    builds one from typed components.
    """
    backend = get_backend(getattr(settings, "database_backend", "postgresql"))
    full = getattr(settings, "database_url", "") or ""
    if full:
        # Scheme/driver pin: a full URL must match the selected backend's URL scheme,
        # so a `database: mysql` config can't be silently pointed at a postgres URL
        # (or vice-versa). Lenient on the driver suffix, strict on the scheme. We match
        # the backend's *driver scheme* (`postgresql` / `mysql`) rather than its name so
        # MariaDB (name "mariadb", but driver scheme `mysql+aiomysql`) accepts a
        # `mysql://` URL — the SQLAlchemy dialect for MariaDB genuinely is mysql.
        dialect = full.split("://", 1)[0].split("+", 1)[0]
        expected = backend.async_driver().split("+", 1)[0]
        if dialect != expected:
            raise SystemExit(
                f"database_url scheme {dialect!r} does not match backend "
                f"{backend.name()!r} (expects {expected!r}). "
                f"Fix velaris.yaml: database: or the URL."
            )
        return full
    return backend.async_url(settings)


def resolve_sync_url(settings: Any) -> str:
    """Sync SQLAlchemy URL for migrations / admin scripts."""
    backend = get_backend(getattr(settings, "database_backend", "postgresql"))
    return backend.sync_url(settings)


__all__ = [
    "ALLOWED_BACKENDS",
    "MariadbBackend",
    "MysqlBackend",
    "PostgresBackend",
    "get_backend",
    "resolve_async_url",
    "resolve_sync_url",
]
