"""HxStatus — public status page + SLA reporting (§8.8).

External-facing uptime, fed by the health samples `integrations/health_sampler`
writes. Enterprise buyers expect a status page; the value is that it is
*public*, which is also the whole security problem.

## What makes the public route safe

**`detail` is never served publicly.** A health sample's `detail` carries the
raw exception text when a component fails — which can contain hostnames, table
names, driver messages and connection fragments. The public payload is built
field by field from a whitelist (`component`, `status`, uptime percentages,
`last_checked`); it is not the stored row with keys removed, because a
subtractive filter silently starts leaking the moment a new column is added.

**It is cached.** An unauthenticated endpoint that runs aggregate queries per
request is a free DoS lever against the OLTP database. Results are held for a
short window, so a flood costs one query rather than thousands.

**It is rate limited** per IP, using the same limiter the other public routes
use.

**It reports only fixed component names**, never a live inventory of internal
modules — a status page should not double as reconnaissance.

The authenticated companion route returns everything, including `detail`, for
operators.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.auth.dependencies import require_role
from velaris_shared import hxguard
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.config import get_settings
from velaris_shared.db.session import get_replica_session
from velaris_shared.middleware.endpoint_rate_limit import SlidingWindowLimiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/status", tags=["status"])

#: Public and unauthenticated — the same posture the storefront public routes
#: take. Generous enough for a browser that polls, tight enough that it is not
#: a lever against the database.
_status_rl = SlidingWindowLimiter(max_calls=60, window_seconds=60.0, name="status_public")

#: Windows the page reports. Anything longer than the sampler's retention
#: would silently under-report, so 30d is the ceiling (retention is 35d).
WINDOWS: dict[str, int] = {"24h": 1, "7d": 7, "30d": 30}

#: Cache TTL. A status page is not a real-time console; a minute of staleness
#: is invisible to a reader and removes the DoS lever entirely.
_CACHE_TTL_SECONDS = 30.0
_cache: dict[str, Any] = {"at": 0.0, "payload": None}


async def _uptime_by_component(
    session: AsyncSession, *, days: int,
) -> dict[str, dict[str, Any]]:
    """Percentage of samples that were `ok`, per component, over a window.

    `degraded` counts against uptime deliberately: a component answering
    slowly is not up as far as a customer is concerned, and a status page that
    calls it green is worse than no page.
    """
    from velaris_shared.db.models import HealthCheckResultModel

    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (await session.execute(
        select(
            HealthCheckResultModel.component,
            HealthCheckResultModel.status,
            func.count().label("n"),
        )
        .where(HealthCheckResultModel.checked_at >= since)
        .group_by(HealthCheckResultModel.component, HealthCheckResultModel.status)
    )).all()

    totals: dict[str, dict[str, int]] = {}
    for component, status, n in rows:
        bucket = totals.setdefault(component, {"ok": 0, "total": 0})
        bucket["total"] += int(n)
        if status == "ok":
            bucket["ok"] += int(n)

    return {
        component: {
            "samples": b["total"],
            # None, not 100, when nothing was sampled. Reporting perfect
            # uptime from zero measurements is a lie a status page must not
            # tell.
            "uptime_pct": (
                round(100.0 * b["ok"] / b["total"], 3) if b["total"] else None
            ),
        }
        for component, b in totals.items()
    }


async def _build_public_payload(session: AsyncSession) -> dict[str, Any]:
    from velaris_shared.db.models import HealthCheckResultModel
    from case_service.integrations import health_sampler as hs

    windows = {
        label: await _uptime_by_component(session, days=days)
        for label, days in WINDOWS.items()
    }

    components: list[dict[str, Any]] = []
    for name in (hs.DATABASE, hs.TELEMETRY):
        latest = (await session.execute(
            select(HealthCheckResultModel.status, HealthCheckResultModel.checked_at)
            .where(HealthCheckResultModel.component == name)
            .order_by(HealthCheckResultModel.checked_at.desc())
            .limit(1)
        )).first()

        # Whitelist construction — every field is named here on purpose. A
        # subtractive filter over the ORM row would start leaking the day
        # someone adds a column.
        components.append({
            "component": name,
            "status": latest.status if latest else "unknown",
            "last_checked": (
                latest.checked_at.isoformat() if latest and latest.checked_at else None
            ),
            "uptime": {
                label: windows[label].get(name, {}).get("uptime_pct")
                for label in WINDOWS
            },
        })

    overall = "operational"
    if any(c["status"] == "down" for c in components):
        overall = "major_outage"
    elif any(c["status"] == "degraded" for c in components):
        overall = "degraded"
    elif all(c["status"] == "unknown" for c in components):
        # No samples at all — say so rather than claiming health.
        overall = "unknown"

    return {
        "status": overall,
        "components": components,
        "windows": list(WINDOWS),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


from case_service.core.tenant_path_guard import platform_operator_only as _platform_operator_only

# authz-parity, authz-role-collisions and internal describe the whole install
# (2026-11-04: they answered every tenant's admin). Platform operator only.


@router.get("/public")
async def public_status(
    request: Request,
    session: AsyncSession = Depends(get_replica_session),
):
    """Public, unauthenticated status page data (§8.8).

    Carries no diagnostic detail, no internal inventory and no tenant data —
    only fixed component names, a coarse status, and uptime percentages.
    """
    ip = (request.client.host if request.client else "unknown") or "unknown"
    allowed, _retry = await _status_rl.allow_async(ip)
    if not allowed:
        raise HTTPException(429, "Too many requests — please try again shortly")

    now = time.monotonic()
    if _cache["payload"] is not None and (now - _cache["at"]) < _CACHE_TTL_SECONDS:
        return _cache["payload"]

    payload = await _build_public_payload(session)
    _cache["at"], _cache["payload"] = now, payload
    return payload


@router.get("/authz-parity")
async def authz_parity(
    _user: AuthenticatedUser = Depends(hxguard.guard("platform.status.read")),
    __op: None = Depends(_platform_operator_only),
):
    """Privilege-authorization shadow counters (Phase 2).

    The exit criterion for Phase 2 is *zero unexplained mismatches over a
    deployment cycle*. Without somewhere to read the counters that claim cannot
    be checked in a running system, only in a test — so this endpoint is part of
    the phase rather than an extra.

    - `compared` / `agreed` — the shadow ran and the two models concurred.
    - `mismatch_would_allow` — the privilege model is **more** permissive than
      the gate. The dangerous direction; investigate before Phase 3.
    - `mismatch_would_deny` — **less** permissive. Breaks users at cutover.
    - `unresolved_route` — a gate whose action could not be resolved, i.e. a hole
      in the inventory. Non-zero means Phase 3 would migrate it blind.
    - `error` — the shadow itself failed. Never affects a request; always a bug.

    Counters are per process and reset on restart, so read them from a worker
    that has been serving for a while.
    """
    from velaris_shared.hxguard import shadow

    snap = shadow.snapshot()
    mismatches = snap.get("mismatch_would_allow", 0) + snap.get("mismatch_would_deny", 0)
    return {
        "phase": 2,
        "enabled": bool(getattr(get_settings(), "hxguard_privilege_shadow", True)),
        "counters": snap,
        "clean": mismatches == 0 and snap.get("unresolved_route", 0) == 0
                 and snap.get("error", 0) == 0,
    }


@router.get("/authz-role-collisions")
async def authz_role_collisions(
    session: AsyncSession = Depends(get_replica_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("platform.status.read")),
    __op: None = Depends(_platform_operator_only),
):
    """Access roles whose NAME used to confer a platform role (Phase 4 step 1).

    Until step 1, `_enrich_with_access_group` merged access-role names into
    `user.roles`, so an access role named `admin` made its members platform
    admins. That merge is gone. Anyone who reached platform privileges only that
    way has lost them, and the request path cannot say so — a denial for a
    missing privilege looks exactly like a correct denial.

    Non-empty is not automatically a problem: it is a problem for the operators
    counted in `operators_affected`, and cosmetic for a role nobody holds.
    The fix is to grant the platform role on the account or to write the access
    role's platform intent as a dotted-namespace privilege entry — **never** to
    rename the role, which revokes case-level access for every member at once.
    """
    from velaris_shared.hxguard.role_names import colliding_access_roles

    found = await colliding_access_roles(session)
    return {
        "phase": 4,
        "name_merge_active": False,
        "collisions": found,
        "operators_affected": sum(r["operators_affected"] for r in found),
        "clean": not any(r["operators_affected"] for r in found),
    }


@router.get("/internal")
async def internal_status(
    days: int = 7,
    session: AsyncSession = Depends(get_replica_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("platform.status.read")),
    __op: None = Depends(_platform_operator_only),
):
    """Full status including diagnostic detail — admin only.

    This is where `detail` (raw exception text) is allowed to surface, because
    the caller is authenticated and privileged. It is the reason the public
    route builds its payload from a whitelist instead of reusing this one.
    """
    from velaris_shared.db.models import HealthCheckResultModel

    days = max(1, min(int(days), 30))
    since = datetime.now(timezone.utc) - timedelta(days=days)

    recent = (await session.execute(
        select(HealthCheckResultModel)
        .where(HealthCheckResultModel.checked_at >= since)
        .order_by(HealthCheckResultModel.checked_at.desc())
        .limit(500)
    )).scalars().all()

    return {
        "window_days": days,
        "uptime": await _uptime_by_component(session, days=days),
        "samples": [{
            "component": r.component,
            "status": r.status,
            "latency_ms": r.latency_ms,
            "detail": r.detail,
            "checked_at": r.checked_at.isoformat() if r.checked_at else None,
        } for r in recent],
    }


@router.get("/readiness")
async def readiness(
    session: AsyncSession = Depends(get_replica_session),
    user: AuthenticatedUser = Depends(hxguard.guard("platform.status.read")),
):
    """Every optional dependency — working, degraded or off, with a plain
    reason (R4). Reasons name settings, never hosts or keys. The platform
    operator sees every line; another tenant's administrator only what their
    users depend on, with their own mailbox and connectors. See
    `case_service/readiness.py`."""
    from case_service import readiness as r
    return await r.check_all(session, user)
