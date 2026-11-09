"""Backup engine — the parts that would be silently wrong (Nov_TODO R5a).

Retention must keep exactly the grandfather-father-son sets and touch nothing
it did not name; the private key must land only in the install report, be
removable, and never reach .env; a bad recipient must be refused.

The engine lives in scripts/ (a host tool, not a service), so it is loaded by
path. Live behaviour — dumps, encryption, off-box copies — is proven on a real
install, not here.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import types
from pathlib import Path

import pytest

_ENGINE = Path(__file__).resolve().parents[4] / "scripts" / "backup" / "velaris_backup.py"


@pytest.fixture
def eng(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("velaris_backup_under_test", _ENGINE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "ROOT", tmp_path)
    monkeypatch.setattr(mod, "REPORT", tmp_path / "velaris-install-report.md")
    (tmp_path / ".env").write_text("A=1\n")
    return mod


def _name(d: dt.datetime) -> str:
    return f"velaris-host-{d.strftime('%Y%m%dT%H%M%SZ')}-v3.2.0.tar.age"


def test_set_names_round_trip(eng):
    n = _name(dt.datetime(2026, 10, 16, 2, 0, 0))
    m = eng.SET_RE.match(n)
    assert m and m.group("stamp") == "20261016T020000Z" and m.group("ver") == "3.2.0"
    assert not eng.SET_RE.match("velaris-20261016-020000.sql"), "legacy dumps are not sets"


def test_retention_keeps_daily_weekly_monthly(eng):
    start = dt.datetime(2026, 1, 1, 2, 0, 0)
    names = [_name(start + dt.timedelta(days=i)) for i in range(200)]
    # Two runs on the newest day: only the newer of the two counts for that day.
    names.append(_name(start + dt.timedelta(days=199, hours=10)))
    kept = eng._keep_set(names, (7, 4, 6))
    newest = start + dt.timedelta(days=199)
    days = {eng.SET_RE.match(n).group("stamp")[:8] for n in kept}
    for i in range(7):
        assert (newest - dt.timedelta(days=i)).strftime("%Y%m%d") in days
    months = {eng.SET_RE.match(n).group("stamp")[:6] for n in kept}
    assert len(months) == 6
    assert len(kept) <= 7 + 4 + 6
    assert _name(start + dt.timedelta(days=199, hours=10)) in kept
    assert _name(start + dt.timedelta(days=199)) not in kept


def test_prune_never_touches_files_it_did_not_name(eng, tmp_path):
    d = tmp_path / "b"
    d.mkdir()
    start = dt.datetime(2026, 1, 1, 2, 0, 0)
    for i in range(40):
        (d / _name(start + dt.timedelta(days=i))).write_text("x")
    (d / "velaris-20260101-020000.sql").write_text("legacy")
    (d / "notes.txt").write_text("mine")
    cfg = types.SimpleNamespace(local_dir=d, nas_dir=None, keep=(3, 2, 1),
                                s3={"endpoint": "", "bucket": ""})
    eng.prune(cfg)
    assert (d / "notes.txt").exists() and (d / "velaris-20260101-020000.sql").exists()
    assert len([p for p in d.iterdir() if eng.SET_RE.match(p.name)]) <= 6


def test_generated_private_key_goes_only_to_the_report(eng, tmp_path):
    rc = eng.cmd_ensure_key(types.SimpleNamespace(recipient=None, no_push=True))
    assert rc == 0
    env = (tmp_path / ".env").read_text()
    report = (tmp_path / "velaris-install-report.md").read_text()
    assert "VELARIS_BACKUP_AGE_RECIPIENT=age1" in env
    assert "AGE-SECRET-KEY-" not in env, "the private key must never reach .env"
    assert "AGE-SECRET-KEY-" in report and eng.KEY_BEGIN in report
    assert oct((tmp_path / "velaris-install-report.md").stat().st_mode & 0o777) == "0o600"
    # Idempotent: a second run keeps the key it has.
    eng.cmd_ensure_key(types.SimpleNamespace(recipient=None, no_push=True))
    assert (tmp_path / ".env").read_text().count("VELARIS_BACKUP_AGE_RECIPIENT=") == 1


def test_forget_private_key_removes_it(eng, tmp_path):
    eng.cmd_ensure_key(types.SimpleNamespace(recipient=None, no_push=True))
    eng.cmd_forget_private_key(types.SimpleNamespace())
    report = (tmp_path / "velaris-install-report.md").read_text()
    assert "AGE-SECRET-KEY-" not in report and eng.KEY_BEGIN not in report
    assert "Removed from this server" in report
    assert eng.cmd_forget_private_key(types.SimpleNamespace()) == 0  # repeat is harmless


def test_operator_recipient_writes_nothing_private(eng, tmp_path):
    from pyrage import x25519
    pub = str(x25519.Identity.generate().to_public())
    assert eng.cmd_ensure_key(types.SimpleNamespace(recipient=pub, no_push=True)) == 0
    assert f"VELARIS_BACKUP_AGE_RECIPIENT={pub}" in (tmp_path / ".env").read_text()
    assert not (tmp_path / "velaris-install-report.md").exists()


def test_bad_recipient_is_refused(eng, tmp_path):
    assert eng.cmd_ensure_key(types.SimpleNamespace(recipient="ssh-rsa AAAA", no_push=True)) == 2
    assert "VELARIS_BACKUP_AGE_RECIPIENT" not in (tmp_path / ".env").read_text()


def test_encrypted_set_decrypts_with_the_private_key_only(eng, tmp_path):
    import pyrage
    from pyrage import x25519
    ident = x25519.Identity.generate()
    src = tmp_path / "set.tar"
    src.write_bytes(b"secret backup bytes" * 1000)
    out = tmp_path / "set.tar.age"
    eng.encrypt(src, out, str(ident.to_public()))
    assert b"secret backup bytes" not in out.read_bytes()
    back = tmp_path / "back.tar"
    pyrage.decrypt_file(str(out), str(back), [ident])
    assert back.read_bytes() == src.read_bytes()
    with pytest.raises(Exception):
        pyrage.decrypt_file(str(out), str(tmp_path / "no.tar"), [x25519.Identity.generate()])


def test_a_partial_set_never_pushes_out_a_complete_one(eng):
    """Found live 2026-10-16: a partial set (vault sealed) taken the same day as
    a complete one made retention delete the COMPLETE one."""
    day = dt.datetime(2026, 10, 16, 2, 0, 0)
    good = _name(day)
    partial = _name(day + dt.timedelta(hours=1))
    kept = eng._keep_set([good, partial], (7, 4, 6), {good: True, partial: False})
    assert good in kept, "the complete set must survive"
    assert partial in kept, "the newer partial set is still the latest thing there is"
    # Once a newer complete set exists, the old partial one goes.
    newer = _name(day + dt.timedelta(days=1))
    kept = eng._keep_set([good, partial, newer], (7, 4, 6), {good: True, partial: False, newer: True})
    assert partial not in kept and good in kept and newer in kept


def test_sets_without_a_sidecar_count_as_complete(eng):
    day = dt.datetime(2026, 10, 16, 2, 0, 0)
    a, b = _name(day), _name(day + dt.timedelta(hours=1))
    assert eng._keep_set([a, b], (7, 4, 6), {}) == {b}


def test_restore_extraction_refuses_paths_out_of_the_target(eng, tmp_path):
    """A crafted set must not write outside the restore directory."""
    import io
    import tarfile
    evil = tmp_path / "evil.tar"
    with tarfile.open(evil, "w") as t:
        for name in ("../escaped.txt", "/abs/escaped.txt"):
            info = tarfile.TarInfo(name)
            info.size = 3
            t.addfile(info, io.BytesIO(b"bad"))
    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(Exception):
        eng._safe_extract(evil, dest)
    assert not (tmp_path / "escaped.txt").exists()


def test_documents_are_extracted_without_their_prefix(eng, tmp_path):
    import io
    import tarfile
    src = tmp_path / "documents.tar"
    with tarfile.open(src, "w") as t:
        info = tarfile.TarInfo("documents/cases/a.bin")
        info.size = 2
        t.addfile(info, io.BytesIO(b"ok"))
    dest = tmp_path / "store"
    dest.mkdir()
    eng._safe_extract(src, dest, strip="documents/")
    assert (dest / "cases" / "a.bin").read_bytes() == b"ok"


def test_version_ordering_for_the_newer_set_refusal(eng):
    assert eng._ver("3.2.0") > eng._ver("3.1.12")
    assert eng._ver("3.1.2") == eng._ver("3.1.2")
    assert not eng._ver("3.1.2") > eng._ver("3.2.0")


def test_key_file_reader_takes_only_secret_key_lines(eng, tmp_path):
    from pyrage import x25519
    ident = x25519.Identity.generate()
    f = tmp_path / "k.txt"
    f.write_text(f"# created: now\n# public key: {ident.to_public()}\n{ident}\n")
    assert len(eng._load_identities(str(f))) == 1
    (tmp_path / "empty.txt").write_text("# nothing here\n")
    with pytest.raises(RuntimeError):
        eng._load_identities(str(tmp_path / "empty.txt"))


def test_aiomysql_pre_ping_passes_its_argument():
    """Found 2026-10-16: with pymysql's ping defaulting to reconnect=False,
    SQLAlchemy called the aiomysql adapter's ping() with NO argument and it
    raised TypeError — on every pooled-connection reuse of a MySQL/MariaDB
    install (pool_pre_ping=True). The DB layer now tells the dialect to pass it."""
    import velaris_shared.db.backends  # noqa: F401 — applies the fix
    from sqlalchemy.dialects.mysql.aiomysql import MySQLDialect_aiomysql

    assert MySQLDialect_aiomysql._send_false_to_ping is True
    calls = []

    class _Conn:
        def ping(self, reconnect):
            calls.append(reconnect)

    MySQLDialect_aiomysql().do_ping(_Conn())
    assert calls == [False]


def test_the_restore_test_verifier_reads_the_throwaway_database_not_the_live_one(eng, monkeypatch):
    """The verifier runs from the install folder and reads its .env: with the
    pooler set it connected to the LIVE database through PgBouncer, so every
    restore test since 6a compared the manifest with the running system (clean
    box 2026-10-23: a busy box failed it; a quiet one passed it on nothing)."""
    import os
    import subprocess
    dsn = "postgresql+asyncpg://velaris:pw@127.0.0.1:59999/velaris"
    env = eng._verifier_env(dsn)
    assert env["VELARIS_CASE_DATABASE_URL"] == dsn
    assert env["VELARIS_CASE_DATABASE_POOLER_URL"] == ""
    assert env["VELARIS_CASE_DATABASE_POOLER_SOCKET"] == "false"
    # As the verifier resolves it, with the install's pooler settings around it.
    for k in ("VELARIS_CASE_DATABASE_POOLER_URL", "VELARIS_CASE_DATABASE_POOLER_SOCKET"):
        monkeypatch.setenv(k, "postgresql+asyncpg://velaris:pw@localhost:6432/velaris" if k.endswith("URL") else "true")
    out = subprocess.run(
        [os.sys.executable, "-c",
         "from velaris_shared.config import get_settings;"
         "from velaris_shared.db.backends import resolve_runtime_url;"
         "print(resolve_runtime_url(get_settings()))"],
        env={**os.environ, **env}, capture_output=True, text=True, timeout=60)
    assert out.stdout.strip().endswith("@127.0.0.1:59999/velaris"), out.stderr[-300:]


def test_a_unit_systemd_runs_counts_as_running_even_with_its_ports_free(eng, monkeypatch):
    """A killed unit is restarted by systemd seconds later: the ports alone said
    "stopped" in that gap and the restarted services wrote into the restore
    (clean box 2026-10-23)."""
    import subprocess
    states = {"velaris-worker-service": "active", "velaris-case-service": "activating"}

    def fake(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, stdout=states.get(argv[-1], "inactive") + "\n")
    monkeypatch.setattr(eng.subprocess, "run", fake)
    assert eng._active_units() == ["velaris-case-service", "velaris-worker-service"]
    states.clear()
    assert eng._active_units() == []


def test_stop_stops_the_systemd_units_before_it_kills_anything():
    root = Path(__file__).resolve().parents[4]
    text = (root / "stop-velaris.sh").read_text()
    stop = text.index("sudo systemctl stop $_units")
    assert stop < text.index('pkill -f "uvicorn helix_engine"')
    for u in ("velaris-worker-service", "velaris-user-service", "velaris-studio"):
        assert u in text[:stop]
    runbook = (root / "docs/recovery-runbook.md").read_text()
    assert "velaris-worker-service velaris-studio" in runbook


def test_the_restore_checks_run_on_the_database_not_through_the_pooler(eng, monkeypatch):
    """A restore starts only the databases; through the pooler every table read
    "missing" (clean box 2026-10-23)."""
    import subprocess
    seen = []
    monkeypatch.setattr(eng.subprocess, "run", lambda argv, **kw: seen.append(kw["env"]) or
                        subprocess.CompletedProcess(argv, 0, stdout=b'{"ok": true, "checks": []}\n', stderr=b""))
    eng._run_verifier(Path("/nonexistent.json"), [])
    assert seen[-1]["VELARIS_CASE_DATABASE_POOLER_URL"] == ""
    assert seen[-1]["VELARIS_CASE_DATABASE_POOLER_SOCKET"] == "false"


def test_the_restore_restarts_the_pooler_after_it_sets_the_roles_password():
    """A new password = a new SCRAM verifier; PgBouncer kept the old one and
    every server login failed after a restore (clean box 2026-10-23)."""
    src = (Path(__file__).resolve().parents[4] / "scripts/backup/velaris_backup.py").read_text()
    alter = src.index("ALTER ROLE velaris PASSWORD")
    block = src[alter:alter + 900]
    assert 'container_of("velaris-pooler")' in block and '"docker", "restart", pcid' in block


def test_no_systemd_on_the_box_means_no_units_and_no_crash(eng, monkeypatch):
    monkeypatch.setattr(eng.shutil, "which", lambda name: None)
    assert eng._active_units() == []


def test_a_restore_recreates_the_containers_from_the_restored_settings():
    """R5b drill 2026-10-30: on a new server the gateway kept that server's identity
    secret while the services read the restored one — every request 403. The
    containers are recreated from the restored .env, after the data is back."""
    src = _ENGINE.read_text()
    restore = src.split("def cmd_restore")[1].split("\ndef ")[0]
    assert restore.index("_restore_documents(") < restore.index("_recreate_containers(cfg)") \
        < restore.index("Restore finished")
    helper = src.split("def _recreate_containers")[1].split("\ndef ")[0]
    assert "compose-profiles.sh" in helper and "velaris_compose_up_services" in helper
    assert '"--env-file"' not in helper and '--env-file "$2" up -d' in helper


def test_the_restore_test_waits_for_the_real_postgres_not_the_init_server():
    """R5b re-run 2026-10-30: the readiness probe went over the socket, which the
    init-time server also answers; it then restarted and the next psql failed, so
    a good set was reported "did NOT pass its restore test". Probe over TCP."""
    src = _ENGINE.read_text()
    test = src.split("throwaway database did not start")[0].rsplit("initdb restarts", 1)[1]
    assert '"psql", "-h", "127.0.0.1"' in test
