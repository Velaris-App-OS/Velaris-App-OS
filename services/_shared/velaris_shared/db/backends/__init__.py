"""First-party database backends — multi-dialect support (DB SDK Phase 0).

SECURITY: backends are FIRST-PARTY ONLY and chosen from `ALLOWED_BACKENDS`, a baked-in
fail-closed allowlist. This deliberately does NOT use `velaris_sdk.plugin.PluginRegistry`
(entry_points auto-discovery) — a DB backend gets credentials and runs SQL at top
privilege, so it must never be loadable from an arbitrary installed package. A code-level
dict is more tamper-resistant than a data file (nothing to read, nothing to swap on disk).
An unknown backend name aborts startup; there is no permissive fallback.

Phase 0 ships PostgreSQL only and is behaviour-preserving: `resolve_async_url` returns the
existing `settings.database_url` verbatim when set (trusted operator / OpenBao config). The
component-built URL path and a scheme/driver pin arrive with the second dialect (Phase 1).

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from velaris_shared.db.backends.mariadb import MariadbBackend
from velaris_shared.db.backends.mysql import MysqlBackend
from velaris_shared.db.backends.postgres import PostgresBackend

if TYPE_CHECKING:
    # `DatabaseBackend` is a structural (Protocol) contract. It is imported for typing
    # ONLY — never at runtime — so the service does not load the velaris_sdk.protocols
    # package (an unrelated stub whose __init__ is currently broken). Backends conform
    # structurally; no runtime dependency on the SDK is created here.
    from velaris_sdk.protocols.database import DatabaseBackend

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


_pooler_warned = False


#: Services with their own pool on the shared pooler: VELARIS_SERVICE_NAME -> setting.
_OWN_POOL = {
    "commerce-service": "commerce_database_pooler_url",   # R5e step 7
    "portal-service": "portal_database_pooler_url",       # R5e step 8
    "user-service": "user_database_pooler_url",           # R5e step 10
}


def resolve_runtime_url(settings: Any) -> str:
    """The URL the application's own pools connect to.

    ``database_pooler_url`` when set (R5d 6a — PgBouncer), else the direct URL from
    ``resolve_async_url``. Empty = direct, exactly as before. PostgreSQL only: on
    MySQL / MariaDB the setting is ignored with one warning (ProxySQL is 6c), never
    a refused boot. The scheme pin is the same as for the direct URL, so a pooler
    URL cannot point a PostgreSQL install at another dialect.

    commerce-service uses ``commerce_database_pooler_url``, portal-service
    ``portal_database_pooler_url`` and user-service ``user_database_pooler_url``
    instead when that is set too (its own pool on the same pooler, R5e steps 7,
    8 and 10). Never without the shared setting: empty
    ``database_pooler_url`` means no pooler runs at all.
    """
    global _pooler_warned  # noqa: PLW0603
    direct = resolve_async_url(settings)
    pooler = (getattr(settings, "database_pooler_url", "") or "").strip()
    if not pooler:
        return direct
    setting, fallback = "VELARIS_CASE_DATABASE_POOLER_URL", "connect directly"
    own_field = _OWN_POOL.get(os.environ.get("VELARIS_SERVICE_NAME", ""))
    if own_field:
        own = (getattr(settings, own_field, "") or "").strip()
        if own:
            pooler = own
            setting = "VELARIS_CASE_" + own_field.upper()
            fallback = "share VELARIS_CASE_DATABASE_POOLER_URL"
    backend = get_backend(getattr(settings, "database_backend", "postgresql"))
    if backend.name() != "postgresql":
        if not _pooler_warned:
            _pooler_warned = True
            import logging
            logging.getLogger(__name__).warning(
                "VELARIS_CASE_DATABASE_POOLER_URL is ignored on %s — the pooler is "
                "PostgreSQL only; connecting directly", backend.name(),
            )
        return direct
    scheme = pooler.split("://", 1)[0]
    if scheme.split("+", 1)[0] not in ("postgresql", "postgres"):
        raise SystemExit(
            f"{setting} scheme {scheme!r} is not PostgreSQL. "
            f"Fix it, or empty it to {fallback}."
        )
    if "+" not in scheme:
        pooler = backend.async_driver() + pooler[len(scheme):]
    if getattr(settings, "database_pooler_socket", False):
        pooler = pooler_socket_url(pooler)
    return pooler


def pooler_socket_dir() -> str:
    """deploy/docker-compose/pooler-socket in this install (ships with the
    release, so the pooler's bind mount always has a source)."""
    from pathlib import Path
    return str(Path(__file__).resolve().parents[5] / "deploy" / "docker-compose" / "pooler-socket")


def pooler_socket_url(url: str, sock_dir: str | None = None) -> str:
    """The same pooler URL — role, password, database name, options — connecting
    through PgBouncer's UNIX socket instead of host:port. The socket is
    `.s.PGSQL.6432` in that folder (PgBouncer's port inside the container)."""
    from urllib.parse import parse_qsl, urlencode
    scheme, rest = url.split("://", 1)
    netloc, _, tail = rest.partition("/")
    cred = netloc.rpartition("@")[0]
    db, _, query = tail.partition("?")
    keep = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True)
            if k not in ("host", "port") and not k.startswith("ssl")]
    sock = sock_dir or pooler_socket_dir()
    import os as _os
    if not _os.path.isdir(sock):
        raise SystemExit(f"VELARIS_CASE_DATABASE_POOLER_SOCKET is on but {sock} does not exist — "
                         "restore it from the release, or empty the setting to use TCP.")
    return (f"{scheme}://{cred + '@' if cred else ''}/{db}?"
            + urlencode(keep + [("host", sock), ("port", "6432")]))


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
    "resolve_runtime_url",
    "resolve_sync_url",
]
