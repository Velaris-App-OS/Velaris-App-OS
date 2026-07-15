"""Case-service database models (async SQLAlchemy ORM).

Maps to the case management schema tables.  Uses the same
patterns as ``engine/helix_engine/db/models.py``.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations
from typing import Any

import json
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    TypeDecorator
)
from sqlalchemy.types import CHAR, TypeEngine
from sqlalchemy.dialects import mysql  # dialect-specific variants for index-key limits (DB SDK)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


# ─── Portable types (work on both PostgreSQL and SQLite) ──────────


class GUID(TypeDecorator):
    """Platform-agnostic UUID type.

    Uses PostgreSQL's native UUID when available, otherwise stores
    as CHAR(36).
    """

    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect) -> TypeEngine:
        if dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import UUID as PG_UUID
            return dialect.type_descriptor(PG_UUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value, dialect):
        if value is None:
            return value
        if dialect.name == "postgresql":
            return value if isinstance(value, uuid.UUID) else uuid.UUID(value)
        return str(value) if isinstance(value, uuid.UUID) else value

    def process_result_value(self, value, dialect):
        if value is None:
            return value
        if not isinstance(value, uuid.UUID):
            return uuid.UUID(str(value))
        return value


class PortableJSON(TypeDecorator):
    """Uses PostgreSQL PortableJSON() when available, generic JSON otherwise."""

    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect) -> TypeEngine:
        if dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import JSONB as PG_JSONB
            return dialect.type_descriptor(PG_JSONB())
        from sqlalchemy import JSON
        return dialect.type_descriptor(JSON())

    def process_bind_param(self, value, dialect):
        if value is None:
            return value
        if dialect.name != "postgresql" and not isinstance(value, str):
            return json.dumps(value, default=str)
        return value

    def process_result_value(self, value, dialect):
        if value is None:
            return value
        if isinstance(value, str):
            return json.loads(value)
        return value


class PortableArray(TypeDecorator):
    """Uses PostgreSQL ARRAY(Text) when available, JSON list otherwise."""

    impl = Text
    cache_ok = True

    def load_dialect_impl(self, dialect) -> TypeEngine:
        if dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import ARRAY
            return dialect.type_descriptor(ARRAY(Text))
        from sqlalchemy import JSON
        return dialect.type_descriptor(JSON())

    def process_bind_param(self, value, dialect):
        if value is None:
            return value
        if dialect.name != "postgresql":
            return json.dumps(value) if not isinstance(value, str) else value
        return value

    def process_result_value(self, value, dialect):
        if value is None:
            return value
        if isinstance(value, str):
            return json.loads(value)
        return value


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_uuid() -> uuid.UUID:
    return uuid.uuid4()


class Base(DeclarativeBase):
    pass


# ═══════════════════════════════════════════════════════════════════════
# DESIGN-TIME TABLES
# ═══════════════════════════════════════════════════════════════════════


class CaseTypeModel(Base):
    __tablename__ = "case_types"
    __table_args__ = (UniqueConstraint("name", "version"),)

    id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=_new_uuid
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("tenants.id"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[str] = mapped_column(String(50), nullable=False)
    lifecycle_process_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    data_model_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), nullable=True
    )
    security_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), nullable=True
    )
    default_priority: Mapped[str] = mapped_column(
        String(20), default="medium"
    )
    definition_json: Mapped[dict] = mapped_column(PortableJSON(), nullable=False)
    icon: Mapped[str | None] = mapped_column(String(100), nullable=True)
    color: Mapped[str | None] = mapped_column(String(7), nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list[str]] = mapped_column(
        PortableArray(), default=list
    )
    portal_enabled: Mapped[bool] = mapped_column(Boolean, default=False)  # P33
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    # Soft-delete (Phase 8)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deleted_by: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )

    # ── Intake trigger (process-case integration) ─────────────────────────────
    # How new case instances are created:
    #   manual   — user action only (default)
    #   webhook  — inbound HTTP POST from external system via connector
    #   schedule — cron-based batch intake
    intake_trigger:       Mapped[str]            = mapped_column(String(20), nullable=False, default="manual")
    trigger_connector_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    # filter_conditions: list of {field, operator, value, logic} dicts
    # Operators: eq, neq, gt, gte, lt, lte, contains, regex
    # Logic: and | or (applied sequentially)
    filter_conditions:    Mapped[dict]            = mapped_column(PortableJSON(), nullable=False, default=dict)
    # field_mapping: {"payload.field.path": "case_field_key"}
    field_mapping:        Mapped[dict]            = mapped_column(PortableJSON(), nullable=False, default=dict)
    # Linked HxFusion process definition — auto-started on case creation
    process_definition_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)

    # Relationships
    stages: Mapped[list[CaseTypeStageModel]] = relationship(
        back_populates="case_type", cascade="all, delete-orphan"
    )


class CaseTypeStageModel(Base):
    __tablename__ = "case_type_stages"
    __table_args__ = (UniqueConstraint("case_type_id", "stage_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=_new_uuid
    )
    case_type_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("case_types.id", ondelete="CASCADE"),
        nullable=False,
    )
    stage_id: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    stage_type: Mapped[str] = mapped_column(String(50), nullable=False)
    order: Mapped[int] = mapped_column(Integer, default=0)
    sla_policy_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    definition_json: Mapped[dict] = mapped_column(PortableJSON(), nullable=False)

    case_type: Mapped[CaseTypeModel] = relationship(
        back_populates="stages"
    )
    steps: Mapped[list[CaseTypeStepModel]] = relationship(
        back_populates="stage", cascade="all, delete-orphan"
    )


class CaseTypeStepModel(Base):
    __tablename__ = "case_type_steps"
    __table_args__ = (UniqueConstraint("case_type_id", "step_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=_new_uuid
    )
    case_type_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("case_types.id", ondelete="CASCADE"),
        nullable=False,
    )
    stage_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("case_type_stages.id", ondelete="CASCADE"),
        nullable=False,
    )
    step_id: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    step_type: Mapped[str] = mapped_column(String(50), nullable=False)
    bpmn_element_id: Mapped[str] = mapped_column(
        String(255), nullable=False
    )
    definition_json: Mapped[dict] = mapped_column(PortableJSON(), nullable=False)

    stage: Mapped[CaseTypeStageModel] = relationship(
        back_populates="steps"
    )


# ═══════════════════════════════════════════════════════════════════════
# RUNTIME TABLES
# ═══════════════════════════════════════════════════════════════════════


class CaseInstanceModel(Base):
    __tablename__ = "case_instances"
    __table_args__ = (
        Index("idx_cases_status", "status"),
        Index("idx_cases_type", "case_type_id"),
        Index("idx_cases_priority", "priority"),
        Index("idx_cases_parent", "parent_case_id"),
        Index("idx_cases_urgency", "urgency_score"),
        Index("idx_cases_created", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=_new_uuid
    )
    case_type_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("case_types.id"),
        nullable=False,
    )
    case_type_version: Mapped[str] = mapped_column(
        String(50), nullable=False
    )
    process_instance_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="new"
    )
    priority: Mapped[str] = mapped_column(
        String(20), nullable=False, default="medium"
    )
    urgency_score: Mapped[float] = mapped_column(Float, default=0.0)
    current_stage_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    parent_case_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(),
        ForeignKey("case_instances.id"),
        nullable=True,
    )
    data: Mapped[dict] = mapped_column(PortableJSON(), default=dict)
    created_by: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    extra_metadata: Mapped[dict] = mapped_column(
        "metadata", PortableJSON(), default=dict
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("tenants.id"), nullable=True, index=True
    )
    # Human-readable identifier — HLX-{TYPE}-{NNNNNN}
    case_number: Mapped[str | None] = mapped_column(String(30), nullable=True, unique=True)
    # P33 Customer Portal
    portal_tracking_token: Mapped[uuid.UUID | None] = mapped_column(GUID(), unique=True, nullable=True)
    portal_submitter_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    portal_submitter_email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Relationships
    assignments: Mapped[list[CaseAssignmentModel]] = relationship(
        back_populates="case_instance", cascade="all, delete-orphan"
    )
    relationships_out: Mapped[list[CaseRelationshipModel]] = relationship(
        foreign_keys="CaseRelationshipModel.source_case_id",
        back_populates="source_case",
        cascade="all, delete-orphan",
    )
    sla_instances: Mapped[list[CaseSLAInstanceModel]] = relationship(
        back_populates="case_instance", cascade="all, delete-orphan"
    )
    audit_entries: Mapped[list[CaseAuditLogModel]] = relationship(
        back_populates="case_instance",
    )


class CaseAssignmentModel(Base):
    __tablename__ = "case_assignments"
    __table_args__ = (
        Index("idx_assignments_assignee", "assignee_type", "assignee_id"),
        Index("idx_assignments_case", "case_id"),
        Index("idx_assignments_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=_new_uuid
    )
    case_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("case_instances.id", ondelete="CASCADE"),
        nullable=False,
    )
    step_id: Mapped[str] = mapped_column(String(255), nullable=False)
    assignee_type: Mapped[str] = mapped_column(
        String(20), nullable=False
    )
    assignee_id: Mapped[str] = mapped_column(
        String(255), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active"
    )
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    assigned_by: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    locked_by: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    locked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lock_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    extra_metadata: Mapped[dict] = mapped_column(
        "metadata", PortableJSON(), default=dict
    )

    case_instance: Mapped[CaseInstanceModel] = relationship(
        back_populates="assignments"
    )


class CaseRelationshipModel(Base):
    __tablename__ = "case_relationships"
    __table_args__ = (
        UniqueConstraint(
            "source_case_id", "target_case_id", "relationship_type"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=_new_uuid
    )
    source_case_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("case_instances.id", ondelete="CASCADE"),
        nullable=False,
    )
    target_case_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("case_instances.id", ondelete="CASCADE"),
        nullable=False,
    )
    relationship_type: Mapped[str] = mapped_column(
        String(30), nullable=False
    )
    propagate_status: Mapped[bool] = mapped_column(Boolean, default=False)
    propagate_priority: Mapped[bool] = mapped_column(
        Boolean, default=False
    )
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )

    source_case: Mapped[CaseInstanceModel] = relationship(
        foreign_keys=[source_case_id], back_populates="relationships_out"
    )


class CaseSLAInstanceModel(Base):
    __tablename__ = "case_sla_instances"
    __table_args__ = (
        Index("idx_sla_case", "case_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=_new_uuid
    )
    case_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("case_instances.id", ondelete="CASCADE"),
        nullable=False,
    )
    sla_policy_id: Mapped[str] = mapped_column(
        String(255), nullable=False
    )
    target_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="on_track"
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    goal_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    deadline_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    paused_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # >>> P34 SLA v2 columns
    pause_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pause_reasons_log: Mapped[list] = mapped_column(PortableJSON(), default=list)
    escalation_level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    escalation_tree_snapshot: Mapped[dict] = mapped_column(PortableJSON(), default=dict)
    escalation_history: Mapped[list] = mapped_column(PortableJSON(), default=list)
    business_calendar_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    # <<< P34 SLA v2 columns
    paused_duration_seconds: Mapped[int] = mapped_column(
        Integer, default=0
    )
    breached_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    extra_metadata: Mapped[dict] = mapped_column(
        "metadata", PortableJSON(), default=dict
    )

    case_instance: Mapped[CaseInstanceModel] = relationship(
        back_populates="sla_instances"
    )


class CaseAuditLogModel(Base):
    __tablename__ = "case_audit_log"
    __table_args__ = (
        Index("idx_audit_case", "case_id", "timestamp"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=_new_uuid
    )
    case_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("case_instances.id"),
        nullable=False,
    )
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    actor_type: Mapped[str] = mapped_column(String(20), default="user")
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    details: Mapped[dict] = mapped_column(PortableJSON(), default=dict)
    previous_value: Mapped[dict | None] = mapped_column(
        PortableJSON(), nullable=True
    )
    new_value: Mapped[dict | None] = mapped_column(PortableJSON(), nullable=True)

    case_instance: Mapped[CaseInstanceModel] = relationship(
        back_populates="audit_entries"
    )


class WorkQueueModel(Base):
    __tablename__ = "work_queues"

    id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=_new_uuid
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("tenants.id"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    filter_criteria: Mapped[dict] = mapped_column(PortableJSON(), default=dict)
    sort_fields: Mapped[list[str]] = mapped_column(
        PortableArray(), default=lambda: ["urgency"]
    )
    sort_ascending: Mapped[bool] = mapped_column(Boolean, default=True)
    visible_to_roles: Mapped[list[str]] = mapped_column(
        PortableArray(), default=list
    )
    auto_assignment: Mapped[bool] = mapped_column(Boolean, default=False)
    urgency_formula: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    max_items: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


# ═══════════════════════════════════════════════════════════════════════
# DESIGN-TIME ARTIFACT STORAGE
# ═══════════════════════════════════════════════════════════════════════


class DataModelModel(Base):
    __tablename__ = "data_models"
    __table_args__ = (UniqueConstraint("name", "version"),)

    id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=_new_uuid
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[str] = mapped_column(String(50), nullable=False)
    definition_json: Mapped[dict] = mapped_column(PortableJSON(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class FormDefinitionModel(Base):
    __tablename__ = "form_definitions"
    __table_args__ = (UniqueConstraint("name", "version"),)

    id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=_new_uuid
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[str] = mapped_column(String(50), nullable=False)
    data_model_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(),
        ForeignKey("data_models.id"),
        nullable=True,
    )
    definition_json: Mapped[dict] = mapped_column(PortableJSON(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class RuleDefinitionModel(Base):
    __tablename__ = "rule_definitions"
    __table_args__ = (UniqueConstraint("name", "version"),)

    id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=_new_uuid
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[str] = mapped_column(String(50), nullable=False)
    rule_type: Mapped[str] = mapped_column(String(50), nullable=False)
    scope: Mapped[str] = mapped_column(String(30), default="global")
    scope_target_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    definition_json: Mapped[dict] = mapped_column(PortableJSON(), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class BusinessCalendarModel(Base):
    """Business calendar for SLA calculations (Phase 8)."""
    __tablename__ = "business_calendars"

    id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=_new_uuid
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    timezone: Mapped[str] = mapped_column(String(100), default="UTC")
    work_days: Mapped[list] = mapped_column(PortableJSON(), default=lambda: [1,2,3,4,5])
    work_start_hour: Mapped[int] = mapped_column(Integer, default=9)
    work_end_hour: Mapped[int] = mapped_column(Integer, default=17)
    holidays: Mapped[list] = mapped_column(PortableJSON(), default=list)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class WebhookSubscriptionModel(Base):
    """Webhook subscription for case events (Phase 10)."""
    __tablename__ = "webhook_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    secret: Mapped[str | None] = mapped_column(String(255), nullable=True)
    events: Mapped[list[str]] = mapped_column(PortableArray(), default=list)
    case_type_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("case_types.id"), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    headers: Mapped[dict] = mapped_column(PortableJSON(), default=dict)
    retry_count: Mapped[int] = mapped_column(Integer, default=3)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=10)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class WebhookDeliveryModel(Base):
    """Webhook delivery attempt log (Phase 10)."""
    __tablename__ = "webhook_deliveries"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("webhook_subscriptions.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict] = mapped_column(PortableJSON(), nullable=False)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class CaseEventLogModel(Base):
    """Process mining event log (Phase 14).

    Captures every significant event in case lifecycle for
    downstream analysis: bottlenecks, variants, conformance.
    """
    __tablename__ = "case_event_log"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    case_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("case_instances.id", ondelete="CASCADE"), nullable=False
    )
    case_type_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("case_types.id"), nullable=False
    )
    activity: Mapped[str] = mapped_column(String(255), nullable=False)
    activity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    stage_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    step_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    actor_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    actor_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(50), nullable=True)
    extra_metadata: Mapped[dict] = mapped_column("metadata", PortableJSON(), default=dict)


class MigrationScanModel(Base):
    """Scout migration scan record (Phase 16)."""
    __tablename__ = "migration_scans"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_platform: Mapped[str] = mapped_column(String(50), nullable=False)
    source_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    filename: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    compatibility_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    effort_weeks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    artifacts_found: Mapped[dict] = mapped_column(PortableJSON(), default=dict)
    scan_report: Mapped[dict] = mapped_column(PortableJSON(), default=dict)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TenantModel(Base):
    """Tenant — organizational isolation boundary (Phase 17)."""
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default="active")
    settings: Mapped[dict] = mapped_column(PortableJSON(), default=dict)
    max_cases: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_users: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class TenantMembershipModel(Base):
    """User-tenant membership with role (Phase 17)."""
    __tablename__ = "tenant_memberships"
    __table_args__ = (UniqueConstraint("tenant_id", "user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(50), default="member")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class TenantDekModel(Base):
    """HxVault (#19) — per-tenant Data Encryption Key (envelope encryption).

    Holds a random 256-bit DEK wrapped (AES-256-GCM) under the master KEK.
    tenant_id NULL = the platform DEK (tenantless/HxFusion case data).
    Crypto-shredding: status='shredded' + wrapped_dek cleared → data encrypted
    under this DEK is permanently unrecoverable (GDPR Art-17).
    """
    __tablename__ = "tenant_deks"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True, unique=True)
    key_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    wrapped_dek: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class TestSuiteModel(Base):
    """Test Suite (#27) — a named collection of test-case definitions (DSL)."""
    __test__ = False  # not a pytest test class
    __tablename__ = "hxtest_suites"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # platform | component | security | conformance | generated
    suite_type: Mapped[str] = mapped_column(String(30), nullable=False)
    # builtin | ai_generated | developer | structural
    source: Mapped[str] = mapped_column(String(20), default="builtin", nullable=False)
    case_type_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    definition: Mapped[list] = mapped_column(PortableJSON(), default=list)
    version: Mapped[str] = mapped_column(String(40), default="1.0.0", nullable=False)
    # AI scenarios are stale vs the current case-type definition/rules/integrations
    # (set by the regen hooks; cleared when AI scenarios are regenerated). #27 Part B.
    ai_stale: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class TestRunModel(Base):
    """Test Suite (#27) — one execution of a suite (or 'all')."""
    __test__ = False  # not a pytest test class
    __tablename__ = "hxtest_runs"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    suite_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    suite_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    triggered_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tenant_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # running | passed | failed | partial | error
    status: Mapped[str] = mapped_column(String(20), default="running", nullable=False)
    total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    passed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    app_package_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    ephemeral_tenant_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TestResultModel(Base):
    """Test Suite (#27) — per-test result within a run."""
    __test__ = False  # not a pytest test class
    __tablename__ = "hxtest_results"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    run_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    test_id: Mapped[str] = mapped_column(String(200), nullable=False)
    test_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # passed | failed | skipped | error
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    step_results: Mapped[list] = mapped_column(PortableJSON(), default=list)
    ran_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ArtifactAnalysisModel(Base):
    """AI-generated analysis of a migration artifact (Phase 19)."""
    __tablename__ = "artifact_analyses"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    scan_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("migration_scans.id", ondelete="CASCADE"), nullable=True
    )
    artifact_identifier: Mapped[str] = mapped_column(String(500), nullable=False)
    artifact_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    source_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    business_logic: Mapped[str | None] = mapped_column(Text, nullable=True)
    complexity: Mapped[str | None] = mapped_column(String(20), nullable=True)
    external_calls: Mapped[list] = mapped_column(PortableJSON(), default=list)
    data_reads: Mapped[list] = mapped_column(PortableJSON(), default=list)
    data_writes: Mapped[list] = mapped_column(PortableJSON(), default=list)
    side_effects: Mapped[list] = mapped_column(PortableJSON(), default=list)
    helix_mapping: Mapped[dict] = mapped_column(PortableJSON(), default=dict)
    generated_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    ai_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class SecurityEventModel(Base):
    """Security event log (Phase 20)."""
    __tablename__ = "security_events"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), default="info")
    user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    resource_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    action: Mapped[str | None] = mapped_column(String(100), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(20), nullable=True)
    details: Mapped[dict] = mapped_column(PortableJSON(), default=dict)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class RetentionPolicyModel(Base):
    """Data retention policy (Phase 20)."""
    __tablename__ = "retention_policies"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    retention_days: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(20), default="archive")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class GDPRRequestModel(Base):
    """GDPR data subject request (Phase 20)."""
    __tablename__ = "gdpr_requests"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    subject_id: Mapped[str] = mapped_column(String(255), nullable=False)
    request_type: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    requested_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_file: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class MigrationProjectModel(Base):
    """Migration project — tracks a full app migration end-to-end (Phase 21)."""
    __tablename__ = "migration_projects"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_platform: Mapped[str | None] = mapped_column(String(50), nullable=True)
    scan_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("migration_scans.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(30), default="draft")
    total_artifacts: Mapped[int] = mapped_column(Integer, default=0)
    analyzed_count: Mapped[int] = mapped_column(Integer, default=0)
    generated_count: Mapped[int] = mapped_column(Integer, default=0)
    ported_count: Mapped[int] = mapped_column(Integer, default=0)
    roadmap: Mapped[dict] = mapped_column(PortableJSON(), default=dict)
    dependencies: Mapped[dict] = mapped_column(PortableJSON(), default=dict)
    settings: Mapped[dict] = mapped_column(PortableJSON(), default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class MigrationTaskModel(Base):
    """Individual migration task for one artifact (Phase 21)."""
    __tablename__ = "migration_tasks"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    project_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("migration_projects.id", ondelete="CASCADE"), nullable=False
    )
    artifact_id: Mapped[str] = mapped_column(String(500), nullable=False)
    artifact_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    artifact_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phase: Mapped[int] = mapped_column(Integer, default=1)
    sequence: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    depends_on: Mapped[list] = mapped_column(PortableJSON(), default=list)
    analysis_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("artifact_analyses.id", ondelete="SET NULL"), nullable=True
    )
    generated_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    complexity: Mapped[str | None] = mapped_column(String(20), nullable=True)
    estimated_hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    actual_hours: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


# >>> P23 observability models
class TelemetryEventModel(Base):
    __tablename__ = "telemetry_events"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="INFO", index=True)
    payload: Mapped[dict] = mapped_column(PortableJSON(), nullable=False, default=dict)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, index=True,
    )


class HealthCheckResultModel(Base):
    __tablename__ = "health_check_results"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    component: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    detail: Mapped[dict] = mapped_column(PortableJSON(), nullable=False, default=dict)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, index=True,
    )
# <<< P23 observability models


# >>> P24 document models
class DocumentModel(Base):
    __tablename__ = "documents"
    __table_args__ = (
        Index("idx_documents_case", "case_id"),
        Index("idx_documents_tenant", "tenant_id"),
        Index("idx_documents_deleted", "is_deleted"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    case_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("case_instances.id"), nullable=False,
    )
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False, default="application/octet-stream")
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    uploaded_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tags: Mapped[dict] = mapped_column(PortableJSON(), default=dict)
    ocr_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # P39b — customer portal sharing
    portal_visible: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    portal_source: Mapped[str | None] = mapped_column(String(20), nullable=True)  # 'staff' | 'customer'

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False,
    )

    versions: Mapped[list["DocumentVersionModel"]] = relationship(
        "DocumentVersionModel", back_populates="document",
        cascade="all, delete-orphan", lazy="selectin",
    )


class DocumentVersionModel(Base):
    __tablename__ = "document_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "version", name="uq_document_version"),
        Index("idx_document_versions_doc", "document_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    document_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    uploaded_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    document: Mapped["DocumentModel"] = relationship(
        "DocumentModel", back_populates="versions",
    )
# <<< P24 document models


# >>> P34 escalation
class EscalationTreeModel(Base):
    """Escalation tree — global (scope='global') or case-type scoped."""
    __tablename__ = "escalation_trees"
    __table_args__ = (
        Index("idx_escalation_trees_scope", "scope"),
        Index("idx_escalation_trees_case_type", "case_type_id"),
        Index("idx_escalation_trees_tenant", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    scope: Mapped[str] = mapped_column(String(32), nullable=False, default="global")
    # scope: "global" | "case_type"
    case_type_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("case_types.id"), nullable=True,
    )
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Tree data — list of levels, each level has trigger + actions
    # See docs/escalation-tree-schema.md for canonical format
    tree_json: Mapped[dict] = mapped_column(PortableJSON(), default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False,
    )
# <<< P34 escalation


# >>> P34b user directory
class UserDirectoryModel(Base):
    """User directory — maps user IDs to managers, groups, roles, metadata.

    Populated via SSO sync or manual admin CRUD. Used by escalation engine
    to resolve dynamic targets (manager_of, access_group, role).
    """
    __tablename__ = "user_directory"
    __table_args__ = (
        Index("idx_user_directory_manager", "manager_user_id"),
        Index("idx_user_directory_tenant", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    manager_user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    access_group_ids: Mapped[list] = mapped_column(PortableJSON(), default=list)  # deprecated — use operator_access_groups
    roles: Mapped[list] = mapped_column(PortableJSON(), default=list)             # deprecated — use access_roles via group
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    metadata_json: Mapped[dict] = mapped_column(PortableJSON(), default=dict)
    # P37 — persists the operator's active access group across sessions
    current_access_group_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
    )
# <<< P34b user directory


# >>> P36 compliance
class AuditChainModel(Base):
    """Hash-chain seal over case_audit_log entries.

    Each row references one audit log entry and stores:
      - prev_hash: content_hash of previous chain row (or "0"*64 for genesis)
      - content_hash: sha256(prev_hash || canonical(audit_row))

    Tampering with case_audit_log is detectable by walking the chain.
    """
    __tablename__ = "case_audit_log_chain"
    __table_args__ = (
        Index("idx_audit_chain_seq", "sequence"),
        Index("idx_audit_chain_audit_id", "audit_log_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    audit_log_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    sealed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False, index=True,
    )


class AuditAnchorModel(Base):
    """RFC-3161 timestamp receipt over an audit-chain tip (Group I).

    A timestamp authority signed sha256(tip_hash) at anchored_at, proving the
    chain existed in this state at that time — even a DB admin who rewrites
    the whole chain cannot forge history older than the latest anchor.
    tsr_der holds the raw TimeStampResp; verify offline with
    `openssl ts -verify`.
    """
    __tablename__ = "audit_anchors"
    __table_args__ = (
        Index("idx_audit_anchors_tip_seq", "tip_sequence"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tip_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    tip_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    tsa_url: Mapped[str] = mapped_column(String(512), nullable=False)
    tsr_der: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    anchored_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False, index=True,
    )


class DataLineageEventModel(Base):
    """Denormalized lineage events for case-data hot-path changes.

    Other lineage (assignments, documents, escalations) is derived from
    existing audit log on demand. This table only holds the high-frequency
    case-data mutation events for fast reads.
    """
    __tablename__ = "data_lineage_events"
    __table_args__ = (
        Index("idx_lineage_case", "case_id"),
        Index("idx_lineage_at", "at"),
        Index("idx_lineage_kind", "kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    case_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    field_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    before_value: Mapped[Any] = mapped_column(PortableJSON(), nullable=True)
    after_value: Mapped[Any] = mapped_column(PortableJSON(), nullable=True)
    actor_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="api")
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ComplianceReportModel(Base):
    """Generated compliance report record (artifact + metadata)."""
    __tablename__ = "compliance_reports"
    __table_args__ = (
        Index("idx_compliance_framework", "framework"),
        Index("idx_compliance_generated_at", "generated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    framework: Mapped[str] = mapped_column(String(32), nullable=False)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    generated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )
    summary: Mapped[dict] = mapped_column(PortableJSON(), default=dict)
    storage_key_json: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    storage_key_pdf: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    chain_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    cadence: Mapped[str] = mapped_column(String(16), nullable=False, default="on_demand")
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
# <<< P36 compliance


# >>> P25 email
class EmailAccountModel(Base):
    """SMTP/IMAP credentials for an email mailbox.

    NOTE: For production, encrypt password fields via KMS/Vault. Stored plain
    in dev mode.
    """
    __tablename__ = "email_accounts"
    __table_args__ = (
        Index("idx_email_accounts_active", "is_active"),
        Index("idx_email_accounts_tenant", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    address: Mapped[str] = mapped_column(String(320), nullable=False)
    smtp_host: Mapped[str] = mapped_column(String(255), nullable=False)
    smtp_port: Mapped[int] = mapped_column(Integer, default=587)
    smtp_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    smtp_password: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    smtp_use_tls: Mapped[bool] = mapped_column(Boolean, default=True)
    imap_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    imap_port: Mapped[int] = mapped_column(Integer, default=993)
    imap_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    imap_password: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    imap_use_ssl: Mapped[bool] = mapped_column(Boolean, default=True)
    imap_folder: Mapped[str] = mapped_column(String(255), default="INBOX")
    poll_interval_seconds: Mapped[int] = mapped_column(Integer, default=15)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default_outbound: Mapped[bool] = mapped_column(Boolean, default=False)
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
    )


class EmailTemplateModel(Base):
    """Reusable email body template (Jinja2 or f-string)."""
    __tablename__ = "email_templates"
    __table_args__ = (
        Index("idx_email_templates_scope", "scope"),
        Index("idx_email_templates_case_type", "case_type_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    subject: Mapped[str] = mapped_column(String(998), nullable=False)
    body_text: Mapped[str] = mapped_column(Text, nullable=False)
    body_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    engine: Mapped[str] = mapped_column(String(16), default="jinja2")
    scope: Mapped[str] = mapped_column(String(32), default="global")
    case_type_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("case_types.id"), nullable=True,
    )
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
    )


class EmailMessageModel(Base):
    """An email message (inbound or outbound) linked to a case."""
    __tablename__ = "email_messages"
    __table_args__ = (
        Index("idx_email_messages_case", "case_id"),
        Index("idx_email_messages_msgid", "message_id", mysql_length={"message_id": 255}),
        Index("idx_email_messages_direction", "direction"),
        Index("idx_email_messages_status", "status"),
        Index("idx_email_messages_received", "received_at"),
        Index("idx_email_messages_unread", "is_read"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    case_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    account_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    message_id: Mapped[str | None] = mapped_column(String(998), nullable=True)
    in_reply_to: Mapped[str | None] = mapped_column(String(998), nullable=True)
    references: Mapped[list] = mapped_column(PortableJSON(), default=list)
    from_address: Mapped[str] = mapped_column(String(320), default="")
    to_addresses: Mapped[list] = mapped_column(PortableJSON(), default=list)
    cc_addresses: Mapped[list] = mapped_column(PortableJSON(), default=list)
    subject: Mapped[str] = mapped_column(Text, default="")
    body_text: Mapped[str] = mapped_column(Text, default="")
    body_html: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_headers: Mapped[dict] = mapped_column(PortableJSON(), default=dict)
    status: Mapped[str] = mapped_column(String(32), default="received")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
# <<< P25 email


# >>> P27 push notifications
class DeviceTokenModel(Base):
    """A registered push notification device token for a user."""
    __tablename__ = "push_device_tokens"
    __table_args__ = (
        Index("idx_push_device_user", "user_id"),
        Index("idx_push_device_channel", "channel"),
        Index("idx_push_device_active", "is_active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)       # fcm | apns | webpush
    token: Mapped[str] = mapped_column(Text, nullable=False)               # full token — never log
    platform: Mapped[str | None] = mapped_column(String(64), nullable=True)   # android | ios | web
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class NotificationPreferenceModel(Base):
    """Per-user notification preferences for a given event type."""
    __tablename__ = "notification_preferences"
    __table_args__ = (
        Index("idx_notif_pref_user_event", "user_id", "event_type", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    channels: Mapped[list] = mapped_column(PortableJSON(), default=list)   # ["fcm","webpush"]
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
    )


class CaseTypeNotificationOverrideModel(Base):
    """Per-case-type notification channel overrides (highest priority)."""
    __tablename__ = "case_type_notification_overrides"
    __table_args__ = (
        Index("idx_ctno_case_type_event", "case_type_id", "event_type", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    case_type_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("case_types.id"), nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    channels: Mapped[list] = mapped_column(PortableJSON(), default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
    )


class NotificationLogModel(Base):
    """Immutable delivery log for every push attempt."""
    __tablename__ = "notification_logs"
    __table_args__ = (
        Index("idx_notif_log_device", "device_id"),
        Index("idx_notif_log_user", "user_id"),
        Index("idx_notif_log_status", "status"),
        Index("idx_notif_log_sent", "sent_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    device_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)        # delivered | failed
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
# <<< P27 push notifications


# >>> P30 HxNexus AI Copilot
class DocumentChunkModel(Base):
    """A text chunk from a document, with embedding for RAG retrieval."""
    __tablename__ = "hxnexus_document_chunks"
    __table_args__ = (
        Index("idx_hxnexus_chunk_document", "document_id"),
        Index("idx_hxnexus_chunk_case", "case_id"),
        Index("idx_hxnexus_chunk_tenant", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    document_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    case_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    chunk_index: Mapped[int] = mapped_column(Integer, default=0)
    chunk_text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list] = mapped_column(PortableJSON(), default=list)  # list[float]
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class CopilotConversationModel(Base):
    """A HxNexus chat conversation session."""
    __tablename__ = "hxnexus_conversations"
    __table_args__ = (
        Index("idx_hxnexus_conv_user", "user_id"),
        Index("idx_hxnexus_conv_case", "case_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    case_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
    )


class CopilotMessageModel(Base):
    """A single message in a HxNexus conversation."""
    __tablename__ = "hxnexus_messages"
    __table_args__ = (
        Index("idx_hxnexus_msg_conv", "conversation_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("hxnexus_conversations.id"), nullable=False,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)   # user | assistant
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
# <<< P30 HxNexus AI Copilot


# >>> P37 Operator & Access Group Model

class PortalModel(Base):
    """Named portal configuration — each access group has exactly one portal.

    portal_type controls which UI shell is rendered for operators in that group:
      staff    → full Helix Studio
      manager  → analytics + reporting focused
      admin    → system administration console
      customer → external customer self-service (public portal)
      mobile   → mobile-optimised shell (future)
    """
    __tablename__ = "portals"
    __table_args__ = (
        Index("idx_portals_tenant", "tenant_id"),
        Index("idx_portals_type", "portal_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    portal_type: Mapped[str] = mapped_column(String(50), nullable=False, default="staff")
    modules: Mapped[list] = mapped_column(PortableJSON(), default=list)
    homepage: Mapped[str] = mapped_column(String(100), nullable=False, default="/work-center")
    theme: Mapped[dict] = mapped_column(PortableJSON(), default=dict)
    tenant_id: Mapped[str | None] = mapped_column(String(255), nullable=True)  # NULL = system-wide
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
    )

    access_groups: Mapped[list["AccessGroupModel"]] = relationship(back_populates="portal")


class AccessRoleModel(Base):
    """Named role definition with a privilege set.

    Privileges JSONB matches the format consumed by core/access_control.py
    evaluate_access() — no separate evaluator needed:
      [{"resource": "case", "case_type_id": "*", "actions": ["create", "read"]}]

    Seeded role names ("admin", "staff", "viewer") match existing require_role()
    strings so AuthenticatedUser.roles keeps working without changes to routes.
    """
    __tablename__ = "access_roles"
    __table_args__ = (
        UniqueConstraint("name", "tenant_id", name="access_roles_name_tenant_uq"),
        Index("idx_access_roles_tenant", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    privileges: Mapped[list] = mapped_column(PortableJSON(), default=list)
    tenant_id: Mapped[str | None] = mapped_column(String(255), nullable=True)  # NULL = built-in
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
    )


class AccessGroupModel(Base):
    """Access group — the Pega equivalent binding: portal + roles + case/queue scope.

    One operator can belong to many access groups (via operator_access_groups).
    Switching active group changes the operator's portal + permissions.
    """
    __tablename__ = "access_groups"
    __table_args__ = (
        UniqueConstraint("name", "tenant_id", name="access_groups_name_tenant_uq"),
        Index("idx_access_groups_tenant", "tenant_id"),
        Index("idx_access_groups_portal", "portal_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    tenant_id: Mapped[str] = mapped_column(String(255), nullable=False)
    portal_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("portals.id", ondelete="RESTRICT"), nullable=False,
    )
    role_ids: Mapped[list] = mapped_column(PortableJSON(), default=list)
    allowed_case_type_ids: Mapped[list] = mapped_column(PortableJSON(), default=lambda: ["*"])
    allowed_queue_ids: Mapped[list] = mapped_column(PortableJSON(), default=lambda: ["*"])
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
    )

    portal: Mapped[PortalModel] = relationship(back_populates="access_groups")
    members: Mapped[list["OperatorAccessGroupModel"]] = relationship(
        back_populates="access_group", cascade="all, delete-orphan",
    )


class OperatorAccessGroupModel(Base):
    """Many-to-many: operator ↔ access groups.

    is_primary = True marks the startup default for this operator.
    Only one primary row per operator_id should exist (enforced by the
    switch-context endpoint, not by a DB constraint, to allow bulk import).
    """
    __tablename__ = "operator_access_groups"
    __table_args__ = (
        UniqueConstraint("operator_id", "access_group_id", name="oag_operator_group_uq"),
        Index("idx_oag_operator", "operator_id"),
        Index("idx_oag_access_group", "access_group_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    operator_id: Mapped[str] = mapped_column(String(255), nullable=False)
    access_group_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("access_groups.id", ondelete="CASCADE"), nullable=False,
    )
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    assigned_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    access_group: Mapped[AccessGroupModel] = relationship(back_populates="members")

# <<< P37 Operator & Access Group Model


# >>> P38 Step Completions

class CaseStepCompletionModel(Base):
    """Records a completed (or rejected) step within a case's current stage.

    step_id is the logical ID from definition_json — survives case type versioning.
    Unique per (case_id, step_id): second submit upserts.
    """
    __tablename__ = "case_step_completions"
    __table_args__ = (
        UniqueConstraint("case_id", "step_id", name="csc_case_step_uq"),
        Index("idx_csc_case_id", "case_id"),
        Index("idx_csc_stage_id", "case_id", "stage_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    case_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("case_instances.id", ondelete="CASCADE"), nullable=False,
    )
    stage_id: Mapped[str] = mapped_column(String(255), nullable=False)
    step_id: Mapped[str] = mapped_column(String(255), nullable=False)
    step_type: Mapped[str] = mapped_column(String(50), nullable=False, default="user_task")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="completed")
    data: Mapped[dict] = mapped_column(PortableJSON(), default=dict)
    completed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

# <<< P38 Step Completions


# >>> P46 HxStream — Live Execution & Interaction Stream

class TraceEventModel(Base):
    """One observable event in the HxStream live feed.

    Covers both backend lifecycle events (stage transitions, AI calls, etc.)
    and frontend UI interactions (clicks, form opens, context switches).
    event_type discriminates the payload shape.
    """

    __tablename__ = "trace_events"
    __table_args__ = (
        Index("ix_trace_events_case_id",  "case_id",      "occurred_at"),
        Index("ix_trace_events_tenant",   "tenant_id",    "occurred_at"),
        Index("ix_trace_events_type",     "event_type",   "occurred_at"),
        Index("ix_trace_events_session",  "session_id",   "occurred_at"),
        Index("ix_trace_events_actor",    "actor_user_id","occurred_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    case_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("case_instances.id", ondelete="SET NULL"), nullable=True,
    )
    tenant_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    actor_user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    actor_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    payload: Mapped[dict] = mapped_column(PortableJSON(), default=dict)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False,
    )
    session_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

# <<< P46 HxStream


# >>> P41 HxGraph — Helix Native Knowledge Graph

class GraphNodeModel(Base):
    """One node in the HxGraph knowledge graph.

    Covers every platform concept: case types, stages, steps, forms, fields,
    modules, endpoints, connectors, concepts (HxNexus-created), and patterns
    (derived from HxStream runtime data).
    """

    __tablename__ = "graph_nodes"
    __table_args__ = (
        Index("ix_graph_nodes_type",      "node_type"),
        Index("ix_graph_nodes_name",      "name"),
        Index("ix_graph_nodes_community", "community_id"),
        Index("ix_graph_nodes_tenant",    "tenant_id"),
        Index("ix_graph_nodes_source",    "source"),
    )

    id:             Mapped[uuid.UUID]      = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    node_type:      Mapped[str]            = mapped_column(String(50), nullable=False)
    name:           Mapped[str]            = mapped_column(String(500), nullable=False)
    label:          Mapped[str]            = mapped_column(String(500), nullable=False)
    source:         Mapped[str]            = mapped_column(String(20), nullable=False, default="db")
    properties:     Mapped[dict]           = mapped_column(PortableJSON(), default=dict)
    summary:        Mapped[str | None]     = mapped_column(Text, nullable=True)
    embedding:      Mapped[list | None]    = mapped_column(PortableJSON(), nullable=True)
    community_id:   Mapped[int | None]     = mapped_column(Integer, nullable=True)
    tenant_id:      Mapped[str | None]     = mapped_column(String(255), nullable=True)
    last_synced_at: Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow)
    created_at:     Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow)

    outgoing_edges: Mapped[list["GraphEdgeModel"]] = relationship(
        "GraphEdgeModel", foreign_keys="GraphEdgeModel.from_node_id",
        back_populates="from_node", cascade="all, delete-orphan",
    )
    incoming_edges: Mapped[list["GraphEdgeModel"]] = relationship(
        "GraphEdgeModel", foreign_keys="GraphEdgeModel.to_node_id",
        back_populates="to_node",
    )


class GraphEdgeModel(Base):
    """A directed edge between two graph nodes."""

    __tablename__ = "graph_edges"
    __table_args__ = (
        Index("ix_graph_edges_from", "from_node_id"),
        Index("ix_graph_edges_to",   "to_node_id"),
        Index("ix_graph_edges_type", "edge_type"),
        UniqueConstraint("from_node_id", "to_node_id", "edge_type", name="uq_graph_edges"),
    )

    id:           Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    from_node_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("graph_nodes.id", ondelete="CASCADE"), nullable=False,
    )
    to_node_id:   Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("graph_nodes.id", ondelete="CASCADE"), nullable=False,
    )
    edge_type:    Mapped[str]       = mapped_column(String(50), nullable=False)
    weight:       Mapped[float]     = mapped_column(Float, nullable=False, default=1.0)
    properties:   Mapped[dict]      = mapped_column(PortableJSON(), default=dict)
    created_at:   Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=_utcnow)

    from_node: Mapped["GraphNodeModel"] = relationship(
        "GraphNodeModel", foreign_keys=[from_node_id], back_populates="outgoing_edges",
    )
    to_node: Mapped["GraphNodeModel"] = relationship(
        "GraphNodeModel", foreign_keys=[to_node_id], back_populates="incoming_edges",
    )

# <<< P41 HxGraph


# >>> P42 HxNexus Polyglot Intelligence

class BpmConceptModel(Base):
    """One BPM concept → Helix mapping in the polyglot knowledge base."""

    __tablename__ = "bpm_concepts"
    __table_args__ = (
        Index("ix_bpm_concepts_tool",       "source_tool"),
        Index("ix_bpm_concepts_concept",    "source_concept"),
        Index("ix_bpm_concepts_confidence", "confidence"),
    )

    id:              Mapped[uuid.UUID]     = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    source_tool:     Mapped[str]           = mapped_column(String(50),  nullable=False)
    source_concept:  Mapped[str]           = mapped_column(String(255), nullable=False)
    helix_equiv:     Mapped[str]           = mapped_column(String(255), nullable=False)
    helix_node_type: Mapped[str | None]    = mapped_column(String(50),  nullable=True)
    description:     Mapped[str]           = mapped_column(Text,        nullable=False)
    example:         Mapped[str | None]    = mapped_column(Text,        nullable=True)
    confidence:      Mapped[str]           = mapped_column(String(10),  nullable=False, default="exact")
    notes:           Mapped[str | None]    = mapped_column(Text,        nullable=True)
    created_at:      Mapped[datetime]      = mapped_column(DateTime(timezone=True), default=_utcnow)


class GeneratedDocModel(Base):
    """Cached AI-generated documentation (business guide / developer guide)."""

    __tablename__ = "generated_docs"

    id:           Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    doc_type:     Mapped[str]       = mapped_column(String(50), nullable=False, unique=True)
    content:      Mapped[str]       = mapped_column(Text,       nullable=False)
    generated_at: Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=_utcnow)
    node_count:   Mapped[int]       = mapped_column(Integer,    nullable=False, default=0)

# <<< P42 HxNexus Polyglot


# >>> P43 App Registry

class AppPackageModel(Base):
    """A versioned snapshot of the full Helix platform state."""

    __tablename__ = "app_packages"
    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_app_packages_name_version"),
        Index("ix_app_packages_status",  "status"),
        Index("ix_app_packages_created", "created_at"),
    )

    id:          Mapped[uuid.UUID]     = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    name:        Mapped[str]           = mapped_column(String(255), nullable=False)
    version:     Mapped[str]           = mapped_column(String(50),  nullable=False)
    description: Mapped[str | None]    = mapped_column(Text, nullable=True)
    bundle:      Mapped[dict]          = mapped_column(PortableJSON(), default=dict)
    manifest:    Mapped[dict]          = mapped_column(PortableJSON(), default=dict)
    status:      Mapped[str]           = mapped_column(String(20), nullable=False, default="draft")
    created_by:  Mapped[str | None]    = mapped_column(String(255), nullable=True)
    created_at:  Mapped[datetime]      = mapped_column(DateTime(timezone=True), default=_utcnow)

    deployments: Mapped[list["AppDeploymentModel"]] = relationship(
        "AppDeploymentModel", back_populates="package", cascade="all, delete-orphan",
    )


class AppDeploymentModel(Base):
    """One promotion of an app package to an environment."""

    __tablename__ = "app_deployments"
    __table_args__ = (
        Index("ix_app_deployments_package",     "package_id"),
        Index("ix_app_deployments_environment", "environment"),
        Index("ix_app_deployments_deployed",    "deployed_at"),
    )

    id:               Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    package_id:       Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("app_packages.id", ondelete="CASCADE"), nullable=False,
    )
    environment:      Mapped[str]       = mapped_column(String(50),  nullable=False)
    status:           Mapped[str]       = mapped_column(String(20),  nullable=False, default="deployed")
    deployed_by:      Mapped[str | None]= mapped_column(String(255), nullable=True)
    deployed_at:      Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=_utcnow)
    notes:            Mapped[str | None]= mapped_column(Text, nullable=True)
    config_overrides: Mapped[dict]      = mapped_column(PortableJSON(), default=dict)

    package: Mapped["AppPackageModel"] = relationship("AppPackageModel", back_populates="deployments")

# <<< P43 App Registry


# >>> P44 BPM Importer

class ImportJobModel(Base):
    """Tracks one five-pass BPM import pipeline job."""

    __tablename__ = "import_jobs"
    __table_args__ = (
        Index("ix_import_jobs_status",  "status"),
        Index("ix_import_jobs_tool",    "tool"),
        Index("ix_import_jobs_created", "created_at"),
    )

    id:           Mapped[uuid.UUID]     = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tool:         Mapped[str]           = mapped_column(String(50),  nullable=False)
    filename:     Mapped[str]           = mapped_column(String(500), nullable=False)
    status:       Mapped[str]           = mapped_column(String(30),  nullable=False, default="pending")
    pass1_result: Mapped[dict]          = mapped_column(PortableJSON(), default=dict)
    pass2_result: Mapped[dict]          = mapped_column(PortableJSON(), default=dict)
    pass3_result: Mapped[dict]          = mapped_column(PortableJSON(), default=dict)
    pass4_result: Mapped[dict]          = mapped_column(PortableJSON(), default=dict)
    report:       Mapped[dict]          = mapped_column(PortableJSON(), default=dict)
    error:        Mapped[str | None]    = mapped_column(Text,        nullable=True)
    created_by:   Mapped[str | None]    = mapped_column(String(255), nullable=True)
    created_at:   Mapped[datetime]      = mapped_column(DateTime(timezone=True), default=_utcnow)
    completed_at: Mapped[datetime|None] = mapped_column(DateTime(timezone=True), nullable=True)

# <<< P44 BPM Importer


# >>> P28 HxBridge — Connector Protocol Foundation

class ConnectorRegistryModel(Base):
    """A registered connector — config + encrypted credentials."""

    __tablename__ = "connector_registry"
    __table_args__ = (
        UniqueConstraint("name", "tenant_id", name="uq_connector_name_tenant"),
        Index("ix_connector_type",    "connector_type"),
        Index("ix_connector_tenant",  "tenant_id"),
        Index("ix_connector_enabled", "enabled"),
    )

    id:             Mapped[uuid.UUID]     = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    name:           Mapped[str]           = mapped_column(String(255), nullable=False)
    connector_type: Mapped[str]           = mapped_column(String(100), nullable=False)
    description:    Mapped[str | None]    = mapped_column(Text,        nullable=True)
    config_schema:  Mapped[dict]          = mapped_column(PortableJSON(), default=dict)
    config:         Mapped[dict]          = mapped_column(PortableJSON(), default=dict)
    credentials:    Mapped[dict]          = mapped_column(PortableJSON(), default=dict)
    tenant_id:      Mapped[str | None]    = mapped_column(String(255), nullable=True)
    enabled:        Mapped[bool]          = mapped_column(Boolean,     default=True)
    last_tested_at:        Mapped[datetime|None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_test_ok:          Mapped[bool | None]   = mapped_column(Boolean,    nullable=True)
    credential_expires_at: Mapped[datetime|None] = mapped_column(DateTime(timezone=True), nullable=True)
    credentials_updated_at:Mapped[datetime|None] = mapped_column(DateTime(timezone=True), nullable=True, default=_utcnow)
    created_by:            Mapped[str | None]    = mapped_column(String(255), nullable=True)
    created_at:            Mapped[datetime]      = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at:            Mapped[datetime]      = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    calls: Mapped[list["IntegrationCallModel"]] = relationship(
        "IntegrationCallModel", back_populates="connector", cascade="all, delete-orphan",
    )
    dlq_items: Mapped[list["DeadLetterQueueModel"]] = relationship(
        "DeadLetterQueueModel", back_populates="connector", cascade="all, delete-orphan",
    )


class IntegrationCallModel(Base):
    """Log of every connector execution."""

    __tablename__ = "integration_calls"
    __table_args__ = (
        Index("ix_int_calls_connector", "connector_id"),
        Index("ix_int_calls_case",      "case_id"),
        Index("ix_int_calls_status",    "status"),
        Index("ix_int_calls_created",   "created_at"),
    )

    id:           Mapped[uuid.UUID]     = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    connector_id: Mapped[uuid.UUID|None]= mapped_column(GUID(), ForeignKey("connector_registry.id", ondelete="SET NULL"), nullable=True)
    case_id:      Mapped[uuid.UUID|None]= mapped_column(GUID(), nullable=True)
    step_id:      Mapped[str | None]    = mapped_column(String(255), nullable=True)
    status:       Mapped[str]           = mapped_column(String(20),  nullable=False, default="pending")
    request:      Mapped[dict]          = mapped_column(PortableJSON(), default=dict)
    response:     Mapped[dict | None]   = mapped_column(PortableJSON(), nullable=True)
    error:        Mapped[str | None]    = mapped_column(Text,        nullable=True)
    latency_ms:   Mapped[int | None]    = mapped_column(Integer,     nullable=True)
    retry_count:  Mapped[int]           = mapped_column(Integer,     default=0)
    next_retry_at:Mapped[datetime|None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at:   Mapped[datetime]      = mapped_column(DateTime(timezone=True), default=_utcnow)
    completed_at: Mapped[datetime|None] = mapped_column(DateTime(timezone=True), nullable=True)

    connector: Mapped["ConnectorRegistryModel | None"] = relationship(
        "ConnectorRegistryModel", back_populates="calls",
    )


class DeadLetterQueueModel(Base):
    """Failed connector calls awaiting retry or manual resolution."""

    __tablename__ = "dead_letter_queue"
    __table_args__ = (
        Index("ix_dlq_connector",  "connector_id"),
        Index("ix_dlq_resolution", "resolution"),
        Index("ix_dlq_retry",      "next_retry_at"),
    )

    id:            Mapped[uuid.UUID]     = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    connector_id:  Mapped[uuid.UUID|None]= mapped_column(GUID(), ForeignKey("connector_registry.id", ondelete="SET NULL"), nullable=True)
    case_id:       Mapped[uuid.UUID|None]= mapped_column(GUID(), nullable=True)
    step_id:       Mapped[str | None]    = mapped_column(String(255), nullable=True)
    payload:       Mapped[dict]          = mapped_column(PortableJSON(), default=dict)
    error:         Mapped[str | None]    = mapped_column(Text,        nullable=True)
    retry_count:   Mapped[int]           = mapped_column(Integer,     default=0)
    max_retries:   Mapped[int]           = mapped_column(Integer,     default=3)
    next_retry_at: Mapped[datetime|None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution:    Mapped[str | None]    = mapped_column(String(20),  nullable=True)
    created_at:    Mapped[datetime]      = mapped_column(DateTime(timezone=True), default=_utcnow)
    resolved_at:   Mapped[datetime|None] = mapped_column(DateTime(timezone=True), nullable=True)

    connector: Mapped["ConnectorRegistryModel | None"] = relationship(
        "ConnectorRegistryModel", back_populates="dlq_items",
    )

# <<< P28 HxBridge


# >>> P48 HxConnect Payments

class PaymentRequestModel(Base):
    """A payment request initiated from a case step."""

    __tablename__ = "payment_requests"
    __table_args__ = (
        Index("ix_payment_requests_case",   "case_id"),
        Index("ix_payment_requests_ref",    "provider_ref"),
        Index("ix_payment_requests_status", "status"),
        Index("ix_payment_requests_tenant", "tenant_id"),
    )

    id:           Mapped[uuid.UUID]      = mapped_column(GUID(),       primary_key=True, default=_new_uuid)
    tenant_id:    Mapped[str]            = mapped_column(String(255),  nullable=False)
    case_id:      Mapped[uuid.UUID]      = mapped_column(GUID(),       ForeignKey("case_instances.id", ondelete="CASCADE"), nullable=False)
    step_id:      Mapped[str]            = mapped_column(String(255),  nullable=False)
    connector_id: Mapped[uuid.UUID|None] = mapped_column(GUID(),       ForeignKey("connector_registry.id", ondelete="SET NULL"), nullable=True)
    provider:     Mapped[str]            = mapped_column(String(50),   nullable=False)
    provider_ref: Mapped[str | None]     = mapped_column(String(255),  nullable=True)
    checkout_url: Mapped[str | None]     = mapped_column(Text,         nullable=True)
    amount_cents: Mapped[int]            = mapped_column(BigInteger,   nullable=False)
    currency:     Mapped[str]            = mapped_column(String(10),   nullable=False, default="usd")
    status:       Mapped[str]            = mapped_column(String(50),   nullable=False, default="pending")
    description:  Mapped[str | None]     = mapped_column(Text,         nullable=True)
    payment_meta: Mapped[dict]           = mapped_column("metadata", PortableJSON(), default=dict)
    created_at:   Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at:   Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
    completed_at: Mapped[datetime|None]  = mapped_column(DateTime(timezone=True), nullable=True)


class PaymentWebhookEventModel(Base):
    """Log of every inbound payment provider webhook."""

    __tablename__ = "payment_webhook_events"
    __table_args__ = (
        Index("ix_pwh_provider_ref", "provider_ref"),
        Index("ix_pwh_received",     "received_at"),
    )

    id:           Mapped[uuid.UUID]    = mapped_column(GUID(),       primary_key=True, default=_new_uuid)
    provider:     Mapped[str]          = mapped_column(String(50),   nullable=False)
    event_type:   Mapped[str | None]   = mapped_column(String(255),  nullable=True)
    provider_ref: Mapped[str | None]   = mapped_column(String(255),  nullable=True)
    payload:      Mapped[dict]         = mapped_column(PortableJSON(), default=dict)
    verified:     Mapped[bool]         = mapped_column(Boolean,      default=False)
    processed:    Mapped[bool]         = mapped_column(Boolean,      default=False)
    error:        Mapped[str | None]   = mapped_column(Text,         nullable=True)
    received_at:  Mapped[datetime]     = mapped_column(DateTime(timezone=True), default=_utcnow)

class PaymentDisbursementModel(Base):
    """An outgoing payment disbursed to a customer from a case step."""

    __tablename__ = "payment_disbursements"
    __table_args__ = (
        Index("ix_payment_disbursements_case",   "case_id"),
        Index("ix_payment_disbursements_status", "status"),
        Index("ix_payment_disbursements_tenant", "tenant_id"),
    )

    id:             Mapped[uuid.UUID]    = mapped_column(GUID(),       primary_key=True, default=_new_uuid)
    tenant_id:      Mapped[str]          = mapped_column(String(255),  nullable=False)
    case_id:        Mapped[uuid.UUID]    = mapped_column(GUID(),       ForeignKey("case_instances.id", ondelete="CASCADE"), nullable=False)
    step_id:        Mapped[str]          = mapped_column(String(255),  nullable=False)
    amount_cents:   Mapped[int]          = mapped_column(BigInteger,   nullable=False)
    currency:       Mapped[str]          = mapped_column(String(10),   nullable=False, default="usd")
    status:         Mapped[str]          = mapped_column(String(50),   nullable=False, default="pending")
    description:    Mapped[str | None]   = mapped_column(Text,         nullable=True)
    bank_reference: Mapped[str | None]   = mapped_column(Text,         nullable=True)
    notes:          Mapped[str | None]   = mapped_column(Text,         nullable=True)
    confirmed_by:             Mapped[str | None]    = mapped_column(String(255),  nullable=True)
    confirmed_at:             Mapped[datetime|None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at:             Mapped[datetime|None] = mapped_column(DateTime(timezone=True), nullable=True)
    disbursement_executed:    Mapped[bool]          = mapped_column(Boolean, default=False, nullable=False)
    disbursement_executed_at: Mapped[datetime|None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at:               Mapped[datetime]      = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
    created_at:               Mapped[datetime]      = mapped_column(DateTime(timezone=True), default=_utcnow)

# <<< P48 HxConnect Payments

# >>> P49 HxConnect KYC & E-Sign

class IdentityVerificationModel(Base):
    """KYC identity check initiated from a case step via Onfido hosted flow."""

    __tablename__ = "identity_verifications"
    __table_args__ = (
        Index("ix_iv_case",   "case_id"),
        Index("ix_iv_check",  "check_id"),
        Index("ix_iv_status", "status"),
    )

    id:               Mapped[uuid.UUID]    = mapped_column(GUID(),       primary_key=True, default=_new_uuid)
    tenant_id:        Mapped[str]          = mapped_column(String(255),  nullable=False)
    case_id:          Mapped[uuid.UUID]    = mapped_column(GUID(),       ForeignKey("case_instances.id", ondelete="CASCADE"), nullable=False)
    step_id:          Mapped[str]          = mapped_column(String(255),  nullable=False)
    connector_id:     Mapped[uuid.UUID|None] = mapped_column(GUID(),     ForeignKey("connector_registry.id", ondelete="SET NULL"), nullable=True)
    provider:         Mapped[str]          = mapped_column(String(50),   nullable=False, default="onfido")
    check_id:         Mapped[str|None]     = mapped_column(String(255),  nullable=True)
    applicant_id:     Mapped[str|None]     = mapped_column(String(255),  nullable=True)
    sdk_token:        Mapped[str|None]     = mapped_column(Text,         nullable=True)
    verification_url: Mapped[str|None]     = mapped_column(Text,         nullable=True)
    status:           Mapped[str]          = mapped_column(String(50),   nullable=False, default="pending")
    result:           Mapped[str|None]     = mapped_column(String(50),   nullable=True)
    result_hash:      Mapped[str|None]     = mapped_column(Text,         nullable=True)
    created_at:       Mapped[datetime]     = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at:       Mapped[datetime]     = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
    completed_at:     Mapped[datetime|None]= mapped_column(DateTime(timezone=True), nullable=True)


class ESignRequestModel(Base):
    """DocuSign e-signature request initiated from a case step."""

    __tablename__ = "esign_requests"
    __table_args__ = (
        Index("ix_esign_case",     "case_id"),
        Index("ix_esign_envelope", "envelope_id"),
        Index("ix_esign_status",   "status"),
    )

    id:            Mapped[uuid.UUID]    = mapped_column(GUID(),       primary_key=True, default=_new_uuid)
    tenant_id:     Mapped[str]          = mapped_column(String(255),  nullable=False)
    case_id:       Mapped[uuid.UUID]    = mapped_column(GUID(),       ForeignKey("case_instances.id", ondelete="CASCADE"), nullable=False)
    step_id:       Mapped[str]          = mapped_column(String(255),  nullable=False)
    connector_id:  Mapped[uuid.UUID|None] = mapped_column(GUID(),     ForeignKey("connector_registry.id", ondelete="SET NULL"), nullable=True)
    provider:      Mapped[str]          = mapped_column(String(50),   nullable=False, default="docusign")
    envelope_id:   Mapped[str|None]     = mapped_column(String(255),  nullable=True)
    signing_url:   Mapped[str|None]     = mapped_column(Text,         nullable=True)
    document_name: Mapped[str|None]     = mapped_column(Text,         nullable=True)
    signer_email:  Mapped[str|None]     = mapped_column(Text,         nullable=True)
    signer_name:   Mapped[str|None]     = mapped_column(Text,         nullable=True)
    status:        Mapped[str]          = mapped_column(String(50),   nullable=False, default="pending")
    signed_at:     Mapped[datetime|None]= mapped_column(DateTime(timezone=True), nullable=True)
    created_at:    Mapped[datetime]     = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at:    Mapped[datetime]     = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

# <<< P49 HxConnect KYC & E-Sign

# >>> P50 HxConnect CRM & Accounting

class CrmSyncRecordModel(Base):
    __tablename__ = "crm_sync_records"
    __table_args__ = (
        Index("ix_crm_sync_case",   "case_id"),
        Index("ix_crm_sync_status", "status"),
        Index("ix_crm_sync_record", "crm_record_id"),
    )

    id:             Mapped[uuid.UUID]    = mapped_column(GUID(),       primary_key=True, default=_new_uuid)
    tenant_id:      Mapped[str]          = mapped_column(String(255),  nullable=False)
    case_id:        Mapped[uuid.UUID]    = mapped_column(GUID(),       ForeignKey("case_instances.id", ondelete="CASCADE"), nullable=False)
    step_id:        Mapped[str]          = mapped_column(String(255),  nullable=False)
    connector_id:   Mapped[uuid.UUID|None] = mapped_column(GUID(),     ForeignKey("connector_registry.id", ondelete="SET NULL"), nullable=True)
    provider:       Mapped[str]          = mapped_column(String(50),   nullable=False, default="salesforce")
    crm_object_type:Mapped[str|None]     = mapped_column(String(100),  nullable=True)
    crm_record_id:  Mapped[str|None]     = mapped_column(String(255),  nullable=True)
    crm_record_url: Mapped[str|None]     = mapped_column(Text,         nullable=True)
    status:         Mapped[str]          = mapped_column(String(50),   nullable=False, default="pending")
    sync_data:      Mapped[dict]         = mapped_column(PortableJSON(), default=dict)
    error:          Mapped[str|None]     = mapped_column(Text,         nullable=True)
    created_at:     Mapped[datetime]     = mapped_column(DateTime(timezone=True), default=_utcnow)
    synced_at:      Mapped[datetime|None]= mapped_column(DateTime(timezone=True), nullable=True)


class InvoiceRecordModel(Base):
    __tablename__ = "invoice_records"
    __table_args__ = (
        Index("ix_invoice_rec_case",    "case_id"),
        Index("ix_invoice_rec_status",  "status"),
        Index("ix_invoice_rec_invoice", "invoice_id"),
    )

    id:             Mapped[uuid.UUID]    = mapped_column(GUID(),       primary_key=True, default=_new_uuid)
    tenant_id:      Mapped[str]          = mapped_column(String(255),  nullable=False)
    case_id:        Mapped[uuid.UUID]    = mapped_column(GUID(),       ForeignKey("case_instances.id", ondelete="CASCADE"), nullable=False)
    step_id:        Mapped[str]          = mapped_column(String(255),  nullable=False)
    connector_id:   Mapped[uuid.UUID|None] = mapped_column(GUID(),     ForeignKey("connector_registry.id", ondelete="SET NULL"), nullable=True)
    provider:       Mapped[str]          = mapped_column(String(50),   nullable=False, default="xero")
    invoice_id:     Mapped[str|None]     = mapped_column(String(255),  nullable=True)
    invoice_number: Mapped[str|None]     = mapped_column(String(100),  nullable=True)
    invoice_url:    Mapped[str|None]     = mapped_column(Text,         nullable=True)
    amount_cents:   Mapped[int|None]     = mapped_column(BigInteger,   nullable=True)
    currency:       Mapped[str]          = mapped_column(String(10),   nullable=False, default="usd")
    status:         Mapped[str]          = mapped_column(String(50),   nullable=False, default="pending")
    contact_name:   Mapped[str|None]     = mapped_column(Text,         nullable=True)
    line_items:     Mapped[list]         = mapped_column(PortableJSON(), default=list)
    created_at:     Mapped[datetime]     = mapped_column(DateTime(timezone=True), default=_utcnow)
    issued_at:      Mapped[datetime|None]= mapped_column(DateTime(timezone=True), nullable=True)

# <<< P50 HxConnect CRM & Accounting


# >>> P26 HxAnalytics

class SavedReportModel(Base):
    """A user-defined analytics report definition."""

    __tablename__ = "saved_reports"
    __table_args__ = (
        Index("ix_saved_reports_tenant",  "tenant_id"),
        Index("ix_saved_reports_public",  "is_public"),
        Index("ix_saved_reports_created", "created_at"),
    )

    id:          Mapped[uuid.UUID]  = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    name:        Mapped[str]        = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text,        nullable=True)
    query_type:  Mapped[str]        = mapped_column(String(20),  nullable=False, default="structured")
    query_def:   Mapped[dict]       = mapped_column(PortableJSON(), default=dict)
    chart_type:  Mapped[str]        = mapped_column(String(30),  nullable=False, default="bar")
    created_by:  Mapped[str | None] = mapped_column(String(255), nullable=True)
    tenant_id:   Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_public:   Mapped[bool]       = mapped_column(Boolean,     default=False)
    created_at:  Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at:  Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    subscriptions: Mapped[list["ReportSubscriptionModel"]] = relationship(
        back_populates="report", cascade="all, delete-orphan",
    )


class ReportSubscriptionModel(Base):
    """Scheduled delivery subscription for a saved report."""

    __tablename__ = "report_subscriptions"

    id:            Mapped[uuid.UUID]     = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    report_id:     Mapped[uuid.UUID]     = mapped_column(GUID(), ForeignKey("saved_reports.id", ondelete="CASCADE"), nullable=False)
    delivery_type: Mapped[str]           = mapped_column(String(20),  nullable=False, default="email")
    destination:   Mapped[str]           = mapped_column(String(500), nullable=False)
    schedule:      Mapped[str]           = mapped_column(String(50),  nullable=False, default="daily")
    format:        Mapped[str]           = mapped_column(String(10),  nullable=False, default="csv")
    enabled:       Mapped[bool]          = mapped_column(Boolean,     default=True)
    last_sent_at:  Mapped[datetime|None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by:    Mapped[str | None]    = mapped_column(String(255), nullable=True)
    created_at:    Mapped[datetime]      = mapped_column(DateTime(timezone=True), default=_utcnow)

    report: Mapped["SavedReportModel"] = relationship(back_populates="subscriptions")

# <<< P26 HxAnalytics


# >>> P29 HxSync

class SyncDestinationModel(Base):
    """A configured sync destination (DWH, stream, file)."""

    __tablename__ = "sync_destinations"
    __table_args__ = (
        Index("ix_sync_dest_tenant",  "tenant_id"),
        Index("ix_sync_dest_enabled", "enabled"),
    )

    id:                 Mapped[uuid.UUID]  = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    name:               Mapped[str]        = mapped_column(String(255), nullable=False)
    dest_type:          Mapped[str]        = mapped_column(String(30),  nullable=False, default="duckdb")
    connection_config:  Mapped[dict]       = mapped_column(PortableJSON(), default=dict)
    enabled:            Mapped[bool]       = mapped_column(Boolean, default=True)
    tenant_id:          Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by:         Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_synced_at:     Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_status:   Mapped[str | None] = mapped_column(String(20), default="never")
    created_at:         Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at:         Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    runs:     Mapped[list["SyncRunModel"]]          = relationship(back_populates="destination", cascade="all, delete-orphan")
    mappings: Mapped[list["SyncFieldMappingModel"]] = relationship(back_populates="destination", cascade="all, delete-orphan")
    redactions: Mapped[list["SyncRedactionRuleModel"]] = relationship(back_populates="destination", cascade="all, delete-orphan")


class SyncRunModel(Base):
    """One execution of a sync pipeline against a destination."""

    __tablename__ = "sync_runs"
    __table_args__ = (
        Index("ix_sync_runs_dest",    "destination_id"),
        Index("ix_sync_runs_status",  "status"),
        Index("ix_sync_runs_started", "started_at"),
    )

    id:             Mapped[uuid.UUID]      = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    destination_id: Mapped[uuid.UUID]      = mapped_column(GUID(), ForeignKey("sync_destinations.id", ondelete="CASCADE"), nullable=False)
    status:         Mapped[str]            = mapped_column(String(20), nullable=False, default="running")
    rows_synced:    Mapped[int]            = mapped_column(Integer, default=0)
    error_msg:      Mapped[str | None]     = mapped_column(Text, nullable=True)
    watermark_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    watermark_to:   Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at:     Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow)
    finished_at:    Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at:     Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow)

    destination: Mapped["SyncDestinationModel"] = relationship(back_populates="runs")


class SyncFieldMappingModel(Base):
    """Maps a Helix case field to a DWH column with optional transform."""

    __tablename__ = "sync_field_mappings"

    id:             Mapped[uuid.UUID]  = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    destination_id: Mapped[uuid.UUID]  = mapped_column(GUID(), ForeignKey("sync_destinations.id", ondelete="CASCADE"), nullable=False)
    case_type_id:   Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_field:   Mapped[str]        = mapped_column(String(255), nullable=False)
    dest_column:    Mapped[str]        = mapped_column(String(255), nullable=False)
    transform:      Mapped[str]        = mapped_column(String(30),  nullable=False, default="passthrough")
    pii:            Mapped[bool]       = mapped_column(Boolean, default=False)
    created_at:     Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_utcnow)

    destination: Mapped["SyncDestinationModel"] = relationship(back_populates="mappings")


class SyncRedactionRuleModel(Base):
    """PII redaction rule: hash/drop/mask a field before it leaves Helix."""

    __tablename__ = "sync_redaction_rules"

    id:             Mapped[uuid.UUID]  = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    destination_id: Mapped[uuid.UUID]  = mapped_column(GUID(), ForeignKey("sync_destinations.id", ondelete="CASCADE"), nullable=False)
    case_type_id:   Mapped[str | None] = mapped_column(String(255), nullable=True)
    field_path:     Mapped[str]        = mapped_column(String(255), nullable=False)
    action:         Mapped[str]        = mapped_column(String(10),  nullable=False, default="hash")
    reason:         Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at:     Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_utcnow)

    destination: Mapped["SyncDestinationModel"] = relationship(back_populates="redactions")

# <<< P29 HxSync


# >>> P35 HxGlobal

class RegionModel(Base):
    """A registered deployment region (cloud zone, on-prem DC, etc.)."""

    __tablename__ = "region_registry"
    __table_args__ = (Index("ix_region_enabled", "enabled"),)

    id:                Mapped[uuid.UUID]  = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    name:              Mapped[str]        = mapped_column(String(100), nullable=False, unique=True)
    provider:          Mapped[str]        = mapped_column(String(20),  nullable=False, default="local")
    location:          Mapped[str | None] = mapped_column(String(100), nullable=True)
    endpoint:          Mapped[str | None] = mapped_column(String(500), nullable=True)
    connection_config: Mapped[dict]       = mapped_column(PortableJSON(), default=dict)
    is_primary:        Mapped[bool]       = mapped_column(Boolean, default=False)
    enabled:           Mapped[bool]       = mapped_column(Boolean, default=True)
    created_at:        Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at:        Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    sovereignty_rules: Mapped[list["SovereigntyRuleModel"]]      = relationship(back_populates="region", cascade="all, delete-orphan")
    tenant_assignments: Mapped[list["TenantRegionAssignmentModel"]] = relationship(back_populates="region", cascade="all, delete-orphan")
    health_logs:       Mapped[list["RegionHealthLogModel"]]       = relationship(back_populates="region", cascade="all, delete-orphan")
    access_logs:       Mapped[list["RegionAccessLogModel"]]       = relationship(back_populates="region", cascade="all, delete-orphan")


class SovereigntyRuleModel(Base):
    """Data residency rule: tenant/case-type data must stay in this region."""

    __tablename__ = "sovereignty_rules"
    __table_args__ = (
        Index("ix_sov_tenant",    "tenant_id"),
        Index("ix_sov_case_type", "case_type_id"),
        Index("ix_sov_region",    "region_id"),
    )

    id:           Mapped[uuid.UUID]  = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:    Mapped[str | None] = mapped_column(String(255), nullable=True)
    case_type_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    region_id:    Mapped[uuid.UUID]  = mapped_column(GUID(), ForeignKey("region_registry.id", ondelete="CASCADE"), nullable=False)
    regulation:   Mapped[str]        = mapped_column(String(50),  nullable=False, default="GDPR")
    description:  Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by:   Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at:   Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_utcnow)

    region: Mapped["RegionModel"] = relationship(back_populates="sovereignty_rules")


class TenantRegionAssignmentModel(Base):
    """Maps a tenant to its authoritative (primary/replica) region."""

    __tablename__ = "tenant_region_assignments"
    __table_args__ = (
        Index("ix_tra_tenant", "tenant_id"),
        UniqueConstraint("tenant_id", "region_id", "assignment_type", name="uq_tra"),
    )

    id:              Mapped[uuid.UUID]      = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:       Mapped[str]            = mapped_column(String(255), nullable=False)
    region_id:       Mapped[uuid.UUID]      = mapped_column(GUID(), ForeignKey("region_registry.id", ondelete="CASCADE"), nullable=False)
    assignment_type: Mapped[str]            = mapped_column(String(20),  nullable=False, default="primary")
    migrated_at:     Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at:      Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow)

    region: Mapped["RegionModel"] = relationship(back_populates="tenant_assignments")


class RegionHealthLogModel(Base):
    """Point-in-time health snapshot for a region."""

    __tablename__ = "region_health_log"
    __table_args__ = (
        Index("ix_rhl_region",   "region_id"),
        Index("ix_rhl_recorded", "recorded_at"),
    )

    id:                 Mapped[uuid.UUID]  = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    region_id:          Mapped[uuid.UUID]  = mapped_column(GUID(), ForeignKey("region_registry.id", ondelete="CASCADE"), nullable=False)
    status:             Mapped[str]        = mapped_column(String(20),  nullable=False, default="healthy")
    latency_ms:         Mapped[int | None] = mapped_column(Integer, nullable=True)
    active_cases:       Mapped[int | None] = mapped_column(Integer, nullable=True)
    replication_lag_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_msg:          Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_at:        Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_utcnow)

    region: Mapped["RegionModel"] = relationship(back_populates="health_logs")


class RegionAccessLogModel(Base):
    """Immutable cross-region data access record (GDPR Article 30)."""

    __tablename__ = "region_access_log"
    __table_args__ = (
        Index("ix_ral_region",   "region_id"),
        Index("ix_ral_tenant",   "tenant_id"),
        Index("ix_ral_recorded", "recorded_at"),
    )

    id:          Mapped[uuid.UUID]  = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    region_id:   Mapped[uuid.UUID]  = mapped_column(GUID(), ForeignKey("region_registry.id", ondelete="CASCADE"), nullable=False)
    tenant_id:   Mapped[str | None] = mapped_column(String(255), nullable=True)
    actor_id:    Mapped[str | None] = mapped_column(String(255), nullable=True)
    action:      Mapped[str]        = mapped_column(String(50),  nullable=False)
    resource:    Mapped[str | None] = mapped_column(String(255), nullable=True)
    legal_basis: Mapped[str | None] = mapped_column(String(100), nullable=True)
    recorded_at: Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_utcnow)

    region: Mapped["RegionModel"] = relationship(back_populates="access_logs")

# <<< P35 HxGlobal


# >>> P59 HxShield

class SecurityRuleModel(Base):
    """Configurable detection rule for fraud / abuse patterns."""

    __tablename__ = "security_rules"
    __table_args__ = (
        Index("ix_shield_rules_pattern", "pattern_type"),
        Index("ix_shield_rules_enabled", "enabled"),
    )

    id:             Mapped[uuid.UUID]  = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    name:           Mapped[str]        = mapped_column(String(255), nullable=False)
    pattern_type:   Mapped[str]        = mapped_column(String(50),  nullable=False)
    description:    Mapped[str | None] = mapped_column(Text, nullable=True)
    threshold:      Mapped[int]        = mapped_column(Integer, default=10)
    window_seconds: Mapped[int]        = mapped_column(Integer, default=600)
    action:         Mapped[str]        = mapped_column(String(20),  nullable=False, default="flag")
    severity:       Mapped[str]        = mapped_column(String(10),  nullable=False, default="medium")
    enabled:        Mapped[bool]       = mapped_column(Boolean, default=True)
    tenant_id:      Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by:     Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at:     Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at:     Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    incidents: Mapped[list["SecurityIncidentModel"]] = relationship(back_populates="rule")


class SecurityIncidentModel(Base):
    """A detected fraud/abuse event — a first-class Helix incident record."""

    __tablename__ = "security_incidents"
    __table_args__ = (
        Index("ix_shield_inc_status",   "status"),
        Index("ix_shield_inc_severity", "severity"),
        Index("ix_shield_inc_actor",    "actor_id"),
        Index("ix_shield_inc_tenant",   "tenant_id"),
        Index("ix_shield_inc_detected", "detected_at"),
    )

    id:               Mapped[uuid.UUID]      = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    rule_id:          Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("security_rules.id", ondelete="SET NULL"), nullable=True)
    pattern_type:     Mapped[str]            = mapped_column(String(50),  nullable=False)
    severity:         Mapped[str]            = mapped_column(String(10),  nullable=False, default="medium")
    status:           Mapped[str]            = mapped_column(String(20),  nullable=False, default="open")
    actor_id:         Mapped[str | None]     = mapped_column(String(255), nullable=True)
    tenant_id:        Mapped[str | None]     = mapped_column(String(255), nullable=True)
    case_type_id:     Mapped[str | None]     = mapped_column(String(255), nullable=True)
    context:          Mapped[dict]           = mapped_column(PortableJSON(), default=dict)
    explanation:      Mapped[str | None]     = mapped_column(Text, nullable=True)
    detected_at:      Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow)
    resolved_at:      Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by:      Mapped[str | None]     = mapped_column(String(255), nullable=True)

    rule: Mapped["SecurityRuleModel | None"] = relationship(back_populates="incidents")


class ShieldEventModel(Base):
    """Raw scored event record from the HxShield detection engine."""

    __tablename__ = "shield_events"
    __table_args__ = (
        Index("ix_shield_ev_actor",    "actor_id"),
        Index("ix_shield_ev_type",     "event_type"),
        Index("ix_shield_ev_recorded", "recorded_at"),
    )

    id:               Mapped[uuid.UUID]  = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    event_type:       Mapped[str]        = mapped_column(String(50),  nullable=False)
    actor_id:         Mapped[str | None] = mapped_column(String(255), nullable=True)
    tenant_id:        Mapped[str | None] = mapped_column(String(255), nullable=True)
    case_type_id:     Mapped[str | None] = mapped_column(String(255), nullable=True)
    payload_hash:     Mapped[str | None] = mapped_column(String(64),  nullable=True)
    score:            Mapped[float]      = mapped_column(Float, default=0.0)
    patterns_matched: Mapped[list]       = mapped_column(PortableJSON(), default=list)
    raw_context:      Mapped[dict]       = mapped_column(PortableJSON(), default=dict)
    recorded_at:      Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_utcnow)

# <<< P59 HxShield


# >>> P47 HxFusion

class ProcessDefinitionModel(Base):
    """Deployed BPMN process definition."""

    __tablename__ = "process_definitions"
    __table_args__ = (
        Index("ix_pd_case_type", "case_type_id"),
        Index("ix_pd_status",    "status"),
    )

    id:           Mapped[uuid.UUID]  = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    name:         Mapped[str]        = mapped_column(String(255), nullable=False)
    version:      Mapped[int]        = mapped_column(Integer, default=1)
    description:  Mapped[str | None] = mapped_column(Text, nullable=True)
    bpmn_xml:     Mapped[str]        = mapped_column(Text, nullable=False)
    case_type_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status:       Mapped[str]        = mapped_column(String(20), nullable=False, default="active")
    created_by:   Mapped[str | None] = mapped_column(String(255), nullable=True)
    tenant_id:    Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at:   Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at:   Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    instances: Mapped[list["ProcessInstanceModel"]] = relationship(back_populates="definition")


class ProcessInstanceModel(Base):
    """A running or completed process instance."""

    __tablename__ = "process_instances"
    __table_args__ = (
        Index("ix_pi_definition", "definition_id"),
        Index("ix_pi_case",       "case_id"),
        Index("ix_pi_status",     "status"),
        Index("ix_pi_tenant",     "tenant_id"),
    )

    id:            Mapped[uuid.UUID]      = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    definition_id: Mapped[uuid.UUID]      = mapped_column(GUID(), ForeignKey("process_definitions.id", ondelete="RESTRICT"), nullable=False)
    case_id:       Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    status:        Mapped[str]            = mapped_column(String(20), nullable=False, default="running")
    current_node:  Mapped[str | None]     = mapped_column(String(255), nullable=True)
    context:       Mapped[dict]           = mapped_column(PortableJSON(), default=dict)
    error_node:    Mapped[str | None]     = mapped_column(String(255), nullable=True)
    error_message: Mapped[str | None]     = mapped_column(Text, nullable=True)
    started_at:    Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow)
    ended_at:      Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    tenant_id:     Mapped[str | None]     = mapped_column(String(255), nullable=True)

    definition: Mapped["ProcessDefinitionModel"]        = relationship(back_populates="instances")
    bindings:   Mapped[list["ProcessCaseBindingModel"]] = relationship(back_populates="instance")
    task_log:   Mapped[list["ProcessTaskLogModel"]]     = relationship(back_populates="instance", order_by="ProcessTaskLogModel.started_at")


class ProcessCaseBindingModel(Base):
    """Bidirectional case ↔ process binding."""

    __tablename__ = "process_case_bindings"
    __table_args__ = (
        Index("ix_pcb_case",     "case_id"),
        Index("ix_pcb_instance", "instance_id"),
        Index("ix_pcb_status",   "status"),
    )

    id:           Mapped[uuid.UUID]      = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    case_id:      Mapped[uuid.UUID]      = mapped_column(GUID(), nullable=False)
    instance_id:  Mapped[uuid.UUID]      = mapped_column(GUID(), ForeignKey("process_instances.id", ondelete="CASCADE"), nullable=False)
    binding_type: Mapped[str]            = mapped_column(String(30), nullable=False, default="embedded_subprocess")
    direction:    Mapped[str]            = mapped_column(String(30), nullable=False, default="case_to_process")
    status:       Mapped[str]            = mapped_column(String(20), nullable=False, default="active")
    stage_id:     Mapped[str | None]     = mapped_column(String(255), nullable=True)
    step_id:      Mapped[str | None]     = mapped_column(String(255), nullable=True)
    created_at:   Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow)
    resolved_at:  Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    instance: Mapped["ProcessInstanceModel"] = relationship(back_populates="bindings")


class ProcessTaskLogModel(Base):
    """Execution log entry for a single BPMN node."""

    __tablename__ = "process_task_log"
    __table_args__ = (
        Index("ix_ptl_instance", "instance_id"),
        Index("ix_ptl_node_id",  "node_id"),
        Index("ix_ptl_status",   "status"),
    )

    id:            Mapped[uuid.UUID]      = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    instance_id:   Mapped[uuid.UUID]      = mapped_column(GUID(), ForeignKey("process_instances.id", ondelete="CASCADE"), nullable=False)
    node_id:       Mapped[str]            = mapped_column(String(255), nullable=False)
    node_name:     Mapped[str | None]     = mapped_column(String(255), nullable=True)
    node_type:     Mapped[str]            = mapped_column(String(50), nullable=False)
    status:        Mapped[str]            = mapped_column(String(20), nullable=False, default="running")
    input_context: Mapped[dict]           = mapped_column(PortableJSON(), default=dict)
    result:        Mapped[dict | None]    = mapped_column(PortableJSON(), nullable=True)
    error:         Mapped[str | None]     = mapped_column(Text, nullable=True)
    started_at:    Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow)
    ended_at:      Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    instance: Mapped["ProcessInstanceModel"] = relationship(back_populates="task_log")

# <<< P47 HxFusion


# <<< P51 HxConnect: Communications

class SmsMessageModel(Base):
    """Outbound SMS via Twilio."""

    __tablename__ = "sms_messages"
    __table_args__ = (
        Index("ix_sms_case",   "case_id"),
        Index("ix_sms_status", "status"),
    )

    id:           Mapped[uuid.UUID]      = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:    Mapped[str]            = mapped_column(String(255), nullable=False)
    case_id:      Mapped[uuid.UUID]      = mapped_column(GUID(), ForeignKey("case_instances.id", ondelete="CASCADE"), nullable=False)
    step_id:      Mapped[str]            = mapped_column(String(255), nullable=False)
    connector_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("connector_registry.id", ondelete="SET NULL"), nullable=True)
    provider:     Mapped[str]            = mapped_column(String(50), nullable=False, default="twilio")
    to_number:    Mapped[str]            = mapped_column(String(50), nullable=False)
    from_number:  Mapped[str | None]     = mapped_column(String(50), nullable=True)
    body:         Mapped[str]            = mapped_column(Text, nullable=False)
    message_sid:  Mapped[str | None]     = mapped_column(String(255), nullable=True)
    status:       Mapped[str]            = mapped_column(String(50), nullable=False, default="pending")
    error:        Mapped[str | None]     = mapped_column(Text, nullable=True)
    created_at:   Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow)
    sent_at:      Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SlackNotificationModel(Base):
    """Outbound Slack notification."""

    __tablename__ = "slack_notifications"
    __table_args__ = (
        Index("ix_slack_case",   "case_id"),
        Index("ix_slack_status", "status"),
    )

    id:           Mapped[uuid.UUID]      = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:    Mapped[str]            = mapped_column(String(255), nullable=False)
    case_id:      Mapped[uuid.UUID]      = mapped_column(GUID(), ForeignKey("case_instances.id", ondelete="CASCADE"), nullable=False)
    step_id:      Mapped[str]            = mapped_column(String(255), nullable=False)
    connector_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("connector_registry.id", ondelete="SET NULL"), nullable=True)
    channel:      Mapped[str | None]     = mapped_column(String(255), nullable=True)
    message:      Mapped[str]            = mapped_column(Text, nullable=False)
    blocks:       Mapped[list]           = mapped_column(PortableJSON(), nullable=False, default=list)
    slack_ts:     Mapped[str | None]     = mapped_column(String(100), nullable=True)
    status:       Mapped[str]            = mapped_column(String(50), nullable=False, default="pending")
    error:        Mapped[str | None]     = mapped_column(Text, nullable=True)
    created_at:   Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow)
    sent_at:      Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# <<< P52 HxConnect: Document Intelligence & Storage

class DocExtractionJobModel(Base):
    """AI-powered document field extraction job."""

    __tablename__ = "doc_extraction_jobs"
    __table_args__ = (
        Index("ix_docex_case",   "case_id"),
        Index("ix_docex_status", "status"),
    )

    id:               Mapped[uuid.UUID]      = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:        Mapped[str]            = mapped_column(String(255), nullable=False)
    case_id:          Mapped[uuid.UUID]      = mapped_column(GUID(), ForeignKey("case_instances.id", ondelete="CASCADE"), nullable=False)
    step_id:          Mapped[str]            = mapped_column(String(255), nullable=False)
    connector_id:     Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("connector_registry.id", ondelete="SET NULL"), nullable=True)
    provider:         Mapped[str]            = mapped_column(String(50), nullable=False, default="docling")
    document_id:      Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    document_name:    Mapped[str | None]     = mapped_column(Text, nullable=True)
    source_url:       Mapped[str | None]     = mapped_column(Text, nullable=True)
    extracted_fields: Mapped[dict]           = mapped_column(PortableJSON(), nullable=False, default=dict)
    raw_text:         Mapped[str | None]     = mapped_column(Text, nullable=True)
    confidence:       Mapped[float | None]   = mapped_column(Float, nullable=True)
    status:           Mapped[str]            = mapped_column(String(50), nullable=False, default="pending")
    error:            Mapped[str | None]     = mapped_column(Text, nullable=True)
    created_at:       Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow)
    completed_at:     Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DocStorageRouteModel(Base):
    """Cloud storage upload tracking record."""

    __tablename__ = "doc_storage_routes"
    __table_args__ = (
        Index("ix_docst_case",   "case_id"),
        Index("ix_docst_status", "status"),
    )

    id:            Mapped[uuid.UUID]      = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:     Mapped[str]            = mapped_column(String(255), nullable=False)
    case_id:       Mapped[uuid.UUID]      = mapped_column(GUID(), ForeignKey("case_instances.id", ondelete="CASCADE"), nullable=False)
    step_id:       Mapped[str]            = mapped_column(String(255), nullable=False)
    connector_id:  Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("connector_registry.id", ondelete="SET NULL"), nullable=True)
    provider:      Mapped[str]            = mapped_column(String(50), nullable=False, default="s3")
    document_name: Mapped[str]            = mapped_column(Text, nullable=False)
    bucket:        Mapped[str | None]     = mapped_column(Text, nullable=True)
    object_key:    Mapped[str | None]     = mapped_column(Text, nullable=True)
    storage_url:   Mapped[str | None]     = mapped_column(Text, nullable=True)
    presigned_url: Mapped[str | None]     = mapped_column(Text, nullable=True)
    size_bytes:    Mapped[int | None]     = mapped_column(BigInteger, nullable=True)
    content_type:  Mapped[str | None]     = mapped_column(String(255), nullable=True)
    status:        Mapped[str]            = mapped_column(String(50), nullable=False, default="pending")
    error:         Mapped[str | None]     = mapped_column(Text, nullable=True)
    created_at:    Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow)
    uploaded_at:   Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# <<< P53 HxConnect: Developer & Custom Connectors

class WebhookReceiverRuleModel(Base):
    """Routing rule: map inbound webhook payload to a Helix case."""

    __tablename__ = "webhook_receiver_rules"
    __table_args__ = (
        Index("ix_wrr_connector", "connector_id"),
        Index("ix_wrr_tenant",    "tenant_id"),
    )

    id:                 Mapped[uuid.UUID]      = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:          Mapped[str]            = mapped_column(String(255), nullable=False)
    connector_id:       Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("connector_registry.id", ondelete="CASCADE"), nullable=True)
    name:               Mapped[str]            = mapped_column(Text, nullable=False)
    case_id_field:      Mapped[str | None]     = mapped_column(Text, nullable=True)
    match_case_field:   Mapped[str | None]     = mapped_column(Text, nullable=True)
    match_payload_field: Mapped[str | None]    = mapped_column(Text, nullable=True)
    field_updates:      Mapped[dict]           = mapped_column(PortableJSON(), nullable=False, default=dict)
    advance_stage:      Mapped[bool]           = mapped_column(Boolean, nullable=False, default=False)
    enabled:            Mapped[bool]           = mapped_column(Boolean, nullable=False, default=True)
    created_at:         Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow)


class WebhookReceiverEventModel(Base):
    """Log of every inbound webhook received."""

    __tablename__ = "webhook_receiver_events"
    __table_args__ = (
        Index("ix_wre_connector", "connector_id"),
        Index("ix_wre_status",    "status"),
        Index("ix_wre_case",      "matched_case_id"),
    )

    id:              Mapped[uuid.UUID]      = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:       Mapped[str]            = mapped_column(String(255), nullable=False)
    connector_id:    Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("connector_registry.id", ondelete="SET NULL"), nullable=True)
    rule_id:         Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("webhook_receiver_rules.id", ondelete="SET NULL"), nullable=True)
    payload:         Mapped[dict]           = mapped_column(PortableJSON(), nullable=False, default=dict)
    matched_case_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    status:          Mapped[str]            = mapped_column(String(50), nullable=False, default="received")
    error:           Mapped[str | None]     = mapped_column(Text, nullable=True)
    received_at:     Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow)
    processed_at:    Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ── Intake Event Log ──────────────────────────────────────────────────────────

class IntakeEventModel(Base):
    """Every inbound payload received by the intake webhook endpoint.

    Tracks the full lifecycle: received → filter evaluation → case created / rejected.
    """

    __tablename__ = "intake_events"
    __table_args__ = (
        Index("ix_intake_case_type",  "case_type_id"),
        Index("ix_intake_status",     "status"),
        Index("ix_intake_received",   "received_at"),
        Index("ix_intake_case",       "created_case_id"),
    )

    id:               Mapped[uuid.UUID]       = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    case_type_id:     Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("case_types.id", ondelete="SET NULL"), nullable=True)
    connector_id:     Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    source_ip:        Mapped[str | None]       = mapped_column(String(50), nullable=True)
    raw_payload:      Mapped[dict]             = mapped_column(PortableJSON(), nullable=False, default=dict)
    # status: received | passed | filtered | failed | created
    status:           Mapped[str]             = mapped_column(String(20), nullable=False, default="received")
    filter_result:    Mapped[dict]             = mapped_column(PortableJSON(), nullable=False, default=dict)
    created_case_id:  Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    process_instance_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    error:            Mapped[str | None]       = mapped_column(Text, nullable=True)
    received_at:      Mapped[datetime]         = mapped_column(DateTime(timezone=True), default=_utcnow)
    processed_at:     Mapped[datetime | None]  = mapped_column(DateTime(timezone=True), nullable=True)


# <<< P54 HxMigrate: Unified Migration Pipeline

class MigrationPipelineRunModel(Base):
    """One HxMigrate pipeline run — tracks all 5 stages."""

    __tablename__ = "migration_pipeline_runs"
    __table_args__ = (
        Index("ix_mpr_tenant", "tenant_id"),
        Index("ix_mpr_status", "status"),
    )

    id:              Mapped[uuid.UUID]      = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:       Mapped[str]            = mapped_column(String(255), nullable=False)
    name:            Mapped[str]            = mapped_column(Text, nullable=False)
    source_platform: Mapped[str]            = mapped_column(String(50), nullable=False)
    status:          Mapped[str]            = mapped_column(String(50), nullable=False, default="pending")
    mode:            Mapped[str]            = mapped_column(String(20), nullable=False, default="full")
    current_stage:   Mapped[int]            = mapped_column(Integer, nullable=False, default=0)
    scan_id:         Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    import_job_id:   Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    project_id:      Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    package_id:      Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    source_filename: Mapped[str | None]     = mapped_column(Text, nullable=True)
    source_size:     Mapped[int | None]     = mapped_column(BigInteger, nullable=True)
    error:           Mapped[str | None]     = mapped_column(Text, nullable=True)
    created_at:      Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow)
    completed_at:    Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    stages: Mapped[list["PipelineStageEventModel"]] = relationship(
        back_populates="run", order_by="PipelineStageEventModel.stage"
    )


class PipelineStageEventModel(Base):
    """One event per pipeline stage — feeds HxStream for live progress."""

    __tablename__ = "pipeline_stage_events"
    __table_args__ = (
        Index("ix_pse_run", "run_id"),
    )

    id:          Mapped[uuid.UUID]      = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    run_id:      Mapped[uuid.UUID]      = mapped_column(GUID(), ForeignKey("migration_pipeline_runs.id", ondelete="CASCADE"), nullable=False)
    stage:       Mapped[int]            = mapped_column(Integer, nullable=False)
    stage_name:  Mapped[str]            = mapped_column(String(100), nullable=False)
    status:      Mapped[str]            = mapped_column(String(50), nullable=False, default="pending")
    summary:     Mapped[dict]           = mapped_column(PortableJSON(), nullable=False, default=dict)
    error:       Mapped[str | None]     = mapped_column(Text, nullable=True)
    started_at:  Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    run: Mapped["MigrationPipelineRunModel"] = relationship(back_populates="stages")


# ── HxMigrate Audit: case_type_migrations ────────────────────────────────────

class CaseTypeMigrationModel(Base):
    """Immutable audit record created each time a case type is imported via HxMigrate.

    Visible to all users (no tenant restriction — import history is global).
    """
    __tablename__ = "case_type_migrations"

    id                  : Mapped[uuid.UUID]        = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    case_type_id        : Mapped[uuid.UUID]        = mapped_column(GUID(), nullable=False, index=True)
    run_id              : Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    source_platform     : Mapped[str]              = mapped_column(String(100), nullable=False, default="")
    source_filename     : Mapped[str]              = mapped_column(String(500), nullable=False, default="")
    imported_by_user_id : Mapped[str]              = mapped_column(Text, nullable=False, default="")
    imported_by_email   : Mapped[str]              = mapped_column(Text, nullable=False, default="")
    imported_at         : Mapped[datetime]         = mapped_column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    stages_count        : Mapped[int]              = mapped_column(Integer, nullable=False, default=0)
    steps_count         : Mapped[int]              = mapped_column(Integer, nullable=False, default=0)
    forms_count         : Mapped[int]              = mapped_column(Integer, nullable=False, default=0)
    rules_count         : Mapped[int]              = mapped_column(Integer, nullable=False, default=0)
    slas_count          : Mapped[int]              = mapped_column(Integer, nullable=False, default=0)
    notes               : Mapped[str]              = mapped_column(Text, nullable=False, default="")


# <<< P55 HxDeploy: Intelligent Deployment Governance

class EnvironmentRegistryModel(Base):
    """Known deployment environments and their current state."""

    __tablename__ = "environment_registry"
    __table_args__ = (
        Index("ix_env_tenant", "tenant_id"),
        Index("ix_env_name_tenant", "tenant_id", "name", unique=True),
    )

    id:                 Mapped[uuid.UUID]      = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:          Mapped[str]            = mapped_column(String(255), nullable=False)
    name:               Mapped[str]            = mapped_column(String(100), nullable=False)
    label:              Mapped[str]            = mapped_column(Text, nullable=False)
    url:                Mapped[str | None]     = mapped_column(Text, nullable=True)
    order_index:        Mapped[int]            = mapped_column(Integer, nullable=False, default=0)
    current_package_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    current_version:    Mapped[str | None]     = mapped_column(Text, nullable=True)
    status:                 Mapped[str]              = mapped_column(String(50), nullable=False, default="healthy")
    last_deployed_at:       Mapped[datetime | None]  = mapped_column(DateTime(timezone=True), nullable=True)
    api_token_enc:          Mapped[dict | None]      = mapped_column(PortableJSON(), nullable=True)
    connection_verified_at: Mapped[datetime | None]  = mapped_column(DateTime(timezone=True), nullable=True)
    delivery_method:        Mapped[str]              = mapped_column(String(20), nullable=False, default="manual")
    webhook_url:            Mapped[str | None]       = mapped_column(Text, nullable=True)
    webhook_secret:         Mapped[str | None]       = mapped_column(Text, nullable=True)
    import_api_key:         Mapped[str | None]       = mapped_column(Text, nullable=True)
    created_at:             Mapped[datetime]         = mapped_column(DateTime(timezone=True), default=_utcnow)


class DeploymentRunModel(Base):
    """One promotion run: package → environment with risk gate."""

    __tablename__ = "deployment_runs"
    __table_args__ = (
        Index("ix_dr_tenant", "tenant_id"),
        Index("ix_dr_status", "status"),
        Index("ix_dr_to_env", "to_env_id"),
    )

    id:               Mapped[uuid.UUID]      = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:        Mapped[str]            = mapped_column(String(255), nullable=False)
    package_id:       Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    from_env_id:      Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("environment_registry.id", ondelete="SET NULL"), nullable=True)
    to_env_id:        Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("environment_registry.id", ondelete="SET NULL"), nullable=True)
    risk_level:       Mapped[str]            = mapped_column(String(20), nullable=False, default="medium")
    risk_summary:     Mapped[dict]           = mapped_column(PortableJSON(), nullable=False, default=dict)
    status:           Mapped[str]            = mapped_column(String(50), nullable=False, default="pending")
    approval_case_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    approved_by:      Mapped[str | None]     = mapped_column(Text, nullable=True)
    rejected_by:      Mapped[str | None]     = mapped_column(Text, nullable=True)
    rejection_reason: Mapped[str | None]     = mapped_column(Text, nullable=True)
    initiated_by:     Mapped[str]            = mapped_column(Text, nullable=False)
    deploy_notes:     Mapped[str | None]     = mapped_column(Text, nullable=True)
    deployed_at:      Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at:       Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow)
    completed_at:     Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DeploymentWindowModel(Base):
    """Change window configuration per environment."""

    __tablename__ = "deployment_windows"
    __table_args__ = (
        Index("ix_dw_env", "env_id"),
    )

    id:             Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:      Mapped[str]       = mapped_column(String(255), nullable=False)
    env_id:         Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("environment_registry.id", ondelete="CASCADE"), nullable=True)
    name:           Mapped[str]       = mapped_column(Text, nullable=False)
    days_of_week:   Mapped[list]      = mapped_column(PortableJSON(), nullable=False, default=lambda: [0,1,2,3,4,5,6])
    start_hour_utc: Mapped[int]       = mapped_column(Integer, nullable=False, default=0)
    end_hour_utc:   Mapped[int]       = mapped_column(Integer, nullable=False, default=23)
    enabled:        Mapped[bool]      = mapped_column(Boolean, nullable=False, default=True)


class DeploymentHealthCheckModel(Base):
    """Post-deployment health probe result."""

    __tablename__ = "deployment_health_checks"
    __table_args__ = (
        Index("ix_dhc_run", "run_id"),
    )

    id:          Mapped[uuid.UUID]      = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    run_id:      Mapped[uuid.UUID]      = mapped_column(GUID(), ForeignKey("deployment_runs.id", ondelete="CASCADE"), nullable=False)
    check_url:   Mapped[str | None]     = mapped_column(Text, nullable=True)
    status_code: Mapped[int | None]     = mapped_column(Integer, nullable=True)
    response_ms: Mapped[int | None]     = mapped_column(Integer, nullable=True)
    healthy:     Mapped[bool | None]    = mapped_column(Boolean, nullable=True)
    error:       Mapped[str | None]     = mapped_column(Text, nullable=True)
    checked_at:  Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow)


# <<< P56 HxWork: Kanban + Sprint Board

class HxWorkBoardModel(Base):
    """A Kanban/Sprint board backed by a Helix case type."""

    __tablename__ = "hxwork_boards"
    __table_args__ = (
        Index("ix_board_tenant",    "tenant_id"),
        Index("ix_board_case_type", "case_type_id"),
    )

    id:            Mapped[uuid.UUID]      = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:     Mapped[str]            = mapped_column(String(255), nullable=False)
    name:          Mapped[str]            = mapped_column(Text, nullable=False)
    description:   Mapped[str | None]     = mapped_column(Text, nullable=True)
    case_type_id:  Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("case_types.id", ondelete="SET NULL"), nullable=True)
    artifact_type: Mapped[str | None]     = mapped_column(Text, nullable=True)
    artifact_id:   Mapped[str | None]     = mapped_column(Text, nullable=True)
    column_config: Mapped[list]           = mapped_column(PortableJSON(), nullable=False, default=list)
    created_by:    Mapped[str | None]     = mapped_column(Text, nullable=True)
    created_at:    Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow)

    sprints:   Mapped[list["HxWorkSprintModel"]]       = relationship(back_populates="board", cascade="all, delete-orphan")
    stories:   Mapped[list["HxWorkStoryModel"]]        = relationship(back_populates="board", cascade="all, delete-orphan")
    relations: Mapped[list["HxWorkCardRelationModel"]] = relationship(back_populates="board", cascade="all, delete-orphan")


class HxWorkSprintModel(Base):
    """A time-boxed sprint on an HxWork board."""

    __tablename__ = "hxwork_sprints"
    __table_args__ = (
        Index("ix_sprint_board",  "board_id"),
        Index("ix_sprint_status", "status"),
    )

    id:           Mapped[uuid.UUID]      = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    board_id:     Mapped[uuid.UUID]      = mapped_column(GUID(), ForeignKey("hxwork_boards.id", ondelete="CASCADE"), nullable=False)
    tenant_id:    Mapped[str]            = mapped_column(String(255), nullable=False)
    name:         Mapped[str]            = mapped_column(Text, nullable=False)
    goal:         Mapped[str | None]     = mapped_column(Text, nullable=True)
    status:       Mapped[str]            = mapped_column(String(20), nullable=False, default="planned")
    start_date:   Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_date:     Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    velocity:     Mapped[int | None]     = mapped_column(Integer, nullable=True)
    created_at:   Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    board: Mapped["HxWorkBoardModel"] = relationship(back_populates="sprints")
    cards: Mapped[list["HxWorkSprintCardModel"]] = relationship(back_populates="sprint", cascade="all, delete-orphan")


class HxWorkSprintCardModel(Base):
    """A case assigned to a sprint with story points."""

    __tablename__ = "hxwork_sprint_cards"

    sprint_id:    Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("hxwork_sprints.id", ondelete="CASCADE"), primary_key=True)
    case_id:      Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True)
    story_points: Mapped[int]       = mapped_column(Integer, default=0)
    added_at:     Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=_utcnow)

    sprint: Mapped["HxWorkSprintModel"] = relationship(back_populates="cards")


class HxWorkCardRelationModel(Base):
    """Dependency / blocking relationship between two cards (cases)."""

    __tablename__ = "hxwork_card_relations"
    __table_args__ = (
        Index("ix_cr_board", "board_id"),
        Index("ix_cr_from",  "from_case_id"),
        Index("ix_cr_to",    "to_case_id"),
    )

    id:            Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    board_id:      Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("hxwork_boards.id", ondelete="CASCADE"), nullable=False)
    from_case_id:  Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    to_case_id:    Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    relation_type: Mapped[str]       = mapped_column(String(30), nullable=False, default="blocks")
    created_at:    Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=_utcnow)

    board: Mapped["HxWorkBoardModel"] = relationship(back_populates="relations")


# ═══════════════════════════════════════════════════════════════════
# P61 — HxWork redesign + platform-wide Commit pattern
# ═══════════════════════════════════════════════════════════════════

class ComponentCommitModel(Base):
    """Immutable commit record for any saved change across the platform."""

    __tablename__ = "component_commits"
    __table_args__ = (
        Index("ix_cc_component", "component_type", "component_id"),
        Index("ix_cc_committed", "committed_at"),
        Index("ix_cc_user",      "committed_by"),
    )

    id:              Mapped[uuid.UUID]    = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    component_type:  Mapped[str]          = mapped_column(String(64), nullable=False)
    component_id:    Mapped[str]          = mapped_column(String(255), nullable=False)
    component_name:  Mapped[str]          = mapped_column(Text, nullable=False, default="")
    commit_message:  Mapped[str]          = mapped_column(Text, nullable=False)
    committed_by:    Mapped[str]          = mapped_column(String(255), nullable=False)
    diff_snapshot:   Mapped[dict | None]  = mapped_column(PortableJSON(), nullable=True)
    story_matches:   Mapped[list | None]  = mapped_column(PortableJSON(), nullable=True)
    committed_at:    Mapped[datetime]     = mapped_column(DateTime(timezone=True), default=_utcnow)


class HxWorkStoryModel(Base):
    """A user story on a dev board — tracks development work, not case instances."""

    __tablename__ = "hxwork_stories"
    __table_args__ = (
        Index("ix_hws_board",  "board_id"),
        Index("ix_hws_sprint", "sprint_id"),
        Index("ix_hws_status", "status"),
        Index("ix_hws_branch", "branch_id"),
    )

    id:                  Mapped[uuid.UUID]      = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    board_id:            Mapped[uuid.UUID]      = mapped_column(GUID(), ForeignKey("hxwork_boards.id", ondelete="CASCADE"), nullable=False)
    sprint_id:           Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("hxwork_sprints.id", ondelete="SET NULL"), nullable=True)
    branch_id:           Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("artifact_branches.id", ondelete="SET NULL"), nullable=True)
    branch_name:         Mapped[str | None]     = mapped_column(Text, nullable=True)
    title:               Mapped[str]            = mapped_column(Text, nullable=False)
    description:         Mapped[str | None]     = mapped_column(Text, nullable=True)
    acceptance_criteria: Mapped[str | None]     = mapped_column(Text, nullable=True)
    status:              Mapped[str]            = mapped_column(String(30), nullable=False, default="backlog")
    story_points:        Mapped[int | None]     = mapped_column(Integer, nullable=True)
    assigned_to:         Mapped[str | None]     = mapped_column(Text, nullable=True)
    linked_commit_ids:   Mapped[list]           = mapped_column(PortableJSON(), nullable=False, default=list)
    created_by:          Mapped[str]            = mapped_column(Text, nullable=False, default="system")
    created_at:          Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at:          Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    board: Mapped["HxWorkBoardModel"] = relationship(back_populates="stories")


class HxWorkStoryRelationModel(Base):
    """Blocking / dependency relation between two user stories."""

    __tablename__ = "hxwork_story_relations"
    __table_args__ = (
        Index("ix_hws_rel_board", "board_id"),
    )

    id:         Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    board_id:   Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("hxwork_boards.id", ondelete="CASCADE"), nullable=False)
    from_story: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("hxwork_stories.id", ondelete="CASCADE"), nullable=False)
    to_story:   Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("hxwork_stories.id", ondelete="CASCADE"), nullable=False)
    relation:   Mapped[str]       = mapped_column(String(30), nullable=False, default="blocks")

# <<< P61 HxWork + Commit

# >>> P57 HxCanvas: Visual Whiteboard

class HxCanvasBoardModel(Base):
    """An infinite-canvas whiteboard board."""

    __tablename__ = "hxcanvas_boards"
    __table_args__ = (
        Index("ix_hxcb_tenant", "tenant_id"),
        Index("ix_hxcb_case",   "case_id"),
    )

    id:          Mapped[uuid.UUID]      = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:   Mapped[str]            = mapped_column(String(255), nullable=False)
    name:        Mapped[str]            = mapped_column(Text, nullable=False)
    description: Mapped[str | None]     = mapped_column(Text, nullable=True)
    case_id:     Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("case_instances.id", ondelete="SET NULL"), nullable=True)
    created_by:  Mapped[str | None]     = mapped_column(Text, nullable=True)
    created_at:  Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at:  Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    items: Mapped[list["HxCanvasItemModel"]] = relationship(back_populates="board", cascade="all, delete-orphan")


class HxCanvasItemModel(Base):
    """A single item on an HxCanvas board (polymorphic via type + data jsonb)."""

    __tablename__ = "hxcanvas_items"
    __table_args__ = (
        Index("ix_hxci_board",  "board_id"),
        Index("ix_hxci_tenant", "tenant_id"),
        Index("ix_hxci_type",   "type"),
    )

    id:         Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    board_id:   Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("hxcanvas_boards.id", ondelete="CASCADE"), nullable=False)
    tenant_id:  Mapped[str]       = mapped_column(String(255), nullable=False)
    type:       Mapped[str]       = mapped_column(String(30), nullable=False)
    x:          Mapped[float]     = mapped_column(Float, nullable=False, default=0.0)
    y:          Mapped[float]     = mapped_column(Float, nullable=False, default=0.0)
    width:      Mapped[float]     = mapped_column(Float, nullable=False, default=120.0)
    height:     Mapped[float]     = mapped_column(Float, nullable=False, default=60.0)
    data:       Mapped[dict]      = mapped_column(PortableJSON(), nullable=False, default=dict)
    z_index:    Mapped[int]       = mapped_column(Integer, nullable=False, default=0)
    created_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    board: Mapped["HxCanvasBoardModel"] = relationship(back_populates="items")

# <<< P57 HxCanvas: Visual Whiteboard


# >>> P58 HxDocs: Living Documentation

class HxDocsSpaceModel(Base):
    """A documentation space (like a Confluence space)."""

    __tablename__ = "hxdocs_spaces"
    __table_args__ = (
        Index("ix_hxds_tenant", "tenant_id"),
    )

    id:          Mapped[uuid.UUID]  = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:   Mapped[str]        = mapped_column(String(255), nullable=False)
    name:        Mapped[str]        = mapped_column(Text, nullable=False)
    slug:        Mapped[str]        = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_public:   Mapped[bool]       = mapped_column(Boolean, nullable=False, default=False)
    created_by:  Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at:  Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_utcnow)

    articles: Mapped[list["HxDocsArticleModel"]] = relationship(back_populates="space", cascade="all, delete-orphan")


class HxDocsArticleModel(Base):
    """A living documentation article with block-based content."""

    __tablename__ = "hxdocs_articles"
    __table_args__ = (
        Index("ix_hxda_space",  "space_id"),
        Index("ix_hxda_tenant", "tenant_id"),
        Index("ix_hxda_status", "status"),
        Index("ix_hxda_public", "is_public"),
    )

    id:              Mapped[uuid.UUID]  = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    space_id:        Mapped[uuid.UUID]  = mapped_column(GUID(), ForeignKey("hxdocs_spaces.id", ondelete="CASCADE"), nullable=False)
    tenant_id:       Mapped[str]        = mapped_column(String(255), nullable=False)
    title:           Mapped[str]        = mapped_column(Text, nullable=False)
    slug:            Mapped[str]        = mapped_column(String(200), nullable=False)
    content:         Mapped[list]       = mapped_column(PortableJSON(), nullable=False, default=list)
    status:          Mapped[str]        = mapped_column(String(20), nullable=False, default="draft")
    is_public:       Mapped[bool]       = mapped_column(Boolean, nullable=False, default=False)
    auto_generated:  Mapped[bool]       = mapped_column(Boolean, nullable=False, default=False)
    source_concept:  Mapped[str | None] = mapped_column(Text, nullable=True)
    word_count:      Mapped[int]        = mapped_column(Integer, nullable=False, default=0)
    version:         Mapped[int]        = mapped_column(Integer, nullable=False, default=1)
    package_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    tags:            Mapped[list]       = mapped_column(PortableJSON(), nullable=False, default=list)
    created_by:      Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_by:      Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at:      Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at:      Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    space:    Mapped["HxDocsSpaceModel"]              = relationship(back_populates="articles")
    versions: Mapped[list["HxDocsArticleVersionModel"]] = relationship(back_populates="article", cascade="all, delete-orphan")


class HxDocsArticleVersionModel(Base):
    """Snapshot of an article at a specific version."""

    __tablename__ = "hxdocs_article_versions"
    __table_args__ = (
        Index("ix_hxdav_article", "article_id"),
        Index("ix_hxdav_version", "version"),
    )

    id:              Mapped[uuid.UUID]  = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    article_id:      Mapped[uuid.UUID]  = mapped_column(GUID(), ForeignKey("hxdocs_articles.id", ondelete="CASCADE"), nullable=False)
    tenant_id:       Mapped[str]        = mapped_column(String(255), nullable=False)
    version:         Mapped[int]        = mapped_column(Integer, nullable=False)
    title:           Mapped[str]        = mapped_column(Text, nullable=False)
    content:         Mapped[list]       = mapped_column(PortableJSON(), nullable=False, default=list)
    package_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    saved_by:        Mapped[str | None] = mapped_column(Text, nullable=True)
    saved_at:        Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_utcnow)

    article: Mapped["HxDocsArticleModel"] = relationship(back_populates="versions")

# <<< P58 HxDocs: Living Documentation


# ═══════════════════════════════════════════════════════════════════
# P60 HxBranch — Artifact Version Control & Live Environment Sync
# ═══════════════════════════════════════════════════════════════════

class ArtifactBranchModel(Base):
    """A named branch of a Helix artifact (case type, form, etc.) or full app package,
    pulled from a remote environment for review and merge into dev main."""

    __tablename__ = "artifact_branches"
    __table_args__ = (
        Index("ix_artifact_branches_status",     "status"),
        Index("ix_artifact_branches_source_env", "source_env_id"),
        Index("ix_artifact_branches_type",       "branch_type", "artifact_type"),
        Index("ix_artifact_branches_created",    "created_at"),
    )

    id:                    Mapped[uuid.UUID]      = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    name:                  Mapped[str]            = mapped_column(Text, nullable=False)
    description:           Mapped[str | None]     = mapped_column(Text, nullable=True)
    branch_type:           Mapped[str]            = mapped_column(String(20),  nullable=False, default="artifact")
    artifact_type:         Mapped[str | None]     = mapped_column(String(50),  nullable=True)
    artifact_id:           Mapped[str | None]     = mapped_column(Text, nullable=True)
    app_package_id:        Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("app_packages.id", ondelete="SET NULL"), nullable=True)
    source_env_id:         Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("environment_registry.id", ondelete="SET NULL"), nullable=True)
    source_env_name:       Mapped[str]            = mapped_column(Text, nullable=False, default="unknown")
    status:                Mapped[str]            = mapped_column(String(30),  nullable=False, default="open")
    content_snapshot:      Mapped[dict]           = mapped_column(PortableJSON(), nullable=False, default=dict)
    base_snapshot:         Mapped[dict]           = mapped_column(PortableJSON(), nullable=False, default=dict)
    conflict_detected:     Mapped[bool]           = mapped_column(Boolean, nullable=False, default=False)
    merge_diff:            Mapped[dict | None]    = mapped_column(PortableJSON(), nullable=True)
    # v2 ownership + SOD fields (migration 042)
    owner_id:              Mapped[str | None]     = mapped_column(Text, nullable=True)
    assigned_reviewer_id:  Mapped[str | None]     = mapped_column(Text, nullable=True)
    access_group_id:       Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    created_by:            Mapped[str]            = mapped_column(Text, nullable=False, default="system")
    reviewed_by:           Mapped[str | None]     = mapped_column(Text, nullable=True)
    merged_by:             Mapped[str | None]     = mapped_column(Text, nullable=True)
    created_at:            Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at:            Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
    merged_at:             Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    reviews: Mapped[list["BranchReviewModel"]] = relationship(
        "BranchReviewModel", back_populates="branch", cascade="all, delete-orphan",
    )


class BranchReviewModel(Base):
    """A reviewer's decision on a branch (approve / reject / changes_requested)."""

    __tablename__ = "branch_reviews"
    __table_args__ = (
        Index("ix_branch_reviews_branch",  "branch_id"),
        Index("ix_branch_reviews_created", "created_at"),
    )

    id:          Mapped[uuid.UUID]  = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    branch_id:   Mapped[uuid.UUID]  = mapped_column(GUID(), ForeignKey("artifact_branches.id", ondelete="CASCADE"), nullable=False)
    reviewer_id: Mapped[str]        = mapped_column(Text, nullable=False)
    decision:    Mapped[str]        = mapped_column(String(30), nullable=False)
    comments:    Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at:  Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_utcnow)

    branch: Mapped["ArtifactBranchModel"] = relationship(back_populates="reviews")


class BranchAuditEventModel(Base):
    """Immutable audit trail — one row per lifecycle event on a branch.

    Event types: branch_created, submitted_for_review, recalled,
    reviewed, merged, content_saved, reverted_to_base, branch_deleted.
    """

    __tablename__ = "branch_audit_events"
    __table_args__ = (
        Index("ix_bae_branch",  "branch_id"),
        Index("ix_bae_created", "created_at"),
    )

    id:             Mapped[uuid.UUID]  = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    branch_id:      Mapped[uuid.UUID]  = mapped_column(GUID(), nullable=False)
    event_type:     Mapped[str]        = mapped_column(String(60), nullable=False)
    actor_id:       Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_name:     Mapped[str | None] = mapped_column(Text, nullable=True)
    event_metadata: Mapped[dict]       = mapped_column("metadata", PortableJSON(), nullable=False, default=dict)
    created_at:     Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_utcnow)

# <<< P60 HxBranch


# ═══════════════════════════════════════════════════════════════════
# P64 — Real Authentication (ENH-10)
# ═══════════════════════════════════════════════════════════════════

class HelixUserModel(Base):
    """A real authenticated user with bcrypt password, lockout, and MFA support."""

    __tablename__ = "helix_users"
    __table_args__ = (
        Index("ix_helix_users_email",    "email",    unique=True),
        Index("ix_helix_users_username", "username", unique=True),
        Index("ix_helix_users_sso",      "sso_provider", "sso_subject"),
    )

    id:                       Mapped[uuid.UUID]      = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    username:                 Mapped[str]            = mapped_column(String(255), nullable=False, unique=True)
    email:                    Mapped[str]            = mapped_column(String(255), nullable=False, unique=True)
    display_name:             Mapped[str | None]     = mapped_column(Text, nullable=True)
    password_hash:            Mapped[str | None]     = mapped_column(Text, nullable=True)
    roles:                    Mapped[list]           = mapped_column(PortableJSON(), nullable=False, default=list)
    is_superadmin:            Mapped[bool]           = mapped_column(Boolean, nullable=False, default=False)
    is_active:                Mapped[bool]           = mapped_column(Boolean, nullable=False, default=True)
    failed_attempts:          Mapped[int]            = mapped_column(Integer, nullable=False, default=0)
    locked_until:             Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    password_change_required: Mapped[bool]           = mapped_column(Boolean, nullable=False, default=False)
    mfa_enabled:              Mapped[bool]           = mapped_column(Boolean, nullable=False, default=False)
    mfa_secret_enc:           Mapped[dict | None]    = mapped_column(PortableJSON(), nullable=True)
    sso_provider:             Mapped[str | None]     = mapped_column(String(255), nullable=True)
    sso_subject:              Mapped[str | None]     = mapped_column(String(255), nullable=True)
    created_at:               Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at:               Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
    last_login_at:            Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    otps: Mapped[list["AuthOtpModel"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class AuthOtpModel(Base):
    """Short-lived single-use OTP for password reset and MFA enrolment."""

    __tablename__ = "auth_otp"
    __table_args__ = (
        Index("ix_auth_otp_user", "user_id"),
    )

    id:         Mapped[uuid.UUID]      = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    user_id:    Mapped[uuid.UUID]      = mapped_column(GUID(), ForeignKey("helix_users.id", ondelete="CASCADE"), nullable=False)
    otp_hash:   Mapped[str]            = mapped_column(Text, nullable=False)
    purpose:    Mapped[str]            = mapped_column(String(30), nullable=False)
    expires_at: Mapped[datetime]       = mapped_column(DateTime(timezone=True), nullable=False)
    used_at:    Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow)

    user: Mapped["HelixUserModel"] = relationship(back_populates="otps")


class SsoProviderModel(Base):
    """SSO/OAuth2 provider configuration per tenant."""

    __tablename__ = "sso_providers"
    __table_args__ = (
        Index("ix_sso_tenant", "tenant_id", "provider"),
    )

    id:                Mapped[uuid.UUID]   = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:         Mapped[str | None]  = mapped_column(String(255), nullable=True)
    provider:          Mapped[str]         = mapped_column(String(30), nullable=False)
    client_id:         Mapped[str]         = mapped_column(Text, nullable=False)
    client_secret_enc: Mapped[dict | None] = mapped_column(PortableJSON(), nullable=True)
    enabled:           Mapped[bool]        = mapped_column(Boolean, nullable=False, default=True)
    config:            Mapped[dict]        = mapped_column(PortableJSON(), nullable=False, default=dict)
    created_at:        Mapped[datetime]    = mapped_column(DateTime(timezone=True), default=_utcnow)

# <<< P64 Real Auth


# >>> Connector Intelligence (outbound rules + form audit)

class OutboundConnectorRuleModel(Base):
    """Outbound trigger rule: case event → fire a connector."""

    __tablename__ = "outbound_connector_rules"
    __table_args__ = (
        Index("ix_ocr_tenant",    "tenant_id"),
        Index("ix_ocr_connector", "connector_id"),
        Index("ix_ocr_enabled",   "enabled"),
    )

    id:              Mapped[uuid.UUID]      = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:       Mapped[str]            = mapped_column(String(255), nullable=False)
    name:            Mapped[str]            = mapped_column(Text, nullable=False)
    trigger_event:   Mapped[str]            = mapped_column(String(50), nullable=False)
    # stage_enter | stage_exit | step_complete | field_change | case_created
    case_type_id:    Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    condition_expr:  Mapped[dict | None]    = mapped_column(PortableJSON(), nullable=True)
    connector_id:    Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("connector_registry.id", ondelete="SET NULL"), nullable=True
    )
    input_mapping:   Mapped[dict]           = mapped_column(PortableJSON(), nullable=False, default=dict)
    enabled:         Mapped[bool]           = mapped_column(Boolean, nullable=False, default=True)
    created_at:      Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at:      Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class FieldPopulationAuditModel(Base):
    """Audit record for every form field value populated from a connector."""

    __tablename__ = "field_population_audit"
    __table_args__ = (
        Index("ix_fpa_case",      "case_id"),
        Index("ix_fpa_connector", "connector_id"),
        Index("ix_fpa_user",      "user_id"),
    )

    id:            Mapped[uuid.UUID]      = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:     Mapped[str]            = mapped_column(String(255), nullable=False)
    case_id:       Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    form_id:       Mapped[str | None]     = mapped_column(String(255), nullable=True)
    field_key:     Mapped[str]            = mapped_column(String(255), nullable=False)
    connector_id:  Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("connector_registry.id", ondelete="SET NULL"), nullable=True
    )
    user_id:       Mapped[str | None]     = mapped_column(String(255), nullable=True)
    response_hash: Mapped[str | None]     = mapped_column(String(64), nullable=True)
    populated_at:  Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow)

# <<< Connector Intelligence


# ═══════════════════════════════════════════════════════════════════
#  Marketplace (P-Marketplace)
# ═══════════════════════════════════════════════════════════════════

class MarketplaceSourceModel(Base):
    """A registered package source — publisher's velaris.json URL.

    Each source is fetched independently on its own poll interval.
    Official sources (tier='official') are pre-seeded; community/private
    sources are added by admins. Tokens are AES-encrypted at rest.
    """
    __tablename__ = "marketplace_sources"
    __table_args__ = (
        UniqueConstraint("url", name="uq_mks_url"),
        Index("ix_mks_tier", "tier"),
    )

    id:                   Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    name:                 Mapped[str]       = mapped_column(String(255), nullable=False)
    # velaris.json URL. UNIQUE (uq_mks_url). URLs are ASCII, so on MySQL use an ascii
    # charset (1 byte/char) → the unique index fits the 3072-byte key limit at full
    # 1024-char length, keeping true uniqueness without a prefix.
    url:                  Mapped[str]       = mapped_column(
        String(1024).with_variant(mysql.VARCHAR(1024, charset="ascii"), "mysql"), nullable=False)
    tier:                 Mapped[str]       = mapped_column(String(32), default="community")
    # tier: official | community | private
    token_enc:            Mapped[str | None]= mapped_column(Text, nullable=True)            # AES-encrypted PAT
    poll_interval_hours:  Mapped[int]       = mapped_column(Integer, default=6)
    enabled:              Mapped[bool]      = mapped_column(Boolean, default=True)
    last_polled_at:       Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error:           Mapped[str | None]= mapped_column(Text, nullable=True)
    package_count:        Mapped[int]       = mapped_column(Integer, default=0)
    added_by:             Mapped[str]       = mapped_column(String(255), nullable=False)
    created_at:           Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=_utcnow)


class MarketplacePackageCacheModel(Base):
    """Local cache of packages fetched from all registered sources."""
    __tablename__ = "marketplace_packages"

    id:               Mapped[str]           = mapped_column(String(255), primary_key=True)  # e.g. "velaris/stripe-connector"
    name:             Mapped[str]           = mapped_column(String(255), nullable=False)
    description:      Mapped[str]           = mapped_column(Text, nullable=False)
    package_type:     Mapped[str]           = mapped_column(String(64), nullable=False)      # connector | case_template | …
    category:         Mapped[str]           = mapped_column(String(128), nullable=False)
    publisher:        Mapped[str]           = mapped_column(String(255), nullable=False)
    publisher_tier:   Mapped[str]           = mapped_column(String(32), nullable=False)      # official | community
    version:          Mapped[str]           = mapped_column(String(64), nullable=False)
    price:            Mapped[str]           = mapped_column(String(32), nullable=False)      # free | paid
    price_label:      Mapped[str | None]    = mapped_column(String(64), nullable=True)
    contact_url:      Mapped[str | None]    = mapped_column(String(512), nullable=True)
    rating:           Mapped[float]         = mapped_column(Float, default=0.0)
    installs:         Mapped[int]           = mapped_column(Integer, default=0)
    download_url:     Mapped[str]           = mapped_column(String(512), nullable=False)
    checksum_sha256:  Mapped[str]           = mapped_column(String(64), nullable=False)
    outbound_domains: Mapped[str]           = mapped_column(Text, default="[]")              # JSON array
    tags:             Mapped[str]           = mapped_column(Text, default="[]")              # JSON array
    icon_color:       Mapped[str | None]    = mapped_column(String(16), nullable=True)
    icon_letter:      Mapped[str | None]    = mapped_column(String(8), nullable=True)
    min_platform_version: Mapped[str]       = mapped_column(String(32), default="1.0.0")
    updated_at:       Mapped[str | None]    = mapped_column(String(32), nullable=True)
    release_notes:    Mapped[str | None]    = mapped_column(Text, nullable=True)
    all_versions:     Mapped[str]           = mapped_column(Text, default="[]")              # JSON: full version history from velaris.json
    source_id:        Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("marketplace_sources.id", ondelete="SET NULL"), nullable=True
    )
    fetched_at:       Mapped[datetime]      = mapped_column(DateTime(timezone=True), default=_utcnow)


class MarketplaceWorkspaceModel(Base):
    """Sandbox workspace — one isolated Docker container, N packages, one admin review."""
    __tablename__ = "marketplace_workspaces"
    __table_args__ = (
        Index("ix_mw_tenant",  "tenant_id"),
        Index("ix_mw_user",    "created_by"),
        Index("ix_mw_status",  "status"),
    )

    id:           Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:    Mapped[str]       = mapped_column(String(255), nullable=False)
    name:         Mapped[str]       = mapped_column(String(255), nullable=False)
    status:       Mapped[str]       = mapped_column(String(32), default="active")
    # status: active | submitted | approved | rejected | expired | destroyed
    dataset_id:   Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)           # admin-defined dataset
    container_id: Mapped[str | None]       = mapped_column(String(255), nullable=True)      # Docker container ID
    created_by:   Mapped[str]       = mapped_column(String(255), nullable=False)
    reviewed_by:  Mapped[str | None]= mapped_column(String(255), nullable=True)
    review_note:  Mapped[str | None]= mapped_column(Text, nullable=True)
    created_at:   Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=_utcnow)
    expires_at:   Mapped[datetime]  = mapped_column(DateTime(timezone=True), nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_at:  Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Conformance gate (#27 Phase C): none|legacy_unverified|unverified|structural_passed|full_passed
    conformance_status:     Mapped[str]              = mapped_column(String(30), default="none", nullable=False)
    conformance_run_id:     Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    conformance_checked_at: Mapped[datetime | None]  = mapped_column(DateTime(timezone=True), nullable=True)


class MarketplaceWorkspaceItemModel(Base):
    """A package installed inside a sandbox workspace."""
    __tablename__ = "marketplace_workspace_items"
    __table_args__ = (
        Index("ix_mwi_workspace", "workspace_id"),
    )

    id:              Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    workspace_id:    Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("marketplace_workspaces.id", ondelete="CASCADE"), nullable=False)
    package_id:      Mapped[str]       = mapped_column(String(255), nullable=False)
    package_version: Mapped[str]       = mapped_column(String(64), nullable=False)
    status:          Mapped[str]       = mapped_column(String(32), default="installed")
    # status: installed | approved | rejected
    licence_key_enc: Mapped[str | None]= mapped_column(Text, nullable=True)                 # AES-encrypted licence key
    installed_at:    Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=_utcnow)
    approved_at:     Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MarketplaceInstallModel(Base):
    """Production-approved package install per tenant."""
    __tablename__ = "marketplace_installs"
    __table_args__ = (
        Index("ix_mi_tenant",  "tenant_id"),
        UniqueConstraint("tenant_id", "package_id", name="uq_mi_tenant_package"),
    )

    id:              Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:       Mapped[str]       = mapped_column(String(255), nullable=False)
    package_id:      Mapped[str]       = mapped_column(String(255), nullable=False)
    package_version: Mapped[str]       = mapped_column(String(64), nullable=False)
    package_type:    Mapped[str]       = mapped_column(String(64), nullable=False)
    licence_key_enc: Mapped[str | None]= mapped_column(Text, nullable=True)
    licence_expires: Mapped[str | None]= mapped_column(String(32), nullable=True)           # ISO date string
    approved_by:     Mapped[str]       = mapped_column(String(255), nullable=False)
    workspace_id:    Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    installed_at:    Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=_utcnow)
    revoked_at:      Mapped[datetime | None]  = mapped_column(DateTime(timezone=True), nullable=True)


class MarketplaceNetworkLogModel(Base):
    """Every outbound call attempt from a sandbox container — blocked or allowed."""
    __tablename__ = "marketplace_network_log"
    __table_args__ = (
        Index("ix_mnl_workspace", "workspace_id"),
        Index("ix_mnl_package",   "package_id"),
    )

    id:              Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    workspace_id:    Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("marketplace_workspaces.id", ondelete="CASCADE"), nullable=False)
    package_id:      Mapped[str]       = mapped_column(String(255), nullable=False)
    destination_url: Mapped[str]       = mapped_column(String(1024), nullable=False)
    destination_ip:  Mapped[str | None]= mapped_column(String(64), nullable=True)
    http_method:     Mapped[str | None]= mapped_column(String(16), nullable=True)
    bytes_sent:      Mapped[int]       = mapped_column(Integer, default=0)
    bytes_received:  Mapped[int]       = mapped_column(Integer, default=0)
    status:          Mapped[str]       = mapped_column(String(16), nullable=False)           # blocked | allowed
    http_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_declared:     Mapped[bool]      = mapped_column(Boolean, default=True)               # False = undeclared domain → SECURITY VIOLATION
    created_at:      Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=_utcnow)


class MarketplaceWhitelistModel(Base):
    """Admin-approved outbound domain whitelist per workspace."""
    __tablename__ = "marketplace_whitelist"
    __table_args__ = (
        Index("ix_mwl_workspace", "workspace_id"),
    )

    id:            Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    workspace_id:  Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("marketplace_workspaces.id", ondelete="CASCADE"), nullable=False)
    package_id:    Mapped[str]       = mapped_column(String(255), nullable=False)
    domain:        Mapped[str]       = mapped_column(String(255), nullable=False)
    justification: Mapped[str | None]= mapped_column(Text, nullable=True)
    status:        Mapped[str]       = mapped_column(String(32), default="pending")         # pending | approved | denied
    requested_by:  Mapped[str]       = mapped_column(String(255), nullable=False)
    decided_by:    Mapped[str | None]= mapped_column(String(255), nullable=True)
    created_at:    Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=_utcnow)
    decided_at:    Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MarketplaceUpdateModel(Base):
    """Detected available update for an installed package.

    Created when a poll finds a newer version than what's installed.
    'fast_track' = True means no new outbound domains — admin can approve directly.
    'fast_track' = False means new/changed domains — full sandbox required.
    """
    __tablename__ = "marketplace_updates"
    __table_args__ = (
        UniqueConstraint("tenant_id", "package_id", name="uq_mku_tenant_package"),
        Index("ix_mku_tenant", "tenant_id"),
    )

    id:                   Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:            Mapped[str]       = mapped_column(String(255), nullable=False)
    package_id:           Mapped[str]       = mapped_column(String(255), nullable=False)
    installed_version:    Mapped[str]       = mapped_column(String(64), nullable=False)
    available_version:    Mapped[str]       = mapped_column(String(64), nullable=False)
    release_notes:        Mapped[str | None]= mapped_column(Text, nullable=True)
    new_outbound_domains: Mapped[str]       = mapped_column(Text, default="[]")             # JSON: domains added in new version
    fast_track:           Mapped[bool]      = mapped_column(Boolean, default=True)          # True = no new domains, direct approve
    status:               Mapped[str]       = mapped_column(String(32), default="pending")  # pending | approved | dismissed
    detected_at:          Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=_utcnow)
    approved_at:          Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by:          Mapped[str | None]= mapped_column(String(255), nullable=True)


class MarketplaceBlacklistModel(Base):
    """Tenant-managed blacklist — admin blocks packages/orgs/sources locally.

    Checked at install time and on every poll cycle.
    Tier: global blacklist (from GitHub) + per-tenant DB blacklist both enforced.
    type: 'org' | 'source' | 'package'
    """
    __tablename__ = "marketplace_blacklist"
    __table_args__ = (
        Index("ix_mbl_tenant", "tenant_id"),
        Index("ix_mbl_type_value", "type", "value", mysql_length={"value": 500}),
    )

    id:              Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:       Mapped[str]       = mapped_column(String(255), nullable=False)
    type:            Mapped[str]       = mapped_column(String(32), nullable=False)   # org | source | package
    value:           Mapped[str]       = mapped_column(String(1024), nullable=False) # org name / source URL / package ID
    reason:          Mapped[str]       = mapped_column(Text, nullable=False)
    blacklisted_by:  Mapped[str]       = mapped_column(String(255), nullable=False)
    notify_velaris:  Mapped[bool]      = mapped_column(Boolean, default=False)       # report to Velaris security webhook
    created_at:      Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=_utcnow)


class MarketplaceAccessRuleModel(Base):
    """Per-access-group install restrictions.

    Controls which packages a developer in a given access group can install into sandbox.
    rule_type:
      'allow_all'           — no restriction (default)
      'official_only'       — only Official tier packages
      'allowlist'           — only specific package IDs in allowed_package_ids
      'blocklist'           — all except specific package IDs in blocked_package_ids
    """
    __tablename__ = "marketplace_access_rules"
    __table_args__ = (
        UniqueConstraint("tenant_id", "access_group_id", name="uq_mar_tenant_group"),
        Index("ix_mar_tenant", "tenant_id"),
    )

    id:                  Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:           Mapped[str]       = mapped_column(String(255), nullable=False)
    access_group_id:     Mapped[str]       = mapped_column(String(255), nullable=False)
    rule_type:           Mapped[str]       = mapped_column(String(32), default="allow_all")
    allowed_package_ids: Mapped[str]       = mapped_column(Text, default="[]")   # JSON array
    blocked_package_ids: Mapped[str]       = mapped_column(Text, default="[]")   # JSON array
    updated_by:          Mapped[str]       = mapped_column(String(255), nullable=False, default="system")
    updated_at:          Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=_utcnow)


class MarketplaceReleaseRequestModel(Base):
    """Official package flagged for inclusion in the next HxDeploy release cycle.

    Created when a developer installs an official package in dev.
    HxDeploy reads pending requests when building the next deployment package.
    Never installed directly to production — always travels through the deployment pipeline.
    """
    __tablename__ = "marketplace_release_requests"
    __table_args__ = (
        Index("ix_mrr_tenant", "tenant_id"),
        UniqueConstraint("tenant_id", "package_id", "status", name="uq_mrr_tenant_package_status"),
    )

    id:              Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:       Mapped[str]       = mapped_column(String(255), nullable=False)
    package_id:      Mapped[str]       = mapped_column(String(255), nullable=False)
    package_version: Mapped[str]       = mapped_column(String(64), nullable=False)
    requested_by:    Mapped[str]       = mapped_column(String(255), nullable=False)
    status:          Mapped[str]       = mapped_column(String(32), default="pending")
    # status: pending | included_in_release | deployed | cancelled
    included_in_deploy_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at:      Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=_utcnow)
    deployed_at:     Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MarketplaceSandboxDatasetModel(Base):
    """Admin-defined named sandbox datasets (Option B synthetic data)."""
    __tablename__ = "marketplace_sandbox_datasets"

    id:          Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:   Mapped[str]       = mapped_column(String(255), nullable=False)
    name:        Mapped[str]       = mapped_column(String(255), nullable=False)
    description: Mapped[str | None]= mapped_column(Text, nullable=True)
    data_json:   Mapped[str]       = mapped_column(Text, default="{}")                      # serialized synthetic records
    created_by:  Mapped[str]       = mapped_column(String(255), nullable=False)
    created_at:  Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=_utcnow)


class DbManagerQueryLogModel(Base):
    """HxDBManager — audit log of every SQL query run through the DB Manager."""
    __tablename__ = "db_manager_query_log"
    __table_args__ = (
        Index("ix_dbmgr_log_tenant",  "tenant_id"),
        Index("ix_dbmgr_log_user",    "user_id"),
        Index("ix_dbmgr_log_ran_at",  "ran_at"),
        Index("ix_dbmgr_log_hash",    "query_hash"),
    )

    id:            Mapped[uuid.UUID]      = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:     Mapped[str]            = mapped_column(String(255), nullable=False)
    user_id:       Mapped[str]            = mapped_column(String(255), nullable=False)
    query_text:    Mapped[str]            = mapped_column(Text, nullable=False)
    query_hash:    Mapped[str]            = mapped_column(String(64), nullable=False)
    duration_ms:   Mapped[int | None]     = mapped_column(Integer, nullable=True)
    rows_affected: Mapped[int | None]     = mapped_column(Integer, nullable=True)
    status:        Mapped[str]            = mapped_column(String(20), nullable=False, default="success")
    error_detail:  Mapped[str | None]     = mapped_column(Text, nullable=True)
    ran_at:        Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow)


class RevokedSessionModel(Base):
    """Instantly revoked JWT tokens — checked on every auth request via in-memory cache."""
    __tablename__ = "revoked_sessions"
    __table_args__ = (
        Index("ix_revoked_sessions_user",    "user_id"),
        Index("ix_revoked_sessions_expires", "expires_at"),
    )

    token_hash: Mapped[str]            = mapped_column(String(64), primary_key=True)
    user_id:    Mapped[str]            = mapped_column(String(255), nullable=False)
    revoked_at: Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow)
    reason:     Mapped[str | None]     = mapped_column(Text, nullable=True)
    revoked_by: Mapped[str | None]     = mapped_column(String(255), nullable=True)
    expires_at: Mapped[datetime]       = mapped_column(DateTime(timezone=True), nullable=False)


class RefreshTokenModel(Base):
    """Opaque refresh tokens — stored hashed, rotated on every use."""
    __tablename__ = "refresh_tokens"
    __table_args__ = (
        Index("ix_refresh_tokens_user",    "user_id"),
        Index("ix_refresh_tokens_expires", "expires_at"),
        Index("ix_refresh_tokens_device",  "device_id"),
    )

    token_hash:  Mapped[str]           = mapped_column(String(64), primary_key=True)
    user_id:     Mapped[str]           = mapped_column(String(255), nullable=False)
    jti:         Mapped[str]           = mapped_column(String(36), nullable=False)
    expires_at:  Mapped[datetime]      = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at:  Mapped[datetime|None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by:  Mapped[str|None]      = mapped_column(String(255), nullable=True)
    created_at:  Mapped[datetime]      = mapped_column(DateTime(timezone=True), default=_utcnow)
    # Group J: binds the token chain to the auth_devices row it was issued on
    device_id:   Mapped[uuid.UUID|None] = mapped_column(GUID(), nullable=True)


# >>> Group J: device-bound sessions + WebAuthn passkeys
class AuthDeviceModel(Base):
    """One row per browser/machine a user signs in from.

    Refresh tokens carry this row's id; revoking the device revokes the
    whole chain. user_agent_hash covers browser family + OS only (not the
    version), so routine browser updates don't invalidate sessions, but a
    refresh token replayed from different software dies on first use.
    """
    __tablename__ = "auth_devices"
    __table_args__ = (
        Index("ix_auth_devices_user", "user_id"),
    )

    id:              Mapped[uuid.UUID]     = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    user_id:         Mapped[str]           = mapped_column(String(255), nullable=False)
    device_name:     Mapped[str]           = mapped_column(String(255), default="Unknown device")
    user_agent_hash: Mapped[str]           = mapped_column(String(64), nullable=False)
    first_ip:        Mapped[str|None]      = mapped_column(String(64), nullable=True)
    last_ip:         Mapped[str|None]      = mapped_column(String(64), nullable=True)
    created_at:      Mapped[datetime]      = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_seen_at:    Mapped[datetime]      = mapped_column(DateTime(timezone=True), default=_utcnow)
    revoked_at:      Mapped[datetime|None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by:      Mapped[str|None]      = mapped_column(String(255), nullable=True)


class WebAuthnCredentialModel(Base):
    """FIDO2 passkey public keys. Private keys never leave the authenticator."""
    __tablename__ = "webauthn_credentials"
    __table_args__ = (
        Index("ix_webauthn_creds_user", "user_id"),
    )

    id:            Mapped[uuid.UUID]     = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    user_id:       Mapped[str]           = mapped_column(String(255), nullable=False)
    # WebAuthn credential id (raw bytes). BYTEA on PG; VARBINARY(1023) on MySQL so the
    # UNIQUE index fits InnoDB's 3072-byte key limit at FULL length — no prefix-unique
    # weakening on an auth table (spec caps credential ids at 1023 bytes).
    credential_id: Mapped[bytes]         = mapped_column(
        LargeBinary().with_variant(mysql.VARBINARY(1023), "mysql"), nullable=False, unique=True)
    public_key:    Mapped[bytes]         = mapped_column(LargeBinary, nullable=False)
    sign_count:    Mapped[int]           = mapped_column(BigInteger, default=0, nullable=False)
    transports:    Mapped[list]          = mapped_column(PortableJSON(), default=list)
    aaguid:        Mapped[str|None]      = mapped_column(String(64), nullable=True)
    device_name:   Mapped[str]           = mapped_column(String(255), default="Passkey")
    created_at:    Mapped[datetime]      = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_used_at:  Mapped[datetime|None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at:    Mapped[datetime|None] = mapped_column(DateTime(timezone=True), nullable=True)


# >>> Case Variables Phase 1 (spec v2 — docs/Future/case_variables.md)
class VariableNamespaceModel(Base):
    """Registered variable namespace — the trust boundary left of the dot.

    Namespaces are rows, not strings: a write to an unregistered namespace is
    rejected, and the namespace a caller writes to is derived from its
    authenticated identity (owner_type + owner_ref), never from a parameter.
    """
    __tablename__ = "variable_namespaces"

    id:          Mapped[uuid.UUID]     = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    name:        Mapped[str]           = mapped_column(String(100), unique=True, nullable=False)
    owner_type:  Mapped[str]           = mapped_column(String(20), nullable=False)
    owner_ref:   Mapped[uuid.UUID|None] = mapped_column(GUID(), nullable=True)
    sensitivity: Mapped[str]           = mapped_column(String(10), default="internal", nullable=False)
    status:      Mapped[str]           = mapped_column(String(10), default="active", nullable=False)
    created_by:  Mapped[str|None]      = mapped_column(String(255), nullable=True)
    created_at:  Mapped[datetime]      = mapped_column(DateTime(timezone=True), default=_utcnow)


class NamespaceGrantModel(Base):
    """Explicit cross-namespace capability — owner-only access is the default."""
    __tablename__ = "namespace_grants"
    __table_args__ = (
        UniqueConstraint("namespace_id", "grantee_type", "grantee_ref", "capability"),
    )

    id:           Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    namespace_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("variable_namespaces.id", ondelete="CASCADE"), nullable=False)
    grantee_type: Mapped[str]       = mapped_column(String(20), nullable=False)
    grantee_ref:  Mapped[str]       = mapped_column(String(255), nullable=False)
    capability:   Mapped[str]       = mapped_column(String(10), nullable=False)
    granted_by:   Mapped[str|None]  = mapped_column(String(255), nullable=True)
    granted_at:   Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=_utcnow)


class CaseTypeVariableModel(Base):
    """Variable definition scoped to one case type (spec v2)."""
    __tablename__ = "case_type_variables"
    __table_args__ = (
        UniqueConstraint("case_type_id", "full_key"),
        Index("ix_ctv_case_type", "case_type_id"),
        Index("ix_ctv_status", "case_type_id", "definition_status"),
    )

    id:                   Mapped[uuid.UUID]  = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    case_type_id:         Mapped[uuid.UUID]  = mapped_column(GUID(), ForeignKey("case_types.id", ondelete="CASCADE"), nullable=False)
    namespace_id:         Mapped[uuid.UUID]  = mapped_column(GUID(), ForeignKey("variable_namespaces.id"), nullable=False)
    name:                 Mapped[str]        = mapped_column(String(100), nullable=False)
    full_key:             Mapped[str]        = mapped_column(String(201), nullable=False)
    var_type:             Mapped[str]        = mapped_column(String(20), default="any", nullable=False)
    definition_status:    Mapped[str]        = mapped_column(String(12), default="defined", nullable=False)
    sensitivity_override: Mapped[str|None]   = mapped_column(String(10), nullable=True)
    label:                Mapped[str|None]   = mapped_column(String(255), nullable=True)
    description:          Mapped[str|None]   = mapped_column(Text, nullable=True)
    default_value:        Mapped[str|None]   = mapped_column(Text, nullable=True)
    required:             Mapped[bool]       = mapped_column(Boolean, default=False)
    indexed:              Mapped[bool]       = mapped_column(Boolean, default=False)  # UI filter flag ONLY — never DDL
    promoted_source:      Mapped[str|None]   = mapped_column(String(255), nullable=True)  # blob key this was promoted from (088)
    created_at:           Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_utcnow)


class CaseInstanceVariableModel(Base):
    """One value per (case, full_key) — upserted; history lives in lineage events."""
    __tablename__ = "case_instance_variables"
    __table_args__ = (
        UniqueConstraint("case_id", "full_key"),
        Index("ix_civ_key_num", "full_key", "value_num"),
        Index("ix_civ_key_text", "full_key", "value_text", mysql_length={"value_text": 255}),
    )

    id:         Mapped[uuid.UUID]   = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    case_id:    Mapped[uuid.UUID]   = mapped_column(GUID(), ForeignKey("case_instances.id", ondelete="CASCADE"), nullable=False)
    full_key:   Mapped[str]         = mapped_column(String(201), nullable=False)
    value_text: Mapped[str|None]    = mapped_column(Text, nullable=True)
    value_num:  Mapped[float|None]  = mapped_column(Float, nullable=True)
    value_bool: Mapped[bool|None]   = mapped_column(Boolean, nullable=True)
    value_json: Mapped[dict|list|None] = mapped_column(PortableJSON(), nullable=True)
    written_by: Mapped[str]         = mapped_column(String(255), nullable=False)
    written_at: Mapped[datetime]    = mapped_column(DateTime(timezone=True), default=_utcnow)
# <<< Case Variables Phase 1


class WebAuthnChallengeModel(Base):
    """Short-lived server-side WebAuthn challenges (5-minute expiry).

    DB-backed (not in-memory) so verification works across worker processes;
    rows are deleted on use and swept on expiry.
    """
    __tablename__ = "webauthn_challenges"
    __table_args__ = (
        Index("ix_webauthn_chal_expires", "expires_at"),
    )

    id:         Mapped[uuid.UUID]  = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    user_id:    Mapped[str|None]   = mapped_column(String(255), nullable=True)
    challenge:  Mapped[bytes]      = mapped_column(LargeBinary, nullable=False)
    purpose:    Mapped[str]        = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_utcnow)
    expires_at: Mapped[datetime]   = mapped_column(DateTime(timezone=True), nullable=False)
# <<< Group J


class DmlBeforeImageModel(Base):
    """Before-image snapshots for HxDBManager DML rollback."""
    __tablename__ = "dml_before_image"
    __table_args__ = (
        Index("ix_dml_before_tenant",   "tenant_id"),
        Index("ix_dml_before_user",     "user_id"),
        Index("ix_dml_before_captured", "captured_at"),
    )

    id:             Mapped[uuid.UUID]      = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:      Mapped[str]            = mapped_column(String(255), nullable=False)
    user_id:        Mapped[str]            = mapped_column(String(255), nullable=False)
    operation:      Mapped[str]            = mapped_column(String(10),  nullable=False)
    table_hint:     Mapped[str | None]     = mapped_column(String(255), nullable=True)
    original_sql:   Mapped[str]            = mapped_column(Text, nullable=False)
    old_rows:       Mapped[dict | None]    = mapped_column(PortableJSON(), nullable=True)
    new_rows:       Mapped[dict | None]    = mapped_column(PortableJSON(), nullable=True)
    row_count:      Mapped[int | None]     = mapped_column(Integer, nullable=True)
    capture_method: Mapped[str]            = mapped_column(String(30),  nullable=False)
    captured_at:    Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow)


class ScheduledReleaseModel(Base):
    """P66 — Velaris product release schedule."""
    __tablename__ = "scheduled_releases"
    __table_args__ = (
        Index("ix_scheduled_releases_status", "status"),
        Index("ix_scheduled_releases_date",   "release_date"),
    )

    id:            Mapped[uuid.UUID]      = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    feature_key:   Mapped[str]            = mapped_column(String(100), nullable=False, unique=True)
    version:       Mapped[str | None]     = mapped_column(String(32), nullable=True)
    title:         Mapped[str]            = mapped_column(String(255), nullable=False)
    description:   Mapped[str | None]     = mapped_column(Text, nullable=True)
    release_notes: Mapped[str | None]     = mapped_column(Text, nullable=True)
    release_date:  Mapped[date | None]    = mapped_column(Date, nullable=True)
    status:        Mapped[str]            = mapped_column(String(20), nullable=False, default="draft")
    enabled:       Mapped[str | None]     = mapped_column(String(20), nullable=True, default=None)
    created_at:    Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at:    Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow)
    released_at:   Mapped[datetime | None]= mapped_column(DateTime(timezone=True), nullable=True)


class PortalCustomerModel(Base):
    """P65 — Persistent portal customer identity (one record per email per tenant)."""
    __tablename__ = "portal_customers"
    __table_args__ = (
        UniqueConstraint("tenant_id", "primary_email", name="uq_portal_customer_email_tenant"),
        Index("ix_portal_customers_tenant", "tenant_id"),
        Index("ix_portal_customers_email",  "primary_email"),
    )

    id:              Mapped[uuid.UUID]       = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:       Mapped[uuid.UUID]       = mapped_column(GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    primary_email:   Mapped[str]             = mapped_column(String(255), nullable=False)
    alt_email:       Mapped[str | None]      = mapped_column(String(255), nullable=True)
    preferred_email: Mapped[str]             = mapped_column(String(10), nullable=False, default="primary")
    display_name:    Mapped[str]             = mapped_column(String(255), nullable=False)
    phone:           Mapped[str | None]      = mapped_column(String(64), nullable=True)
    verified:        Mapped[bool]            = mapped_column(Boolean, nullable=False, default=False)
    otp_code:        Mapped[str | None]      = mapped_column(String(64), nullable=True)   # SHA-256 hash
    otp_expires_at:  Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notify_email:    Mapped[bool]            = mapped_column(Boolean, nullable=False, default=True)  # P4 (mig 116)
    created_at:      Mapped[datetime]        = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_active_at:  Mapped[datetime]        = mapped_column(DateTime(timezone=True), default=_utcnow)


class PortalCustomerCaseLinkModel(Base):
    """P65 — Links a portal customer to the cases they have submitted/own."""
    __tablename__ = "portal_customer_cases"
    __table_args__ = (
        Index("ix_pcc_customer", "customer_id"),
        Index("ix_pcc_case",     "case_id"),
    )

    customer_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("portal_customers.id", ondelete="CASCADE"), primary_key=True)
    case_id:     Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("case_instances.id",   ondelete="CASCADE"), primary_key=True)
    linked_at:   Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=_utcnow)


class CaseMessageModel(Base):
    """Portal v2 P4 (mig 116) — human case thread (worker ↔ customer).

    author is a principal ("user:{id}" | "customer:{id}"); portal_visible
    FALSE = internal worker note, never served to the portal.
    """
    __tablename__ = "case_messages"
    __table_args__ = (
        Index("idx_case_messages_case", "case_id", "created_at"),
    )

    id:             Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    case_id:        Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("case_instances.id", ondelete="CASCADE"), nullable=False)
    author:         Mapped[str]       = mapped_column(String(255), nullable=False)
    author_name:    Mapped[str | None] = mapped_column(String(255), nullable=True)
    body:           Mapped[str]       = mapped_column(Text, nullable=False)
    portal_visible: Mapped[bool]      = mapped_column(Boolean, default=True, nullable=False)
    created_at:     Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=_utcnow)


class PortalCsatModel(Base):
    """Portal v2 P5 (mig 117) — one post-resolution rating per case."""
    __tablename__ = "portal_csat"

    case_id:     Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("case_instances.id", ondelete="CASCADE"), primary_key=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("portal_customers.id", ondelete="CASCADE"), nullable=False)
    rating:      Mapped[int]       = mapped_column(Integer, nullable=False)
    comment:     Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at:  Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=_utcnow)


class PortalAskFeedbackModel(Base):
    """Portal v2 P5 (mig 117) — pre-submit AI deflection feedback."""
    __tablename__ = "portal_ask_feedback"
    __table_args__ = (
        Index("idx_portal_ask_feedback_tenant", "tenant_slug", "created_at"),
    )

    id:          Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_slug: Mapped[str]       = mapped_column(String(255), nullable=False)
    question:    Mapped[str]       = mapped_column(Text, nullable=False)
    helpful:     Mapped[bool]      = mapped_column(Boolean, nullable=False)
    created_at:  Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=_utcnow)


class PortalSubmissionRefModel(Base):
    """Portal v2 P2 (mig 115) — offline-submission idempotency.

    A PWA submission carries a client-generated UUID; replayed syncs
    (double flush, two tabs, background-sync retry) resolve to the original
    case instead of creating a duplicate. Prunable after 30 days.
    """
    __tablename__ = "portal_submission_refs"
    __table_args__ = (
        Index("idx_portal_submission_refs_created", "created_at"),
    )

    client_ref:  Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True)
    tenant_slug: Mapped[str]       = mapped_column(String(255), nullable=False)
    case_id:     Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("case_instances.id", ondelete="CASCADE"), nullable=False)
    created_at:  Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=_utcnow)


class HxGuardTupleModel(Base):
    """HxGuard Phase B relationship tuple (Zanzibar shape, migration 089).

    Written in the SAME transaction as the source mutation (assignment
    lifecycle, case shares) — authz state cannot diverge from case state.
    """
    __tablename__ = "hxguard_tuples"
    __table_args__ = (
        UniqueConstraint("object_type", "object_id", "relation",
                         "subject_type", "subject_id", name="uq_hxguard_tuple"),
        Index("ix_hxg_tuples_object", "object_type", "object_id"),
        Index("ix_hxg_tuples_subject", "subject_type", "subject_id"),
    )

    id:           Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    object_type:  Mapped[str]       = mapped_column(String(30), nullable=False)
    object_id:    Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    relation:     Mapped[str]       = mapped_column(String(30), nullable=False)
    subject_type: Mapped[str]       = mapped_column(String(30), nullable=False)
    subject_id:   Mapped[str]       = mapped_column(String(255), nullable=False)
    created_by:   Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at:   Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=_utcnow)


class OutboxEventModel(Base):
    """Transactional outbox for reliable webhook delivery (C1 / migration 083).

    Written in the same DB transaction as the triggering case mutation.
    OutboxRelay reads pending rows, delivers HTTP calls, marks delivered_at.
    Crash-safe: claimed_at is reset after 5 min so stuck rows are re-tried.
    """
    __tablename__ = "outbox"
    __table_args__ = (
        Index("ix_outbox_pending", "created_at",
              postgresql_where="delivered_at IS NULL"),
    )

    id:              Mapped[uuid.UUID]       = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    event_type:      Mapped[str]             = mapped_column(Text, nullable=False)
    payload:         Mapped[dict]            = mapped_column(PortableJSON(), nullable=False, default=dict)
    case_type_id:    Mapped[uuid.UUID | None]= mapped_column(GUID(), ForeignKey("case_types.id", ondelete="SET NULL"), nullable=True)
    created_at:      Mapped[datetime]        = mapped_column(DateTime(timezone=True), default=_utcnow)
    claimed_at:      Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at:    Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts:        Mapped[int]             = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# >>> PUO Phase 3 — platform update rollout plans (migration 084)
# Sibling of HxDeploy's DeploymentRunModel: these orchestrate PLATFORM CODE
# updates across registered environments; deployment_runs promote Studio
# artifacts. Deliberately separate entities.

class PlatformUpdatePlanModel(Base):
    """One fleet rollout of a platform version across registered environments."""
    __tablename__ = "platform_update_plans"
    __table_args__ = (Index("ix_puo_plans_state", "state"),)

    id:               Mapped[uuid.UUID]       = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    resolved_version: Mapped[str]             = mapped_column(Text, nullable=False)
    channel:          Mapped[str]             = mapped_column(Text, nullable=False, default="stable")
    soak_hours:       Mapped[int]             = mapped_column(Integer, nullable=False, default=48)
    state:            Mapped[str]             = mapped_column(String(32), nullable=False, default="draft")
    halted_reason:    Mapped[str | None]      = mapped_column(Text, nullable=True)
    approved_by:      Mapped[str | None]      = mapped_column(Text, nullable=True)
    approved_at:      Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    prod_approved_by: Mapped[str | None]      = mapped_column(Text, nullable=True)
    prod_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    soak_started_at:  Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at:       Mapped[datetime]        = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at:       Mapped[datetime]        = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class PlatformUpdateRunModel(Base):
    """One environment's update inside a rollout plan (a ring)."""
    __tablename__ = "platform_update_runs"
    __table_args__ = (Index("ix_puo_runs_plan", "plan_id"),)

    id:             Mapped[uuid.UUID]       = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    plan_id:        Mapped[uuid.UUID]       = mapped_column(GUID(), ForeignKey("platform_update_plans.id", ondelete="CASCADE"), nullable=False)
    environment_id: Mapped[uuid.UUID]       = mapped_column(GUID(), ForeignKey("environment_registry.id", ondelete="CASCADE"), nullable=False)
    ring_order:     Mapped[int]             = mapped_column(Integer, nullable=False, default=0)
    is_final_ring:  Mapped[bool]            = mapped_column(Boolean, nullable=False, default=False)
    state:          Mapped[str]             = mapped_column(Text, nullable=False, default="pending")
    detail:         Mapped[str | None]      = mapped_column(Text, nullable=True)
    triggered_at:   Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at:    Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at:     Mapped[datetime]        = mapped_column(DateTime(timezone=True), default=_utcnow)


class PlatformUpdateSettingsModel(Base):
    """Single-row PUO settings (mode, default soak, calendar)."""
    __tablename__ = "platform_update_settings"

    id:                 Mapped[int]             = mapped_column(Integer, primary_key=True, default=1)
    mode:               Mapped[str]             = mapped_column(Text, nullable=False, default="auto-soak")
    default_soak_hours: Mapped[int]             = mapped_column(Integer, nullable=False, default=48)
    calendar_id:        Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    updated_at:         Mapped[datetime]        = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
# <<< PUO Phase 3


# >>> DB SDK — portable monotonic counter (case numbers)
class SequenceCounterModel(Base):
    """Named monotonic counters for backends without native SEQUENCEs (MySQL et al).

    PostgreSQL uses its native ``helix_case_seq`` SEQUENCE (migration 035) and never
    touches this table — the per-dialect ``DatabaseBackend.next_case_seq`` decides.
    Intentional cross-dialect drift: present in metadata/MySQL baseline, unused on PG.
    """
    __tablename__ = "velaris_sequences"

    name:  Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
# <<< DB SDK


# >>> DB SDK — settings/config tables that raw migrations create without a model.
# Modeling them puts them in the generated baseline (so MySQL/SQLite get them) and
# lets callers use the ORM, which quotes the reserved word `key` per-dialect — no
# hand-written backticks. `value` columns mirror the live migration types (068/070).
class HelixSettingModel(Base):
    """Key/value platform settings (migration 068). e.g. token_expiry_days."""
    __tablename__ = "helix_settings"

    key:        Mapped[str]            = mapped_column(String(255), primary_key=True)
    value:      Mapped[str]            = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
    updated_by: Mapped[str | None]     = mapped_column(Text, nullable=True)


class SystemConfigModel(Base):
    """Admin-managed JSON config (migration 070). e.g. route_permissions matrix."""
    __tablename__ = "system_config"

    key:        Mapped[str]            = mapped_column(String(255), primary_key=True)
    value:      Mapped[dict | list]    = mapped_column(PortableJSON(), nullable=False)
    updated_at: Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
    updated_by: Mapped[str | None]     = mapped_column(String(255), nullable=True)
# <<< DB SDK


# ══════════════════════════════════════════════════════════════════════════════
#  HxCheckout — commerce integration layer (marketplace app `velaris/hxcheckout`).
#
#  The HxCheckout Python ships in-image like every official marketplace module;
#  these tables ship via the normal startup migration track (095_checkout.sql /
#  MySQL baseline). Installing the marketplace package only flips the per-tenant
#  gate + Studio routes — it does NOT provision schema (the tables are always
#  present, empty until the app is used). An order is stored here AND opened as a
#  Velaris Order `case_type`; checkout_orders.case_id links the two.
# ══════════════════════════════════════════════════════════════════════════════

class CheckoutOrderModel(Base):
    """One commerce order. Mirrors a Velaris Order case (case_id) and carries the
    customer-facing tracking token. Immutable from the external API after creation."""

    __tablename__ = "checkout_orders"
    __table_args__ = (
        Index("ix_checkout_orders_tenant",   "tenant_id"),
        Index("ix_checkout_orders_case",     "case_id"),
        Index("ix_checkout_orders_status",   "status"),
        UniqueConstraint("tracking_token", name="uq_checkout_orders_tracking"),
        # Optional client-supplied idempotency key (Idempotency-Key header). A retry
        # after a timeout returns the existing order instead of creating a duplicate
        # Order case. Scoped per tenant; NULLs are distinct so keyless orders never collide.
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_checkout_orders_idem"),
    )

    id:                 Mapped[uuid.UUID]      = mapped_column(GUID(),      primary_key=True, default=_new_uuid)
    tenant_id:          Mapped[str]            = mapped_column(String(255), nullable=False)
    # SET NULL (not CASCADE): deleting a case must never erase order history.
    case_id:            Mapped[uuid.UUID|None] = mapped_column(GUID(),      ForeignKey("case_instances.id", ondelete="SET NULL"), nullable=True)
    tracking_token:     Mapped[str]            = mapped_column(String(64),  nullable=False)
    status:             Mapped[str]            = mapped_column(String(50),  nullable=False, default="pending_payment")
    currency:           Mapped[str]            = mapped_column(String(10),  nullable=False, default="GBP")
    total_cents:        Mapped[int]            = mapped_column(BigInteger,  nullable=False, default=0)
    customer:           Mapped[dict]           = mapped_column(PortableJSON(), default=dict)   # name/email/phone
    shipping:           Mapped[dict]           = mapped_column(PortableJSON(), default=dict)   # address + method
    order_meta:         Mapped[dict]           = mapped_column("metadata", PortableJSON(), default=dict)
    source:             Mapped[str]            = mapped_column(String(50),  nullable=False, default="api")  # api|sdk|webhook|storefront
    idempotency_key:    Mapped[str|None]       = mapped_column(String(255), nullable=True)
    integration_id:     Mapped[uuid.UUID|None] = mapped_column(GUID(),      ForeignKey("checkout_webhook_integrations.id", ondelete="SET NULL"), nullable=True)
    payment_request_id: Mapped[uuid.UUID|None] = mapped_column(GUID(),      nullable=True)
    is_test:            Mapped[bool]           = mapped_column(Boolean,     nullable=False, default=False)
    created_at:         Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at:         Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class CheckoutOrderItemModel(Base):
    """A line item on a checkout order (a basket entry, captured at purchase time)."""

    __tablename__ = "checkout_order_items"
    __table_args__ = (
        Index("ix_checkout_order_items_order", "order_id"),
    )

    id:               Mapped[uuid.UUID] = mapped_column(GUID(),      primary_key=True, default=_new_uuid)
    order_id:         Mapped[uuid.UUID] = mapped_column(GUID(),      ForeignKey("checkout_orders.id", ondelete="CASCADE"), nullable=False)
    sku:              Mapped[str]       = mapped_column(String(255), nullable=False)
    name:             Mapped[str]       = mapped_column(String(512), nullable=False)
    quantity:         Mapped[int]       = mapped_column(Integer,     nullable=False, default=1)
    unit_price_cents: Mapped[int]       = mapped_column(BigInteger,  nullable=False, default=0)
    item_meta:        Mapped[dict]      = mapped_column("metadata", PortableJSON(), default=dict)


class CheckoutServiceTokenModel(Base):
    """A per-tenant service token (API key) authenticating external order creation.
    Only the bcrypt hash is persisted — the plaintext is shown once at creation."""

    __tablename__ = "checkout_service_tokens"
    __table_args__ = (
        Index("ix_checkout_tokens_tenant", "tenant_id"),
        # token_prefix carries the public key-id (vsk_<mode>_<keyid>) and is the
        # O(1) lookup key at auth time — bcrypt is salted so token_hash can't be
        # queried directly. Unique so a key-id resolves to exactly one row.
        UniqueConstraint("token_prefix", name="uq_checkout_tokens_prefix"),
    )

    id:           Mapped[uuid.UUID]     = mapped_column(GUID(),      primary_key=True, default=_new_uuid)
    tenant_id:    Mapped[str]           = mapped_column(String(255), nullable=False)
    label:        Mapped[str]           = mapped_column(String(255), nullable=False, default="")
    token_hash:   Mapped[str]           = mapped_column(String(255), nullable=False)   # bcrypt
    token_prefix: Mapped[str]           = mapped_column(String(24),  nullable=False)  # public key-id vsk_<mode>_<keyid>; UNIQUE lookup key (always set by generate_token)
    scope:        Mapped[str]           = mapped_column(String(50),  nullable=False, default="orders:create")
    last_used_at: Mapped[datetime|None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at:   Mapped[datetime|None] = mapped_column(DateTime(timezone=True), nullable=True)
    suspended:    Mapped[bool]          = mapped_column(Boolean,     nullable=False, default=False)  # auto-suspend on rate spike
    created_by:   Mapped[str|None]      = mapped_column(String(255), nullable=True)
    created_at:   Mapped[datetime]      = mapped_column(DateTime(timezone=True), default=_utcnow)


class CheckoutWebhookIntegrationModel(Base):
    """Inbound webhook source config (Shopify/WooCommerce/Magento/BigCommerce/custom)
    with the HMAC secret (encrypted) and the field mapping into the order shape."""

    __tablename__ = "checkout_webhook_integrations"
    __table_args__ = (
        Index("ix_checkout_integrations_tenant", "tenant_id"),
    )

    id:              Mapped[uuid.UUID] = mapped_column(GUID(),      primary_key=True, default=_new_uuid)
    tenant_id:       Mapped[str]       = mapped_column(String(255), nullable=False)
    platform:        Mapped[str]       = mapped_column(String(50),  nullable=False, default="custom")
    label:           Mapped[str]       = mapped_column(String(255), nullable=False, default="")
    hmac_secret_enc: Mapped[str|None]  = mapped_column(Text,        nullable=True)   # hxv1: encrypted shared secret
    field_map:       Mapped[dict]      = mapped_column(PortableJSON(), default=dict)  # {} = use built-in platform map
    enabled:         Mapped[bool]      = mapped_column(Boolean,     nullable=False, default=True)
    created_by:      Mapped[str|None]  = mapped_column(String(255), nullable=True)
    created_at:      Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=_utcnow)


class CheckoutWebhookEventModel(Base):
    """Full log of every inbound webhook event — raw payload + mapped result + status —
    written regardless of success/failure (key invariant 6)."""

    __tablename__ = "checkout_webhook_events"
    __table_args__ = (
        Index("ix_checkout_wh_events_integration", "integration_id"),
        Index("ix_checkout_wh_events_created",     "created_at"),
    )

    id:             Mapped[uuid.UUID]      = mapped_column(GUID(),      primary_key=True, default=_new_uuid)
    integration_id: Mapped[uuid.UUID|None] = mapped_column(GUID(),      ForeignKey("checkout_webhook_integrations.id", ondelete="CASCADE"), nullable=True)
    raw:            Mapped[dict]           = mapped_column(PortableJSON(), default=dict)
    mapped:         Mapped[dict]           = mapped_column(PortableJSON(), default=dict)
    status:         Mapped[str]            = mapped_column(String(50),  nullable=False, default="received")  # received|order_created|rejected|error
    order_id:       Mapped[uuid.UUID|None] = mapped_column(GUID(),      nullable=True)
    error:          Mapped[str|None]       = mapped_column(Text,        nullable=True)
    created_at:     Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow)


class CheckoutNotificationLogModel(Base):
    """Record of every customer notification sent for an order event (per channel)."""

    __tablename__ = "checkout_notifications_log"
    __table_args__ = (
        Index("ix_checkout_notif_order", "order_id"),
    )

    id:         Mapped[uuid.UUID] = mapped_column(GUID(),      primary_key=True, default=_new_uuid)
    order_id:   Mapped[uuid.UUID] = mapped_column(GUID(),      ForeignKey("checkout_orders.id", ondelete="CASCADE"), nullable=False)
    event:      Mapped[str]       = mapped_column(String(100), nullable=False)   # order_received|payment_confirmed|dispatched|…
    channel:    Mapped[str]       = mapped_column(String(20),  nullable=False)   # email|sms|push
    status:     Mapped[str]       = mapped_column(String(50),  nullable=False, default="sent")
    created_at: Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=_utcnow)


# ══════════════════════════════════════════════════════════════════════════════
#  HxStorefront — hosted store builder (marketplace app `velaris/hxstorefront`).
#
#  Same pattern as HxCheckout: Python + Studio ship in-image, tables on the normal
#  migration track (096), install flips the per-tenant gate + routes. A purchase on
#  a storefront flows through HxCheckout to become an Order case. A tenant can run
#  multiple stores (multi-store); storefront_stores.slug is the public identifier.
# ══════════════════════════════════════════════════════════════════════════════

class StorefrontStoreModel(Base):
    """One hosted store. slug is the public route (/store/:slug). Multi-store: a
    tenant may own several. settings holds currency/tax/shipping/locale config."""

    __tablename__ = "storefront_stores"
    __table_args__ = (
        Index("ix_storefront_stores_tenant", "tenant_id"),
        UniqueConstraint("slug", name="uq_storefront_stores_slug"),
    )

    id:         Mapped[uuid.UUID] = mapped_column(GUID(),       primary_key=True, default=_new_uuid)
    tenant_id:  Mapped[str]       = mapped_column(String(255),  nullable=False)
    slug:       Mapped[str]       = mapped_column(String(255),  nullable=False)
    name:       Mapped[str]       = mapped_column(String(255),  nullable=False)
    currency:   Mapped[str]       = mapped_column(String(10),   nullable=False, default="GBP")
    locale:     Mapped[str]       = mapped_column(String(20),   nullable=False, default="en-GB")
    status:     Mapped[str]       = mapped_column(String(20),   nullable=False, default="active")  # active|archived
    settings:   Mapped[dict]      = mapped_column(PortableJSON(), default=dict)   # tax rules, shipping zones, etc.
    created_at: Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class StorefrontProductModel(Base):
    """A product in a store's catalogue. Prices are integer minor units (pence/cents)."""

    __tablename__ = "storefront_products"
    __table_args__ = (
        Index("ix_storefront_products_store",  "store_id"),
        Index("ix_storefront_products_status", "status"),
        UniqueConstraint("store_id", "slug", name="uq_storefront_products_slug"),
    )

    id:                  Mapped[uuid.UUID] = mapped_column(GUID(),       primary_key=True, default=_new_uuid)
    store_id:            Mapped[uuid.UUID] = mapped_column(GUID(),       ForeignKey("storefront_stores.id", ondelete="CASCADE"), nullable=False)
    name:                Mapped[str]       = mapped_column(String(512),  nullable=False)
    slug:                Mapped[str]       = mapped_column(String(255),  nullable=False)
    sku:                 Mapped[str|None]  = mapped_column(String(255),  nullable=True)
    description:         Mapped[str]       = mapped_column(Text,         default="")
    short_description:   Mapped[str]       = mapped_column(String(512),  default="")
    tags:                Mapped[list]      = mapped_column(PortableJSON(), default=list)
    price_cents:         Mapped[int]       = mapped_column(BigInteger,   nullable=False, default=0)
    compare_at_cents:    Mapped[int|None]  = mapped_column(BigInteger,   nullable=True)   # crossed-out sale price
    tax_class:           Mapped[str]       = mapped_column(String(20),   nullable=False, default="standard")
    weight_grams:        Mapped[int]       = mapped_column(Integer,      nullable=False, default=0)
    status:              Mapped[str]       = mapped_column(String(20),   nullable=False, default="draft")  # draft|active|archived
    stock_quantity:      Mapped[int|None]  = mapped_column(Integer,      nullable=True)   # NULL = unlimited (no variants)
    low_stock_threshold: Mapped[int|None]  = mapped_column(Integer,      nullable=True)
    is_featured:         Mapped[bool]      = mapped_column(Boolean,      nullable=False, default=False)
    is_digital:          Mapped[bool]      = mapped_column(Boolean,      nullable=False, default=False)
    product_meta:        Mapped[dict]      = mapped_column("metadata", PortableJSON(), default=dict)
    created_at:          Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at:          Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class StorefrontProductImageModel(Base):
    """A product image (stored in MinIO). First by display_order = primary."""

    __tablename__ = "storefront_product_images"
    __table_args__ = (Index("ix_storefront_images_product", "product_id"),)

    id:            Mapped[uuid.UUID] = mapped_column(GUID(),       primary_key=True, default=_new_uuid)
    product_id:    Mapped[uuid.UUID] = mapped_column(GUID(),       ForeignKey("storefront_products.id", ondelete="CASCADE"), nullable=False)
    media_path:    Mapped[str]       = mapped_column(String(1024), nullable=False)   # MinIO object path
    alt_text:      Mapped[str]       = mapped_column(String(512),  default="")
    display_order: Mapped[int]       = mapped_column(Integer,      nullable=False, default=0)


class StorefrontVariantOptionModel(Base):
    """An option definition per product (e.g. name='Size', values=['S','M','L'])."""

    __tablename__ = "storefront_variant_options"
    __table_args__ = (Index("ix_storefront_varopt_product", "product_id"),)

    id:            Mapped[uuid.UUID] = mapped_column(GUID(),       primary_key=True, default=_new_uuid)
    product_id:    Mapped[uuid.UUID] = mapped_column(GUID(),       ForeignKey("storefront_products.id", ondelete="CASCADE"), nullable=False)
    name:          Mapped[str]       = mapped_column(String(255),  nullable=False)   # "Size", "Colour"
    values:        Mapped[list]      = mapped_column(PortableJSON(), default=list)   # ["S","M","L"]
    display_order: Mapped[int]       = mapped_column(Integer,      nullable=False, default=0)


class StorefrontProductVariantModel(Base):
    """A concrete variant (a cross-product of option values). Overrides price/stock/sku."""

    __tablename__ = "storefront_product_variants"
    __table_args__ = (
        Index("ix_storefront_variants_product", "product_id"),
        UniqueConstraint("product_id", "sku", name="uq_storefront_variants_sku"),
    )

    id:             Mapped[uuid.UUID] = mapped_column(GUID(),       primary_key=True, default=_new_uuid)
    product_id:     Mapped[uuid.UUID] = mapped_column(GUID(),       ForeignKey("storefront_products.id", ondelete="CASCADE"), nullable=False)
    sku:            Mapped[str]       = mapped_column(String(255),  nullable=False)
    option_values:  Mapped[dict]      = mapped_column(PortableJSON(), default=dict)   # {"Size":"M","Colour":"Blue"}
    price_cents:    Mapped[int|None]  = mapped_column(BigInteger,   nullable=True)    # NULL = use product price
    stock_quantity: Mapped[int|None]  = mapped_column(Integer,      nullable=True)    # NULL = unlimited
    media_path:     Mapped[str|None]  = mapped_column(String(1024), nullable=True)
    display_order:  Mapped[int]       = mapped_column(Integer,      nullable=False, default=0)


class StorefrontCategoryModel(Base):
    """Category tree (adjacency list via parent_id). Products link many-to-many."""

    __tablename__ = "storefront_categories"
    __table_args__ = (
        Index("ix_storefront_categories_store",  "store_id"),
        Index("ix_storefront_categories_parent", "parent_id"),
        UniqueConstraint("store_id", "slug", name="uq_storefront_categories_slug"),
    )

    id:            Mapped[uuid.UUID]      = mapped_column(GUID(),       primary_key=True, default=_new_uuid)
    store_id:      Mapped[uuid.UUID]      = mapped_column(GUID(),       ForeignKey("storefront_stores.id", ondelete="CASCADE"), nullable=False)
    parent_id:     Mapped[uuid.UUID|None] = mapped_column(GUID(),       ForeignKey("storefront_categories.id", ondelete="SET NULL"), nullable=True)
    name:          Mapped[str]            = mapped_column(String(255),  nullable=False)
    slug:          Mapped[str]            = mapped_column(String(255),  nullable=False)
    description:   Mapped[str]            = mapped_column(Text,         default="")
    banner_path:   Mapped[str|None]       = mapped_column(String(1024), nullable=True)
    display_order: Mapped[int]            = mapped_column(Integer,      nullable=False, default=0)


class StorefrontProductCategoryModel(Base):
    """Many-to-many product ↔ category."""

    __tablename__ = "storefront_product_categories"
    __table_args__ = (
        Index("ix_storefront_prodcat_product",  "product_id"),
        Index("ix_storefront_prodcat_category", "category_id"),
        UniqueConstraint("product_id", "category_id", name="uq_storefront_prodcat"),
    )

    id:          Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    product_id:  Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("storefront_products.id", ondelete="CASCADE"), nullable=False)
    category_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("storefront_categories.id", ondelete="CASCADE"), nullable=False)


class StorefrontInventoryLogModel(Base):
    """Stock movement history per variant (sale / manual edit / restock)."""

    __tablename__ = "storefront_inventory_log"
    __table_args__ = (Index("ix_storefront_invlog_variant", "variant_id"),)

    id:           Mapped[uuid.UUID] = mapped_column(GUID(),       primary_key=True, default=_new_uuid)
    variant_id:   Mapped[uuid.UUID] = mapped_column(GUID(),       ForeignKey("storefront_product_variants.id", ondelete="CASCADE"), nullable=False)
    change:       Mapped[int]       = mapped_column(Integer,      nullable=False)   # signed delta
    new_quantity: Mapped[int|None]  = mapped_column(Integer,      nullable=True)
    reason:       Mapped[str]       = mapped_column(String(100),  nullable=False, default="adjustment")  # sale|adjustment|restock
    actor:        Mapped[str|None]  = mapped_column(String(255),  nullable=True)
    created_at:   Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=_utcnow)


class StorefrontThemeModel(Base):
    """Versioned theme config JSON per store (keep last 10; one active)."""

    __tablename__ = "storefront_themes"
    __table_args__ = (Index("ix_storefront_themes_store", "store_id"),)

    id:         Mapped[uuid.UUID] = mapped_column(GUID(),       primary_key=True, default=_new_uuid)
    store_id:   Mapped[uuid.UUID] = mapped_column(GUID(),       ForeignKey("storefront_stores.id", ondelete="CASCADE"), nullable=False)
    config:     Mapped[dict]      = mapped_column(PortableJSON(), default=dict)
    version:    Mapped[int]       = mapped_column(Integer,      nullable=False, default=1)
    is_active:  Mapped[bool]      = mapped_column(Boolean,      nullable=False, default=True)
    created_at: Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=_utcnow)


class StorefrontPageModel(Base):
    """A store page (home/about/custom) built from Page Builder sections."""

    __tablename__ = "storefront_pages"
    __table_args__ = (
        Index("ix_storefront_pages_store", "store_id"),
        UniqueConstraint("store_id", "page_slug", name="uq_storefront_pages_slug"),
    )

    id:           Mapped[uuid.UUID] = mapped_column(GUID(),       primary_key=True, default=_new_uuid)
    store_id:     Mapped[uuid.UUID] = mapped_column(GUID(),       ForeignKey("storefront_stores.id", ondelete="CASCADE"), nullable=False)
    page_slug:    Mapped[str]       = mapped_column(String(255),  nullable=False)   # "home", "about", …
    title:        Mapped[str]       = mapped_column(String(512),  default="")
    sections:     Mapped[list]      = mapped_column(PortableJSON(), default=list)   # ordered section blocks
    is_published: Mapped[bool]      = mapped_column(Boolean,      nullable=False, default=False)
    created_at:   Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at:   Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class StorefrontNavigationModel(Base):
    """Header/footer menu config per store."""

    __tablename__ = "storefront_navigation"
    __table_args__ = (
        UniqueConstraint("store_id", "location", name="uq_storefront_nav_location"),
    )

    id:         Mapped[uuid.UUID] = mapped_column(GUID(),       primary_key=True, default=_new_uuid)
    store_id:   Mapped[uuid.UUID] = mapped_column(GUID(),       ForeignKey("storefront_stores.id", ondelete="CASCADE"), nullable=False)
    location:   Mapped[str]       = mapped_column(String(20),   nullable=False, default="header")  # header|footer
    items:      Mapped[list]      = mapped_column(PortableJSON(), default=list)
    updated_at: Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class StorefrontPromotionModel(Base):
    """Discount code or automatic discount rule."""

    __tablename__ = "storefront_promotions"
    __table_args__ = (
        Index("ix_storefront_promotions_store", "store_id"),
        Index("ix_storefront_promotions_code",  "code"),
    )

    id:                Mapped[uuid.UUID]   = mapped_column(GUID(),       primary_key=True, default=_new_uuid)
    store_id:          Mapped[uuid.UUID]   = mapped_column(GUID(),       ForeignKey("storefront_stores.id", ondelete="CASCADE"), nullable=False)
    code:              Mapped[str|None]    = mapped_column(String(64),   nullable=True)    # NULL = automatic discount
    discount_type:     Mapped[str]         = mapped_column(String(30),   nullable=False)   # percentage|fixed|free_shipping|bxgy|spend|quantity|bundle|flash
    config:            Mapped[dict]        = mapped_column(PortableJSON(), default=dict)    # type-specific params
    applies_to:        Mapped[dict]        = mapped_column(PortableJSON(), default=dict)    # all|categories|products
    min_order_cents:   Mapped[int|None]    = mapped_column(BigInteger,   nullable=True)
    usage_limit:       Mapped[int|None]    = mapped_column(Integer,      nullable=True)
    per_customer_limit:Mapped[int|None]    = mapped_column(Integer,      nullable=True)
    valid_from:        Mapped[datetime|None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_until:       Mapped[datetime|None] = mapped_column(DateTime(timezone=True), nullable=True)
    stackable:         Mapped[bool]        = mapped_column(Boolean,      nullable=False, default=False)
    active:            Mapped[bool]        = mapped_column(Boolean,      nullable=False, default=True)
    created_at:        Mapped[datetime]    = mapped_column(DateTime(timezone=True), default=_utcnow)


class StorefrontPromotionUseModel(Base):
    """Per-order usage of a promotion (enforces usage / per-customer limits)."""

    __tablename__ = "storefront_promotion_uses"
    __table_args__ = (Index("ix_storefront_promouse_promo", "promotion_id"),)

    id:            Mapped[uuid.UUID] = mapped_column(GUID(),       primary_key=True, default=_new_uuid)
    promotion_id:  Mapped[uuid.UUID] = mapped_column(GUID(),       ForeignKey("storefront_promotions.id", ondelete="CASCADE"), nullable=False)
    order_ref:     Mapped[str|None]  = mapped_column(String(255),  nullable=True)   # checkout order id / tracking token
    customer_email:Mapped[str|None]  = mapped_column(String(255),  nullable=True)
    used_at:       Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=_utcnow)


class StorefrontDomainModel(Base):
    """Custom domain config per store (+ DNS/SSL status)."""

    __tablename__ = "storefront_domains"
    __table_args__ = (
        Index("ix_storefront_domains_store", "store_id"),
        UniqueConstraint("domain", name="uq_storefront_domains_domain"),
    )

    id:          Mapped[uuid.UUID] = mapped_column(GUID(),       primary_key=True, default=_new_uuid)
    store_id:    Mapped[uuid.UUID] = mapped_column(GUID(),       ForeignKey("storefront_stores.id", ondelete="CASCADE"), nullable=False)
    domain:      Mapped[str]       = mapped_column(String(255),  nullable=False)
    domain_type: Mapped[str]       = mapped_column(String(20),   nullable=False, default="cname")  # subdomain|cname|root
    dns_verified:Mapped[bool]      = mapped_column(Boolean,      nullable=False, default=False)
    ssl_status:  Mapped[str]       = mapped_column(String(20),   nullable=False, default="pending")  # pending|active|failed
    created_at:  Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=_utcnow)


class StorefrontSeoOverrideModel(Base):
    """Per-product / per-category / per-page SEO fields."""

    __tablename__ = "storefront_seo_overrides"
    __table_args__ = (
        UniqueConstraint("store_id", "target_type", "target_id", name="uq_storefront_seo_target"),
    )

    id:               Mapped[uuid.UUID] = mapped_column(GUID(),       primary_key=True, default=_new_uuid)
    store_id:         Mapped[uuid.UUID] = mapped_column(GUID(),       ForeignKey("storefront_stores.id", ondelete="CASCADE"), nullable=False)
    target_type:      Mapped[str]       = mapped_column(String(20),   nullable=False)  # product|category|page|store
    target_id:        Mapped[str]       = mapped_column(String(255),  nullable=False)  # the target's id/slug ("" for store-level)
    meta_title:       Mapped[str]       = mapped_column(String(255),  default="")
    meta_description: Mapped[str]       = mapped_column(String(512),  default="")
    og_title:         Mapped[str]       = mapped_column(String(255),  default="")
    og_description:   Mapped[str]       = mapped_column(String(512),  default="")
    og_image:         Mapped[str|None]  = mapped_column(String(1024), nullable=True)
    canonical_url:    Mapped[str|None]  = mapped_column(String(1024), nullable=True)


class StorefrontSubscriberModel(Base):
    """Newsletter sign-up. Stored only here — never merged into user accounts."""

    __tablename__ = "storefront_subscribers"
    __table_args__ = (
        UniqueConstraint("store_id", "email", name="uq_storefront_subscribers_email"),
    )

    id:         Mapped[uuid.UUID] = mapped_column(GUID(),       primary_key=True, default=_new_uuid)
    store_id:   Mapped[uuid.UUID] = mapped_column(GUID(),       ForeignKey("storefront_stores.id", ondelete="CASCADE"), nullable=False)
    email:      Mapped[str]       = mapped_column(String(255),  nullable=False)
    created_at: Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=_utcnow)


class StorefrontMediaModel(Base):
    """Media library item (MinIO-backed)."""

    __tablename__ = "storefront_media"
    __table_args__ = (Index("ix_storefront_media_store", "store_id"),)

    id:         Mapped[uuid.UUID] = mapped_column(GUID(),       primary_key=True, default=_new_uuid)
    store_id:   Mapped[uuid.UUID] = mapped_column(GUID(),       ForeignKey("storefront_stores.id", ondelete="CASCADE"), nullable=False)
    media_path: Mapped[str]       = mapped_column(String(1024), nullable=False)
    media_type: Mapped[str]       = mapped_column(String(50),   nullable=False, default="image")  # image|video|file
    size_bytes: Mapped[int]       = mapped_column(BigInteger,   nullable=False, default=0)
    alt_text:   Mapped[str]       = mapped_column(String(512),  default="")
    folder:     Mapped[str]       = mapped_column(String(512),  default="")
    created_at: Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=_utcnow)


class StorefrontAnalyticsEventModel(Base):
    """Raw commerce event log (feeds HxAnalytics)."""

    __tablename__ = "storefront_analytics_events"
    __table_args__ = (
        Index("ix_storefront_events_store", "store_id"),
        Index("ix_storefront_events_created", "created_at"),
    )

    id:         Mapped[uuid.UUID] = mapped_column(GUID(),       primary_key=True, default=_new_uuid)
    store_id:   Mapped[uuid.UUID] = mapped_column(GUID(),       ForeignKey("storefront_stores.id", ondelete="CASCADE"), nullable=False)
    event:      Mapped[str]       = mapped_column(String(50),   nullable=False)  # store.page_view, store.add_to_basket, …
    data:       Mapped[dict]      = mapped_column(PortableJSON(), default=dict)
    session:    Mapped[str|None]  = mapped_column(String(128),  nullable=True)
    created_at: Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=_utcnow)


# ── HxDBMigrate — migrate an external source DB into Velaris (P1: connect + discover) ──

class HxDBMigrateSourceModel(Base):
    """A registered READ-ONLY external source database (migrate-INTO-Velaris).

    Credentials are stored HxVault-encrypted ({'_enc': …}); host/port/db/user are plain.
    source_type is a first-party allowlist value (postgresql | mysql | mariadb).
    """

    __tablename__ = "hxdbmigrate_sources"
    __table_args__ = (
        UniqueConstraint("name", "tenant_id", name="uq_hxdbmig_src_name_tenant"),
        Index("ix_hxdbmig_src_tenant", "tenant_id"),
    )

    id:          Mapped[uuid.UUID]  = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    name:        Mapped[str]        = mapped_column(String(255), nullable=False)
    source_type: Mapped[str]        = mapped_column(String(32),  nullable=False)
    host:        Mapped[str]        = mapped_column(String(255), nullable=False)
    port:        Mapped[int]        = mapped_column(Integer,     nullable=False)
    database:    Mapped[str]        = mapped_column(String(255), nullable=False)
    username:    Mapped[str]        = mapped_column(String(255), nullable=False)
    ssl_mode:    Mapped[str]        = mapped_column(String(16),  nullable=False, default="disable")
    credentials: Mapped[dict]       = mapped_column(PortableJSON(), default=dict)
    tenant_id:   Mapped[str | None] = mapped_column(String(255), nullable=True)
    # P6 lifecycle: active -> cutover -> completed | (rollback -> active)
    status:      Mapped[str]        = mapped_column(String(16), nullable=False, default="active")
    cutover_at:  Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rollback_window_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=72)
    last_connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_connect_ok:   Mapped[bool | None]     = mapped_column(Boolean, nullable=True)
    created_by:  Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at:  Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at:  Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class HxDBMigrateAnalysisModel(Base):
    """A discovery analysis of a source: Schema Autobiography + data-quality scoring."""

    __tablename__ = "hxdbmigrate_analyses"
    __table_args__ = (
        Index("ix_hxdbmig_an_source", "source_id"),
        Index("ix_hxdbmig_an_tenant", "tenant_id"),
        Index("ix_hxdbmig_an_created", "created_at"),
    )

    id:            Mapped[uuid.UUID]  = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    source_id:     Mapped[uuid.UUID]  = mapped_column(GUID(), nullable=False)
    tenant_id:     Mapped[str | None] = mapped_column(String(255), nullable=True)
    status:        Mapped[str]        = mapped_column(String(32), default="complete")  # complete | failed
    table_count:   Mapped[int | None] = mapped_column(Integer, nullable=True)
    quality_score: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 0–100
    pii_count:     Mapped[int | None] = mapped_column(Integer, nullable=True)  # sensitive columns (deep)
    report:        Mapped[dict]       = mapped_column(PortableJSON(), default=dict)
    error:         Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by:    Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at:    Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_utcnow)


class HxDBMigrateMigrationRunModel(Base):
    """A P4 batch data-migration run: source table rows → Velaris cases."""

    __tablename__ = "hxdbmigrate_migration_runs"
    __table_args__ = (
        Index("ix_hxdbmig_run_source", "source_id"),
        Index("ix_hxdbmig_run_tenant", "tenant_id"),
        Index("ix_hxdbmig_run_created", "created_at"),
    )

    id:             Mapped[uuid.UUID]  = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    source_id:      Mapped[uuid.UUID]  = mapped_column(GUID(), nullable=False)
    tenant_id:      Mapped[str | None] = mapped_column(String(255), nullable=True)
    table_name:     Mapped[str]        = mapped_column(String(255), nullable=False)
    case_type_id:   Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    kind:           Mapped[str]        = mapped_column(String(16), default="migrate")   # migrate|sync|cutover|rollback
    status:         Mapped[str]        = mapped_column(String(32), default="complete")  # complete|failed|dry_run
    pii_mode:       Mapped[str]        = mapped_column(String(16), default="safe")       # safe|exclude_all|as_is
    dry_run:        Mapped[bool]       = mapped_column(Boolean, default=False)
    rows_read:      Mapped[int | None] = mapped_column(Integer, nullable=True)
    rows_migrated:  Mapped[int | None] = mapped_column(Integer, nullable=True)
    rows_updated:   Mapped[int | None] = mapped_column(Integer, nullable=True)
    excluded_columns: Mapped[list]     = mapped_column(PortableJSON(), default=list)
    error:          Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by:     Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at:     Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_utcnow)


class HxDBMigrateRowLinkModel(Base):
    """P5 identity spine: one source row → one Velaris case.

    Unique on (source, table, source_pk) — sync upserts through it, and rollback
    knows exactly which cases a source's migration created.
    """

    __tablename__ = "hxdbmigrate_row_links"
    __table_args__ = (
        UniqueConstraint("source_id", "table_name", "source_pk",
                         name="uq_hxdbmig_link_row"),
        Index("ix_hxdbmig_link_source", "source_id"),
        Index("ix_hxdbmig_link_case",   "case_id"),
        Index("ix_hxdbmig_link_tenant", "tenant_id"),
    )

    id:             Mapped[uuid.UUID]  = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    source_id:      Mapped[uuid.UUID]  = mapped_column(GUID(), nullable=False)
    tenant_id:      Mapped[str | None] = mapped_column(String(255), nullable=True)
    table_name:     Mapped[str]        = mapped_column(String(255), nullable=False)
    source_pk:      Mapped[str]        = mapped_column(String(512), nullable=False)
    case_id:        Mapped[uuid.UUID]  = mapped_column(GUID(), nullable=False)
    case_type_id:   Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    row_checksum:   Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_synced_at: Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_utcnow)
    created_at:     Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_utcnow)


class HxEvolveInsightModel(Base):
    """An HxEvolve optimization insight — every proposal the system generated.

    HxEvolve's ONLY write surface. Discarded proposals stay here for provenance
    (§5) but are never surfaced; production config changes only ever happen
    through a human-approved HxBranch merge (P2 records the branch id).
    """

    __tablename__ = "hxevolve_insights"
    __table_args__ = (
        Index("ix_hxevolve_ins_ct",      "case_type_id"),
        Index("ix_hxevolve_ins_tenant",  "tenant_id"),
        Index("ix_hxevolve_ins_status",  "status"),
        Index("ix_hxevolve_ins_created", "created_at"),
    )

    id:             Mapped[uuid.UUID]  = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:      Mapped[str | None] = mapped_column(String(255), nullable=True)
    case_type_id:   Mapped[uuid.UUID]  = mapped_column(GUID(), nullable=False)
    signal:         Mapped[dict]       = mapped_column(PortableJSON(), default=dict)
    proposal:       Mapped[dict]       = mapped_column(PortableJSON(), default=dict)
    proposal_kind:  Mapped[str | None] = mapped_column(String(32), nullable=True)
    evidence:       Mapped[dict | None] = mapped_column(PortableJSON(), nullable=True)
    evidence_kind:  Mapped[str | None] = mapped_column(String(16), nullable=True)
    replay_run_id:  Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    rationale:      Mapped[str | None] = mapped_column(Text, nullable=True)
    status:         Mapped[str]        = mapped_column(String(32), default="surfaced")
    branch_id:      Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    staged_rule_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    created_by:     Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at:     Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_utcnow)


class HxEvolveConfigModel(Base):
    """Per-case-type HxEvolve objective/guardrail configuration (§4).

    Conservative defaults; scheduled scanning is opt-in per case type.
    """

    __tablename__ = "hxevolve_config"
    __table_args__ = (
        Index("ix_hxevolve_cfg_tenant",  "tenant_id"),
        Index("ix_hxevolve_cfg_enabled", "scan_enabled"),
    )

    case_type_id:         Mapped[uuid.UUID]  = mapped_column(GUID(), primary_key=True)
    tenant_id:            Mapped[str | None] = mapped_column(String(255), nullable=True)
    min_improvement:      Mapped[float]      = mapped_column(Float, default=0.10)
    max_auto_ratio_rise:  Mapped[float]      = mapped_column(Float, default=0.15)
    min_coverage:         Mapped[float]      = mapped_column(Float, default=0.7)
    min_determinate:      Mapped[int]        = mapped_column(Integer, default=50)
    scan_frequency_hours: Mapped[int]        = mapped_column(Integer, default=24)
    scan_enabled:         Mapped[bool]       = mapped_column(Boolean, default=False)
    # cumulative-drift guardrail (§6): re-check the holistic baseline after
    # every N merged HxEvolve changes
    drift_check_every_n_changes: Mapped[int] = mapped_column(Integer, default=3)
    updated_by:           Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_at:           Mapped[datetime]   = mapped_column(DateTime(timezone=True),
                                                             default=_utcnow, onupdate=_utcnow)


class HxEvolveBaselineModel(Base):
    """HxEvolve cumulative-drift guardrail (§6): the holistic reference point.

    Each replay proof only compares against the immediately previous config, so
    a chain of individually-improving merges can compound into a regression.
    This row pins the case-type's metrics when HxEvolve first saw it; after
    every N merged changes the CURRENT metrics are compared against it — a
    cumulative regression freezes further scans until an admin re-baselines.
    """

    __tablename__ = "hxevolve_baselines"

    case_type_id:       Mapped[uuid.UUID]  = mapped_column(GUID(), primary_key=True)
    tenant_id:          Mapped[str | None] = mapped_column(String(255), nullable=True)
    metrics:            Mapped[dict]       = mapped_column(PortableJSON(), nullable=False)
    merged_at_baseline: Mapped[int]        = mapped_column(Integer, nullable=False, default=0)
    checked_through:    Mapped[int]        = mapped_column(Integer, nullable=False, default=0)
    frozen:             Mapped[bool]       = mapped_column(Boolean, nullable=False, default=False)
    frozen_reason:      Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by:         Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at:         Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_utcnow)
    rebaselined_at:     Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReplayRunModel(Base):
    """A HxReplay run (single-case or cohort counterfactual replay).

    Replay output lives ONLY here + replay_results — never in real case tables.
    """

    __tablename__ = "replay_runs"
    __table_args__ = (
        Index("ix_replay_runs_tenant", "tenant_id"),
        Index("ix_replay_runs_created", "created_at"),
        Index("ix_replay_runs_status", "status"),
    )

    id:            Mapped[uuid.UUID]  = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:     Mapped[str | None] = mapped_column(String(255), nullable=True)
    kind:          Mapped[str]        = mapped_column(String(16), default="single")   # single|cohort
    status:        Mapped[str]        = mapped_column(String(32), default="pending")  # pending|running|complete|failed
    branch_id:     Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    candidate:     Mapped[dict]       = mapped_column(PortableJSON(), default=dict)
    case_id:       Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    cohort_filter: Mapped[dict]       = mapped_column(PortableJSON(), default=dict)
    config_epoch:  Mapped[str]        = mapped_column(String(32), default="current+branch")
    estimate:      Mapped[bool]       = mapped_column(Boolean, default=False)   # P3 opt-in
    summary:       Mapped[dict | None] = mapped_column(PortableJSON(), nullable=True)
    result_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    anchored:      Mapped[bool]       = mapped_column(Boolean, default=False)
    error:         Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by:    Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at:    Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_utcnow)
    started_at:    Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at:   Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ReplayResultModel(Base):
    """Per-case outcome of a replay run: baseline vs counterfactual + determinacy."""

    __tablename__ = "replay_results"
    __table_args__ = (
        Index("ix_replay_results_run", "run_id"),
        Index("ix_replay_results_case", "case_id"),
    )

    id:                     Mapped[uuid.UUID]  = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    run_id:                 Mapped[uuid.UUID]  = mapped_column(GUID(), ForeignKey("replay_runs.id", ondelete="CASCADE"), nullable=False)
    case_id:                Mapped[uuid.UUID]  = mapped_column(GUID(), nullable=False)
    tenant_id:              Mapped[str | None] = mapped_column(String(255), nullable=True)
    determinacy:            Mapped[str]        = mapped_column(String(16), default="determinate")  # determinate|indeterminate
    exclusion_reason:       Mapped[str | None] = mapped_column(Text, nullable=True)
    divergence_point:       Mapped[str | None] = mapped_column(String(255), nullable=True)
    baseline_metrics:       Mapped[dict]       = mapped_column(PortableJSON(), default=dict)
    counterfactual_metrics: Mapped[dict | None] = mapped_column(PortableJSON(), nullable=True)
    trace:                  Mapped[dict | None] = mapped_column(PortableJSON(), nullable=True)
    created_at:             Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_utcnow)


class RateCardModel(Base):
    """Per-tenant cost rate (HxReplay P4 / Case Costing). role='*' = tenant default.

    Commercially sensitive: read/write only through the HxGuard-gated costing API,
    never exposed to portal/customer identities.
    """

    __tablename__ = "rate_cards"
    __table_args__ = (
        UniqueConstraint("tenant_id", "role", name="uq_rate_cards_tenant_role"),
    )

    id:          Mapped[uuid.UUID]  = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:   Mapped[str | None] = mapped_column(String(255), nullable=True)
    role:        Mapped[str]        = mapped_column(String(100), default="*")
    hourly_rate: Mapped[float]      = mapped_column(Float, nullable=False)
    currency:    Mapped[str]        = mapped_column(String(8), default="USD")
    created_by:  Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at:  Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at:  Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MCPIdempotencyKeyModel(Base):
    """HxNexus Operator (MCP) P2: idempotency record for a mutating tool call.

    Scoped by (user_id, idempotency_key) so a caller's key namespace is private.
    Claim-first: a 'pending' row is committed before the write executes, so a
    concurrent duplicate cannot double-apply; it flips to 'done' with the stored
    response once the write commits.
    """

    __tablename__ = "mcp_idempotency_keys"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_mcp_idem_user_key"),
        Index("ix_mcp_idem_created", "created_at"),
    )

    id:              Mapped[uuid.UUID]  = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    user_id:         Mapped[str]        = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str]        = mapped_column(String(255), nullable=False)
    tool_name:       Mapped[str]        = mapped_column(String(100), nullable=False)
    request_hash:    Mapped[str]        = mapped_column(String(64), nullable=False)
    status:          Mapped[str]        = mapped_column(String(20), default="pending")
    response_json:   Mapped[dict | None] = mapped_column(PortableJSON(), nullable=True)
    is_error:        Mapped[bool]       = mapped_column(Boolean, default=False)
    created_at:      Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_utcnow)


class MCPActionProposalModel(Base):
    """HxNexus Operator (MCP) P3: a stateful action awaiting human confirmation.

    The AI proposes; a human confirms. Execution re-checks authorization as the
    CONFIRMER (who is accountable), and the row's status gates it to once.
    """

    __tablename__ = "mcp_action_proposals"
    __table_args__ = (
        Index("ix_mcp_prop_status", "status"),
        Index("ix_mcp_prop_user", "user_id"),
        Index("ix_mcp_prop_case", "case_id"),
        Index("ix_mcp_prop_tenant", "tenant_id"),
    )

    id:             Mapped[uuid.UUID]  = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    user_id:        Mapped[str]        = mapped_column(String(255), nullable=False)
    tenant_id:      Mapped[str | None] = mapped_column(String(255), nullable=True)  # proposer's tenant; scopes review/confirm
    tool_name:      Mapped[str]        = mapped_column(String(100), nullable=False)
    arguments_json: Mapped[dict]       = mapped_column(PortableJSON(), nullable=False)
    case_id:        Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    summary:        Mapped[str | None] = mapped_column(Text, nullable=True)
    status:         Mapped[str]        = mapped_column(String(20), default="pending")
    result_json:    Mapped[dict | None] = mapped_column(PortableJSON(), nullable=True)
    is_error:       Mapped[bool]       = mapped_column(Boolean, default=False)
    decided_by:     Mapped[str | None] = mapped_column(String(255), nullable=True)
    decided_at:     Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at:     Mapped[datetime]   = mapped_column(DateTime(timezone=True), nullable=False)
    created_at:     Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_utcnow)


class CaseTimeEntryModel(Base):
    """§11 P2: an explicit, human-captured effort entry (timer or timesheet).

    Wall-clock event durations (case_event_log) measure elapsed time; these
    entries measure BILLABLE EFFORT — a worker juggling three cases logs what
    each actually took. cost = billable time × role rate (rate_cards).
    """

    __tablename__ = "case_time_entries"
    __table_args__ = (
        Index("ix_time_entries_case", "case_id"),
        Index("ix_time_entries_user", "user_id"),
        Index("ix_time_entries_tenant", "tenant_id"),
    )

    id:               Mapped[uuid.UUID]  = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:        Mapped[str | None] = mapped_column(String(255), nullable=True)
    case_id:          Mapped[uuid.UUID]  = mapped_column(GUID(), nullable=False)
    user_id:          Mapped[str]        = mapped_column(String(255), nullable=False)
    role:             Mapped[str | None] = mapped_column(String(100), nullable=True)   # rate-card lookup; NULL = tenant default '*'
    source:           Mapped[str]        = mapped_column(String(20), nullable=False, default="timesheet")  # timer | timesheet
    started_at:       Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at:         Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)      # NULL + source=timer = running
    duration_seconds: Mapped[int]        = mapped_column(Integer, nullable=False, default=0)
    billable:         Mapped[bool]       = mapped_column(Boolean, nullable=False, default=True)
    note:             Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at:       Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_utcnow)


class MCPTokenGrantModel(Base):
    """HxNexus Operator (MCP) P4: an external-agent scoped-token grant.

    The JWT is stateless (claims: token_use="mcp", mcp_scope, jti=this row's
    id); this row is its server-side anchor — listing, audit, and INSTANT
    revocation (the transport checks the row on every call). A grant is a
    restriction of the grantor's own authority: authorization always runs as
    the grantor, the grant only shrinks the tool surface.
    """

    __tablename__ = "mcp_token_grants"
    __table_args__ = (
        Index("ix_mcp_grant_user", "user_id"),
        Index("ix_mcp_grant_expires", "expires_at"),
    )

    id:         Mapped[uuid.UUID]  = mapped_column(GUID(), primary_key=True, default=_new_uuid)  # = JWT jti
    user_id:    Mapped[str]        = mapped_column(String(255), nullable=False)   # grantor
    tenant_id:  Mapped[str | None] = mapped_column(String(255), nullable=True)
    tools:      Mapped[list]       = mapped_column(PortableJSON(), nullable=False)  # granted tool names
    label:      Mapped[str | None] = mapped_column(String(255), nullable=True)     # e.g. which agent this is for
    revoked:    Mapped[bool]       = mapped_column(Boolean, nullable=False, default=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    expires_at: Mapped[datetime]   = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_utcnow)


class CaseSessionModel(Base):
    """HxMeet P1: a real-time session (meeting) attached to a case.

    One provider-agnostic abstraction, two drivers: `off_platform` (P1 —
    Teams/Zoom/Meet/generic connector creates the meeting, recording stays
    with the provider) and `embedded` (P2+ — self-hosted LiveKit).
    recording_document_id / audit_anchor_ref stay NULL until the embedded
    driver's sealed-recording path (P3) lands.
    """

    __tablename__ = "case_sessions"
    __table_args__ = (
        Index("ix_case_sessions_case", "case_id"),
        Index("ix_case_sessions_tenant", "tenant_id"),
        Index("ix_case_sessions_status", "status"),
    )

    id:                    Mapped[uuid.UUID]      = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    case_id:               Mapped[uuid.UUID]      = mapped_column(GUID(), ForeignKey("case_instances.id", ondelete="CASCADE"), nullable=False)
    tenant_id:             Mapped[str | None]     = mapped_column(String(255), nullable=True)
    driver:                Mapped[str]            = mapped_column(String(20), nullable=False, default="off_platform")  # off_platform | embedded
    provider:              Mapped[str]            = mapped_column(String(50), nullable=False)   # teams | zoom | gmeet | generic (P2+: livekit)
    connector_id:          Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)      # connector_registry row that created it
    status:                Mapped[str]            = mapped_column(String(20), nullable=False, default="created")  # created | active | ended | cancelled
    title:                 Mapped[str | None]     = mapped_column(String(255), nullable=True)
    external_meeting_id:   Mapped[str | None]     = mapped_column(Text, nullable=True)
    join_url:              Mapped[str | None]     = mapped_column(Text, nullable=True)
    scheduled_at:          Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_by:            Mapped[str]            = mapped_column(String(255), nullable=False)
    started_at:            Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at:              Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # P3 sealed recording (mig 114): intent declared at start, per-join consent
    # stamps on participants; none > recording > processing > sealed|failed.
    record_intent:         Mapped[bool]           = mapped_column(Boolean, nullable=False, default=False)
    recording_status:      Mapped[str]            = mapped_column(String(20), nullable=False, default="none")
    recording_egress_id:   Mapped[str | None]     = mapped_column(Text, nullable=True)
    recording_document_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("documents.id"), nullable=True)
    audit_anchor_ref:      Mapped[str | None]     = mapped_column(Text, nullable=True)
    # P4a-live-2 sealed live transcript (mig 119) — the recording's twin:
    # none > sealed|failed, sealed .hxsealed case document + chain anchor.
    transcript_status:      Mapped[str]            = mapped_column(String(20), nullable=False, default="none")
    transcript_document_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("documents.id"), nullable=True)
    transcript_anchor_ref:  Mapped[str | None]     = mapped_column(Text, nullable=True)
    created_at:            Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow)


class CaseSessionCaptionSegmentModel(Base):
    """HxMeet P4a-live-2: staging for finalized live-caption segments of a
    RECORD-INTENT session. Speaker comes from the verified room token, never
    the client. Composed + tenant-DEK sealed on session end, then deleted —
    plaintext conversation text never rests here longer than the session."""

    __tablename__ = "case_session_caption_segments"
    __table_args__ = (
        Index("idx_caption_segments_session", "session_id", "spoken_at"),
    )

    id:         Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    session_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("case_sessions.id", ondelete="CASCADE"), nullable=False)
    tenant_id:  Mapped[str | None] = mapped_column(String(255), nullable=True)
    speaker:    Mapped[str]       = mapped_column(String(255), nullable=False)
    text:       Mapped[str]       = mapped_column(Text, nullable=False)
    spoken_at:  Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=_utcnow)
    created_at: Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=_utcnow)


class CaseSessionParticipantModel(Base):
    """HxMeet P2: a participant of a case session (embedded/LiveKit driver).

    Created at invite time (guest) or first token mint (worker); joined_at /
    left_at are stamped by LiveKit webhooks. Guest invites are single-use —
    only the SHA-256 of the invite token is stored (same posture as the
    portal-customer OTP) and it is consumed atomically on exchange.
    consent_recorded_at stays NULL until the P3 sealed-recording path.
    """

    __tablename__ = "case_session_participants"
    __table_args__ = (
        Index("ix_csp_session", "session_id"),
        Index("ix_csp_tenant", "tenant_id"),
    )

    id:                  Mapped[uuid.UUID]        = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    session_id:          Mapped[uuid.UUID]        = mapped_column(GUID(), ForeignKey("case_sessions.id", ondelete="CASCADE"), nullable=False)
    tenant_id:           Mapped[str | None]       = mapped_column(String(255), nullable=True)
    identity:            Mapped[str]              = mapped_column(String(512), nullable=False)  # user:{id} | customer:{uuid} | email:{addr}
    display_name:        Mapped[str | None]       = mapped_column(String(255), nullable=True)
    role:                Mapped[str]              = mapped_column(String(20), nullable=False, default="guest")  # host | guest
    invited_by:          Mapped[str | None]       = mapped_column(String(255), nullable=True)   # worker user_id (NULL for the host's own row)
    invite_token_hash:   Mapped[str | None]       = mapped_column(String(64), nullable=True, unique=True)  # SHA-256, NULL for internal joins
    invite_expires_at:   Mapped[datetime | None]  = mapped_column(DateTime(timezone=True), nullable=True)
    token_used_at:       Mapped[datetime | None]  = mapped_column(DateTime(timezone=True), nullable=True)
    joined_at:           Mapped[datetime | None]  = mapped_column(DateTime(timezone=True), nullable=True)
    left_at:             Mapped[datetime | None]  = mapped_column(DateTime(timezone=True), nullable=True)
    consent_recorded_at: Mapped[datetime | None]  = mapped_column(DateTime(timezone=True), nullable=True)   # P3
    created_at:          Mapped[datetime]         = mapped_column(DateTime(timezone=True), default=_utcnow)


class CaseSessionIntelligenceModel(Base):
    """HxMeet P4a (mig 118) — one local-only analysis run per sealed session.

    The transcript lives as a case document; summary/action items and the
    model versions that produced them live here, so results stay
    interpretable after model upgrades. Re-running replaces the row.
    """
    __tablename__ = "case_session_intelligence"

    session_id:             Mapped[uuid.UUID]      = mapped_column(GUID(), ForeignKey("case_sessions.id", ondelete="CASCADE"), primary_key=True)
    status:                 Mapped[str]            = mapped_column(String(20), nullable=False, default="pending")
    transcript_document_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    summary:                Mapped[str | None]     = mapped_column(Text, nullable=True)
    action_items:           Mapped[list]           = mapped_column(PortableJSON(), default=list)
    language:               Mapped[str | None]     = mapped_column(String(16), nullable=True)
    duration_seconds:       Mapped[int | None]     = mapped_column(Integer, nullable=True)
    model_versions:         Mapped[dict]           = mapped_column(PortableJSON(), default=dict)
    error:                  Mapped[str | None]     = mapped_column(Text, nullable=True)
    requested_by:           Mapped[str]            = mapped_column(String(255), nullable=False)
    created_at:             Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow)
    completed_at:           Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DocumentVerificationModel(Base):
    """HxMeet P4b (mig 118) — the document-first gate's evidence record.

    Automated checks + the worker's checklist verdict for one uploaded
    document. Multiple rows per document are allowed (re-verification);
    the latest row is authoritative.
    """
    __tablename__ = "document_verifications"
    __table_args__ = (
        Index("idx_doc_verifications_case", "case_id", "created_at"),
        Index("idx_doc_verifications_doc", "document_id"),
    )

    id:          Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    case_id:     Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("case_instances.id", ondelete="CASCADE"), nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    status:      Mapped[str]       = mapped_column(String(20), nullable=False, default="review")
    checks:      Mapped[list]      = mapped_column(PortableJSON(), default=list)
    verified_by: Mapped[str]       = mapped_column(String(255), nullable=False)
    notes:       Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at:  Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=_utcnow)
