"""R5c step 11 (a) — the standby link: off unless linked, SSH only, nothing exposed.

The live behaviour (copy, streaming, WAL cap, network cut, unlink, rollback) was
proven on two clean boxes — Nov_TODO step 11. These guard the parts that decide
safety and the "unlinked = exactly as before" rule.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
import importlib.util
import pathlib
import re
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[4]
COMPOSE = (ROOT / "deploy/docker-compose/docker-compose.yml").read_text()


@pytest.fixture
def standby(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("velaris_standby",
                                                  ROOT / "scripts/standby/velaris_standby.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod.Path, "home", staticmethod(lambda: tmp_path))
    # Every path the tool writes points into a throwaway install: no test may
    # reach the real one (seen 2026-10-22: nine `.backups/before-promote-*`
    # folders in this checkout, 224 lines in the host's journal, 14 database
    # rows). Read-only files (the compose file) stay real.
    inst = tmp_path / "install"
    st = inst / ".standby"
    st.mkdir(parents=True)
    for name, val in (("ROOT", inst), ("STATE", st), ("MAIN_RECORD", st / "main.json"),
                      ("STATUS_FILE", st / "status.json"), ("TUNNEL_ENV", st / "tunnel.env"),
                      ("SYNC_REPORT", st / "sync_report.json"), ("DEMOTED", st / "demoted.json"),
                      ("PROMOTED", st / "promoted.json"), ("FENCE_ENV", st / "fence.env"),
                      ("PREPARED", st / "prepared.json"), ("SYNC_STATE", st / "sync.json"),
                      ("MAIN_ENV_COPY", st / "main.env"), ("VAULT_COPY", st / "vault"),
                      ("IDENTITY_COPY", st / "identity"), ("OPENBAO_DIR", inst / "deploy" / "openbao"),
                      ("READS_ENV", st / "reads-tunnel.env"), ("READS_KEY", st / "reads_key"),
                      ("READS_KNOWN", st / "reads_known_hosts"), ("READS_SOCK_DIR", st / "reads-sock"),
                      ("READS_SOCK", st / "reads-sock" / ".s.PGSQL.5432"),
                      ("READS_UNIT", tmp_path / "units" / "velaris-standby-reads.path")):
        monkeypatch.setattr(mod, name, val)
    monkeypatch.setattr(mod, "ENV", {})
    mod._log_refusal_real = mod._log_refusal
    monkeypatch.setattr(mod, "_log_refusal", lambda req: None)
    # The real recorder writes to the install's database through a subprocess —
    # from a test that is this box's live database (seen 2026-10-22: 14 rows).
    mod._hxdr_record_real = mod.hxdr_record
    monkeypatch.setattr(mod, "hxdr_record", lambda *a, **k: None)
    return mod


def _service(name: str) -> str:
    return re.search(rf"^  {name}:\n((?:    .*\n|\s*\n)*)", COMPOSE, re.M).group(1)


def test_the_gateway_runs_only_under_the_standby_profile_and_on_loopback():
    gw = _service("replication-gw")
    assert 'profiles: ["standby"]' in gw
    ports = re.findall(r'- "([^"]+)"', gw.split("ports:")[1].split("volumes:")[0])
    assert ports and all(p.startswith("127.0.0.1:") for p in ports)
    conf = (ROOT / "deploy/docker-compose/replication-gw/nginx.conf").read_text()
    # Resolved per connection: an update recreates velaris-db with a new address.
    assert "resolver 127.0.0.11" in conf and "proxy_pass $velaris_db" in conf


@pytest.mark.parametrize("linked", [False, True])
def test_the_profile_follows_the_link_record_only(tmp_path, linked):
    (tmp_path / ".env").write_text("")
    if linked:
        (tmp_path / ".standby").mkdir()
        (tmp_path / ".standby/main.json").write_text("{}")
    out = subprocess.run(
        ["bash", "-c", f'VELARIS_DIR="{tmp_path}"; unset COMPOSE_PROFILES; '
                       f'. "{ROOT}/deploy/docker-compose/compose-profiles.sh"; '
                       'echo "[${COMPOSE_PROFILES:-}] $VELARIS_STANDBY_LINKED"'],
        capture_output=True, text=True, check=True).stdout.strip()
    assert ("standby" in out.split("]")[0]) is linked
    assert out.endswith("true" if linked else "false")


def test_setup_and_update_never_link():
    for f in ("setup-velaris.sh", "update-velaris.sh", "start-velaris.sh"):
        text = (ROOT / f).read_text()
        assert "standby-velaris.sh link" not in text, f
        # Reading the link record (a message) is fine; writing it never.
        assert not re.search(r">>?\s*\"?\$VELARIS_DIR/\.standby", text), f


def test_the_tunnel_key_may_forward_to_the_gateway_and_do_nothing_else(standby, tmp_path):
    ak = tmp_path / ".ssh/authorized_keys"
    ak.parent.mkdir()
    ak.write_text("ssh-ed25519 AAAAoperator me@laptop\n")
    standby.authorize_tunnel_key("ssh-ed25519 AAAAstandby velaris-standby-tunnel@s", "s.example")
    standby.authorize_tunnel_key("ssh-ed25519 AAAAnewer velaris-standby-tunnel@s", "s.example")
    lines = ak.read_text().splitlines()
    assert lines[0] == "ssh-ed25519 AAAAoperator me@laptop"          # untouched
    assert len(lines) == 2 and "AAAAnewer" in lines[1]               # replaced, not added
    opts = lines[1].split(" ssh-ed25519 ")[0].split(",")
    assert opts[0] == "restrict" and "port-forwarding" in opts
    assert sorted(o for o in opts if o.startswith("permitopen")) == [
        'permitopen="127.0.0.1:5442"', 'permitopen="127.0.0.1:5443"']
    # Remote forwards are pinned to the port the gateway already holds: none opens.
    assert 'permitlisten="127.0.0.1:5442"' in opts
    assert any(o.startswith('command="') and o.endswith('/standby-velaris.sh serve"') for o in opts)
    assert oct(ak.stat().st_mode & 0o777) == "0o600"
    assert standby.revoke_tunnel_key() is True
    assert ak.read_text() == "ssh-ed25519 AAAAoperator me@laptop\n"
    assert standby.revoke_tunnel_key() is False


@pytest.mark.parametrize("key", ['command="sh" ssh-ed25519 AAAA', 'ssh-ed25519 AA"AA',
                                 "ssh-ed25519", 'ssh-rsa,command="x" AAAA'])
def test_a_key_that_is_not_one_is_refused(standby, key):
    with pytest.raises(standby.Refused):
        standby.authorize_tunnel_key(key, "s")


def test_replication_is_allowed_for_the_role_only_by_password(standby):
    assert standby.HBA_LINE == "host replication velaris_replicator samenet scram-sha-256"


def test_the_tunnel_unit_stays_down_once_unlinked():
    """Seen 2026-10-22: an automatic restart does not re-test ConditionPathExists,
    so after `unlink` the unit restarted every 5 s for ever."""
    unit = (ROOT / "deploy/systemd/velaris-standby-tunnel.service").read_text()
    assert "ExecCondition=/usr/bin/test -f __VELARIS_DIR__/.standby/tunnel.env" in unit
    assert "EnvironmentFile=-__VELARIS_DIR__/.standby/tunnel.env" in unit
    assert "Restart=on-failure" in unit
    assert "StrictHostKeyChecking=yes" in unit and "-N" in unit
    assert "User=__VELARIS_USER__" in unit


def test_a_silent_standby_is_stalled_not_streaming(standby, monkeypatch):
    rows = {"velaris": "t|reserved|streaming|0|0.001|100|45",
            "temporal": "t|reserved|streaming|0|0.001|100|3"}
    monkeypatch.setattr(standby, "psql", lambda key, sql: rows[key])
    st = standby.main_db_state()
    assert st["velaris"]["state"] == "stalled" and st["temporal"]["state"] == "streaming"


@pytest.mark.parametrize("row, state", [
    ("||||||", "no_slot"),
    ("f|lost|||||", "needs_relink"),
    ("f|extended|||||", "disconnected"),
    ("t|reserved|catchup|500|||1", "catchup"),
])
def test_states(standby, monkeypatch, row, state):
    monkeypatch.setattr(standby, "psql", lambda key, sql: row)
    assert standby.main_db_state()["velaris"]["state"] == state


def test_link_state_never_reaches_git_or_the_mirror():
    assert ".standby/" in (ROOT / ".gitignore").read_text().splitlines()
    assert ".standby/" in (ROOT / "scripts/velaris-mirror-exclude.txt").read_text().splitlines()


# ── audit 2026-10-22 ────────────────────────────────────────────────────────

def test_the_database_is_given_a_scram_hash_never_the_password(standby):
    """The verifier must be one PostgreSQL accepts for that password: recompute it."""
    import base64
    import hashlib
    import hmac
    v = standby.scram_verifier("s3cret-pw")
    m = re.fullmatch(r"SCRAM-SHA-256\$4096:([^$]+)\$([^:]+):(.+)", v)
    salt, stored, server = (base64.b64decode(x) for x in m.groups())
    salted = hashlib.pbkdf2_hmac("sha256", b"s3cret-pw", salt, 4096)
    assert stored == hashlib.sha256(hmac.new(salted, b"Client Key", "sha256").digest()).digest()
    assert server == hmac.new(salted, b"Server Key", "sha256").digest()
    src = (ROOT / "scripts/standby/velaris_standby.py").read_text()
    assert "PASSWORD '{scram_verifier(password)}'" in src and "PASSWORD '{password}'" not in src


def test_the_replicas_never_trust_a_tcp_connection(standby):
    """Proven 2026-10-22: the main's pg_hba (copied by the base backup) trusts
    127.0.0.1, and on the standby's host network that is every local user."""
    hosts = [ln for ln in standby.REPLICA_HBA.splitlines() if ln.startswith("host")]
    assert hosts and all(ln.split()[-1] == "scram-sha-256" for ln in hosts)
    src = (ROOT / "scripts/standby/velaris_standby.py").read_text()
    assert '"-c", f"hba_file=/var/lib/postgresql/data/{REPLICA_HBA_NAME}"' in src
    assert '"listen_addresses=127.0.0.1"' in src


@pytest.mark.parametrize("host", ["-oProxyCommand=touch /tmp/x", "me@-oProxyCommand=x",
                                  "me@host;id", "me@host name", "Me$@host", "me@"])
def test_link_refuses_what_is_not_a_user_at_host(standby, host):
    import argparse
    a = argparse.Namespace(host=host, ssh_port=22, standby_dir=None, main_address=None,
                           main_ssh_port=22, replace_standby_data=False)
    with pytest.raises(standby.Refused, match="not a user@host"):
        standby.cmd_link(a)


def test_unlink_keeps_the_record_while_anything_is_left(standby, tmp_path, monkeypatch):
    """A slot left behind keeps WAL; once the record is gone nothing shows it."""
    import argparse
    rec = tmp_path / "main.json"
    rec.write_text('{"standby": "s"}')
    monkeypatch.setattr(standby, "MAIN_RECORD", rec)
    monkeypatch.setattr(standby, "STATUS_FILE", tmp_path / "status.json")
    monkeypatch.setattr(standby, "container_of", lambda s: "cid")
    monkeypatch.setattr(standby, "revoke_tunnel_key", lambda: True)
    monkeypatch.setattr(standby, "gw", lambda *a: None)
    monkeypatch.setattr(standby, "main_teardown", lambda: ["velaris: database refused"])
    a = argparse.Namespace(local_only=True)
    assert standby.cmd_unlink(a) == 1 and rec.exists()
    monkeypatch.setattr(standby, "main_teardown", lambda: [])
    assert standby.cmd_unlink(a) == 0 and not rec.exists()


@pytest.mark.parametrize("script", ["start-velaris.sh", "update-velaris.sh", "setup-velaris.sh",
                                    "restore-velaris.sh", "deploy/systemd/velaris-wait-deps.sh",
                                    "deploy/systemd/refresh-after-update.sh"])
def test_nothing_starts_the_platform_on_a_linked_standby(script):
    """A second PostgreSQL on the replicas' own data — refused before any docker call."""
    lines = (ROOT / script).read_text().splitlines()
    first = next(i for i, ln in enumerate(lines) if re.match(r"^VELARIS_DIR=", ln))
    guard = lines.index('if [ -f "$VELARIS_DIR/.standby/standby.json" ]; then')
    # Only the guard's own comment sits between VELARIS_DIR being known and it.
    assert all(ln.startswith("#") for ln in lines[first + 1:guard])
    # …and the demoted-old-main guard right after it.
    demoted = lines.index('if [ -f "$VELARIS_DIR/.standby/demoted.json" ]; then')
    assert all(ln.startswith(("#", "  ", "fi", "if [ -f")) or not ln for ln in lines[guard:demoted])


def test_the_guard_refuses_and_lets_an_unlinked_server_through(tmp_path):
    text = (ROOT / "start-velaris.sh").read_text()
    guard = re.search(r'if \[ -f "\$VELARIS_DIR/\.standby/standby\.json" \]; then.*?\nfi\n',
                      text, re.S).group(0)
    run = lambda: subprocess.run(["bash", "-c", f'VELARIS_DIR="{tmp_path}"\n{guard}echo passed'],  # noqa: E731
                                 capture_output=True, text=True)
    assert run().stdout.strip() == "passed"
    (tmp_path / ".standby").mkdir()
    (tmp_path / ".standby/standby.json").write_text("{}")
    r = run()
    assert r.returncode == 1 and "standby" in r.stderr and "passed" not in r.stdout


def test_uninstall_unlinks_before_it_deletes_anything():
    text = (ROOT / "uninstall-velaris.sh").read_text()
    unlink = text.find('"$VELARIS_DIR/standby-velaris.sh" unlink')
    assert 0 < unlink < text.find("down --volumes")
    for c in ("velaris-standby-db", "velaris-standby-temporal-db"):
        assert c in text


# ── part (b): documents, settings, vault ────────────────────────────────────

@pytest.fixture
def linked_main(standby, tmp_path, monkeypatch):
    rec = tmp_path / "main.json"
    rec.write_text("{}")
    monkeypatch.setattr(standby, "MAIN_RECORD", rec)
    monkeypatch.setattr(standby, "SYNC_REPORT", tmp_path / "sync_report.json")
    monkeypatch.setattr(standby, "STATE", tmp_path)
    return standby


@pytest.mark.parametrize("req", [
    "id", "export env; id", "export ../.env", "export", "sh -c id",
    "rsync --server --sender -logDtpre.iLsfxCIvu . /etc/",
    "rsync --server --sender -logDtpre.iLsfxCIvu . docs/../",
    "rsync --server --sender -logDtpre.iLsfxCIvu . docs/ /etc",
    "rsync --server -logDtpre.iLsfxCIvu . docs/",                 # a push INTO the main
    "rsync --server --sender -e.sh . docs/",
    "rsync --server --sender -logDtpre.iLsfxCIvu . docs/",          # -a: links, devices, owners
    "rsync --server --sender -tpre.iLsfxCIvu . keys/",
    "rsync --server --sender --rsync-path=sh -l . docs/",
])
def test_the_standby_key_gets_nothing_off_the_list(linked_main, monkeypatch, req):
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", req)
    monkeypatch.setattr(linked_main.os, "execvp", lambda *a: pytest.fail(f"exec'd {a}"))
    assert linked_main.cmd_serve() == 1


def test_the_rsync_sender_is_rooted_at_the_documents(linked_main, monkeypatch, tmp_path):
    docs = tmp_path / "docs-here"
    docs.mkdir()
    monkeypatch.setattr(linked_main, "docs_source", lambda: (docs, ""))
    monkeypatch.setattr(linked_main, "folder_source", lambda n: (docs, ""))
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "rsync --server --sender -tpre.iLsfxCIvu . docs/")
    seen = {}
    monkeypatch.setattr(linked_main.os, "execvp", lambda f, a: seen.update(f=f, a=a))
    linked_main.cmd_serve()
    assert seen["a"] == ["rsync", "--server", "--sender", "-tpre.iLsfxCIvu", ".", f"{docs}/"]


def test_an_unlinked_main_serves_nothing(standby, tmp_path, monkeypatch):
    monkeypatch.setattr(standby, "MAIN_RECORD", tmp_path / "absent.json")
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "export env")
    assert standby.cmd_serve() == 1


class _R:
    def __init__(self, rc=0, out=b"", err=b""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def _docs_env(standby, monkeypatch, tmp_path, *, main_has_files, local_files):
    import json
    target = tmp_path / "copy"
    target.mkdir()
    for i in range(local_files):
        (target / f"d{i}").write_text("x")
    monkeypatch.setattr(standby, "ssh_main", lambda req, **k: _R(0, json.dumps(
        {"path": str(target), "rel": None, "has_files": main_has_files}).encode()))
    monkeypatch.setattr(standby, "read_env", lambda p: {"MAIN_USER": "u", "MAIN_HOST": "h",
                                                       "MAIN_SSH_PORT": "22"})
    calls = []

    def fake_run(cmd, **k):
        calls.append(cmd)
        return _R(0, "Number of files: 7\n", "")
    monkeypatch.setattr(standby.subprocess, "run", fake_run)
    return calls


def test_an_empty_main_folder_never_empties_the_copy(standby, monkeypatch, tmp_path):
    """Seen 2026-10-22: a main whose document folder was emptied (an unmounted
    disk looks exactly like that) took all 301 of the standby's copies with it."""
    calls = _docs_env(standby, monkeypatch, tmp_path, main_has_files=False, local_files=3)
    res = standby.sync_folder("docs", {"docs": {"files": 301}})
    assert res["state"] == "deletes_held" and "EMPTY" in res["note"] and calls == []
    res = standby.sync_folder("docs", {"docs": {"files": 301}}, allow_deletes=True)
    assert calls and "--max-delete=1000000000" in calls[0]


@pytest.mark.parametrize("prev_files, cap", [(None, 10), (301, 30), (100_000, 500)])
def test_deletions_are_capped_by_a_share_of_the_store(standby, monkeypatch, tmp_path, prev_files, cap):
    calls = _docs_env(standby, monkeypatch, tmp_path, main_has_files=True, local_files=1)
    standby.sync_folder("docs", {"docs": {"files": prev_files}})
    assert f"--max-delete={cap}" in calls[0] and "--delete-delay" in calls[0]
    # Regular files, times, modes — never links, devices or owners.
    assert "-rtp" in calls[0] and "-a" not in calls[0]


def test_a_held_deletion_keeps_the_count_it_is_measured_against(standby, monkeypatch, tmp_path):
    _docs_env(standby, monkeypatch, tmp_path, main_has_files=True, local_files=1)
    monkeypatch.setattr(standby.subprocess, "run", lambda cmd, **k: _R(25, "Number of files: 40\n", ""))
    res = standby.sync_folder("docs", {"docs": {"files": 301}})
    assert res["state"] == "deletes_held" and res["files"] == 301


def test_unlink_takes_the_mains_secrets_off_the_standby(standby, tmp_path, monkeypatch):
    for n in ("main.env", "standby.json", "tunnel_key", "sync.json"):
        (tmp_path / n).write_text("x")
    (tmp_path / "vault").mkdir()
    (tmp_path / "vault" / "raft.snap").write_text("x")
    (tmp_path / "vault" / ".bao-init.json").write_text("x")
    monkeypatch.setattr(standby, "STATE", tmp_path)
    monkeypatch.setattr(standby, "TUNNEL_ENV", tmp_path / "tunnel.env")
    monkeypatch.setattr(standby, "VAULT_COPY", tmp_path / "vault")
    monkeypatch.setattr(standby, "run", lambda *a, **k: _R())
    standby.remote_stop({})
    assert [f for f in tmp_path.iterdir() if f.name != "install"] == []


def test_the_sync_timer_runs_only_while_linked_and_never_overlaps():
    svc = (ROOT / "deploy/systemd/velaris-standby-sync.service").read_text()
    timer = (ROOT / "deploy/systemd/velaris-standby-sync.timer").read_text()
    assert ("ExecCondition=/bin/sh -c 'test -f __VELARIS_DIR__/.standby/tunnel.env || "
            "test -f __VELARIS_DIR__/.standby/fence.env'") in svc
    assert "User=__VELARIS_USER__" in svc and "Type=oneshot" in svc
    assert "OnUnitInactiveSec=60" in timer
    inst = (ROOT / "deploy/systemd/install-standby-tunnel.sh").read_text()
    assert "velaris-standby-sync.timer" in inst and "command -v rsync" in inst


# ── part (b) audit ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/", "~", ".", "..", ".standby", "deploy/tls", ".venv/lib",
                                  "~/.ssh", "/etc/ssh", "/var/lib/docker/volumes"])
def test_a_copied_folder_is_never_one_that_holds_anything_else(standby, path):
    """VELARIS_CASE_STORAGE_LOCAL_PATH=. would serve the install (keys included)
    and, mirrored with deletions on the standby, delete it."""
    p = (standby.Path.home() / path[2:]) if path.startswith("~/") else (
        standby.Path.home() if path == "~" else pathlib.Path(path))
    p = p if p.is_absolute() else standby.ROOT / p
    assert standby.unsafe_folder(p)


@pytest.mark.parametrize("path", ["data/velaris-docs", "/mnt/nas/velaris-docs", "/srv/recordings"])
def test_ordinary_folders_are_fine(standby, path):
    p = pathlib.Path(path)
    assert standby.unsafe_folder(p if p.is_absolute() else standby.ROOT / p) is None


def test_the_main_will_not_serve_an_unsafe_folder(linked_main, monkeypatch):
    monkeypatch.setattr(linked_main, "folder_source", lambda n: (linked_main.ROOT, ""))
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "rsync --server --sender -tpre.iLsfxCIvu . docs/")
    monkeypatch.setattr(linked_main.os, "execvp", lambda *a: pytest.fail("served the install"))
    assert linked_main.cmd_serve() == 1


@pytest.mark.parametrize("info, why", [
    ({"path": "/x", "rel": "../../.ssh", "has_files": True}, "leaves the install"),
    ({"path": "/x", "rel": ".standby", "has_files": True}, "refused to write"),
    ({"path": "/nonexistent-mount/docs", "rel": None, "has_files": True}, "does not exist here"),
])
def test_the_standby_will_not_write_where_it_must_not(standby, monkeypatch, info, why):
    import json
    monkeypatch.setattr(standby, "ssh_main", lambda req, **k: _R(0, json.dumps(info).encode()))
    monkeypatch.setattr(standby.subprocess, "run", lambda *a, **k: pytest.fail("rsync ran"))
    res = standby.sync_folder("docs", {})
    assert res["state"] == "failed" and why in res["note"]


def test_two_syncs_never_run_at_once(standby, tmp_path, monkeypatch):
    import fcntl
    monkeypatch.setattr(standby, "STATE", tmp_path)
    monkeypatch.setattr(standby, "TUNNEL_ENV", tmp_path / "tunnel.env")
    (tmp_path / "tunnel.env").write_text("")
    held = open(tmp_path / "sync.lock", "w")
    fcntl.flock(held, fcntl.LOCK_EX)
    monkeypatch.setattr(standby, "sync_file", lambda *a: pytest.fail("ran while locked"))
    # The main answers (step 12 probe) — so it is the lock, not the probe, that stops this run.
    monkeypatch.setattr(standby, "ssh_main", lambda *a, **k: type("R", (), {"returncode": 0})())
    assert standby.cmd_sync() == 0


def test_a_sync_never_holds_the_lock_while_the_main_is_gone(standby, tmp_path, monkeypatch):
    """Step 12 (e), found on the boxes: a run against a vanished main spent 32 s in
    SSH timeouts holding the lock an automatic promote waited for."""
    import fcntl
    monkeypatch.setattr(standby, "STATE", tmp_path)
    monkeypatch.setattr(standby, "TUNNEL_ENV", tmp_path / "tunnel.env")
    (tmp_path / "tunnel.env").write_text("")
    monkeypatch.setattr(standby, "ssh_main", lambda *a, **k: type("R", (), {"returncode": 255})())
    monkeypatch.setattr(standby, "sync_file", lambda *a: pytest.fail("copied from a main that is gone"))
    assert standby.cmd_sync() == 0
    lock = open(tmp_path / "sync.lock", "w") if (tmp_path / "sync.lock").exists() else None
    if lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)          # free: never taken


def test_a_sync_stops_when_a_takeover_begins(standby, tmp_path, monkeypatch):
    monkeypatch.setattr(standby, "STATE", tmp_path)
    monkeypatch.setattr(standby, "TUNNEL_ENV", tmp_path / "tunnel.env")
    (tmp_path / "tunnel.env").write_text("")
    monkeypatch.setattr(standby, "ssh_main", lambda *a, **k: type("R", (), {"returncode": 0})())
    done = []

    def first_part(*a):
        done.append("env")
        (tmp_path / "failover").mkdir(exist_ok=True)
        (tmp_path / "failover" / "taking-over").write_text("{}")   # the agent starts a promote now
        return {"state": "ok"}
    monkeypatch.setattr(standby, "sync_file", first_part)
    monkeypatch.setattr(standby, "sync_folder", lambda *a: pytest.fail("went on after the takeover began"))
    assert standby.cmd_sync() == 0 and done == ["env"]


def test_refused_requests_are_logged_on_the_main(linked_main, monkeypatch):
    seen = []
    monkeypatch.setattr(linked_main, "_log_refusal", seen.append)
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "cat /etc/shadow")
    linked_main.cmd_serve()
    assert seen == ["cat /etc/shadow"]


# ── part (c): promote, demote, fence, retire ─────────────────────────────────

import argparse as _argparse  # noqa: E402
import json as _json  # noqa: E402


@pytest.fixture
def standby_box(standby, tmp_path, monkeypatch):
    """A linked standby's .standby/ in a temp dir, everything external stubbed."""
    st = tmp_path / ".standby"
    st.mkdir()
    for name, val in (("STATE", st), ("TUNNEL_ENV", st / "tunnel.env"), ("PROMOTED", st / "promoted.json"),
                      ("FENCE_ENV", st / "fence.env"), ("MAIN_ENV_COPY", st / "main.env"),
                      ("SYNC_STATE", st / "sync.json"), ("VAULT_COPY", st / "vault"),
                      ("DEMOTED", st / "demoted.json"), ("MAIN_RECORD", st / "main.json")):
        monkeypatch.setattr(standby, name, val)
    (st / "standby.json").write_text("{}")
    (st / "tunnel.env").write_text("MAIN_HOST=m\nMAIN_USER=u\nMAIN_SSH_PORT=22\n")
    (st / "main.env").write_text("X=1\n")
    (st / "main.velaris.yaml").write_text('velaris:\n  version: "3.1.2"\n')
    (st / "identity").mkdir()
    (st / "identity" / ".velaris-setup-complete").write_text("")
    monkeypatch.setattr(standby, "IDENTITY_COPY", st / "identity")
    monkeypatch.setattr(standby, "version", lambda: "3.1.2")
    monkeypatch.setattr(standby, "remote_status", lambda *a: {"databases": {
        "velaris": {"running": True, "in_recovery": True, "receiver": "streaming"},
        "temporal": {"running": True, "in_recovery": True, "receiver": "streaming"}}})
    monkeypatch.setattr(standby.shutil if hasattr(standby, "shutil") else __import__("shutil"),
                        "which", lambda t: "/usr/bin/" + t)
    return st


def _promote_args(**kw):
    return _argparse.Namespace(**{"main_is_down": False, "yes": True, "no_start": True,
                                  "accept_loss": False, **kw})


def test_promote_refuses_a_different_release(standby, standby_box):
    (standby_box / "main.velaris.yaml").write_text('velaris:\n  version: "3.2.0"\n')
    with pytest.raises(standby.Refused, match="install 3.2.0 here first"):
        standby.cmd_promote(_promote_args())
    assert not standby.PROMOTED.exists()


def test_promote_checks_its_tools_before_it_fences_anything(standby, standby_box, monkeypatch):
    """Seen 2026-10-22: no jq on the standby left the vault sealed AFTER the
    databases had been promoted."""
    (standby_box / "vault").mkdir()
    (standby_box / "vault" / "raft.snap").write_bytes(b"x")
    import shutil
    monkeypatch.setattr(shutil, "which", lambda t: None if t == "jq" else "/usr/bin/" + t)
    monkeypatch.setattr(standby, "ssh_main", lambda *a, **k: pytest.fail("fenced before checking"))
    with pytest.raises(standby.Refused, match="jq"):
        standby.cmd_promote(_promote_args())


def test_an_unreachable_main_needs_the_operator_to_say_it_is_down(standby, standby_box, monkeypatch):
    monkeypatch.setattr(standby, "ssh_main", lambda *a, **k: _R(255, b"", b"no route"))
    with pytest.raises(standby.Refused, match="--main-is-down"):
        standby.cmd_promote(_promote_args())
    # Nothing done: still a linked standby, nothing recorded.
    assert standby.TUNNEL_ENV.exists() and not standby.PROMOTED.exists()


def test_main_is_down_leaves_a_fence_for_when_it_comes_back(standby, standby_box, monkeypatch):
    monkeypatch.setattr(standby, "ssh_main", lambda *a, **k: _R(255, b"", b"no route"))
    stop = RuntimeError("stop after fencing")
    monkeypatch.setattr(standby, "_replica_sql", lambda *a, **k: (_ for _ in ()).throw(stop))
    with pytest.raises(RuntimeError):
        standby.cmd_promote(_promote_args(main_is_down=True))
    assert standby.FENCE_ENV.exists() and not standby.TUNNEL_ENV.exists()
    rec = _json.loads(standby.PROMOTED.read_text())
    assert rec["stage"] == "fenced" and rec["main_reached"] is False


def test_a_promote_stopped_part_way_resumes_and_a_finished_one_refuses(standby, standby_box, monkeypatch):
    standby.PROMOTED.write_text(_json.dumps({"stage": "done"}))
    with pytest.raises(standby.Refused, match="already been promoted"):
        standby.cmd_promote(_promote_args())
    standby.PROMOTED.write_text(_json.dumps({"stage": "vault", "stamp": "s", "main_reached": True}))
    monkeypatch.setattr(standby, "ssh_main", lambda *a, **k: pytest.fail("fenced again on resume"))
    monkeypatch.setattr(standby, "run", lambda *a, **k: _R())
    monkeypatch.setattr(standby, "container_of", lambda s: "cid")
    monkeypatch.setattr(standby, "missing_documents", lambda: (3, ["t1/x"]))
    assert standby.cmd_promote(_promote_args()) == 0
    rec = _json.loads(standby.PROMOTED.read_text())
    assert rec["stage"] == "done" and rec["documents_missing"] == 1


def test_a_demoted_main_serves_nothing(standby, standby_box, monkeypatch):
    standby.MAIN_RECORD.write_text("{}")
    standby.DEMOTED.write_text("{}")
    monkeypatch.setattr(standby, "demote_here", lambda: pytest.fail("demoted twice"))
    for req in ("export env", "demote", "rsync --server --sender -tpre.iLsfxCIvu . docs/"):
        monkeypatch.setenv("SSH_ORIGINAL_COMMAND", req)
        assert standby.cmd_serve() == 1


def test_the_fence_demotes_a_returning_old_main_and_flags_lost_writes(standby, standby_box, monkeypatch):
    standby.FENCE_ENV.write_text("MAIN_HOST=m\nMAIN_USER=u\nMAIN_SSH_PORT=22\n")
    for f in ("tunnel_key", "known_hosts"):
        (standby_box / f).write_text("x")
    standby.PROMOTED.write_text(_json.dumps({"stage": "done", "replayed_lsn": {
        "velaris": "0/9000000", "temporal": "0/5000000"}}))
    monkeypatch.setattr(standby, "ssh_to", lambda *a, **k: _R(0, _json.dumps({"final_lsn": {
        "velaris": "0/9100000", "temporal": "0/5000080"}}).encode()))
    monkeypatch.setattr(standby.subprocess, "run", lambda *a, **k: _R())
    assert standby.fence_retry() == 0
    rec = _json.loads(standby.PROMOTED.read_text())
    assert list(rec["old_main_diverged"]) == ["velaris"]        # temporal: only its shutdown record
    assert not standby.FENCE_ENV.exists() and not (standby_box / "tunnel_key").exists()


def test_the_fence_keeps_trying_while_the_old_main_is_away(standby, standby_box, monkeypatch):
    standby.FENCE_ENV.write_text("MAIN_HOST=m\nMAIN_USER=u\nMAIN_SSH_PORT=22\n")
    monkeypatch.setattr(standby, "ssh_to", lambda *a, **k: _R(255, b"", b"no route"))
    assert standby.fence_retry() == 0 and standby.FENCE_ENV.exists()


def test_the_timer_runs_the_fence_after_a_promote():
    svc = (ROOT / "deploy/systemd/velaris-standby-sync.service").read_text()
    assert "test -f __VELARIS_DIR__/.standby/fence.env" in svc


@pytest.mark.parametrize("text, n", [("0/0", 0), ("0/9000148", 0x9000148), ("1/0", 1 << 32)])
def test_lsn(standby, text, n):
    assert standby._lsn(text) == n


def test_retire_is_only_for_a_demoted_main(standby, standby_box):
    with pytest.raises(standby.Refused, match="not been demoted"):
        standby.cmd_retire(_argparse.Namespace())


# ── part (c) audit ──────────────────────────────────────────────────────────

def test_a_standby_that_is_not_receiving_is_not_promoted_over_a_live_main(standby, standby_box, monkeypatch):
    """Proven 2026-10-22 (gateway stopped): promoting would have thrown away what
    the main wrote since, without a word."""
    monkeypatch.setattr(standby, "remote_status", lambda *a: {"databases": {
        "velaris": {"running": True, "in_recovery": True, "receiver": None},
        "temporal": {"running": True, "in_recovery": True, "receiver": "streaming"}}})
    monkeypatch.setattr(standby, "ssh_main", lambda *a, **k: pytest.fail("fenced a main we are behind"))
    with pytest.raises(standby.Refused, match="not receiving"):
        standby.cmd_promote(_promote_args())


def _fenced_main(standby, monkeypatch, replayed):
    monkeypatch.setattr(standby, "ssh_main", lambda *a, **k: _R(0, _json.dumps(
        {"final_lsn": {"velaris": "0/D001040", "temporal": "0/5000000"}}).encode()))
    sql = []

    def fake_sql(key, q, check=True):
        sql.append((key, q))
        if "pg_wal_lsn_diff" in q:
            return "f"
        if "pg_last_wal_replay_lsn()" in q:
            return replayed[key]
        if "pg_is_in_recovery" in q:
            return "t"
        if "pg_promote" in q:
            return "t"
        return ""
    monkeypatch.setattr(standby, "_replica_sql", fake_sql)
    monkeypatch.setattr(standby.time, "monotonic", iter(range(0, 10_000, 200)).__next__)
    monkeypatch.setattr(standby, "run", lambda *a, **k: _R())
    return sql


def test_a_standby_short_of_the_stopped_main_stops_before_promoting(standby, standby_box, monkeypatch):
    """Proven 2026-10-22 (replay paused): the main stopped at 0/D001040, the
    standby had 0/D000FC8 — nothing may be promoted on that silently."""
    sql = _fenced_main(standby, monkeypatch, {"velaris": "0/D000FC8", "temporal": "0/5000000"})
    with pytest.raises(standby.Refused, match="did not receive all of it"):
        standby.cmd_promote(_promote_args())
    rec = _json.loads(standby.PROMOTED.read_text())
    assert rec["stage"] == "fenced" and list(rec["short_of_main"]) == ["velaris"]
    assert not any("pg_promote" in q for _, q in sql)


def test_accept_loss_promotes_and_the_replication_role_goes(standby, standby_box, monkeypatch):
    sql = _fenced_main(standby, monkeypatch, {"velaris": "0/D000FC8", "temporal": "0/5000000"})
    monkeypatch.setattr(standby, "container_of", lambda s: "cid")
    monkeypatch.setattr(standby, "missing_documents", lambda: (0, []))
    monkeypatch.setattr(standby, "_install", lambda *a: None)
    monkeypatch.setattr(standby.os, "chmod", lambda *a: None)
    assert standby.cmd_promote(_promote_args(accept_loss=True)) == 0
    for key in ("velaris", "temporal"):
        assert (key, "DROP ROLE IF EXISTS velaris_replicator") in sql
        assert any(k == key and "RESET primary_conninfo" in q for k, q in sql)


def test_unlink_on_a_demoted_main_never_reaches_the_new_main(standby, standby_box, monkeypatch):
    standby.MAIN_RECORD.write_text(_json.dumps({"ssh_target": "u@new-main", "standby_dir": "/x"}))
    standby.DEMOTED.write_text("{}")
    monkeypatch.setattr(standby, "Remote", lambda *a: pytest.fail("contacted the new main"))
    monkeypatch.setattr(standby, "revoke_tunnel_key", lambda: False)
    monkeypatch.setattr(standby, "STATUS_FILE", standby_box / "status.json")
    monkeypatch.setattr(standby, "SYNC_REPORT", standby_box / "sync_report.json")
    assert standby.cmd_unlink(_argparse.Namespace(local_only=False)) == 0
    assert not standby.MAIN_RECORD.exists() and standby.DEMOTED.exists()


def test_a_promoted_main_cannot_be_stopped_as_a_standby(standby, standby_box):
    (standby_box / "standby.json").unlink()
    standby.PROMOTED.write_text('{"stage": "done"}')
    with pytest.raises(standby.Refused, match="promoted"):
        standby.remote_stop({})


def test_status_on_a_demoted_main_says_so(standby, standby_box, capsys):
    standby.DEMOTED.write_text('{"demoted_at": "t"}')
    assert standby.cmd_status(_argparse.Namespace(json=False)) == 3
    assert "REPLACED" in capsys.readouterr().out


def test_a_failed_fence_attempt_is_recorded(standby, standby_box, monkeypatch, capsys):
    standby.FENCE_ENV.write_text("MAIN_HOST=m\nMAIN_USER=u\nMAIN_SSH_PORT=22\n")
    standby.PROMOTED.write_text("{}")
    monkeypatch.setattr(standby, "ssh_to", lambda *a, **k: _R(255, b"", b"Permission denied (publickey)"))
    standby.fence_retry()
    rec = _json.loads(standby.PROMOTED.read_text())
    assert "denied" in rec["fence_last_error"] and standby.FENCE_ENV.exists()
    assert "refuses this server's key" in capsys.readouterr().err


def test_same_host(standby):
    assert standby._same_host("localhost", "127.0.0.1")
    assert not standby._same_host("127.0.0.1", "127.0.0.2")


# ── part (d): the standby follows the main's code ───────────────────────────

@pytest.mark.parametrize("rel", [
    ".env", "Velaris/.env", "deploy/docker-compose/.env", ".env.production", "velaris-install-report.md",
    "deploy/openbao/.bao-init.json", "deploy/openbao/agent/role_id", "x/secret_id", "deploy/tls/certs/ca.pem",
    "certs/server.key", ".standby/main.json", ".backups/set.tar.age", ".venv/bin/python",
    "studio/node_modules/x.js", "studio/dist/index.html", "data/velaris-docs/a.bin", "logs/a.log",
    "services/x/__pycache__/a.pyc", ".git/config", "home/.ssh/authorized_keys", "id_ed25519",
    "deploy/docker-compose/egress-gw/conf/allowlist.json",
    # Seen on the dev box 2026-10-22 — files an install writes that no list named:
    ".velaris-key", ".velaris-setup-complete", "tenant_deks-unreadable-20260824T163402Z.json",
    "db_cred.txt", "demo_cred.txt", ".update-status", "app.py", "CLAUDE.md", ".claude/settings.json",
])
def test_nothing_secret_or_local_travels_as_code(standby, rel):
    assert standby.not_code(rel)


@pytest.mark.parametrize("rel", [
    "services/case-service/case_service/main.py", "standby-velaris.sh", ".env.example", "velaris.yaml",
    "migrations/postgresql/222_dr_standby_events.sql", "deploy/systemd/install.sh", "studio/src/App.tsx",
])
def test_code_is_code(standby, rel):
    assert not standby.not_code(rel)


def test_the_export_holds_no_secret(standby, tmp_path, monkeypatch):
    import io
    import tarfile
    root = tmp_path / "inst"
    for rel, body in (("velaris.yaml", "version: 3.2.0"), ("services/app.py", "x"), (".env", "SECRET=1"),
                      (".velaris-key", "{}"), ("tenant_deks-unreadable-x.json", "{}"), ("db_cred.txt", "pw"),
                      ("deploy/openbao/.bao-init.json", "{}"), ("Velaris/.env", "S=1"),
                      ("data/velaris-docs/d.bin", "doc"), (".standby/main.json", "{}")):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(body)
    (root / "link-to-env").symlink_to(root / ".env")
    monkeypatch.setattr(standby, "ROOT", root)
    monkeypatch.setattr(standby, "folder_source", lambda n: (root / "data/velaris-docs", "") if n == "docs" else (None, ""))
    buf = io.BytesIO()
    standby.export_code(buf)
    buf.seek(0)
    assert sorted(t.name for t in tarfile.open(fileobj=buf, mode="r:gz")) == ["services/app.py", "velaris.yaml"]


def _tar_bytes(files):
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, body in files:
            data = body.encode()
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def test_the_standby_writes_only_code_whatever_the_main_sends(standby, standby_box, tmp_path, monkeypatch):
    root = tmp_path / "standby-root"
    root.mkdir()
    (root / ".env").write_text("MINE=1")
    monkeypatch.setattr(standby, "ROOT", root)
    (standby_box / "main.velaris.yaml").write_text('velaris:\n  version: "3.2.0"\n')
    payload = _tar_bytes([("services/app.py", "new"), (".env", "EVIL=1"), (".standby/tunnel.env", "x"),
                          ("../outside.txt", "x"), ("deploy/openbao/.bao-init.json", "{}"),
                          (".velaris-key", "{}"), ("evil.py", "x")])

    class P:
        def __init__(self, *a, **k):
            import io
            self.stdout, self.stderr, self.returncode = io.BytesIO(payload), io.BytesIO(), 0

        def wait(self):
            return 0

        def kill(self):
            pass
    monkeypatch.setattr(standby.subprocess, "Popen", P)
    monkeypatch.setattr(standby.subprocess, "run", lambda *a, **k: _R(0, "", ""))
    res = standby.sync_code({})
    assert res["state"] == "ok" and res["files"] == 1
    assert (root / "services/app.py").read_text() == "new" and (root / ".env").read_text() == "MINE=1"
    assert not (tmp_path / "outside.txt").exists() and not (root / "deploy").exists()
    assert not (root / ".velaris-key").exists() and not (root / "evil.py").exists()


def test_same_version_fetches_nothing_and_a_failure_is_retried_slowly(standby, standby_box, monkeypatch):
    monkeypatch.setattr(standby.subprocess, "Popen", lambda *a, **k: pytest.fail("fetched"))
    assert standby.sync_code({})["state"] == "ok"                       # 3.1.2 == 3.1.2
    (standby_box / "main.velaris.yaml").write_text('velaris:\n  version: "3.2.0"\n')
    recent = {"code": {"target": "3.2.0", "state": "failed", "attempt_at": standby.now()}}
    assert standby.sync_code(recent)["state"] == "failed"               # not again within 15 min


def test_every_report_refreshes_the_status_the_platform_reads(linked_main, monkeypatch):
    import io
    seen = []
    monkeypatch.setattr(linked_main, "main_db_state", lambda: {"velaris": {"state": "streaming"}})
    monkeypatch.setattr(linked_main, "write_status", seen.append)
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "report")
    monkeypatch.setattr(linked_main.sys, "stdin", io.StringIO('{"standby_version": "3.2.0"}'))
    assert linked_main.cmd_serve() == 0 and seen == [{"velaris": {"state": "streaming"}}]


def test_a_failed_prepare_stays_failed_until_it_passes(standby, standby_box, monkeypatch):
    """Seen 2026-10-22: the run after a failed `prepare` found the versions equal
    and reported the code `ok` — the failure vanished from status and alerts."""
    failed = {"code": {"target": "3.1.2", "state": "failed", "note": "prepare failed",
                       "attempt_at": "2026-01-01T00:00:00+00:00"}}
    monkeypatch.setattr(standby.subprocess, "run", lambda *a, **k: _R(2, "", "missing uv"))
    res = standby.sync_code(failed)
    assert res["state"] == "failed" and "missing uv" in res["note"]
    assert standby.sync_code({"code": res})["state"] == "failed"         # within 15 min: unchanged
    monkeypatch.setattr(standby.subprocess, "run", lambda *a, **k: _R(0, "", ""))
    assert standby.sync_code(failed)["state"] == "ok"


def test_a_code_copy_that_cannot_finish_changes_nothing(standby, standby_box, tmp_path, monkeypatch):
    """Seen 2026-10-22: one file the owner could not write stopped the copy
    half-way — a tree of two releases."""
    import io
    root = tmp_path / "r"
    (root / "services" / "locked").mkdir(parents=True)
    (root / "services" / "a.py").write_text("old-a")
    (root / "services" / "locked" / "b.py").write_text("old-b")
    (root / "services" / "locked").chmod(0o500)
    monkeypatch.setattr(standby, "ROOT", root)
    (standby_box / "main.velaris.yaml").write_text('velaris:\n  version: "3.2.0"\n')
    payload = _tar_bytes([("services/a.py", "new-a"), ("services/locked/b.py", "new-b"),
                          ("velaris.yaml", "version: 3.2.0")])

    class P:
        def __init__(self, *a, **k):
            self.stdout, self.stderr, self.returncode = io.BytesIO(payload), io.BytesIO(), 0

        def wait(self):
            return 0

        def kill(self):
            pass
    monkeypatch.setattr(standby.subprocess, "Popen", P)
    try:
        res = standby.sync_code({})
    finally:
        (root / "services" / "locked").chmod(0o700)
    assert res["state"] == "failed" and "nothing was changed" in res["note"]
    assert (root / "services" / "a.py").read_text() == "old-a" and not (root / "velaris.yaml").exists()


# ── part (d) audit ──────────────────────────────────────────────────────────

def test_the_standby_keeps_the_mains_identity_for_promote(standby, standby_box, monkeypatch):
    """Without setup's marker the start gate refuses ("STARTUP BLOCKED") — found
    2026-10-22: a promoted standby would not have started."""
    monkeypatch.setattr(standby, "ssh_main", lambda *a, **k: _R(0, _json.dumps(
        {".velaris-setup-complete": "", ".velaris-key": '{"key_id": "k"}'}).encode()))
    res = standby.sync_identity({})
    assert res["state"] == "ok" and (standby_box / "identity" / ".velaris-setup-complete").exists()
    assert oct((standby_box / "identity" / ".velaris-key").stat().st_mode & 0o777) == "0o600"


def test_promote_refuses_without_the_setup_marker(standby, standby_box, monkeypatch):
    (standby_box / "identity" / ".velaris-setup-complete").unlink()
    monkeypatch.setattr(standby, "ssh_main", lambda *a, **k: pytest.fail("fenced"))
    with pytest.raises(standby.Refused, match="setup marker"):
        standby.cmd_promote(_promote_args())


def test_promote_waits_for_a_copy_in_progress(standby, standby_box, monkeypatch):
    import fcntl
    import threading
    (standby_box / "identity" / ".velaris-setup-complete").unlink()   # promote then refuses quickly
    held = open(standby_box / "sync.lock", "w")
    fcntl.flock(held, fcntl.LOCK_EX)
    out = []
    t = threading.Thread(target=lambda: out.append(
        pytest.raises(standby.Refused, standby.cmd_promote, _promote_args())))
    t.start()
    t.join(1.0)
    assert t.is_alive()                     # blocked behind the copy
    fcntl.flock(held, fcntl.LOCK_UN)
    t.join(5.0)
    assert not t.is_alive()


# ── part (e): found on two full clean installs ──────────────────────────────

def test_hxdr_is_recorded_directly_never_through_the_pooler(standby, monkeypatch, tmp_path):
    """Both promotes on the clean boxes lost their HxDR record: the pooler is
    not running yet when a promote records (connect call failed … 6432)."""
    seen = {}
    monkeypatch.setattr(standby, "ROOT", tmp_path)
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "python").write_text("")
    monkeypatch.setattr(standby.subprocess, "run", lambda cmd, **k: seen.update(env=k["env"]) or _R(0, "", ""))
    standby._hxdr_record_real("promote", "old", {})
    assert seen["env"]["VELARIS_CASE_DATABASE_POOLER_URL"] == ""


def test_relinking_an_old_main_saves_its_databases_first(standby, standby_box, monkeypatch):
    """Proven 2026-10-22: a main cut off the network took 1,186 writes after the
    switch-over; `link --replace-standby-data` would have wiped them."""
    standby.DEMOTED.write_text("{}")
    order = []
    monkeypatch.setattr(standby, "save_old_main_databases", lambda image: order.append("save") or "/x")
    monkeypatch.setattr(standby, "_port_open", lambda port, *a, **k: str(port) in ("5442", "5443"))
    monkeypatch.setattr(standby, "docker_ok", lambda *a: order.append(("docker", a[0])) or False)
    monkeypatch.setattr(standby, "run", lambda cmd, **k: order.append(cmd[1]) or _R(0, "", ""))
    monkeypatch.setattr(standby, "remote_status", lambda *a: {"databases": {
        "velaris": {"receiver": "streaming"}, "temporal": {"receiver": "streaming"}}})
    res = standby.remote_start({"image": "pg", "known_hosts": "", "main_host": "m", "main_ssh_port": 22,
                                "main_user": "u", "main_ports": {"velaris": "5442", "temporal": "5443"},
                                "password": "p", "replace": True})
    assert order[order.index("save"):].count("save") == 1
    assert order.index("save") < next(i for i, x in enumerate(order) if x == ("docker", "volume"))
    assert res["saved_old_databases"] == "/x"


def test_retire_names_every_platform_unit_not_only_services(standby):
    src = (ROOT / "scripts/standby/velaris_standby.py").read_text()
    assert "list-unit-files 'velaris-*' --state=enabled" in src    # timers and paths too
