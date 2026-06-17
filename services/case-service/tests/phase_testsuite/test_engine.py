"""Test Suite (#27) Phase A — DSL parser/interpolation/asserts + executor e2e.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest

from case_service.testsuite import dsl, executor


# ── DSL parsing / validation (closed action set, D4) ──────────────────────────

def test_parse_rejects_unknown_action():
    with pytest.raises(dsl.DslError):
        dsl.parse_suite([{"id": "t1", "steps": [{"action": "rm_rf", "endpoint": "/x"}]}])


def test_parse_rejects_http_without_endpoint():
    with pytest.raises(dsl.DslError):
        dsl.parse_suite([{"id": "t1", "steps": [{"action": "api_get"}]}])


def test_parse_rejects_unknown_identity():
    with pytest.raises(dsl.DslError):
        dsl.parse_suite([{"id": "t1", "steps": [
            {"action": "api_get", "endpoint": "/x", "identity": "root"}]}])


def test_parse_valid_suite():
    suite = dsl.parse_suite([{
        "id": "t1", "name": "ok", "tags": ["smoke"],
        "steps": [{"action": "api_get", "endpoint": "/health",
                   "assert": {"response_status": 200}}],
    }])
    assert suite[0].id == "t1" and suite[0].steps[0].action == "api_get"
    assert suite[0].steps[0].asserts[0]["op"] == "status"


def test_interpolate_preserves_type_for_whole_ref():
    ctx = {"case_id": 42, "name": "x"}
    assert dsl.interpolate("{{case_id}}", ctx) == 42            # int preserved
    assert dsl.interpolate("/cases/{{case_id}}", ctx) == "/cases/42"
    assert dsl.interpolate({"a": "{{name}}"}, ctx) == {"a": "x"}


def test_check_assert_ops():
    assert dsl.check_assert({"path": "response_status", "op": "status", "value": 200}, 200, {})[0]
    assert dsl.check_assert({"path": "response.s", "op": "eq", "value": "new"}, 200, {"s": "new"})[0]
    assert not dsl.check_assert({"path": "response.s", "op": "eq", "value": "x"}, 200, {"s": "new"})[0]
    assert dsl.check_assert({"path": "response.msg", "op": "contains", "value": "ok"}, 200, {"msg": "all ok"})[0]


# ── Executor end-to-end against the live app ──────────────────────────────────

@pytest.mark.asyncio
async def test_execute_test_lifecycle(client, anon_client):
    """A real lifecycle test driven entirely by the DSL engine."""
    clients = {"admin": client, "none": anon_client}
    lifecycle = str(uuid.uuid4())
    test = dsl.parse_test({
        "id": "case-lifecycle",
        "name": "create case-type + case, fetch, teardown",
        "steps": [
            {"action": "api_post", "endpoint": "/api/v1/case-types",
             "body": {"name": "TS Engine CT", "version": "1.0.0",
                      "lifecycle_process_id": lifecycle,
                      "definition_json": {"stages": []}, "default_priority": "medium"},
             "capture": "ct_id = response.id",
             "assert": {"response_status": 201}},
            {"action": "create_case",
             "body": {"case_type_id": "{{ct_id}}", "data": {"subject": "ts"}},
             "capture": "case_id = response.id",
             "assert": {"response_status": 201}},
            {"action": "api_get", "endpoint": "/api/v1/cases/{{case_id}}",
             "assert": {"response_status": 200}},
        ],
        "teardown": [
            {"action": "api_delete", "endpoint": "/api/v1/cases/{{case_id}}"},
            {"action": "api_delete", "endpoint": "/api/v1/case-types/{{ct_id}}"},
        ],
    })
    result = await executor.execute_test(clients, test)
    assert result["status"] == "passed", result["error_detail"]
    # teardown steps ran
    assert any(s.get("phase") == "teardown" for s in result["step_results"])


@pytest.mark.asyncio
async def test_execute_test_records_failure(client):
    """A failing assert marks the test failed, not errored."""
    test = dsl.parse_test({
        "id": "bad-assert", "name": "wrong status",
        "steps": [{"action": "api_get", "endpoint": "/health",
                   "assert": {"response_status": 500}}],
    })
    result = await executor.execute_test({"admin": client}, test)
    assert result["status"] == "failed"
