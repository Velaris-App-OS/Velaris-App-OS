"""HxDBMigrate P2 — semantic column classification from a bounded value sample.

Reads a CAPPED sample (default 100 rows per table, ONE query per table) and classifies each
column by value patterns: email · phone · credit_card · ssn · date_of_birth · postal_code ·
enum · id · boolean · numeric · free_text · text. Read-only.

SECURITY: raw sampled values are used only transiently, in memory. Nothing here persists a
real value — callers store the derived category, ratios, and MASKED examples only.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import datetime as _dt
import re
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

SAMPLE_ROWS = 100          # rows sampled per table (bounded — never a full scan)
MAX_SAMPLE_TABLES = 300    # cap tables sampled per analysis

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE = re.compile(r"^\+?[\d][\d\s().\-]{6,}$")
_SSN = re.compile(r"^\d{3}-?\d{2}-?\d{4}$")
_CC = re.compile(r"^\d[\d \-]{11,21}\d$")            # 13–19 digits with optional separators
_NUM = re.compile(r"^-?\d+(\.\d+)?$")


def _luhn(s: str) -> bool:
    ds = [int(c) for c in re.sub(r"\D", "", s)]
    if not 13 <= len(ds) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(ds)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def mask(category: str, value: Any) -> str:
    """Return a privacy-preserving masked example — never the raw value."""
    v = str(value)
    if category == "email":
        p = v.split("@")
        return (p[0][:1] + "***@***") if len(p) == 2 else "***"
    if category in ("credit_card", "ssn"):
        digits = re.sub(r"\D", "", v)
        return "***" + digits[-4:] if len(digits) >= 4 else "***"
    if len(v) <= 2:
        return "*" * len(v)
    return v[:1] + "***"


async def sample_table(session: AsyncSession, quoted_table: str, limit: int = SAMPLE_ROWS) -> list[dict[str, Any]]:
    """Read up to ``limit`` rows from a source table (read-only, one query)."""
    res = await session.execute(text(f"SELECT * FROM {quoted_table} LIMIT {int(limit)}"))
    return [dict(r) for r in res.mappings().all()]


def classify_column(name: str, values: list[Any]) -> dict[str, Any]:
    """Classify a column from its sampled values. Returns category + ratios + masked examples."""
    total = len(values)
    vals = [v for v in values if v is not None]
    n = len(vals)
    null_ratio = round((total - n) / total, 2) if total else 0.0
    if n == 0:
        return {"category": "unknown", "confidence": 0.0, "null_ratio": null_ratio,
                "distinct_ratio": 0.0, "masked_examples": []}

    strs = [str(v) for v in vals]
    distinct = len(set(strs))
    distinct_ratio = round(distinct / n, 2)
    lname = (name or "").lower()

    def frac(rx: re.Pattern) -> float:
        return sum(1 for s in strs if rx.match(s)) / n

    scores = {
        "email": frac(_EMAIL),
        "ssn": frac(_SSN),
        "credit_card": sum(1 for s in strs if _CC.match(s) and _luhn(s)) / n,
    }
    if any(k in lname for k in ("phone", "mobile", "tel")):
        scores["phone"] = frac(_PHONE)
    date_frac = sum(1 for v in vals if isinstance(v, (_dt.date, _dt.datetime))) / n
    if date_frac > 0.6 and any(k in lname for k in ("dob", "birth")):
        scores["date_of_birth"] = date_frac

    best = max(scores, key=scores.get) if scores else ""
    if best and scores[best] >= 0.6:
        cat, conf = best, scores[best]
    elif distinct <= 20 and distinct_ratio < 0.5 and n >= 5:
        cat, conf = "enum", round(1 - distinct_ratio, 2)
    elif lname == "id" or lname.endswith("_id") or lname == "uuid":
        cat, conf = "id", 0.9
    elif set(s.lower() for s in strs) <= {"0", "1", "true", "false"}:
        cat, conf = "boolean", 0.9
    elif all(_NUM.match(s) for s in strs):
        cat, conf = "numeric", 0.8
    elif max(len(s) for s in strs) > 80:
        cat, conf = "free_text", 0.6
    else:
        cat, conf = "text", 0.4

    examples = [mask(cat, s) for s in list(dict.fromkeys(strs))[:3]]
    return {"category": cat, "confidence": round(conf, 2), "null_ratio": null_ratio,
            "distinct_ratio": distinct_ratio, "masked_examples": examples}
