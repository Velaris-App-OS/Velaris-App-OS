"""HxDBMigrate P2 — PII/PHI compliance scan (derived from semantic classification).

Maps each column's semantic category (and name hints) to a sensitivity class and a
recommended pre-migration handling. The output is the precursor to the Compliance
Migration Certificate. Uses only classifications + masked examples — never raw values.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from typing import Any

# semantic category → (sensitivity, recommended pre-migration action)
_CATEGORY_SENSITIVITY: dict[str, tuple[str, str]] = {
    "email":         ("PII",            "gdpr_erasure_flag"),
    "phone":         ("PII",            "gdpr_erasure_flag"),
    "ssn":           ("PII/national_id", "tokenize"),
    "credit_card":   ("financial/PCI",  "tokenize"),
    "date_of_birth": ("PII/sensitive",  "encrypt_at_rest"),
}

# column-name hints for PII that values alone won't reveal (names, addresses)
_NAME_HINTS = ("first_name", "last_name", "full_name", "surname", "address",
               "street", "city", "postcode", "zip", "passport", "national_id")


def scan(semantic_by_table: dict[str, dict[str, dict[str, Any]]]) -> dict[str, Any]:
    """Build compliance findings + summary from per-table column classifications."""
    findings: list[dict[str, Any]] = []
    for table, cols in semantic_by_table.items():
        for col, c in cols.items():
            cat = c.get("category", "")
            sensitivity = action = None
            if cat in _CATEGORY_SENSITIVITY:
                sensitivity, action = _CATEGORY_SENSITIVITY[cat]
            elif any(h in col.lower() for h in _NAME_HINTS):
                sensitivity, action = "PII", "gdpr_erasure_flag"
            if sensitivity:
                findings.append({
                    "table": table, "column": col, "category": cat,
                    "sensitivity": sensitivity, "recommended_action": action,
                    "masked_examples": c.get("masked_examples", []),
                })

    by_sensitivity: dict[str, int] = {}
    for f in findings:
        by_sensitivity[f["sensitivity"]] = by_sensitivity.get(f["sensitivity"], 0) + 1
    return {
        "findings": findings,
        "summary": {
            "pii_column_count": len(findings),
            "tokenize_required": [f"{f['table']}.{f['column']}" for f in findings
                                  if f["recommended_action"] == "tokenize"],
            "by_sensitivity": by_sensitivity,
        },
    }
