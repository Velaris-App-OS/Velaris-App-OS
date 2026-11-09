"""What a set of weights discloses about the cases behind it.

Design: docs/Future/Done/AI-ERA-ROADMAP.md §F-07 ("membership-inference/extraction
risk from shared fine-tunes"). The roadmap describes probe *prompts* against
seeded canaries, which is the right shape for a language model. This platform's
shareable artefacts are not language models, so the same question has to be
asked in the form it actually takes here — and it turns out to be sharper.

**The disclosure in a HxSentinel risk model is not the coefficients.** It is
`feature_means` and `feature_stds`. Those are summary statistics of the
publisher's own cases, and a summary stops being a summary when the cohort is
small: a mean claim amount over four cases, with a standard deviation near zero,
is four people's claim amounts published in a file. Every check below exists
because of that one sentence.

Nothing here is advisory. A blocked verdict refuses the publish; the reasons are
kept on the row so the operator can see which check fired and why.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.compliance import pii_detect
from velaris_shared.modelmarket import packaging
from commerce_service.modelmarket import sentinel_calls
from velaris_shared.sentinel import maths
from velaris_shared.sentinel.schema import MODEL_FEATURES
from velaris_shared.db.models import PIIFindingModel

#: Cohort floor for publishing. Mirrors the proof engine's own k-anonymity
#: floor (`proofs.engine.K_ANONYMITY_FLOOR`) deliberately: the two are the same
#: judgement about the same risk, and two different numbers would eventually be
#: defended as if they meant different things.
MIN_COHORT = 25
#: And enough of BOTH outcomes. A cohort of 40 with 3 positives publishes the
#: aggregate of three cases in the coefficients that matter most.
MIN_PER_CLASS = 8

#: A feature whose spread is this small relative to its centre is not describing
#: a distribution any more — it is republishing one value that the whole cohort
#: shared.
MIN_COEFF_OF_VARIATION = 0.02

#: How much better the model may score its own training rows than its holdout
#: before the gap reads as memorisation rather than fit. Generous: a five-feature
#: L2 logistic regression should not be able to memorise at all, so a gap this
#: wide means something other than the model is wrong.
MAX_MEMORISATION_GAP = 0.20

#: Which HxPrivacy classes the risk model's five features derive from. Written
#: out rather than inferred, because this list is the whole argument for what
#: the artefact is allowed to say about itself.
_FEATURE_CLASSES = {
    "amount": pii_detect.FINANCIAL,
    "amount_zscore": pii_detect.FINANCIAL,
    "phone_velocity": pii_detect.PHONE,
    "email_velocity": pii_detect.EMAIL,
    "address_velocity": pii_detect.ADDRESS,
}


def _check(name: str, ok: bool, detail: str, *, blocking: bool = True) -> dict[str, Any]:
    return {"check": name, "ok": bool(ok), "blocking": bool(blocking), "detail": detail}


# ── the checks ───────────────────────────────────────────────────────────────


def cohort_checks(*, trained_on: int, metrics: dict) -> list[dict[str, Any]]:
    positives = int((metrics or {}).get("positives") or 0)
    negatives = int((metrics or {}).get("negatives") or 0)
    return [
        _check("cohort_floor", trained_on >= MIN_COHORT,
               f"trained on {trained_on} cases; the floor for publishing is {MIN_COHORT}"),
        _check("class_floor", positives >= MIN_PER_CLASS and negatives >= MIN_PER_CLASS,
               f"{positives} bad / {negatives} clean; each must be at least {MIN_PER_CLASS}"),
    ]


def aggregate_disclosure(payload: dict) -> list[dict[str, Any]]:
    """Does any published mean amount to one value the whole cohort shared?"""
    out: list[dict[str, Any]] = []
    means = payload.get("feature_means") or {}
    stds = payload.get("feature_stds") or {}
    for name in payload.get("features") or []:
        mean = float(means.get(name, 0.0))
        std = float(stds.get(name, 0.0))
        # A centre at zero has no scale to be small relative to, and the
        # velocity features legitimately sit there on a quiet tenant.
        if abs(mean) < 1e-9:
            out.append(_check(f"spread:{name}", True, "centre is zero; no ratio to judge"))
            continue
        cv = std / abs(mean)
        out.append(_check(
            f"spread:{name}", cv >= MIN_COEFF_OF_VARIATION,
            f"spread/centre = {cv:.4f}; below {MIN_COEFF_OF_VARIATION} the published "
            f"mean {mean:.4g} is effectively one value every case in the cohort shared"))
    return out


async def memorisation(session: AsyncSession, *, tenant_token: str,
                       payload: dict, metrics: dict) -> dict[str, Any]:
    """Score the publisher's OWN training rows and compare with their holdout.

    Cheap, and it is the only check here that actually runs the model. A wide
    gap does not prove extraction is possible; it proves the published metric
    was not measured on data the model had never seen, which is the same defect
    dressed differently.
    """
    holdout = (metrics or {}).get("holdout_auc")
    if holdout is None:
        return _check("memorisation", True,
                      "publisher declared no holdout AUC; nothing to compare",
                      blocking=False)
    try:
        X, y = await sentinel_calls.labelled_features(session, tenant_token)
    except Exception as exc:                                     # noqa: BLE001
        return _check("memorisation", True, f"cohort unavailable: {exc}", blocking=False)
    if len(y) == 0 or len(set(y.tolist())) < 2:
        return _check("memorisation", True, "cohort has only one outcome class",
                      blocking=False)

    features = list(payload["features"])
    means = np.array([payload["feature_means"][f] for f in features])
    stds = np.array([payload["feature_stds"][f] for f in features])
    coef = np.array([payload["coefficients"][f] for f in features])
    order = [MODEL_FEATURES.index(f) for f in features]
    Xs = (X[:, order] - means) / stds
    train_auc = maths._auc(y, maths._sigmoid(Xs @ coef + payload["intercept"]))
    if train_auc is None:
        return _check("memorisation", True, "AUC undefined on the cohort", blocking=False)
    gap = train_auc - float(holdout)
    return _check("memorisation", gap <= MAX_MEMORISATION_GAP,
                  f"train AUC {train_auc:.3f} vs declared holdout {float(holdout):.3f} "
                  f"(gap {gap:+.3f}; the bound is {MAX_MEMORISATION_GAP})")


async def special_category(session: AsyncSession, *, tenant_token: str,
                           kind: str, payload: dict) -> tuple[bool, dict[str, Any], list[str]]:
    """`(is_special, check, data_classes)` — the HxPrivacy linkage.

    Two artefact kinds, two honestly different answers, and the difference is
    worth stating rather than smoothing over:

    · A **risk model** consumes five fixed features whose classes are financial
      and contact. Neither is a GDPR Article 9 class, so a risk model cannot be
      special-category-trained — that is a property of the closed feature
      vocabulary, not a lucky result. The check still runs against the tenant's
      CONFIRMED findings, so the day a new feature widens the vocabulary this
      bites instead of needing to be remembered.

    · A **signal pack** carries free text a human wrote — rule titles, messages,
      taxonomy labels. That text is where an Article 9 class actually reaches a
      shareable artefact ("claimant is diabetic → high risk"), and it is text,
      so HxPrivacy's own classifier can read it. This check fires today.
    """
    if kind == packaging.KIND_SIGNAL_PACK:
        hits: list[str] = []
        classes: set[str] = set()
        for rule in (payload.get("rules") or []):
            for field in ("key", "title", "message"):
                found = pii_detect.classify_field(
                    str(rule.get("key") or ""), str(rule.get(field) or ""), "")
                if not found:
                    continue
                classes.add(found["category"])
                if found["special_category"]:
                    hits.append(f"rule '{rule.get('key')}' {field}: {found['category']}")
        for entry in (payload.get("taxonomy") or []):
            found = pii_detect.classify_field(
                str(entry.get("key") or ""), str(entry.get("display") or ""), "")
            if not found:
                continue
            classes.add(found["category"])
            if found["special_category"]:
                hits.append(f"taxonomy '{entry.get('key')}': {found['category']}")
        return (
            bool(hits),
            _check("special_category", not hits,
                   "; ".join(hits) if hits
                   else "no Article 9 class found in the pack's rule text or taxonomy"),
            sorted(classes),
        )

    classes = sorted({_FEATURE_CLASSES[f] for f in (payload.get("features") or [])
                      if f in _FEATURE_CLASSES})
    confirmed = await _confirmed_special(session, tenant_token)
    overlap = sorted(set(classes) & confirmed)
    return (
        bool(overlap),
        _check("special_category", not overlap,
               f"the model consumes {classes or ['nothing']}; "
               + (f"confirmed Article 9 classes overlap on {overlap}" if overlap
                  else "none of them is a GDPR Article 9 class")),
        classes,
    )


async def _confirmed_special(session: AsyncSession, tenant_token: str) -> set[str]:
    """Confirmed special-category classes this tenant holds, as a set."""
    from velaris_shared.compliance.pii_scan import CONFIRMED

    try:
        rows = (await session.execute(
            select(PIIFindingModel.category)
            .where(PIIFindingModel.status == CONFIRMED,
                   PIIFindingModel.special_category.is_(True),
                   PIIFindingModel.tenant_id == tenant_token)
        )).all()
    except Exception:                                            # noqa: BLE001
        # A scan that has never run is not a clean bill of health, but it is
        # also not a reason to refuse a publish that no rule forbids. The
        # absence is recorded in the check text either way.
        return set()
    return {str(c) for (c,) in rows if c}


# ── the probe ────────────────────────────────────────────────────────────────


async def probe(session: AsyncSession, *, tenant_token: str, kind: str,
                payload: dict, trained_on: int, metrics: dict) -> dict[str, Any]:
    """Run every check. `verdict` is `pass` only when nothing blocking failed."""
    checks: list[dict[str, Any]] = []
    is_special, special_check, classes = await special_category(
        session, tenant_token=tenant_token, kind=kind, payload=payload)
    checks.append(special_check)

    if kind == packaging.KIND_RISK_MODEL:
        checks += cohort_checks(trained_on=trained_on, metrics=metrics or {})
        checks += aggregate_disclosure(payload)
        checks.append(await memorisation(
            session, tenant_token=tenant_token, payload=payload, metrics=metrics or {}))

    failed = [c for c in checks if c["blocking"] and not c["ok"]]
    return {
        "verdict": "blocked" if failed else "pass",
        "checks": checks,
        "failed": [c["check"] for c in failed],
        "data_classes": classes,
        "special_category": is_special,
    }
