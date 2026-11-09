#!/usr/bin/env python3
"""Velaris failover witness — the third vote (R5c, Nov_TODO step 12).

Automatic failover between a main server and its standby needs someone to
decide which of the two is in charge: the two alone cannot tell "the other is
down" from "the link between us is cut", and guessing wrong gives two servers
accepting writes (split-brain). This file is that someone. It runs on a third,
small machine the customer provides — any Linux with python3 and sshd.

It runs NO daemon and opens NO port. It is copied to the witness at
`standby-velaris.sh failover enable` and runs only as the forced command of two
SSH keys, one per Velaris server (`serve <node>`). Its whole state is one file,
`~/.velaris-witness/state.json` (0600, changed under `flock`).

The protocol — a lease:

* The server holding the lease (the main) renews it every few seconds:
  `renew <term>`.
* The other server may take it — `acquire <term>` — only when the witness has
  heard no renewal for `lease + margin` seconds by its own clock. Each grant
  raises the term; a server whose term is older is told to stop (`fence`).
* Time is the witness's `CLOCK_BOOTTIME` — never wall time, which an NTP step
  can move. After the witness reboots that clock restarts, so the boot id is
  kept with it and a fresh boot waits a whole `lease + margin` before granting.
* `pause` (holder only) refuses every `acquire` until `resume` — planned work.

Every answer is one JSON line. Anything not on the list is refused.

Standard library only; python 3.8+.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import re
import sys
import time
from pathlib import Path

# Fixed, never from the environment: an SSH client may be allowed to send
# variables (AcceptEnv), and the state's location must not be one of them.
HOME = Path.home() / ".velaris-witness"
STATE = HOME / "state.json"
LOCK = HOME / "state.lock"
KEY_MARK = "velaris-failover-witness"
NODE_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")
LABEL_RE = re.compile(r"[A-Za-z0-9@._:-]{1,120}")
HISTORY_MAX = 50
# Bounds a setup may choose; outside them the guarantees are not worth having.
LEASE_RANGE = (10, 600)
MARGIN_RANGE = (1, 120)


def boottime() -> float:
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return "unknown"


def wall() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def answer(obj: dict) -> int:
    print(json.dumps(obj), flush=True)
    return 0 if obj.get("ok", True) else 1


class Locked:
    """The state, read and written under an exclusive lock."""

    def __enter__(self) -> dict:
        HOME.mkdir(mode=0o700, exist_ok=True)
        self.fd = os.open(LOCK, os.O_WRONLY | os.O_CREAT, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        self.state = json.loads(STATE.read_text()) if STATE.exists() else {}
        return self.state

    def __exit__(self, *_exc) -> None:
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)


def _clock(st: dict) -> float:
    """Now on this boot's clock — and, on a new boot, forget the old renewal:
    the clock it was measured on is gone, so the witness waits a full lease
    from the first request it sees after booting."""
    now = boottime()
    bid = boot_id()
    if st.get("boot_id") != bid:
        st["boot_id"] = bid
        st["boot_first"] = now
        st["last_renew"] = None
        _event(st, "witness_booted", None)
    return now


def silent_for(st: dict, now: float) -> float:
    """Seconds the holder has not renewed, by this witness's clock."""
    since = max(x for x in (st.get("last_renew"), st.get("boot_first")) if x is not None)
    return max(0.0, now - since)


def _event(st: dict, what: str, node: str | None, **extra) -> None:
    st.setdefault("history", []).append({"at": wall(), "event": what, "node": node, **extra})
    st["history"] = st["history"][-HISTORY_MAX:]


def summary(st: dict, now: float) -> dict:
    return {"term": st.get("term"), "holder": st.get("holder"), "paused": st.get("paused"),
            "lease": st.get("lease"), "margin": st.get("margin"),
            "holder_silent_for": round(silent_for(st, now), 1), "holder_sync": st.get("holder_sync"),
            "last_seen": st.get("last_seen", {}), "witness_time": wall()}


# ── the forced command ───────────────────────────────────────────────────────

# `renew N sync|async` (step 12 d): the holder says whether its commits wait for
# the standby. The witness keeps it, so a standby can tell "nothing committed
# without me" from "the main went on alone" — it is said BEFORE the main drops
# to asynchronous, never after.
REQUEST_RE = re.compile(r"(?P<verb>renew|acquire) (?P<term>\d{1,9})(?: (?P<mode>sync|async))?"
                        r"|(?P<plain>state|seen|resume)|pause(?P<reason> [A-Za-z0-9 ._:,-]{0,120})?")


def serve(node: str) -> int:
    req = os.environ.get("SSH_ORIGINAL_COMMAND", "").strip()
    if not NODE_RE.fullmatch(node or ""):
        return answer({"ok": False, "error": "bad node"})
    m = REQUEST_RE.fullmatch(req)
    if not m:
        _refused(node, req)
        return answer({"ok": False, "error": "refused"})
    with Locked() as st:
        if node not in (st.get("nodes") or {}):
            return answer({"ok": False, "error": "unknown node"})
        now = _clock(st)
        st.setdefault("last_seen", {})[node] = wall()
        verb = m.group("verb") or m.group("plain") or "pause"
        if verb == "acquire" and m.group("mode"):
            return answer({"ok": False, "error": "refused"})
        out = HANDLERS[verb](st, node, now, m)
        _save(st)
        return answer(out)


def _save(st: dict) -> None:
    """Written whole and fsynced, then renamed — and the rename fsynced too, so
    a power cut leaves the old state or the new one, never half of either."""
    tmp = STATE.with_name(STATE.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(st, indent=2))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE)
    dfd = os.open(HOME, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def h_renew(st: dict, node: str, now: float, m) -> dict:
    term = int(m.group("term"))
    if st.get("holder") != node or term != st.get("term"):
        # A server holding an older term must stop writing at once: another
        # was granted the lease while it could not reach this witness.
        _event(st, "renew_refused", node, its_term=term)
        return {"ok": True, "renewed": False, "fence": True, **summary(st, now)}
    st["last_renew"] = now
    mode = m.group("mode")
    st["holder_sync"] = None if mode is None else (mode == "sync")
    return {"ok": True, "renewed": True, "fence": False, **summary(st, now)}


def h_acquire(st: dict, node: str, now: float, m) -> dict:
    term = int(m.group("term"))
    s = summary(st, now)
    why = None
    if st.get("holder") == node:
        why = "already the holder"
    elif term != st.get("term"):
        why = f"term {term} is not the current term {st.get('term')}"
    elif st.get("paused"):
        why = "failover is paused"
    elif silent_for(st, now) < st["lease"] + st["margin"]:
        why = f"the holder renewed {s['holder_silent_for']} s ago (needs {st['lease'] + st['margin']} s)"
    if why:
        _event(st, "acquire_refused", node, reason=why)
        return {"ok": True, "granted": False, "reason": why, **s}
    old = st["holder"]
    st["term"] = int(st["term"]) + 1
    st["holder"] = node
    st["last_renew"] = now          # the new holder's lease starts now
    st["holder_sync"] = None        # the new holder says its own mode
    _event(st, "acquired", node, term=st["term"], from_holder=old)
    return {"ok": True, "granted": True, **summary(st, now)}


def h_state(st: dict, node: str, now: float, m) -> dict:
    return {"ok": True, **summary(st, now)}


def h_seen(st: dict, node: str, now: float, m) -> dict:
    return {"ok": True, **summary(st, now)}


def h_pause(st: dict, node: str, now: float, m) -> dict:
    if st.get("holder") != node:
        return {"ok": False, "error": "only the holder can pause", **summary(st, now)}
    reason = (m.group("reason") or "").strip() or "maintenance"
    st["paused"] = {"by": node, "at": wall(), "reason": reason}
    _event(st, "paused", node, reason=reason)
    return {"ok": True, **summary(st, now)}


def h_resume(st: dict, node: str, now: float, m) -> dict:
    if st.get("holder") != node:
        return {"ok": False, "error": "only the holder can resume", **summary(st, now)}
    st["paused"] = None
    # Resuming must not let a long-silent holder be taken over at once.
    st["last_renew"] = now
    _event(st, "resumed", node)
    return {"ok": True, **summary(st, now)}


HANDLERS = {"renew": h_renew, "acquire": h_acquire, "state": h_state, "seen": h_seen,
            "pause": h_pause, "resume": h_resume}


def _refused(node: str, req: str) -> None:
    try:
        with Locked() as st:
            if st:
                _event(st, "refused_request", node, request=req[:80])
                _save(st)
    except OSError:
        pass


# ── set up / take down (run over the operator's own SSH) ────────────────────

def _authorized_keys() -> Path:
    return Path.home() / ".ssh" / "authorized_keys"


def _write_0600(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".velaris-tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def _valid_key(pub: str) -> tuple[str, str]:
    if not isinstance(pub, str) or any(ord(ch) < 32 for ch in pub):
        # One line, or nothing: a newline in a key is an extra authorized_keys line.
        raise ValueError(f"not a usable public key: {str(pub)[:60]!r}")
    parts = pub.split()
    if (len(parts) < 2 or not re.fullmatch(r"ssh-[a-z0-9-]+", parts[0])
            or not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", parts[1])):
        raise ValueError(f"not a usable public key: {pub[:60]!r}")
    return parts[0], parts[1]


def setup(payload: dict) -> dict:
    """Install the two servers' keys (forced command, nothing else) and the
    first state: `holder` holds term 1. Re-running replaces both."""
    nodes = payload.get("nodes") or {}
    holder = payload.get("holder")
    lease, margin = int(payload.get("lease", 30)), int(payload.get("margin", 5))
    if len(nodes) != 2 or holder not in nodes or not all(NODE_RE.fullmatch(n) for n in nodes):
        raise ValueError("two nodes are needed, one of them the holder")
    if not (LEASE_RANGE[0] <= lease <= LEASE_RANGE[1] and MARGIN_RANGE[0] <= margin <= MARGIN_RANGE[1]):
        raise ValueError(f"lease must be {LEASE_RANGE}, margin {MARGIN_RANGE} seconds")
    me = Path(__file__).resolve()
    if not re.fullmatch(r"[A-Za-z0-9/._-]+", str(me)):
        raise ValueError(f"the witness path {me} cannot be written into authorized_keys safely")
    lines = []
    for n, info in nodes.items():
        kt, kb = _valid_key(info.get("pubkey", ""))
        label = info.get("label", n)
        if not LABEL_RE.fullmatch(label):
            raise ValueError(f"bad label for {n}")
        lines.append(f'restrict,command="python3 {me} serve {n}" {kt} {kb} {KEY_MARK} {n} {label}')
    ak = _authorized_keys()
    ak.parent.mkdir(mode=0o700, exist_ok=True)
    keep = [ln for ln in (ak.read_text().splitlines() if ak.exists() else []) if f" {KEY_MARK} " not in ln]
    _write_0600(ak, "\n".join(keep + lines) + "\n")
    with Locked() as st:
        st.clear()
        st.update({"version": 1, "term": 1, "holder": holder, "lease": lease, "margin": margin,
                   "paused": None, "created_at": wall(),
                   "nodes": {n: {"label": i.get("label", n)} for n, i in nodes.items()}})
        now = _clock(st)
        st["last_renew"] = now
        _event(st, "setup", holder, nodes=sorted(nodes))
        _save(st)
        return {"state": summary(st, now)}


def teardown(_payload: dict) -> dict:
    ak = _authorized_keys()
    removed = 0
    if ak.exists():
        lines = ak.read_text().splitlines()
        keep = [ln for ln in lines if f" {KEY_MARK} " not in ln]
        removed = len(lines) - len(keep)
        _write_0600(ak, "\n".join(keep) + ("\n" if keep else ""))
    for p in (STATE, LOCK, STATE.with_name(STATE.name + ".tmp"), HOME / "velaris_witness.py"):
        p.unlink(missing_ok=True)          # this file too: nothing of Velaris stays on the witness
    try:
        HOME.rmdir()
    except OSError:
        pass
    return {"keys_removed": removed}


def show(_payload: dict) -> dict:
    with Locked() as st:
        if not st:
            return {"state": None}
        now = _clock(st)
        _save(st)
        return {"state": summary(st, now), "history": st.get("history", [])[-10:]}


OPERATOR = {"setup": setup, "teardown": teardown, "show": show}


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "serve":
        return serve(argv[1])
    if len(argv) == 1 and argv[0] in OPERATOR:
        try:
            payload = json.loads(sys.stdin.read() or "{}") if not sys.stdin.isatty() else {}
            return answer({"ok": True, **OPERATOR[argv[0]](payload)})
        except Exception as e:  # noqa: BLE001
            return answer({"ok": False, "error": str(e)})
    print("usage: velaris_witness.py serve NODE | setup | teardown | show", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
