#!/usr/bin/env python3
"""
Verify superadmin exists in helix_users.
Called by start-velaris.sh as a belt-and-suspenders DB check.

Exit 0 = superadmin found (or DB unavailable — don't block startup).
Exit 1 = DB reachable but no superadmin found.
"""
from __future__ import annotations

import asyncio
import os
import sys
from urllib.parse import urlparse


DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://helix:helix_dev_password@localhost:5432/helix",
)


async def check() -> int:
    db_url = DATABASE_URL.replace("+asyncpg", "").replace("+aiosqlite", "")
    parsed = urlparse(db_url)
    try:
        import asyncpg
        conn = await asyncpg.connect(
            host=parsed.hostname or "localhost",
            port=parsed.port or 5432,
            database=parsed.path.lstrip("/"),
            user=parsed.username,
            password=parsed.password,
            timeout=5,
        )
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM helix_users WHERE is_superadmin = TRUE AND is_active = TRUE"
        )
        await conn.close()

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
