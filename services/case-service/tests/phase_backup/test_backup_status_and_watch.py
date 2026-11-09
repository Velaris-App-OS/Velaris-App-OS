"""HxDR backup status, the watcher's notification rules, and recording a
restore test from the CLI (R5a step c).

The status reader must never report an unknown host as fine; the watcher must
push a high finding once a day — and at once when it changes — and forget it
when it clears; a restore test recorded by the backup run must land on that
backup's row.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from case_service.compliance import dr_status
from case_service.compliance.dr_watch import due_notifications

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


def _status(tmp_path, **fields):
    b = tmp_path / ".backups"
    b.mkdir(exist_ok=True)
    (b / "status.json").write_text(json.dumps(fields))


def _codes(res):
    return {f["code"] for f in res["findings"]}


def test_no_status_file_is_reported_not_assumed_fine(tmp_path):
    res = dr_status.backup_status(tmp_path, NOW)
    assert res["available"] is False
    assert "no_backup_status" in _codes(res)


def test_a_healthy_host_has_no_findings(tmp_path):
    t = (NOW - timedelta(hours=10)).isoformat()
    _status(tmp_path, last_run_at=t, last_good_at=t, last_outcome="ok", last_encrypted=True,
            key_configured=True, offbox_configured=True, timer_installed=True,
            last_proven_restore_at=t, last_offbox={"s3": True})
    res = dr_status.backup_status(tmp_path, NOW)
    assert res["findings"] == []
    assert res["facts"]["encrypted"] is True


@pytest.mark.parametrize("outcome,code", [
    ("failed", "backup_failed"), ("partial", "backup_partial"),
    ("test_failed", "restore_test_failed"), ("no_offbox_copy", "offbox_copy_failed"),
    ("unencrypted_database_only", "backup_unencrypted"),
])
def test_each_bad_outcome_is_a_high_finding(tmp_path, outcome, code):
    t = (NOW - timedelta(hours=1)).isoformat()
    _status(tmp_path, last_run_at=t, last_good_at=t, last_outcome=outcome,
            offbox_configured=True, timer_installed=True, last_proven_restore_at=t)
    res = dr_status.backup_status(tmp_path, NOW)
    f = [x for x in res["findings"] if x["code"] == code]
    assert f and f[0]["severity"] == "high"


def test_no_complete_backup_in_26_hours_is_stale(tmp_path):
    t = (NOW - timedelta(hours=30)).isoformat()
    _status(tmp_path, last_run_at=t, last_good_at=t, last_outcome="ok",
            offbox_configured=True, timer_installed=True, last_proven_restore_at=t)
    assert "backup_stale" in _codes(dr_status.backup_status(tmp_path, NOW))


def test_private_key_left_on_the_server_after_a_day(tmp_path):
    import os
    r = tmp_path / "velaris-install-report.md"
    r.write_text(dr_status.KEY_MARKER + "\nAGE-SECRET-KEY-…\n")
    old = (NOW - timedelta(hours=30)).timestamp()
    os.utime(r, (old, old))
    assert "private_key_on_server" in _codes(dr_status.backup_status(tmp_path, NOW))
    fresh = (NOW - timedelta(hours=2)).timestamp()
    os.utime(r, (fresh, fresh))
    assert "private_key_on_server" not in _codes(dr_status.backup_status(tmp_path, NOW))


def test_watcher_notifies_once_a_day_and_again_on_change():
    f = [{"code": "backup_stale", "severity": "high", "message": "stale A"},
         {"code": "no_backup_timer", "severity": "medium", "message": "no timer"}]
    due, st = due_notifications(f, {}, NOW)
    assert [x["code"] for x in due] == ["backup_stale"], "medium findings are not pushed"
    due, st = due_notifications(f, st, NOW + timedelta(hours=1))
    assert due == [], "not again within the day"
    f2 = [{"code": "backup_stale", "severity": "high", "message": "stale B"}]
    due, st = due_notifications(f2, st, NOW + timedelta(hours=2))
    assert len(due) == 1, "a changed message notifies at once"
    due, st = due_notifications(f2, st, NOW + timedelta(hours=27))
    assert len(due) == 1, "and again after a day"


def test_watcher_forgets_a_cleared_finding():
    f = [{"code": "backup_failed", "severity": "high", "message": "x"}]
    _, st = due_notifications(f, {}, NOW)
    _, st = due_notifications([], st, NOW + timedelta(hours=1))
    assert st == {"sent": {}}
    due, _ = due_notifications(f, st, NOW + timedelta(hours=2))
    assert len(due) == 1, "a recurrence notifies again"


@pytest.mark.asyncio
async def test_cli_records_the_restore_test_on_the_same_backup(session, tmp_path, monkeypatch):
    """backup-velaris.sh passes --restore-test; it must land on the new row."""
    from sqlalchemy import select
    from velaris_shared.db.models import BackupRecordModel, RegionModel
    from case_service.compliance import dr_cli

    session.add(RegionModel(name="t-region", is_primary=True))
    await session.commit()
    f = tmp_path / "set.tar.age"
    f.write_bytes(b"x" * 100)
    import argparse
    rc = await dr_cli._run(argparse.Namespace(
        location=str(f), scope="full", status="succeeded", region=None, encrypted=True,
        error=None, recorded_by="backup-velaris.sh", no_checksum=False,
        restore_test="passed", restore_test_seconds=42, restore_test_notes="row_counts equal"))
    assert rc == 0
    row = (await session.execute(select(BackupRecordModel).where(
        BackupRecordModel.location == str(f)))).scalars().one()
    assert row.restore_test_result == "passed"
    assert row.restore_test_duration_seconds == 42
    assert row.restore_tested_at is not None
