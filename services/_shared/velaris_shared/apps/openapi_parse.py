"""Best-effort OpenAPI parse — no LLM — shared by DevConn and marketplace grants.

Moved out of `case_service.devconn.service` (2026-10-19, step 7 — the commerce
split): marketplace grants (commerce-service) turn a package's OpenAPI document
into connector operations with it. Unchanged; DevConn imports it back.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
import re


def _heuristic_parse(spec_text: str, name: str) -> dict:
    """Best-effort parse without LLM — extracts paths and methods."""
    try:
        spec = json.loads(spec_text)
    except Exception:
        try:
            import yaml  # type: ignore
            spec = yaml.safe_load(spec_text)
        except Exception:
            return {"name": name, "suggested_operations": [], "auth_notes": "Could not parse spec", "base_url": ""}

    servers = spec.get("servers", [{}])
    base_url = servers[0].get("url", "") if servers else ""
    paths = spec.get("paths", {})

    ops = []
    for path, methods in list(paths.items())[:10]:
        for method, op in methods.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            ops.append({
                "operation_id":       op.get("operationId", f"{method}_{path.replace('/', '_')}"),
                "summary":            op.get("summary", ""),
                "method":             method.upper(),
                "url":                base_url + re.sub(r"\{(\w+)\}", r"{\1}", path),
                "headers":            {},
                "auth_type":          "bearer" if spec.get("components", {}).get("securitySchemes") else "none",
                "body_template":      "",
                "response_mapping":   {},
                "step_type_suggestion": f"http_{method}",
            })

    return {
        "name":                name,
        "suggested_operations": ops,
        "auth_notes":          "Check the spec security schemes for authentication requirements.",
        "base_url":            base_url,
    }
