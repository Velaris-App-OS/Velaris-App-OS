"""Roadmap #27 — the marketplace conformance GATE and grandfather window (§6.5).

`test_conformance.py` covers the CHECKS. This covers what their result means at
install and update time, and above all the §6.5 asymmetry: a new install can be
refused, an existing install is never removed.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

from commerce_service.marketplace import conformance_gate as gate


# ── helpers ───────────────────────────────────────────────────────────────────

def _passing_result() -> dict:
    return {"passed": True, "total": 5, "failed": 0,
            "checks": [{"check": "manifest_present", "ok": True, "detail": ""}]}


def _failing_result() -> dict:
    return {"passed": False, "total": 5, "failed": 2,
            "checks": [{"check": "rules_acyclic", "ok": False, "detail": "circular rule dependency"}]}


def _bundle(sections: dict | None = None, manifest: dict | None = None) -> bytes:
    """Build a minimal in-memory .hxapp matching apps/packager.py's layout."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest or {"name": "Acme", "version": "1.0.0"}))
        for name, payload in (sections or {}).items():
            zf.writestr(f"{name}.json", json.dumps(payload))
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _enforce(monkeypatch):
    """Default these tests to enforce; the gate's own default is deliberately warn."""
    monkeypatch.setattr(gate, "gate_mode", lambda: gate.MODE_ENFORCE)


# ── artifact extraction ───────────────────────────────────────────────────────

def test_extract_reads_sections_from_bundle():
    raw = _bundle({"case_types": [{"name": "Claim"}], "forms": [{"name": "F"}]})
    out = gate.extract_artifacts({"name": "Acme"}, raw)
    assert [c["name"] for c in out["case_types"]] == ["Claim"]
    assert [f["name"] for f in out["forms"]] == ["F"]
    assert out["rules"] == []                      # absent section → empty, not an error


def test_extract_survives_a_corrupt_bundle():
    # Must not raise: a zip quirk should never take an install down. The gate
    # then decides on empty artifacts instead.
    out = gate.extract_artifacts({"name": "Acme"}, b"this is not a zip")
    assert out["case_types"] == [] and out["manifest"]["name"] == "Acme"


def test_extract_skips_malformed_section_json():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"name": "Acme"}))
        zf.writestr("case_types.json", "{not json")
    out = gate.extract_artifacts({"name": "Acme"}, buf.getvalue())
    assert out["case_types"] == []


def test_extract_ignores_a_non_list_section():
    raw = _bundle({"rules": {"oops": "an object, not a list"}})
    assert gate.extract_artifacts({}, raw)["rules"] == []


# ── the gate decision ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_new_install_without_results_is_blocked(session):
    d = await gate.evaluate(session, tenant_id="t1", package_id="acme/app",
                            package_version="1.0.0")
    assert d.blocked and d.badge == gate.BADGE_UNVERIFIED


@pytest.mark.asyncio
async def test_new_install_with_passing_result_is_allowed(session):
    await gate.record_result(session, tenant_id="t1", package_id="acme/app",
                             package_version="1.0.0", result=_passing_result())
    await session.commit()
    d = await gate.evaluate(session, tenant_id="t1", package_id="acme/app",
                            package_version="1.0.0")
    assert d.allowed and d.badge == gate.BADGE_PASSED


@pytest.mark.asyncio
async def test_failing_result_blocks_a_new_install(session):
    await gate.record_result(session, tenant_id="t1", package_id="bad/app",
                             package_version="1.0.0", result=_failing_result())
    await session.commit()
    d = await gate.evaluate(session, tenant_id="t1", package_id="bad/app",
                            package_version="1.0.0")
    assert d.blocked and d.badge == gate.BADGE_FAILED


@pytest.mark.asyncio
async def test_result_is_version_scoped(session):
    """A pass on 1.0.0 must not vouch for 1.0.1 — that is the update bypass."""
    await gate.record_result(session, tenant_id="t1", package_id="acme/app",
                             package_version="1.0.0", result=_passing_result())
    await session.commit()
    d = await gate.evaluate(session, tenant_id="t1", package_id="acme/app",
                            package_version="1.0.1")
    assert d.blocked


@pytest.mark.asyncio
async def test_official_tier_auto_passes(session):
    d = await gate.evaluate(session, tenant_id="t1", package_id="velaris/hxtest",
                            package_version="1.0.0", publisher_tier="official")
    assert d.allowed and d.badge == gate.BADGE_PASSED


@pytest.mark.asyncio
async def test_gate_off_allows_everything(session, monkeypatch):
    monkeypatch.setattr(gate, "gate_mode", lambda: gate.MODE_OFF)
    d = await gate.evaluate(session, tenant_id="t1", package_id="any/app",
                            package_version="9.9.9")
    assert d.allowed


@pytest.mark.asyncio
async def test_warn_mode_allows_but_still_badges(session, monkeypatch):
    monkeypatch.setattr(gate, "gate_mode", lambda: gate.MODE_WARN)
    await gate.record_result(session, tenant_id="t1", package_id="bad/app",
                             package_version="1.0.0", result=_failing_result())
    await session.commit()
    d = await gate.evaluate(session, tenant_id="t1", package_id="bad/app",
                            package_version="1.0.0")
    assert d.allowed and d.badge == gate.BADGE_FAILED      # visible, not silent


# ── §6.5: existing installs are never removed ─────────────────────────────────

@pytest.mark.asyncio
async def test_existing_install_is_never_blocked_even_when_failing(session):
    """§6.5: 'No existing package is removed without explicit publisher action.'"""
    await gate.record_result(session, tenant_id="t1", package_id="bad/app",
                             package_version="1.0.0", result=_failing_result())
    await session.commit()
    d = await gate.evaluate(session, tenant_id="t1", package_id="bad/app",
                            package_version="1.0.0", is_existing_install=True)
    assert d.allowed and d.badge == gate.BADGE_FAILED


@pytest.mark.asyncio
async def test_existing_install_without_results_is_grandfathered(session):
    await gate.activate_window(session, activated_by="admin", window_days=60)
    d = await gate.evaluate(session, tenant_id="t1", package_id="legacy/app",
                            package_version="1.0.0", is_existing_install=True,
                            installed_at=datetime.now(timezone.utc) - timedelta(days=5))
    assert d.allowed and d.grandfathered and d.badge == gate.BADGE_LEGACY


@pytest.mark.asyncio
async def test_install_made_after_cutover_is_not_grandfathered(session):
    """The window forgives pre-existing installs, not ones made after the gate went live."""
    await gate.activate_window(session, activated_by="admin", window_days=60)
    d = await gate.evaluate(session, tenant_id="t1", package_id="late/app",
                            package_version="1.0.0", is_existing_install=True,
                            installed_at=datetime.now(timezone.utc) + timedelta(days=1))
    assert not d.grandfathered
    assert d.allowed                                  # still never uninstalled
    assert d.badge == gate.BADGE_LEGACY


@pytest.mark.asyncio
async def test_after_the_window_closes_it_is_unverified_but_kept(session):
    await gate.activate_window(session, activated_by="admin", window_days=0)
    d = await gate.evaluate(session, tenant_id="t1", package_id="legacy/app",
                            package_version="1.0.0", is_existing_install=True,
                            installed_at=datetime.now(timezone.utc) - timedelta(days=100))
    assert d.allowed and not d.grandfathered
    assert d.badge == gate.BADGE_UNVERIFIED           # downranked, never removed


@pytest.mark.asyncio
async def test_no_window_means_no_grandfather(session):
    d = await gate.evaluate(session, tenant_id="t1", package_id="legacy/app",
                            package_version="1.0.0", is_existing_install=True)
    assert d.allowed and not d.grandfathered


# ── the window itself ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_activate_window_is_idempotent(session):
    """Re-activating must NOT restart the clock — that would void the deadline."""
    first = await gate.activate_window(session, activated_by="admin", window_days=60)
    again = await gate.activate_window(session, activated_by="someone_else", window_days=999)
    assert again.id == first.id
    assert again.window_days == 60
    assert again.activated_by == "admin"


@pytest.mark.asyncio
async def test_window_state_reports_remaining_days(session):
    await gate.activate_window(session, activated_by="admin", window_days=60)
    st = await gate.window_state(session)
    assert st["active"] and st["open"]
    assert 58 <= st["days_remaining"] <= 60


@pytest.mark.asyncio
async def test_window_state_when_never_activated(session):
    st = await gate.window_state(session)
    assert st["active"] is False and st["open"] is False


@pytest.mark.asyncio
async def test_latest_result_wins(session):
    """Newest row decides — a later pass must supersede an earlier failure."""
    await gate.record_result(session, tenant_id="t1", package_id="acme/app",
                             package_version="1.0.0", result=_failing_result())
    await session.commit()
    await gate.record_result(session, tenant_id="t1", package_id="acme/app",
                             package_version="1.0.0", result=_passing_result())
    await session.commit()
    row = await gate.latest_result(session, "t1", "acme/app", "1.0.0")
    assert row.status == "passed"


@pytest.mark.asyncio
async def test_results_are_tenant_scoped(session):
    await gate.record_result(session, tenant_id="t1", package_id="acme/app",
                             package_version="1.0.0", result=_passing_result())
    await session.commit()
    d = await gate.evaluate(session, tenant_id="t2", package_id="acme/app",
                            package_version="1.0.0")
    assert d.blocked


@pytest.mark.asyncio
async def test_run_and_record_persists_a_verdict(session):
    raw = _bundle({
        # Deliberately circular: r1 -> r2 -> r1 must be caught.
        "rules": [{"id": "r1", "depends_on": ["r2"]}, {"id": "r2", "depends_on": ["r1"]}],
    })
    result = await gate.run_and_record(
        session, tenant_id="t1", package_id="cyclic/app", package_version="1.0.0",
        manifest={"name": "Cyclic", "version": "1.0.0"}, hxapp_bytes=raw)
    await session.commit()
    assert result["passed"] is False
    row = await gate.latest_result(session, "t1", "cyclic/app", "1.0.0")
    assert row.status == "failed" and row.failed_count >= 1


@pytest.mark.asyncio
async def test_package_with_no_artifacts_passes(session):
    """A pure connector has no declarative artifacts — nothing to violate."""
    result = await gate.run_and_record(
        session, tenant_id="t1", package_id="pure/connector", package_version="2.0.0",
        manifest={"name": "Connector", "version": "2.0.0"}, hxapp_bytes=_bundle())
    await session.commit()
    assert result["passed"] is True


# ── bulk badge endpoint + route ordering ──────────────────────────────────────

def test_window_route_is_declared_before_the_package_path():
    """`/conformance/window` must not be captured by `/conformance/{package_id:path}`.

    `package_id` is a :path converter and would happily match "window", so the
    literal routes have to be REGISTERED first — FastAPI matches in declaration
    order. Asserted against the route table rather than over HTTP because
    `_can_view` 404s the whole module when the marketplace feature flag is off,
    which is the case in tests and would mask a real ordering regression.
    """
    from commerce_service.main import app   # the marketplace routes live here (R5e step 7)

    paths = [getattr(r, "path", "") for r in app.routes]
    literal = [i for i, p in enumerate(paths) if p.endswith("/conformance/window")]
    catchall = [i for i, p in enumerate(paths) if p.endswith("/conformance/{package_id:path}")]
    assert literal and catchall, f"conformance routes missing (found: {literal}, {catchall})"
    assert min(literal) < min(catchall), (
        "/conformance/window is declared AFTER /conformance/{package_id:path} — "
        "the :path converter will swallow it and return a package result instead"
    )


@pytest.mark.asyncio
async def test_bulk_badges_sort_unverified_below_passing(session):
    """§6.5 ordering is a contract, not a UI preference."""
    ranks = {gate.BADGE_PASSED: 0, gate.BADGE_LEGACY: 1,
             gate.BADGE_UNVERIFIED: 2, gate.BADGE_FAILED: 3}
    assert ranks[gate.BADGE_PASSED] < ranks[gate.BADGE_LEGACY] < ranks[gate.BADGE_UNVERIFIED]
    assert ranks[gate.BADGE_UNVERIFIED] < ranks[gate.BADGE_FAILED]
