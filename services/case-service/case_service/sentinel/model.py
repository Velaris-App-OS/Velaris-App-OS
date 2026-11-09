"""HxSentinel P5 — the optional local risk-scoring model.

A LOCAL, explainable logistic regression (numpy, no external service, no heavy
ML dependency) over the LABEL-INDEPENDENT features only. It recommends, never
decides; training produces a pending version with holdout metrics that a human
activates.

Why the restricted feature set: ``shares_doc_with_bad`` and
``shared_signals_with_bad`` are computed from *other cases' outcome labels*, so
feeding them to a classifier trained against those same labels launders the
deterministic rules back as a fake AUC. They stay with the rules that cite the
actual case. The model learns only from intrinsic / co-occurrence quantities.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import CaseInstanceModel, CaseOutcomeModel, CaseRiskModelModel
from case_service.sentinel import engine as eng
from case_service.sentinel import packs

#: Label-INDEPENDENT features the model may learn from (never outcome-derived).
# Shared with the model market (step 7): one tuple, imported back.
from velaris_shared.sentinel.schema import MODEL_FEATURES  # noqa: E402,F401

MIN_SAMPLES = 20          # too little history to learn anything trustworthy
MIN_PER_CLASS = 5         # need enough of BOTH classes (imbalance guard)


class TrainError(ValueError):
    """Raised when there is not enough labelled history to train honestly."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# The scoring formulas live in the shared layer (step 7); same functions, same names.
from velaris_shared.sentinel.maths import _auc, _sigmoid  # noqa: E402,F401


def _fit_logreg(X: np.ndarray, y: np.ndarray, *, l2: float = 1.0,
                iters: int = 800, lr: float = 0.3) -> tuple[np.ndarray, float]:
    n, d = X.shape
    w = np.zeros(d)
    b = 0.0
    for _ in range(iters):
        p = _sigmoid(X @ w + b)
        gw = X.T @ (p - y) / n + l2 * w / n
        gb = float(np.mean(p - y))
        w -= lr * gw
        b -= lr * gb
    return w, b


# ── Training ─────────────────────────────────────────────────────────────────


async def _labelled_features(session: AsyncSession, tenant: str) -> tuple[np.ndarray, np.ndarray]:
    """(X, y) over the tenant's cases whose CURRENT outcome is known."""
    negatives = await eng._negative_keys(session, tenant)
    rows = (await session.execute(
        select(CaseOutcomeModel.case_id, CaseOutcomeModel.outcome)
        .where(CaseOutcomeModel.tenant_id == tenant)
        .order_by(CaseOutcomeModel.created_at.desc(), CaseOutcomeModel.id.desc()))).all()
    latest: dict[uuid.UUID, str] = {}
    for cid, outcome in rows:
        latest.setdefault(cid, outcome)

    X, y = [], []
    for cid, outcome in latest.items():
        case = await session.get(CaseInstanceModel, cid)
        if case is None:
            continue
        ctx = await packs.build_feature_context(session, case, tenant)
        X.append([float(ctx[f]) for f in MODEL_FEATURES])
        y.append(1.0 if outcome in negatives else 0.0)
    return np.array(X, dtype=float), np.array(y, dtype=float)


async def train_model(session: AsyncSession, tenant: str) -> CaseRiskModelModel:
    """Train a PENDING model version on the tenant's labelled history."""
    X, y = await _labelled_features(session, tenant)
    n = len(y)
    npos = int(y.sum()) if n else 0
    nneg = n - npos
    if n < MIN_SAMPLES or npos < MIN_PER_CLASS or nneg < MIN_PER_CLASS:
        raise TrainError(
            f"need >= {MIN_SAMPLES} labelled cases with >= {MIN_PER_CLASS} of each "
            f"outcome class to train honestly (have {n}: {npos} bad / {nneg} clean)")

    means = X.mean(axis=0)
    stds = X.std(axis=0)
    stds[stds == 0] = 1.0                 # a constant feature -> no scaling, no div0
    Xs = (X - means) / stds

    # Stratified 80/20 holdout so a class can't vanish from either side.
    rng = np.random.default_rng(42)
    idx_pos = np.where(y == 1)[0]; rng.shuffle(idx_pos)
    idx_neg = np.where(y == 0)[0]; rng.shuffle(idx_neg)

    def _split(idx):
        k = max(1, int(round(len(idx) * 0.2)))
        return idx[k:], idx[:k]

    tr_p, ho_p = _split(idx_pos)
    tr_n, ho_n = _split(idx_neg)
    tr = np.concatenate([tr_p, tr_n])
    ho = np.concatenate([ho_p, ho_n])

    w, b = _fit_logreg(Xs[tr], y[tr])
    auc = _auc(y[ho], _sigmoid(Xs[ho] @ w + b))

    last = (await session.execute(
        select(CaseRiskModelModel.version).where(CaseRiskModelModel.tenant_id == tenant)
        .order_by(CaseRiskModelModel.version.desc()).limit(1))).scalar_one_or_none()
    version = (last or 0) + 1

    model = CaseRiskModelModel(
        tenant_id=tenant, version=version,
        coefficients={f: round(float(wi), 4) for f, wi in zip(MODEL_FEATURES, w)},
        intercept=round(float(b), 4),
        feature_means={f: round(float(m), 4) for f, m in zip(MODEL_FEATURES, means)},
        feature_stds={f: round(float(s), 4) for f, s in zip(MODEL_FEATURES, stds)},
        trained_on=n,
        metrics={"holdout_auc": auc, "positives": npos, "negatives": nneg,
                 "holdout_size": int(len(ho))},
        status="pending")
    session.add(model)
    await session.flush()
    return model


# ── Activation ───────────────────────────────────────────────────────────────


async def activate_model(session: AsyncSession, tenant: str, version: int, *, actor: str
                         ) -> CaseRiskModelModel | None:
    m = (await session.execute(
        select(CaseRiskModelModel).where(
            CaseRiskModelModel.tenant_id == tenant, CaseRiskModelModel.version == version))
    ).scalars().first()
    if m is None:
        return None
    # Archive whatever is currently active — one active version per tenant.
    for a in (await session.execute(
        select(CaseRiskModelModel).where(
            CaseRiskModelModel.tenant_id == tenant, CaseRiskModelModel.status == "active"))
    ).scalars().all():
        a.status = "archived"
    m.status = "active"
    m.activated_by = actor
    m.activated_at = _utcnow()
    await session.flush()
    return m


async def get_active_model(session: AsyncSession, tenant: str) -> CaseRiskModelModel | None:
    return (await session.execute(
        select(CaseRiskModelModel).where(
            CaseRiskModelModel.tenant_id == tenant, CaseRiskModelModel.status == "active")
        .limit(1))).scalars().first()


async def list_models(session: AsyncSession, tenant: str) -> list[CaseRiskModelModel]:
    return list((await session.execute(
        select(CaseRiskModelModel).where(CaseRiskModelModel.tenant_id == tenant)
        .order_by(CaseRiskModelModel.version.desc()))).scalars().all())


# ── Inference ────────────────────────────────────────────────────────────────


def score_case(model: CaseRiskModelModel, ctx: dict) -> dict:
    """Probability + explainable top factors for a case's feature context."""
    x = np.array([float(ctx.get(f, 0.0)) for f in MODEL_FEATURES])
    means = np.array([model.feature_means[f] for f in MODEL_FEATURES])
    stds = np.array([model.feature_stds[f] for f in MODEL_FEATURES])
    coef = np.array([model.coefficients[f] for f in MODEL_FEATURES])
    xs = (x - means) / stds
    prob = float(_sigmoid(np.array([float(xs @ coef + model.intercept)]))[0])
    contribs = coef * xs
    top = sorted(zip(MODEL_FEATURES, contribs), key=lambda t: abs(t[1]), reverse=True)
    return {
        "score": round(prob, 3),
        "top_factors": [{"feature": f, "contribution": round(float(c), 3)}
                        for f, c in top[:3] if abs(c) > 1e-6],
    }


def _severity_for(score: float) -> str:
    if score >= 0.8:
        return "high"
    if score >= 0.65:
        return "medium"
    return "low"


def finding_payload(model: CaseRiskModelModel, ctx: dict) -> dict | None:
    """A `model_risk` finding payload if the model scores above its threshold."""
    scored = score_case(model, ctx)
    if scored["score"] <= model.threshold:
        return None
    factors = ", ".join(f["feature"] for f in scored["top_factors"]) or "n/a"
    return {
        "rule_key": "model_risk",          # stable across versions — never orphan on re-activate
        "severity": _severity_for(scored["score"]),
        "title": "Elevated risk (local model)",
        "message": (f"The local risk model scores this case {scored['score']} "
                    f"(v{model.version}). Top factors: {factors}. A recommendation, not a verdict."),
        "evidence": {"source": "model", "model_version": model.version,
                     "score": scored["score"], "top_factors": scored["top_factors"]},
    }
