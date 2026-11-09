"""F-04 (second slice) — the digital twin.

The most important test in this file is the isolation one: **a "twin" that
points at production's database is refused.** That failure would be both
catastrophic and silent — synthetic cases written into real data, and a
promotion cleared by a rehearsal that damaged the thing it rehearsed — and it is
checked on (host, port, database), never on the connection string, because two
DSNs differing only by password are the same database.

After that the tests are about the twin refusing to be satisfied cheaply:

* no twin configured is **refused**, and the simulation reads exactly as it did
  before the twin existed;
* a twin threshold that is set and could NOT be measured is a **breach**, not a
  pass — a broken twin must not quietly disable the gate an operator asked for;
* `synthetic_load` is true only when a twin actually executed something;
* a case that stalls counts against the failure limit, because a candidate that
  strands cases has failed at the only thing a rehearsal checks;
* a Temporal-orchestrated case type with no twin worker is **not run**, rather
  than driven inline under a different execution model.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio

from velaris_shared.db.models import (
    ArbiterNamespaceModel,
    DeploymentRunModel,
    TenantModel,
    TwinRunModel,
)

from case_service.hxdeploy import simulation as sim
from case_service.hxdeploy import twin

pytestmark = pytest.mark.asyncio

SEED = [("ai", "deny"), ("egress", "deny"), ("export", "deny"),
        ("vault", "deny"), ("case", "allow"), ("auth", "allow"),
        ("agent", "deny")]

PROD = "postgresql://velaris:secret@localhost:5432/velaris"


@pytest_asyncio.fixture(autouse=True)
async def tenant(session):
    from case_service.agents import identity as agent_identity
    from case_service.tenancy import identity as _identity

    _identity.clear_cache()
    agent_identity._SLUG_CACHE.clear()
    twin.reset_twin_engine()
    for ns, eff in SEED:
        session.add(ArbiterNamespaceModel(
            namespace=ns, default_effect=eff, max_mode="shadow", description="",
            sample_allow_rate=1, sample_default_rate=1,
            updated_at=datetime.now(timezone.utc)))
    t = TenantModel(id=uuid.uuid4(), slug="default", name="Default")
    session.add(t)
    await session.commit()
    yield t
    twin.reset_twin_engine()
    agent_identity._SLUG_CACHE.clear()
    _identity.clear_cache()


def _settings(**over):
    base = {"database_url": PROD, "twin_database_url": "", "twin_task_queue": "",
            "temporal_task_queue": "velaris-case-queue",
            "twin_max_cases": 200, "twin_deadline_seconds": 300}
    base.update(over)
    return SimpleNamespace(**base)


async def _run(session, *, checksum="abc123"):
    run = DeploymentRunModel(
        id=uuid.uuid4(), tenant_id="default", to_env_id=None,
        package_checksum=checksum, risk_level="low", risk_summary={},
        status="awaiting_approval", initiated_by="alice")
    session.add(run)
    await session.commit()
    return run


def _summary(total=100, divergent=2):
    return {"total": total, "determinate": total, "diverged": divergent}


def _twin_row(**over):
    base = {"id": uuid.uuid4(), "status": "complete", "refusal_reason": None,
            "error": None, "isolation_verified": True, "load_profile": "batch",
            "requested_cases": 10, "started_cases": 10, "terminal_cases": 10,
            "stuck_cases": 0, "failed_cases": 0, "p50_ms": 12, "p95_ms": 30,
            "max_ms": 44, "torn_down_rows": 10, "teardown_error": None}
    base.update(over)
    return SimpleNamespace(**base)


# ══ isolation — the one that matters ════════════════════════════════════════


def test_a_twin_pointing_at_production_is_refused():
    """The catastrophic-and-silent failure. Same database, different password."""
    ok, detail = twin.isolation(_settings(
        twin_database_url="postgresql://other:DIFFERENT@localhost:5432/velaris"))
    assert ok is False
    assert "production" in detail["reason"]
    assert detail["database"]["same"] is True


def test_a_twin_at_the_pooler_address_is_production():
    """R5d 6a: production is also reachable through PgBouncer on 6432."""
    ok, detail = twin.isolation(_settings(
        database_pooler_url="postgresql+asyncpg://velaris:pw@localhost:6432/velaris",
        twin_database_url="postgresql://other:x@localhost:6432/velaris"))
    assert ok is False and detail["database"]["same"] is True


def test_a_twin_at_the_commerce_pool_address_is_production():
    """R5e step 7: `velaris_commerce` on the pooler is the same database again."""
    ok, detail = twin.isolation(_settings(
        database_pooler_url="postgresql+asyncpg://velaris:pw@localhost:6432/velaris",
        commerce_database_pooler_url="postgresql+asyncpg://velaris:pw@localhost:6432/velaris_commerce",
        twin_database_url="postgresql://other:x@localhost:6432/velaris_commerce"))
    assert ok is False and detail["database"]["same"] is True


def test_without_a_pooler_port_6432_is_just_another_database():
    ok, _ = twin.isolation(_settings(
        twin_database_url="postgresql://other:x@localhost:6432/velaris"))
    assert ok is True


def test_a_password_is_not_isolation():
    """Two DSNs differing only by credentials name the same database."""
    assert twin.fingerprint("postgresql://a:p1@h:5432/db") == \
           twin.fingerprint("postgresql://a:p2@h:5432/db")


def test_a_different_database_on_the_same_host_is_isolation():
    ok, detail = twin.isolation(_settings(
        twin_database_url="postgresql://velaris:secret@localhost:5432/velaris_twin"))
    assert ok is True and detail["database"]["same"] is False


def test_a_twin_queue_production_workers_poll_is_refused():
    ok, detail = twin.isolation(_settings(
        twin_database_url="postgresql://velaris:secret@localhost:5432/velaris_twin",
        twin_task_queue="helix-case-service"))
    assert ok is False
    assert "production" in detail["reason"]


def test_an_unparseable_twin_url_is_not_quietly_accepted():
    ok, detail = twin.isolation(_settings(twin_database_url="::::"))
    assert ok is False and "cannot be parsed" in detail["reason"]


def test_no_twin_queue_is_allowed_and_says_what_it_costs():
    ok, detail = twin.isolation(_settings(
        twin_database_url="postgresql://velaris:secret@localhost:5432/velaris_twin"))
    assert ok is True
    assert "Temporal-orchestrated" in detail["reason"]


# ══ the upgrade ═════════════════════════════════════════════════════════════


async def test_with_no_twin_configured_the_run_is_refused_not_failed(session, tenant):
    """The shipped state. Refused is not failed: nothing is wrong with the
    candidate, there is simply nowhere to rehearse it."""
    run = await _run(session)
    row = await twin.run(session, tenant_uuid=tenant.id, bundle={"case_types": [{}]},
                         run_row=run)
    assert row.status == "refused"
    assert row.refusal_reason == twin.REFUSE_NO_TWIN


async def test_a_simulation_without_a_twin_reads_exactly_as_before(session, tenant):
    run = await _run(session)
    s = await sim.simulate(session, run=run, tenant_uuid=tenant.id,
                           tenant_slug="default", replay_summary=_summary())
    assert s.status == "passed"
    assert s.findings["synthetic_load"] is False
    assert s.findings["twin"] is None
    assert "no synthetic load was generated" in s.findings["coverage"]


async def test_twin_thresholds_are_null_by_default(session, tenant):
    gate = await sim.upsert_gate(session, tenant_id=tenant.id, enabled=True)
    await session.commit()
    assert gate.min_twin_cases is None
    assert gate.max_twin_failure_pct is None
    assert gate.max_twin_p95_ms is None


# ══ judging the twin half ═══════════════════════════════════════════════════


async def test_a_required_rehearsal_that_did_not_happen_is_a_breach(session, tenant):
    """Setting a twin threshold is an operator saying "do not promote this
    without rehearsing it". A broken twin must not satisfy that by silence."""
    await sim.upsert_gate(session, tenant_id=tenant.id, enabled=True,
                          min_twin_cases=5)
    await session.commit()
    run = await _run(session)
    refused = _twin_row(status="refused", refusal_reason=twin.REFUSE_NO_TWIN,
                        error="no twin configured", started_cases=0)
    s = await sim.simulate(session, run=run, tenant_uuid=tenant.id,
                           tenant_slug="default", replay_summary=_summary(),
                           twin_run=refused)
    assert s.status == "failed"
    assert any("requires a twin rehearsal" in b for b in s.findings["breaches"])
    assert s.findings["synthetic_load"] is False


async def test_an_unset_twin_threshold_does_not_demand_a_rehearsal(session, tenant):
    await sim.upsert_gate(session, tenant_id=tenant.id, enabled=True,
                          max_divergent_pct=50)
    await session.commit()
    run = await _run(session)
    s = await sim.simulate(session, run=run, tenant_uuid=tenant.id,
                           tenant_slug="default", replay_summary=_summary(),
                           twin_run=_twin_row(status="refused", started_cases=0))
    assert s.status == "passed"


async def test_a_clean_twin_run_passes_and_says_synthetic_load_ran(session, tenant):
    await sim.upsert_gate(session, tenant_id=tenant.id, enabled=True,
                          min_twin_cases=5, max_twin_failure_pct=10,
                          max_twin_p95_ms=1000)
    await session.commit()
    run = await _run(session)
    s = await sim.simulate(session, run=run, tenant_uuid=tenant.id,
                           tenant_slug="default", replay_summary=_summary(),
                           twin_run=_twin_row())
    assert s.status == "passed"
    assert s.findings["synthetic_load"] is True
    assert "isolated twin database" in s.findings["coverage"]
    assert s.findings["twin"]["terminal"] == 10


async def test_a_stalled_case_counts_against_the_failure_limit(session, tenant):
    """A candidate that strands cases has failed at the only thing a rehearsal
    is asked to check, and `terminal`/`failed` alone would hide it."""
    await sim.upsert_gate(session, tenant_id=tenant.id, enabled=True,
                          max_twin_failure_pct=10)
    await session.commit()
    run = await _run(session)
    s = await sim.simulate(
        session, run=run, tenant_uuid=tenant.id, tenant_slug="default",
        replay_summary=_summary(),
        twin_run=_twin_row(started_cases=10, terminal_cases=7, stuck_cases=3,
                           failed_cases=0))
    assert s.status == "failed"
    assert any("stalled" in b for b in s.findings["breaches"])


async def test_a_slow_candidate_breaches_the_p95_limit(session, tenant):
    await sim.upsert_gate(session, tenant_id=tenant.id, enabled=True,
                          max_twin_p95_ms=20)
    await session.commit()
    run = await _run(session)
    s = await sim.simulate(session, run=run, tenant_uuid=tenant.id,
                           tenant_slug="default", replay_summary=_summary(),
                           twin_run=_twin_row(p95_ms=90))
    assert s.status == "failed"
    assert any("p95" in b for b in s.findings["breaches"])


async def test_synthetic_load_is_false_when_the_twin_started_nothing(session, tenant):
    """`complete` with zero cases is not synthetic load, and calling it that
    would make the flag worthless."""
    run = await _run(session)
    s = await sim.simulate(session, run=run, tenant_uuid=tenant.id,
                           tenant_slug="default", replay_summary=_summary(),
                           twin_run=_twin_row(status="complete", started_cases=0))
    assert s.findings["synthetic_load"] is False


async def test_the_simulation_points_at_the_twin_run(session, tenant):
    run = await _run(session)
    row = _twin_row()
    s = await sim.simulate(session, run=run, tenant_uuid=tenant.id,
                           tenant_slug="default", replay_summary=_summary(),
                           twin_run=row)
    assert s.twin_run_id == row.id


async def test_a_package_with_no_case_types_is_refused_not_failed(session, tenant, monkeypatch):
    monkeypatch.setattr(twin, "configured", lambda: True)
    monkeypatch.setattr(twin, "isolation", lambda *_a, **_k: (True, {"reason": "ok"}))
    monkeypatch.setattr(twin, "fingerprint", lambda *_a, **_k: "f" * 64)
    run = await _run(session)
    row = await twin.run(session, tenant_uuid=tenant.id, bundle={"case_types": []},
                         run_row=run)
    assert row.status == "refused"
    assert row.refusal_reason == twin.REFUSE_NOTHING_TO_RUN
    assert "not a fault in the package" in row.error


async def test_a_non_isolated_twin_never_reaches_a_session(session, tenant, monkeypatch):
    """The refusal happens before any connection is made. A twin that is
    production must not be opened even to be told it is production."""
    monkeypatch.setattr(twin, "configured", lambda: True)

    def _boom():
        raise AssertionError("the twin session must never be opened here")

    monkeypatch.setattr(twin, "twin_sessions", _boom)
    monkeypatch.setattr(twin, "isolation",
                        lambda *_a, **_k: (False, {"reason": "it is production"}))
    monkeypatch.setattr(twin, "fingerprint", lambda *_a, **_k: "f" * 64)
    run = await _run(session)
    row = await twin.run(session, tenant_uuid=tenant.id,
                         bundle={"case_types": [{"name": "X"}]}, run_row=run)
    assert row.status == "refused"
    assert row.refusal_reason == twin.REFUSE_NOT_ISOLATED
    assert row.isolation_verified is False


# ══ the twin's own machinery ════════════════════════════════════════════════


def test_required_steps_exclude_automated_ones():
    """Mirrors `_auto_advance_if_complete`. A twin that waited for automated
    steps would report every candidate as stalling."""
    stage = {"id": "s1", "steps": [
        {"id": "a", "required": True, "step_type": "manual"},
        {"id": "b", "required": True, "step_type": "automated"},
        {"id": "c", "required": False, "step_type": "manual"},
    ]}
    assert twin._required_steps(stage) == ["a"]


def test_stages_are_ordered_by_order_not_by_list_position():
    definition = {"stages": [{"id": "b", "order": 2}, {"id": "a", "order": 1}]}
    assert [s["id"] for s in twin._ordered_stages(definition)] == ["a", "b"]


def test_a_stage_with_nothing_a_person_can_do_says_so():
    """Found live. "It stalled" alone sends an author looking in the wrong
    place; the commonest cause is a step-type mistake, not a workflow bug."""
    stage = {"id": "review", "steps": [
        {"id": "a", "required": True, "step_type": "automated"}]}
    assert twin._required_steps(stage) == []


def test_percentiles_on_a_single_sample():
    assert twin._pct([7], 0.95) == 7
    assert twin._pct([], 0.5) is None


async def test_a_twin_run_row_survives_its_refusal(session, tenant):
    """A refusal is a record. Raising would unwind the row that holds it."""
    run = await _run(session)
    row = await twin.run(session, tenant_uuid=tenant.id, bundle={"case_types": [{}]},
                         run_row=run)
    await session.commit()
    assert await session.get(TwinRunModel, row.id) is not None
