"""HxSentinel API — P1: outcome labelling + signal store.

P1 exposes NO recommendations. It records confirmed outcomes (the ground truth
the engine will later learn from) and the extracted signals that make two cases
comparable.

Authorization posture matches the rest of the platform: every case-scoped route
resolves the case first and 404s when the caller may not see it, so this
surface cannot be used to probe for case existence. Labelling is gated more
narrowly than reading — a wrong label poisons everything trained on it.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared import hxguard
from case_service.api.routers.releases import is_feature_enabled
from velaris_shared.core.error_codes import (
    RISK_NOTFOUND_404,
    RISK_REJECTED_400,
    RISK_URL_400,
    coded,
)
from velaris_shared.auth.dependencies import get_current_user
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.db import repository as repo
from velaris_shared.db.models import CaseInstanceModel, CaseOutcomeModel
from velaris_shared.db.session import get_session
from case_service.tenancy.identity import same_tenant
from case_service.sentinel import engine as risk_engine
from case_service.sentinel import rules as rulecat
from case_service.sentinel import service as sentinel
from velaris_shared.db.read_routing import get_read_session

router = APIRouter(prefix="/risk", tags=["hxsentinel"])

FLAG = "hxsentinel"


def _tenant(user: AuthenticatedUser) -> str:
    return user.tenant_id or "default"


def _require_enabled() -> None:
    """Dark until enabled, and 404 rather than 403.

    A 403 would confirm the module exists for this tenant; 404 keeps a
    disabled module indistinguishable from an absent one.
    """
    if not is_feature_enabled(FLAG):
        raise coded(404, RISK_NOTFOUND_404, "Not found")


async def _authorized_case(session, user, case_id: str, action: str) -> uuid.UUID:
    try:
        cid = uuid.UUID(case_id)
    except ValueError:
        raise coded(404, RISK_NOTFOUND_404, "Case not found")
    case = await repo.get_case_instance(session, cid)
    if case is None or not await same_tenant(session, case.tenant_id, user.tenant_id):
        raise coded(404, RISK_NOTFOUND_404, "Case not found")
    await hxguard.require_case(session, user, action, cid)
    return cid


# ── Taxonomy ────────────────────────────────────────────────────────────────


@router.get("/taxonomy")
async def get_taxonomy(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_enabled()
    await hxguard.require(session, hxguard.subject_from_user(user), "risk.assessment.read")
    return {"labels": await sentinel.list_labels(session, _tenant(user))}


class LabelBody(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    display: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    is_negative: bool = True


@router.post("/taxonomy", status_code=201)
async def add_label(
    body: LabelBody,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_enabled()
    await hxguard.require(session, hxguard.subject_from_user(user), "risk.taxonomy.write")
    try:
        row = await sentinel.create_label(
            session, tenant_id=_tenant(user), key=body.key, display=body.display,
            description=body.description, is_negative=body.is_negative,
        )
    except ValueError as exc:
        raise coded(400, RISK_REJECTED_400, str(exc))
    await session.commit()
    return {"key": row.key, "display": row.display, "is_core": False}


# ── Outcome labelling ───────────────────────────────────────────────────────


class OutcomeBody(BaseModel):
    outcome: str = Field(min_length=1, max_length=64)
    note: str | None = Field(default=None, max_length=4000)


def _outcome_view(row: CaseOutcomeModel) -> dict:
    return {
        "outcome": row.outcome,
        "note": row.note,
        "source": row.source,
        "labelled_by": row.labelled_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.get("/cases/{case_id}/outcome")
async def get_outcome(
    case_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_enabled()
    cid = await _authorized_case(session, user, case_id, "case.read")
    await hxguard.require(session, hxguard.subject_from_user(user), "risk.assessment.read")
    current = await sentinel.get_current_outcome(session, cid)
    history = await sentinel.get_outcome_history(session, cid)
    return {
        "case_id": str(cid),
        "current": _outcome_view(current) if current else None,
        # The history is part of the evidence, not an audit curiosity: a case
        # relabelled clean -> fraud months later tells a different story from
        # one labelled fraud at closure.
        "history": [_outcome_view(r) for r in history],
    }


@router.put("/cases/{case_id}/outcome")
async def put_outcome(
    case_id: str,
    body: OutcomeBody,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_enabled()
    cid = await _authorized_case(session, user, case_id, "case.read")
    await hxguard.require(session, hxguard.subject_from_user(user), "risk.outcomes.write")
    try:
        row = await sentinel.set_outcome(
            session, case_id=cid, tenant_id=_tenant(user), outcome=body.outcome,
            labelled_by=user.user_id, note=body.note, source="manual",
        )
    except sentinel.UnknownOutcomeError as exc:
        raise coded(400, RISK_REJECTED_400, str(exc))
    await session.commit()
    return _outcome_view(row)


class BulkOutcomeBody(BaseModel):
    #: Capped so a mislabelled bulk action stays recoverable by hand. The
    #: ground truth is the one thing nothing downstream can correct for.
    case_ids: list[str] = Field(min_length=1, max_length=500)
    outcome: str = Field(min_length=1, max_length=64)
    note: str | None = Field(default=None, max_length=4000)


@router.post("/outcomes/bulk")
async def bulk_outcome(
    body: BulkOutcomeBody,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Label many cases at once — retroactive labelling of closed history.

    Partial success is reported per case rather than failing the batch: one
    unreadable case in a 300-case selection should not discard the other 299.
    """
    _require_enabled()
    await hxguard.require(session, hxguard.subject_from_user(user), "risk.outcomes.write")
    try:
        await sentinel.validate_outcome(session, _tenant(user), body.outcome.strip().lower())
    except sentinel.UnknownOutcomeError as exc:
        raise coded(400, RISK_REJECTED_400, str(exc))

    labelled, skipped = [], []
    for raw in body.case_ids:
        try:
            cid = await _authorized_case(session, user, raw, "case.read")
        except HTTPException:
            skipped.append({"case_id": raw, "reason": "not_found_or_forbidden"})
            continue
        await sentinel.set_outcome(
            session, case_id=cid, tenant_id=_tenant(user), outcome=body.outcome,
            labelled_by=user.user_id, note=body.note, source="bulk",
        )
        labelled.append(str(cid))

    await session.commit()
    return {"labelled": labelled, "skipped": skipped,
            "labelled_count": len(labelled), "skipped_count": len(skipped)}


@router.get("/outcomes/unlabelled")
async def list_unlabelled(
    limit: int = Query(default=50, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Closed cases with no outcome label — the retro-labelling work queue."""
    _require_enabled()
    await hxguard.require(session, hxguard.subject_from_user(user), "risk.assessment.read")

    labelled = select(CaseOutcomeModel.case_id).where(
        CaseOutcomeModel.tenant_id == _tenant(user)
    )

    # The tenant filter MUST be in the WHERE, not applied in Python after the
    # limit: otherwise other tenants' closed cases consume the limit first and
    # a caller sees fewer of their own cases than asked for (or none), with no
    # offset to page past them. case_instances.tenant_id is a UUID column and
    # _tenant() may be the non-UUID sentinel "default", so match on the same
    # semantics _authorized_case uses: the caller's own tenant, plus legacy
    # tenantless (NULL) cases which are visible to everyone.
    tenant_pred = [CaseInstanceModel.tenant_id.is_(None)]
    try:
        tenant_pred.append(CaseInstanceModel.tenant_id == uuid.UUID(_tenant(user)))
    except (ValueError, AttributeError):
        pass  # tenantless caller ("default") sees only NULL-tenant cases

    rows = (await session.execute(
        select(CaseInstanceModel)
        .where(
            CaseInstanceModel.status.in_(("closed", "resolved", "cancelled")),
            CaseInstanceModel.id.notin_(labelled),
            # A deleted case is not offered up for labelling — it is hidden,
            # and asking a human to review it would surface it again.
            CaseInstanceModel.is_deleted.is_(False),
            or_(*tenant_pred),
        )
        # NULLS LAST is Postgres-only ANSI syntax (SQLAlchemy renders it literally and
        # MySQL/MariaDB reject it with a 1064 syntax error). Emulate portably: the
        # `closed_at IS NULL` boolean sorts False(0) before True(1) in default ASC, so
        # non-null rows come first (nulls last), then closed_at DESC orders within them.
        .order_by(CaseInstanceModel.closed_at.is_(None), CaseInstanceModel.closed_at.desc())
        .limit(limit)
    )).scalars().all()
    return {"cases": [{
        "case_id": str(r.id),
        "case_number": r.case_number,
        "status": r.status,
        "closed_at": r.closed_at.isoformat() if r.closed_at else None,
    } for r in rows]}


# ── Signals ─────────────────────────────────────────────────────────────────


@router.get("/cases/{case_id}/signals")
async def get_signals(
    case_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_enabled()
    cid = await _authorized_case(session, user, case_id, "case.read")
    await hxguard.require(session, hxguard.subject_from_user(user), "risk.assessment.read")
    rows = await sentinel.get_signals(session, cid)
    return {"case_id": str(cid), "signals": [{
        "signal_type": r.signal_type,
        # The digest is returned for correlation between findings, never a
        # plaintext identity — there is no plaintext stored to return.
        "signal_key": r.signal_key,
        "hashed": r.hashed,
        "value": r.value,
        "computed_at": r.computed_at.isoformat() if r.computed_at else None,
    } for r in rows]}


@router.post("/cases/{case_id}/signals/extract")
async def extract_signals(
    case_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Re-extract signals for a case. Idempotent — adds only what is new."""
    _require_enabled()
    cid = await _authorized_case(session, user, case_id, "case.read")
    await hxguard.require(session, hxguard.subject_from_user(user), "risk.outcomes.write")
    added = await sentinel.extract_signals(session, cid)
    await session.commit()
    return {"case_id": str(cid), "added": len(added),
            # Why nothing was added, when nothing was. A bare "added: 0" reads as
            # "this case has no signals" even when the real cause is a vault the
            # engine could not open — and that silence keeps the whole engine dark.
            "skipped": getattr(added, "skipped", []),
            # Re-extraction reconciles, so a signal can disappear. Say so rather
            # than let a shrinking index look like nothing happened.
            "removed": getattr(added, "removed", []),
            "signals": [{"signal_type": r.signal_type, "hashed": r.hashed} for r in added]}


# ── Findings (P2 recommendations) ────────────────────────────────────────────


def _finding_view(f) -> dict:
    return {
        "id": str(f.id),
        "rule_key": f.rule_key,
        "severity": f.severity,
        "title": f.title,
        "message": f.message,
        "evidence": f.evidence,
        "status": f.status,
        "created_at": f.created_at.isoformat() if f.created_at else None,
        "updated_at": f.updated_at.isoformat() if f.updated_at else None,
    }


@router.get("/cases/{case_id}/findings")
async def get_case_findings(
    case_id: str,
    evaluate: bool = Query(default=True),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """The risk panel: (re-)evaluate the deterministic rules and list findings.

    Recommendations only — a finding never changes the case on its own. Every
    displayed finding is auditable: evaluation records what it raised, and the
    view records what was shown.
    """
    _require_enabled()
    cid = await _authorized_case(session, user, case_id, "case.read")
    await hxguard.require(session, hxguard.subject_from_user(user), "risk.assessment.read")
    if evaluate:
        await risk_engine.evaluate_case(session, cid, actor_id=user.user_id)
        await session.commit()
    findings = await risk_engine.get_findings(session, cid)
    return {"case_id": str(cid), "findings": [_finding_view(f) for f in findings]}


# ── Human feedback on findings (P2b) ─────────────────────────────────────────


async def _authorized_finding(session, user, finding_id: str):
    """Resolve a finding, then authorize via its case (404 anti-oracle)."""
    try:
        fid = uuid.UUID(finding_id)
    except ValueError:
        raise coded(404, RISK_NOTFOUND_404, "Finding not found")
    from velaris_shared.db.models import CaseRiskFindingModel
    finding = await session.get(CaseRiskFindingModel, fid)
    if finding is None:
        raise coded(404, RISK_NOTFOUND_404, "Finding not found")
    await _authorized_case(session, user, str(finding.case_id), "case.read")
    return finding


class FeedbackBody(BaseModel):
    #: 1 = bad analysis … 5 = best analysis.
    score: int = Field(ge=1, le=5)
    note: str | None = Field(default=None, max_length=4000)


@router.post("/findings/{finding_id}/feedback", status_code=201)
async def add_finding_feedback(
    finding_id: str,
    body: FeedbackBody,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Score a finding 1–5 with an optional note.

    Gated `risk.assessment.read`: scoring requires having *seen* the evidence (the score UI
    lives on the finding). Append-only — re-scoring adds a row.
    """
    _require_enabled()
    finding = await _authorized_finding(session, user, finding_id)
    await hxguard.require(session, hxguard.subject_from_user(user), "risk.assessment.read")
    row = await risk_engine.add_feedback(
        session, finding=finding, reviewer=user.user_id, score=body.score, note=body.note)
    await session.commit()
    return {"id": str(row.id), "score": row.score, "note": row.note}


@router.get("/findings/{finding_id}/feedback")
async def get_finding_feedback(
    finding_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Latest score + full history for a finding, each with an outcome cross-check."""
    _require_enabled()
    finding = await _authorized_finding(session, user, finding_id)
    await hxguard.require(session, hxguard.subject_from_user(user), "risk.assessment.read")
    return {"finding_id": finding_id, **await risk_engine.get_feedback(session, finding)}


# ── Supervisor oversight (P3b) ───────────────────────────────────────────────


@router.get("/scores/dashboard")
async def scores_dashboard(
    session: AsyncSession = Depends(get_read_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Per-reviewer scoring stats for the tenant (supervisor view).

    Reliability = outcome-agreement rate with disputed scores excluded; fast
    median time-to-score or low reliability flags a reviewer for attention.
    """
    _require_enabled()
    await hxguard.require(session, hxguard.subject_from_user(user), "risk.scores.review")
    from case_service.sentinel import oversight
    return await oversight.reviewer_dashboard(session, _tenant(user))


class DisputeBody(BaseModel):
    #: The dispute must be justified — a supervisor overriding a reviewer's
    #: judgment records why, and it is audited.
    note: str = Field(min_length=1, max_length=4000)


@router.post("/feedback/{feedback_id}/dispute", status_code=201)
async def dispute_feedback(
    feedback_id: str,
    body: DisputeBody,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Mark a score disputed — excludes it from reliability and future training."""
    _require_enabled()
    await hxguard.require(session, hxguard.subject_from_user(user), "risk.scores.review")
    try:
        fid = uuid.UUID(feedback_id)
    except ValueError:
        raise coded(404, RISK_NOTFOUND_404, "Feedback not found")
    from velaris_shared.db.models import CaseRiskFeedbackModel
    fb = await session.get(CaseRiskFeedbackModel, fid)
    # Tenant scope: a supervisor disputes only their own tenant's scores.
    if fb is None or norm_tenant_str(fb.tenant_id) != _tenant(user):
        raise coded(404, RISK_NOTFOUND_404, "Feedback not found")
    from case_service.sentinel import oversight
    row = await oversight.dispute_score(
        session, feedback_id=fid, tenant=_tenant(user), disputed_by=user.user_id, note=body.note)
    await session.commit()
    return {"id": str(row.id), "feedback_id": feedback_id, "note": row.note}


def norm_tenant_str(t) -> str:
    from case_service.sentinel.service import norm_tenant
    return norm_tenant(t)


# ── Rule configuration ───────────────────────────────────────────────────────


class RuleConfigBody(BaseModel):
    enabled: bool | None = None
    severity: str | None = Field(default=None, max_length=16)
    params: dict | None = None


@router.get("/rules")
async def list_rules(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """The rule catalogue (code defaults) merged with this tenant's overrides."""
    _require_enabled()
    await hxguard.require(session, hxguard.subject_from_user(user), "risk.assessment.read")
    cfg = await risk_engine._effective_config(session, _tenant(user))
    return {"rules": [{**item, **cfg.get(item["key"], {})} for item in rulecat.catalogue()]}


@router.put("/rules/{rule_key}")
async def put_rule_config(
    rule_key: str,
    body: RuleConfigBody,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Override one rule for this tenant (enable/disable, severity, params)."""
    _require_enabled()
    await hxguard.require(session, hxguard.subject_from_user(user), "risk.rules.configure")
    if rule_key not in rulecat.RULES_BY_KEY:
        raise coded(404, RISK_NOTFOUND_404, "Unknown rule")
    if body.severity is not None and body.severity not in rulecat.SEVERITIES:
        raise HTTPException(400, f"severity must be one of {rulecat.SEVERITIES}")
    # Defence in depth: params only ever carries a handful of numeric thresholds
    # (+ the amount hints list). Bound it so even a trusted admin cannot store an
    # arbitrarily large blob in the JSONB column.
    if body.params is not None:
        import json as _json
        if len(body.params) > 20 or len(_json.dumps(body.params)) > 2000:
            raise HTTPException(400, "params is too large")

    row = await sentinel_rule_upsert(
        session, tenant=_tenant(user), rule_key=rule_key,
        enabled=body.enabled, severity=body.severity, params=body.params,
        updated_by=user.user_id,
    )
    await session.commit()
    return {"rule_key": row.rule_key, "enabled": row.enabled,
            "severity": row.severity, "params": row.params}


# ── Local scoring model (P5) ─────────────────────────────────────────────────


def _model_view(m) -> dict:
    return {
        "version": m.version, "status": m.status, "trained_on": m.trained_on,
        "metrics": m.metrics, "threshold": m.threshold, "coefficients": m.coefficients,
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "activated_by": m.activated_by,
    }


@router.post("/model/train", status_code=201)
async def train_risk_model(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Train a new (pending) local model version on labelled history. 400 when
    there is too little labelled data to train honestly."""
    _require_enabled()
    await hxguard.require(session, hxguard.subject_from_user(user), "risk.rules.configure")
    from case_service.sentinel import model as risk_model
    try:
        m = await risk_model.train_model(session, _tenant(user))
    except risk_model.TrainError as exc:
        raise coded(400, RISK_REJECTED_400, str(exc))
    await session.commit()
    return _model_view(m)


@router.get("/model")
async def get_risk_models(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """All model versions for the tenant (newest first)."""
    _require_enabled()
    await hxguard.require(session, hxguard.subject_from_user(user), "risk.rules.configure")
    from case_service.sentinel import model as risk_model
    return {"models": [_model_view(m) for m in await risk_model.list_models(session, _tenant(user))]}


@router.post("/model/{version}/activate")
async def activate_risk_model(
    version: int,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Activate a trained model version (archives the prior active). Never silent."""
    _require_enabled()
    await hxguard.require(session, hxguard.subject_from_user(user), "risk.rules.configure")
    from case_service.sentinel import model as risk_model
    m = await risk_model.activate_model(session, _tenant(user), version, actor=user.user_id)
    if m is None:
        raise coded(404, RISK_NOTFOUND_404, "Model version not found")
    await session.commit()
    return _model_view(m)


# ── Signal packs (P4a) ───────────────────────────────────────────────────────


def _pack_view(p) -> dict:
    return {
        "pack_id": p.pack_id, "name": p.name, "vertical": p.vertical,
        "version": p.version, "enabled": p.enabled,
        "rule_count": len((p.manifest or {}).get("rules", [])),
        "installed_by": p.installed_by,
        "installed_at": p.installed_at.isoformat() if p.installed_at else None,
    }


class PackInstallBody(BaseModel):
    manifest: dict


@router.post("/packs/install", status_code=201)
async def install_signal_pack(
    body: PackInstallBody,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Install a signal pack (content, not code) for this tenant.

    The manifest is validated structurally and every rule expression must be a
    CONFORMING safe-expression — an unsafe or malformed pack is rejected (400).
    """
    _require_enabled()
    await hxguard.require(session, hxguard.subject_from_user(user), "risk.rules.configure")
    from case_service.sentinel import packs
    try:
        row = await packs.install_pack(
            session, tenant=_tenant(user), manifest=body.manifest, installed_by=user.user_id)
    except packs.ManifestError as exc:
        raise coded(400, RISK_REJECTED_400, str(exc))
    await session.commit()
    return _pack_view(row)


@router.get("/packs/catalogue")
async def signal_pack_catalogue(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """The bundled official packs, each flagged whether this tenant installed it."""
    _require_enabled()
    await hxguard.require(session, hxguard.subject_from_user(user), "risk.rules.configure")
    from case_service.sentinel import official_packs, packs
    installed = {p.pack_id for p in await packs.list_packs(session, _tenant(user))}
    return {"catalogue": [{**c, "installed": c["pack_id"] in installed}
                          for c in official_packs.catalogue()]}


@router.post("/packs/catalogue/{pack_id}/install", status_code=201)
async def install_official_pack(
    pack_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """One-click install of a bundled official pack (no manifest to paste)."""
    _require_enabled()
    await hxguard.require(session, hxguard.subject_from_user(user), "risk.rules.configure")
    from case_service.sentinel import official_packs, packs
    manifest = official_packs.get_official(pack_id)
    if manifest is None:
        raise coded(404, RISK_NOTFOUND_404, "Official pack not found")
    row = await packs.install_pack(
        session, tenant=_tenant(user), manifest=manifest, installed_by=user.user_id)
    await session.commit()
    return _pack_view(row)


class PackUrlBody(BaseModel):
    url: str = Field(min_length=1, max_length=2000)


@router.post("/packs/install-from-url", status_code=201)
async def install_pack_from_url(
    body: PackUrlBody,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Install a community pack from a remote manifest URL (SSRF-guarded, size-
    capped, and validated by the same CONFORMING sandbox as any pack)."""
    _require_enabled()
    await hxguard.require(session, hxguard.subject_from_user(user), "risk.rules.configure")
    from case_service.sentinel import packs
    try:
        row = await packs.install_from_url(
            session, tenant=_tenant(user), url=body.url, installed_by=user.user_id)
    except packs.ManifestError as exc:
        raise coded(400, RISK_REJECTED_400, str(exc))
    except ValueError as exc:             # SSRF / blocked host
        raise coded(400, RISK_URL_400, f"URL rejected: {exc}")
    await session.commit()
    return _pack_view(row)


@router.get("/packs")
async def list_signal_packs(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_enabled()
    await hxguard.require(session, hxguard.subject_from_user(user), "risk.rules.configure")
    from case_service.sentinel import packs
    return {"packs": [_pack_view(p) for p in await packs.list_packs(session, _tenant(user))]}


@router.delete("/packs/{pack_id}", status_code=204)
async def uninstall_signal_pack(
    pack_id: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_enabled()
    await hxguard.require(session, hxguard.subject_from_user(user), "risk.rules.configure")
    from case_service.sentinel import packs
    if not await packs.uninstall_pack(session, _tenant(user), pack_id):
        raise coded(404, RISK_NOTFOUND_404, "Pack not found")
    await session.commit()


# ── Score-driven rule tuning (P3c-1) ─────────────────────────────────────────


def _proposal_view(p) -> dict:
    return {
        "id": str(p.id), "rule_key": p.rule_key, "proposed_change": p.proposed_change,
        "rationale": p.rationale, "evidence": p.evidence, "status": p.status,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "decided_by": p.decided_by, "decision_note": p.decision_note,
    }


@router.post("/rules/tuning/analyze")
async def run_tuning_analysis(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Aggregate the reliability-weighted scores and generate pending proposals.

    Idempotent — will not create a second pending proposal for a rule that
    already has one open. Managing rule tuning is a `risk.rules` action.
    """
    _require_enabled()
    await hxguard.require(session, hxguard.subject_from_user(user), "risk.rules.configure")
    from case_service.sentinel import tuning
    created = await tuning.generate_proposals(session, _tenant(user))
    await session.commit()
    return {"created": [_proposal_view(p) for p in created]}


@router.post("/rules/tuning/mine-notes")
async def run_notes_mining(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Cluster reviewers' feedback notes (LOCAL LLM only — notes never leave the
    platform) into rule-change proposals. Honest 200 with []-created when the
    local model is unavailable. Human approves each proposal."""
    _require_enabled()
    await hxguard.require(session, hxguard.subject_from_user(user), "risk.rules.configure")
    from case_service.sentinel import tuning
    created = await tuning.mine_notes(session, _tenant(user))
    await session.commit()
    return {"created": [_proposal_view(p) for p in created]}


@router.get("/rules/proposals")
async def list_rule_proposals(
    status: str = Query(default="pending"),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_enabled()
    await hxguard.require(session, hxguard.subject_from_user(user), "risk.rules.configure")
    from case_service.sentinel import tuning
    rows = await tuning.list_proposals(session, _tenant(user),
                                       status=None if status == "all" else status)
    return {"proposals": [_proposal_view(p) for p in rows]}


class ProposalDecisionBody(BaseModel):
    decision: str = Field(pattern="^(approved|rejected)$")
    note: str | None = Field(default=None, max_length=4000)


@router.post("/rules/proposals/{proposal_id}/decision")
async def decide_rule_proposal(
    proposal_id: str,
    body: ProposalDecisionBody,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Approve (apply to the rule) or reject a proposal. Never silent."""
    _require_enabled()
    await hxguard.require(session, hxguard.subject_from_user(user), "risk.rules.configure")
    try:
        pid = uuid.UUID(proposal_id)
    except ValueError:
        raise coded(404, RISK_NOTFOUND_404, "Proposal not found")
    from case_service.sentinel import tuning
    p = await tuning.decide_proposal(
        session, proposal_id=pid, tenant=_tenant(user),
        decision=body.decision, decided_by=user.user_id, note=body.note)
    if p is None:
        raise coded(404, RISK_NOTFOUND_404, "Proposal not found")
    await session.commit()
    return _proposal_view(p)


# ── Tenant config: the verify gate ───────────────────────────────────────────


class GateConfigBody(BaseModel):
    #: None / "off" disables the gate; otherwise the minimum finding severity
    #: that opens a stage-advance hold.
    verify_gate_severity: str | None = Field(default=None, max_length=16)


@router.get("/config")
async def get_config(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_enabled()
    await hxguard.require(session, hxguard.subject_from_user(user), "risk.assessment.read")
    from velaris_shared.db.models import CaseRiskConfigModel
    row = await session.get(CaseRiskConfigModel, _tenant(user))
    return {"verify_gate_severity": row.verify_gate_severity if row else None}


@router.put("/config")
async def put_config(
    body: GateConfigBody,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Set the optional verify gate. Managing the gate is a rule-config action."""
    _require_enabled()
    await hxguard.require(session, hxguard.subject_from_user(user), "risk.rules.configure")
    from velaris_shared.db.models import CaseRiskConfigModel
    sev = body.verify_gate_severity
    if sev in ("", "off"):
        sev = None
    if sev is not None and sev not in rulecat.SEVERITIES:
        raise HTTPException(400, f"verify_gate_severity must be null/off or one of {rulecat.SEVERITIES}")
    row = await session.get(CaseRiskConfigModel, _tenant(user))
    if row is None:
        row = CaseRiskConfigModel(tenant_id=_tenant(user), verify_gate_severity=sev,
                                  updated_by=user.user_id)
        session.add(row)
    else:
        row.verify_gate_severity = sev
        row.updated_by = user.user_id
    await session.commit()
    return {"verify_gate_severity": row.verify_gate_severity}


# ── Verify-gate holds ────────────────────────────────────────────────────────


def _hold_view(h) -> dict:
    return {
        "id": str(h.id),
        "rule_key": h.rule_key,
        "severity": h.severity,
        "status": h.status,
        "opened_at": h.opened_at.isoformat() if h.opened_at else None,
        "resolved_by": h.resolved_by,
        "resolved_at": h.resolved_at.isoformat() if h.resolved_at else None,
        "resolution_note": h.resolution_note,
    }


@router.get("/cases/{case_id}/holds")
async def get_case_holds(
    case_id: str,
    all: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    _require_enabled()
    cid = await _authorized_case(session, user, case_id, "case.read")
    await hxguard.require(session, hxguard.subject_from_user(user), "risk.assessment.read")
    holds = await risk_engine.list_holds(session, cid, status=None if all else "open")
    return {"case_id": str(cid), "holds": [_hold_view(h) for h in holds]}


class ResolveHoldBody(BaseModel):
    #: "cleared" (verified, safe to proceed) or "dismissed" (not applicable).
    status: str = Field(default="cleared", max_length=16)
    #: A note is required — the gate exists to force a human to look and say why.
    note: str = Field(min_length=1, max_length=4000)


@router.post("/holds/{hold_id}/resolve")
async def resolve_hold(
    hold_id: str,
    body: ResolveHoldBody,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Clear a verify-gate hold — the only thing that releases the stage gate.

    Gated at ``risk.assessment.read``: the worker who performs the verification is exactly
    who the gate is meant to force to look. The clear is audited with the note.
    """
    _require_enabled()
    try:
        hid = uuid.UUID(hold_id)
    except ValueError:
        raise coded(404, RISK_NOTFOUND_404, "Hold not found")
    from velaris_shared.db.models import CaseRiskHoldModel
    hold = await session.get(CaseRiskHoldModel, hid)
    if hold is None:
        raise coded(404, RISK_NOTFOUND_404, "Hold not found")
    # Case-scoped authorization: resolve the hold's case (404 if not visible).
    await _authorized_case(session, user, str(hold.case_id), "case.read")
    await hxguard.require(session, hxguard.subject_from_user(user), "risk.assessment.read")
    if body.status not in ("cleared", "dismissed"):
        raise HTTPException(400, "status must be 'cleared' or 'dismissed'")
    resolved = await risk_engine.resolve_hold(
        session, hid, resolved_by=user.user_id, status=body.status, note=body.note)
    await session.commit()
    return _hold_view(resolved)


async def sentinel_rule_upsert(session, *, tenant, rule_key, enabled, severity, params, updated_by):
    from velaris_shared.db.models import CaseRiskRuleModel
    row = (await session.execute(
        select(CaseRiskRuleModel).where(
            CaseRiskRuleModel.tenant_id == tenant,
            CaseRiskRuleModel.rule_key == rule_key,
        )
    )).scalars().first()
    if row is None:
        row = CaseRiskRuleModel(
            tenant_id=tenant, rule_key=rule_key,
            enabled=True if enabled is None else enabled,
            severity=severity, params=params, updated_by=updated_by,
        )
        session.add(row)
    else:
        if enabled is not None:
            row.enabled = enabled
        if severity is not None:
            row.severity = severity
        if params is not None:
            row.params = params
        row.updated_by = updated_by
    await session.flush()
    return row
