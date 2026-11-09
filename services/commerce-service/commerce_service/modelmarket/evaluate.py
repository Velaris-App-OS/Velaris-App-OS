"""The gate: score a stranger's model against the importing tenant's own data.

Design: docs/Future/Done/AI-ERA-ROADMAP.md §F-07 ("mandatory eval run in the sandbox
against the task's benchmark set ... refusal of packages whose manifest claims
conflict with measured behavior").

The importing tenant has a property no publisher can fake: **the model has never
seen any of their cases**, so every labelled case they hold is a holdout. That
makes this a real measurement rather than a re-run of the publisher's own
arithmetic, and it is why the eval happens here rather than at publish time.

Two ways a package fails, and they are different failures:

  · **It does not work here.** Measured AUC at or below chance means installing
    it would add noise to somebody's risk queue.
  · **The claim does not reproduce.** Measured materially below what the card
    declares means the card is wrong, whatever the cause. Worse *within*
    tolerance is expected — a model trained on another tenant's distribution
    transferring imperfectly is a fact about the data, not a fault.

And one non-failure that must never be reported as one: a tenant with no
labelled history cannot judge anybody's model. `insufficient_data` refuses the
install with a reason, and on a young install it is the common answer.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.modelmarket import packaging
from commerce_service.modelmarket import sentinel_calls
from velaris_shared.sentinel import maths
from velaris_shared.sentinel.schema import MODEL_FEATURES
from velaris_shared.db.models import CaseInstanceModel

VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VERDICT_INSUFFICIENT = "insufficient_data"

#: Enough of the importer's own labelled history for a measurement to mean
#: anything. Below this the AUC swings on single cases.
MIN_HOLDOUT = 20
MIN_PER_CLASS = 5

#: At or below this the model is not distinguishing anything on this data.
MIN_USEFUL_AUC = 0.55
#: How far below the published figure a re-measurement may land before the card
#: is treated as wrong rather than as optimistic.
DEFAULT_TOLERANCE = 0.10

#: How many recent cases a signal pack is dry-run over.
PACK_SAMPLE = 200
#: And how few make the dry-run stop being a measurement. Found live: a pack
#: was gated in on a sample of ONE, which proves the rules parse and nothing
#: whatever about how they behave here. Same floor as the risk model's, for the
#: same reason.
MIN_PACK_SAMPLE = 20
#: A rule that fires on nearly everything is not a signal, it is a background
#: colour — reported, never fatal, because a tenant may want exactly that.
PACK_NOISY_RATE = 0.9


async def evaluate(session: AsyncSession, *, tenant_token: str, tenant_uuid: Any,
                   kind: str, payload: dict, declared: dict | None,
                   tolerance: float = DEFAULT_TOLERANCE) -> dict[str, Any]:
    """Both tenant identities, because the two paths need different ones.

    `case_outcomes.tenant_id` is a TOKEN (a slug, or a UUID in string form) and
    `case_instances.tenant_id` is a real UUID column. Passing one where the
    other belongs does not fail loudly — it silently matches nothing, or it
    raises deep inside the row loader on a value that was never a UUID. Both
    are named here so a caller cannot get it wrong by having only one of them.
    """
    if kind == packaging.KIND_RISK_MODEL:
        return await _evaluate_risk_model(
            session, tenant_token=tenant_token, payload=payload,
            declared=declared or {}, tolerance=tolerance)
    return await _evaluate_signal_pack(
        session, tenant_token=tenant_token, tenant_uuid=tenant_uuid,
        payload=payload)


async def _evaluate_risk_model(session: AsyncSession, *, tenant_token: str,
                               payload: dict, declared: dict,
                               tolerance: float) -> dict[str, Any]:
    X, y = await sentinel_calls.labelled_features(session, tenant_token)
    n = len(y)
    positives = int(y.sum()) if n else 0
    negatives = n - positives
    base = {"holdout_size": n, "positives": positives, "negatives": negatives,
            "declared": declared, "tolerance": tolerance}

    if n < MIN_HOLDOUT or positives < MIN_PER_CLASS or negatives < MIN_PER_CLASS:
        return {**base, "measured": None, "verdict": VERDICT_INSUFFICIENT,
                "reason": (
                    f"this tenant has {n} labelled cases ({positives} bad / "
                    f"{negatives} clean); judging a model needs at least "
                    f"{MIN_HOLDOUT} with {MIN_PER_CLASS} of each. Nothing is wrong "
                    f"with the package — there is nothing here to test it on.")}

    features = list(payload["features"])
    order = [MODEL_FEATURES.index(f) for f in features]
    means = np.array([payload["feature_means"][f] for f in features])
    stds = np.array([payload["feature_stds"][f] for f in features])
    coef = np.array([payload["coefficients"][f] for f in features])
    scores = maths._sigmoid((X[:, order] - means) / stds @ coef + payload["intercept"])

    auc = maths._auc(y, scores)
    above = float((scores > payload["threshold"]).mean())
    measured = {"holdout_auc": auc, "flag_rate": round(above, 3), "holdout_size": n}

    declared_auc = declared.get("holdout_auc")
    if auc is None:
        return {**base, "measured": measured, "verdict": VERDICT_INSUFFICIENT,
                "reason": "AUC is undefined on this tenant's cases"}
    if auc < MIN_USEFUL_AUC:
        return {**base, "measured": measured, "verdict": VERDICT_FAIL,
                "reason": (f"measured AUC {auc:.3f} on this tenant's own cases is at or "
                           f"near chance ({MIN_USEFUL_AUC}); the model does not transfer here")}
    if declared_auc is not None and (float(declared_auc) - auc) > tolerance:
        return {**base, "measured": measured, "verdict": VERDICT_FAIL,
                "reason": (f"the card claims {float(declared_auc):.3f} and it measures "
                           f"{auc:.3f} here — outside the {tolerance} tolerance, so the "
                           f"claim does not reproduce")}
    return {**base, "measured": measured, "verdict": VERDICT_PASS,
            "reason": (f"measured AUC {auc:.3f} over {n} of this tenant's own labelled "
                       f"cases, none of which the model has seen")}


async def _evaluate_signal_pack(session: AsyncSession, *, tenant_token: str,
                                tenant_uuid: Any, payload: dict) -> dict[str, Any]:
    """Dry-run every rule. A pack has no AUC; what it can be wrong about is
    whether its expressions actually evaluate against this platform's feature
    context, and how much noise each one would make here."""
    cases = list((await session.execute(
        select(CaseInstanceModel)
        .where(CaseInstanceModel.tenant_id == tenant_uuid)
        .order_by(CaseInstanceModel.created_at.desc())
        .limit(PACK_SAMPLE))).scalars().all())
    base = {"holdout_size": len(cases), "positives": 0, "negatives": 0,
            "declared": None, "tolerance": None}
    if len(cases) < MIN_PACK_SAMPLE:
        return {**base, "measured": None, "verdict": VERDICT_INSUFFICIENT,
                "reason": (
                    f"this tenant has {len(cases)} cases; dry-running a pack's "
                    f"rules needs at least {MIN_PACK_SAMPLE} for the fire rates "
                    f"to mean anything. Nothing is wrong with the package.")}

    # case-service builds them (HxSentinel); a case it cannot build is skipped.
    contexts = await sentinel_calls.feature_contexts(
        session, tenant_token, [case.id for case in cases])
    if len(contexts) < MIN_PACK_SAMPLE:
        return {**base, "measured": None, "verdict": VERDICT_INSUFFICIENT,
                "reason": (f"a feature context could be built for only "
                           f"{len(contexts)} cases; the floor is {MIN_PACK_SAMPLE}")}

    from velaris_shared.core import safe_expression

    fired: dict[str, int] = {}
    errors: list[str] = []
    for rule in payload.get("rules") or []:
        key = str(rule.get("key"))
        hits = 0
        for ctx in contexts:
            try:
                if safe_expression.evaluate(str(rule.get("when")), ctx):
                    hits += 1
            except Exception as exc:                             # noqa: BLE001
                errors.append(f"rule '{key}' does not evaluate here: {exc}")
                break
        fired[key] = hits

    rates = {k: round(v / len(contexts), 3) for k, v in fired.items()}
    measured = {"sample": len(contexts), "fire_rates": rates,
                "noisy": sorted(k for k, r in rates.items() if r >= PACK_NOISY_RATE)}
    if errors:
        return {**base, "holdout_size": len(contexts), "measured": measured,
                "verdict": VERDICT_FAIL, "reason": "; ".join(errors[:3])}
    return {**base, "holdout_size": len(contexts), "measured": measured,
            "verdict": VERDICT_PASS,
            "reason": (f"all {len(rates)} rules evaluated over {len(contexts)} of this "
                       f"tenant's cases"
                       + (f"; {len(measured['noisy'])} fire on almost everything here"
                          if measured["noisy"] else ""))}
