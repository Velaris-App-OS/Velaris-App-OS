"""PUO Phase 3 — rollout plan engine (rings, soak, supersede).

Background task (60 s cycle) that advances platform update rollout plans:

  1. AUTO-DRAFT     — when THIS environment's beacon shows a successful update
                      to version V and no plan for V exists, draft a plan with
                      one run per registered environment (ring order =
                      environment_registry.order_index; the last ring is the
                      prod ring). Notifies admins.
  2. SUPERSEDE      — any non-terminal plan whose version no longer matches
                      the channel pin halts as `superseded` (prod never
                      receives an outdated release).
  3. RING ADVANCE   — active plans trigger non-final rings sequentially via
                      the peer trigger endpoint (Bearer import_api_key — the
                      HxDeploy trust channel); each ring must reach the plan
                      version (verified via /peer-status) before the next
                      starts. Ring failure halts the chain.
  4. SOAK           — when all non-final rings succeed, the plan soaks for
                      plan.soak_hours, then flips to awaiting_prod_approval
                      (notifies). Soak expiry never auto-fires prod — the
                      admin's second approval (state prod_approved) does.
  5. FINAL RING     — prod_approved plans trigger the final ring; success
                      completes the plan.

Plans orchestrate PLATFORM CODE updates — sibling of HxDeploy, which is
never used for this.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

logger = logging.getLogger(__name__)

_POLL_INTERVAL = 60  # seconds


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PlatformUpdatePlanEngine:
    def __init__(self, session_factory) -> None:
        self._factory = session_factory
        self._task: asyncio.Task | None = None
        self._running = False

    def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run(), name="puo-plan-engine")
        logger.info("PlatformUpdatePlanEngine started (poll=%ds)", _POLL_INTERVAL)

    def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()

    async def _run(self) -> None:
        while self._running:
            try:
                await self._cycle()
            except Exception as exc:
                logger.warning("PUO plan engine cycle error: %s", exc)
            await asyncio.sleep(_POLL_INTERVAL)

    # ── One engine cycle ──────────────────────────────────────────────────────

    async def _cycle(self) -> None:
        from case_service.api.routers.platform_updates import (
            _fetch_manifest, _read_json_file, _read_platform_config,
        )
        from case_service.db.models import PlatformUpdatePlanModel

        cfg = _read_platform_config()
        manifest = await _fetch_manifest(cfg)
        pin = manifest.get(cfg["channel"]) if manifest else None

        async with self._factory() as session:
            await self._auto_draft(session, cfg, _read_json_file)
            plans = (await session.execute(
                select(PlatformUpdatePlanModel).where(
                    PlatformUpdatePlanModel.state.in_(
                        ("active", "soaking", "awaiting_prod_approval", "prod_approved")
                    )
                )
            )).scalars().all()

            for plan in plans:
                # 2. Supersede — only when the manifest is reachable AND disagrees
                if pin is not None and pin != plan.resolved_version:
                    plan.state = "superseded"
                    plan.halted_reason = f"channel now pins v{pin}"
                    for r in await self._runs(session, plan.id):
                        if r.state in ("pending", "awaiting_approval", "approved", "triggered", "running"):
                            r.state = "halted"
                    await session.commit()
                    await self._notify(
                        "Rollout plan superseded",
                        f"Plan for v{plan.resolved_version} halted — the {plan.channel} "
                        f"channel now pins v{pin}. A new plan will be drafted after this "
                        "environment updates.",
                    )
                    continue

                if plan.state == "active":
                    await self._advance_rings(session, plan, final=False)
                elif plan.state == "soaking":
                    await self._check_soak(session, plan)
                elif plan.state == "prod_approved":
                    await self._advance_rings(session, plan, final=True)

    # ── 1. Auto-draft on local update success ─────────────────────────────────

    async def _auto_draft(self, session, cfg, _read_json_file) -> None:
        from case_service.db.models import (
            EnvironmentRegistryModel, PlatformUpdatePlanModel,
            PlatformUpdateRunModel, PlatformUpdateSettingsModel,
        )

        # Phase 4 modes policy: "manual" disables auto-drafting entirely
        settings_check = await session.get(PlatformUpdateSettingsModel, 1)
        if settings_check is not None and settings_check.mode == "manual":
            return

        beacon = _read_json_file(cfg["root"] / ".update-status") or {}
        if beacon.get("result") != "updated":
            return
        version = beacon.get("version")
        if not version or version != cfg["version"]:
            return  # beacon stale or yaml not stamped yet

        # Halted/superseded plans don't block a fresh draft — after a halt the
        # admin must be able to retry once the cause is fixed. Exactly one
        # live-or-completed plan per version.
        existing = (await session.execute(
            select(PlatformUpdatePlanModel).where(
                PlatformUpdatePlanModel.resolved_version == version,
                PlatformUpdatePlanModel.state.notin_(("halted", "superseded")),
            )
        )).scalars().first()
        if existing is not None:
            return

        envs = (await session.execute(
            select(EnvironmentRegistryModel)
            .where(EnvironmentRegistryModel.url.isnot(None))
            .order_by(EnvironmentRegistryModel.order_index)
        )).scalars().all()
        envs = [e for e in envs if e.url and e.import_api_key]
        if not envs:
            return  # nothing to orchestrate

        settings_row = await session.get(PlatformUpdateSettingsModel, 1)
        soak = settings_row.default_soak_hours if settings_row else 48

        plan = PlatformUpdatePlanModel(
            resolved_version=version,
            channel=cfg["channel"],
            soak_hours=soak,
            state="draft",
        )
        session.add(plan)
        await session.flush()
        for i, env in enumerate(envs):
            session.add(PlatformUpdateRunModel(
                plan_id=plan.id,
                environment_id=env.id,
                ring_order=i,
                is_final_ring=(i == len(envs) - 1),
            ))
        await session.commit()
        logger.info("PUO: drafted rollout plan %s for v%s (%d rings)", plan.id, version, len(envs))
        await self._notify(
            "Rollout plan ready for approval",
            f"This environment updated to v{version}. A rollout plan for "
            f"{len(envs)} registered environment(s) is drafted and awaiting your approval.",
        )

    # ── 3/5. Ring advancement ─────────────────────────────────────────────────

    async def _advance_rings(self, session, plan, final: bool) -> None:
        from case_service.db.models import EnvironmentRegistryModel, PlatformUpdateSettingsModel

        settings_row = await session.get(PlatformUpdateSettingsModel, 1)
        per_env = settings_row is not None and settings_row.mode == "per-env"

        runs = sorted(await self._runs(session, plan.id), key=lambda r: r.ring_order)
        wave = [r for r in runs if r.is_final_ring == final]

        for run in wave:
            if run.state == "succeeded":
                continue

            env = await session.get(EnvironmentRegistryModel, run.environment_id)
            if env is None or not env.url or not env.import_api_key:
                run.state = "failed"
                run.detail = "environment unreachable (no url/key)"
                await self._halt_plan(session, plan, f"ring '{run.ring_order}' has no reachable environment")
                return

            # Phase 4 per-env mode: every ring needs its own explicit approval
            # (the final ring is already gated by prod approval).
            if per_env and not final and run.state == "pending":
                run.state = "awaiting_approval"
                await session.commit()
                await self._notify(
                    "Ring awaiting approval",
                    f"per-env mode: ring {run.ring_order + 1} ({env.label or env.name}) "
                    f"of the v{plan.resolved_version} rollout awaits your approval.",
                )
                return

            if run.state == "awaiting_approval":
                return  # waiting for the admin

            if run.state in ("pending", "approved"):
                ok, detail = await self._peer_trigger(env)
                run.triggered_at = _utcnow()
                if ok:
                    run.state = "triggered"
                    run.detail = detail
                else:
                    run.state = "failed"
                    run.detail = detail
                    await self._halt_plan(session, plan, f"trigger failed for {env.label or env.name}: {detail}")
                    return
                await session.commit()
                return  # one ring per cycle — strictly sequential

            if run.state in ("triggered", "running"):
                status = await self._peer_status(env)
                if status is None:
                    return  # transient — check again next cycle
                if status.get("current_version") == plan.resolved_version:
                    run.state = "succeeded"
                    run.finished_at = _utcnow()
                    await session.commit()
                    continue  # next ring (or fall through to wave end)
                beacon = status.get("last_update_status") or {}
                if beacon.get("result") in ("failed", "unhealthy") \
                        and self._beacon_after(beacon.get("timestamp"), run.triggered_at):
                    run.state = "failed"
                    run.finished_at = _utcnow()
                    run.detail = beacon.get("message")
                    await self._halt_plan(
                        session, plan,
                        f"{env.label or env.name} failed: {beacon.get('message')}",
                    )
                    return
                if status.get("pending_request"):
                    run.state = "running"
                    await session.commit()
                return  # still in progress — wait

        # Whole wave succeeded
        if final:
            plan.state = "completed"
            await session.commit()
            await self._notify(
                "Rollout completed",
                f"v{plan.resolved_version} is now live on every environment in the plan.",
            )
        else:
            plan.state = "soaking"
            plan.soak_started_at = _utcnow()
            await session.commit()
            await self._notify(
                "Soak started",
                f"All staging rings run v{plan.resolved_version}. Prod approval unlocks "
                f"after the {plan.soak_hours} h soak.",
            )

    # ── 4. Soak ───────────────────────────────────────────────────────────────

    async def _check_soak(self, session, plan) -> None:
        if plan.soak_started_at is None:
            plan.soak_started_at = _utcnow()
            await session.commit()
            return
        if _utcnow() - plan.soak_started_at >= timedelta(hours=plan.soak_hours):
            plan.state = "awaiting_prod_approval"
            await session.commit()
            await self._notify(
                "Prod approval unlocked",
                f"The {plan.soak_hours} h soak for v{plan.resolved_version} completed with "
                "staging healthy. The final (prod) ring now awaits your approval.",
            )

    # ── Peer HTTP (HxDeploy trust channel: Bearer import_api_key) ─────────────
    # Environment URLs are admin-registered internal addresses — the SSRF
    # guard (for untrusted user URLs) deliberately does not apply here.

    async def _peer_trigger(self, env) -> tuple[bool, str]:
        url = env.url.rstrip("/") + "/api/v1/platform/update/trigger"
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
                resp = await client.post(
                    url,
                    json={"mode": "window"},
                    headers={"Authorization": f"Bearer {env.import_api_key}"},
                )
            if resp.status_code < 400:
                sched = (resp.json() or {}).get("scheduled_for")
                return True, f"triggered (scheduled_for={sched})"
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
        except Exception as exc:
            return False, str(exc)

    async def _peer_status(self, env) -> dict | None:
        url = env.url.rstrip("/") + "/api/v1/platform/update/peer-status"
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
                resp = await client.get(
                    url, headers={"Authorization": f"Bearer {env.import_api_key}"},
                )
            return resp.json() if resp.status_code < 400 else None
        except Exception:
            return None

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _beacon_after(beacon_ts, triggered_at: datetime | None) -> bool:
        """Timezone-aware: is the beacon newer than the trigger moment?

        Beacon timestamps carry the remote host's local offset (date -Iseconds);
        triggered_at is UTC. Comparing as strings would be wrong across offsets.
        """
        if not beacon_ts or triggered_at is None:
            return False
        try:
            ts = datetime.fromisoformat(str(beacon_ts))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return ts > triggered_at
        except Exception:
            return False

    async def _runs(self, session, plan_id):
        from case_service.db.models import PlatformUpdateRunModel
        return (await session.execute(
            select(PlatformUpdateRunModel).where(PlatformUpdateRunModel.plan_id == plan_id)
        )).scalars().all()

    async def _halt_plan(self, session, plan, reason: str) -> None:
        plan.state = "halted"
        plan.halted_reason = reason
        for r in await self._runs(session, plan.id):
            if r.state in ("pending", "awaiting_approval", "approved", "triggered", "running"):
                r.state = "halted"
        await session.commit()
        logger.error("PUO: plan %s halted — %s", plan.id, reason)
        await self._notify("Rollout halted", f"Plan for v{plan.resolved_version} halted: {reason}")

    async def _notify(self, title: str, body: str) -> None:
        from case_service.integrations.platform_update_watcher import PlatformUpdateWatcher
        watcher = PlatformUpdateWatcher(self._factory)
        await watcher._notify_admins(title, body, "platform_update.plan")
