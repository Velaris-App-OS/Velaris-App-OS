"""Velaris automatic failover — enable, status, pause, resume, disable (R5c, Nov_TODO step 12).

Step 11 linked a standby and gave the operator one command to promote it.
Step 12 lets the servers do that themselves, with a third machine — the
witness (`velaris_witness.py`) — holding a lease that decides which server is
in charge. This module is part (a): setting the witness up and talking to it.
The agents that renew the lease, fence the main and promote the standby are
parts (b) and (c); until they exist, enabling changes nothing on either server's
behaviour.

* Off by default. Nothing exists until `failover enable` runs on a linked main;
  an update never enables it.
* SSH only, as the link: each server has its own key to the witness, and on the
  witness that key can run `velaris_witness.py serve <node>` and nothing else.
* Each server keeps `.standby/failover/` (0700): its node name, its key, the
  witness's host keys and address.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import velaris_standby as sb  # noqa: E402

WITNESS_TOOL = HERE / "velaris_witness.py"
WITNESS_DIR = ".velaris-witness"          # on the witness, in its user's home
KEY_MARK = "velaris-failover-witness"
# The node names never change when roles swap: the witness knows servers, not
# roles. The main at `enable` is "a"; its standby "b".
NODE_MAIN, NODE_STANDBY = "a", "b"


def fdir() -> Path:
    return sb.STATE / "failover"


def conf_path() -> Path:
    return fdir() / "failover.json"


def key_path() -> Path:
    return fdir() / "witness_key"


def known_path() -> Path:
    return fdir() / "witness_known_hosts"


def _private() -> None:
    sb.private_dir()
    fdir().mkdir(mode=0o700, exist_ok=True)


def read_conf() -> dict:
    return json.loads(conf_path().read_text()) if conf_path().exists() else {}


def write_conf(c: dict) -> None:
    _private()
    sb.write_private(conf_path(), json.dumps(c, indent=2))


def new_key() -> str:
    _private()
    k = key_path()
    for f in (k, k.with_suffix(".pub")):
        f.unlink(missing_ok=True)
    sb.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C",
            f"{KEY_MARK}@{socket.gethostname()}", "-f", str(k)])
    return k.with_suffix(".pub").read_text().strip()


def parse_target(target: str) -> tuple[str, str]:
    user, _, host = target.rpartition("@")
    user = user or ""
    if (user and not sb.USER_RE.fullmatch(user)) or not sb.HOST_RE.fullmatch(host):
        raise sb.Refused(f"not a usable [user@]host: {target!r}")
    return user, host


# ── talking to the witness with this server's own key ────────────────────────

def witness(request: str, *, conf: dict | None = None, timeout: int = 10) -> dict:
    """One request with this server's key. Raises Refused when unreachable."""
    c = conf or read_conf()
    if not c:
        raise sb.Refused("failover is not enabled on this server")
    cmd = ["ssh", "-i", str(key_path()), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
           "-o", f"UserKnownHostsFile={known_path()}", "-o", "StrictHostKeyChecking=yes",
           "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=3", "-o", "ServerAliveCountMax=1",
           "-p", str(c["witness_port"]), c["witness_target"], request]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise sb.Refused("the witness did not answer in time") from None
    lines = [ln for ln in r.stdout.splitlines() if ln.startswith("{")]
    if not lines:
        raise sb.Refused(f"the witness gave no answer (exit {r.returncode}): {(r.stderr or '').strip()[-200:]}")
    return json.loads(lines[-1])


# ── the witness, set up over the operator's own SSH ──────────────────────────

class WitnessAdmin:
    """The operator's SSH session to the witness (multiplexed: one prompt)."""

    def __init__(self, target: str, port: int) -> None:
        self.target, self.port = target, port
        _private()
        self.base = ["ssh", "-p", str(port), "-o", "ConnectTimeout=15", "-o", "ControlMaster=auto",
                     "-o", f"ControlPath={fdir()}/cm-%C", "-o", "ControlPersist=60", target]

    def sh(self, script: str, data: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(self.base + [script], input=data, capture_output=True, text=True, timeout=120)

    def tool(self, cmd: str, payload: dict) -> dict:
        r = self.sh(f"python3 ~/{WITNESS_DIR}/velaris_witness.py {cmd}", json.dumps(payload))
        lines = [ln for ln in r.stdout.splitlines() if ln.startswith("{")]
        if not lines:
            raise sb.Refused(f"witness: `{cmd}` gave no answer: {(r.stderr or r.stdout).strip()[-300:]}")
        res = json.loads(lines[-1])
        if not res.get("ok"):
            raise sb.Refused(f"witness: {res.get('error')}")
        return res

    def close(self) -> None:
        subprocess.run(["ssh", "-o", f"ControlPath={fdir()}/cm-%C", "-O", "exit", self.target],
                       capture_output=True)


def witness_host_keys(adm: WitnessAdmin, host: str) -> str:
    """The witness's host keys, read through the operator's session — whose
    own known_hosts check vouches for them — never trusted on first use."""
    r = adm.sh("cat /etc/ssh/ssh_host_*_key.pub 2>/dev/null")
    name = host if adm.port == 22 else f"[{host}]:{adm.port}"
    lines = [f"{name} {p[0]} {p[1]}" for p in (ln.split() for ln in r.stdout.splitlines())
             if len(p) >= 2 and re.fullmatch(r"(ssh|ecdsa)-[a-z0-9-]+", p[0])]
    if not lines:
        raise sb.Refused("could not read the witness's SSH host keys (/etc/ssh) — is sshd installed?")
    return "\n".join(lines) + "\n"


def cmd_enable(a: argparse.Namespace) -> int:
    rec = json.loads(sb.MAIN_RECORD.read_text()) if sb.MAIN_RECORD.exists() else {}
    if not rec:
        raise sb.Refused("this server has no standby — `standby-velaris.sh link` first")
    if sb.DEMOTED.exists():
        raise sb.Refused("this server was replaced by its standby")
    if read_conf() and not a.again:
        raise sb.Refused("failover is already enabled (`failover status`; --again to set it up anew)")
    user, host = parse_target(a.witness)
    if sb._same_host(host, rec.get("standby", "")) or sb._same_host(host, socket.gethostname()):
        raise sb.Refused("the witness must be a third machine — not this server or its standby")
    if not (10 <= a.lease <= 600 and 1 <= a.margin <= 120):
        raise sb.Refused("--lease must be 10–600 s and --margin 1–120 s")
    st = sb.main_db_state()
    if not all(v["state"] == "streaming" for v in st.values()):
        raise sb.Refused("the standby is not streaming (`standby-velaris.sh status`) — a failover "
                         "onto it now would lose what it has not received")

    sb.say(f"→ Setting up the witness on {a.witness} …")
    adm = WitnessAdmin(a.witness, a.witness_port)
    remote = sb.Remote(rec["ssh_target"], rec.get("ssh_port", 22), rec["standby_dir"])
    try:
        # The standby first: if it cannot be reached, nothing on the witness is touched.
        standby_pub = remote.call("remote-failover-key")["pubkey"]
        r = adm.sh("command -v python3 >/dev/null && python3 -c 'import sys; print(sys.version_info >= (3, 8))'")
        if r.stdout.strip() != "True":
            raise sb.Refused("the witness needs python3 (3.8 or newer)")
        r = adm.sh(f"umask 077 && mkdir -p ~/{WITNESS_DIR} && cat > ~/{WITNESS_DIR}/velaris_witness.py",
                   WITNESS_TOOL.read_text())
        if r.returncode:
            raise sb.Refused(f"could not copy the witness tool: {r.stderr.strip()[-200:]}")
        known = witness_host_keys(adm, host)
        main_pub = new_key()
        stamp = boottime()            # setup starts this server's lease at the witness
        res = adm.tool("setup", {"holder": NODE_MAIN, "lease": a.lease, "margin": a.margin, "nodes": {
            NODE_MAIN: {"pubkey": main_pub, "label": re.sub(r"[^A-Za-z0-9@._:-]", "", socket.gethostname())[:60] or "main"},
            NODE_STANDBY: {"pubkey": standby_pub, "label": re.sub(r"[^A-Za-z0-9@._:-]", "", rec["standby"])[:60] or "standby"}}})
        target = f"{user}@{host}" if user else host
        conf = {"node": NODE_MAIN, "peer": NODE_STANDBY, "witness_target": target, "holding": True,
                "witness_port": a.witness_port, "lease": a.lease, "margin": a.margin,
                "term": res["state"]["term"], "enabled_at": sb.now()}
        _private()
        sb.write_private(known_path(), known)
        write_conf(conf)
        # The lease the witness just started, as the fence check reads it: an
        # agent started now has a whole lease to make its first renewal.
        _write(lease_path(), {"boot_id": boot_id(), "renew_ok_bt": stamp, "term": conf["term"],
                              "paused": None, "at": sb.now(), "by": "enable"})
        remote.call("remote-failover-set", {**conf, "node": NODE_STANDBY, "peer": NODE_MAIN,
                                            "holding": False, "known_hosts": known})
        # Both keys work before anything relies on them.
        mine = witness("state", conf=conf)
        theirs = remote.call("remote-failover-check")
        if not mine.get("ok") or not theirs.get("witness", {}).get("ok"):
            raise sb.Refused(f"a server cannot reach the witness with its key: {mine} / {theirs}")
    except Exception:
        # Half set up is worse than not set up: take both sides back.
        _undo(adm, remote)
        sb.say("! enable did not finish — automatic failover is OFF on these servers "
               "(nothing is left half set up). Fix the cause and run `failover enable` again.")
        raise
    finally:
        adm.close()
        remote.close()
    sb.hxdr_record("failover_enable", a.witness, {"lease": a.lease, "margin": a.margin})
    sb.say(f"✓ Witness set up on {a.witness}: this server holds the lease (term {conf['term']}, "
           f"lease {a.lease} s + {a.margin} s).")
    sb.say("  Next, on BOTH servers: `sudo ./deploy/systemd/install-failover.sh` (the agents). Until "
           "then nothing renews the lease and nothing fails over.")
    return 0


def _undo(adm: WitnessAdmin, remote) -> None:
    for step in (lambda: adm.tool("teardown", {}), lambda: remote.call("remote-failover-clear")):
        try:
            step()
        except Exception as e:  # noqa: BLE001
            sb.say(f"! undo: {e}")
    clear_local()


def clear_local() -> None:
    """Everything failover keeps here — except a fence mark, which only the
    agent (on a renewal) or the operator lifts."""
    for f in (conf_path(), key_path(), key_path().with_suffix(".pub"), known_path(),
              lease_path(), peer_path(), agent_path(), watch_path(), fdir() / "taking-over"):
        f.unlink(missing_ok=True)


def cmd_status(a: argparse.Namespace) -> int:
    c = read_conf()
    if not c:
        out = {"enabled": False}
    else:
        try:
            w = witness("state", conf=c)
            reached = True
        except sb.Refused as e:
            w, reached = {"error": str(e)}, False
        out = {"enabled": True, "node": c["node"], "witness": c["witness_target"],
               "witness_reached": reached, "holds_lease": reached and w.get("holder") == c["node"],
               **({k: w.get(k) for k in ("term", "holder", "paused", "holder_silent_for", "lease",
                                         "margin", "last_seen")} if reached else {"error": w["error"]})}
    if out["enabled"]:
        ag = _read(agent_path())
        out["agent"] = {k: ag.get(k) for k in ("at", "renewed", "witness_error", "check", "error") if k in ag}
        out["fenced"] = _read(fenced_path()) or None
        if is_main():
            now_ = boottime()
            out["lease_age"] = round(age(_read(lease_path()), "renew_ok_bt", now_), 1)
            out["standby_heartbeat_age"] = round(age(_read(peer_path()), "bt", now_), 1)
            out["fence_after"] = fence_after(c)
    if a.json:
        print(json.dumps(out, indent=2))
        return 0
    if not out["enabled"]:
        sb.say("Automatic failover: off (`failover enable --witness [user@]host` on a linked main).")
        return 0
    sb.say(f"Automatic failover: on — witness {out['witness']}, this server is node {out['node']}")
    if not out["witness_reached"]:
        sb.say(f"  ! witness not reached: {out['error']}")
    else:
        sb.say(f"  term {out['term']}, lease held by {out['holder']}"
               f"{' (this server)' if out['holds_lease'] else ''}, last renewed {out['holder_silent_for']} s ago")
    if out.get("paused"):
        sb.say(f"  PAUSED since {out['paused']['at']} — {out['paused']['reason']} (`failover resume`)")
    if "lease_age" in out:
        def ago(path: Path, key: str, secs: float) -> str:
            st_ = _read(path)
            return f"{secs} s ago" if st_.get("boot_id") == boot_id() and key in st_ else "not since this boot"
        sb.say(f"  this server: lease confirmed {ago(lease_path(), 'renew_ok_bt', out['lease_age'])}, standby "
               f"heard {ago(peer_path(), 'bt', out['standby_heartbeat_age'])} — it stops itself when both "
               f"pass {out['fence_after']:.0f} s")
    if out.get("fenced"):
        sb.say(f"  ! FENCED since {out['fenced'].get('fenced_at')}: {out['fenced'].get('reason')}")
    if not out.get("agent"):
        sb.say("  ! no agent has run here — `sudo deploy/systemd/install-failover.sh`")
    return 0 if out["witness_reached"] and not out.get("fenced") else 3


def cmd_pause(a: argparse.Namespace) -> int:
    reason = re.sub(r"[^A-Za-z0-9 ._:,-]", "", a.reason or "maintenance")[:120] or "maintenance"
    r = witness(f"pause {reason}")
    if not r.get("ok"):
        raise sb.Refused(r.get("error") or "refused")
    sb.hxdr_record("failover_pause", read_conf().get("witness_target"), {"reason": reason})
    sb.say(f"✓ Failover paused ({reason}). The standby will not take over until `failover resume`.")
    return 0


def cmd_resume(_a: argparse.Namespace) -> int:
    r = witness("resume")
    if not r.get("ok"):
        raise sb.Refused(r.get("error") or "refused")
    sb.hxdr_record("failover_resume", read_conf().get("witness_target"), {})
    sb.say("✓ Failover resumed.")
    return 0


def cmd_disable(a: argparse.Namespace) -> int:
    c = read_conf()
    if not c:
        sb.say("Automatic failover is not enabled here.")
        return 0
    rec = json.loads(sb.MAIN_RECORD.read_text()) if sb.MAIN_RECORD.exists() else {}
    notes = []
    if not a.local_only:
        adm = WitnessAdmin(c["witness_target"], c["witness_port"])
        try:
            adm.tool("teardown", {})
        except Exception as e:  # noqa: BLE001
            notes.append(f"witness not cleared ({e}) — on it, remove the `{KEY_MARK}` lines from "
                         f"~/.ssh/authorized_keys and ~/{WITNESS_DIR}")
        finally:
            adm.close()
        if rec:
            remote = sb.Remote(rec["ssh_target"], rec.get("ssh_port", 22), rec["standby_dir"])
            try:
                remote.call("remote-failover-clear")
            except Exception as e:  # noqa: BLE001
                notes.append(f"standby not cleared ({e}) — on it: `standby-velaris.sh remote-failover-clear`")
            finally:
                remote.close()
    clear_local()
    sb.hxdr_record("failover_disable", c.get("witness_target"), {"local_only": a.local_only, "notes": notes})
    for n in notes:
        sb.say(f"! {n}")
    sb.say("✓ Automatic failover disabled on this server." + (" (with the notes above)" if notes else ""))
    return 1 if notes else 0


# ── part (b): the main's agent, the fence check, the heartbeat ─────────────
#
# The rule (design decision 3): a main that has had NO lease renewal at the
# witness AND NO heartbeat from its standby for `fence_after` seconds (half the
# lease) stops writing — it cannot know that the standby has not taken over.
# A main that reaches either one keeps running. Times are CLOCK_BOOTTIME: an NTP
# step cannot stretch a deadline, and a stamp from before a reboot counts as
# "at boot" — after booting, a main writes only once it has been confirmed.

RENEW_EVERY = 5


def boottime() -> float:
    import time
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return "unknown"


def lease_path() -> Path:
    return fdir() / "lease.json"        # main: the last renewal the witness accepted


def peer_path() -> Path:
    return fdir() / "peer.json"         # main: the standby's last heartbeat


def agent_path() -> Path:
    return fdir() / "agent.json"        # either: what the agent last did


def fenced_path() -> Path:
    return sb.STATE / "fenced.json"     # start/update/setup/restore/units refuse while it exists


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _write(path: Path, d: dict) -> None:
    _private()
    sb.write_private(path, json.dumps(d, indent=2))


def age(stamp: dict, key: str, now: float | None = None) -> float:
    """Seconds since `stamp[key]` on this boot's clock; a stamp from another
    boot (or none) counts as taken at boot — i.e. the time since booting."""
    now = boottime() if now is None else now
    if stamp.get("boot_id") == boot_id() and isinstance(stamp.get(key), (int, float)):
        return max(0.0, now - stamp[key])
    return now


def is_main() -> bool:
    """A main with a linked standby — the only server that may fence itself.
    A main without a standby (just promoted, or unlinked) has no one to be
    replaced by, so it never fences (design decision 6)."""
    return (sb.MAIN_RECORD.exists() and not sb.DEMOTED.exists()
            and not (sb.STATE / "standby.json").exists())


def fence_after(conf: dict) -> float:
    return max(5.0, float(conf.get("lease", 30)) / 2)


def _log(msg: str, prio: str = "user.warning") -> None:
    subprocess.run(["logger", "-p", prio, "-t", "velaris-failover", msg], capture_output=True)


def _db_containers() -> list[str]:
    out = []
    for key in sb.DBS:
        try:
            cid = sb.container_of(sb.DBS[key][0])
        except Exception:  # noqa: BLE001
            cid = None
        if cid:
            out.append(cid)
    return out


class _FenceLock:
    """Fencing and lifting a fence never interleave (found 2026-10-29 on the
    boxes): a fence waiting its 5 s before SIGKILL, and a renewal lifting it
    meanwhile, killed the databases the lift had just restarted — gateway up,
    databases dead, no mark. One lock, held through each whole procedure."""

    def __enter__(self):
        import fcntl
        _private()
        self.fd = os.open(fdir() / "fence.lock", os.O_WRONLY | os.O_CREAT, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *_exc):
        import fcntl
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)


def fence_here(reason: str, *, replaced: bool = False) -> dict:
    with _FenceLock():
        return _fence_here(reason, replaced=replaced)


def _fence_here(reason: str, *, replaced: bool = False) -> dict:
    """Stop this server writing, now. The mark goes first (nothing may start
    the platform again in between), then users lose the door (gateway, killed),
    then the databases: SIGINT (PostgreSQL's fast shutdown), SIGKILL after 5 s.
    Killed containers keep `unless-stopped` from bringing them back, also
    across a reboot. A `replaced` main (the witness gave the lease to the
    standby) is also marked demoted: it rejoins only as a standby."""
    import time
    if fenced_path().exists() and not replaced:
        return _read(fenced_path())
    rec = _read(fenced_path()) or {"fenced_at": sb.now(), "reason": reason, "term": read_conf().get("term")}
    dbs = _db_containers()
    rec["containers"] = sorted(set(rec.get("containers", [])) | {"velaris-gateway", *dbs})
    rec["replaced"] = replaced or rec.get("replaced", False)
    sb.write_private(fenced_path(), json.dumps(rec, indent=2))
    if replaced and not sb.DEMOTED.exists():
        sb.write_private(sb.DEMOTED, json.dumps({"demoted_at": sb.now(), "by": "failover witness",
                                                 "reason": reason}, indent=2))
    _log(f"FENCED: {reason}", "user.crit")
    subprocess.run(["docker", "kill", "velaris-gateway"], capture_output=True)
    for cid in dbs:
        subprocess.run(["docker", "kill", "-s", "INT", cid], capture_output=True)
    end = time.monotonic() + 5
    while dbs and time.monotonic() < end:
        running = [c for c in dbs if subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", c],
                                                    capture_output=True, text=True).stdout.strip() == "true"]
        if not running:
            break
        time.sleep(0.3)
    else:
        for cid in dbs:
            subprocess.run(["docker", "kill", cid], capture_output=True)
    return rec


def recover_if_safe(reply: dict) -> bool:
    """A fenced main that the witness has just renewed — same term, still the
    holder — may write again: the witness has granted no one, and the renewal
    restarts the lease, so no one can be granted for lease + margin. Only a
    fence this agent set on a lost lease is lifted; a `replaced` one never."""
    with _FenceLock():
        return _recover(reply)


def _recover(reply: dict) -> bool:
    rec = _read(fenced_path())
    if not rec or rec.get("replaced") or not reply.get("renewed"):
        return False
    import time
    names = [c for c in rec.get("containers", []) if c != "velaris-gateway"]
    for c in names:
        subprocess.run(["docker", "start", c], capture_output=True)
    end = time.monotonic() + 60
    for key in sb.DBS:
        while True:
            try:
                sb.psql(key, "SELECT 1")
                break
            except Exception:  # noqa: BLE001
                if time.monotonic() > end:
                    # Users get no door to a platform without its database: the
                    # fence stays, the next renewal tries again.
                    _log(f"fence kept: {key} database did not come back within 60 s")
                    return False
                time.sleep(1)
    subprocess.run(["docker", "start", "velaris-gateway"], capture_output=True)
    fenced_path().unlink(missing_ok=True)
    _log("fence lifted: the witness renewed this server's lease (same term)", "user.notice")
    return True


def fence_check() -> dict:
    """The rule, applied once. Run by the agent each loop AND by its own timer,
    so a crashed or hung agent still fences."""
    conf = read_conf()
    if not conf or not is_main():
        return {"fence": False, "why": "not a main with failover enabled"}
    if fenced_path().exists():
        return {"fence": True, "why": "already fenced"}
    lease = _read(lease_path())
    if lease.get("paused"):
        # A pause makes the witness refuse every grant until the holder (this
        # server) resumes — no one can take over, so there is nothing to fence for.
        return {"fence": False, "why": "paused"}
    now = boottime()
    w, p, f = age(lease, "renew_ok_bt", now), age(_read(peer_path()), "bt", now), fence_after(conf)
    if w > f and p > f:
        fence_here(f"no lease renewal for {w:.0f} s and no standby heartbeat for {p:.0f} s (limit {f:.0f} s)")
        return {"fence": True, "why": "fenced now", "witness_age": w, "peer_age": p}
    return {"fence": False, "witness_age": round(w, 1), "peer_age": round(p, 1), "limit": f}


# ── the synchronous option (decision 5, part d) ─────────────────────────────
#
# VELARIS_STANDBY_SYNC=on (main): commits wait until the standby has the WAL
# (`synchronous_standby_names`), so a takeover loses nothing. If the standby is
# not heard for SYNC_DROP_SECONDS the main drops to asynchronous — or every
# commit would hang — and it TELLS THE WITNESS FIRST: a standby whose main last
# said "async" applies the loss guards instead of assuming zero loss. Unset or
# off = asynchronous, exactly as before.

SYNC_DROP_SECONDS = 10.0


def sync_wanted() -> bool:
    return sb.setting("VELARIS_STANDBY_SYNC", "off").strip().lower() in ("on", "true", "1", "yes")


def db_sync() -> bool | None:
    """Whether the main's databases make commits wait for the standby (None = unreadable)."""
    try:
        vals = [sb.psql(k, "SHOW synchronous_standby_names") for k in sb.DBS]
    except Exception:  # noqa: BLE001
        return None
    return all(v.strip() for v in vals)


def set_db_sync(on: bool) -> None:
    value = f"'\"{sb.APP_NAME}\"'" if on else "''"
    for k in sb.DBS:
        sb.psql(k, f"ALTER SYSTEM SET synchronous_standby_names = {value};\nSELECT pg_reload_conf();")
    _log(f"commits now {'wait for the standby (synchronous)' if on else 'do not wait for the standby (asynchronous)'}",
         "user.notice")


def _renew(conf: dict, mode: str | None) -> dict | None:
    req = f"renew {int(conf['term'])}" + (f" {mode}" if mode else "")
    return witness(req, conf=conf, timeout=RENEW_EVERY - 1)


def renew_with_mode(conf: dict, out: dict) -> dict | None:
    """Renew, keeping the database's commit mode and what the witness was told
    in step. Order is the safety: to go asynchronous the witness hears it
    BEFORE the database stops waiting; to go synchronous the database waits
    BEFORE the witness hears it. Without the setting: a plain renewal (and a
    database left synchronous by an earlier setting is turned back, announced)."""
    # Read the database's mode only when it can matter (the setting is on, or
    # was on last time): with the option off nothing extra runs each loop.
    last = _read(lease_path()).get("db_sync")
    current = (db_sync() if (sync_wanted() or last) else False) if is_main() else None
    out["synchronous"] = current
    want = sync_wanted() and is_main() and age(_read(peer_path()), "bt") <= SYNC_DROP_SECONDS
    if current is None:
        return _renew(conf, None)
    if current and not want:
        reply = _renew(conf, "async")
        if reply and reply.get("renewed"):
            set_db_sync(False)
            out["sync"] = "dropped" if sync_wanted() else "off"
        elif reply and reply.get("ok") is False:
            out["witness_too_old"] = True          # cannot announce it: stay synchronous, say so
            reply = _renew(conf, None)
        return reply
    if want and not current:
        set_db_sync(True)
        reply = _renew(conf, "sync")
        if reply and reply.get("ok") is False:     # a witness tool from before part (d)
            set_db_sync(False)
            out["witness_too_old"] = True
            return _renew(conf, None)
        out["sync"] = "raised"
        return reply
    if not sync_wanted() and not current:
        return _renew(conf, None)
    reply = _renew(conf, "sync" if current else "async")
    if reply and reply.get("ok") is False:
        out["witness_too_old"] = True
        return _renew(conf, None)
    return reply


def agent_step() -> dict:
    """One loop of the agent on a main: renew, then apply the rule."""
    conf = read_conf()
    if conf and is_standby():
        return standby_step()
    out: dict = {"at": sb.now(), "role": "main" if is_main() else ("holder" if conf.get("holding") else "other")}
    # A main renews; so does a promoted server holding the lease without a
    # standby yet (it cannot fence — nothing could replace it — but the
    # witness then shows who holds it).
    if not conf or not (is_main() or conf.get("holding")):
        return out
    send = boottime()                   # the lease counts from BEFORE the request left
    try:
        reply = renew_with_mode(conf, out)
    except sb.Refused as e:
        reply, out["witness_error"] = None, str(e)
    if reply and reply.get("renewed"):
        _write(lease_path(), {"boot_id": boot_id(), "renew_ok_bt": send, "term": reply.get("term"),
                              "paused": reply.get("paused"), "at": sb.now(),
                              "db_sync": {"raised": True, "dropped": False, "off": False}.get(
                                  out.get("sync"), out.get("synchronous"))})
        out["renewed"] = True
        if recover_if_safe(reply):
            out["recovered"] = True
    elif reply and reply.get("fence"):
        # The witness gave the lease to the standby: it is the main now.
        if is_main() or fenced_path().exists():
            # Also when already fenced (and perhaps demoted by the new main first):
            # the mark must say "replaced", so nothing suggests it may restart.
            fence_here(f"the witness gave the lease to {reply.get('holder')} (term {reply.get('term')})",
                       replaced=True)
        conf["holding"] = False
        write_conf(conf)
        out["replaced"] = True
    out["check"] = fence_check()
    return out


def cmd_agent(_a: argparse.Namespace) -> int:
    """velaris-failover-agent.service: every RENEW_EVERY seconds until disabled."""
    import time
    _log("agent started", "user.notice")
    while read_conf():
        t0 = time.monotonic()
        try:
            step = agent_step()
        except Exception as e:  # noqa: BLE001 — the loop must not die; the fence timer is the backstop
            step = {"at": sb.now(), "error": f"{type(e).__name__}: {e}"}
        _write(agent_path(), step)
        time.sleep(max(0.5, RENEW_EVERY - (time.monotonic() - t0)))
    _log("agent stopped: failover disabled", "user.notice")
    return 0


def cmd_fence_check(_a: argparse.Namespace) -> int:
    r = fence_check()
    if r.get("fence"):
        print(json.dumps(r))
    return 0


def heartbeat() -> dict:
    """The main's answer to its standby's heartbeat (over the standby's step 11
    key, `serve`): it is recorded — a main that hears its standby is not cut
    off — and the standby learns this server's term, whether it is fenced,
    and where its WAL is (for the loss guard, part c)."""
    conf = read_conf()
    if not conf:
        return {"ok": False, "error": "failover not enabled"}
    _write(peer_path(), {"boot_id": boot_id(), "bt": boottime(), "at": sb.now()})
    lsn = {}
    if not fenced_path().exists():
        for key in sb.DBS:
            try:
                lsn[key] = sb.psql(key, "SELECT pg_current_wal_lsn()")
            except Exception:  # noqa: BLE001
                lsn[key] = None
    return {"ok": True, "term": conf.get("term"), "fenced": fenced_path().exists(), "lsn": lsn,
            "at": sb.now()}


# ── part (c): the standby's agent ────────────────────────────────────────────
#
# Each loop the standby sends the main a heartbeat over its step 11 key. The main
# answers with its WAL position; the standby keeps a minute or two of those
# (`ring`). It takes over only when:
#   1. it has not reached a writing main for a whole lease (its own clock), AND
#   2. the witness grants `acquire` (the main has been silent there for lease +
#      margin — by then the main has fenced itself: decision 3), AND
#   3. the loss guard holds: its replicas have replayed everything the main had
#      written `max_lag` seconds before the last contact (decision 5), AND
#   4. a promote here would not stop halfway (the same preflight `promote` runs).
# Then it promotes with the existing step 11 promote, records it, and runs the
# operator's hook. 1, 3 and 4 are checked BEFORE asking the witness: a lease
# taken by a standby that then cannot promote leaves no main at all.

RING_SECONDS = 180


def watch_path() -> Path:
    return fdir() / "watch.json"       # standby: last contact, the main's WAL positions


def is_standby() -> bool:
    return (sb.STATE / "standby.json").exists() and not sb.PROMOTED.exists()


def max_lag() -> float:
    try:
        return max(0.0, float(sb.setting("VELARIS_FAILOVER_MAX_LAG_SECONDS", "10")))
    except ValueError:
        return 10.0


def _lsn(v) -> int | None:
    try:
        return sb._lsn(v) if isinstance(v, str) and "/" in v else None
    except ValueError:
        return None


def replay_positions() -> dict:
    out = {}
    for key in sb.DBS:
        try:
            out[key] = sb._replica_sql(key, "SELECT pg_last_wal_replay_lsn()", check=False) or None
        except Exception:  # noqa: BLE001
            out[key] = None
    return out


def loss_guard(watch: dict, replay: dict, limit: float) -> tuple[bool, str]:
    """Holds when the replicas have replayed what the main had written `limit`
    seconds before the last contact. Writes after that point — at most `limit`
    plus one heartbeat interval — are what an automatic promote may lose."""
    ring = [e for e in watch.get("ring", []) if isinstance(e, list) and len(e) == 2]
    contact = watch.get("contact_bt")
    if not ring or contact is None:
        return False, "no recent word from the main (nothing to measure the loss against)"
    target = contact - limit
    older = [e for e in ring if e[0] <= target]
    point = older[-1] if older else None
    if point is None:
        if ring[0][0] > target and contact - ring[0][0] < limit:
            # Heard for less than `limit` seconds in all: measure against the first word.
            point = ring[0]
        else:
            return False, "the main's positions before the cut are not known"
    for key, want in (point[1] or {}).items():
        w, have = _lsn(want), _lsn(replay.get(key))
        if w is None:
            return False, f"the main did not report its {key} position"
        if have is None:
            return False, f"the {key} replica does not answer"
        if have < w:
            return False, (f"the {key} replica is more than {limit:.0f} s behind what the main had "
                           f"(replayed {replay.get(key)}, the main had {want})")
    return True, "ok"


def heartbeat_main() -> dict | None:
    """The main's answer, or None when there is no writing main to talk to."""
    try:
        r = sb.ssh_main("failover heartbeat", timeout=RENEW_EVERY - 1)
    except Exception:  # noqa: BLE001 — no tunnel record, timeout, ssh missing
        return None
    line = next((ln for ln in r.stdout.decode(errors="replace").splitlines() if ln.startswith("{")), "")
    try:
        ans = json.loads(line) if r.returncode == 0 and line else None
    except ValueError:
        ans = None
    if not ans or not ans.get("ok") or ans.get("fenced"):
        return None                      # a fenced main does not count as reached
    return ans


def hook_setting() -> str:
    """VELARIS_FAILOVER_HOOK as this server's .env says it NOW. Read from disk,
    not from the settings the agent loaded when it started: a hook set later
    must be seen without restarting the agent. take_over reads it BEFORE the
    promote, which installs the main's .env in place of this one."""
    return (os.environ.get("VELARIS_FAILOVER_HOOK")
            or sb.read_env(sb.ROOT / ".env").get("VELARIS_FAILOVER_HOOK") or "").strip()


def run_hook(info: dict, path: str | None = None) -> dict:
    """VELARIS_FAILOVER_HOOK: the operator's own script that moves users (DNS,
    keepalived, a floating IP). Run only if it is an absolute path to a regular,
    executable file that no one but its owner (this user or root) can change.
    Its outcome never undoes the promote."""
    import stat
    path = (hook_setting() if path is None else path).strip()
    if not path:
        return {"ran": False, "why": "no hook set (VELARIS_FAILOVER_HOOK)"}
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return {"ran": False, "why": f"{path} not found"}
    bad = (not p.is_absolute() or not stat.S_ISREG(st.st_mode) or not os.access(p, os.X_OK)
           or st.st_uid not in (0, os.getuid()) or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
           or p.parent.stat().st_mode & stat.S_IWOTH)
    if bad:
        _log(f"failover hook NOT run: {path} must be an absolute, executable file only its owner can change")
        return {"ran": False, "why": "unsafe hook path (absolute, executable, owned by this user or root, "
                                     "not group/other-writable)"}
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(Path.home()),
           "VELARIS_NEW_MAIN": socket.gethostname(), "VELARIS_TERM": str(info.get("term")),
           "VELARIS_OLD_MAIN": str(info.get("old_main") or ""), "VELARIS_DIR": str(sb.ROOT)}
    try:
        r = subprocess.run([str(p)], env=env, capture_output=True, text=True, timeout=120)
        out = {"ran": True, "exit": r.returncode, "output": (r.stdout + r.stderr)[-500:]}
    except subprocess.TimeoutExpired:
        out = {"ran": True, "exit": None, "output": "timed out after 120 s"}
    _log(f"failover hook ran: exit {out['exit']}", "user.notice" if out["exit"] == 0 else "user.warning")
    return out


def standby_step() -> dict:
    """One loop of the agent on a standby."""
    conf = read_conf()
    out: dict = {"at": sb.now(), "role": "standby"}
    watch = _read(watch_path())
    if watch.get("boot_id") != boot_id():
        watch = {"boot_id": boot_id(), "ring": []}
    ans = heartbeat_main()
    now = boottime()
    if ans:
        # Measured from when the answer came BACK: the main heard us no later
        # than this, so our wait starts no earlier than its own (decision 4).
        watch["contact_bt"] = now
        watch["ring"] = [e for e in watch.get("ring", []) if now - e[0] <= RING_SECONDS] + [[now, ans.get("lsn") or {}]]
        out["main"] = "reached"
    else:
        out["main"] = "not reached"
    _write(watch_path(), watch)
    try:
        w = witness("seen", conf=conf, timeout=RENEW_EVERY - 1)
        out["witness"] = {k: w.get(k) for k in ("term", "holder", "paused", "holder_silent_for", "holder_sync")}
        if w.get("term") and w.get("holder") != conf.get("node") and w["term"] != conf.get("term"):
            conf["term"] = w["term"]          # the term is public; ours must be current to acquire
            write_conf(conf)
    except sb.Refused as e:
        out["witness"] = {"error": str(e)}
    silent = age(watch, "contact_bt", now)
    out["main_silent_for"] = round(silent, 1)
    if silent < float(conf.get("lease", 30)):
        return out
    # 3. the loss guards, 4. a promote that will finish — before the witness.
    wit = out.get("witness") or {}
    if wit.get("holder_sync") is True:
        # The main's last word to the witness was "my commits wait for the
        # standby", and it must say "async" there BEFORE it stops waiting:
        # everything it committed is here.
        ok, why = True, "synchronous"
    else:
        ok, why = loss_guard(watch, replay_positions(), max_lag())
        held = wit.get("holder_silent_for")
        if ok and isinstance(held, (int, float)) and silent - held > max_lag() + RENEW_EVERY:
            # Found writing part (d), 2026-10-29: the main kept renewing at the
            # witness long after this standby last heard it — it was alive, and
            # writing what never came here. Measured on two clocks over the same
            # interval (now), so only their rates matter, not their settings.
            ok, why = False, (f"the main kept talking to the witness {silent - held:.0f} s after this "
                              "standby last heard it — it may have written what is not here")
    if not ok:
        out["takeover"] = f"NOT taking over: {why}"
        _log(f"main lost {silent:.0f} s; not taking over: {why}")
        return out
    try:
        sb._promote_preflight(sb.STATE / sb.MAIN_CONFIG_COPY_NAME)
    except sb.Refused as e:
        out["takeover"] = f"NOT taking over: a promote would fail here — {e}"
        _log(out["takeover"])
        return out
    # 2. the witness decides.
    try:
        g = witness(f"acquire {int(conf['term'])}", conf=conf, timeout=RENEW_EVERY - 1)
    except sb.Refused as e:
        out["takeover"] = f"witness not reached: {e}"
        return out
    if not g.get("granted"):
        out["takeover"] = f"refused by the witness: {g.get('reason')}"
        return out
    conf.update(term=g["term"], holding=True, acquired_at=sb.now())
    write_conf(conf)
    _log(f"lease acquired (term {g['term']}) — promoting this standby", "user.crit")
    out["takeover"] = take_over(conf, watch)
    return out


def take_over(conf: dict, watch: dict) -> dict:
    """The lease is ours: promote (step 11's promote, main down), record, hook."""
    old = sb.read_env(sb.TUNNEL_ENV).get("MAIN_HOST")
    result: dict = {"term": conf["term"], "old_main": old, "started_at": sb.now()}
    # A copy running now must stop at its next step: the promote waits for it.
    _write(sb.taking_over_path(), {"at": sb.now(), "term": conf["term"]})
    hook = hook_setting()                # before the promote replaces this server's .env
    try:
        rc = sb.cmd_promote(argparse.Namespace(main_is_down=True, yes=True, accept_loss=False, no_start=False))
        result["promote_exit"] = rc
    except Exception as e:  # noqa: BLE001
        # The lease is held and the old main stays fenced: say so loudly. A
        # promote stopped part-way is finished by running it again.
        result["promote_error"] = f"{type(e).__name__}: {e}"
        _log(f"AUTOMATIC PROMOTE FAILED: {e} — finish it with `standby-velaris.sh promote "
             "--main-is-down --yes`", "user.crit")
    result["hook"] = run_hook(result, hook) if "promote_error" not in result else {"ran": False, "why": "promote failed"}
    result["finished_at"] = sb.now()
    sb.taking_over_path().unlink(missing_ok=True)
    _write(fdir() / "takeover.json", result)
    sb.hxdr_record("auto_promote", old, {k: v for k, v in result.items() if k != "hook"}
                   | {"hook_exit": result["hook"].get("exit")})
    return result


# ── on the standby, called by the main over the operator's SSH ───────────────

def remote_failover_key(_p: dict) -> dict:
    return {"pubkey": new_key()}


def remote_failover_set(p: dict) -> dict:
    for k in ("node", "peer", "witness_target", "witness_port", "lease", "margin", "term", "known_hosts"):
        if k not in p:
            raise sb.Refused(f"missing {k}")
    parse_target(p["witness_target"])
    if not key_path().exists():
        raise sb.Refused("no witness key here — run enable again")
    lines = [ln for ln in str(p["known_hosts"]).splitlines() if ln.strip()]
    if not lines or not all(re.fullmatch(r"\[?[A-Za-z0-9._:-]+\]?(:\d+)? (ssh|ecdsa)-[a-z0-9-]+ [A-Za-z0-9+/]+={0,2}", ln)
                            for ln in lines):
        raise sb.Refused("bad witness host keys")
    _private()
    sb.write_private(known_path(), "\n".join(lines) + "\n")
    write_conf({k: p[k] for k in ("node", "peer", "witness_target", "witness_port", "lease", "margin", "term")}
               | {"holding": False, "enabled_at": sb.now()})
    return {}


def remote_failover_check(_p: dict) -> dict:
    try:
        return {"witness": witness("seen")}
    except sb.Refused as e:
        return {"witness": {"ok": False, "error": str(e)}}


def remote_failover_clear(_p: dict) -> dict:
    clear_local()
    return {}


REMOTE = {"remote-failover-key": remote_failover_key, "remote-failover-set": remote_failover_set,
          "remote-failover-check": remote_failover_check, "remote-failover-clear": remote_failover_clear}


def add_parser(sub) -> None:
    fo = sub.add_parser("failover", help="automatic failover with a witness (step 12)")
    fs = fo.add_subparsers(dest="fo_cmd", required=True)
    en = fs.add_parser("enable", help="(linked main) set up the witness")
    en.add_argument("--witness", required=True, help="[user@]host of the third machine (your own SSH)")
    en.add_argument("--witness-port", type=int, default=22)
    en.add_argument("--lease", type=int, default=30, help="seconds the main's lease lasts")
    en.add_argument("--margin", type=int, default=5, help="extra seconds before the standby may take it")
    en.add_argument("--again", action="store_true", help="set it up anew (new keys, term 1)")
    st = fs.add_parser("status", help="the lease as the witness sees it")
    st.add_argument("--json", action="store_true")
    pa = fs.add_parser("pause", help="planned work: the standby will not take over")
    pa.add_argument("--reason", default="maintenance")
    fs.add_parser("resume", help="end a pause")
    fs.add_parser("agent", help=argparse.SUPPRESS)          # velaris-failover-agent.service
    fs.add_parser("fence-check", help=argparse.SUPPRESS)    # velaris-failover-fence.timer
    di = fs.add_parser("disable", help="remove the witness keys and settings")
    di.add_argument("--local-only", action="store_true", help="do not contact the witness or the standby")


def run_command(a: argparse.Namespace) -> int:
    return {"enable": cmd_enable, "status": cmd_status, "pause": cmd_pause, "resume": cmd_resume,
            "disable": cmd_disable, "agent": cmd_agent, "fence-check": cmd_fence_check}[a.fo_cmd](a)
