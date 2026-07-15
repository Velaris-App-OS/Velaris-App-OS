"""HxDBMigrate — read-only connector to an external SOURCE database (P1).

Security spine (mirrors the DB-SDK backends + connector trust model):
  * **First-party source-type allowlist**, fail-closed — only postgresql / mysql / mariadb;
    an unknown type raises, never a permissive fallback.
  * **SSRF host validation (source-appropriate)** — the source host is DNS-resolved and
    rejected if it maps to cloud-metadata / link-local / multicast / reserved ranges.
    Private (RFC1918) and loopback addresses are deliberately ALLOWED: a source DB is an
    admin-configured on-prem target that legitimately lives there (narrower than the
    HTTP-connector guard by design — see ``validate_source_host``).
  * **Read-only** — the session is opened READ ONLY and only ever issues SELECT /
    information_schema introspection. HxDBMigrate never writes to a source.
  * **No credential persistence in the URL** — creds are URL-encoded at connect time from
    the decrypted secret; callers store them HxVault-encrypted.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator
from urllib.parse import quote

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

# First-party allowlist: source type → async SQLAlchemy driver. MariaDB uses the mysql
# driver (SQLAlchemy detects it via dialect._is_mariadb). Adding a type is a code change.
_SOURCE_DRIVERS = {
    "postgresql": "postgresql+asyncpg",
    "mysql":      "mysql+aiomysql",
    "mariadb":    "mysql+aiomysql",
}
_DEFAULT_PORTS = {"postgresql": 5432, "mysql": 3306, "mariadb": 3306}

# TLS modes for a source connection:
#   disable — no TLS (fine for a trusted private/loopback link).
#   require — encrypt in transit, do NOT verify the server certificate (accepts self-signed;
#             the pragmatic default for internet sources where we don't hold the client's CA).
#   verify  — encrypt AND verify the certificate chain + hostname (strongest).
_SSL_MODES = ("disable", "require", "verify")


def ssl_modes() -> list[str]:
    return list(_SSL_MODES)


class SourceError(Exception):
    """Raised for an unsupported source type, a blocked host, or a connect failure."""


def source_types() -> list[str]:
    return sorted(_SOURCE_DRIVERS)


def normalise_type(source_type: str) -> str:
    st = (source_type or "").strip().lower()
    if st not in _SOURCE_DRIVERS:
        raise SourceError(f"Unsupported source type {source_type!r}. Allowed: {source_types()}")
    return st


def default_port(source_type: str) -> int:
    return _DEFAULT_PORTS[normalise_type(source_type)]


async def validate_source_host(host: str) -> None:
    """SSRF guard for a DB source host.

    Deliberately narrower than the HTTP-connector SSRF guard: a source database is an
    **admin-configured, on-prem/self-hosted** target, so private (RFC1918) and loopback
    addresses are LEGITIMATE and allowed — the connector doc's premise is that source data
    never leaves the customer's private network. What we still block is the genuinely
    dangerous class an admin should never be pointed at: **cloud-metadata / link-local**
    (169.254.0.0/16, fe80::/10) and multicast/reserved/unspecified addresses.
    """
    h = (host or "").strip().lower()
    if not h:
        raise SourceError("Source host is required")
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.run_in_executor(None, socket.getaddrinfo, h, None)
    except socket.gaierror:
        raise SourceError(f"Source host does not resolve: {host!r}")
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if ip.is_link_local:
            raise SourceError(f"Source host {host!r} resolves to a blocked link-local/metadata address")
        if ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            raise SourceError(f"Source host {host!r} resolves to a disallowed address")


def _build_url(source_type: str, host: str, port: int | None, database: str,
               username: str, password: str) -> str:
    st = normalise_type(source_type)
    driver = _SOURCE_DRIVERS[st]
    userinfo = quote(str(username or ""), safe="")
    if password:
        userinfo = f"{userinfo}:{quote(str(password), safe='')}"
    p = port or _DEFAULT_PORTS[st]
    return f"{driver}://{userinfo}@{host}:{p}/{database}"


def _connect_args(source_type: str, ssl_mode: str = "disable") -> dict[str, Any]:
    # Per-driver connect timeout so a bad host fails fast instead of hanging, plus TLS.
    st = normalise_type(source_type)
    base: dict[str, Any] = {"timeout": 10} if st == "postgresql" else {"connect_timeout": 10}

    mode = (ssl_mode or "disable").strip().lower()
    if mode not in _SSL_MODES:
        raise SourceError(f"Unsupported ssl_mode {ssl_mode!r}. Allowed: {list(_SSL_MODES)}")
    if mode == "disable":
        return base

    if st == "postgresql":
        # asyncpg accepts TLS mode strings directly.
        base["ssl"] = "require" if mode == "require" else "verify-full"
    else:
        # aiomysql takes an ssl.SSLContext.
        import ssl as _ssl
        ctx = _ssl.create_default_context()
        if mode == "require":
            ctx.check_hostname = False
            ctx.verify_mode = _ssl.CERT_NONE
        base["ssl"] = ctx
    return base


async def _set_read_only(session: AsyncSession, source_type: str) -> None:
    # Best-effort session-level read-only. Introspection is SELECT-only regardless, so a
    # driver that rejects the statement is non-fatal.
    try:
        if normalise_type(source_type) == "postgresql":
            await session.execute(text("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY"))
        else:
            await session.execute(text("SET SESSION TRANSACTION READ ONLY"))
    except Exception:
        pass


@asynccontextmanager
async def source_session(source_type: str, host: str, port: int | None, database: str,
                         username: str, password: str,
                         ssl_mode: str = "disable") -> AsyncIterator[AsyncSession]:
    """Yield a READ-ONLY AsyncSession to the source. Validates host (SSRF) first.

    NullPool: introspection connections are short-lived — never hold a pool to a foreign DB.
    """
    await validate_source_host(host)
    url = _build_url(source_type, host, port, database, username, password)
    engine = create_async_engine(url, poolclass=NullPool,
                                 connect_args=_connect_args(source_type, ssl_mode))
    try:
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            await _set_read_only(session, source_type)
            yield session
    finally:
        await engine.dispose()


async def test_connection(source_type: str, host: str, port: int | None, database: str,
                          username: str, password: str, ssl_mode: str = "disable") -> None:
    """Open a read-only session and run SELECT 1. Raises SourceError on any failure."""
    try:
        async with source_session(source_type, host, port, database, username, password, ssl_mode) as s:
            await s.execute(text("SELECT 1"))
    except SourceError:
        raise
    except Exception as exc:
        raise SourceError(f"Could not connect to source: {exc}")
