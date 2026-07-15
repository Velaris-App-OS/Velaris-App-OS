"""HxEvolve — the detect → propose → prove pipeline, shared by the scan endpoint
and the P3 scheduled cron. Writes ONLY hxevolve_insights rows.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import (
    HxEvolveConfigModel,
    HxEvolveInsightModel,
    RuleDefinitionModel,
)
from case_service.hxevolve import detector, proposer, prover

logger = logging.getLogger(__name__)

MAX_SURFACED_PER_SCAN = 3      # a scan surfaces a handful of insights, not a firehose


def config_view(cfg: HxEvolveConfigModel | None) -> dict[str, Any]:
    if cfg is None:
        return {"min_improvement": prover.MIN_IMPROVEMENT,
                "max_auto_ratio_rise": prover.MAX_AUTO_RATIO_RISE,
                "min_coverage": prover.MIN_COVERAGE,
                "min_determinate": prover.MIN_DETERMINATE,
                "scan_frequency_hours": 24, "scan_enabled": False,
                "drift_check_every_n_changes": 3,
                "is_default": True}
    return {"min_improvement": cfg.min_improvement,
            "max_auto_ratio_rise": cfg.max_auto_ratio_rise,
            "min_coverage": cfg.min_coverage,
            "min_determinate": cfg.min_determinate,
            "scan_frequency_hours": cfg.scan_frequency_hours,
            "scan_enabled": cfg.scan_enabled,
            "drift_check_every_n_changes": cfg.drift_check_every_n_changes,
            "is_default": False}


async def get_config(session: AsyncSession, case_type_id) -> HxEvolveConfigModel | None:
    return await session.get(HxEvolveConfigModel, case_type_id)


async def last_scan_at(session: AsyncSession, case_type_id) -> datetime | None:
    """The newest insight timestamp = when this case type was last scanned."""
    row = (await session.execute(
        select(HxEvolveInsightModel.created_at)
        .where(HxEvolveInsightModel.case_type_id == case_type_id)
        .order_by(desc(HxEvolveInsightModel.created_at)).limit(1)
    )).scalar_one_or_none()
    if row is not None and row.tzinfo is None:
        row = row.replace(tzinfo=timezone.utc)
    return row


async def scan_due(session: AsyncSession, case_type_id,
                   frequency_hours: int) -> bool:
    last = await last_scan_at(session, case_type_id)
    if last is None:
        return True
    return datetime.now(timezone.utc) >= last + timedelta(hours=frequency_hours)


async def run_scan(session: AsyncSession, ct, *, tenant_id: str,
                   created_by: str | None, days: int = 30) -> dict[str, Any]:
    """One full loop for one case type. Every proposal is recorded; only
    replay-proven / plausibly-described ones end up ``surfaced``."""
    cfg = config_view(await get_config(session, ct.id))

    # cumulative-drift guardrail (§6): after N merged HxEvolve changes the
    # holistic baseline is re-checked; a cumulative regression FREEZES scans
    # (drift insight surfaced once) until an admin re-baselines.
    from case_service.hxevolve import drift
    drift_state = await drift.check_drift(session, ct, cfg,
                                          tenant_id=tenant_id, created_by=created_by)
    if drift_state["frozen"]:
        await session.commit()
        fresh = drift_state["insight"]
        if fresh is not None:
            await session.refresh(fresh)
        return {"candidates": 0, "recorded": 1 if fresh is not None else 0,
                "insights": [fresh] if fresh is not None else [],
                "frozen": True,
                "hint": f"Scans are frozen for this case type — cumulative drift: "
                        f"{drift_state['reason']}. Re-baseline to resume."}

    candidates = await detector.detect_candidates(session, ct.id, days=days)
    if not candidates:
        return {"insights": [], "candidates": 0, "recorded": 0,
                "hint": "No optimization candidates in the window — nothing slow, "
                        "drifting or dominant enough to act on."}

    rules = list((await session.execute(
        select(RuleDefinitionModel).where(
            RuleDefinitionModel.rule_type == "when",
            RuleDefinitionModel.scope_target_id == str(ct.id))
    )).scalars().all())

    insights: list[HxEvolveInsightModel] = []
    surfaced = 0
    for candidate in candidates:
        if surfaced >= MAX_SURFACED_PER_SCAN:
            break
        prop = await proposer.propose(session, candidate, ct, rules)

        insight = HxEvolveInsightModel(
            tenant_id=tenant_id, case_type_id=ct.id, signal=candidate,
            proposal=prop["proposal"], proposal_kind=prop["kind"],
            rationale=prop["rationale"], created_by=created_by,
        )
        if prop["errors"]:
            # gate-rejected: provenance only, never surfaced (§3.3)
            insight.status = "discarded_gate"
            insight.evidence = {"gate_errors": prop["errors"]}
        else:
            proof = await prover.prove(
                session, kind=prop["kind"], proposal=prop["proposal"],
                case_type_id=ct.id, tenant_id=tenant_id,
                candidate_signal=candidate, created_by=created_by, config=cfg)
            insight.status = proof["verdict"]
            insight.evidence_kind = proof["evidence_kind"]
            insight.evidence = {**(proof["evidence"] or {}),
                                "policy_alternative": prop["policy_alternative"]}
            insight.replay_run_id = proof.get("replay_run_id")
            if insight.status == "surfaced":
                surfaced += 1
        insights.append(insight)
        session.add(insight)

    await session.commit()
    for i in insights:
        await session.refresh(i)
    return {"candidates": len(candidates), "recorded": len(insights),
            "insights": [i for i in insights if i.status == "surfaced"]}


async def hxevolve_cron() -> None:
    """Hourly scheduled scans for opted-in case types (P3). Insights only —
    the cron can never change config; it has no apply/stage path at all.

    Authorization posture: there is no request subject here, so the HxGuard
    `replay.run` check the interactive scan endpoint performs cannot apply.
    The standing authorization is the admin's explicit `scan_enabled` opt-in on
    the config row (set through the admin-gated PUT /config); the cron runs
    ONLY for those rows, and the per-tenant cohort concurrency cap still holds
    (enforced in the prover, shared with the endpoint path)."""
    import asyncio
    from case_service.db.session import get_session_factory
    from case_service.db import repository as repo

    await asyncio.sleep(20)     # let the DB come up
    while True:
        try:
            factory = get_session_factory()
            async with factory() as session:
                cfgs = (await session.execute(
                    select(HxEvolveConfigModel).where(
                        HxEvolveConfigModel.scan_enabled == True)  # noqa: E712
                )).scalars().all()
                for cfg in cfgs:
                    if not await scan_due(session, cfg.case_type_id,
                                          cfg.scan_frequency_hours):
                        continue
                    ct = await repo.get_case_type(session, cfg.case_type_id)
                    if ct is None:
                        continue
                    try:
                        result = await run_scan(
                            session, ct,
                            tenant_id=cfg.tenant_id or "default",
                            created_by="hxevolve-cron")
                        logger.info("hxevolve cron: %s → %d surfaced / %d recorded",
                                    ct.name, len(result["insights"]),
                                    result["recorded"])
                    except Exception:
                        await session.rollback()
                        logger.warning("hxevolve cron scan failed for %s "
                                       "(non-fatal)", cfg.case_type_id,
                                       exc_info=True)
        except Exception:
            logger.warning("hxevolve cron cycle failed (non-fatal)", exc_info=True)
        await asyncio.sleep(3600)
