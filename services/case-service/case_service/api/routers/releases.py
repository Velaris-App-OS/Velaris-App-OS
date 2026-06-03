"""P66 — Scheduled Releases: manage Velaris feature release dates and notes.

Public:
  GET  /releases/flags          — returns {feature_key: enabled} map (no auth)
  GET  /releases/manifest       — reads releases.txt, returns parsed lines + DB status (admin)

Admin (superadmin only):
  GET    /releases               — list all releases
  POST   /releases               — create a release entry
  PUT    /releases/{id}          — update date / notes / status
  DELETE /releases/{id}          — delete
  POST   /releases/{id}/publish  — immediately enable (manual release now)
  POST   /releases/{id}/rollback — disable and mark rolled_back
"""
from __future__ import annotations

import os
import time
import uuid
import logging
from datetime import datetime, timezone, date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser
from case_service.db.session import get_session

log = logging.getLogger(__name__)

router = APIRouter(tags=["releases"])

# ── Version-based feature flag cache ─────────────────────────────────────────
# Populated by release_cron() after each cycle (hourly).
# Feature checks call is_feature_enabled() — no DB round-trip at check time.

_ENABLED_VERSIONS: dict[str, str | None] = {}  # feature_key -> version string or None


def version_gte(actual: str | None, required: str) -> bool:
    """Return True if actual version >= required. Handles vX.Y.Z format."""
    if not actual:
        return False
    try:
        a = tuple(int(x) for x in actual.lstrip("v").split("."))
        r = tuple(int(x) for x in required.lstrip("v").split("."))
        return a >= r
    except (ValueError, AttributeError):
        return (actual or "") >= required


def is_feature_enabled(feature_key: str) -> bool:
    """Check if a feature is enabled.

    Returns True if the DB has any non-null version for this feature key.
    Version comparison is the DB's concern — the code only needs on/off.
    Reads from the in-memory cache populated by release_cron() — no DB call.
    Returns False during the first ~10 seconds after startup (before first cron cycle).
    """
    return _ENABLED_VERSIONS.get(feature_key) is not None


async def _refresh_version_cache(session: AsyncSession) -> None:
    """Reload _ENABLED_VERSIONS from DB. Called by the cron after each cycle."""
    from case_service.db.models import ScheduledReleaseModel
    rows = (await session.execute(select(ScheduledReleaseModel))).scalars().all()
    _ENABLED_VERSIONS.clear()
    for r in rows:
        _ENABLED_VERSIONS[r.feature_key] = r.enabled
    log.info("Feature flag cache refreshed: %s", dict(_ENABLED_VERSIONS))


# ── Model (inline to avoid circular import) ───────────────────────────────────

def _utcnow():
    return datetime.now(timezone.utc)


# ── Schemas ───────────────────────────────────────────────────────────────────

class ReleaseIn(BaseModel):
    feature_key:   str
    version:       Optional[str] = None
    title:         str
    description:   Optional[str] = None
    release_notes: Optional[str] = None
    release_date:  Optional[date] = None
    status:        Optional[str] = "draft"


class ReleaseUpdate(BaseModel):
    title:         Optional[str] = None
    description:   Optional[str] = None
    release_notes: Optional[str] = None
    release_date:  Optional[date] = None
    status:        Optional[str] = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _require_superadmin(user: AuthenticatedUser):
    if "superadmin" not in (user.roles or []):
        raise HTTPException(403, "Superadmin required")


async def _get_or_404(session: AsyncSession, release_id: uuid.UUID):
    from case_service.db.models import ScheduledReleaseModel
    r = await session.get(ScheduledReleaseModel, release_id)
    if not r:
        raise HTTPException(404, "Release not found")
    return r


def _serialize(r) -> dict:
    return {
        "id":            str(r.id),
        "feature_key":   r.feature_key,
        "version":       r.version,
        "title":         r.title,
        "description":   r.description,
        "release_notes": r.release_notes,
        "release_date":  r.release_date.isoformat() if r.release_date else None,
        "status":        r.status,
        "enabled":       r.enabled,          # version string (e.g. "v1.2.0") or null
        "enabled_bool":  r.enabled is not None,   # convenience for UI boolean checks
        "created_at":    r.created_at.isoformat(),
        "updated_at":    r.updated_at.isoformat(),
        "released_at":   r.released_at.isoformat() if r.released_at else None,
    }


# ── Public: feature flags (no auth) ──────────────────────────────────────────

@router.get("/releases/flags")
async def get_release_flags(session: AsyncSession = Depends(get_session)):
    """Return {feature_key: version_string} for all enabled features. Null = not enabled."""
    from case_service.db.models import ScheduledReleaseModel
    rows = (await session.execute(select(ScheduledReleaseModel))).scalars().all()
    return {r.feature_key: r.enabled for r in rows}  # version string or null


# ── Admin: CRUD ───────────────────────────────────────────────────────────────

@router.get("/releases")
async def list_releases(
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _require_superadmin(current_user)
    from case_service.db.models import ScheduledReleaseModel
    rows = (await session.execute(
        select(ScheduledReleaseModel).order_by(ScheduledReleaseModel.release_date.asc().nullslast())
    )).scalars().all()
    return {"releases": [_serialize(r) for r in rows]}


@router.post("/releases")
async def create_release(
    body: ReleaseIn,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _require_superadmin(current_user)
    from case_service.db.models import ScheduledReleaseModel
    r = ScheduledReleaseModel(
        feature_key   = body.feature_key.strip().lower(),
        version       = body.version.strip() if body.version else None,
        title         = body.title.strip(),
        description   = body.description,
        release_notes = body.release_notes,
        release_date  = body.release_date,
        status        = body.status or "draft",
    )
    session.add(r)
    await session.commit()
    await session.refresh(r)
    return _serialize(r)


@router.put("/releases/{release_id}")
async def update_release(
    release_id: uuid.UUID,
    body: ReleaseUpdate,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _require_superadmin(current_user)
    r = await _get_or_404(session, release_id)
    if body.title         is not None: r.title         = body.title.strip()
    if body.description   is not None: r.description   = body.description
    if body.release_notes is not None: r.release_notes = body.release_notes
    if body.release_date  is not None: r.release_date  = body.release_date
    if body.status        is not None:
        if body.status not in ("draft", "scheduled", "released", "rolled_back"):
            raise HTTPException(400, "Invalid status")
        r.status = body.status
    r.updated_at = _utcnow()
    session.add(r)
    await session.commit()
    return _serialize(r)


@router.delete("/releases/{release_id}")
async def delete_release(
    release_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _require_superadmin(current_user)
    r = await _get_or_404(session, release_id)
    await session.delete(r)
    await session.commit()
    return {"ok": True}


@router.post("/releases/{release_id}/publish")
async def publish_release(
    release_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _require_superadmin(current_user)
    r = await _get_or_404(session, release_id)
    r.enabled     = r.version or "v1.0.0"   # store the version string, not True
    r.status      = "released"
    r.released_at = _utcnow()
    r.updated_at  = _utcnow()
    session.add(r)
    await session.commit()
    await _refresh_version_cache(session)
    log.info("Release manually published: %s @ %s (%s)", r.feature_key, r.enabled, r.title)
    return _serialize(r)


@router.post("/releases/{release_id}/rollback")
async def rollback_release(
    release_id: uuid.UUID,
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _require_superadmin(current_user)
    r = await _get_or_404(session, release_id)
    r.enabled    = None    # null = disabled
    r.status     = "rolled_back"
    r.updated_at = _utcnow()
    session.add(r)
    await session.commit()
    await _refresh_version_cache(session)
    log.info("Release rolled back: %s (%s)", r.feature_key, r.title)
    return _serialize(r)


# ── Manifest: read releases.txt ──────────────────────────────────────────────

RELEASES_TXT = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "releases.txt")

def _read_manifest() -> list[dict]:
    """Parse releases.txt — each line: `feature_key version` (e.g. marketplace v1.2.0)."""
    path = os.environ.get("VELARIS_RELEASES_TXT", RELEASES_TXT)
    entries = []
    try:
        with open(path) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) >= 2:
                    entries.append({"feature_key": parts[0], "version": parts[1]})
                elif len(parts) == 1:
                    entries.append({"feature_key": parts[0], "version": None})
    except FileNotFoundError:
        pass
    return entries


@router.get("/releases/manifest")
async def get_manifest(
    current_user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Read releases.txt and cross-reference with DB. Returns each line with registered=True/False."""
    _require_superadmin(current_user)
    from case_service.db.models import ScheduledReleaseModel

    # Load DB records keyed by feature_key
    db_rows = (await session.execute(select(ScheduledReleaseModel))).scalars().all()
    db_map = {r.feature_key: _serialize(r) for r in db_rows}

    manifest = _read_manifest()
    result = []
    for entry in manifest:
        key = entry["feature_key"]
        db_rec = db_map.get(key)
        result.append({
            "feature_key": key,
            "version":     entry["version"],
            "registered":  db_rec is not None,
            "db":          db_rec,  # None if not yet in DB
        })

    # Also include DB entries not in the txt (manually created)
    txt_keys = {e["feature_key"] for e in manifest}
    for key, rec in db_map.items():
        if key not in txt_keys:
            result.append({
                "feature_key": key,
                "version":     rec.get("version"),
                "registered":  True,
                "db":          rec,
            })

    return {"manifest": result}


# ── Cron: auto-release on due date ────────────────────────────────────────────

async def release_cron():
    """Runs at startup and every hour. Enables any scheduled release whose date has arrived."""
    import asyncio
    from case_service.db.session import get_session_factory

    await asyncio.sleep(10)  # wait for DB to be ready on startup

    # Load feature flag cache immediately on first boot — no waiting for first cron cycle.
    # This means is_feature_enabled() returns correct values from second 10 onward,
    # not second 3600.
    try:
        factory = get_session_factory()
        async with factory() as session:
            await _refresh_version_cache(session)
    except Exception as exc:
        log.warning("Initial feature flag load failed: %s", exc)

    while True:
        try:
            from case_service.db.models import ScheduledReleaseModel
            factory = get_session_factory()
            async with factory() as session:
                today = datetime.now(timezone.utc).date()
                due = (await session.execute(
                    select(ScheduledReleaseModel).where(
                        ScheduledReleaseModel.status == "scheduled",
                        ScheduledReleaseModel.enabled.is_(None),   # NULL = not yet enabled
                        ScheduledReleaseModel.release_date <= today,
                    )
                )).scalars().all()

                for r in due:
                    r.enabled     = r.version or "v1.0.0"   # version string, not True
                    r.status      = "released"
                    r.released_at = datetime.now(timezone.utc)
                    r.updated_at  = datetime.now(timezone.utc)
                    session.add(r)
                    log.info("Auto-released feature: %s @ %s on %s", r.feature_key, r.enabled, today)

                if due:
                    await session.commit()

                # Refresh in-memory cache every cycle so is_feature_enabled() stays current
                await _refresh_version_cache(session)

        except Exception as exc:
            log.warning("release_cron error: %s", exc)

        await asyncio.sleep(3600)  # check every hour
