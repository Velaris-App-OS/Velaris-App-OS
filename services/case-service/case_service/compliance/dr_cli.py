"""HxDR — record a backup from ops tooling.

`update-velaris.sh` takes a `pg_dump` before every update and aborts if it
fails. It is the one place on the platform that reliably knows a good backup
exists — and until now it told nothing, so `GET /dr/posture` reported
`no_backup` for every region while backups were in fact being taken. A DR
posture that under-reports is not merely useless, it invites someone to go
looking for a problem that does not exist.

**Why a CLI and not an HTTP call.** No ops script on the platform authenticates
to the API — there is no machine token to hand one, and inventing an admin
credential that lives in a shell script to record a backup would be a worse
trade than the thing it fixes. A local process already holds the database
credentials it needs. It also keeps working when the API does not, which is
exactly the situation a DR record is most worth having.

**Why not `psql -c "INSERT …"`.** That would duplicate the schema into a shell
script and skip `record_backup()` — losing the sovereignty check, the status
validation and the location check. Going through the service means the ops path
and the API path cannot drift.

**It never fails the caller.** Exit status is 0 even when recording fails: a
backup that succeeded must not be reported as failed because a bookkeeping row
did not land. Problems are printed for a human, and `--strict` is available for
anyone who wants the opposite.

Usage::

    python -m case_service.compliance.dr_cli \\
        --location /opt/velaris/.backups/velaris-v2.2.0-20260804.sql \\
        --scope database --status succeeded

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
import uuid
from pathlib import Path

from sqlalchemy import select

from case_service.compliance import dr
from velaris_shared.db.models import RegionModel


def _err(msg: str) -> None:
    print(f"  ! dr: {msg}", file=sys.stderr)


async def _resolve_region(session, explicit: str | None) -> tuple[uuid.UUID | None, str]:
    """Decide which region this backup belongs to.

    A backup with no region is **invisible to `posture()`** — it reports per
    region, so an unattached row is recorded and then never shown. That failure
    is silent, so the resolution order is explicit and the last case says so out
    loud rather than appearing to have worked.

    **`region_registry` outranks the environment.** HxGlobal is where an
    operator declares which region is authoritative, and it is what the UI
    shows. An env var that silently overrode it would put two answers to
    "which region is this" in the system with no way to tell from the UI which
    one won — the same split-brain the tenant-identity work exists to prevent.
    So the env var sits *below* `is_primary`: an escape hatch for a deployment
    that cannot mark one, not configuration.

    `--region` still wins outright, because that is a human typing an explicit
    instruction at the moment of the run.
    """
    if explicit:
        try:
            return uuid.UUID(explicit), "explicit"
        except ValueError:
            _err(f"--region {explicit!r} is not a UUID; falling back to discovery")

    regions = (await session.execute(select(RegionModel))).scalars().all()

    primary = [r for r in regions if r.is_primary]
    if len(primary) == 1:
        return primary[0].id, f"primary region {primary[0].name!r}"
    if len(primary) > 1:
        # Migration 152 makes this unreachable through the API. Kept as a
        # guard against direct database writes, which the index also blocks —
        # belt and braces on a question that must not be guessed.
        _err(f"{len(primary)} regions are marked primary — cannot choose between them")

    # Below HxGlobal, deliberately. See the docstring.
    env = os.getenv("VELARIS_DR_REGION_ID")
    if env:
        try:
            return uuid.UUID(env), "VELARIS_DR_REGION_ID (no primary region set)"
        except ValueError:
            _err(f"VELARIS_DR_REGION_ID={env!r} is not a UUID; ignoring")

    if len(regions) == 1:
        return regions[0].id, f"the only region, {regions[0].name!r}"
    if not regions:
        _err("no regions are registered, so posture has nothing to report against")
    else:
        _err(
            f"{len(regions)} regions registered and none is primary — "
            f"mark one primary in HxGlobal (preferred), or set VELARIS_DR_REGION_ID"
        )
    return None, "none"


def _checksum(path: Path) -> str | None:
    """SHA-256 of the dump, read in chunks.

    A dump is routinely larger than memory, so it is never read whole. Returns
    None rather than raising: a checksum is evidence about the backup, not a
    precondition for recording that it happened.
    """
    try:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError as exc:
        _err(f"could not checksum {path}: {exc}")
        return None


async def _run(args: argparse.Namespace) -> int:
    from velaris_shared.db.session import get_session_factory

    path = Path(args.location)
    size = checksum = None
    if args.status == dr.SUCCEEDED:
        try:
            size = path.stat().st_size
        except OSError as exc:
            _err(f"cannot stat {path}: {exc}")
        if size and not args.no_checksum:
            checksum = _checksum(path)

    factory = get_session_factory()
    async with factory() as session:
        region_id, how = await _resolve_region(session, args.region)

        try:
            row = await dr.record_backup(
                session,
                location=str(path),
                region_id=region_id,
                stored_region_id=region_id,
                scope=args.scope,
                status=args.status,
                size_bytes=size,
                checksum=checksum,
                encrypted=args.encrypted,
                error=args.error,
                recorded_by=args.recorded_by,
            )
        except dr.DRError as exc:
            _err(f"refused: {exc}")
            return 1
        # A restore test run as part of taking this backup (backup-velaris.sh
        # restores its own fresh dump into a throwaway database before
        # encrypting it): recorded on the same row, with its measured duration.
        if args.restore_test and args.status == dr.SUCCEEDED:
            try:
                await session.flush()
                await dr.record_restore_test(
                    session, row.id, result=args.restore_test,
                    notes=args.restore_test_notes, actor_id=args.recorded_by,
                    duration_seconds=args.restore_test_seconds,
                )
            except dr.DRError as exc:
                _err(f"restore test not recorded: {exc}")
        await session.commit()

    mb = f"{size / 1_048_576:.1f} MB" if size else "size unknown"
    print(f"  ✓ DR: backup recorded ({mb}, region: {how})")
    if region_id is None:
        print("  ! DR: recorded WITHOUT a region — /dr/posture will not show it")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="dr_cli", description="Record a backup in the HxDR registry."
    )
    p.add_argument("--location", required=True, help="Path or URI of the backup")
    p.add_argument("--scope", default="database", help="What was backed up")
    p.add_argument("--status", default=dr.SUCCEEDED, choices=[dr.SUCCEEDED, dr.FAILED])
    p.add_argument("--region", help="Region UUID. Otherwise: the primary region in "
                                   "HxGlobal, then VELARIS_DR_REGION_ID, then the sole region")
    p.add_argument("--encrypted", action="store_true", help="The backup is encrypted at rest")
    p.add_argument("--error", help="Failure detail, when --status failed")
    p.add_argument("--recorded-by", default="update-velaris.sh")
    p.add_argument("--no-checksum", action="store_true", help="Skip hashing a large dump")
    p.add_argument("--restore-test", choices=[dr.PASSED, dr.FAILED_TEST],
                   help="Result of restoring this backup into a throwaway database")
    p.add_argument("--restore-test-seconds", type=int, help="How long that restore took")
    p.add_argument("--restore-test-notes", help="What the restore test checked or found")
    p.add_argument(
        "--strict", action="store_true",
        help="Exit non-zero if recording fails (default: never fail the caller)",
    )
    args = p.parse_args(argv)

    try:
        rc = asyncio.run(_run(args))
    except Exception as exc:                       # noqa: BLE001 — see module docstring
        _err(f"{type(exc).__name__}: {exc}")
        rc = 1
    return rc if args.strict else 0


if __name__ == "__main__":                          # pragma: no cover
    raise SystemExit(main())
