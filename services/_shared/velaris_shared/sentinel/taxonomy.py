"""HxSentinel — the outcome taxonomy.

The CORE labels are a code constant, deliberately not database rows. Two
reasons:

1. Cross-tenant learning depends on a core label meaning the same thing
   everywhere. If tenants could redefine ``fraud``, a model trained across
   tenants would be learning from incompatible vocabularies.
2. Seeded rows would have to be written twice — once in the PostgreSQL
   migration track, once in the generated MySQL baseline — and would silently
   drift. A constant cannot.

Tenants extend the vocabulary through ``case_outcome_labels``; they cannot
shadow a core key (enforced in :func:`validate_outcome`).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CoreLabel:
    key: str
    display: str
    description: str
    #: False marks the "good" outcome. Recording clean cases matters as much as
    #: recording bad ones — a model trained only on confirmed fraud has no
    #: negative class and will find fraud everywhere.
    is_negative: bool = True


CORE_LABELS: tuple[CoreLabel, ...] = (
    CoreLabel("fraud", "Fraud", "Confirmed fraudulent case."),
    CoreLabel("identity_theft", "Identity theft",
              "The subject's identity was used without their consent."),
    CoreLabel("criminal_transaction", "Criminal transaction",
              "Transaction linked to criminal activity."),
    CoreLabel("chargeback", "Chargeback", "Payment reversed by the issuer."),
    CoreLabel("abuse", "Abuse", "Policy or platform abuse, not necessarily criminal."),
    CoreLabel("clean", "Clean",
              "Reviewed and confirmed legitimate.", is_negative=False),
)

CORE_KEYS: frozenset[str] = frozenset(lbl.key for lbl in CORE_LABELS)


def is_core(key: str) -> bool:
    return key in CORE_KEYS


def core_as_dicts() -> list[dict]:
    return [
        {
            "key": lbl.key,
            "display": lbl.display,
            "description": lbl.description,
            "is_negative": lbl.is_negative,
            "is_core": True,
        }
        for lbl in CORE_LABELS
    ]
