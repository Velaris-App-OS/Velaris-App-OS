"""PUO Phase 2 — platform update watcher: timely admin notifications.

Background task (started in lifespan, like OutboxRelay) that polls the channel
manifest and beacon, and notifies admin users via the push notification
service on these SLA events:

  - update_available     — a new version appeared on this install's channel
  - awaiting_decision    — available > 24 h with no pending request (re-nags
                           every 24 h; security releases get a stronger text)
  - update_applied       — beacon flipped to "updated"
  - update_failed        — beacon flipped to "failed" / "unhealthy"

State lives in `.update-notify-state` (repo root, gitignored) so restarts
never re-spam. The dashboard banner remains the always-on in-app surface;
push delivery is best-effort (silently skipped when no devices/channels are
configured).

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 1800          # seconds between checks (30 min)
_DECISION_SLA_HOURS = 24       # nag when an update sits undecided this long
_NAG_INTERVAL_HOURS = 24       # at most one nag per day


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PlatformUpdateWatcher:
    def __init__(self, session_factory) -> None:
        self._factory = session_factory
        self._task: asyncio.Task | None = None
        self._running = False

    def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run(), name="platform-update-watcher")
        logger.info("PlatformUpdateWatcher started (poll=%ds)", _POLL_INTERVAL)

    def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()

    async def _run(self) -> None:
        while self._running:
            try:
                await self._check()
            except Exception as exc:
                logger.warning("platform update watcher cycle error: %s", exc)
            await asyncio.sleep(_POLL_INTERVAL)

    # ── One check cycle ───────────────────────────────────────────────────────

    async def _check(self) -> None:
        from case_service.api.routers.platform_updates import (
            _fetch_manifest, _read_json_file, _read_platform_config, _version_tuple,
        )

        cfg = _read_platform_config()
        root: Path = cfg["root"]
        state_path = root / ".update-notify-state"
        state = _read_json_file(state_path) or {}

        manifest = await _fetch_manifest(cfg)
        target = manifest.get(cfg["channel"]) if manifest else None
        release = (manifest.get("releases", {}) or {}).get(target, {}) if (manifest and target) else {}
        security = bool(release.get("security", False))

        available = bool(
            target
            and target != cfg["version"]
            and _version_tuple(target) > _version_tuple(cfg["version"])
        )
        pending = _read_json_file(root / ".update-request")
        beacon = _read_json_file(root / ".update-status") or {}
        now = _utcnow()

        # 1. New version on the channel
        if available and state.get("last_available_version") != target:
            await self._notify_admins(
                ("Security update available" if security else "Platform update available"),
                f"Velaris v{target} is available on the {cfg['channel']} channel "
                f"(installed: v{cfg['version']}). Review it on the dashboard.",
                "platform_update.available",
            )
            state["last_available_version"] = target
            state["first_seen_at"] = now.isoformat()
            state["last_nag_at"] = now.isoformat()

        # 2. Decision SLA — re-nag while undecided
        elif available and not pending:
            first_seen = self._parse(state.get("first_seen_at"))
            last_nag = self._parse(state.get("last_nag_at"))
            if first_seen and (now - first_seen).total_seconds() > _DECISION_SLA_HOURS * 3600 \
               and (last_nag is None or (now - last_nag).total_seconds() > _NAG_INTERVAL_HOURS * 3600):
                body = (
                    f"Security update v{target} has been waiting for "
                    f"{int((now - first_seen).total_seconds() // 3600)} h — remaining on "
                    f"v{cfg['version']} leaves known vulnerabilities unpatched."
                    if security else
                    f"Platform update v{target} has been awaiting a decision for "
                    f"{int((now - first_seen).total_seconds() // 3600)} h."
                )
                await self._notify_admins(
                    "Platform update awaiting decision", body, "platform_update.awaiting_decision",
                )
                state["last_nag_at"] = now.isoformat()

        # 3. Beacon transitions (applied / failed)
        beacon_key = f"{beacon.get('result')}:{beacon.get('timestamp')}"
        if beacon and beacon_key != state.get("last_beacon_key"):
            result = beacon.get("result")
            if result == "updated":
                await self._notify_admins(
                    "Platform updated",
                    f"This environment was updated successfully: {beacon.get('message', '')}.",
                    "platform_update.applied",
                )
            elif result == "rolled_back":
                await self._notify_admins(
                    "Platform rolled back",
                    f"This environment was reverted: {beacon.get('message', '')}. "
                    "The DB schema was not changed; review whether a backup restore is needed.",
                    "platform_update.rolled_back",
                )
            elif result in ("failed", "unhealthy"):
                await self._notify_admins(
                    "Platform update needs attention",
                    f"Update problem on this environment: {beacon.get('message', '')}. "
                    "It is halted for human attention.",
                    "platform_update.failed",
                )
            state["last_beacon_key"] = beacon_key

        state_path.write_text(json.dumps(state, indent=2))

        # Group H: flush queued generic AI-egress events into SecurityEvents
        try:
            from case_service.hxnexus.egress_audit import flush_pending
            async with self._factory() as session:
                flushed = await flush_pending(session)
                if flushed:
                    await session.commit()
                    logger.info("ai.egress: flushed %d queued event(s)", flushed)
        except Exception as exc:
            logger.warning("ai.egress flush failed: %s", exc)

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _parse(value) -> datetime | None:
        try:
            return datetime.fromisoformat(value) if value else None
        except Exception:
            return None

    async def _notify_admins(self, title: str, body: str, event_type: str) -> None:
        from case_service.db.models import HelixUserModel
        from case_service.push.protocol import PushPayload
        from case_service.push.service import send_to_user

        logger.info("platform-update notify: %s — %s", title, body)
        try:
            async with self._factory() as session:
                users = (await session.execute(
                    select(HelixUserModel).where(HelixUserModel.is_active == True)  # noqa: E712
                )).scalars().all()
                admin_ids = [
                    str(u.id) for u in users
                    if u.is_superadmin or "admin" in (u.roles or [])
                ]
                payload = PushPayload(title=title, body=body, data={"kind": event_type})
                for uid in admin_ids:
                    try:
                        await send_to_user(session, uid, event_type, payload)
                    except Exception as exc:
                        logger.debug("push to %s skipped: %s", uid, exc)
        except Exception as exc:
            # Notification delivery is best-effort — never break the watcher
            logger.warning("platform-update notify failed: %s", exc)
