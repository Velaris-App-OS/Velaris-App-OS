"""Test Suite (#27) — structural conformance checks (deterministic, no LLM).

This is the HARD marketplace gate (decision D3): 100% of these must pass before
a package may be submitted. Pure static analysis of a package's declarative
artifacts — never AI, never live execution — so the gate can never be flaky.

A `package` is a dict: {manifest, case_types[], forms[], rules[]}.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import re
import uuid
from typing import Any

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
# Artifact keys whose LITERAL uuid values indicate a hardcoded tenant/user binding.
_IDENTITY_KEYS = frozenset({"tenant_id", "user_id", "assignee", "assignee_id", "created_by", "owner_id"})
_VALID_FIELD_TYPES = frozenset({
    "text", "textarea", "number", "select", "multiselect", "date", "datetime",
    "checkbox", "radio", "rating", "signature", "file", "email", "phone", "currency",
})


def _check(name: str, ok: bool, detail: str = "") -> dict:
    return {"check": name, "ok": ok, "detail": detail}


def _check_manifest(pkg: dict) -> list[dict]:
    m = pkg.get("manifest") or {}
    if not m:
        return [_check("manifest_present", False, "no manifest in package")]
    out = [_check("manifest_present", True)]
    out.append(_check("manifest_name", bool(m.get("name")), "manifest.name required"))
    ver = m.get("version", "")
    out.append(_check("manifest_version", bool(re.match(r"^\d+\.\d+\.\d+", str(ver))),
                      f"semver-ish version required, got {ver!r}"))
    return out


def _check_stages_reachable(pkg: dict) -> list[dict]:
    out: list[dict] = []
    for ct in pkg.get("case_types", []):
        name = ct.get("name", "?")
        stages = (ct.get("definition_json") or {}).get("stages", [])
        if not stages:
            # An empty lifecycle is valid (manual-only) — not a failure.
            out.append(_check(f"stages[{name}]", True, "no stages (manual lifecycle)"))
            continue
        ids = {s.get("id") or s.get("name") for s in stages}
        # every transition target must resolve
        bad = []
        for s in stages:
            for t in (s.get("transitions") or []):
                if t not in ids:
                    bad.append(t)
        out.append(_check(f"stage_transitions[{name}]", not bad,
                          f"dangling transition targets: {bad}" if bad else ""))
        # at least one terminal stage (explicit flag or the last by order)
        has_terminal = any(s.get("terminal") for s in stages) or len(stages) >= 1
        out.append(_check(f"stage_terminal[{name}]", has_terminal, "no terminal stage"))
    return out


def _check_forms_reference_fields(pkg: dict) -> list[dict]:
    out: list[dict] = []
    for form in pkg.get("forms", []):
        name = form.get("name", form.get("id", "?"))
        fields = form.get("fields", [])
        keys = [f.get("key") or f.get("name") for f in fields]
        out.append(_check(f"form_field_keys[{name}]", all(keys) and len(keys) == len(set(keys)),
                          "fields need unique non-empty keys"))
        bad_types = [f.get("type") for f in fields if f.get("type") not in _VALID_FIELD_TYPES]
        out.append(_check(f"form_field_types[{name}]", not bad_types,
                          f"unknown field types: {bad_types}" if bad_types else ""))
        # explicit field references (e.g. rule/prefill bindings) must resolve
        refs = form.get("field_refs", [])
        missing = [r for r in refs if r not in keys]
        out.append(_check(f"form_field_refs[{name}]", not missing,
                          f"references to missing fields: {missing}" if missing else ""))
    return out


def _check_rules_acyclic(pkg: dict) -> list[dict]:
    rules = pkg.get("rules", [])
    graph = {r.get("id") or r.get("name"): list(r.get("depends_on", [])) for r in rules}
    WHITE, GREY, BLACK = 0, 1, 2
    color = {n: WHITE for n in graph}
    cycle = []

    def dfs(n: str) -> bool:
        color[n] = GREY
        for m in graph.get(n, []):
            if m not in color:
                continue  # external dep — ignore
            if color[m] == GREY or (color[m] == WHITE and dfs(m)):
                cycle.append(n)
                return True
        color[n] = BLACK
        return False

    acyclic = not any(color[n] == WHITE and dfs(n) for n in graph)
    return [_check("rules_acyclic", acyclic,
                   f"circular rule dependency near {cycle}" if not acyclic else "")]


def _scan_hardcoded(value: Any, path: str, hits: list[str]) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            if k in _IDENTITY_KEYS and isinstance(v, str) and _UUID_RE.match(v):
                hits.append(f"{path}.{k}={v}")
            _scan_hardcoded(v, f"{path}.{k}", hits)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _scan_hardcoded(v, f"{path}[{i}]", hits)


def _check_no_hardcoded_ids(pkg: dict) -> list[dict]:
    hits: list[str] = []
    for section in ("case_types", "forms", "rules"):
        _scan_hardcoded(pkg.get(section, []), section, hits)
    return [_check("no_hardcoded_ids", not hits,
                   f"hardcoded tenant/user ids: {hits}" if hits else "")]


def run_structural(package: dict) -> dict:
    """Run all structural checks. Returns {passed, total, failed, checks}."""
    checks: list[dict] = []
    checks += _check_manifest(package)
    checks += _check_stages_reachable(package)
    checks += _check_forms_reference_fields(package)
    checks += _check_rules_acyclic(package)
    checks += _check_no_hardcoded_ids(package)
    failed = sum(1 for c in checks if not c["ok"])
    return {"passed": failed == 0, "total": len(checks), "failed": failed, "checks": checks}


async def record_conformance_run(session, package: dict, *, triggered_by: str,
                                 app_package_id: uuid.UUID | None = None):
    """Run structural conformance and persist it as a TestRun (+ per-check results).

    Returns the persisted TestRunModel. status = passed | failed.
    """
    from datetime import datetime, timezone
    from case_service.db.models import TestRunModel, TestResultModel

    result = run_structural(package)
    run = TestRunModel(
        suite_name="conformance:structural", triggered_by=triggered_by,
        app_package_id=app_package_id, status="passed" if result["passed"] else "failed",
        total=result["total"], passed=result["total"] - result["failed"],
        failed=result["failed"], completed_at=datetime.now(timezone.utc),
    )
    session.add(run)
    await session.flush()
    for c in result["checks"]:
        session.add(TestResultModel(
            run_id=run.id, test_id=c["check"], test_name=c["check"],
            status="passed" if c["ok"] else "failed",
            error_detail=None if c["ok"] else c["detail"], step_results=[c],
        ))
    await session.commit()
    return run
