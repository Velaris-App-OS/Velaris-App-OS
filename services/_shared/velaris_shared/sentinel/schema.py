"""HxSentinel pack + model SCHEMA — the part other services validate against.

Moved out of `case_service.sentinel.packs` / `.model` (2026-10-19, step 7 — the
commerce split): the model market (commerce-service) validates sentinel pack
manifests and model feature lists, and must not import the sentinel engine to do
it. Nothing here changed; `packs` and `model` import these names back.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from velaris_shared.core import safe_expression
from velaris_shared.sentinel import rules as rulecat

#: The features a trained risk model scores on (was `sentinel.model.MODEL_FEATURES`).
MODEL_FEATURES = ("amount", "amount_zscore", "phone_velocity", "email_velocity", "address_velocity")

#: The flat feature names a pack expression may reference. Documented + fixed:
#: adding a feature is an API change. (The grammar forbids attribute/subscript,
#: so everything a pack sees is a bare scalar/bool name.)
FEATURE_KEYS = (
    "amount", "amount_zscore",
    "phone_velocity", "email_velocity", "address_velocity",
    "shares_doc_with_bad", "shared_signals_with_bad",
)


class ManifestError(ValueError):
    """Raised when a pack manifest is malformed or carries an unsafe rule."""


# ── Manifest validation ──────────────────────────────────────────────────────


def validate_manifest(manifest: dict) -> None:
    """Structural + safety validation. Raises ManifestError; returns None if ok."""
    if not isinstance(manifest, dict):
        raise ManifestError("manifest must be an object")
    pack_id = manifest.get("id")
    if not isinstance(pack_id, str) or not (1 <= len(pack_id) <= 128):
        raise ManifestError("manifest.id is required (1..128 chars)")
    if not isinstance(manifest.get("name"), str) or not manifest["name"]:
        raise ManifestError("manifest.name is required")
    if not isinstance(manifest.get("version"), str) or not manifest["version"]:
        raise ManifestError("manifest.version is required")

    rules = manifest.get("rules")
    if not isinstance(rules, list) or not rules:
        raise ManifestError("manifest.rules must be a non-empty list")
    if len(rules) > 100:
        raise ManifestError("manifest.rules too large (max 100)")
    seen: set[str] = set()
    for r in rules:
        if not isinstance(r, dict):
            raise ManifestError("each rule must be an object")
        key = r.get("key")
        if not isinstance(key, str) or not key:
            raise ManifestError("rule.key is required")
        if key in seen:
            raise ManifestError(f"duplicate rule key '{key}'")
        seen.add(key)
        if r.get("severity") not in rulecat.SEVERITIES:
            raise ManifestError(f"rule '{key}': severity must be one of {rulecat.SEVERITIES}")
        for f in ("title", "message"):
            if not isinstance(r.get(f), str) or not r[f]:
                raise ManifestError(f"rule '{key}': {f} is required")
        when = r.get("when")
        if not isinstance(when, str) or not when.strip():
            raise ManifestError(f"rule '{key}': when-expression is required")
        cls, reason = safe_expression.classify_expression(when)
        # Only the STRICT grammar is allowed for pack rules — no hardened
        # fallback, no rejected expressions. This is the sandbox boundary.
        if cls is not safe_expression.Classification.CONFORMING:
            raise ManifestError(f"rule '{key}': expression not allowed ({reason})")

    taxonomy = manifest.get("taxonomy", [])
    if not isinstance(taxonomy, list):
        raise ManifestError("manifest.taxonomy must be a list")
    for t in taxonomy:
        if not isinstance(t, dict) or not isinstance(t.get("key"), str) or not t["key"]:
            raise ManifestError("each taxonomy entry needs a key")
