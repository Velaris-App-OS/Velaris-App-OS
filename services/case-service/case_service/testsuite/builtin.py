"""Test Suite (#27) — built-in suite definitions (DSL).

Phase A ships a minimal read-only Platform Smoke suite. Phase B fills out the
Component / Security suites; Phase C adds the structural Conformance suite.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

# Each suite is a list of DSL test definitions (see testsuite/dsl.py).
PLATFORM_SMOKE = [
    {
        "id": "smoke-health", "name": "service is alive (/health)", "tags": ["smoke"],
        "steps": [{"action": "api_get", "endpoint": "/health",
                   "assert": {"response_status": 200, "response.status": "ok"}}],
    },
    {
        "id": "smoke-ready", "name": "service is ready (/ready)", "tags": ["smoke"],
        "steps": [{"action": "api_get", "endpoint": "/ready",
                   "assert": {"response_status": 200}}],
    },
    {
        "id": "smoke-auth-required", "name": "protected endpoint rejects anonymous",
        "tags": ["smoke", "security"],
        "steps": [{"action": "api_get", "endpoint": "/api/v1/case-types",
                   "identity": "none",
                   "assert": [{"path": "response_status", "op": "status", "value": 401}]}],
    },
]

# ── Component suite (per-module; mutating → self-cleaning via teardown) ────────
COMPONENT = [
    {
        "id": "comp-case-lifecycle", "name": "case-type → case → fetch → resolve",
        "suite": "component", "module": "case-management", "tags": ["component"],
        "steps": [
            {"action": "api_post", "endpoint": "/api/v1/case-types",
             "body": {"name": "TS Component CT", "version": "1.0.0",
                      "lifecycle_process_id": "00000000-0000-0000-0000-0000000000c1",
                      "definition_json": {"stages": []}, "default_priority": "medium"},
             "capture": "ct_id = response.id", "assert": {"response_status": 201}},
            {"action": "create_case",
             "body": {"case_type_id": "{{ct_id}}", "data": {"subject": "component"}},
             "capture": "case_id = response.id", "assert": {"response_status": 201}},
            {"action": "api_get", "endpoint": "/api/v1/cases/{{case_id}}",
             "assert": {"response_status": 200}},
        ],
        "teardown": [
            {"action": "api_delete", "endpoint": "/api/v1/cases/{{case_id}}"},
            {"action": "api_delete", "endpoint": "/api/v1/case-types/{{ct_id}}"},
        ],
    },
]

# ── Security suite (conformance of guardrails — not attack tests) ──────────────
SECURITY = [
    {
        "id": "sec-anon-401", "name": "protected endpoint rejects anonymous",
        "suite": "security", "tags": ["security"],
        "steps": [{"action": "api_get", "endpoint": "/api/v1/case-types", "identity": "none",
                   "assert": {"response_status": 401}}],
    },
    {
        "id": "sec-nonadmin-403", "name": "admin endpoint rejects non-admin",
        "suite": "security", "tags": ["security"],
        "steps": [{"action": "api_get", "endpoint": "/api/v1/testsuite/runs", "identity": "non_admin",
                   "assert": {"response_status": 403}}],
    },
]

_BUILTIN: dict[str, dict] = {
    "platform-smoke": {"suite_type": "platform",  "definition": PLATFORM_SMOKE},
    "component":      {"suite_type": "component", "definition": COMPONENT},
    "security":       {"suite_type": "security",  "definition": SECURITY},
}


def get_builtin_suite(name: str) -> list | None:
    entry = _BUILTIN.get(name)
    return entry["definition"] if entry else None


def list_builtin_suites() -> list[dict]:
    return [{"name": n, "suite_type": e["suite_type"], "count": len(e["definition"])}
            for n, e in _BUILTIN.items()]
