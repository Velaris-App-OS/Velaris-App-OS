"""HELIX Temporal — durable SLA timer activity tests.

Exercises ``sla_timer_activities`` directly, with no Temporal runtime. The
activities open their own DB session via ``get_session_factory()``; we point
that global at the conftest in-memory engine so seeded rows are visible (and
writes do not leak to the dev database).

Covers the verify-before-fire contract: goal -> at_risk, deadline -> breached,
due escalation levels, idempotency, and the paused / cancelled no-ops.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import case_service.db.session as db_session
from case_service.db.models import CaseAuditLogModel, CaseSLAInstanceModel
from case_service.temporal.activities import sla_timer_activities as acts


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest_asyncio.fixture
async def patch_factory(session, monkeypatch):
    """Route get_session_factory() to the same engine the test seeds on."""
    factory = async_sessionmaker(
        session.bind, class_=AsyncSession, expire_on_commit=False
    )
    monkeypatch.setattr(db_session, "_session_factory", factory)
    return factory


async def _seed(session, **overrides) -> CaseSLAInstanceModel:
    now = _now()
    row = CaseSLAInstanceModel(
        case_id=overrides.pop("case_id", uuid.uuid4()),
        sla_policy_id=overrides.pop("sla_policy_id", "sla-default"),
        target_id=overrides.pop("target_id", "stage-1"),
        status=overrides.pop("status", "on_track"),
        started_at=overrides.pop("started_at", now - timedelta(hours=1)),
        goal_at=overrides.pop("goal_at", now + timedelta(hours=1)),
        deadline_at=overrides.pop("deadline_at", now + timedelta(hours=2)),
        **overrides,
    )
    session.add(row)
    await session.commit()
    return row


async def _audit_actions(session, case_id) -> list[str]:
    rows = (
        await session.execute(
            select(CaseAuditLogModel).where(
                CaseAuditLogModel.case_id == case_id
            )
        )
    ).scalars().all()
    return [r.action for r in rows]


# ── fire_sla_event: status transitions ───────────────────────────────


@pytest.mark.asyncio
async def test_goal_passed_marks_at_risk(session, patch_factory):
    row = await _seed(session, goal_at=_now() - timedelta(minutes=1))

    result = await acts.fire_sla_event(str(row.id))

    assert result["status"] == "at_risk"
    assert result["events_fired"] == 1
    assert "sla_at_risk" in await _audit_actions(session, row.case_id)


@pytest.mark.asyncio
async def test_deadline_passed_marks_breached(session, patch_factory):
    row = await _seed(
        session,
        goal_at=_now() - timedelta(minutes=2),
        deadline_at=_now() - timedelta(minutes=1),
    )

    result = await acts.fire_sla_event(str(row.id))

    assert result["status"] == "breached"
    assert result["events_fired"] == 1
    assert "sla_breached" in await _audit_actions(session, row.case_id)


@pytest.mark.asyncio
async def test_due_escalation_level_fires(session, patch_factory):
    snapshot = {
        "schedule": [
            {
                "level": 1,
                "name": "L1",
                "fires_at": (_now() - timedelta(minutes=1)).isoformat(),
                "actions": [],  # no side effects — just record + audit
            }
        ]
    }
    row = await _seed(session, escalation_tree_snapshot=snapshot)

    result = await acts.fire_sla_event(str(row.id))

    assert result["events_fired"] == 1
    assert "sla_escalated" in await _audit_actions(session, row.case_id)


# ── fire_sla_event: idempotency + no-ops ──────────────────────────────


@pytest.mark.asyncio
async def test_fire_is_idempotent(session, patch_factory):
    row = await _seed(session, goal_at=_now() - timedelta(minutes=1))

    first = await acts.fire_sla_event(str(row.id))
    second = await acts.fire_sla_event(str(row.id))

    assert first["events_fired"] == 1
    assert second["events_fired"] == 0
    # exactly one at_risk audit row, not two
    actions = await _audit_actions(session, row.case_id)
    assert actions.count("sla_at_risk") == 1


@pytest.mark.asyncio
async def test_paused_sla_never_fires(session, patch_factory):
    row = await _seed(
        session,
        status="paused",
        goal_at=_now() - timedelta(minutes=5),
        deadline_at=_now() - timedelta(minutes=4),
    )

    result = await acts.fire_sla_event(str(row.id))

    assert result["status"] == "paused"
    assert result["events_fired"] == 0
    assert await _audit_actions(session, row.case_id) == []


@pytest.mark.asyncio
async def test_cancelled_sla_never_fires(session, patch_factory):
    row = await _seed(
        session,
        status="cancelled",
        deadline_at=_now() - timedelta(minutes=1),
    )

    result = await acts.fire_sla_event(str(row.id))

    assert result["events_fired"] == 0
    assert await _audit_actions(session, row.case_id) == []


@pytest.mark.asyncio
async def test_fire_missing_sla_is_safe(session, patch_factory):
    result = await acts.fire_sla_event(str(uuid.uuid4()))
    assert result["exists"] is False
    assert result["terminal"] is True


# ── get_sla_timer_state ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_state_on_track_reports_next_event(session, patch_factory):
    row = await _seed(session)  # goal +1h, deadline +2h

    state = await acts.get_sla_timer_state(str(row.id))

    assert state["status"] == "on_track"
    assert state["paused"] is False
    assert state["terminal"] is False
    # earliest pending event is the goal (at_risk) deadline
    assert state["next_event_at"] == row.goal_at.replace(
        tzinfo=timezone.utc
    ).isoformat()


@pytest.mark.asyncio
async def test_state_cancelled_is_terminal(session, patch_factory):
    row = await _seed(session, status="cancelled")
    state = await acts.get_sla_timer_state(str(row.id))
    assert state["terminal"] is True
    assert state["next_event_at"] is None


@pytest.mark.asyncio
async def test_state_paused_reports_paused(session, patch_factory):
    row = await _seed(session, status="paused")
    state = await acts.get_sla_timer_state(str(row.id))
    assert state["paused"] is True
    assert state["terminal"] is False


# ── list_case_sla_timers ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_returns_case_timers(session, patch_factory):
    case_id = uuid.uuid4()
    await _seed(session, case_id=case_id, sla_policy_id="a", target_id="s1")
    await _seed(session, case_id=case_id, sla_policy_id="b", target_id="s2")
    await _seed(session, case_id=uuid.uuid4(), sla_policy_id="c")  # other case

    timers = await acts.list_case_sla_timers(str(case_id))

    assert len(timers) == 2
    assert {t["status"] for t in timers} == {"on_track"}
    assert all("sla_id" in t for t in timers)
