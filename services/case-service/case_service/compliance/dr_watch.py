"""HxDR backup watcher — failure is loud (R5a step c, design §8).

A backup timer is the thing that can quietly stop: a full disk, an expired S3
credential, a sealed vault, a timer nobody installed. Every hour this reads the
same findings the posture API shows (`dr_status.backup_status`) and notifies
admins through the platform's own push/in-app path:

  - a high-severity finding (stale, failed, partial, failed restore test, no
    off-box copy, unencrypted, private key still on the server) — at most once a
    day per finding, and again as soon as it changes;
  - a finding that clears is forgotten, so a recurrence notifies again.

Medium findings (no timer, no off-box destination, restore unproven) are shown
in Studio and the posture API, not pushed.

Delivery: push to admins with a registered device, AND an email to
VELARIS_CASE_ALERT_EMAIL through the system mailbox when it is configured
(`case_service/mail/operator_alerts.py`).

State lives in `<backups>/.watch-state.json`. Placement: case-service, beside
HxDR and the update watcher, until background loops move to worker-service
(Nov_TODO R5e).

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from case_service.compliance.dr_status import backup_status, install_root, standby_status

from velaris_shared.config import env_int  # R1: a typo here must not stop the service

logger = logging.getLogger(__name__)

POLL_SECONDS = env_int("VELARIS_BACKUP_WATCH_SECONDS", 3600)
# With a standby linked (R5c) the same watcher looks every 5 minutes: a standby
# that stopped receiving is a loss window growing by the minute, not by the day.
STANDBY_POLL_SECONDS = env_int("VELARIS_STANDBY_WATCH_SECONDS", 300)
RENOTIFY = timedelta(hours=24)


def _state_path() -> Path:
    root = install_root()
    return Path(os.environ.get("VELARIS_BACKUP_DIR") or (root / ".backups")) / ".watch-state.json"


def due_notifications(findings: list[dict], state: dict, now: datetime) -> tuple[list[dict], dict]:
    """Which high findings to push now, and the new state. Pure, for tests."""
    sent: dict = dict(state.get("sent") or {})
    current = {f["code"]: f for f in findings if f.get("severity") == "high"}
    out = []
    for code, f in current.items():
        prev = sent.get(code)
        last = datetime.fromisoformat(prev["at"]) if prev else None
        if prev is None or prev.get("message") != f["message"] or now - last >= RENOTIFY:
            out.append(f)
            sent[code] = {"at": now.isoformat(), "message": f["message"]}
    for code in list(sent):
        if code not in current:
            del sent[code]          # cleared: a recurrence notifies again
    return out, {"sent": sent}


_TITLE = {"standby": "Standby server needs attention", "failover": "Automatic failover needs attention",
          "backup": "Backup needs attention"}
_SHORT = {"standby": "Standby", "failover": "Automatic failover", "backup": "Backup"}


def _kind(code: str) -> str:
    """Which part a finding belongs to — standby_* / failover_* (step 12) / backup."""
    return "standby" if code.startswith("standby_") else "failover" if code.startswith("failover_") else "backup"


class BackupWatcher:
    def __init__(self, session_factory) -> None:
        self._factory = session_factory
        self._task: asyncio.Task | None = None
        self._running = False

    def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run(), name="backup-watcher")
        logger.info("BackupWatcher started (poll=%ds)", POLL_SECONDS)

    def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()

    async def _run(self) -> None:
        # Let the platform finish starting before the first look.
        await asyncio.sleep(min(120, POLL_SECONDS))
        while self._running:
            try:
                await self.check()
            except Exception as exc:
                logger.warning("backup watcher cycle error: %s", exc)
            watching_standby = (install_root() / ".standby").is_dir()
            await asyncio.sleep(min(POLL_SECONDS, STANDBY_POLL_SECONDS) if watching_standby else POLL_SECONDS)

    async def check(self) -> list[dict]:
        now = datetime.now(timezone.utc)
        status = backup_status(now=now)
        findings = status["findings"] + standby_status(now=now)["findings"]
        path = _state_path()
        try:
            state = json.loads(path.read_text())
        except Exception:
            state = {}
        due, new_state = due_notifications(findings, state, now)
        for f in due:
            kind = _kind(f["code"])
            await self._notify_admins(_TITLE[kind], f["message"], f"{kind}.{f['code']}")
        if due:
            # One email for everything due this check (VELARIS_CASE_ALERT_*; off when unset).
            from case_service.mail.operator_alerts import send_operator_alert
            kinds = {_kind(f["code"]) for f in due}
            only = kinds.pop() if len(kinds) == 1 else None
            await send_operator_alert(
                (_TITLE[only] if only else "Recovery needs attention")
                if len(due) == 1 else f"{_SHORT.get(only, 'Recovery')}: {len(due)} problems",
                "\n\n".join(f"• {f['message']}" for f in due)
                + "\n\nDetails: Studio → Compliance → Disaster recovery, or GET /api/v1/compliance/dr/posture.")
        if due or new_state != state:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(new_state))
            except OSError as exc:
                logger.debug("backup watcher state not saved: %s", exc)
        return due

    async def _notify_admins(self, title: str, body: str, event_type: str) -> None:
        """Same delivery path as the platform update watcher. Best-effort."""
        from velaris_shared.db.models import HelixUserModel
        from case_service.push.protocol import PushPayload
        from case_service.push.service import send_to_user

        logger.warning("backup: %s", body)
        try:
            async with self._factory() as session:
                users = (await session.execute(
                    select(HelixUserModel).where(HelixUserModel.is_active == True)  # noqa: E712
                )).scalars().all()
                admins = [str(u.id) for u in users if u.is_superadmin or "admin" in (u.roles or [])]
                payload = PushPayload(title=title, body=body, data={"kind": event_type})
                for uid in admins:
                    try:
                        await send_to_user(session, uid, event_type, payload)
                    except Exception as exc:
                        logger.debug("backup notify to %s skipped: %s", uid, exc)
        except Exception as exc:
            logger.warning("backup notify failed: %s", exc)
