"""Test Suite (#27) — execute a parsed TestCase against the live instance.

Runs each step via a per-identity HTTP client (decision D5), interpolates
`{{var}}`, captures values, evaluates structured asserts, and ALWAYS runs
teardown (even on failure) so tests self-clean.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import time
from typing import Any, Callable

from case_service.testsuite import dsl
from case_service.testsuite.dsl import Step, TestCase

# High-level action → (HTTP method, endpoint template, body source)
# endpoint templates use {{var}} which interpolate() resolves from ctx.
_HIGH_LEVEL = {
    "create_case":       ("POST",  "/api/v1/cases"),
    "stage_transition":  ("POST",  "/api/v1/cases/{{case_id}}/stage-transition"),
    "resolve_case":      ("POST",  "/api/v1/cases/{{case_id}}/resolve"),
    "submit_via_portal": ("POST",  "/api/v1/portal/{{slug}}/submit"),
    "assert_notification_sent": ("GET", "/api/v1/testsuite/notifications/{{case_id}}"),
}
_HTTP_METHOD = {"api_get": "GET", "api_post": "POST", "api_patch": "PATCH", "api_delete": "DELETE"}


def _resolve_http(step: Step, ctx: dict) -> tuple[str, str, dict | None] | None:
    """Return (method, endpoint, body) for an HTTP step, or None for ctx-only actions."""
    if step.action in _HTTP_METHOD:
        return _HTTP_METHOD[step.action], dsl.interpolate(step.endpoint, ctx), dsl.interpolate(step.body, ctx)
    if step.action == "time_travel":
        # ctx-only: record an offset a later trigger-sla-check action consumes.
        ctx["_time_offset_hours"] = step.params.get("offset_hours", 0)
        return None
    if step.action in _HIGH_LEVEL:
        method, tmpl = _HIGH_LEVEL[step.action]
        endpoint = dsl.interpolate(step.endpoint or tmpl, ctx)
        return method, endpoint, dsl.interpolate(step.body, ctx)
    return None


async def _run_step(clients: dict, step: Step, ctx: dict) -> dict:
    """Execute one step; returns a step-result record. Raises on assert failure."""
    resolved = _resolve_http(step, ctx)
    if resolved is None:  # ctx-only action (e.g. time_travel)
        return {"action": step.action, "ok": True, "detail": "ctx-only"}

    method, endpoint, body = resolved
    client = clients.get(step.identity) or clients["admin"]
    resp = await client.request(method, endpoint, json=body)
    try:
        rjson = resp.json()
    except Exception:
        rjson = None

    if step.capture:
        dsl.apply_capture(step.capture, rjson, ctx)

    detail = {"action": step.action, "endpoint": endpoint, "status": resp.status_code, "ok": True, "asserts": []}
    for a in step.asserts:
        ok, msg = dsl.check_assert(a, resp.status_code, rjson)
        detail["asserts"].append({"ok": ok, "msg": msg})
        if not ok:
            detail["ok"] = False
    if not detail["ok"]:
        raise AssertionError(f"step '{step.action} {endpoint}' assertions failed: {detail['asserts']}")
    return detail


async def execute_test(clients: dict, test: TestCase, base_ctx: dict | None = None) -> dict:
    """Run a TestCase end-to-end. Teardown always runs. Returns a result record."""
    ctx = dict(base_ctx or {})
    started = time.monotonic()
    step_results: list[dict] = []
    status = "passed"
    error_detail: str | None = None

    try:
        for step in test.steps:
            step_results.append(await _run_step(clients, step, ctx))
    except AssertionError as e:
        status = "failed"
        error_detail = str(e)
    except Exception as e:  # noqa: BLE001 — a transport/5xx error is an errored test, not a crash
        status = "error"
        error_detail = f"{type(e).__name__}: {e}"

    # Teardown ALWAYS runs (best-effort; never flips a pass to fail).
    for step in test.teardown:
        try:
            step_results.append({**(await _run_step(clients, step, ctx)), "phase": "teardown"})
        except Exception as e:  # noqa: BLE001
            step_results.append({"action": step.action, "ok": False, "phase": "teardown", "detail": str(e)})

    return {
        "test_id": test.id,
        "test_name": test.name,
        "status": status,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "error_detail": error_detail,
        "step_results": step_results,
    }
