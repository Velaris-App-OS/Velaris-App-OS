"""HxEvolve Phase C — cumulative-drift guardrail (§6).

Pins: baseline pinned lazily on first scan; drift check only fires after N
merged HxEvolve changes; a cumulative regression FREEZES scans and surfaces
one drift insight; frozen case types don't propose; only the admin
rebaseline endpoint unfreezes; too-little-data on either side = no freeze.

Copyright (c) 2024-2026 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from case_service.db.models import (
    ArtifactBranchModel,
    CaseInstanceModel,
    CaseTypeModel,
    HxEvolveBaselineModel,
    HxEvolveInsightModel,
)
from case_service.hxevolve import drift

from tests.phase_hxevolve.test_pB_api import _mk_ct

TENANT = "default"


async def _mk_merged_changes(session, ct, n: int) -> None:
    """n insights whose branches are MERGED — the drift-counter input."""
    for _ in range(n):
        b = ArtifactBranchModel(name=f"evolve-{uuid.uuid4().hex[:6]}",
                                branch_type="artifact", artifact_type="case_type",
                                artifact_id=str(ct.id), status="merged")
        session.add(b)
        await session.flush()
        session.add(HxEvolveInsightModel(
            tenant_id=TENANT, case_type_id=ct.id, signal={"kind": "bottleneck"},
            proposal={}, proposal_kind="sla_duration", status="staged",
            branch_id=b.id))
    await session.commit()


async def _mk_resolved_cases(session, ct, n: int, hours: float) -> None:
    """n resolved cases with a fixed cycle time inside the 30-day window."""
    now = datetime.now(timezone.utc)
    for _ in range(n):
        session.add(CaseInstanceModel(
            case_type_id=ct.id, case_type_version=ct.version, status="resolved",
            priority="medium", data={},
            created_at=now - timedelta(hours=hours + 1),
            resolved_at=now - timedelta(hours=1),
        ))
    await session.commit()


class TestBaseline:
    async def test_baseline_pinned_lazily(self, session):
        ct = await _mk_ct(session)
        state = await drift.check_drift(session, ct, {"drift_check_every_n_changes": 3},
                                        tenant_id=TENANT, created_by="t")
        await session.commit()
        assert state["frozen"] is False
        row = await session.get(HxEvolveBaselineModel, ct.id)
        assert row is not None and row.frozen is False
        assert "avg_duration_hours" in (row.metrics or {})

    async def test_no_check_before_n_merges(self, session):
        ct = await _mk_ct(session)
        await drift.ensure_baseline(session, ct.id, TENANT, "t")
        await _mk_merged_changes(session, ct, 2)          # N=3 → below threshold
        state = await drift.check_drift(session, ct, {"drift_check_every_n_changes": 3},
                                        tenant_id=TENANT, created_by="t")
        assert state["frozen"] is False
        row = await session.get(HxEvolveBaselineModel, ct.id)
        assert row.checked_through == 0                    # check never ran


class TestDriftFreeze:
    async def _drifted_ct(self, session):
        """Baseline = 5 fast cases; current = those + regression is simulated by
        editing the stored baseline to a much faster value."""
        ct = await _mk_ct(session)
        await _mk_resolved_cases(session, ct, 6, hours=20.0)
        base = await drift.ensure_baseline(session, ct.id, TENANT, "t")
        # simulate "things were much faster at baseline time"
        base.metrics = {**base.metrics, "avg_duration_hours": 2.0, "cases": 6}
        await _mk_merged_changes(session, ct, 3)
        await session.commit()
        return ct

    async def test_cumulative_regression_freezes_and_surfaces(self, session):
        ct = await self._drifted_ct(session)
        state = await drift.check_drift(session, ct,
                                        {"drift_check_every_n_changes": 3,
                                         "min_improvement": 0.10},
                                        tenant_id=TENANT, created_by="t")
        await session.commit()
        assert state["frozen"] is True and "cycle time" in state["reason"]
        ins = state["insight"]
        assert ins is not None and ins.proposal_kind == "drift"
        assert ins.status == "surfaced" and ins.evidence_kind == "descriptive"
        assert ins.evidence["problems"]

    async def test_frozen_scan_skips_proposing(self, session):
        from case_service.hxevolve import pipeline
        ct = await self._drifted_ct(session)
        first = await pipeline.run_scan(session, ct, tenant_id=TENANT, created_by="t")
        assert first.get("frozen") is True and len(first["insights"]) == 1
        # second scan: still frozen, but the drift insight is NOT re-surfaced
        second = await pipeline.run_scan(session, ct, tenant_id=TENANT, created_by="t")
        assert second.get("frozen") is True and second["insights"] == []
        total = (await session.execute(
            select(HxEvolveInsightModel).where(
                HxEvolveInsightModel.case_type_id == ct.id,
                HxEvolveInsightModel.proposal_kind == "drift"))).scalars().all()
        assert len(total) == 1

    async def test_rebaseline_unfreezes(self, client, session):
        ct = await self._drifted_ct(session)
        state = await drift.check_drift(session, ct,
                                        {"drift_check_every_n_changes": 3,
                                         "min_improvement": 0.10},
                                        tenant_id=TENANT, created_by="t")
        await session.commit()
        assert state["frozen"] is True

        resp = await client.post(f"/api/v1/hxevolve/config/{ct.id}/rebaseline")
        assert resp.status_code == 200, resp.text
        out = resp.json()
        assert out["status"] == "rebaselined" and out["frozen"] is False

        after = await drift.check_drift(session, ct,
                                        {"drift_check_every_n_changes": 3},
                                        tenant_id=TENANT, created_by="t")
        assert after["frozen"] is False

    async def test_insufficient_data_never_freezes(self, session):
        ct = await _mk_ct(session)                         # zero resolved cases
        base = await drift.ensure_baseline(session, ct.id, TENANT, "t")
        base.metrics = {**base.metrics, "avg_duration_hours": 1.0, "cases": 0}
        await _mk_merged_changes(session, ct, 3)
        await session.commit()
        state = await drift.check_drift(session, ct,
                                        {"drift_check_every_n_changes": 3,
                                         "min_improvement": 0.10},
                                        tenant_id=TENANT, created_by="t")
        assert state["frozen"] is False                    # no evidence, no freeze


class TestConfigSurface:
    async def test_config_round_trips_drift_n(self, client, session):
        ct = await _mk_ct(session)
        resp = await client.put(f"/api/v1/hxevolve/config/{ct.id}", json={
            "min_improvement": 0.10, "max_auto_ratio_rise": 0.15,
            "min_coverage": 0.7, "min_determinate": 50,
            "scan_frequency_hours": 24, "scan_enabled": False,
            "drift_check_every_n_changes": 5,
        })
        assert resp.status_code == 200
        assert resp.json()["drift_check_every_n_changes"] == 5
        got = await client.get(f"/api/v1/hxevolve/config/{ct.id}")
        assert got.json()["drift_check_every_n_changes"] == 5

    async def test_baseline_view_endpoint(self, client, session):
        ct = await _mk_ct(session)
        empty = await client.get(f"/api/v1/hxevolve/config/{ct.id}/baseline")
        assert empty.json() == {"exists": False}
        await drift.ensure_baseline(session, ct.id, TENANT, "t")
        await session.commit()
        got = await client.get(f"/api/v1/hxevolve/config/{ct.id}/baseline")
        assert got.json()["exists"] is True and got.json()["frozen"] is False

    async def test_rebaseline_requires_admin(self, client, session):
        from tests.phase_hxdraft.test_pD_p2_api import _viewer_headers
        ct = await _mk_ct(session)
        resp = await client.post(f"/api/v1/hxevolve/config/{ct.id}/rebaseline",
                                 headers=_viewer_headers())
        assert resp.status_code == 403
