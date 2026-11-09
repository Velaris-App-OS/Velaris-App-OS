"""HxDR — record a standby link event from the host tool (R5c step 11 d).

    python -m case_service.compliance.standby_cli --event link --peer standby.example \
        --detail '{"copy_seconds": 13.5}'

Same contract as dr_cli: a local process with the database credentials, going
through the service function (not raw SQL), and it **never fails the caller** —
a link that worked is not undone because a bookkeeping row did not land.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from case_service.compliance import dr


async def _run(args: argparse.Namespace) -> int:
    from velaris_shared.db.session import get_session_factory

    try:
        detail = json.loads(args.detail or "{}")
    except ValueError:
        detail = {"raw": args.detail[:500]}
    factory = get_session_factory()
    async with factory() as session:
        await dr.record_standby_event(session, event=args.event, peer=args.peer, detail=detail)
        await session.commit()
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--event", required=True, choices=dr.STANDBY_EVENTS)
    p.add_argument("--peer")
    p.add_argument("--detail", default="{}", help="JSON")
    p.add_argument("--strict", action="store_true", help="exit non-zero when recording fails")
    args = p.parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except Exception as exc:  # noqa: BLE001
        print(f"  ! dr: standby event not recorded: {exc}", file=sys.stderr)
        return 1 if args.strict else 0


if __name__ == "__main__":
    sys.exit(main())
