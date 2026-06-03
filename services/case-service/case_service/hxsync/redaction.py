"""HxSync — PII redaction engine.

Applies redaction rules to a row before it leaves Helix.
Actions: hash (SHA-256 hex), drop (remove key), mask (replace with ***).
"""
from __future__ import annotations

import hashlib


def apply_redaction(row: dict, rules: list[dict]) -> dict:
    """Return a copy of row with redaction rules applied."""
    result = dict(row)
    for rule in rules:
        field = rule["field_path"]
        action = rule.get("action", "hash")
        if field not in result:
            continue
        val = result[field]
        if action == "drop":
            del result[field]
        elif action == "mask":
            result[field] = "***"
        else:  # hash
            result[field] = hashlib.sha256(str(val).encode()).hexdigest() if val is not None else None
    return result


def apply_transforms(row: dict, mappings: list[dict]) -> dict:
    """Rename fields and apply transforms, returning a new dict keyed by dest_column."""
    result = {}
    for mapping in mappings:
        src = mapping["source_field"]
        dst = mapping["dest_column"]
        transform = mapping.get("transform", "passthrough")
        val = row.get(src)
        if transform == "seconds_to_hours" and val is not None:
            try:
                val = round(float(val) / 3600, 4)
            except (TypeError, ValueError):
                pass
        elif transform == "to_string" and val is not None:
            val = str(val)
        elif transform == "to_int" and val is not None:
            try:
                val = int(val)
            except (TypeError, ValueError):
                pass
        result[dst] = val
    return result
