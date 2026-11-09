#!/usr/bin/env python3
"""
Verify superadmin exists in velaris_users.
Called by start-velaris.sh as a belt-and-suspenders DB check.

Exit 0 = superadmin found (or DB unavailable — don't block startup).
Exit 1 = DB reachable but no superadmin found.
"""
from __future__ import annotations

import asyncio
import os
import sys
from urllib.parse import unquote, urlparse


DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://helix:helix_dev_password@localhost:5432/helix",
)


# The superadmin count query — identical on both dialects (MySQL treats TRUE as 1,
# and is_superadmin / is_active are booleans → tinyint(1) there).
_QUERY = "SELECT COUNT(*) FROM velaris_users WHERE is_superadmin = TRUE AND is_active = TRUE"


def _tls() -> dict:
    """The platform's own database TLS (VELARIS_CASE_DATABASE_URL's sslmode /
    sslrootcert / ssl_ca …), as driver arguments — so this check reaches a
    database on another server the same way the platform does. {} when the URL
    names none: the connection is exactly what it was."""
    url = os.environ.get("VELARIS_CASE_DATABASE_URL", "")
    if "?" not in url:
        return {}
    from velaris_shared.db.tls import split_tls
    return split_tls(url)[1]


async def _count_postgres(parsed) -> int:
    import asyncpg
    conn = await asyncpg.connect(
        host=parsed.hostname or "localhost",
        port=parsed.port or 5432,
        database=parsed.path.lstrip("/"),
        user=unquote(parsed.username or ""),
        password=unquote(parsed.password or ""),
        timeout=5,
        **_tls(),
    )
    try:
        return await conn.fetchval(_QUERY) or 0
    finally:
        await conn.close()


async def _count_mysql(parsed) -> int:
    import aiomysql
    # Password from the URL, else VELARIS_DB_PASSWORD — so a BYO password with
    # @ : / # special chars need not be (mis)parsed out of the URL.
    conn = await aiomysql.connect(
        host=parsed.hostname or "localhost",
        port=parsed.port or 3306,
        db=parsed.path.lstrip("/"),
        user=unquote(parsed.username or ""),
        password=unquote(parsed.password or "") or os.environ.get("VELARIS_DB_PASSWORD", ""),
        connect_timeout=5,
        **_tls(),
    )
    try:
        async with conn.cursor() as cur:
            await cur.execute(_QUERY)
            row = await cur.fetchone()
            return (row[0] if row else 0) or 0
    finally:
        conn.close()  # aiomysql's close() is synchronous


async def check() -> int:
    # Strip the SQLAlchemy +driver suffix; dispatch on the dialect.
    db_url = (
        DATABASE_URL.replace("+asyncpg", "").replace("+aiosqlite", "")
        .replace("+aiomysql", "").replace("+pymysql", "")
    )
    parsed = urlparse(db_url)
    is_mysql = (parsed.scheme or "").startswith("mysql")
    try:
        count = await (_count_mysql(parsed) if is_mysql else _count_postgres(parsed))
        if count and count > 0:
            return 0
        print("\033[0;31m  ✗  No superadmin found in database.\033[0m")
        print("\033[0;31m     Run ./setup-velaris.sh to create one.\033[0m")
        return 1

    except Exception as e:
        print(f"\033[0;33m  ⚠  Could not check superadmin (DB unavailable): {e}\033[0m")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(check()))
