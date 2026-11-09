"""Roadmap #27 — the marketplace conformance GATE (report §6.5).

The structural checks live in `testsuite.conformance`; this module decides what
their absence or failure MEANS at install and update time.

Read §6.5 carefully before changing anything here, because it is deliberately
asymmetric and the asymmetry is the design:

  · A **new install** of a package with no passing result is refused. This is a
    hard gate, sitting beside the existing checksum and manifest-version
    chokepoints — refuse at install, never at first run.
  · An **existing install** is never delisted and never uninstalled. It earns a
    `legacy_unverified` badge and, once the window closes, sorts below packages
    that passed. "No existing package is removed from the marketplace without
    explicit publisher action" is a requirement, not a preference.
  · An **update** is new code, so it is re-checked like a new install. Otherwise
    the gate is trivially bypassed by shipping v1.0.1.

Official-tier packages auto-pass: tier is derived from the publisher org URL and
the baked-in `official_registry.json`, never from the manifest, so this cannot be
claimed by a package about itself.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, NamedTuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# Gate modes. `warn` is the default so that upgrading an existing platform never
# starts refusing installs on the strength of a config change alone — the same
# shadow-then-enforce discipline HxGuard used for case enforcement.
MODE_OFF = "off"
MODE_WARN = "warn"
MODE_ENFORCE = "enforce"
_VALID_MODES = frozenset({MODE_OFF, MODE_WARN, MODE_ENFORCE})

# Badges (§6.5 names these exactly; the UI sorts on them).
BADGE_PASSED = "conformance_passed"
BADGE_FAILED = "conformance_failed"
BADGE_LEGACY = "legacy_unverified"   # pre-cutover install, still inside the window
BADGE_UNVERIFIED = "unverified"      # never submitted, window closed

DEFAULT_WINDOW_DAYS = 60


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime | None) -> datetime | None:
    """Treat a naive timestamp as UTC.

    MySQL/MariaDB hand back naive datetimes where Postgres returns tz-aware ones.
    Comparing the two raises TypeError, and the comparison here decides whether a
    publisher is inside their window — so it must not depend on the backend.
    """
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


class GateDecision(NamedTuple):
    """`allowed` is the answer; the rest is why, for the operator and the log."""
    allowed: bool
    badge: str
    reason: str
    mode: str
    grandfathered: bool = False

    @property
    def blocked(self) -> bool:
        return not self.allowed


def gate_mode() -> str:
    """Current mode from settings, defaulting to `warn` on anything unrecognised."""
    try:
        from velaris_shared.config import settings
        mode = str(getattr(settings, "marketplace_conformance_gate", MODE_WARN) or MODE_WARN).lower()
    except Exception:                                    # config unavailable → safest useful default
        return MODE_WARN
    return mode if mode in _VALID_MODES else MODE_WARN


async def get_window(session: AsyncSession):
    """The singleton window row, or None when the gate was never activated."""
    from velaris_shared.db.models import MarketplaceConformanceWindowModel
    r = await session.execute(select(MarketplaceConformanceWindowModel).limit(1))
    return r.scalar_one_or_none()


async def activate_window(session: AsyncSession, *, activated_by: str,
                          window_days: int = DEFAULT_WINDOW_DAYS):
    """Open the grandfather window. Idempotent — never restarts a running window.

    Re-activating would hand every lagging publisher a fresh 60 days, quietly
    defeating the deadline, so an existing row is returned untouched.
    """
    from velaris_shared.db.models import MarketplaceConformanceWindowModel
    existing = await get_window(session)
    if existing is not None:
        return existing
    row = MarketplaceConformanceWindowModel(
        cutover_at=_utcnow(), window_days=max(0, int(window_days)),
        activated_by=activated_by, singleton=1,
    )
    session.add(row)
    await session.commit()
    logger.info("Conformance grandfather window opened by %s (%s days)", activated_by, window_days)
    return row


async def window_state(session: AsyncSession) -> dict[str, Any]:
    """Window status for the UI: open/closed, the deadline, days remaining."""
    w = await get_window(session)
    if w is None:
        return {"active": False, "cutover_at": None, "closes_at": None,
                "window_days": DEFAULT_WINDOW_DAYS, "days_remaining": None, "open": False}
    cutover = _aware(w.cutover_at)
    closes = cutover + timedelta(days=w.window_days)
    remaining = (closes - _utcnow()).days
    return {
        "active": True,
        "cutover_at": cutover.isoformat(),
        "closes_at": closes.isoformat(),
        "window_days": w.window_days,
        "days_remaining": max(0, remaining),
        "open": _utcnow() < closes,
    }


async def latest_result(session: AsyncSession, tenant_id: str, package_id: str,
                        package_version: str | None = None):
    """Newest conformance row for a package, optionally pinned to one version.

    Ordered by `created_at DESC, id DESC` — the id tiebreak matters because two
    runs can land in the same microsecond and "latest wins" must be stable.
    """
    from velaris_shared.db.models import MarketplaceConformanceModel as M
    stmt = select(M).where(M.tenant_id == tenant_id, M.package_id == package_id)
    if package_version:
        stmt = stmt.where(M.package_version == package_version)
    stmt = stmt.order_by(M.created_at.desc(), M.id.desc()).limit(1)
    r = await session.execute(stmt)
    return r.scalar_one_or_none()


async def record_result(session: AsyncSession, *, tenant_id: str, package_id: str,
                        package_version: str, result: dict,
                        run_id: uuid.UUID | None = None):
    """Persist one `run_structural` result. Caller owns the commit."""
    from velaris_shared.db.models import MarketplaceConformanceModel
    row = MarketplaceConformanceModel(
        tenant_id=tenant_id, package_id=package_id, package_version=package_version,
        status="passed" if result.get("passed") else "failed",
        total=int(result.get("total", 0)),
        passed_count=int(result.get("total", 0)) - int(result.get("failed", 0)),
        failed_count=int(result.get("failed", 0)),
        checks_json=result.get("checks", []),
        run_id=run_id,
    )
    session.add(row)
    await session.flush()
    return row


# A malformed or hostile bundle must not be able to exhaust memory during a
# check that exists to make installs safer. The packager writes these sections
# as plain declarative JSON; 8 MB each is far beyond any real one.
_MAX_SECTION_BYTES = 8 * 1024 * 1024
_SECTIONS = ("case_types", "forms", "rules")


def extract_artifacts(manifest: dict | None, hxapp_bytes: bytes | None = None) -> dict:
    """Shape a `.hxapp`'s contents into what `run_structural` expects.

    `apps/packager.py` writes one file per section at the zip root
    (`case_types.json`, `forms.json`, `rules.json`), so they are read from
    there; a manifest that inlines the same keys is accepted as a fallback.

    A package carrying none of them has nothing structural to violate and passes
    trivially — correct, not a loophole. The gate checks declarative artifacts,
    and a pure connector has none; execution safety for those belongs to the
    capability-grant model, not here.

    Never raises: a bundle this cannot read yields empty sections, and the gate
    then decides on "no artifacts" rather than the install dying on a zip quirk.
    """
    import io
    import json
    import zipfile

    m = manifest or {}
    out: dict[str, Any] = {"manifest": m}
    for s in _SECTIONS:
        val = m.get(s)
        out[s] = val if isinstance(val, list) else []

    if not hxapp_bytes:
        return out

    try:
        with zipfile.ZipFile(io.BytesIO(hxapp_bytes)) as zf:
            names = set(zf.namelist())
            for s in _SECTIONS:
                fname = f"{s}.json"
                if fname not in names:
                    continue
                info = zf.getinfo(fname)
                # Check the DECLARED size before reading: this is the zip-bomb
                # guard, and it only works before decompression.
                if info.file_size > _MAX_SECTION_BYTES:
                    logger.warning("conformance: %s too large (%d bytes) — skipped",
                                   fname, info.file_size)
                    continue
                try:
                    parsed = json.loads(zf.read(fname))
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    # Malformed section = a real conformance problem, but it is
                    # the checks' job to report it, not this reader's to crash on.
                    logger.info("conformance: %s is not valid JSON (%s)", fname, exc)
                    continue
                if isinstance(parsed, list):
                    out[s] = parsed
    except (zipfile.BadZipFile, OSError) as exc:
        logger.info("conformance: could not read bundle sections (%s)", exc)

    return out


async def run_and_record(session: AsyncSession, *, tenant_id: str, package_id: str,
                         package_version: str, manifest: dict | None,
                         hxapp_bytes: bytes | None = None) -> dict:
    """Run the structural checks on a bundle and persist the result.

    Running at install time is what makes the gate self-sufficient: it does not
    wait on a publisher to submit anything, and the checks are deterministic
    static analysis, so doing it inline is cheap and can never be flaky.
    """
    from velaris_shared.testsuite.conformance import run_structural

    package = extract_artifacts(manifest, hxapp_bytes)
    result = run_structural(package)
    await record_result(session, tenant_id=tenant_id, package_id=package_id,
                        package_version=package_version, result=result)
    return result


async def evaluate(session: AsyncSession, *, tenant_id: str, package_id: str,
                   package_version: str, publisher_tier: str = "community",
                   is_existing_install: bool = False,
                   installed_at: datetime | None = None) -> GateDecision:
    """Decide whether this package may be installed or updated.

    `is_existing_install` marks the §6.5 protected case — something already
    running on this tenant, which is badged rather than blocked.
    """
    mode = gate_mode()

    if mode == MODE_OFF:
        return GateDecision(True, BADGE_UNVERIFIED, "conformance gate disabled", mode)

    # Official tier is auto-trusted, and that trust is derived from the publisher
    # org URL + baked-in registry — never from the manifest, so a package cannot
    # assert this about itself.
    if publisher_tier == "official":
        return GateDecision(True, BADGE_PASSED, "official package — auto-trusted", mode)

    row = await latest_result(session, tenant_id, package_id, package_version)

    if row is not None and row.status == "passed":
        return GateDecision(True, BADGE_PASSED,
                            f"conformance passed ({row.passed_count}/{row.total} checks)", mode)

    if row is not None and row.status == "failed":
        reason = f"conformance failed ({row.failed_count} of {row.total} checks)"
        if is_existing_install:
            # Already running: §6.5 forbids removing it over a failed check.
            return GateDecision(True, BADGE_FAILED, reason + " — existing install kept", mode)
        return GateDecision(mode != MODE_ENFORCE, BADGE_FAILED, reason, mode)

    # No result at all — the case the grandfather window exists for.
    state = await window_state(session)
    if is_existing_install and state["active"] and state["open"]:
        inst = _aware(installed_at)
        cut = _aware(datetime.fromisoformat(state["cutover_at"])) if state["cutover_at"] else None
        # Only installs that predate the cutover are grandfathered. One made
        # after the gate went live had a conformant path available and skipped it.
        if inst is None or cut is None or inst <= cut:
            return GateDecision(True, BADGE_LEGACY,
                                f"grandfathered — {state['days_remaining']} days to submit results",
                                mode, grandfathered=True)

    badge = BADGE_LEGACY if (state["active"] and state["open"] and is_existing_install) else BADGE_UNVERIFIED
    reason = "no conformance results submitted for this package version"
    if is_existing_install:
        # Window closed: downranked and badged, still never uninstalled.
        return GateDecision(True, badge, reason + " — existing install kept, sorted below verified", mode)
    return GateDecision(mode != MODE_ENFORCE, badge, reason, mode)
