"""HxDR — disaster recovery posture (§8.9).

Per-region, sovereignty-aware backup registry with RPO/RTO reporting and
recorded restore drills.

**A registry, not an executor.** Taking a dump belongs to the ops tooling that
already holds the credentials and can block for minutes — `update-velaris.sh`
does exactly that before every update and aborts if it fails. A request path
must not shell out to `pg_dump`. What was missing was the *record*: without one,
"when did we last have a good backup" and "does that meet our RPO" cannot be
answered, which is the whole of a DR posture.

**An untested backup is a hope.** `restore_tested_at` is treated as a distinct
state from "recent" — a fresh backup nobody has ever restored is reported as
untested, never as healthy. Reporting it green is precisely the failure DR
exists to prevent.

**Sovereignty follows the bytes.** A backup of an EU region parked in another
jurisdiction is a residency violation even though the primary data never moved,
so the stored location is checked against the source region's rules and a
violation is refused at record time rather than discovered at audit.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import BackupRecordModel, RegionModel

logger = logging.getLogger(__name__)

SUCCEEDED = "succeeded"
FAILED = "failed"

PASSED = "passed"
FAILED_TEST = "failed"

#: Default objectives. Recovery *point* is how much data a failure may lose;
#: recovery *time* is how long restoring may take. Both are declared rather
#: than inferred — a system cannot discover what an organisation promised.
DEFAULT_RPO_HOURS = 24
DEFAULT_RTO_HOURS = 4

#: A restore drill older than this no longer demonstrates anything: schema,
#: volume and tooling all drift.
RESTORE_TEST_VALID_DAYS = 90


class DRError(ValueError):
    """The record would misstate the recovery posture — refused."""


async def _region_regulations(session: AsyncSession, region_id) -> set[str]:
    from velaris_shared.db.models import SovereigntyRuleModel

    rows = (await session.execute(
        select(SovereigntyRuleModel.regulation).where(
            SovereigntyRuleModel.region_id == region_id
        )
    )).all()
    return {r[0] for r in rows if r[0]}


async def record_backup(
    session: AsyncSession,
    *,
    location: str,
    region_id: uuid.UUID | None = None,
    stored_region_id: uuid.UUID | None = None,
    scope: str = "database",
    status: str = SUCCEEDED,
    size_bytes: int | None = None,
    checksum: str | None = None,
    encrypted: bool = False,
    error: str | None = None,
    tenant_id: str | None = None,
    recorded_by: str | None = None,
    taken_at: datetime | None = None,
) -> BackupRecordModel:
    """Record a backup that ops has taken.

    Refuses a sovereignty violation: if the source region carries residency
    rules and the backup was stored in a different region, the record is
    rejected rather than stored as compliant. Catching it here is the only
    point at which it is cheap to fix.
    """
    if not (location or "").strip():
        raise DRError("location is required — a backup nobody can find is not a backup")
    if status not in (SUCCEEDED, FAILED):
        raise DRError(f"status must be {SUCCEEDED} or {FAILED}")

    if region_id and stored_region_id and region_id != stored_region_id:
        regs = await _region_regulations(session, region_id)
        if regs:
            raise DRError(
                f"refusing to record a backup of a region governed by "
                f"{', '.join(sorted(regs))} stored in a different region — "
                f"residency follows the bytes, and a backup is data"
            )

    row = BackupRecordModel(
        region_id=region_id, stored_region_id=stored_region_id or region_id,
        scope=scope, status=status, location=location.strip(),
        size_bytes=size_bytes, checksum=checksum, encrypted=encrypted,
        error=error, tenant_id=tenant_id, recorded_by=recorded_by,
        taken_at=taken_at or datetime.now(timezone.utc),
    )
    session.add(row)
    await session.flush()
    return row


async def record_restore_test(
    session: AsyncSession, backup_id: uuid.UUID, *,
    result: str, notes: str | None = None, actor_id: str | None = None,
    duration_seconds: int | None = None,
) -> BackupRecordModel:
    """Record the outcome of actually restoring a backup.

    A failed drill is recorded, not discarded — knowing a backup does **not**
    restore is more valuable than any number of untested ones.

    `duration_seconds` is how long the drill actually took, and it is what turns
    a *declared* RTO into a measured one. Optional, because a drill run before
    the column existed genuinely has no measurement — and a fabricated number
    would be worse than the gap, since the posture would report an objective as
    met on the strength of it. Unmeasured is its own reported state.
    """
    if result not in (PASSED, FAILED_TEST):
        raise DRError(f"result must be {PASSED} or {FAILED_TEST}")

    row = await session.get(BackupRecordModel, backup_id)
    if row is None:
        raise DRError(f"Backup {backup_id} not found")
    if row.status != SUCCEEDED:
        raise DRError("cannot restore-test a backup that did not succeed")

    if duration_seconds is not None:
        seconds = int(duration_seconds)
        if seconds < 0:
            raise DRError("restore duration cannot be negative")
        row.restore_test_duration_seconds = seconds

    row.restore_tested_at = datetime.now(timezone.utc)
    row.restore_test_result = result
    row.restore_test_notes = notes
    row.recorded_by = actor_id or row.recorded_by
    return row


async def posture(
    session: AsyncSession, *, rpo_hours: int = DEFAULT_RPO_HOURS,
    rto_hours: int = DEFAULT_RTO_HOURS,
) -> dict[str, Any]:
    """Per-region recovery posture against the declared objectives.

    Four distinct states, deliberately not collapsed into a boolean:

    * `no_backup`    — nothing recorded at all;
    * `rpo_breached` — the newest good backup is older than the objective;
    * `untested`     — recent, but never proven restorable, or the last drill
      is stale/failed;
    * `ok`           — recent and demonstrably restorable.

    Collapsing `untested` into `ok` is how organisations discover at the worst
    possible moment that their backups never worked.
    """
    now = datetime.now(timezone.utc)
    rpo_cutoff = now - timedelta(hours=rpo_hours)
    test_cutoff = now - timedelta(days=RESTORE_TEST_VALID_DAYS)

    regions = (await session.execute(select(RegionModel))).scalars().all()
    out: list[dict[str, Any]] = []

    for region in regions:
        latest = (await session.execute(
            select(BackupRecordModel)
            .where(
                BackupRecordModel.region_id == region.id,
                BackupRecordModel.status == SUCCEEDED,
            )
            .order_by(BackupRecordModel.taken_at.desc())
            .limit(1)
        )).scalar_one_or_none()

        if latest is None:
            state, detail = "no_backup", "no successful backup recorded"
        elif _aware(latest.taken_at) < rpo_cutoff:
            age_h = round((now - _aware(latest.taken_at)).total_seconds() / 3600, 1)
            state, detail = "rpo_breached", f"newest backup is {age_h}h old, RPO is {rpo_hours}h"
        elif latest.restore_tested_at is None:
            state, detail = "untested", "never restore-tested — an untested backup is a hope"
        elif latest.restore_test_result != PASSED:
            state, detail = "untested", "the last restore drill FAILED"
        elif _aware(latest.restore_tested_at) < test_cutoff:
            state, detail = "untested", (
                f"last restore drill is older than {RESTORE_TEST_VALID_DAYS} days — "
                f"schema and tooling drift, so it no longer demonstrates anything"
            )
        elif (
            latest.restore_test_duration_seconds is not None
            and latest.restore_test_duration_seconds > rto_hours * 3600
        ):
            # A restore that works but takes longer than the objective is not
            # "ok". This is the only state the posture could not previously
            # express: the drill passed, so everything upstream reported green,
            # while the organisation would in fact miss the recovery time it
            # committed to. Distinct from `untested` — the backup IS restorable,
            # which is why the wording says so.
            took_h = round(latest.restore_test_duration_seconds / 3600, 1)
            state, detail = "rto_breached", (
                f"restorable, but the last drill took {took_h}h against an RTO "
                f"of {rto_hours}h"
            )
        else:
            state, detail = "ok", "recent and demonstrably restorable"

        out.append({
            "region": region.name,
            "region_id": str(region.id),
            "is_primary": region.is_primary,
            "state": state,
            "detail": detail,
            "last_backup_at": latest.taken_at.isoformat() if latest else None,
            "last_restore_test_at": (
                latest.restore_tested_at.isoformat()
                if latest and latest.restore_tested_at else None
            ),
            # NULL is reported as null, never as 0 or as the objective. A drill
            # predating the measurement column has no duration, and the report
            # must not imply the RTO was met on the strength of nothing.
            "last_restore_seconds": (
                latest.restore_test_duration_seconds if latest else None
            ),
            "rto_measured": bool(
                latest is not None
                and latest.restore_test_duration_seconds is not None
            ),
        })

    return {
        "rpo_hours": rpo_hours,
        "rto_hours": rto_hours,
        "restore_test_valid_days": RESTORE_TEST_VALID_DAYS,
        "regions": out,
        # A single green light would hide a broken region behind healthy ones.
        "all_regions_ok": bool(out) and all(r["state"] == "ok" for r in out),
        "regions_at_risk": [r["region"] for r in out if r["state"] != "ok"],
    }


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ── Standby link events (R5c step 11 d, mig 222) ─────────────────────────────

# serve_reads_*: the main reads from the standby's copy (R5d 11b part c).
STANDBY_EVENTS = ("link", "unlink", "promote", "serve_reads_on", "serve_reads_off",
                  # Step 12 — automatic failover (additive; the column is free text).
                  "failover_enable", "failover_disable", "failover_pause", "failover_resume",
                  "auto_promote")


async def record_standby_event(session: AsyncSession, *, event: str, peer: str | None = None,
                               detail: dict | None = None,
                               recorded_by: str = "standby-velaris.sh"):
    """One standby event, written by the host tool through standby_cli."""
    import json

    from velaris_shared.db.models import StandbyEventModel
    if event not in STANDBY_EVENTS:
        raise DRError(f"event must be one of {', '.join(STANDBY_EVENTS)}")
    row = StandbyEventModel(event=event, peer=(peer or None) and peer[:255],
                            detail=json.dumps(detail or {}, default=str)[:20000],
                            recorded_by=recorded_by[:64])
    session.add(row)
    await session.flush()
    return row


async def recent_standby_events(session: AsyncSession, limit: int = 10) -> list[dict]:
    import json

    from velaris_shared.db.models import StandbyEventModel
    rows = (await session.execute(
        select(StandbyEventModel).order_by(StandbyEventModel.occurred_at.desc()).limit(limit)
    )).scalars().all()
    out = []
    for r in rows:
        try:
            detail = json.loads(r.detail or "{}")
        except ValueError:
            detail = {}
        out.append({"event": r.event, "peer": r.peer, "occurred_at": r.occurred_at.isoformat(),
                    "detail": detail})
    return out
