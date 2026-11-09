"""R5c step 12 (a) — the failover witness and `failover` commands.

The witness decides which server may write. These pin the rules the safety
argument rests on (Nov_TODO step 12 design, decisions 2 and 7): no grant while
the holder renews; a grant only after lease + margin of silence on the
witness's own clock; a new boot waits a full lease; an older term is told to
fence; a pause refuses every grant; nothing but the listed requests is served.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
import importlib.util
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[4]
PUB_A = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa a@x"
PUB_B = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb b@y"


@pytest.fixture
def w(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("velaris_witness", ROOT / "scripts/standby/velaris_witness.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    home = tmp_path / ".velaris-witness"
    monkeypatch.setattr(mod, "HOME", home)
    monkeypatch.setattr(mod, "STATE", home / "state.json")
    monkeypatch.setattr(mod, "LOCK", home / "state.lock")
    monkeypatch.setattr(mod.Path, "home", staticmethod(lambda: tmp_path))
    clock = {"t": 1000.0, "boot": "boot-1"}
    monkeypatch.setattr(mod, "boottime", lambda: clock["t"])
    monkeypatch.setattr(mod, "boot_id", lambda: clock["boot"])
    mod.clock = clock
    mod.setup({"holder": "a", "lease": 30, "margin": 5,
               "nodes": {"a": {"pubkey": PUB_A, "label": "main"}, "b": {"pubkey": PUB_B, "label": "standby"}}})
    return mod


def ask(w, node, req, monkeypatch, capsys):
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", req)
    w.serve(node)
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_no_grant_while_the_holder_renews(w, monkeypatch, capsys):
    for _ in range(20):
        w.clock["t"] += 10
        assert ask(w, "a", "renew 1", monkeypatch, capsys)["renewed"] is True
        assert ask(w, "b", "acquire 1", monkeypatch, capsys)["granted"] is False


def test_grant_only_after_lease_plus_margin_of_silence(w, monkeypatch, capsys):
    ask(w, "a", "renew 1", monkeypatch, capsys)
    w.clock["t"] += 34.9
    r = ask(w, "b", "acquire 1", monkeypatch, capsys)
    assert r["granted"] is False and "needs 35" in r["reason"]
    w.clock["t"] += 0.2
    r = ask(w, "b", "acquire 1", monkeypatch, capsys)
    assert r["granted"] is True and r["term"] == 2 and r["holder"] == "b"


def test_the_old_holder_is_told_to_fence_and_cannot_take_it_back(w, monkeypatch, capsys):
    w.clock["t"] += 40
    assert ask(w, "b", "acquire 1", monkeypatch, capsys)["granted"]
    r = ask(w, "a", "renew 1", monkeypatch, capsys)
    assert r["renewed"] is False and r["fence"] is True and r["term"] == 2
    # Its stale term cannot acquire either; the new holder's lease started at the grant.
    assert ask(w, "a", "acquire 1", monkeypatch, capsys)["granted"] is False
    w.clock["t"] += 20
    assert ask(w, "a", "acquire 2", monkeypatch, capsys)["granted"] is False
    assert ask(w, "b", "renew 2", monkeypatch, capsys)["renewed"] is True


def test_a_witness_reboot_waits_a_whole_lease(w, monkeypatch, capsys):
    w.clock["t"] += 100                     # long silent on the OLD boot …
    w.clock.update(t=3.0, boot="boot-2")    # … then the witness reboots
    r = ask(w, "b", "acquire 1", monkeypatch, capsys)
    assert r["granted"] is False            # the old boot's silence does not count
    w.clock["t"] += 35.1
    assert ask(w, "b", "acquire 1", monkeypatch, capsys)["granted"] is True


def test_pause_refuses_every_grant_and_only_the_holder_pauses(w, monkeypatch, capsys):
    assert ask(w, "b", "pause", monkeypatch, capsys)["ok"] is False
    assert ask(w, "a", "pause kernel update", monkeypatch, capsys)["paused"]["reason"] == "kernel update"
    w.clock["t"] += 1000
    assert ask(w, "b", "acquire 1", monkeypatch, capsys)["granted"] is False
    ask(w, "a", "resume", monkeypatch, capsys)
    # Resume restarts the lease: a long-silent holder is not taken over at once.
    assert ask(w, "b", "acquire 1", monkeypatch, capsys)["granted"] is False


@pytest.mark.parametrize("req", ["renew", "renew x", "acquire 1; rm -rf ~", "state now", "teardown",
                                 "setup", "pause $(id)", "renew 1\nrenew 1", ""])
def test_anything_off_the_list_is_refused(w, monkeypatch, capsys, req):
    before = json.loads(w.STATE.read_text())
    r = ask(w, "a", req, monkeypatch, capsys)
    assert r == {"ok": False, "error": "refused"}
    after = json.loads(w.STATE.read_text())
    assert after["term"] == before["term"] and after["holder"] == before["holder"]


def test_an_unknown_node_gets_nothing(w, monkeypatch, capsys):
    assert ask(w, "c", "state", monkeypatch, capsys)["ok"] is False
    assert ask(w, "A;x", "state", monkeypatch, capsys)["ok"] is False


def test_setup_writes_forced_command_keys_only(w, tmp_path):
    ak = (tmp_path / ".ssh/authorized_keys").read_text().splitlines()
    assert len(ak) == 2
    for line, node in zip(ak, ("a", "b")):
        assert line.startswith(f'restrict,command="python3 ') and f' serve {node}" ' in line
    assert oct((tmp_path / ".ssh/authorized_keys").stat().st_mode)[-3:] == "600"
    assert oct(w.STATE.stat().st_mode)[-3:] == "600"


def test_setup_keeps_other_keys_and_teardown_removes_only_ours(w, tmp_path):
    ak = tmp_path / ".ssh/authorized_keys"
    ak.write_text("ssh-ed25519 AAAAown me@laptop\n" + ak.read_text())
    w.setup({"holder": "a", "nodes": {"a": {"pubkey": PUB_A}, "b": {"pubkey": PUB_B}}})
    assert ak.read_text().count("velaris-failover-witness") == 2 and "me@laptop" in ak.read_text()
    assert w.teardown({})["keys_removed"] == 2
    assert ak.read_text() == "ssh-ed25519 AAAAown me@laptop\n" and not w.STATE.exists()
    assert not w.HOME.exists()                 # nothing of Velaris is left on the witness


@pytest.mark.parametrize("payload, msg", [
    ({"holder": "a", "nodes": {"a": {"pubkey": PUB_A}}}, "two nodes"),
    ({"holder": "c", "nodes": {"a": {"pubkey": PUB_A}, "b": {"pubkey": PUB_B}}}, "two nodes"),
    ({"holder": "a", "lease": 5, "nodes": {"a": {"pubkey": PUB_A}, "b": {"pubkey": PUB_B}}}, "lease"),
    ({"holder": "a", "nodes": {"a": {"pubkey": "ssh-ed25519 AAA\ncommand=x"}, "b": {"pubkey": PUB_B}}}, "usable"),
    ({"holder": "a", "nodes": {"a": {"pubkey": PUB_A, "label": "x y"}, "b": {"pubkey": PUB_B}}}, "label"),
])
def test_setup_refuses_bad_input(w, payload, msg):
    with pytest.raises(ValueError, match=msg):
        w.setup(payload)


def test_the_witness_runs_on_python_3_8_syntax():
    import ast
    src = (ROOT / "scripts/standby/velaris_witness.py").read_text()
    ast.parse(src, feature_version=(3, 8))
    assert "import " not in src.split('"""', 2)[2].replace("from __future__ import annotations", "").split(
        "def ")[0].replace("import datetime as dt", "").replace("import fcntl", "").replace(
        "import json", "").replace("import os", "").replace("import re", "").replace(
        "import sys", "").replace("import time", "").replace("from pathlib import Path", "")


# ── the commands on the Velaris servers ──────────────────────────────────────

@pytest.fixture
def fo(tmp_path, monkeypatch):
    import sys
    sys.path.insert(0, str(ROOT / "scripts/standby"))
    import velaris_failover as mod
    spec = importlib.util.spec_from_file_location("velaris_standby_t", ROOT / "scripts/standby/velaris_standby.py")
    sb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sb)
    st = tmp_path / ".standby"
    st.mkdir()
    for name, val in (("ROOT", tmp_path), ("STATE", st), ("MAIN_RECORD", st / "main.json"),
                      ("DEMOTED", st / "demoted.json")):
        monkeypatch.setattr(sb, name, val)
    monkeypatch.setattr(sb, "hxdr_record", lambda *a, **k: None)
    monkeypatch.setattr(mod, "sb", sb)
    return mod


def _args(**kw):
    import argparse
    base = dict(witness="w@witness.example", witness_port=22, lease=30, margin=5, again=False,
                json=False, reason="maintenance", local_only=False)
    return argparse.Namespace(**{**base, **kw})


def test_enable_needs_a_linked_main(fo):
    with pytest.raises(fo.sb.Refused, match="no standby"):
        fo.cmd_enable(_args())


def test_enable_refuses_a_witness_that_is_the_standby_or_this_server(fo, monkeypatch):
    fo.sb.MAIN_RECORD.write_text(json.dumps({"standby": "sb.example", "ssh_target": "sb.example",
                                             "standby_dir": "/x"}))
    with pytest.raises(fo.sb.Refused, match="third machine"):
        fo.cmd_enable(_args(witness="sb.example"))
    import socket
    with pytest.raises(fo.sb.Refused, match="third machine"):
        fo.cmd_enable(_args(witness=socket.gethostname()))


def test_enable_refuses_a_standby_that_is_not_streaming(fo, monkeypatch):
    fo.sb.MAIN_RECORD.write_text(json.dumps({"standby": "sb.example", "ssh_target": "sb.example",
                                             "standby_dir": "/x"}))
    monkeypatch.setattr(fo.sb, "main_db_state", lambda: {"velaris": {"state": "streaming"},
                                                          "temporal": {"state": "disconnected"}})
    with pytest.raises(fo.sb.Refused, match="not streaming"):
        fo.cmd_enable(_args())


@pytest.mark.parametrize("target", ["-oProxyCommand=x", "a b", "root@", "us er@host", "h;id"])
def test_witness_targets_are_validated(fo, target):
    with pytest.raises(fo.sb.Refused):
        fo.parse_target(target)


def test_the_standby_accepts_only_well_formed_witness_settings(fo):
    fo.new_key = lambda: "ssh-ed25519 AAAA x"
    (fo.fdir()).mkdir(mode=0o700, parents=True, exist_ok=True)
    fo.key_path().write_text("k")
    good = {"node": "b", "peer": "a", "witness_target": "w@witness.example", "witness_port": 22,
            "lease": 30, "margin": 5, "term": 1,
            "known_hosts": "witness.example ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAaa"}
    fo.remote_failover_set(good)
    assert fo.read_conf()["node"] == "b" and oct(fo.conf_path().stat().st_mode)[-3:] == "600"
    with pytest.raises(fo.sb.Refused, match="host keys"):
        fo.remote_failover_set({**good, "known_hosts": "witness.example ssh-ed25519 AAAA\n@cert-authority * x"})
    with pytest.raises(fo.sb.Refused):
        fo.remote_failover_set({**good, "witness_target": "-oProxyCommand=sh"})


def test_status_and_disable_when_off_change_nothing(fo, capsys):
    assert fo.cmd_status(_args()) == 0 and "off" in capsys.readouterr().out
    assert fo.cmd_disable(_args()) == 0
    assert not fo.fdir().exists()


def test_witness_calls_use_only_this_servers_key_and_pinned_host_keys(fo, monkeypatch):
    seen = {}

    class R:
        returncode, stdout, stderr = 0, '{"ok": true, "term": 1}\n', ""

    def fake(cmd, **kw):
        seen["cmd"] = cmd
        return R()
    monkeypatch.setattr(fo.subprocess, "run", fake)
    conf = {"witness_target": "w@witness.example", "witness_port": 2222}
    assert fo.witness("state", conf=conf)["term"] == 1
    c = " ".join(seen["cmd"])
    for opt in ("IdentitiesOnly=yes", "BatchMode=yes", "StrictHostKeyChecking=yes",
                f"UserKnownHostsFile={fo.known_path()}", f"-i {fo.key_path()}", "-p 2222"):
        assert opt in c
    assert seen["cmd"][-1] == "state"


def test_the_hxdr_events_exist():
    src = (ROOT / "services/case-service/case_service/compliance/dr.py").read_text()
    for e in ("failover_enable", "failover_disable", "failover_pause", "failover_resume", "auto_promote"):
        assert f'"{e}"' in src


def test_the_witness_never_takes_its_state_location_from_the_environment():
    src = (ROOT / "scripts/standby/velaris_witness.py").read_text()
    assert 'HOME = Path.home() / ".velaris-witness"' in src and "environ.get(\"VELARIS_WITNESS" not in src


@pytest.mark.parametrize("rel", [".standby/failover/witness_key", "scripts/standby/witness_key",
                                 "scripts/x/witness_known_hosts", "deploy/tunnel_key"])
def test_failover_keys_never_travel_as_code(rel):
    spec = importlib.util.spec_from_file_location("velaris_standby_c", ROOT / "scripts/standby/velaris_standby.py")
    sb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sb)
    assert sb.not_code(rel)


def test_unlink_disables_failover_before_the_link_goes(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("velaris_standby_u", ROOT / "scripts/standby/velaris_standby.py")
    sb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sb)
    st = tmp_path / ".standby"
    st.mkdir()
    for name, val in (("ROOT", tmp_path), ("STATE", st), ("MAIN_RECORD", st / "main.json"),
                      ("STATUS_FILE", st / "status.json"), ("SYNC_REPORT", st / "sync_report.json"),
                      ("DEMOTED", st / "demoted.json"), ("READS_ENV", st / "reads-tunnel.env")):
        monkeypatch.setattr(sb, name, val)
    order = []
    fo = sb._failover()
    monkeypatch.setattr(fo, "read_conf", lambda: {"witness_target": "w@x"})
    monkeypatch.setattr(fo, "cmd_disable", lambda a: order.append(("disable", a.local_only)) or 0)
    monkeypatch.setattr(sb, "_read_url_is_ours", lambda: False)
    monkeypatch.setattr(sb, "container_of", lambda s: None)
    monkeypatch.setattr(sb, "revoke_tunnel_key", lambda: order.append("revoke") or False)
    monkeypatch.setattr(sb, "gw", lambda *a: None)
    monkeypatch.setattr(sb, "hxdr_record", lambda *a, **k: None)
    import argparse
    sb.cmd_unlink(argparse.Namespace(local_only=True))
    assert order[0] == ("disable", True) and "revoke" in order
    # The failover module worked on this test's install, not the real one.
    assert fo.fdir() == st / "failover"


# ── part (b): the main's agent, the fence rule, the heartbeat ───────────────

@pytest.fixture
def main(fo, monkeypatch):
    """A linked main with failover enabled; clocks and docker are fakes."""
    fo.sb.MAIN_RECORD.write_text("{}")
    fo.write_conf({"node": "a", "peer": "b", "witness_target": "w@x", "witness_port": 22,
                   "lease": 30, "margin": 5, "term": 1})
    clock = {"t": 500.0, "boot": "boot-1"}
    monkeypatch.setattr(fo, "boottime", lambda: clock["t"])
    monkeypatch.setattr(fo, "boot_id", lambda: clock["boot"])
    calls = []

    class R:
        returncode, stdout, stderr = 0, "false\n", ""

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return R()
    monkeypatch.setattr(fo.subprocess, "run", fake_run)
    monkeypatch.setattr(fo.sb, "container_of", lambda svc: f"cid-{svc}")
    monkeypatch.setattr(fo.sb, "psql", lambda key, sql: "" if "synchronous_standby_names" in sql else "0/1A2B")
    fo.clock, fo.calls = clock, calls
    return fo


def _stamp(fo, path, key, t):
    fo._write(path, {"boot_id": fo.clock["boot"], key: t})


def test_a_main_that_hears_either_one_keeps_running(main):
    _stamp(main, main.lease_path(), "renew_ok_bt", 500 - 100)   # witness long silent
    _stamp(main, main.peer_path(), "bt", 500 - 3)              # standby heard
    assert main.fence_check()["fence"] is False
    _stamp(main, main.lease_path(), "renew_ok_bt", 500 - 3)
    _stamp(main, main.peer_path(), "bt", 500 - 100)
    assert main.fence_check()["fence"] is False
    assert not main.fenced_path().exists() and main.calls == []


def test_a_main_that_hears_neither_for_half_the_lease_fences(main):
    _stamp(main, main.lease_path(), "renew_ok_bt", 500 - 15.5)
    _stamp(main, main.peer_path(), "bt", 500 - 16)
    r = main.fence_check()
    assert r["fence"] is True and main.fenced_path().exists()
    flat = [" ".join(c) for c in main.calls]
    # Mark first (already on disk before any kill), then the gateway, then the databases.
    assert flat.index("docker kill velaris-gateway") < flat.index("docker kill -s INT cid-velaris-db")
    assert "docker kill -s INT cid-temporal-db" in flat
    rec = json.loads(main.fenced_path().read_text())
    assert rec["replaced"] is False and "velaris-gateway" in rec["containers"]
    assert oct(main.fenced_path().stat().st_mode)[-3:] == "600"


def test_just_under_the_limit_does_not_fence(main):
    _stamp(main, main.lease_path(), "renew_ok_bt", 500 - 14.9)
    _stamp(main, main.peer_path(), "bt", 500 - 100)
    assert main.fence_check()["fence"] is False


def test_after_a_reboot_old_stamps_count_as_at_boot(main):
    _stamp(main, main.lease_path(), "renew_ok_bt", 499)
    _stamp(main, main.peer_path(), "bt", 499)
    main.clock.update(t=10.0, boot="boot-2")          # booted 10 s ago, nothing confirmed yet
    assert main.fence_check()["fence"] is False
    main.clock["t"] = 16.0                             # 16 s after boot, still unconfirmed
    assert main.fence_check()["fence"] is True


def test_no_fence_while_paused_or_on_a_server_that_is_not_a_linked_main(main):
    main._write(main.lease_path(), {"boot_id": "boot-1", "renew_ok_bt": 0, "paused": {"by": "a"}})
    assert main.fence_check() == {"fence": False, "why": "paused"}
    main._write(main.lease_path(), {})
    main.sb.MAIN_RECORD.unlink()                       # no standby: no one to be replaced by
    assert main.fence_check()["fence"] is False
    main.sb.MAIN_RECORD.write_text("{}")
    (main.sb.STATE / "standby.json").write_text("{}")  # this is a standby
    assert main.fence_check()["fence"] is False
    assert main.calls == []


def test_the_agent_renews_from_before_the_request_and_never_fences_a_renewed_main(main, monkeypatch):
    seen = []
    monkeypatch.setattr(main, "witness", lambda req, **k: seen.append(req) or {"ok": True, "renewed": True,
                                                                              "term": 1, "paused": None})
    out = main.agent_step()
    assert seen == ["renew 1"] and out["renewed"] and out["check"]["fence"] is False
    assert json.loads(main.lease_path().read_text())["renew_ok_bt"] == 500.0


def test_a_main_the_witness_replaced_fences_and_is_demoted(main, monkeypatch):
    monkeypatch.setattr(main, "witness", lambda req, **k: {"ok": True, "renewed": False, "fence": True,
                                                           "term": 2, "holder": "b"})
    out = main.agent_step()
    assert out["replaced"] and main.fenced_path().exists() and main.sb.DEMOTED.exists()
    assert json.loads(main.fenced_path().read_text())["replaced"] is True


def test_a_fence_is_lifted_only_by_a_renewal_and_never_for_a_replaced_main(main, monkeypatch):
    _stamp(main, main.lease_path(), "renew_ok_bt", 400)
    main.fence_check()
    assert main.fenced_path().exists()
    assert main.recover_if_safe({"renewed": False}) is False and main.fenced_path().exists()
    assert main.recover_if_safe({"renewed": True}) is True and not main.fenced_path().exists()
    flat = [" ".join(c) for c in main.calls]
    assert flat.index("docker start cid-velaris-db") < flat.index("docker start velaris-gateway")
    main.fence_here("replaced", replaced=True)
    assert main.recover_if_safe({"renewed": True}) is False and main.fenced_path().exists()


def test_the_heartbeat_is_recorded_and_answers_term_fence_and_wal(main):
    r = main.heartbeat()
    assert r["ok"] and r["term"] == 1 and r["fenced"] is False and r["lsn"]["velaris"] == "0/1A2B"
    assert json.loads(main.peer_path().read_text())["bt"] == 500.0


def test_serve_answers_the_heartbeat_only_on_the_standbys_key_path():
    src = (ROOT / "scripts/standby/velaris_standby.py").read_text()
    serve = src.split("def cmd_serve")[1].split("\ndef ")[0]
    assert 'req == "failover heartbeat"' in serve
    # After the linked / not-demoted gate, like every other request.
    assert serve.index("DEMOTED.exists()") < serve.index('"failover heartbeat"')


@pytest.mark.parametrize("script", ["start-velaris.sh", "update-velaris.sh", "setup-velaris.sh",
                                    "restore-velaris.sh", "deploy/systemd/velaris-wait-deps.sh",
                                    "deploy/systemd/refresh-after-update.sh"])
def test_every_start_path_refuses_a_fenced_server(script, tmp_path):
    import subprocess
    src = (ROOT / script).read_text()
    assert '.standby/fenced.json' in src
    block = src[src.index('if [ -f "$VELARIS_DIR/.standby/fenced.json" ]'):]
    block = block[:block.index("\nfi\n") + 4]
    (tmp_path / ".standby").mkdir()
    (tmp_path / ".standby/fenced.json").write_text("{}")
    r = subprocess.run(["bash", "-c", f'VELARIS_DIR="{tmp_path}"\n{block}\necho started'],
                       capture_output=True, text=True)
    assert r.returncode == 1 and "started" not in r.stdout and "stopped itself" in r.stderr
    (tmp_path / ".standby/fenced.json").unlink()
    r = subprocess.run(["bash", "-c", f'VELARIS_DIR="{tmp_path}"\n{block}\necho started'],
                       capture_output=True, text=True)
    assert r.returncode == 0 and "started" in r.stdout          # not fenced: exactly as before


def test_the_units_run_as_the_owner_only_while_enabled_and_the_fence_check_is_independent():
    d = ROOT / "deploy/systemd"
    agent = (d / "velaris-failover-agent.service").read_text()
    fence = (d / "velaris-failover-fence.service").read_text()
    timer = (d / "velaris-failover-fence.timer").read_text()
    for u in (agent, fence):
        assert "User=__VELARIS_USER__" in u and "failover/failover.json" in u.split("ExecCondition=")[1]
    assert "Restart=always" in agent and "failover agent" in agent
    # A platform an automatic promote started is the agent's child: restarting
    # the agent must not take it down (box test F4, 2026-10-30).
    assert "KillMode=process" in agent
    assert "failover fence-check" in fence and "After=velaris-failover-agent" not in fence
    assert "OnUnitActiveSec=5" in timer and "OnBootSec=5" in timer
    inst = (d / "install-failover.sh").read_text()
    # The agent's first renewal before the fence check is armed.
    assert inst.index("systemctl restart velaris-failover-agent.service") < inst.index(
        "systemctl enable --now velaris-failover-fence.timer")


def test_enable_starts_the_fence_clock_at_the_witness_setup(fo):
    src = (ROOT / "scripts/standby/velaris_failover.py").read_text()
    enable = src.split("def cmd_enable")[1].split("\ndef ")[0]
    assert enable.index("stamp = boottime()") < enable.index('adm.tool("setup"')
    assert '"renew_ok_bt": stamp' in enable


def test_a_fence_stays_when_the_databases_do_not_come_back(main, monkeypatch):
    _stamp(main, main.lease_path(), "renew_ok_bt", 400)
    main.fence_check()
    def down(key, sql):
        raise RuntimeError("down")
    monkeypatch.setattr(main.sb, "psql", down)
    import time as _t
    clock = iter(range(0, 1000, 30))
    monkeypatch.setattr(_t, "monotonic", lambda: next(clock))
    monkeypatch.setattr(_t, "sleep", lambda s: None)
    assert main.recover_if_safe({"renewed": True}) is False
    assert main.fenced_path().exists()
    assert "docker start velaris-gateway" not in [" ".join(c) for c in main.calls]


def test_fencing_and_lifting_never_interleave(main, monkeypatch):
    """A lift waits for a fence in progress (and the other way round)."""
    import threading
    import time as _t
    order = []
    _stamp(main, main.lease_path(), "renew_ok_bt", 400)
    orig = main._fence_here

    def slow_fence(reason, replaced=False):
        order.append("fence-start")
        r = orig(reason, replaced=replaced)
        _t.sleep(0.3)                     # stands in for the 5 s before SIGKILL
        order.append("fence-end")
        return r
    monkeypatch.setattr(main, "_fence_here", slow_fence)
    t = threading.Thread(target=main.fence_check)
    t.start()
    _t.sleep(0.05)
    orig_rec = main._recover
    monkeypatch.setattr(main, "_recover", lambda reply: order.append("lift") or orig_rec(reply))
    main.recover_if_safe({"renewed": True})
    t.join()
    assert order == ["fence-start", "fence-end", "lift"]


# ── part (c): the standby's agent ────────────────────────────────────────────

@pytest.fixture
def standby(fo, monkeypatch):
    (fo.sb.STATE / "standby.json").write_text("{}")
    fo.write_conf({"node": "b", "peer": "a", "witness_target": "w@x", "witness_port": 22,
                   "lease": 30, "margin": 5, "term": 1, "holding": False})
    clock = {"t": 1000.0}
    monkeypatch.setattr(fo, "boottime", lambda: clock["t"])
    monkeypatch.setattr(fo, "boot_id", lambda: "boot-s")
    monkeypatch.setattr(fo, "_log", lambda *a, **k: None)
    monkeypatch.setattr(fo.sb, "PROMOTED", fo.sb.STATE / "promoted.json")
    monkeypatch.setattr(fo.sb, "TUNNEL_ENV", fo.sb.STATE / "tunnel.env")
    state = {"main": {"ok": True, "term": 1, "fenced": False, "lsn": {"velaris": "0/100", "temporal": "0/100"}},
             "replay": {"velaris": "0/100", "temporal": "0/100"}, "witness": [], "promoted": 0,
             "grant": {"ok": True, "granted": True, "term": 2, "holder": "b"}, "preflight": None,
             "main_alive_at_witness": None, "holder_sync": None}
    def hb():
        if state["main"] and not state["main"].get("fenced"):
            state["main_seen_at"] = clock["t"]      # a live main also renews at the witness
                                                    # (a fenced one fenced BECAUSE it could not)
        return state["main"] if state["main"] and not state["main"].get("fenced") else None
    monkeypatch.setattr(fo, "heartbeat_main", hb)
    monkeypatch.setattr(fo, "replay_positions", lambda: state["replay"])

    def wit(req, **k):
        state["witness"].append(req)
        if req.startswith("acquire"):
            return state["grant"]
        held = clock["t"] - state.get("main_seen_at", clock["t"]) if state.get("main_alive_at_witness") is None \
            else state["main_alive_at_witness"]
        return {"ok": True, "term": 1, "holder": "a", "paused": None, "holder_silent_for": held,
                "holder_sync": state.get("holder_sync")}
    monkeypatch.setattr(fo, "witness", wit)

    def preflight(_p):
        if state["preflight"]:
            raise fo.sb.Refused(state["preflight"])
    monkeypatch.setattr(fo.sb, "_promote_preflight", preflight)
    monkeypatch.setattr(fo.sb, "cmd_promote", lambda a: state.__setitem__("promoted", state["promoted"] + 1) or 0)
    monkeypatch.setattr(fo, "run_hook", lambda info, path=None: state.__setitem__("hook_path", path)
                        or {"ran": False, "why": "test"})
    fo.state, fo.clock = state, clock
    return fo


def _run(fo, seconds, step=5):
    out = None
    for _ in range(int(seconds / step)):
        fo.clock["t"] += step
        out = fo.agent_step()
    return out


def test_a_standby_that_reaches_its_main_never_asks_for_the_lease(standby):
    out = _run(standby, 300)
    assert out["role"] == "standby" and out["main"] == "reached"
    assert not any(r.startswith("acquire") for r in standby.state["witness"]) and standby.state["promoted"] == 0


def test_main_lost_less_than_a_lease_no_takeover(standby):
    _run(standby, 60)
    standby.state["main"] = None
    out = _run(standby, 25)
    assert "takeover" not in out and not any(r.startswith("acquire") for r in standby.state["witness"])


def test_main_lost_a_whole_lease_and_caught_up_takes_over_once(standby):
    _run(standby, 60)
    standby.state["main"] = None
    out = _run(standby, 30)
    assert out["takeover"]["term"] == 2 and standby.state["promoted"] == 1
    conf = standby.read_conf()
    assert conf["holding"] is True and conf["term"] == 2
    assert [r for r in standby.state["witness"] if r.startswith("acquire")] == ["acquire 1"]


def test_a_fenced_main_counts_as_lost(standby):
    _run(standby, 60)
    standby.state["main"] = {**standby.state["main"], "fenced": True}
    out = _run(standby, 30)
    assert standby.state["promoted"] == 1 and out["takeover"]["term"] == 2


def test_the_loss_guard_holds_back_a_standby_that_is_behind(standby):
    _run(standby, 60)                                          # main at 0/100 all along …
    standby.state["main"]["lsn"] = {"velaris": "0/900", "temporal": "0/100"}
    _run(standby, 30)                                          # … then it moved on
    standby.state["main"] = None
    standby.state["replay"] = {"velaris": "0/100", "temporal": "0/100"}   # we never got it
    out = _run(standby, 35)
    assert out["takeover"].startswith("NOT taking over") and "velaris replica" in out["takeover"]
    assert not any(r.startswith("acquire") for r in standby.state["witness"]) and standby.state["promoted"] == 0


def test_the_loss_guard_needs_something_to_measure_against(standby):
    standby.state["main"] = None                               # never heard the main this boot
    out = _run(standby, 40)
    assert out["takeover"].startswith("NOT taking over") and "no recent word" in out["takeover"]
    assert standby.state["promoted"] == 0


def test_a_promote_that_would_fail_is_not_started_and_the_lease_not_taken(standby):
    _run(standby, 60)
    standby.state["main"], standby.state["preflight"] = None, "the main runs 3.1.3, this standby 3.1.2"
    out = _run(standby, 30)
    assert "a promote would fail here" in out["takeover"]
    assert not any(r.startswith("acquire") for r in standby.state["witness"]) and standby.state["promoted"] == 0


def test_a_witness_refusal_stops_the_takeover(standby):
    _run(standby, 60)
    standby.state["main"] = None
    standby.state["grant"] = {"ok": True, "granted": False, "reason": "failover is paused"}
    out = _run(standby, 30)
    assert out["takeover"] == "refused by the witness: failover is paused" and standby.state["promoted"] == 0
    assert standby.read_conf()["holding"] is False


def test_the_standby_learns_the_current_term_from_the_witness(standby, monkeypatch):
    def wit(req, **k):
        return {"ok": True, "term": 7, "holder": "a"}
    monkeypatch.setattr(standby, "witness", wit)
    _run(standby, 5)
    assert standby.read_conf()["term"] == 7


def test_after_taking_over_the_new_main_renews_as_holder(standby, monkeypatch):
    _run(standby, 60)
    standby.state["main"] = None
    _run(standby, 30)
    standby.sb.PROMOTED.write_text('{"stage": "done"}')        # what promote leaves behind
    seen = []
    monkeypatch.setattr(standby, "witness", lambda req, **k: seen.append(req) or {"ok": True, "renewed": True, "term": 2})
    out = standby.agent_step()
    assert out["role"] == "holder" and seen == ["renew 2"]
    assert out["check"]["fence"] is False                       # no standby yet: never fences


def test_a_replaced_main_stops_holding(main, monkeypatch):
    main.write_conf({**main.read_conf(), "holding": True})
    monkeypatch.setattr(main, "witness", lambda req, **k: {"ok": True, "renewed": False, "fence": True,
                                                           "term": 2, "holder": "b"})
    main.agent_step()
    assert main.read_conf()["holding"] is False


@pytest.mark.parametrize("problem", ["relative", "group_writable", "not_executable", "missing"])
def test_an_unsafe_hook_is_never_run(fo, tmp_path, monkeypatch, problem):
    hook = tmp_path / "hook.sh"
    hook.write_text("#!/bin/sh\ntouch " + str(tmp_path / "ran") + "\n")
    hook.chmod(0o755)
    value = str(hook)
    if problem == "relative":
        value = "hook.sh"
    elif problem == "group_writable":
        hook.chmod(0o775)
    elif problem == "not_executable":
        hook.chmod(0o644)
    elif problem == "missing":
        value = str(tmp_path / "nope.sh")
    monkeypatch.setenv("VELARIS_FAILOVER_HOOK", value)
    monkeypatch.setattr(fo, "_log", lambda *a, **k: None)
    assert fo.run_hook({"term": 2})["ran"] is False and not (tmp_path / "ran").exists()


def test_a_safe_hook_runs_with_the_new_mains_details(fo, tmp_path, monkeypatch):
    hook = tmp_path / "hook.sh"
    out = tmp_path / "env.txt"
    hook.write_text(f"#!/bin/sh\nenv > {out}\n")
    hook.chmod(0o700)
    monkeypatch.setenv("VELARIS_FAILOVER_HOOK", str(hook))
    monkeypatch.setattr(fo, "_log", lambda *a, **k: None)
    r = fo.run_hook({"term": 2, "old_main": "old.example"})
    env = out.read_text()
    assert r["ran"] and r["exit"] == 0
    assert "VELARIS_TERM=2" in env and "VELARIS_OLD_MAIN=old.example" in env
    assert "VELARIS_FAILOVER_HOOK" not in env                   # a minimal environment, not ours


def test_the_hook_is_read_from_this_servers_env_now_not_at_agent_start(fo, tmp_path, monkeypatch):
    """Set after the agent started, it is still seen; and it is the standby's own
    .env that counts, read before the promote installs the main's (box test 2026-10-30)."""
    monkeypatch.delenv("VELARIS_FAILOVER_HOOK", raising=False)
    (tmp_path / ".env").write_text("VELARIS_FAILOVER_HOOK=/opt/move-users.sh\n")
    assert fo.hook_setting() == "/opt/move-users.sh"
    src = (ROOT / "scripts/standby/velaris_failover.py").read_text()
    take = src.split("def take_over")[1].split("\ndef ")[0]
    assert take.index("hook = hook_setting()") < take.index("sb.cmd_promote(")
    assert "run_hook(result, hook)" in take


def test_retire_clears_a_fence_mark(fo):
    src = (ROOT / "scripts/standby/velaris_standby.py").read_text()
    retire = src.split("def cmd_retire")[1].split("\ndef ")[0]
    assert 'STATE / "fenced.json"' in retire


# ── part (d): the gap check and the synchronous option ──────────────────────

def test_a_main_that_outlived_the_standbys_contact_is_not_taken_over(standby):
    """The async gap (found 2026-10-29): the standby lost the main at t, but the
    main went on renewing at the witness — alive, writing what never came here —
    and died much later. The loss guard alone measured only up to t."""
    _run(standby, 60)
    standby.state["main"] = None                 # the standby loses the main …
    standby.state["main_alive_at_witness"] = 13  # … the witness heard it 13 s ago, not 300
    out = _run(standby, 300)
    assert out["takeover"].startswith("NOT taking over") and "kept talking to the witness" in out["takeover"]
    assert not any(r.startswith("acquire") for r in standby.state["witness"]) and standby.state["promoted"] == 0


def test_a_synchronous_main_is_taken_over_without_the_loss_guards(standby):
    _run(standby, 60)
    standby.state["main"] = None
    standby.state["holder_sync"] = True
    standby.state["main_alive_at_witness"] = 13          # would fail the gap check …
    standby.state["replay"] = {"velaris": None, "temporal": None}   # … and the loss guard
    out = _run(standby, 30)
    assert out["takeover"]["term"] == 2 and standby.state["promoted"] == 1


def _sync_main(main, monkeypatch, db_state, peer_age, wanted=True):
    monkeypatch.setenv("VELARIS_STANDBY_SYNC", "on" if wanted else "off")
    state = {"db": db_state, "log": []}

    def psql(key, sql):
        if sql.startswith("SHOW synchronous_standby_names"):
            return '"velaris_standby"' if state["db"] else ""
        if "ALTER SYSTEM SET synchronous_standby_names" in sql:
            state["db"] = "''" not in sql
            state["log"].append(("db", state["db"]))
        return "0/1"
    monkeypatch.setattr(main.sb, "psql", psql)
    _stamp(main, main.peer_path(), "bt", main.clock["t"] - peer_age)

    def wit(req, **k):
        state["log"].append(("witness", req))
        return {"ok": True, "renewed": True, "term": 1}
    monkeypatch.setattr(main, "witness", wit)
    return state


def test_going_asynchronous_is_announced_to_the_witness_before_the_database_stops_waiting(main, monkeypatch):
    st = _sync_main(main, monkeypatch, db_state=True, peer_age=30)     # standby lost
    out = main.agent_step()
    assert st["log"][:3] == [("witness", "renew 1 async"), ("db", False), ("db", False)] and out["sync"] == "dropped"


def test_going_synchronous_makes_the_database_wait_before_the_witness_hears_it(main, monkeypatch):
    st = _sync_main(main, monkeypatch, db_state=False, peer_age=1)     # standby heard
    out = main.agent_step()
    # Both databases wait first, then the witness is told.
    assert st["log"][:3] == [("db", True), ("db", True), ("witness", "renew 1 sync")] and out["sync"] == "raised"


def test_a_witness_that_cannot_hear_async_keeps_the_database_waiting(main, monkeypatch):
    st = _sync_main(main, monkeypatch, db_state=True, peer_age=30)
    calls = []

    def old_witness(req, **k):
        calls.append(req)
        return {"ok": False, "error": "refused"} if req.endswith(("sync", "async")) else {"ok": True, "renewed": True, "term": 1}
    monkeypatch.setattr(main, "witness", old_witness)
    out = main.agent_step()
    assert out["witness_too_old"] and st["db"] is True            # never dropped unannounced
    assert calls == ["renew 1 async", "renew 1"]


def test_without_the_setting_nothing_changes(main, monkeypatch):
    st = _sync_main(main, monkeypatch, db_state=False, peer_age=1, wanted=False)
    main.agent_step()
    assert st["log"] == [("witness", "renew 1")]                  # a plain renewal, no database change


def test_the_witness_keeps_the_holders_mode_and_forgets_it_on_a_grant(w, monkeypatch, capsys):
    assert ask(w, "a", "renew 1 sync", monkeypatch, capsys)["holder_sync"] is True
    assert ask(w, "a", "renew 1 async", monkeypatch, capsys)["holder_sync"] is False
    assert ask(w, "a", "renew 1", monkeypatch, capsys)["holder_sync"] is None
    assert ask(w, "b", "acquire 1 sync", monkeypatch, capsys) == {"ok": False, "error": "refused"}
    ask(w, "a", "renew 1 sync", monkeypatch, capsys)
    w.clock["t"] += 40
    assert ask(w, "b", "acquire 1", monkeypatch, capsys)["holder_sync"] is None


@pytest.mark.parametrize("agent, ready", [
    ('{"role": "main", "renewed": true}', True), ('{"role": "standby", "main": "reached"}', True),
    ('{"role": "holder", "renewed": true}', True), ('{"role": "other"}', True),
    ('{"role": "main", "witness_error": "timed out"}', False)])
def test_the_installer_arms_the_fence_check_only_once_the_agent_is_ready(tmp_path, agent, ready):
    """Found on the boxes 2026-10-29: a standby's agent says role "standby", which the
    readiness check did not know — the installer refused on every standby."""
    import subprocess
    src = (ROOT / "deploy/systemd/install-failover.sh").read_text()
    fn = src[src.index("_agent_ready() {"):]
    fn = fn[:fn.index("\n}\n") + 3]
    f = tmp_path / "agent.json"
    f.write_text(json.dumps(json.loads(agent), indent=2))
    r = subprocess.run(["bash", "-c", f'AGENT_JSON="{f}"\n{fn}\n_agent_ready'], capture_output=True)
    assert (r.returncode == 0) is ready
