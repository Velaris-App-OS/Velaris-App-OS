"""VELARIS_DB_QUERY_STATS (2026-10-23): pg_stat_statements for HxDBManager.

Unset = untouched (no database contact at all); setup turns it on for a NEW
install only; start and update apply it before the services connect.
The live behaviour (on / off / unset on a real bundled database): Nov_TODO.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
import os
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[4]
SCRIPT = ROOT / "scripts" / "db-query-stats.sh"


def _run(tmp_path, env_text, *args):
    """The script from a copy of the install with a stub `docker` that records
    every call — so "nothing touched" is checked, not assumed."""
    inst = tmp_path / "inst"
    (inst / "scripts").mkdir(parents=True)
    (inst / "scripts" / "db-query-stats.sh").write_text(SCRIPT.read_text())
    (inst / ".env").write_text(env_text)
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    log = tmp_path / "docker.log"
    (bin_ / "docker").write_text(f'#!/bin/sh\necho "$@" >> {log}\necho true\n')
    (bin_ / "docker").chmod(0o755)
    r = subprocess.run(["bash", str(inst / "scripts" / "db-query-stats.sh"), *args],
                       env={**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}"},
                       capture_output=True, text=True, timeout=30)
    return r, (log.read_text() if log.exists() else "")


def test_unset_or_empty_touches_nothing(tmp_path):
    for text in ("", "VELARIS_DB_QUERY_STATS=\n", "OTHER=1\n"):
        r, calls = _run(tmp_path / str(abs(hash(text))), text)
        assert r.returncode == 0 and calls == "", (text, r.stderr, calls)


def test_a_wrong_value_is_refused_by_name(tmp_path):
    r, calls = _run(tmp_path, "VELARIS_DB_QUERY_STATS=maybe\n")
    assert r.returncode == 1 and "VELARIS_DB_QUERY_STATS=maybe" in r.stderr and calls == ""


def test_mysql_is_skipped(tmp_path):
    r, calls = _run(tmp_path, "VELARIS_DB_QUERY_STATS=true\nVELARIS_CASE_DATABASE_BACKEND=mysql\n")
    assert r.returncode == 0 and "performance_schema" in r.stdout and calls == ""


def test_setup_turns_it_on_for_a_new_install_and_update_never_does():
    setup = (ROOT / "setup-velaris.sh").read_text()
    assert 'grep -q "^VELARIS_DB_QUERY_STATS=" "$ENV_FILE"' in setup
    assert 'echo "VELARIS_DB_QUERY_STATS=true" >> "$ENV_FILE"' in setup
    for f in ("update-velaris.sh", "start-velaris.sh"):
        text = (ROOT / f).read_text()
        assert "VELARIS_DB_QUERY_STATS=true" not in text, f
        assert "scripts/db-query-stats.sh" in text, f
    # Applied after the database is ready, before migrations / services.
    upd = (ROOT / "update-velaris.sh").read_text()
    assert upd.index("scripts/db-query-stats.sh") < upd.index("velaris_psql -q <<'SQL'\nCREATE TABLE IF NOT EXISTS schema_migrations")
