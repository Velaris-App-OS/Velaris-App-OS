"""HELIX IR: Case Management Models.

Pure typed dataclasses representing case types, stages, steps,
SLA policies, assignments, work queues, and relationships.
These are the intermediate representation — no runtime behaviour.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


# ─── Enums ────────────────────────────────────────────────────────────


class CaseStatus(enum.Enum):
    """Lifecycle status of a case instance."""

    NEW = "new"
    OPEN = "open"
    PENDING_EXTERNAL = "pending_external"
    PENDING_SUBCASE = "pending_subcase"
    RESOLVED = "resolved"
    CLOSED = "closed"
    REOPENED = "reopened"
    CANCELLED = "cancelled"


class CasePriority(enum.Enum):
    """Priority levels with numeric weights for urgency arithmetic."""

    LOW = 10
    MEDIUM = 20
    HIGH = 30
    CRITICAL = 40
    BLOCKER = 50


class StageType(enum.Enum):
    """Governs how a stage is entered and exited."""

    LINEAR = "linear"
    PARALLEL = "parallel"
    CONDITIONAL = "conditional"
    OPTIONAL = "optional"
    REPEATABLE = "repeatable"


class StepType(enum.Enum):
    """Mirrors BPMN task types so the IR stays aligned with the engine."""

    USER_TASK = "user_task"
    SERVICE_TASK = "service_task"
    SCRIPT_TASK = "script_task"
    SEND_TASK = "send_task"
    MANUAL_TASK = "manual_task"
    SUBPROCESS = "subprocess"
    CALL_ACTIVITY = "call_activity"
    APPROVAL = "approval"


class AssignmentStrategy(enum.Enum):
    """Routing strategies for work items."""

    SPECIFIC_USER = "specific_user"
    ROLE_BASED = "role_based"
    QUEUE_BASED = "queue_based"
    ROUND_ROBIN = "round_robin"
    LEAST_LOADED = "least_loaded"
    SKILL_BASED = "skill_based"
    RULE_BASED = "rule_based"
    MANAGER_OF = "manager_of"
    SELF_SERVICE = "self_service"


class SLAStatus(enum.Enum):
    """Current state of an SLA clock."""

    ON_TRACK = "on_track"
    AT_RISK = "at_risk"
    BREACHED = "breached"
    PAUSED = "paused"


class RelationshipType(enum.Enum):
    """How two case instances relate to each other."""

    PARENT = "parent"
    CHILD = "child"
    RELATED = "related"
    BLOCKING = "blocking"
    BLOCKED_BY = "blocked_by"
    DUPLICATE_OF = "duplicate_of"
    FOLLOW_UP = "follow_up"
    SPLIT_FROM = "split_from"


class EscalationAction(enum.Enum):
    """What to do when an SLA threshold is crossed."""

    REASSIGN = "reassign"
    NOTIFY = "notify"
    CHANGE_PRIORITY = "change_priority"
    INVOKE_RULE = "invoke_rule"
    CREATE_SUBCASE = "create_subcase"


class WorkQueueSortField(enum.Enum):
    """Fields available for work queue ordering."""

    URGENCY = "urgency"
    PRIORITY = "priority"
    CREATED_AT = "created_at"
    SLA_DEADLINE = "sla_deadline"
    UPDATED_AT = "updated_at"
    CUSTOM = "custom"


# ─── SLA & Escalation ────────────────────────────────────────────────


@dataclass(frozen=True)
class SLAEscalation:
    """Action triggered when an SLA threshold is crossed."""

    threshold_percent: float
    action: EscalationAction
    target: str | None = None
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SLAPolicy:
    """Service-level agreement.  Durations are ISO 8601 (e.g. ``PT4H``)."""

    id: str
    name: str
    goal_duration: str
    deadline_duration: str
    business_calendar_id: str | None = None
    pause_on_statuses: list[CaseStatus] = field(
        default_factory=lambda: [CaseStatus.PENDING_EXTERNAL],
    )
    at_risk_threshold: float = 0.8
    escalations: list[SLAEscalation] = field(default_factory=list)


# ─── Steps & Stages ──────────────────────────────────────────────────


@dataclass(frozen=True)
class AssignmentRule:
    """Determines who receives a work item."""

    strategy: AssignmentStrategy
    target: str | None = None
    fallback_strategy: AssignmentStrategy | None = None
    fallback_target: str | None = None
    rule_id: str | None = None
    skill_requirements: list[str] = field(default_factory=list)
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StepDefinition:
    """A single unit of work within a stage.

    Maps 1:1 to a BPMN task element in the lifecycle process.
    """

    id: str
    name: str
    step_type: StepType
    bpmn_element_id: str
    description: str = ""
    assignment: AssignmentRule | None = None
    sla_policy_id: str | None = None
    form_id: str | None = None
    required: bool = True
    repeatable: bool = False
    preconditions: list[str] = field(default_factory=list)
    postconditions: list[str] = field(default_factory=list)
    ui_hints: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StageDefinition:
    """A named phase in the case lifecycle.

    Corresponds to a BPMN sub-process or a bounded region in the
    lifecycle process.
    """

    id: str
    name: str
    stage_type: StageType
    steps: list[StepDefinition] = field(default_factory=list)
    entry_criteria: list[str] = field(default_factory=list)
    exit_criteria: list[str] = field(default_factory=list)
    sla_policy_id: str | None = None
    allowed_actions: list[str] = field(default_factory=list)
    description: str = ""
    order: int = 0


# ─── Work Queues ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class WorkQueueDefinition:
    """A filtered, sorted live view over case assignments.

    Queues do not own work items — they are query definitions.
    """

    id: str
    name: str
    description: str = ""
    filter_criteria: dict[str, Any] = field(default_factory=dict)
    sort_fields: list[WorkQueueSortField] = field(
        default_factory=lambda: [WorkQueueSortField.URGENCY],
    )
    sort_ascending: bool = True
    max_items: int | None = None
    visible_to_roles: list[str] = field(default_factory=list)
    auto_assignment: bool = False
    urgency_formula: str | None = None


# ─── Relationships ────────────────────────────────────────────────────


@dataclass(frozen=True)
class CaseRelationship:
    """Typed link between two case instances."""

    relationship_type: RelationshipType
    target_case_type_id: str | None = None
    target_case_id: str | None = None
    propagate_status: bool = False
    propagate_priority: bool = False
    required: bool = False


# ─── Case Type (design-time template) ────────────────────────────────


@dataclass
class CaseType:
    """Complete definition of a case type.

    ``lifecycle_process_id`` references a ``BPMNProcess`` in the IR.
    The case-service compiles a ``CaseType`` together with its
    ``BPMNProcess`` into a deployable unit.
    """

    id: str
    name: str
    version: str
    lifecycle_process_id: str
    stages: list[StageDefinition] = field(default_factory=list)
    data_model_id: str | None = None
    sla_policies: list[SLAPolicy] = field(default_factory=list)
    work_queues: list[WorkQueueDefinition] = field(default_factory=list)
    allowed_relationships: list[CaseRelationship] = field(default_factory=list)
    default_priority: CasePriority = CasePriority.MEDIUM
    default_assignment: AssignmentRule | None = None
    icon: str | None = None
    color: str | None = None
    tags: list[str] = field(default_factory=list)
    description: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


# ─── Instance-level supporting types ─────────────────────────────────


@dataclass(frozen=True)
class Assignment:
    """A work item assigned to a user, role, or queue."""

    id: str
    step_id: str
    assignee_type: str
    assignee_id: str
    assigned_at: str
    due_at: str | None = None
    completed_at: str | None = None
    status: str = "active"


@dataclass(frozen=True)
class SLASnapshot:
    """Point-in-time SLA state for a case or stage."""

    sla_policy_id: str
    target_id: str
    status: SLAStatus
    started_at: str
    goal_at: str
    deadline_at: str
    paused_duration_seconds: int = 0
    breached_at: str | None = None


@dataclass(frozen=True)
class AuditEntry:
    """Immutable record of a case event."""

    id: str
    timestamp: str
    action: str
    actor_id: str | None = None
    actor_type: str = "user"
    details: dict[str, Any] = field(default_factory=dict)
    previous_value: Any = None
    new_value: Any = None


# ─── Case Instance (runtime snapshot) ────────────────────────────────


@dataclass
class CaseInstance:
    """Serialisable snapshot of a running case.

    The engine hydrates this from the database.  The IR needs to
    represent it for import/export, migration, and audit purposes.
    """

    id: str
    case_type_id: str
    case_type_version: str
    status: CaseStatus
    priority: CasePriority
    current_stage_id: str | None = None
    current_step_ids: list[str] = field(default_factory=list)
    process_instance_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    assignments: list[Assignment] = field(default_factory=list)
    relationships: list[CaseRelationship] = field(default_factory=list)
    sla_snapshots: list[SLASnapshot] = field(default_factory=list)
    audit_trail: list[AuditEntry] = field(default_factory=list)
    created_by: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    resolved_at: str | None = None
    closed_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
