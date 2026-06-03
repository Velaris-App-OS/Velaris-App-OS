"""Tests for HELIX IR case management models.

Validates that all dataclasses instantiate, serialize to dict,
and enforce enum values correctly.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest

# ── Adjust path so imports work standalone ────────────────────────


from helix_ir.models.case import (
    AssignmentRule,
    AssignmentStrategy,
    AuditEntry,
    CaseInstance,
    CasePriority,
    CaseRelationship,
    CaseStatus,
    CaseType,
    EscalationAction,
    RelationshipType,
    SLAEscalation,
    SLAPolicy,
    SLASnapshot,
    SLAStatus,
    StageDefinition,
    StageType,
    StepDefinition,
    StepType,
    WorkQueueDefinition,
    WorkQueueSortField,
)
from helix_ir.models.data_model import (
    DataModelDefinition,
    FieldDefinition,
    FieldType,
    FieldValidation,
    ValidationRule,
)
from helix_ir.models.form import (
    FormActionButton,
    FormAction,
    FormDefinition,
    FormField,
    FormFieldWidget,
    FormSection,
    VisibilityCondition,
)
from helix_ir.models.rule import (
    DecisionTableColumn,
    DecisionTableRow,
    RuleAction,
    RuleCondition,
    RuleDefinition,
    RuleScope,
    RuleType,
)
from helix_ir.models.security import (
    AccessEffect,
    AccessPolicy,
    FieldLevelAccess,
    Permission,
    RoleDefinition,
    SecurityProfile,
)
from helix_ir.models.application import ApplicationDefinition


# ═══════════════════════════════════════════════════════════════════
# case.py
# ═══════════════════════════════════════════════════════════════════


class TestCaseEnums:
    def test_case_status_values(self):
        assert CaseStatus.NEW.value == "new"
        assert CaseStatus.CLOSED.value == "closed"
        assert len(CaseStatus) == 8

    def test_case_priority_ordering(self):
        assert CasePriority.LOW.value < CasePriority.MEDIUM.value
        assert CasePriority.CRITICAL.value < CasePriority.BLOCKER.value

    def test_step_type_covers_bpmn_tasks(self):
        expected = {
            "user_task", "service_task", "script_task", "send_task",
            "manual_task", "subprocess", "call_activity", "approval",
        }
        actual = {st.value for st in StepType}
        assert expected == actual


class TestSLAPolicy:
    def test_create_with_defaults(self):
        sla = SLAPolicy(
            id="sla-1", name="Standard", goal_duration="PT4H",
            deadline_duration="PT8H",
        )
        assert sla.at_risk_threshold == 0.8
        assert CaseStatus.PENDING_EXTERNAL in sla.pause_on_statuses

    def test_with_escalations(self):
        esc = SLAEscalation(
            threshold_percent=0.9,
            action=EscalationAction.NOTIFY,
            target="manager-role",
        )
        sla = SLAPolicy(
            id="sla-2", name="Urgent", goal_duration="PT1H",
            deadline_duration="PT2H", escalations=[esc],
        )
        assert len(sla.escalations) == 1
        assert sla.escalations[0].action == EscalationAction.NOTIFY


class TestStepDefinition:
    def test_minimal_step(self):
        step = StepDefinition(
            id="step-1", name="Review",
            step_type=StepType.USER_TASK,
            bpmn_element_id="task_review_001",
        )
        assert step.required is True
        assert step.repeatable is False
        assert step.form_id is None

    def test_step_with_assignment(self):
        rule = AssignmentRule(
            strategy=AssignmentStrategy.ROLE_BASED,
            target="claims-adjuster",
        )
        step = StepDefinition(
            id="step-2", name="Adjudicate",
            step_type=StepType.APPROVAL,
            bpmn_element_id="task_adjudicate",
            assignment=rule,
        )
        assert step.assignment.strategy == AssignmentStrategy.ROLE_BASED


class TestStageDefinition:
    def test_stage_with_steps(self):
        steps = [
            StepDefinition(
                id=f"s{i}", name=f"Step {i}",
                step_type=StepType.USER_TASK,
                bpmn_element_id=f"task_{i}",
            )
            for i in range(3)
        ]
        stage = StageDefinition(
            id="stage-intake", name="Intake",
            stage_type=StageType.LINEAR, steps=steps,
        )
        assert len(stage.steps) == 3
        assert stage.order == 0


class TestCaseType:
    def test_full_case_type(self):
        ct = CaseType(
            id="ct-claims", name="Insurance Claim",
            version="1.0.0",
            lifecycle_process_id="proc-claims-lifecycle",
            stages=[
                StageDefinition(
                    id="intake", name="Intake",
                    stage_type=StageType.LINEAR, order=0,
                ),
                StageDefinition(
                    id="review", name="Review",
                    stage_type=StageType.PARALLEL, order=1,
                ),
            ],
            sla_policies=[
                SLAPolicy(
                    id="sla-overall", name="Overall",
                    goal_duration="P5D", deadline_duration="P7D",
                ),
            ],
            default_priority=CasePriority.MEDIUM,
        )
        assert ct.name == "Insurance Claim"
        assert len(ct.stages) == 2
        assert len(ct.sla_policies) == 1

    def test_serialise_to_dict(self):
        ct = CaseType(
            id="ct-1", name="Test", version="0.1.0",
            lifecycle_process_id="proc-1",
        )
        d = dataclasses.asdict(ct)
        assert d["id"] == "ct-1"
        # Verify JSON-serialisable
        json.dumps(d, default=str)


class TestCaseInstance:
    def test_minimal_instance(self):
        ci = CaseInstance(
            id="case-001", case_type_id="ct-1",
            case_type_version="1.0.0",
            status=CaseStatus.OPEN,
            priority=CasePriority.HIGH,
        )
        assert ci.status == CaseStatus.OPEN
        assert ci.data == {}
        assert ci.assignments == []

    def test_with_relationships(self):
        rel = CaseRelationship(
            relationship_type=RelationshipType.CHILD,
            target_case_id="case-002",
            propagate_status=True,
        )
        ci = CaseInstance(
            id="case-001", case_type_id="ct-1",
            case_type_version="1.0.0",
            status=CaseStatus.OPEN,
            priority=CasePriority.MEDIUM,
            relationships=[rel],
        )
        assert ci.relationships[0].propagate_status is True


class TestWorkQueueDefinition:
    def test_defaults(self):
        wq = WorkQueueDefinition(id="q-1", name="Default Queue")
        assert wq.sort_fields == [WorkQueueSortField.URGENCY]
        assert wq.auto_assignment is False


# ═══════════════════════════════════════════════════════════════════
# data_model.py
# ═══════════════════════════════════════════════════════════════════


class TestDataModel:
    def test_field_definition(self):
        f = FieldDefinition(
            id="f-amount", name="claim_amount",
            field_type=FieldType.CURRENCY,
            validations=[
                FieldValidation(
                    rule=ValidationRule.MIN_VALUE, value=0,
                    message="Amount must be positive",
                ),
            ],
        )
        assert f.field_type == FieldType.CURRENCY
        assert len(f.validations) == 1

    def test_data_model_with_fields(self):
        dm = DataModelDefinition(
            id="dm-claim", name="ClaimData", version="1.0.0",
            fields=[
                FieldDefinition(
                    id="f-1", name="description",
                    field_type=FieldType.TEXT,
                ),
                FieldDefinition(
                    id="f-2", name="amount",
                    field_type=FieldType.CURRENCY,
                    pii=False, encrypted=False,
                ),
            ],
        )
        assert len(dm.fields) == 2


# ═══════════════════════════════════════════════════════════════════
# form.py
# ═══════════════════════════════════════════════════════════════════


class TestFormDefinition:
    def test_form_with_sections_and_fields(self):
        form = FormDefinition(
            id="form-intake", name="Intake Form",
            version="1.0.0", data_model_id="dm-claim",
            sections=[
                FormSection(id="sec-1", title="Basic Info"),
            ],
            fields=[
                FormField(
                    id="ff-1", data_field_id="f-1",
                    widget=FormFieldWidget.TEXT_AREA,
                    section_id="sec-1",
                ),
            ],
            actions=[
                FormActionButton(
                    action=FormAction.SUBMIT, label="Submit",
                ),
                FormActionButton(
                    action=FormAction.SAVE_DRAFT, label="Save",
                    variant="secondary",
                ),
            ],
        )
        assert form.layout == "vertical"
        assert len(form.actions) == 2


# ═══════════════════════════════════════════════════════════════════
# rule.py
# ═══════════════════════════════════════════════════════════════════


class TestRuleDefinition:
    def test_when_rule(self):
        rule = RuleDefinition(
            id="rule-auto-approve", name="Auto Approve Low Value",
            version="1.0.0", rule_type=RuleType.WHEN,
            conditions=[
                RuleCondition(
                    field_path="case.data.amount",
                    operator="lt", value=100,
                ),
            ],
            actions=[
                RuleAction(
                    action_type="set_value",
                    target="case.data.auto_approved",
                    value=True,
                ),
            ],
        )
        assert rule.rule_type == RuleType.WHEN
        assert len(rule.conditions) == 1

    def test_decision_table(self):
        rule = RuleDefinition(
            id="rule-routing", name="Route by Region",
            version="1.0.0",
            rule_type=RuleType.DECISION_TABLE,
            table_columns=[
                DecisionTableColumn(
                    id="c-region", name="Region",
                    field_path="case.data.region",
                ),
                DecisionTableColumn(
                    id="c-queue", name="Queue",
                    field_path="assignment.queue_id",
                    is_condition=False,
                ),
            ],
            table_rows=[
                DecisionTableRow(
                    conditions={"c-region": "EMEA"},
                    outcomes={"c-queue": "queue-emea"},
                ),
                DecisionTableRow(
                    conditions={"c-region": "APAC"},
                    outcomes={"c-queue": "queue-apac"},
                ),
            ],
        )
        assert len(rule.table_rows) == 2


# ═══════════════════════════════════════════════════════════════════
# security.py
# ═══════════════════════════════════════════════════════════════════


class TestSecurityProfile:
    def test_role_with_permissions(self):
        role = RoleDefinition(
            id="role-agent", name="Claims Agent",
            permissions=[
                Permission.CASE_CREATE,
                Permission.CASE_READ,
                Permission.CASE_UPDATE,
            ],
        )
        assert Permission.CASE_CREATE in role.permissions

    def test_abac_policy(self):
        policy = AccessPolicy(
            id="pol-1", name="High-value restriction",
            effect=AccessEffect.DENY,
            permissions=[Permission.CASE_UPDATE],
            conditions={
                "case.data.amount": {"$gte": 100000},
                "user.role": "junior-agent",
            },
        )
        assert policy.effect == AccessEffect.DENY

    def test_full_profile(self):
        sp = SecurityProfile(
            id="sp-claims", name="Claims Security",
            roles=[
                RoleDefinition(
                    id="r-1", name="Admin",
                    permissions=[Permission.ADMIN_CONFIGURE],
                ),
            ],
            field_access=[
                FieldLevelAccess(
                    field_id="ssn", role_id="r-1",
                    readable=True, masked=True,
                ),
            ],
        )
        assert sp.field_access[0].masked is True


# ═══════════════════════════════════════════════════════════════════
# application.py
# ═══════════════════════════════════════════════════════════════════


class TestApplicationDefinition:
    def test_application_bundle(self):
        app = ApplicationDefinition(
            id="app-insurance", name="Insurance Claims",
            version="1.0.0",
            case_type_ids=["ct-claims"],
            process_ids=["proc-claims-lifecycle"],
            data_model_ids=["dm-claim"],
            form_ids=["form-intake"],
            rule_ids=["rule-auto-approve", "rule-routing"],
            security_profile_id="sp-claims",
        )
        assert len(app.case_type_ids) == 1
        assert app.default_locale == "en"
        d = dataclasses.asdict(app)
        json.dumps(d)  # must be JSON-serialisable
