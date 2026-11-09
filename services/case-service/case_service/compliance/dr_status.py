"""HxDR — what the backup tooling on this host last did (R5a step c).

The registry (`dr.py`) knows what was recorded. It cannot know that the
backups are encrypted, that a copy left the box, that the timer exists, or that
the backup PRIVATE key is still lying on the server — those live on the host,
outside the database. `scripts/backup/velaris_backup.py` writes them to
`.backups/status.json` after every run (no secrets in it); this reads that file
plus one live fact (is the key still in the install report), and turns them
into findings the posture API, Studio and the watcher all share.

Absent file = this host has never run the new backup (or the platform runs
somewhere that cannot see the host, e.g. Kubernetes): reported as unknown,
never as fine.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

KEY_MARKER = "<!-- velaris-backup-private-key:begin -->"
STALE_AFTER = timedelta(hours=26)        # a daily backup plus slack
PRIVATE_KEY_GRACE = timedelta(hours=24)  # time to copy the key off before nagging


def install_root() -> Path:
    """The install directory: where the services run from (systemd
    WorkingDirectory, start-velaris.sh cd). Overridable for tests."""
    return Path(os.environ.get("VELARIS_INSTALL_ROOT") or os.getcwd())


def _parse(ts: Any) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(ts))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def backup_status(root: Path | None = None, now: datetime | None = None) -> dict:
    """{"available": bool, "facts": {...}, "findings": [{"code","severity","message"}]}"""
    root = root or install_root()
    now = now or datetime.now(timezone.utc)
    bdir = Path(os.environ.get("VELARIS_BACKUP_DIR") or (root / ".backups"))
    report = root / "velaris-install-report.md"

    key_on_server = False
    key_since: datetime | None = None
    try:
        text = report.read_text(errors="replace")
        if KEY_MARKER in text:
            key_on_server = True
            key_since = datetime.fromtimestamp(report.stat().st_mtime, timezone.utc)
    except OSError:
        pass

    try:
        st = json.loads((bdir / "status.json").read_text())
    except Exception:
        st = None

    findings: list[dict] = []

    def add(code: str, severity: str, message: str) -> None:
        findings.append({"code": code, "severity": severity, "message": message})

    if key_on_server and key_since and now - key_since > PRIVATE_KEY_GRACE:
        add("private_key_on_server", "high",
            "The backup private key is still in velaris-install-report.md on this server. "
            "Copy it somewhere safe, then run ./backup-velaris.sh --forget-private-key.")

    if st is None:
        add("no_backup_status", "high",
            "No backup has run on this host with the current backup tool "
            "(./backup-velaris.sh, or sudo ./deploy/systemd/install-backup-timer.sh).")
        return {"available": False, "facts": {"private_key_on_server": key_on_server},
                "findings": findings}

    last_good = _parse(st.get("last_good_at"))
    last_run = _parse(st.get("last_run_at"))
    outcome = st.get("last_outcome")

    if last_good is None or now - last_good > STALE_AFTER:
        add("backup_stale", "high",
            "No complete backup in the last 26 hours" +
            (f" (last complete: {last_good.isoformat()})." if last_good else "."))
    if outcome == "failed":
        add("backup_failed", "high", f"The last backup FAILED: {st.get('last_error', 'see the journal')}")
    elif outcome == "partial":
        add("backup_partial", "high",
            "The last backup is PARTIAL — missing: " + ", ".join(st.get("parts_missing") or ["?"]))
    elif outcome == "test_failed":
        add("restore_test_failed", "high", "The last backup did NOT pass its restore test.")
    elif outcome == "no_offbox_copy":
        add("offbox_copy_failed", "high", "The last backup was not copied off this server.")
    elif outcome == "unencrypted_database_only":
        add("backup_unencrypted", "high",
            "Backups are database-only and unencrypted: no backup key. Run ./backup-velaris.sh --ensure-key.")

    if not st.get("offbox_configured"):
        add("no_offbox_destination", "medium",
            "Backups stay on this server only — set VELARIS_BACKUP_PATH or VELARIS_BACKUP_S3_*.")
    if not st.get("timer_installed"):
        add("no_backup_timer", "medium",
            "No daily backup timer — sudo ./deploy/systemd/install-backup-timer.sh")
    proven = _parse(st.get("last_proven_restore_at"))
    if proven is None or now - proven > timedelta(days=8):
        add("restore_unproven", "medium", "No backup has been proven to restore in the last week.")

    facts = {
        "last_run_at": st.get("last_run_at"), "last_outcome": outcome,
        "last_good_at": st.get("last_good_at"), "last_set": st.get("last_set"),
        "encrypted": bool(st.get("last_encrypted")), "key_configured": bool(st.get("key_configured")),
        "offbox_configured": bool(st.get("offbox_configured")), "last_offbox": st.get("last_offbox") or {},
        "timer_installed": bool(st.get("timer_installed")),
        "last_proven_restore_at": st.get("last_proven_restore_at"),
        "last_restore_test": st.get("last_restore_test"),
        "private_key_on_server": key_on_server,
        "last_run_age_hours": round((now - last_run).total_seconds() / 3600, 1) if last_run else None,
    }
    return {"available": True, "facts": facts, "findings": findings}


# ── The standby server (R5c step 11 d) ───────────────────────────────────────
# `standby-velaris.sh` keeps its state in `.standby/` (no secrets in what is
# read here). On a main with a standby, `status.json` is refreshed each time
# the standby reports — every minute — so its age is itself a finding.

STANDBY_SILENT_AFTER = timedelta(minutes=3)
STANDBY_MAX_LAG_SECONDS = 60
STANDBY_FOLLOW_GRACE = timedelta(minutes=30)   # time to take a new release's code + `prepare`
_PARTS = (("docs", "documents"), ("recordings", "meeting recordings"), ("env", "settings"),
          ("config", "velaris.yaml"), ("identity", "setup marker and product key"), ("vault", "vault"),
          ("code", "code"))


def _read_json(p: Path) -> dict | None:
    """The file's JSON object; None when missing, unreadable or not an object —
    a hand-edited or half-written mark must never take the posture down."""
    try:
        d = json.loads(p.read_text())
    except Exception:
        return None
    return d if isinstance(d, dict) else None


#: The failover agent writes its state every 5 s; silent this long = not running.
FAILOVER_AGENT_SILENT_AFTER = timedelta(seconds=60)
FAILOVER_PAUSE_TOO_LONG = timedelta(hours=24)


def failover_status(root: Path | None = None, now: datetime | None = None) -> dict:
    """Automatic failover (R5c step 12), read from `.standby/failover/` and the
    marks beside it. {"enabled": bool, "facts": {...}, "findings": [...]} —
    off is not a finding: failover is optional."""
    root = root or install_root()
    now = now or datetime.now(timezone.utc)
    sdir = root / ".standby"
    fdir = sdir / "failover"
    findings: list[dict] = []

    def add(code: str, severity: str, message: str) -> None:
        findings.append({"code": code, "severity": severity, "message": message})

    conf = _read_json(fdir / "failover.json")
    fenced = _read_json(sdir / "fenced.json")
    takeover = _read_json(fdir / "takeover.json")
    if fenced and not fenced.get("replaced") and not (sdir / "demoted.json").exists():
        # (A demoted server has its own finding — standby_demoted_here — and will not restart.)
        # Shown even if failover was disabled afterwards: the mark keeps the platform down.
        add("failover_fenced_here", "high",
            f"This server STOPPED ITSELF at {fenced.get('fenced_at')}: {fenced.get('reason')}. It "
            "restarts by itself when the witness confirms it still holds the lease; if the standby "
            "took over, it stays down. `./standby-velaris.sh failover status`.")
    if not conf:
        return {"enabled": False, "facts": {}, "findings": findings}
    agent = _read_json(fdir / "agent.json") or {}
    lease = _read_json(fdir / "lease.json") or {}
    facts: dict[str, Any] = {
        "witness": conf.get("witness_target"), "node": conf.get("node"), "term": conf.get("term"),
        "holding": bool(conf.get("holding")), "lease_seconds": conf.get("lease"),
        "agent_at": agent.get("at"), "agent_role": agent.get("role"),
        "fenced_at": (fenced or {}).get("fenced_at"),
        "auto_promoted_at": (takeover or {}).get("finished_at"),
        "synchronous": agent.get("synchronous"),
    }
    if agent.get("witness_too_old"):
        add("failover_witness_tool_old", "medium",
            "VELARIS_STANDBY_SYNC is on, but the witness's tool predates it, so commits stay "
            "asynchronous. Run `./standby-velaris.sh failover enable --witness … --again` on the main.")
    at = _parse(agent.get("at"))
    if at is None or now - at > FAILOVER_AGENT_SILENT_AFTER:
        add("failover_agent_down", "high",
            "Automatic failover is enabled but its agent is not running here"
            + (f" (last seen {agent.get('at')})" if at else "")
            + " — nothing renews the lease or watches the main. "
              "`sudo ./deploy/systemd/install-failover.sh`; `systemctl status velaris-failover-agent`.")
    role = agent.get("role")
    w = agent.get("witness") if isinstance(agent.get("witness"), dict) else {}
    if agent.get("witness_error") or w.get("error"):
        add("failover_witness_unreachable", "medium",
            f"The witness {conf.get('witness_target')} does not answer this server "
            f"({agent.get('witness_error') or w.get('error')}). "
            + ("This main keeps running while its standby is heard; if both are lost it stops itself."
               if role == "main" else "This standby cannot take over until it answers."))
    paused = lease.get("paused") or w.get("paused")
    if paused:
        since = _parse(paused.get("at")) if isinstance(paused, dict) else None
        long_ = since is not None and now - since > FAILOVER_PAUSE_TOO_LONG
        add("failover_paused", "high" if long_ else "medium",
            f"Automatic failover is PAUSED{' since ' + str(paused.get('at')) if isinstance(paused, dict) and paused.get('at') else ''}"
            f"{' (' + str(paused.get('reason')) + ')' if isinstance(paused, dict) and paused.get('reason') else ''}"
            " — the standby will not take over. `./standby-velaris.sh failover resume` on the main.")
    blocked = agent.get("takeover")
    if role == "standby" and isinstance(blocked, str) and (
            blocked.startswith("NOT taking over") or blocked.startswith("witness not reached")):
        add("failover_takeover_blocked", "high",
            f"The main is lost and this standby is NOT taking over by itself: {blocked}. Decide by "
            "hand: `./standby-velaris.sh promote --main-is-down` (what is not here is lost).")
    if takeover:
        if takeover.get("promote_error"):
            add("failover_promote_failed", "high",
                f"This server took the lease (term {takeover.get('term')}) but the promote failed: "
                f"{takeover.get('promote_error')}. Finish it: `./standby-velaris.sh promote --main-is-down --yes`.")
        elif not (sdir / "main.json").exists():
            # Until the old main is linked again as this server's standby, there is
            # no protection left — high, so the operator is told (and reminded daily).
            hook = takeover.get("hook") if isinstance(takeover.get("hook"), dict) else {}
            add("failover_promoted_automatically", "high",
                f"This server took over AUTOMATICALLY at {takeover.get('finished_at')} (term "
                f"{takeover.get('term')}) from {takeover.get('old_main')}. Link the old main again as "
                "this server's standby to be protected again."
                + ("" if hook.get("exit") == 0 else
                   f" The user-moving hook {'did not run' if not hook.get('ran') else 'failed'}: "
                   f"{hook.get('why') or hook.get('output') or ''} — move users by hand."))
    return {"enabled": True, "facts": facts, "findings": findings}


def standby_status(root: Path | None = None, now: datetime | None = None) -> dict:
    """The standby block of the posture, with automatic failover's (step 12)
    facts under `failover` and its findings among the others."""
    out = _standby_status(root, now)
    fo = failover_status(root, now)
    out["findings"] = out["findings"] + fo["findings"]
    if fo["enabled"]:
        out["failover"] = fo["facts"]
    return out


def _standby_status(root: Path | None = None, now: datetime | None = None) -> dict:
    """{"linked": bool, "role": ..., "facts": {...}, "findings": [...]}.

    Not linked is not a finding: a standby is optional (R5a backups are the
    default protection)."""
    root = root or install_root()
    now = now or datetime.now(timezone.utc)
    sdir = root / ".standby"
    findings: list[dict] = []

    def add(code: str, severity: str, message: str) -> None:
        findings.append({"code": code, "severity": severity, "message": message})

    demoted = _read_json(sdir / "demoted.json")
    promoted = _read_json(sdir / "promoted.json")
    main = _read_json(sdir / "main.json")
    facts: dict[str, Any] = {"linked": bool(main) and not demoted}

    if demoted:
        add("standby_demoted_here", "high",
            f"This server was REPLACED by its standby at {demoted.get('demoted_at')} — the platform "
            "must not run here (two main servers). `./standby-velaris.sh retire`.")
        facts["demoted_at"] = demoted.get("demoted_at")
    if promoted:
        facts["promoted_at"] = promoted.get("promoted_at")
        facts["promote_stage"] = promoted.get("stage")
        facts["promote_documents_missing"] = promoted.get("documents_missing")
        if promoted.get("stage") != "done":
            add("standby_promote_unfinished", "high",
                f"A promote stopped at stage '{promoted.get('stage')}' — run "
                "`./standby-velaris.sh promote` again to finish it.")
        if promoted.get("old_main_diverged"):
            saved = promoted.get("old_main_saved") or {}
            if saved:
                add("standby_old_main_diverged", "medium",
                    f"The old main took writes after the switch-over that are NOT on this server. "
                    f"They were saved on {saved.get('host')} in {saved.get('path')} before it was "
                    "linked again — merge what is needed by hand.")
            else:
                add("standby_old_main_diverged", "high",
                    "The old main took writes after the switch-over that are NOT on this server. "
                    "Keep its databases: linking it again saves them first (.backups/before-rejoin-*).")
    if (sdir / "fence.env").exists():
        add("standby_old_main_not_fenced", "high",
            "The old main has not been demoted yet (it was down at the switch-over) — keep it off; "
            "this server demotes it as soon as it answers."
            + (f" Last attempt: {promoted.get('fence_last_error')}" if promoted and
               promoted.get("fence_last_error") else ""))

    if not main or demoted:
        return {"linked": False, "role": "demoted" if demoted else ("main" if promoted else None),
                "facts": facts, "findings": findings}

    st = _read_json(sdir / "status.json") or {}
    sync = st.get("sync") or {}
    checked = _parse(st.get("checked_at"))
    reported = _parse(sync.get("received_at"))
    facts.update({
        "standby": main.get("standby"), "linked_at": main.get("linked_at"),
        "state": st.get("state"), "checked_at": st.get("checked_at"),
        "version": st.get("version"), "standby_version": st.get("standby_version"),
        "last_report_at": sync.get("received_at"),
        "databases": {k: {"state": v.get("state"), "replay_lag_seconds": v.get("replay_lag_seconds"),
                          "lag_bytes": v.get("lag_bytes")}
                      for k, v in (st.get("databases") or {}).items()},
        "parts": {k: (sync.get(k) or {}).get("state") for k, _ in _PARTS},
        # R5d 11b (c): reads from the standby's copy — absent = off.
        "serve_reads": st.get("serve_reads"),
    })
    reads = st.get("serve_reads") or {}
    if reads and reads.get("tunnel_up") is False:
        add("standby_reads_down", "medium",
            "Reads from the standby are on, but its tunnel is down: dashboards, reports and search "
            "read from this server through a small capped pool (busy pages may be refused). "
            "`./standby-velaris.sh serve-reads status`; `journalctl -u velaris-standby-reads`.")
    if reads and reads.get("credentials_current") is False:
        add("standby_reads_credentials", "high",
            "The standby read URL still carries the old database password — its reads fail and run "
            "on this server. `./standby-velaris.sh serve-reads off`, then `on`.")
    last_seen = max([d for d in (checked, reported) if d], default=None)
    if last_seen is None or now - last_seen > STANDBY_SILENT_AFTER:
        add("standby_silent", "high",
            f"No report from the standby {main.get('standby')} "
            + (f"since {last_seen.isoformat()}" if last_seen else "yet")
            + " — it may be off, or its tunnel or sync timer stopped. `./standby-velaris.sh status`.")
        return {"linked": True, "role": "main", "facts": facts, "findings": findings}
    state = st.get("state")
    if state == "needs_relink":
        add("standby_needs_relink", "high",
            "The standby fell further behind than this server keeps WAL for — it can no longer "
            "catch up. `./standby-velaris.sh unlink`, then `link` again.")
    elif state != "streaming":
        add("standby_not_receiving", "high",
            f"The standby is not receiving database changes ({state}) — a switch-over now would "
            "lose what it has not got.")
    lags = [v.get("replay_lag_seconds") for v in (st.get("databases") or {}).values()
            if v.get("replay_lag_seconds") is not None]
    if lags and max(lags) > STANDBY_MAX_LAG_SECONDS:
        add("standby_lagging", "high", f"The standby is {max(lags):.0f} s behind.")
    code = sync.get("code") or {}
    if st.get("standby_version") and st.get("version") and st["standby_version"] != st["version"]:
        tried = _parse(code.get("attempt_at"))
        if code.get("state") == "failed" or tried is None or now - tried > STANDBY_FOLLOW_GRACE:
            # Failed, never tried, or still not there long after: a promote is
            # refused while the versions differ, so this is not cosmetic.
            add("standby_version_mismatch", "high",
                f"The standby runs {st['standby_version']}, this server {st['version']}"
                + (f", and following failed: {code.get('note')}" if code.get("state") == "failed" else "")
                + " — a promote is refused until it matches.")
        else:
            add("standby_updating", "medium",
                f"The standby is following this server's update ({st['standby_version']} → {st['version']}).")
    for k, label in _PARTS:
        part = sync.get(k) or {}
        if part.get("state") in ("failed", "deletes_held"):
            add(f"standby_{k}_{part['state']}", "high",
                f"Standby {label}: {part.get('note') or part['state']}")
        elif part.get("state") == "not_copied" and "NOT copied" in (part.get("note") or ""):
            add(f"standby_{k}_not_copied", "medium", f"Standby {label}: {part.get('note')}")
    return {"linked": True, "role": "main", "facts": facts, "findings": findings}
