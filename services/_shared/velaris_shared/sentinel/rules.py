"""HxSentinel P2 — the deterministic rule catalogue.

Like the taxonomy, the rule DEFINITIONS are a code constant, not database rows:
a rule must mean the same thing in every tenant, and a constant cannot drift
between the PostgreSQL migration track and the generated MySQL baseline. A
tenant customises a rule through ``case_risk_rules`` (enable/disable, severity,
threshold params) — an override, never a redefinition.

Every rule is deterministic and explainable — no model, no scoring. Each one
cross-references a case's signals (P1) against the tenant's own labelled history
(``case_outcomes``) and, when it fires, cites the exact evidence. The engine
(``case_service.sentinel.engine``) owns the queries; this module owns the
catalogue and the shared severity vocabulary.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: Severity vocabulary, ordered weakest -> strongest. Used both to render a
#: finding and to compare against a tenant's verify-gate threshold.
SEVERITIES: tuple[str, ...] = ("low", "medium", "high")


def severity_rank(severity: str) -> int:
    """Position of a severity in the ordered vocabulary; unknown -> -1."""
    try:
        return SEVERITIES.index(severity)
    except ValueError:
        return -1


def meets(severity: str, threshold: str) -> bool:
    """True when ``severity`` is at or above ``threshold`` (the gate test)."""
    t = severity_rank(threshold)
    return t >= 0 and severity_rank(severity) >= t


@dataclass(frozen=True)
class Rule:
    key: str
    display: str
    description: str
    #: Default severity when the rule fires; a tenant may override it.
    default_severity: str = "medium"
    #: Default threshold parameters; a tenant override is merged over these, so a
    #: tenant only has to specify the keys it wants to change.
    default_params: dict = field(default_factory=dict)

    def params_with(self, override: dict | None) -> dict:
        merged = dict(self.default_params)
        if override:
            merged.update({k: v for k, v in override.items() if v is not None})
        return merged


RULES: tuple[Rule, ...] = (
    Rule(
        key="shared_doc_fraud",
        display="Document reused from a confirmed-bad case",
        description=(
            "A document on this case (by content hash) also appears on another "
            "case this tenant labelled fraud, identity theft, or a criminal "
            "transaction. Recommend re-verifying the document before proceeding."
        ),
        default_severity="high",
    ),
    Rule(
        key="identity_velocity",
        display="Identity shared across many recent cases",
        description=(
            "An identity value on this case (email / phone / address, matched by "
            "keyed digest) recurs on several other cases within the window. High "
            "velocity is a common synthetic-identity and abuse signal. Recommend "
            "enhanced verification. Values shared by more cases than max_cases "
            "are treated as non-identifying and ignored."
        ),
        default_severity="medium",
        #: max_cases is a CEILING, the other end of min_cases. A value on
        #: hundreds of cases is not a person — it is a shared address or a
        #: template constant sitting in a variable whose name happens to match an
        #: identity hint. Without it every case carrying that value fires.
        #:
        #: Set high (100) on purpose: the ceiling also hides a genuine ring that
        #: shares one identity across more cases than this, so it must only catch
        #: values no plausible individual could account for. Tenants tune it.
        default_params={"window_days": 30, "min_cases": 3, "max_cases": 100},
    ),
    Rule(
        key="amount_anomaly",
        display="Transaction amount is an outlier for this case type",
        description=(
            "A numeric amount on this case is a statistical outlier (z-score above "
            "threshold) against the case type's own history. Deterministic — no "
            "model. Recommend review. Skips honestly when there is too little "
            "history to judge."
        ),
        default_severity="low",
        default_params={"z": 3.0, "min_samples": 8, "hints": ["amount", "value", "total", "claim"]},
    ),
    Rule(
        key="shared_signal_negative_outcome",
        display="Signal shared with a confirmed-bad case",
        description=(
            "A signal on this case matches one on another case this tenant labelled "
            "with any negative outcome. Broader and lower-severity than the specific "
            "rules above; it catches shared signals they do not, and cites the case."
        ),
        default_severity="low",
    ),
    Rule(
        key="resembles_confirmed_cases",
        display="Resembles confirmed-bad cases (similarity)",
        description=(
            "This case shares MULTIPLE signals with one or more cases the tenant "
            "confirmed bad — a stronger, higher-confidence match than a single shared "
            "signal. Similarity is arithmetic signal-vector overlap (no model); the "
            "finding cites each neighbour and exactly which signals overlap."
        ),
        default_severity="medium",
        default_params={"min_shared": 2, "top_k": 5},
    ),
)

RULES_BY_KEY: dict[str, Rule] = {r.key: r for r in RULES}


def catalogue() -> list[dict]:
    """Rule catalogue for the config UI (defaults only; no tenant overrides)."""
    return [
        {
            "key": r.key,
            "display": r.display,
            "description": r.description,
            "default_severity": r.default_severity,
            "default_params": r.default_params,
        }
        for r in RULES
    ]
