"""HxDBMigrate P2 — optional AI narrative over the DETERMINISTIC discovery report.

An add-on: if AI is available it writes a short business narrative from the structure +
already-classified semantics + compliance summary. It is fed **only derived data**
(schema, categories, masked examples, compliance counts) — never raw sampled values — and
runs through the HxNexus backend, so `HELIX_CASE_AI_EGRESS_POLICY` (local_only → Ollama)
is enforced by the factory. Returns None if AI is unavailable or errors (never fatal).

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

log = logging.getLogger(__name__)

_SYSTEM = (
    "You are a data-migration analyst. From the database structure, the deterministically "
    "classified column semantics, and the compliance summary below, write a concise, "
    "business-oriented narrative: what this database appears to do, its key entities and "
    "relationships, and the main migration and compliance risks. Do NOT invent data or "
    "values. Respond in Markdown, under 300 words."
)


async def semantic_narrative(report: dict[str, Any]) -> Optional[str]:
    try:
        from case_service.hxnexus.factory import check_ai_available, get_llm_backend
        if not await check_ai_available():
            return None
        # Send only derived, non-sensitive data — never raw values.
        context = {
            "tables": [
                {"table": s["table"],
                 "columns": [{"name": c["name"], "type": c["type"],
                              "semantic": c.get("semantic", {}).get("category")}
                             for c in s["columns"]]}
                for s in report.get("schema", [])[:80]
            ],
            "quality": report.get("quality", {}).get("score"),
            "compliance": report.get("compliance", {}).get("summary"),
        }
        llm = get_llm_backend()
        out = await llm.complete(json.dumps(context), system=_SYSTEM, temperature=0.2)
        return (out or "").strip() or None
    except Exception as exc:
        log.warning("HxDBMigrate AI narrative failed (non-fatal): %s", exc)
        return None
