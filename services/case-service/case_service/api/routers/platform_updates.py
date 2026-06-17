"""PUO Phase 1 — platform update visibility + one-click current-env update.

Part of Platform Update Orchestration (docs/Future/platform-update-orchestration.md).
This is platform-code shipping, a sibling of HxDeploy — it never touches
Studio-artifact deployment.

Design rules enforced here:
- Live version resolution: the channel manifest (channels.json) is fetched at
  request time (10-min cache); no target version is ever persisted. Rejecting
  or ignoring an update stores nothing — the next look always sees the
  current channel pin.
- Web never executes updates: POST /request only writes `.update-request` in
  the repo root; the local systemd agent (update-velaris.sh --auto) picks it
  up, honours the maintenance window (unless mode="now"), runs the full
  update (backup → checkout → images → migrations → .env sync → restart →
  health gate) and reports via the `.update-status` beacon.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.auth.dependencies import get_current_user, require_role
from case_service.auth.models import AuthenticatedUser
from case_service.db.session import get_analytics_session as get_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/platform/update", tags=["platform-updates"])

_MANIFEST_CACHE_TTL = 600  # seconds
_manifest_cache: dict[str, Any] = {"fetched_at": 0.0, "data": None}


# ── Repo root / config discovery ─────────────────────────────────────────────

def _repo_root() -> Path:
    """Walk up from this file until velaris.yaml is found (repo root)."""
    p = Path(__file__).resolve()
    for parent in p.parents:
        if (parent / "velaris.yaml").exists():
            return parent
    return Path.cwd()


def _read_platform_config() -> dict[str, Any]:
    root = _repo_root()
    try:
        cfg = yaml.safe_load((root / "velaris.yaml").read_text()) or {}
    except Exception as exc:
        logger.warning("platform-update: cannot read velaris.yaml: %s", exc)
        cfg = {}
    v = cfg.get("velaris", {}) or {}
    updates = v.get("updates", {}) or {}
    return {
        "root": root,
        "version": str(v.get("version", "unknown")),
        "source": updates.get("source", "github"),
        "github_repo": updates.get("github_repo", ""),
        "server_url": updates.get("server_url", ""),
        "channel": updates.get("channel", "stable"),
        "manifest_branch": updates.get("manifest_branch", "main"),
        "auto_update": bool(updates.get("auto_update", False)),
        "update_window": updates.get("update_window", ""),
    }


# ── Channel manifest (live resolution, short cache) ──────────────────────────

async def _fetch_manifest(cfg: dict[str, Any]) -> dict[str, Any] | None:
    now = time.monotonic()
    if _manifest_cache["data"] is not None and now - _manifest_cache["fetched_at"] < _MANIFEST_CACHE_TTL:
        return _manifest_cache["data"]

    url = None
    if cfg["source"] == "github" and cfg["github_repo"]:
        url = (
            f"https://raw.githubusercontent.com/{cfg['github_repo']}/"
            f"{cfg['manifest_branch']}/channels.json"
        )
    elif cfg["source"] == "server" and cfg["server_url"]:
        url = cfg["server_url"].removesuffix("/latest") + "/channels.json"
    if not url:
        return None

    try:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
            resp = await client.get(url)
        if resp.status_code != 200:
            return None
        data = resp.json()
    except Exception as exc:
        logger.debug("platform-update: manifest unreachable: %s", exc)
        return None

    _manifest_cache["data"] = data
    _manifest_cache["fetched_at"] = now
    return data


def _version_tuple(v: str) -> tuple:
    try:
        return tuple(int(x) for x in v.split("."))
    except Exception:
        return (0,)


# ── PUO Phase 2: business-calendar scheduling ────────────────────────────────
#
# An approved update (mode="window") executes after business hours, taken
# from the /admin business calendars — the same calendars the SLA engine
# uses. We compute the next after-hours timestamp at approval time and store
# it in .update-request; the local agent waits for it.

def _next_after_hours_slot(cal, now: datetime) -> datetime:
    """Next moment that is outside business hours per the calendar.

    Business hours = a work_day (ISO Mon=1..Sun=7), not a holiday, between
    work_start_hour and work_end_hour in the calendar's timezone. If `now`
    is already after-hours the slot is now.
    """
    from zoneinfo import ZoneInfo

    try:
        tz = ZoneInfo(cal.timezone or "UTC")
    except Exception:
        tz = timezone.utc
    local = now.astimezone(tz)

    work_days = set(cal.work_days or [1, 2, 3, 4, 5])
    holidays = set(cal.holidays or [])

    def in_business_hours(dt: datetime) -> bool:
        if dt.isoweekday() not in work_days:
            return False
        if dt.strftime("%Y-%m-%d") in holidays:
            return False
        return cal.work_start_hour <= dt.hour < cal.work_end_hour

    if not in_business_hours(local):
        return now
    # Inside business hours → today's work_end in the calendar's timezone
    slot_local = local.replace(hour=cal.work_end_hour, minute=0, second=0, microsecond=0)
    return slot_local.astimezone(timezone.utc)


async def _resolve_calendar(session: AsyncSession, calendar_id: str | None):
    from case_service.db.models import BusinessCalendarModel

    if calendar_id:
        cal = await session.get(BusinessCalendarModel, calendar_id)
        if not cal:
            raise HTTPException(404, f"Business calendar {calendar_id} not found")
        return cal
    return (await session.execute(
        select(BusinessCalendarModel).order_by(BusinessCalendarModel.created_at).limit(1)
    )).scalar_one_or_none()


# ── Local state files (request + beacon) ─────────────────────────────────────

def _read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return None


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/status")
async def update_status(
    _: AuthenticatedUser = Depends(require_role("admin")),
):
    """Current platform version, channel pin, release metadata, beacon, pending request."""
    cfg = _read_platform_config()
    manifest = await _fetch_manifest(cfg)

    target = None
    release_meta: dict[str, Any] = {}
    if manifest:
        target = manifest.get(cfg["channel"]) or None
        if target:
            release_meta = (manifest.get("releases", {}) or {}).get(target, {}) or {}

    update_available = bool(
        target
        and target != cfg["version"]
        and _version_tuple(target) > _version_tuple(cfg["version"])
    )

    root: Path = cfg["root"]
    return {
        "current_version": cfg["version"],
        "channel": cfg["channel"],
        "target_version": target,
        "update_available": update_available,
        "release": {
            "notes_url": release_meta.get(
                "notes_url",
                f"https://github.com/{cfg['github_repo']}/releases/tag/v{target}" if target and cfg["github_repo"] else None,
            ),
            "security": bool(release_meta.get("security", False)),
            "min_upgrade_from": release_meta.get("min_upgrade_from"),
        } if target else None,
        "manifest_reachable": manifest is not None,
        "auto_update": cfg["auto_update"],
        "update_window": cfg["update_window"],
        "pending_request": _read_json_file(root / ".update-request"),
        "last_update_status": _read_json_file(root / ".update-status"),
    }


class UpdateRequestBody(BaseModel):
    mode: str = "window"  # "window" = next after-hours slot | "now" = next agent tick
    calendar_id: str | None = None  # business calendar; default = first /admin calendar


@router.post("/request", status_code=202)
async def request_update(
    body: UpdateRequestBody,
    user: AuthenticatedUser = Depends(require_role("admin")),
    session: AsyncSession = Depends(get_session),
):
    """Approve a platform update for THIS environment.

    Writes `.update-request` for the local agent. No version is stored —
    the agent resolves the channel pin live at execution time, so an
    approval always installs the vendor's current pin for this channel.
    """
    if body.mode not in ("window", "now"):
        raise HTTPException(400, "mode must be 'window' or 'now'")

    cfg = _read_platform_config()
    manifest = await _fetch_manifest(cfg)
    target = manifest.get(cfg["channel"]) if manifest else None
    if not target:
        raise HTTPException(503, "Channel manifest unreachable — cannot request an update now.")
    if target == cfg["version"]:
        raise HTTPException(409, f"Already on the channel version (v{cfg['version']}).")
    if _version_tuple(target) < _version_tuple(cfg["version"]):
        raise HTTPException(
            409,
            f"Channel pins v{target} but v{cfg['version']} is installed — downgrades are never automatic.",
        )

    # Phase 2: mode="window" schedules into the next after-hours slot from the
    # /admin business calendar. Falls back to the velaris.yaml update_window
    # when no calendar exists.
    scheduled_for: str | None = None
    calendar_name: str | None = None
    if body.mode == "window":
        cal = await _resolve_calendar(session, body.calendar_id)
        if cal is not None:
            slot = _next_after_hours_slot(cal, datetime.now(timezone.utc))
            scheduled_for = slot.isoformat()
            calendar_name = cal.name

    request_payload = {
        # Deliberately NO target version here — live resolution rule.
        "mode": body.mode,
        "requested_by": user.user_id,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "channel": cfg["channel"],
        "scheduled_for": scheduled_for,
        "calendar": calendar_name,
    }
    (cfg["root"] / ".update-request").write_text(json.dumps(request_payload, indent=2))
    logger.info(
        "platform-update: request written by %s (mode=%s, channel=%s, scheduled_for=%s)",
        user.user_id, body.mode, cfg["channel"], scheduled_for,
    )
    if body.mode == "now":
        note = "The local update agent will apply the channel's current version at its next check."
    elif scheduled_for:
        note = f"Scheduled after business hours ({calendar_name}): {scheduled_for}."
    else:
        note = "No business calendar configured — the agent will apply it in the next maintenance window."
    return {
        "status": "requested",
        "mode": body.mode,
        "scheduled_for": scheduled_for,
        "calendar": calendar_name,
        "note": note,
    }


@router.delete("/request", status_code=204)
async def cancel_update_request(
    user: AuthenticatedUser = Depends(require_role("admin")),
):
    cfg = _read_platform_config()
    req = cfg["root"] / ".update-request"
    if req.exists():
        req.unlink()
        logger.info("platform-update: pending request cancelled by %s", user.user_id)


# ═══════════════════════════════════════════════════════════════════════════
# PUO Phase 3 — fleet orchestration (plans, rings, remote triggers)
# ═══════════════════════════════════════════════════════════════════════════

from fastapi import Header, Request  # noqa: E402


async def _env_for_peer_key(session: AsyncSession, authorization: str | None):
    """M2M auth: Bearer <import_api_key> matched against environment_registry.

    Mirrors HxDeploy's import_bundle trust model — the same key an admin
    configured for artifact push authorises platform-update peer calls.
    """
    from case_service.db.models import EnvironmentRegistryModel

    key = None
    if authorization and authorization.lower().startswith("bearer "):
        key = authorization[7:].strip()
    if not key:
        raise HTTPException(401, "Missing peer key — provide Authorization: Bearer <key>")
    env = (await session.execute(
        select(EnvironmentRegistryModel).where(EnvironmentRegistryModel.import_api_key == key)
    )).scalars().first()
    if env is None:
        raise HTTPException(401, "Unknown peer key")
    return env


@router.post("/trigger", status_code=202)
async def peer_trigger_update(
    body: UpdateRequestBody,
    session: AsyncSession = Depends(get_session),
    authorization: str | None = Header(default=None),
):
    """M2M: an orchestrating environment asks THIS environment to update itself.

    Writes the same .update-request the local UI flow writes — the local
    agent resolves the channel pin live and applies it after hours. The
    caller can never name a version (live-resolution rule + channel pin
    means a compromised key can only trigger vendor-published versions).
    """
    env = await _env_for_peer_key(session, authorization)
    if body.mode not in ("window", "now"):
        raise HTTPException(400, "mode must be 'window' or 'now'")

    cfg = _read_platform_config()
    # Never clobber a pending admin rollback with an orchestrator update
    pending = _read_json_file(cfg["root"] / ".update-request") or {}
    if pending.get("action") == "rollback":
        raise HTTPException(409, "A rollback is pending on this environment — update trigger refused.")

    scheduled_for = None
    calendar_name = None
    if body.mode == "window":
        cal = await _resolve_calendar(session, None)
        if cal is not None:
            slot = _next_after_hours_slot(cal, datetime.now(timezone.utc))
            scheduled_for = slot.isoformat()
            calendar_name = cal.name

    (cfg["root"] / ".update-request").write_text(json.dumps({
        "mode": body.mode,
        "requested_by": f"peer:{env.name}",
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "channel": cfg["channel"],
        "scheduled_for": scheduled_for,
        "calendar": calendar_name,
    }, indent=2))
    logger.info("platform-update: peer trigger from '%s' (mode=%s)", env.name, body.mode)
    return {"status": "requested", "scheduled_for": scheduled_for}


@router.get("/peer-status")
async def peer_status(
    session: AsyncSession = Depends(get_session),
    authorization: str | None = Header(default=None),
):
    """M2M: report this environment's installed version + last update outcome."""
    await _env_for_peer_key(session, authorization)
    cfg = _read_platform_config()
    root: Path = cfg["root"]
    return {
        "current_version": cfg["version"],
        "channel": cfg["channel"],
        "pending_request": _read_json_file(root / ".update-request"),
        "last_update_status": _read_json_file(root / ".update-status"),
    }


# ── Step-up auth (PUO Phase 4): the two human gates + rollback re-verify the
#    admin's password (and TOTP when MFA is enrolled) at the moment of
#    approval. A stolen session token alone cannot promote or revert platform
#    code. Brute force on the password field is capped by a D4 sliding-window
#    limiter (5 attempts / 5 min / IP).

from case_service.middleware.endpoint_rate_limit import rate_limit  # noqa: E402

_stepup_rl = rate_limit(max_calls=5, window_seconds=300, name="step-up approval")


class StepUpBody(BaseModel):
    password: str = ""
    mfa_code: str | None = None
    # Group J: a WebAuthn assertion (from /auth/real/webauthn/stepup/options)
    # is accepted instead of password+TOTP — phishing-resistant possession
    # proof with on-device user verification.
    webauthn_credential: dict | None = None


async def _verify_step_up(session: AsyncSession, user: AuthenticatedUser, body: StepUpBody) -> None:
    import bcrypt
    import pyotp
    from case_service.db.models import HelixUserModel
    from case_service.hxbridge.encryption import decrypt_credentials

    # Group J: passkey path — the assertion must verify against a challenge
    # issued for THIS user with purpose="stepup" (a login assertion replayed
    # here fails the purpose check; another user's fails the user pin).
    # User verification (biometric/PIN) is REQUIRED: step-up replaces
    # password+TOTP, so possession of the authenticator alone is not enough.
    if body.webauthn_credential is not None:
        from case_service.auth.webauthn_service import complete_authentication
        try:
            _cred, user_verified = await complete_authentication(
                session, body.webauthn_credential, purpose="stepup",
                expected_user_id=str(user.user_id),
            )
            if not user_verified:
                raise ValueError("user verification required for step-up")
            return
        except Exception:
            logger.warning("PUO step-up: passkey assertion failed for %s", user.user_id)
            raise HTTPException(401, "Step-up verification failed")

    try:
        uid = uuid.UUID(str(user.user_id))
    except Exception:
        raise HTTPException(401, "Step-up verification unavailable for this account")
    u = await session.get(HelixUserModel, uid)
    if u is None or not u.password_hash:
        raise HTTPException(401, "Step-up verification unavailable for this account")
    try:
        ok = bcrypt.checkpw(body.password.encode(), u.password_hash.encode())
    except Exception:
        ok = False
    if not ok:
        logger.warning("PUO step-up: wrong password for %s", user.user_id)
        raise HTTPException(401, "Step-up verification failed")
    if u.mfa_enabled:
        if not body.mfa_code:
            raise HTTPException(401, "MFA code required for this approval")
        secret = decrypt_credentials(u.mfa_secret_enc)["secret"] if u.mfa_secret_enc else ""
        if not secret or not pyotp.TOTP(secret).verify(body.mfa_code, valid_window=1):
            logger.warning("PUO step-up: bad MFA code for %s", user.user_id)
            raise HTTPException(401, "Step-up verification failed")


# ── Rollout plans (admin) ─────────────────────────────────────────────────────

def _plan_out(plan, runs, env_labels) -> dict:
    return {
        "id": str(plan.id),
        "resolved_version": plan.resolved_version,
        "channel": plan.channel,
        "soak_hours": plan.soak_hours,
        "state": plan.state,
        "halted_reason": plan.halted_reason,
        "approved_by": plan.approved_by,
        "approved_at": plan.approved_at.isoformat() if plan.approved_at else None,
        "prod_approved_by": plan.prod_approved_by,
        "soak_started_at": plan.soak_started_at.isoformat() if plan.soak_started_at else None,
        "created_at": plan.created_at.isoformat() if plan.created_at else None,
        "runs": [{
            "id": str(r.id),
            "environment_id": str(r.environment_id),
            "environment": env_labels.get(r.environment_id, str(r.environment_id)),
            "ring_order": r.ring_order,
            "is_final_ring": r.is_final_ring,
            "state": r.state,
            "detail": r.detail,
            "triggered_at": r.triggered_at.isoformat() if r.triggered_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        } for r in sorted(runs, key=lambda x: x.ring_order)],
    }


async def _load_plan(session: AsyncSession, plan_id: uuid.UUID):
    from case_service.db.models import PlatformUpdatePlanModel
    plan = await session.get(PlatformUpdatePlanModel, plan_id)
    if plan is None:
        raise HTTPException(404, "Plan not found")
    return plan


async def _plan_runs(session: AsyncSession, plan_id: uuid.UUID):
    from case_service.db.models import PlatformUpdateRunModel
    return (await session.execute(
        select(PlatformUpdateRunModel).where(PlatformUpdateRunModel.plan_id == plan_id)
    )).scalars().all()


async def _env_labels(session: AsyncSession) -> dict:
    from case_service.db.models import EnvironmentRegistryModel
    envs = (await session.execute(select(EnvironmentRegistryModel))).scalars().all()
    return {e.id: e.label or e.name for e in envs}


@router.get("/plans")
async def list_plans(
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(require_role("admin")),
):
    from case_service.db.models import PlatformUpdatePlanModel
    plans = (await session.execute(
        select(PlatformUpdatePlanModel).order_by(PlatformUpdatePlanModel.created_at.desc()).limit(20)
    )).scalars().all()
    labels = await _env_labels(session)
    out = []
    for p in plans:
        out.append(_plan_out(p, await _plan_runs(session, p.id), labels))
    return {"plans": out, "total": len(out)}


@router.post("/plans/{plan_id}/approve", dependencies=[Depends(_stepup_rl)])
async def approve_plan(
    plan_id: uuid.UUID,
    body: StepUpBody,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(require_role("admin")),
):
    """Activate a draft plan — the engine then triggers non-final rings.

    Human gate 1: requires step-up auth (password + TOTP when enrolled).
    """
    await _verify_step_up(session, user, body)
    plan = await _load_plan(session, plan_id)
    if plan.state != "draft":
        raise HTTPException(409, f"Plan is '{plan.state}', only drafts can be approved")

    # Live-resolution guard: the plan must still match the channel pin
    cfg = _read_platform_config()
    manifest = await _fetch_manifest(cfg)
    pin = manifest.get(plan.channel) if manifest else None
    if pin != plan.resolved_version:
        plan.state = "superseded"
        plan.halted_reason = f"channel now pins v{pin}, plan was for v{plan.resolved_version}"
        await session.commit()
        raise HTTPException(409, f"Plan superseded — channel now pins v{pin}. A new plan will be drafted after dev updates.")

    plan.state = "active"
    plan.approved_by = user.user_id
    plan.approved_at = datetime.now(timezone.utc)
    await session.commit()
    logger.info("PUO: plan %s approved by %s (v%s)", plan_id, user.user_id, plan.resolved_version)
    return {"status": "active"}


@router.post("/plans/{plan_id}/approve-prod", dependencies=[Depends(_stepup_rl)])
async def approve_prod(
    plan_id: uuid.UUID,
    body: StepUpBody,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(require_role("admin")),
):
    """Second deliberate gate: fire the final (prod) ring after the soak.

    Human gate 2: requires step-up auth (password + TOTP when enrolled).
    """
    await _verify_step_up(session, user, body)
    plan = await _load_plan(session, plan_id)
    if plan.state != "awaiting_prod_approval":
        raise HTTPException(409, f"Plan is '{plan.state}' — prod approval unlocks only after the soak completes")
    plan.state = "prod_approved"
    plan.prod_approved_by = user.user_id
    plan.prod_approved_at = datetime.now(timezone.utc)
    await session.commit()
    logger.info("PUO: prod ring approved by %s for plan %s", user.user_id, plan_id)
    return {"status": "prod_approved"}


@router.post("/plans/{plan_id}/halt")
async def halt_plan(
    plan_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(require_role("admin")),
):
    plan = await _load_plan(session, plan_id)
    if plan.state in ("completed", "halted", "superseded"):
        raise HTTPException(409, f"Plan already terminal: {plan.state}")
    plan.state = "halted"
    plan.halted_reason = f"halted by {user.user_id}"
    for r in await _plan_runs(session, plan_id):
        if r.state in ("pending", "awaiting_approval", "approved", "triggered", "running"):
            r.state = "halted"
    await session.commit()
    return {"status": "halted"}


# ═══════════════════════════════════════════════════════════════════════════
# PUO Phase 4 — rollback, modes policy, fleet visibility
# ═══════════════════════════════════════════════════════════════════════════

class RollbackRequestBody(StepUpBody):
    to_version: str | None = None  # default: previous_version from the beacon


@router.post("/rollback", status_code=202, dependencies=[Depends(_stepup_rl)])
async def request_rollback(
    body: RollbackRequestBody,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(require_role("admin")),
):
    """Revert THIS environment to its previous version (code + images only).

    DB migrations are never reverted — expand/contract migrations let old
    code run on the new schema. Restoring the pre-update DB backup is a
    separate, deliberately manual operation. Destructive → step-up auth.
    """
    await _verify_step_up(session, user, body)
    cfg = _read_platform_config()
    beacon = _read_json_file(cfg["root"] / ".update-status") or {}
    target = body.to_version or beacon.get("previous_version")
    if not target:
        raise HTTPException(409, "No previous version recorded — nothing to roll back to.")
    if target == cfg["version"]:
        raise HTTPException(409, f"Already on v{target}.")

    (cfg["root"] / ".update-request").write_text(json.dumps({
        "action": "rollback",
        "to_version": target,
        "mode": "now",
        "requested_by": user.user_id,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "channel": cfg["channel"],
    }, indent=2))
    logger.warning("platform-update: ROLLBACK to v%s requested by %s", target, user.user_id)
    return {
        "status": "requested",
        "to_version": target,
        "note": "Code and images revert at the next agent check. DB schema stays; "
                "restoring the pre-update backup remains a manual decision.",
    }


# ── Update modes policy (auto-soak | per-env | manual) ───────────────────────

class SettingsBody(BaseModel):
    mode: str | None = None
    default_soak_hours: int | None = None


@router.get("/settings")
async def get_update_settings(
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(require_role("admin")),
):
    from case_service.db.models import PlatformUpdateSettingsModel
    row = await session.get(PlatformUpdateSettingsModel, 1)
    if row is None:
        row = PlatformUpdateSettingsModel(id=1)
        session.add(row)
        await session.commit()
    return {"mode": row.mode, "default_soak_hours": row.default_soak_hours}


@router.put("/settings")
async def put_update_settings(
    body: SettingsBody,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(require_role("admin")),
):
    from case_service.db.models import PlatformUpdateSettingsModel
    if body.mode is not None and body.mode not in ("auto-soak", "per-env", "manual"):
        raise HTTPException(400, "mode must be auto-soak, per-env, or manual")
    if body.default_soak_hours is not None and not (1 <= body.default_soak_hours <= 24 * 30):
        raise HTTPException(400, "default_soak_hours must be between 1 and 720")
    row = await session.get(PlatformUpdateSettingsModel, 1)
    if row is None:
        row = PlatformUpdateSettingsModel(id=1)
        session.add(row)
    if body.mode is not None:
        row.mode = body.mode
    if body.default_soak_hours is not None:
        row.default_soak_hours = body.default_soak_hours
    await session.commit()
    logger.info("PUO settings updated by %s: mode=%s soak=%s", user.user_id, row.mode, row.default_soak_hours)
    return {"mode": row.mode, "default_soak_hours": row.default_soak_hours}


@router.post("/runs/{run_id}/approve")
async def approve_run(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(require_role("admin")),
):
    """per-env mode: each ring waits for its own explicit approval."""
    from case_service.db.models import PlatformUpdateRunModel
    run = await session.get(PlatformUpdateRunModel, run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    if run.state != "awaiting_approval":
        raise HTTPException(409, f"Run is '{run.state}', not awaiting approval")
    run.state = "approved"
    run.detail = f"ring approved by {user.user_id}"
    await session.commit()
    return {"status": "approved"}


# ── Fleet platform versions (Environments-tab column) ────────────────────────

@router.get("/environments")
async def fleet_versions(
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(require_role("admin")),
):
    """Platform version + last update outcome of every registered environment."""
    import asyncio as _asyncio
    from case_service.db.models import EnvironmentRegistryModel

    envs = (await session.execute(
        select(EnvironmentRegistryModel).order_by(EnvironmentRegistryModel.order_index)
    )).scalars().all()

    async def probe(env):
        if not env.url or not env.import_api_key:
            return {"id": str(env.id), "label": env.label or env.name,
                    "reachable": False, "platform_version": None, "last_result": None}
        try:
            async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
                resp = await client.get(
                    env.url.rstrip("/") + "/api/v1/platform/update/peer-status",
                    headers={"Authorization": f"Bearer {env.import_api_key}"},
                )
            if resp.status_code >= 400:
                raise RuntimeError(f"HTTP {resp.status_code}")
            data = resp.json()
            beacon = data.get("last_update_status") or {}
            return {"id": str(env.id), "label": env.label or env.name, "reachable": True,
                    "platform_version": data.get("current_version"),
                    "last_result": beacon.get("result")}
        except Exception:
            return {"id": str(env.id), "label": env.label or env.name,
                    "reachable": False, "platform_version": None, "last_result": None}

    results = await _asyncio.gather(*(probe(e) for e in envs))
    cfg = _read_platform_config()
    return {"this_environment": {"platform_version": cfg["version"], "channel": cfg["channel"]},
            "environments": list(results)}
