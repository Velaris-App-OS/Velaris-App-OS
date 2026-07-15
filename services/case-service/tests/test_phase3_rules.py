"""Phase 3 tests: rules evaluator, forms CRUD, data models CRUD.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
from tests.conftest import deploy_case_type, create_case


# ═══════════════════════════════════════════════════════════════════
# Rules Evaluator — Pure Function Tests
# ═══════════════════════════════════════════════════════════════════


class TestResolvePathPure:
    def test_simple_path(self):
        from case_service.core.rules_evaluator import resolve_path
        ctx = {"case": {"data": {"amount": 500}}}
        assert resolve_path(ctx, "case.data.amount") == 500

    def test_missing_path(self):
        from case_service.core.rules_evaluator import resolve_path
        assert resolve_path({"a": 1}, "a.b.c") is None

    def test_top_level(self):
        from case_service.core.rules_evaluator import resolve_path
        assert resolve_path({"status": "open"}, "status") == "open"


class TestConditionEvaluation:
    def test_eq(self):
        from case_service.core.rules_evaluator import evaluate_condition
        c = {"field_path": "x", "operator": "eq", "value": 10}
        assert evaluate_condition(c, {"x": 10}) is True
        assert evaluate_condition(c, {"x": 20}) is False

    def test_gt_lt(self):
        from case_service.core.rules_evaluator import evaluate_condition
        assert evaluate_condition(
            {"field_path": "x", "operator": "gt", "value": 5}, {"x": 10}
        ) is True
        assert evaluate_condition(
            {"field_path": "x", "operator": "lt", "value": 5}, {"x": 10}
        ) is False

    def test_in_operator(self):
        from case_service.core.rules_evaluator import evaluate_condition
        c = {"field_path": "region", "operator": "in", "value": ["EMEA", "APAC"]}
        assert evaluate_condition(c, {"region": "EMEA"}) is True
        assert evaluate_condition(c, {"region": "AMER"}) is False

    def test_is_empty(self):
        from case_service.core.rules_evaluator import evaluate_condition
        c = {"field_path": "notes", "operator": "is_empty", "value": None}
        assert evaluate_condition(c, {"notes": ""}) is True
        assert evaluate_condition(c, {"notes": "hello"}) is False

    def test_matches_regex(self):
        from case_service.core.rules_evaluator import evaluate_condition
        c = {"field_path": "email", "operator": "matches", "value": r".*@.*\.com"}
        assert evaluate_condition(c, {"email": "a@b.com"}) is True
        assert evaluate_condition(c, {"email": "invalid"}) is False

    def test_compare_two_fields(self):
        from case_service.core.rules_evaluator import evaluate_condition
        c = {"field_path": "actual", "operator": "gt", "value_field_path": "expected"}
        assert evaluate_condition(c, {"actual": 100, "expected": 50}) is True

    def test_nested_path(self):
        from case_service.core.rules_evaluator import evaluate_condition
        c = {"field_path": "case.data.amount", "operator": "gte", "value": 1000}
        ctx = {"case": {"data": {"amount": 1500}}}
        assert evaluate_condition(c, ctx) is True


class TestWhenRules:
    def test_matched(self):
        from case_service.core.rules_evaluator import evaluate_when_rule
        rule = {
            "id": "r1", "name": "Auto Approve",
            "conditions": [{"field_path": "amount", "operator": "lt", "value": 100}],
            "actions": [{"action_type": "set_value", "target": "approved", "value": True}],
        }
        ctx = {"amount": 50}
        result = evaluate_when_rule(rule, ctx)
        assert result["matched"] is True
        assert ctx["approved"] is True

    def test_not_matched(self):
        from case_service.core.rules_evaluator import evaluate_when_rule
        rule = {
            "id": "r2", "name": "High Value",
            "conditions": [{"field_path": "amount", "operator": "gt", "value": 10000}],
            "actions": [{"action_type": "set_value", "target": "flagged", "value": True}],
        }
        ctx = {"amount": 50}
        result = evaluate_when_rule(rule, ctx)
        assert result["matched"] is False
        assert "flagged" not in ctx

    def test_multiple_conditions_and(self):
        from case_service.core.rules_evaluator import evaluate_when_rule
        rule = {
            "id": "r3", "name": "Multi",
            "conditions": [
                {"field_path": "amount", "operator": "gt", "value": 100},
                {"field_path": "region", "operator": "eq", "value": "EMEA"},
            ],
            "actions": [{"action_type": "log", "value": "matched"}],
        }
        assert evaluate_when_rule(rule, {"amount": 200, "region": "EMEA"})["matched"] is True
        assert evaluate_when_rule(rule, {"amount": 200, "region": "APAC"})["matched"] is False


class TestDecisionTable:
    def test_basic_lookup(self):
        from case_service.core.rules_evaluator import evaluate_decision_table
        rule = {
            "id": "dt1",
            "table_columns": [
                {"id": "c-region", "name": "Region", "field_path": "region", "is_condition": True},
                {"id": "c-queue", "name": "Queue", "field_path": "queue", "is_condition": False},
            ],
            "table_rows": [
                {"conditions": {"c-region": "EMEA"}, "outcomes": {"c-queue": "queue-emea"}},
                {"conditions": {"c-region": "APAC"}, "outcomes": {"c-queue": "queue-apac"}},
            ],
        }
        result = evaluate_decision_table(rule, {"region": "APAC"})
        assert result["matched"] is True
        assert result["outcomes"]["queue"] == "queue-apac"

    def test_no_match(self):
        from case_service.core.rules_evaluator import evaluate_decision_table
        rule = {
            "id": "dt2",
            "table_columns": [
                {"id": "c-x", "name": "X", "field_path": "x", "is_condition": True},
            ],
            "table_rows": [
                {"conditions": {"c-x": "a"}, "outcomes": {}},
            ],
        }
        result = evaluate_decision_table(rule, {"x": "z"})
        assert result["matched"] is False

    def test_range_condition(self):
        from case_service.core.rules_evaluator import evaluate_decision_table
        rule = {
            "id": "dt3",
            "table_columns": [
                {"id": "c-amt", "name": "Amount", "field_path": "amount", "is_condition": True},
                {"id": "c-tier", "name": "Tier", "field_path": "tier", "is_condition": False},
            ],
            "table_rows": [
                {"conditions": {"c-amt": {"$gte": 0, "$lt": 100}}, "outcomes": {"c-tier": "low"}, "priority": 0},
                {"conditions": {"c-amt": {"$gte": 100, "$lt": 1000}}, "outcomes": {"c-tier": "medium"}, "priority": 0},
                {"conditions": {"c-amt": {"$gte": 1000}}, "outcomes": {"c-tier": "high"}, "priority": 0},
            ],
        }
        assert evaluate_decision_table(rule, {"amount": 50})["outcomes"]["tier"] == "low"
        assert evaluate_decision_table(rule, {"amount": 500})["outcomes"]["tier"] == "medium"
        assert evaluate_decision_table(rule, {"amount": 5000})["outcomes"]["tier"] == "high"


class TestExpressions:
    def test_simple_math(self):
        from case_service.core.rules_evaluator import evaluate_expression
        assert evaluate_expression("x + y", {"x": 10, "y": 20}) == 30

    def test_nested_context(self):
        from case_service.core.rules_evaluator import evaluate_expression
        ctx = {"case": {"data": {"amount": 100}}}
        result = evaluate_expression("case_data_amount * 0.1", ctx)
        assert result == 10.0

    def test_builtins(self):
        from case_service.core.rules_evaluator import evaluate_expression
        assert evaluate_expression("max(a, b)", {"a": 3, "b": 7}) == 7
        assert evaluate_expression("abs(x)", {"x": -5}) == 5

    def test_invalid_expression(self):
        from case_service.core.rules_evaluator import evaluate_expression
        assert evaluate_expression("import os", {}) is None


class TestDataValidation:
    def test_valid_data(self):
        from case_service.core.rules_evaluator import validate_data
        dm = {
            "fields": [
                {"name": "email", "validations": [
                    {"rule": "required"},
                    {"rule": "pattern", "value": r".*@.*"},
                ]},
                {"name": "amount", "validations": [
                    {"rule": "min_value", "value": 0},
                ]},
            ],
        }
        result = validate_data(dm, {"email": "a@b.com", "amount": 100})
        assert result["valid"] is True
        assert result["errors"] == []

    def test_missing_required(self):
        from case_service.core.rules_evaluator import validate_data
        dm = {"fields": [{"name": "name", "validations": [{"rule": "required"}]}]}
        result = validate_data(dm, {"name": ""})
        assert result["valid"] is False
        assert len(result["errors"]) == 1

    def test_min_max_length(self):
        from case_service.core.rules_evaluator import validate_data
        dm = {"fields": [{"name": "code", "validations": [
            {"rule": "min_length", "value": 3},
            {"rule": "max_length", "value": 10},
        ]}]}
        assert validate_data(dm, {"code": "AB"})["valid"] is False
        assert validate_data(dm, {"code": "ABC"})["valid"] is True
        assert validate_data(dm, {"code": "A" * 11})["valid"] is False

    def test_min_max_value(self):
        from case_service.core.rules_evaluator import validate_data
        dm = {"fields": [{"name": "age", "validations": [
            {"rule": "min_value", "value": 0},
            {"rule": "max_value", "value": 150},
        ]}]}
        assert validate_data(dm, {"age": -1})["valid"] is False
        assert validate_data(dm, {"age": 25})["valid"] is True
        assert validate_data(dm, {"age": 200})["valid"] is False


class TestRuleDispatch:
    def test_dispatches_when(self):
        from case_service.core.rules_evaluator import evaluate_rule
        result = evaluate_rule(
            {"id": "r1", "rule_type": "when", "conditions": [], "actions": []},
            {},
        )
        assert result["matched"] is True

    def test_dispatches_constraint(self):
        from case_service.core.rules_evaluator import evaluate_rule
        result = evaluate_rule(
            {"id": "c1", "rule_type": "constraint",
             "conditions": [{"field_path": "x", "operator": "gt", "value": 0}]},
            {"x": 5},
        )
        assert result["holds"] is True

    def test_dispatches_expression(self):
        from case_service.core.rules_evaluator import evaluate_rule
        ctx = {"x": 10}
        result = evaluate_rule(
            {"id": "e1", "rule_type": "expression",
             "expression": "x * 2", "result_field_path": "doubled"},
            ctx,
        )
        assert result["result"] == 20
        assert ctx["doubled"] == 20

    def test_unknown_type(self):
        from case_service.core.rules_evaluator import evaluate_rule
        result = evaluate_rule({"id": "u1", "rule_type": "alien"}, {})
        assert "error" in result


# ═══════════════════════════════════════════════════════════════════
# Rules API — CRUD + Evaluation
# ═══════════════════════════════════════════════════════════════════


class TestRulesAPI:
    async def test_create_and_get(self, client):
        resp = await client.post("/api/v1/rules", json={
            "name": "Auto Approve", "version": "1.0.0",
            "rule_type": "when",
            "definition_json": {
                "conditions": [{"field_path": "amount", "operator": "lt", "value": 100}],
                "actions": [{"action_type": "set_value", "target": "approved", "value": True}],
            },
        })
        assert resp.status_code == 201
        rule = resp.json()
        assert rule["name"] == "Auto Approve"

        resp = await client.get(f"/api/v1/rules/{rule['id']}")
        assert resp.status_code == 200

    async def test_list_with_filters(self, client):
        await client.post("/api/v1/rules", json={
            "name": "R1", "version": "1.0.0", "rule_type": "when",
            "definition_json": {}, "scope": "global",
        })
        await client.post("/api/v1/rules", json={
            "name": "R2", "version": "1.0.0", "rule_type": "decision_table",
            "definition_json": {}, "scope": "case_type",
        })

        resp = await client.get("/api/v1/rules?rule_type=when")
        assert resp.json()["total"] == 1

        resp = await client.get("/api/v1/rules?scope=case_type")
        assert resp.json()["total"] == 1

    async def test_update_and_delete(self, client):
        resp = await client.post("/api/v1/rules", json={
            "name": "ToEdit", "version": "1.0.0", "rule_type": "when",
            "definition_json": {"conditions": []},
        })
        rid = resp.json()["id"]

        resp = await client.patch(f"/api/v1/rules/{rid}", json={
            "enabled": False, "priority": 99,
        })
        assert resp.json()["enabled"] is False
        assert resp.json()["priority"] == 99

        resp = await client.delete(f"/api/v1/rules/{rid}")
        assert resp.status_code == 204

    async def test_evaluate_via_api(self, client):
        resp = await client.post("/api/v1/rules", json={
            "name": "EvalTest", "version": "1.0.0", "rule_type": "when",
            "definition_json": {
                "conditions": [{"field_path": "amount", "operator": "gt", "value": 50}],
                "actions": [{"action_type": "set_value", "target": "flagged", "value": True}],
            },
        })
        rid = resp.json()["id"]

        resp = await client.post(f"/api/v1/rules/{rid}/evaluate", json={
            "context": {"amount": 100},
        })
        assert resp.status_code == 200
        assert resp.json()["result"]["matched"] is True

    async def test_batch_evaluate(self, client):
        r1 = (await client.post("/api/v1/rules", json={
            "name": "B1", "version": "1.0.0", "rule_type": "when",
            "definition_json": {"conditions": [{"field_path": "x", "operator": "eq", "value": 1}], "actions": []},
        })).json()
        r2 = (await client.post("/api/v1/rules", json={
            "name": "B2", "version": "1.0.0", "rule_type": "when",
            "definition_json": {"conditions": [{"field_path": "x", "operator": "eq", "value": 2}], "actions": []},
        })).json()

        resp = await client.post("/api/v1/rules/evaluate/batch", json={
            "rule_ids": [r1["id"], r2["id"]],
            "context": {"x": 1},
        })
        assert resp.status_code == 200
        results = resp.json()["results"]
        assert results[0]["matched"] is True
        assert results[1]["matched"] is False

    async def test_validate_data_via_api(self, client):
        dm = (await client.post("/api/v1/data-models", json={
            "name": "ClaimData", "version": "1.0.0",
            "definition_json": {
                "fields": [
                    {"name": "amount", "validations": [{"rule": "required"}, {"rule": "min_value", "value": 0}]},
                    {"name": "description", "validations": [{"rule": "min_length", "value": 5}]},
                ],
            },
        })).json()

        resp = await client.post("/api/v1/rules/validate-data", json={
            "data_model_id": dm["id"],
            "data": {"amount": 100, "description": "Water damage claim"},
        })
        assert resp.json()["valid"] is True

        resp = await client.post("/api/v1/rules/validate-data", json={
            "data_model_id": dm["id"],
            "data": {"amount": None, "description": "Hi"},
        })
        assert resp.json()["valid"] is False
        assert len(resp.json()["errors"]) >= 2


# ═══════════════════════════════════════════════════════════════════
# Data Models API — CRUD
# ═══════════════════════════════════════════════════════════════════


class TestDataModelsAPI:
    async def test_create_and_get(self, client):
        resp = await client.post("/api/v1/data-models", json={
            "name": "CustomerData", "version": "1.0.0",
            "definition_json": {"fields": [{"name": "email", "field_type": "email"}]},
        })
        assert resp.status_code == 201
        dm = resp.json()

        resp = await client.get(f"/api/v1/data-models/{dm['id']}")
        assert resp.status_code == 200
        assert resp.json()["name"] == "CustomerData"

    async def test_duplicate_rejected(self, client):
        await client.post("/api/v1/data-models", json={
            "name": "Dup", "version": "1.0.0", "definition_json": {},
        })
        resp = await client.post("/api/v1/data-models", json={
            "name": "Dup", "version": "1.0.0", "definition_json": {},
        })
        assert resp.status_code == 409

    async def test_list(self, client):
        await client.post("/api/v1/data-models", json={
            "name": "DM1", "version": "1.0.0", "definition_json": {},
        })
        resp = await client.get("/api/v1/data-models")
        assert resp.json()["total"] >= 1

    async def test_update_and_delete(self, client):
        dm = (await client.post("/api/v1/data-models", json={
            "name": "Editable", "version": "1.0.0",
            "definition_json": {"fields": []},
        })).json()

        resp = await client.patch(f"/api/v1/data-models/{dm['id']}", json={
            "definition_json": {"fields": [{"name": "new_field"}]},
        })
        assert len(resp.json()["definition_json"]["fields"]) == 1

        resp = await client.delete(f"/api/v1/data-models/{dm['id']}")
        assert resp.status_code == 204


# ═══════════════════════════════════════════════════════════════════
# Forms API — CRUD
# ═══════════════════════════════════════════════════════════════════


class TestFormsAPI:
    async def test_create_and_get(self, client):
        resp = await client.post("/api/v1/forms", json={
            "name": "Intake Form", "version": "1.0.0",
            "definition_json": {
                "sections": [{"id": "s1", "title": "Basic Info"}],
                "fields": [{"id": "f1", "data_field_id": "email", "widget": "text_input"}],
            },
        })
        assert resp.status_code == 201
        form = resp.json()

        resp = await client.get(f"/api/v1/forms/{form['id']}")
        assert resp.status_code == 200

    async def test_with_data_model_reference(self, client):
        dm = (await client.post("/api/v1/data-models", json={
            "name": "FormDM", "version": "1.0.0", "definition_json": {},
        })).json()

        resp = await client.post("/api/v1/forms", json={
            "name": "Bound Form", "version": "1.0.0",
            "data_model_id": dm["id"],
            "definition_json": {"fields": []},
        })
        assert resp.status_code == 201
        assert resp.json()["data_model_id"] == dm["id"]

    async def test_invalid_data_model_rejected(self, client):
        resp = await client.post("/api/v1/forms", json={
            "name": "Bad Ref", "version": "1.0.0",
            "data_model_id": str(uuid.uuid4()),
            "definition_json": {},
        })
        assert resp.status_code == 404

    async def test_list(self, client):
        await client.post("/api/v1/forms", json={
            "name": "F1", "version": "1.0.0", "definition_json": {},
        })
        resp = await client.get("/api/v1/forms")
        assert resp.json()["total"] >= 1

    async def test_update_and_delete(self, client):
        form = (await client.post("/api/v1/forms", json={
            "name": "Editable Form", "version": "1.0.0",
            "definition_json": {"fields": []},
        })).json()

        resp = await client.patch(f"/api/v1/forms/{form['id']}", json={
            "definition_json": {"fields": [{"id": "new"}]},
        })
        assert len(resp.json()["definition_json"]["fields"]) == 1

        resp = await client.delete(f"/api/v1/forms/{form['id']}")
        assert resp.status_code == 204


# ═══════════════════════════════════════════════════════════════════
# Rules API — HxGuard rules.write gate (was auth-only; HxDraft backlog)
# ═══════════════════════════════════════════════════════════════════


def _token_for(roles: list[str]) -> dict:
    from case_service.auth.jwt_handler import create_dev_token
    from case_service.config import get_settings

    s = get_settings()
    token = create_dev_token(
        user_id=str(uuid.uuid4()),
        username="test-limited",
        roles=roles,
        secret=s.auth_secret,
        private_key=s.auth_rsa_private_key or "",
    )
    return {"Authorization": f"Bearer {token}"}


class TestRulesWriteHxGuard:
    _BODY = {
        "name": "GateProbe", "version": "1.0.0", "rule_type": "when",
        "definition_json": {"conditions": []},
    }

    async def test_plain_user_cannot_write(self, client):
        hdrs = _token_for(["user"])
        resp = await client.post("/api/v1/rules", json=self._BODY, headers=hdrs)
        assert resp.status_code == 403

        rid = (await client.post("/api/v1/rules", json=self._BODY)).json()["id"]
        assert (await client.patch(f"/api/v1/rules/{rid}", json={"enabled": False},
                                   headers=hdrs)).status_code == 403
        assert (await client.delete(f"/api/v1/rules/{rid}",
                                    headers=hdrs)).status_code == 403
        # rule untouched by the denied calls
        assert (await client.get(f"/api/v1/rules/{rid}")).json()["enabled"] is True

    async def test_plain_user_can_still_read_and_evaluate(self, client):
        rid = (await client.post("/api/v1/rules", json=self._BODY)).json()["id"]
        hdrs = _token_for(["user"])
        assert (await client.get("/api/v1/rules", headers=hdrs)).status_code == 200
        assert (await client.get(f"/api/v1/rules/{rid}", headers=hdrs)).status_code == 200
        resp = await client.post(f"/api/v1/rules/{rid}/evaluate",
                                 json={"context": {}}, headers=hdrs)
        assert resp.status_code == 200

    async def test_designer_can_write(self, client):
        hdrs = _token_for(["designer"])
        resp = await client.post("/api/v1/rules", json=self._BODY, headers=hdrs)
        assert resp.status_code == 201
        rid = resp.json()["id"]
        assert (await client.delete(f"/api/v1/rules/{rid}",
                                    headers=hdrs)).status_code == 204
