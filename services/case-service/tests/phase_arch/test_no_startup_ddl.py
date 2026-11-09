"""R6 soak S9 (2026-11-03): no DDL at case-service start-up; analytics reads
can carry a statement timeout.

The start-up "Migration 042/043/044" blocks ran `ALTER TABLE … ADD COLUMN IF
NOT EXISTS` on every start. PostgreSQL takes the exclusive lock before the
check, so behind long queries it stalled start-up and every read of the table.
They now live in migrations/postgresql/229.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[4]
_MAIN = _ROOT / "services" / "case-service" / "case_service" / "main.py"
_M229 = _ROOT / "migrations" / "postgresql" / "229_startup_ddl_to_migration.sql"

_DDL = re.compile(r"\b(ALTER\s+TABLE|CREATE\s+TABLE|CREATE\s+(UNIQUE\s+)?INDEX)\b", re.I)


def test_case_service_main_runs_no_ddl():
    assert not _DDL.search(_MAIN.read_text()), \
        "DDL belongs in a migration, never in case-service start-up (S9)"


@pytest.mark.parametrize("name", [
    "owner_id", "assigned_reviewer_id", "access_group_id",
    "intake_trigger", "trigger_connector_id", "filter_conditions",
    "field_mapping", "process_definition_id",
    "branch_audit_events", "ix_bae_branch", "ix_bae_created",
    "intake_events", "ix_intake_case_type", "ix_intake_status",
    "ix_intake_received", "ix_intake_case",
])
def test_migration_229_defines_every_object(name):
    assert name in _M229.read_text()


def test_migration_229_takes_no_unconditional_lock():
    """Every ALTER / CREATE sits behind a catalogue check (inside the DO block),
    so an install that already has the objects takes no lock at all."""
    sql = _M229.read_text()
    body = sql[sql.index("DO $$"):]
    assert "IF NOT EXISTS" not in body.replace("NOT EXISTS (", "")


class _Conn:
    def __init__(self):
        self.sql: list[str] = []

    def exec_driver_sql(self, s):
        self.sql.append(s)


class _Bind:
    class dialect:  # noqa: N801
        name = "postgresql"


class _Sess:
    def __init__(self):
        self.bind = _Bind()
        self.info: dict = {}
        self.sync_session = object()


def _hook(monkeypatch, timeout_ms):
    from velaris_shared.db import read_routing as rr
    captured = {}
    monkeypatch.setattr(rr.event, "listen", lambda _t, _n, fn: captured.setdefault("fn", fn))
    monkeypatch.setattr(rr, "_postgres", lambda: True)
    s = _Sess()
    rr._read_only(s, timeout_ms)
    c = _Conn()
    captured["fn"](s, None, c)
    return c.sql


def test_read_only_without_timeout_is_unchanged(monkeypatch):
    assert _hook(monkeypatch, 0) == ["SET TRANSACTION READ ONLY"]


def test_read_only_with_timeout_sets_it_per_transaction(monkeypatch):
    assert _hook(monkeypatch, 60000) == [
        "SET TRANSACTION READ ONLY", "SET LOCAL statement_timeout = 60000"]


def test_timeout_setting_defaults_off():
    from velaris_shared.config import Settings
    assert Settings.model_fields["db_analytics_read_timeout_ms"].default == 0
