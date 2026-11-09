"""HxEvolve API — self-optimizing processes, human-gated (P1/P2).

POST /scan runs the full loop for one case type: DETECT (mining consumer) →
PROPOSE (AI on rails, re-gated) → PROVE (HxReplay vetoes / labelled descriptive
evidence) → store insights. Discarded proposals are stored for provenance but
listed only on request. P2's /stage opens the human-approved HxBranch path —
HxEvolve itself NEVER writes production config.

Design: docs/Future/hxevolve-self-optimizing.md (signed off 2026-07-05).

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared import hxguard
from velaris_shared.tenancy.identity import caller_tenant_uuid
from velaris_shared.auth.dependencies import get_current_user
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.db import repository as repo
from velaris_shared.db.models import HxEvolveInsightModel, RuleDefinitionModel
from velaris_shared.db.session import get_session
from case_service.hxevolve import pipeline, proposer
from velaris_shared.db.read_routing import get_read_session

log = logging.getLogger(__name__)

router = APIRouter(prefix="/hxevolve", tags=["hxevolve"])


async def _require_admin(session: AsyncSession, user: AuthenticatedUser) -> None:
    """Phase 4 step 2: privilege-decided module gate.

    The old `has_privilege("*", "*")` clause is not carried over — that legacy
    access-group wildcard is case-level vocabulary and has never been a platform
    grant (privileges.py::_crosses_into_platform_namespace). `superadmin` still
    passes, inside the evaluator.
    """
    await hxguard.require_user(session, user, "design.evolve.access")


def _tenant(user: AuthenticatedUser) -> str:
    return user.tenant_id or "default"


def _view(i: HxEvolveInsightModel, *, full: bool = False) -> dict:
    out = {
        "id": str(i.id), "case_type_id": str(i.case_type_id),
        "proposal_kind": i.proposal_kind, "status": i.status,
        "evidence_kind": i.evidence_kind,
        "rationale": i.rationale,
        "signal": i.signal,
        "replay_run_id": str(i.replay_run_id) if i.replay_run_id else None,
        "branch_id": str(i.branch_id) if i.branch_id else None,
        "created_at": i.created_at.isoformat() if i.created_at else None,
    }
    if full:
        out["proposal"] = i.proposal
        out["evidence"] = i.evidence
    return out


# ── the loop: scan one case type ────────────────────────────────────────────────

class ScanBody(BaseModel):
    case_type_id: str
    days: int = Field(default=30, ge=1, le=365)
    force: bool = False       # bypass the frequency cap (admin judgement call)


@router.post("/scan", status_code=201)
async def scan_case_type(
    body: ScanBody,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Detect → propose → prove for one case type. Every generated proposal is
    recorded; only replay-proven / plausibly-described ones are surfaced."""
    await _require_admin(session, user)
    try:
        ct_id = uuid.UUID(body.case_type_id)
    except ValueError:
        raise HTTPException(404, "Case type not found")
    ct = await repo.get_case_type(session, ct_id)
    if ct is None or (ct.tenant_id is not None and ct.tenant_id != _tenant(user)):
        raise HTTPException(404, "Case type not found")

    # a scan can run an HxReplay cohort (bulk read of many real cases) — require
    # the SAME HxGuard capability the manual cohort endpoint requires; the
    # concurrency cap is enforced in the prover (shared with the cron path)
    from velaris_shared.hxguard import service as hxguard
    await hxguard.require(session, hxguard.subject_from_user(user), "replay.cohorts.run",
                          resource={"cohort": {"case_type_id": str(ct_id),
                                               "via": "hxevolve"}})

    # anti-Goodhart frequency cap (§3.3) — configurable per case type in P3
    cfg = pipeline.config_view(await pipeline.get_config(session, ct_id))
    if not body.force and not await pipeline.scan_due(
            session, ct_id, cfg["scan_frequency_hours"]):
        raise HTTPException(429, f"This case type was scanned within the last "
                                 f"{cfg['scan_frequency_hours']}h — pass "
                                 f"force=true to override the frequency cap")

    try:
        result = await pipeline.run_scan(session, ct, tenant_id=_tenant(user),
                                         created_by=user.user_id, days=body.days)
    except proposer.ProposeError as exc:
        raise HTTPException(503, str(exc))
    return {**result, "insights": [_view(i, full=True)
                                   for i in result["insights"]]}


# ── P3: per-case-type objective/guardrail configuration ─────────────────────────

class ConfigBody(BaseModel):
    min_improvement: float = Field(default=0.10, ge=0.0, le=1.0)
    max_auto_ratio_rise: float = Field(default=0.15, ge=0.0, le=1.0)
    min_coverage: float = Field(default=0.7, ge=0.0, le=1.0)
    min_determinate: int = Field(default=50, ge=1, le=10000)
    scan_frequency_hours: int = Field(default=24, ge=1, le=24 * 30)
    scan_enabled: bool = False
    drift_check_every_n_changes: int = Field(default=3, ge=1, le=100)


@router.get("/config/{case_type_id}")
async def get_evolve_config(
    case_type_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    await _require_admin(session, user)
    try:
        ct_id = uuid.UUID(case_type_id)
    except ValueError:
        raise HTTPException(404, "Case type not found")
    return pipeline.config_view(await pipeline.get_config(session, ct_id))


@router.put("/config/{case_type_id}")
async def put_evolve_config(
    case_type_id: str,
    body: ConfigBody,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    await _require_admin(session, user)
    try:
        ct_id = uuid.UUID(case_type_id)
    except ValueError:
        raise HTTPException(404, "Case type not found")
    ct = await repo.get_case_type(session, ct_id)
    if ct is None or (ct.tenant_id is not None and ct.tenant_id != _tenant(user)):
        raise HTTPException(404, "Case type not found")

    from velaris_shared.db.models import HxEvolveConfigModel
    cfg = await session.get(HxEvolveConfigModel, ct_id)
    if cfg is None:
        cfg = HxEvolveConfigModel(case_type_id=ct_id, tenant_id=_tenant(user))
        session.add(cfg)
    for key in ("min_improvement", "max_auto_ratio_rise", "min_coverage",
                "min_determinate", "scan_frequency_hours", "scan_enabled",
                "drift_check_every_n_changes"):
        setattr(cfg, key, getattr(body, key))
    cfg.updated_by = user.user_id
    await session.commit()
    return pipeline.config_view(cfg)


# ── §6 cumulative-drift baseline (admin) ───────────────────────────────────────

@router.get("/config/{case_type_id}/baseline")
async def get_baseline(
    case_type_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    await _require_admin(session, user)
    from velaris_shared.db.models import HxEvolveBaselineModel
    from case_service.hxevolve import drift
    try:
        ct_id = uuid.UUID(case_type_id)
    except ValueError:
        raise HTTPException(404, "Case type not found")
    return drift.baseline_view(await session.get(HxEvolveBaselineModel, ct_id))


@router.post("/config/{case_type_id}/rebaseline")
async def rebaseline_case_type(
    case_type_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Human decision point: pin a fresh holistic baseline and unfreeze scans.
    The admin is asserting 'the current configuration is the new reference' —
    typically after reviewing (and possibly reverting) merged HxEvolve changes
    that a drift insight flagged."""
    await _require_admin(session, user)
    from case_service.hxevolve import drift
    try:
        ct_id = uuid.UUID(case_type_id)
    except ValueError:
        raise HTTPException(404, "Case type not found")
    ct = await repo.get_case_type(session, ct_id)
    if ct is None or (ct.tenant_id is not None and ct.tenant_id != _tenant(user)):
        raise HTTPException(404, "Case type not found")
    row = await drift.rebaseline(session, ct_id, _tenant(user), user.user_id)
    await session.commit()
    await session.refresh(row)
    return {"status": "rebaselined", **drift.baseline_view(row)}


# ── insights CRUD (read + dismiss) ──────────────────────────────────────────────

@router.get("/insights")
async def list_insights(
    case_type_id: Optional[str] = None,
    include_discarded: bool = False,
    session: AsyncSession = Depends(get_read_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    await _require_admin(session, user)
    stmt = select(HxEvolveInsightModel).where(
        HxEvolveInsightModel.tenant_id == _tenant(user))
    if case_type_id:
        try:
            stmt = stmt.where(
                HxEvolveInsightModel.case_type_id == uuid.UUID(case_type_id))
        except ValueError:
            raise HTTPException(404, "Case type not found")
    if not include_discarded:
        stmt = stmt.where(HxEvolveInsightModel.status.in_(
            ("surfaced", "staged", "dismissed")))
    rows = (await session.execute(
        stmt.order_by(desc(HxEvolveInsightModel.created_at)).limit(200)
    )).scalars().all()
    return {"insights": [_view(i) for i in rows]}


async def _get_insight(session: AsyncSession, user: AuthenticatedUser,
                       insight_id: str) -> HxEvolveInsightModel:
    try:
        iid = uuid.UUID(insight_id)
    except ValueError:
        raise HTTPException(404, "Insight not found")
    i = (await session.execute(
        select(HxEvolveInsightModel).where(
            HxEvolveInsightModel.id == iid,
            HxEvolveInsightModel.tenant_id == _tenant(user))
    )).scalar_one_or_none()
    if i is None:
        raise HTTPException(404, "Insight not found")
    return i


@router.get("/insights/{insight_id}")
async def get_insight(
    insight_id: str,
    session: AsyncSession = Depends(get_read_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    await _require_admin(session, user)
    return _view(await _get_insight(session, user, insight_id), full=True)


@router.post("/insights/{insight_id}/dismiss")
async def dismiss_insight(
    insight_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    await _require_admin(session, user)
    i = await _get_insight(session, user, insight_id)
    if i.status != "surfaced":
        raise HTTPException(400, f"Only surfaced insights can be dismissed "
                                 f"(current: '{i.status}')")
    i.status = "dismissed"
    await session.commit()
    return _view(i)


# ── P2: open a human-approved change PR (§3.4 — HxEvolve never applies) ──────────

def _evidence_description(i: HxEvolveInsightModel) -> str:
    ev = i.evidence or {}
    parts = [f"HxEvolve proposal ({i.proposal_kind}, evidence: {i.evidence_kind})."]
    if i.rationale:
        parts.append(f"Rationale: {i.rationale}")
    if i.evidence_kind == "counterfactual":
        b = (ev.get("baseline_cycle_time") or {}).get("mean")
        c = (ev.get("counterfactual_cycle_time") or {}).get("mean")
        if b and c is not None:
            parts.append(f"Replay evidence: mean cycle time {b}s -> {c}s over "
                         f"{ev.get('determinate')} determinate case(s), coverage "
                         f"{ev.get('coverage_ratio')}. Replay run {i.replay_run_id}.")
    else:
        parts.append("Descriptive mining evidence only — not a simulated proof "
                     "(see the insight for the statistics).")
    alt = ev.get("policy_alternative")
    if alt:
        parts.append(f"Policy alternative: {alt}")
    parts.append(f"Signal: {i.signal}")
    return " ".join(str(p) for p in parts)[:2000]


@router.post("/insights/{insight_id}/stage", status_code=201)
async def stage_insight(
    insight_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Open the change PR: the proposal is RE-VALIDATED against current state and
    staged through the existing HxBranch review path (SOD, approve = the audited
    merge). new rules land DISABLED behind the branch; everything else is a
    branch on the live artifact whose content_snapshot is the patched config.
    HxEvolve itself changes nothing."""
    await _require_admin(session, user)
    i = await _get_insight(session, user, insight_id)
    if i.status != "surfaced":
        raise HTTPException(400, f"Only surfaced insights can be staged "
                                 f"(current: '{i.status}')")
    ct = await repo.get_case_type(session, i.case_type_id)
    if ct is None:
        raise HTTPException(409, "The case type no longer exists — dismiss this "
                                 "insight")

    from case_service.api.routers.branches import _log_event
    from velaris_shared.db.models import ArtifactBranchModel
    from case_service.nlp import routing_builder, rule_builder, sla_builder
    from case_service.hxevolve import gates

    definition = ct.definition_json or {}
    proposal = i.proposal or {}
    description = _evidence_description(i)
    staged_rule_id = None

    if i.proposal_kind == "rule_add":
        errors = rule_builder.validate_rule_draft(proposal)
        if errors:
            raise HTTPException(409, "Proposal no longer valid: " + "; ".join(errors))
        # 8e: the rule is the deciding admin's tenant's (a default only for
        # the platform operator); the name is unique within that owner.
        from velaris_shared.tenancy import inherit
        try:
            owner = await inherit.owner_for_create(session, user)
        except inherit.InheritError as exc:
            raise HTTPException(exc.status, str(exc))
        existing = await inherit.name_taken(
            session, RuleDefinitionModel, owner,
            name=proposal["name"], version=proposal.get("version", "1.0.0"))
        if existing:
            raise HTTPException(409, f"A rule named '{proposal['name']}' already "
                                     f"exists — re-scan")
        rule = await repo.create_rule(session, data={
            "tenant_id": owner,
            "name": proposal["name"], "version": proposal.get("version", "1.0.0"),
            "rule_type": "when", "scope": proposal.get("scope", "case_type"),
            "scope_target_id": proposal.get("scope_target_id") or str(ct.id),
            "definition_json": {**proposal["definition_json"],
                                "description": f"Proposed by HxEvolve — {i.rationale or ''}"[:500]},
            "enabled": False, "priority": 0,   # disabled until the branch merges
        })
        await session.flush()
        staged_rule_id = rule.id
        snapshot = {"id": str(rule.id), "name": rule.name, "version": rule.version,
                    "rule_type": rule.rule_type,
                    "definition_json": rule.definition_json, "enabled": False}
        artifact_type, artifact_id = "rule", str(rule.id)
        base, content = snapshot, {**snapshot, "enabled": True}

    elif i.proposal_kind == "rule_adjust":
        errors = rule_builder.validate_rule_draft(proposal)
        if errors:
            raise HTTPException(409, "Proposal no longer valid: " + "; ".join(errors))
        rule = (await session.execute(
            select(RuleDefinitionModel).where(
                RuleDefinitionModel.id == uuid.UUID(str(proposal.get("id"))))
        )).scalar_one_or_none()
        if rule is None:
            raise HTTPException(409, "The target rule no longer exists — re-scan")
        base = {"id": str(rule.id), "name": rule.name, "version": rule.version,
                "rule_type": rule.rule_type,
                "definition_json": rule.definition_json, "enabled": rule.enabled}
        content = {**base,
                   "definition_json": {**proposal["definition_json"],
                                       "description": f"Adjusted by HxEvolve — {i.rationale or ''}"[:500]}}
        artifact_type, artifact_id = "rule", str(rule.id)

    elif i.proposal_kind in ("sla_duration", "routing", "reorder"):
        # One implementation of "what does this proposal actually change",
        # shared with F-08's healing ladder — see `hxevolve.patch`. Two copies
        # would eventually disagree about the same proposal.
        from case_service.hxevolve import patch as evolve_patch

        patched, errors = evolve_patch.validate_and_patch(
            definition, i.proposal_kind, proposal)
        if errors:
            raise HTTPException(409, "Proposal no longer valid against the current "
                                     "definition: " + "; ".join(errors))
        base = {"id": str(ct.id), "name": ct.name, "version": ct.version,
                "definition_json": definition,
                "default_priority": ct.default_priority,
                "description": ct.description}
        content = {**base, "definition_json": patched}
        artifact_type, artifact_id = "case_type", str(ct.id)
    else:
        raise HTTPException(400, f"Unknown proposal kind {i.proposal_kind!r}")

    ag = user.active_access_group
    branch = ArtifactBranchModel(
        name=f"hxevolve/{i.proposal_kind}-{str(i.id)[:8]}"[:200],
        description=description,
        branch_type="artifact", artifact_type=artifact_type,
        artifact_id=artifact_id,
        source_env_name="dev (local)", status="open",
        base_snapshot=base, content_snapshot=content,
        created_by=user.username, owner_id=user.user_id,
        access_group_id=uuid.UUID(ag.id) if ag and ag.id else None,
        tenant_id=await caller_tenant_uuid(session, user),   # 8e
    )
    session.add(branch)
    await session.flush()
    await _log_event(session, branch.id, "branch_created", user, {
        "branch_name": branch.name, "artifact_type": artifact_type,
        "artifact_id": artifact_id, "via": "hxevolve",
        "insight_id": str(i.id),
    })
    i.status = "staged"
    i.branch_id = branch.id
    i.staged_rule_id = staged_rule_id
    await session.commit()
    return {**_view(i), "branch_name": branch.name}
