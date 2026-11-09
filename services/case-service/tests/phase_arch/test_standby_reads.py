"""R5d 11b part (c) — `standby-velaris.sh serve-reads`: the main reads from its
standby's replica through a tunnel of its own.

Off unless an operator turns it on (never an update); the standby trusts the
main's key for that one port and nothing else; `off`, `unlink`, a demote,
`retire` and `promote` take it away. Live behaviour: Nov_TODO R5d 11b (c).

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
import json
import pathlib
import subprocess

import pytest

from tests.phase_arch.test_standby_link import standby  # noqa: F401  (fixture)

ROOT = pathlib.Path(__file__).resolve().parents[4]
MAIN_URL = "postgresql+asyncpg://velaris:p%40ss:w@rd@localhost:5432/velaris?sslmode=disable"


def _env(standby, text: str) -> pathlib.Path:  # noqa: F811
    p = standby.ROOT / ".env"
    p.write_text(text)
    standby.ENV.clear()
    standby.ENV.update(standby.read_env(p))
    return p


def test_the_read_url_is_this_databases_own_role_through_the_tunnels_socket(standby):  # noqa: F811
    """A socket in the 0700 .standby folder, never a loopback port: a port is
    free for seconds at each reconnect, and whoever binds it can ask asyncpg
    for the password in clear (11b (c) audit)."""
    from urllib.parse import parse_qs
    _env(standby, f"VELARIS_CASE_DATABASE_URL={MAIN_URL}\n")
    url = standby.read_url_for(standby.READS_SOCK_DIR)
    assert url.startswith("postgresql+asyncpg://velaris:p%40ss:w@rd@/velaris?")
    q = parse_qs(url.split("?", 1)[1])
    assert q["host"] == [str(standby.READS_SOCK_DIR)] and "sslmode" not in q and "port" not in q
    assert "127.0.0.1" not in url and standby._read_url_is_ours(url)
    # Another install path (a promoted standby elsewhere) is still recognised; a replica is not.
    assert standby._read_url_is_ours("postgresql+asyncpg://u:p@/velaris?host=%2Fopt%2Fv%2F.standby%2Freads-sock")
    assert not standby._read_url_is_ours("postgresql+asyncpg://u:p@replica.example:5432/velaris")
    assert not standby._read_url_is_ours("postgresql+asyncpg://u:p@/velaris?host=/var/run/postgresql")
    _env(standby, "VELARIS_CASE_DATABASE_URL=mysql+aiomysql://u:p@h/db\n")
    with pytest.raises(standby.Refused):
        standby.read_url_for(standby.READS_SOCK_DIR)


def test_off_clears_only_the_url_serve_reads_wrote(standby):  # noqa: F811
    ours = "postgresql+asyncpg://u:p@/velaris?host=%2Fsrv%2Fv%2F.standby%2Freads-sock"
    p = _env(standby, f"A=1\nVELARIS_CASE_DATABASE_READ_URL={ours}\nB=2\n")
    assert standby.reads_url_clear(p) == []
    assert p.read_text() == "A=1\nVELARIS_CASE_DATABASE_READ_URL=\nB=2\n"     # emptied, never dropped
    theirs = "VELARIS_CASE_DATABASE_READ_URL=postgresql+asyncpg://u:p@replica.example:5432/velaris\n"
    p.write_text(theirs)
    standby.reads_url_clear(p)
    assert p.read_text() == theirs                                               # an operator's own


def test_off_never_empties_env_while_the_vault_still_holds_the_url(standby, monkeypatch):  # noqa: F811
    """Otherwise the next boot's render would quietly turn it back on."""
    text = "VELARIS_CASE_DATABASE_READ_URL=postgresql+asyncpg://u:p@/velaris?host=%2Fv%2F.standby%2Freads-sock\n"
    p = _env(standby, text)
    monkeypatch.setattr(standby, "_bao_patch", lambda values: "OpenBao is sealed")
    notes = standby.reads_url_clear(p)
    assert notes and "left as it is" in notes[0] and p.read_text() == text


def test_the_standby_trusts_the_reads_key_for_its_replica_port_only(standby, tmp_path, monkeypatch):  # noqa: F811
    (standby.STATE / "standby.json").write_text("{}")
    monkeypatch.setattr(standby, "_port_open", lambda port, timeout=1.0: True)
    monkeypatch.setattr(standby, "host_key_lines", lambda name, port: f"{name} ssh-ed25519 AAAA\n")
    res = standby.remote_reads_allow({"pubkey": "ssh-ed25519 AAAAC3Nz main", "main": "m1",
                                      "name": "s1", "ssh_port": 22})
    line = (tmp_path / ".ssh/authorized_keys").read_text().strip()
    assert line.startswith('restrict,port-forwarding,permitopen="127.0.0.1:5432",'
                           'permitlisten="127.0.0.1:5432",command="/bin/false" ssh-ed25519 AAAAC3Nz ')
    assert line.count("permitopen") == 1 and " velaris-standby-reads " in line
    assert res == {"host_keys": "s1 ssh-ed25519 AAAA\n", "db_port": "5432"}
    # Once only, and gone on revoke — the standby's own tunnel key untouched.
    (tmp_path / ".ssh/authorized_keys").write_text(line + "\nopts ssh-ed25519 BBBB velaris-standby-tunnel m\n")
    standby.remote_reads_allow({"pubkey": "ssh-ed25519 CCCC main", "main": "m1", "name": "s1"})
    assert (tmp_path / ".ssh/authorized_keys").read_text().count("velaris-standby-reads") == 1
    assert standby.remote_reads_revoke({}) == {"revoked": True}
    assert (tmp_path / ".ssh/authorized_keys").read_text() == "opts ssh-ed25519 BBBB velaris-standby-tunnel m\n"


@pytest.mark.parametrize("payload", [{"pubkey": "ssh-ed25519 AAAA\ncommand=x"}, {"pubkey": "nope"},
                                     {"pubkey": "ssh-ed25519 AAAA", "name": "-oProxyCommand=x"}])
def test_the_standby_refuses_a_bad_key_or_name(standby, monkeypatch, payload):  # noqa: F811
    (standby.STATE / "standby.json").write_text("{}")
    monkeypatch.setattr(standby, "_port_open", lambda port, timeout=1.0: True)
    with pytest.raises(standby.Refused):
        standby.remote_reads_allow(payload)


def test_only_a_linked_standby_with_a_running_replica_trusts_a_reads_key(standby, monkeypatch):  # noqa: F811
    with pytest.raises(standby.Refused, match="not a linked standby"):
        standby.remote_reads_allow({"pubkey": "ssh-ed25519 AAAA"})
    (standby.STATE / "standby.json").write_text("{}")
    monkeypatch.setattr(standby, "_port_open", lambda port, timeout=1.0: False)
    with pytest.raises(standby.Refused, match="not listening"):
        standby.remote_reads_allow({"pubkey": "ssh-ed25519 AAAA", "name": "s1"})


def test_unlinking_the_standby_revokes_the_reads_key_too(standby, tmp_path, monkeypatch):  # noqa: F811
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh/authorized_keys").write_text("o ssh-ed25519 AAAA velaris-standby-reads m\n")
    monkeypatch.setattr(standby, "stop_tunnel", lambda: None)
    monkeypatch.setattr(standby, "stop_replicas", lambda: None)
    monkeypatch.setattr(standby, "volume", lambda k: k)
    standby.remote_stop({})
    assert "velaris-standby-reads" not in (tmp_path / ".ssh/authorized_keys").read_text()


def _linked(standby, extra=""):  # noqa: F811
    standby.MAIN_RECORD.write_text(json.dumps({"standby": "s1", "ssh_target": "op@s1", "ssh_port": 22,
                                               "standby_dir": "/srv/velaris", "state": "linked"}))
    return _env(standby, f"VELARIS_CASE_DATABASE_URL={MAIN_URL}\n{extra}")


def _args(action):
    import argparse
    return argparse.Namespace(action=action)


class FakeRemote:
    calls: list = []

    def __init__(self, *a):
        pass

    def call(self, cmd, payload=None, check=True):
        FakeRemote.calls.append(cmd)
        if cmd == "remote-reads-allow":
            return {"host_keys": "s1 ssh-ed25519 AAAA\n", "db_port": "5432"}
        return {"ok": True}

    def close(self):
        pass


@pytest.fixture
def main_side(standby, monkeypatch):  # noqa: F811
    FakeRemote.calls = []
    standby.READS_UNIT.parent.mkdir(parents=True, exist_ok=True)
    standby.READS_UNIT.write_text("")
    monkeypatch.setattr(standby, "Remote", FakeRemote)
    # pytest's tmp paths are longer than an install's; the length guard has its own test.
    monkeypatch.setattr(standby, "READS_SOCK", pathlib.Path("/srv/v/.standby/reads-sock/.s.PGSQL.5432"))
    monkeypatch.setattr(standby, "_bao_patch", lambda values: None)
    killed = []
    real_run = standby.run
    monkeypatch.setattr(standby, "run", lambda cmd, **k: killed.append(cmd) if cmd[0] == "pkill"
                        else real_run(cmd, **k))
    return standby


def test_on_writes_the_url_last_after_the_replica_is_proven(main_side, monkeypatch):
    s = main_side
    env = _linked(s)
    monkeypatch.setattr(s, "_sock_up", lambda: True)
    monkeypatch.setattr(s, "_replica_answers", lambda url: "")
    order = []
    monkeypatch.setattr(s, "_bao_patch", lambda values: order.append(("vault", env.read_text())) or None)
    assert s.cmd_serve_reads(_args("on")) == 0
    assert "VELARIS_CASE_DATABASE_READ_URL=postgresql+asyncpg://velaris:p%40ss:w@rd@/velaris?host=" in env.read_text()
    assert "READ_URL" not in order[0][1]                                  # the vault before .env
    assert s.read_env(s.READS_ENV) == {"STANDBY_HOST": "s1", "STANDBY_USER": "op", "STANDBY_SSH_PORT": "22",
                                       "STANDBY_DB_PORT": "5432"}
    for f in (s.READS_ENV, s.READS_KEY, s.READS_KNOWN):
        assert oct(f.stat().st_mode & 0o777) == "0o600", f
    assert oct(s.READS_SOCK_DIR.stat().st_mode & 0o777) == "0o700"
    assert "serve_reads" in json.loads(s.MAIN_RECORD.read_text())


def test_a_replica_that_is_not_this_database_turns_nothing_on(main_side, monkeypatch):
    s = main_side
    env = _linked(s)
    before = env.read_text()
    monkeypatch.setattr(s, "_sock_up", lambda: True)
    monkeypatch.setattr(s, "_replica_answers", lambda url: "not a copy of this one")
    with pytest.raises(s.Refused, match="not a copy"):
        s.cmd_serve_reads(_args("on"))
    assert env.read_text() == before
    assert not s.READS_ENV.exists() and not s.READS_KEY.exists()
    assert "remote-reads-revoke" in FakeRemote.calls
    assert "serve_reads" not in json.loads(s.MAIN_RECORD.read_text())


@pytest.mark.parametrize("case", ["unlinked", "operator_url", "no_unit", "demoted"])
def test_on_is_refused_without_changing_anything(main_side, case):
    s = main_side
    if case != "unlinked":
        _linked(s, "VELARIS_CASE_DATABASE_READ_URL=postgresql://u:p@replica.example/velaris\n"
                if case == "operator_url" else "")
    if case == "no_unit":
        s.READS_UNIT.unlink()
    if case == "demoted":
        s.DEMOTED.write_text("{}")
    with pytest.raises(s.Refused):
        s.cmd_serve_reads(_args("on"))
    assert not s.READS_ENV.exists() and FakeRemote.calls == []


def test_off_and_unlink_put_the_reads_back_on_the_main_first(main_side, monkeypatch):
    s = main_side
    env = _linked(s, "VELARIS_CASE_DATABASE_READ_URL=postgresql+asyncpg://velaris:x@/velaris"
                     "?host=%2Fsrv%2Fv%2F.standby%2Freads-sock\n")
    rec = json.loads(s.MAIN_RECORD.read_text())
    rec["serve_reads"] = {"at": "t", "port": "5446"}
    s.MAIN_RECORD.write_text(json.dumps(rec))
    s.READS_ENV.write_text("X=1\n")
    assert s.cmd_serve_reads(_args("off")) == 0
    assert "VELARIS_CASE_DATABASE_READ_URL=\n" in env.read_text() and not s.READS_ENV.exists()
    assert FakeRemote.calls == ["remote-reads-revoke"]
    assert "serve_reads" not in json.loads(s.MAIN_RECORD.read_text())


def test_nothing_on_means_nothing_changes(standby, capsys):  # noqa: F811
    """Every existing install: no standby, no read URL — `off` and `status` touch nothing."""
    assert standby.cmd_serve_reads(_args("off")) == 0
    assert standby.cmd_serve_reads(_args("status")) == 0
    assert "off" in capsys.readouterr().out
    assert not standby.READS_ENV.exists() and not (standby.ROOT / ".env").exists()


def test_the_tunnel_unit_runs_only_while_turned_on_and_forwards_one_port():
    svc = (ROOT / "deploy/systemd/velaris-standby-reads.service").read_text()
    path = (ROOT / "deploy/systemd/velaris-standby-reads.path").read_text()
    assert "ExecCondition=/usr/bin/test -f __VELARIS_DIR__/.standby/reads-tunnel.env" in svc
    assert "PathExists=__VELARIS_DIR__/.standby/reads-tunnel.env" in path
    assert svc.count(" -L ") == 1
    assert "-L __VELARIS_DIR__/.standby/reads-sock/.s.PGSQL.5432:127.0.0.1:${STANDBY_DB_PORT}" in svc
    assert "StreamLocalBindMask=0177" in svc and "LOCAL_PORT" not in svc
    assert "StrictHostKeyChecking=yes" in svc and "BatchMode=yes" in svc and "User=__VELARIS_USER__" in svc
    # An operator's step, never setup's or an update's.
    for f in ("setup-velaris.sh", "update-velaris.sh", "start-velaris.sh", "deploy/systemd/install.sh",
              "deploy/systemd/refresh-after-update.sh"):
        assert "install-standby-reads" not in (ROOT / f).read_text(), f
        assert "serve-reads on" not in (ROOT / f).read_text(), f


def test_the_installer_refuses_without_root():
    r = subprocess.run(["bash", str(ROOT / "deploy/systemd/install-standby-reads.sh")],
                       capture_output=True, text=True)
    assert r.returncode == 1 and "sudo" in r.stderr


@pytest.mark.skipif(subprocess.run(["sh", "-c", "command -v helm"], capture_output=True).returncode != 0,
                    reason="helm not installed")
@pytest.mark.parametrize("sets,expected", [
    ([], 0),                                                                  # every install today
    (["readReplica.url=postgresql+asyncpg://v:p@ro:5432/velaris"], 5),
    (["databaseSecret.existingSecret=mine", "readReplica.fromExistingSecret=true"], 5),
    (["databaseSecret.existingSecret=mine"], 0),
])
def test_helm_renders_the_read_url_only_when_an_operator_sets_one(sets, expected):
    args = ["helm", "template", "t", str(ROOT / "deploy/helm/velaris")]
    for s_ in sets:
        args += ["--set", s_]
    out = subprocess.run(args, capture_output=True, text=True, check=True).stdout
    assert out.count("name: VELARIS_CASE_DATABASE_READ_URL") == expected
    assert ("postgresql+asyncpg://v:p@ro" in out) == bool(sets and "url=" in sets[0])
    # The password never in a Deployment — only in the Secret.
    for doc in out.split("\n---"):
        if "kind: Deployment" in doc:
            assert "v:p@ro" not in doc


def test_a_failure_after_the_url_was_written_restores_env_exactly(main_side, monkeypatch):
    """A key that was not there must not be left behind empty: an OpenBao render
    refuses over a key .env has and the vault lacks."""
    s = main_side
    env = _linked(s)
    before = env.read_text()
    monkeypatch.setattr(s, "_sock_up", lambda: True)
    monkeypatch.setattr(s, "_replica_answers", lambda url: "")
    real = s.write_private

    def fail_on_record(path, text):
        if path == s.MAIN_RECORD and "serve_reads" in text:
            raise OSError("disk full")
        real(path, text)
    monkeypatch.setattr(s, "write_private", fail_on_record)
    with pytest.raises(OSError):
        s.cmd_serve_reads(_args("on"))
    assert env.read_text() == before and not s.READS_ENV.exists()


@pytest.mark.parametrize("res", [
    {"host_keys": "@cert-authority * ssh-ed25519 AAAA\n", "db_port": "5432"},      # trust anything signed
    {"host_keys": "other.example ssh-ed25519 AAAA\n", "db_port": "5432"},          # another host's key
    {"host_keys": "s1 ssh-ed25519 AAAA\n", "db_port": "5432 -R x"},                 # into the unit's args
    {"host_keys": "", "db_port": "5432"},
])
def test_what_the_standby_sends_back_is_checked_before_use(main_side, monkeypatch, res):
    s = main_side
    _linked(s)
    monkeypatch.setattr(FakeRemote, "call", lambda self, cmd, payload=None, check=True:
                        res if cmd == "remote-reads-allow" else {"ok": True})
    with pytest.raises(s.Refused):
        s.cmd_serve_reads(_args("on"))
    assert not s.READS_KNOWN.exists() and not s.READS_ENV.exists()


def test_root_and_too_long_a_path_are_refused(main_side, monkeypatch):
    s = main_side
    _linked(s)
    monkeypatch.setattr(s.os, "geteuid", lambda: 0)
    with pytest.raises(s.Refused, match="not root"):
        s.cmd_serve_reads(_args("on"))
    monkeypatch.setattr(s.os, "geteuid", lambda: 1000)
    long_dir = s.STATE / ("x" * 90) / "reads-sock"
    monkeypatch.setattr(s, "READS_SOCK_DIR", long_dir)
    monkeypatch.setattr(s, "READS_SOCK", long_dir / ".s.PGSQL.5432")
    with pytest.raises(s.Refused, match="too long"):
        s.cmd_serve_reads(_args("on"))
    assert FakeRemote.calls == []


def test_a_rotated_password_is_reported(main_side):
    s = main_side
    _linked(s, "VELARIS_CASE_DATABASE_READ_URL=postgresql+asyncpg://velaris:OLD@/velaris"
               "?host=%2Fsrv%2Fv%2F.standby%2Freads-sock\n")
    assert s._reads_credentials_current() is False
    _linked(s, "VELARIS_CASE_DATABASE_READ_URL=postgresql+asyncpg://velaris:p%40ss:w@rd@/velaris"
               "?host=%2Fsrv%2Fv%2F.standby%2Freads-sock\n")
    assert s._reads_credentials_current() is True


def test_the_hxdr_events_are_accepted():
    from case_service.compliance import dr
    assert {"serve_reads_on", "serve_reads_off"} <= set(dr.STANDBY_EVENTS)
    assert all(len(e) <= 32 for e in dr.STANDBY_EVENTS)        # VARCHAR(32), migration 222


@pytest.mark.parametrize("reads,code", [({"tunnel_up": False, "credentials_current": True}, "standby_reads_down"),
                                        ({"tunnel_up": True, "credentials_current": False},
                                         "standby_reads_credentials"),
                                        ({"tunnel_up": True, "credentials_current": True}, None)])
def test_posture_says_when_reads_from_the_standby_are_not_happening(tmp_path, reads, code):
    from datetime import datetime, timezone
    from case_service.compliance import dr_status
    sd = tmp_path / ".standby"
    sd.mkdir()
    now = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)
    (sd / "main.json").write_text(json.dumps({"standby": "s1", "state": "linked"}))
    (sd / "status.json").write_text(json.dumps({
        "state": "streaming", "checked_at": now.isoformat(), "version": "1", "standby_version": "1",
        "databases": {}, "sync": {"received_at": now.isoformat()}, "serve_reads": {"since": "t", **reads}}))
    out = dr_status.standby_status(root=tmp_path, now=now)
    codes = {f["code"] for f in out["findings"]}
    assert out["facts"]["serve_reads"]["tunnel_up"] is reads["tunnel_up"]
    assert (code in codes) if code else not (codes & {"standby_reads_down", "standby_reads_credentials"})


def test_replication_logins_refuse_anything_but_scram():
    """The standby's tunnel ends on loopback ports, free for seconds at each
    reconnect: libpq must never send the replicator password in clear."""
    src = (ROOT / "scripts/standby/velaris_standby.py").read_text()
    assert "require_auth=scram-sha-256" in src.split("conninfo = (", 1)[1].split("\n\n", 1)[0]
    assert 'PGREQUIREAUTH="scram-sha-256"' in src and '"-e", "PGREQUIREAUTH"' in src


def test_the_deletion_ledger_is_copied_to_the_standby_append_only(standby, monkeypatch, tmp_path):  # noqa: F811
    """Purged accounts' records must survive the main: the standby pulls them
    every minute, and never mirrors a deletion of one (a promote makes its copy
    the new main's ledger)."""
    assert "deletions" in standby.FOLDERS and "deletions" in standby.APPEND_ONLY
    assert standby.RSYNC_SENDER_RE.fullmatch("rsync --server --sender -rtpe.LsfxCIvu . deletions/")
    # Nothing purged yet on the main: nothing to copy — not a failure.
    assert standby.folder_source("deletions") == (None, "no deleted accounts purged yet")
    (standby.ROOT / ".deletions").mkdir()
    assert standby.folder_source("deletions") == (standby.ROOT / ".deletions", "")
    standby.TUNNEL_ENV.write_text("MAIN_HOST=m\nMAIN_USER=u\nMAIN_SSH_PORT=22\n")
    monkeypatch.setattr(standby, "ssh_main", lambda req, **k: subprocess.CompletedProcess(
        [], 0, stdout=json.dumps({"path": "/x/.deletions", "rel": ".deletions", "has_files": True}).encode()))
    seen = {}

    def fake_run(cmd, **k):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="Number of files: 3\n", stderr="")
    monkeypatch.setattr(standby.subprocess, "run", fake_run)
    res = standby.sync_folder("deletions", {})
    assert res["state"] == "ok" and res["files"] == 3
    assert not any(a.startswith("--delete") or a.startswith("--max-delete") for a in seen["cmd"])


@pytest.mark.parametrize("out,want", [("true|42", ""), ("false|42", "not in recovery"),
                                      ("true|7", "not a copy")])
def test_the_replica_check_reads_what_postgresql_prints(standby, monkeypatch, out, want):  # noqa: F811
    """`pg_is_in_recovery()` joined to text prints "true", not psql's "t" — the
    check refused every real replica (clean box 2026-10-23); the earlier tests
    replaced the whole check, so its parsing had never run."""
    monkeypatch.setattr(standby.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=out + "\n", stderr=""))
    monkeypatch.setattr(standby, "psql", lambda db, sql: "42")
    got = standby._replica_answers("postgresql+asyncpg://velaris:pw@/velaris?host=/s")
    assert (want in got) if want else got == ""


def test_the_restart_hint_names_only_units_this_box_has(standby, monkeypatch):  # noqa: F811
    monkeypatch.setattr(standby.shutil, "which", lambda n: "/bin/systemctl")
    have = {"velaris-case-service", "velaris-analytics-service"}
    monkeypatch.setattr(standby.subprocess, "run", lambda argv, **k: subprocess.CompletedProcess(
        argv, 0 if argv[-1] in have else 1))
    h = standby.restart_hint()
    assert "restart velaris-case-service velaris-analytics-service — or" in h
    assert "user-service" not in h
    monkeypatch.setattr(standby.shutil, "which", lambda n: None)
    assert "systemctl" not in standby.restart_hint()
