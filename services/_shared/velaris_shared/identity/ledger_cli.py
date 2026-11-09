"""The deletion ledger, from the host (restore-velaris.sh, the backup tool).

    python -m velaris_shared.identity.ledger_cli replay --dir D [--dir D2 …]
    python -m velaris_shared.identity.ledger_cli flush

`replay` purges every account a verified record names that the database still
holds (a restored set taken before the purge brought it back) and prints one
JSON line: replayed, already gone, refused records. Run with the restored
install's settings, directly on the database (never the pooler, which a
restore has not started).

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path


async def _replay(dirs: list[Path]) -> dict:
    from velaris_shared.db.models import HelixUserModel
    from velaris_shared.db.session import get_session_factory
    from velaris_shared.identity import ledger, lifecycle

    records, bad = ledger.load(dirs)
    replayed, gone, failed = [], 0, []
    async with get_session_factory()() as session:
        for account_id in sorted(records):
            user = await session.get(HelixUserModel, uuid.UUID(account_id))
            if user is None:
                gone += 1
                continue
            try:
                await lifecycle.purge_user(session, user, actor="restore:deletion-ledger")
                await session.commit()
                replayed.append(account_id)
            except Exception as exc:  # noqa: BLE001
                await session.rollback()
                failed.append(f"{account_id}: {exc}"[:300])
        await ledger.flush(session)
    return {"records": len(records), "replayed": replayed, "already_gone": gone,
            "refused_records": bad, "failed": failed}


async def _flush() -> dict:
    from velaris_shared.db.session import get_session_factory
    from velaris_shared.identity import ledger
    async with get_session_factory()() as session:
        written, failed = await ledger.flush(session)
    return {"written": written, "failed": failed}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ledger_cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("replay")
    r.add_argument("--dir", action="append", default=[], help="a folder of ledger records")
    sub.add_parser("flush")
    a = ap.parse_args(argv)
    out = asyncio.run(_replay([Path(d) for d in a.dir]) if a.cmd == "replay" else _flush())
    print(json.dumps(out), flush=True)
    return 1 if out.get("failed") else 0


if __name__ == "__main__":
    sys.exit(main())
