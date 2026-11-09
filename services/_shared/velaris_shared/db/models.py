"""Case-service database models (async SQLAlchemy ORM).

Maps to the case management schema tables.  Uses the same
patterns as ``engine/helix_engine/db/models.py``.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations
from typing import Any

import json
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import (
    BigInteger,
    SmallInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    TypeDecorator
)
from sqlalchemy.types import CHAR, TypeEngine
from sqlalchemy.dialects import mysql  # dialect-specific variants for index-key limits (DB SDK)
from sqlalchemy import event
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


class PortableIntArray(PortableArray):
    """PostgreSQL INT[], JSON list elsewhere.

    `business_calendars.work_days` is `INT[]` on PostgreSQL (migration 002) and
    JSON on MySQL/MariaDB. It was mapped as PortableJSON, which binds JSONB, so
    every calendar INSERT on PostgreSQL failed — creating one, and 8e's fork of a
    default on a tenant's first edit. Found live 2026-10-02.
    """

    cache_ok = True

    def load_dialect_impl(self, dialect) -> TypeEngine:
        if dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import ARRAY
            return dialect.type_descriptor(ARRAY(Integer))
        from sqlalchemy import JSON
        return dialect.type_descriptor(JSON())


class MicroDateTime(TypeDecorator):
    """Timezone-aware timestamp that keeps MICROSECOND precision on every backend.

    PostgreSQL ``TIMESTAMPTZ`` is already microsecond, but MySQL/MariaDB ``DATETIME``
    defaults to SECOND precision. That makes "latest row wins" ordering
    (``ORDER BY created_at DESC``) non-deterministic for append-only rows written in
    the same second — the tiebreak then falls to a random UUID id. Emitting
    ``DATETIME(6)`` on MySQL/MariaDB restores the sub-second ordering the append-only
    sentinel tables (case_outcomes, case_risk_feedback) depend on. SQLite keeps its
    native (microsecond) datetime handling.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def load_dialect_impl(self, dialect) -> TypeEngine:
        if dialect.name in ("mysql", "mariadb"):
            return dialect.type_descriptor(mysql.DATETIME(fsp=6))
        return dialect.type_descriptor(DateTime(timezone=True))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_uuid() -> uuid.UUID:
    return uuid.uuid4()


class Base(DeclarativeBase):
    pass


# ═══════════════════════════════════════════════════════════════════════
# DESIGN-TIME TABLES
# ═══════════════════════════════════════════════════════════════════════


def case_type_tenant_key(tenant_id: Any) -> str:
    """`case_types.tenant_key` for a tenant id; 'platform' for a default."""
    return str(tenant_id) if tenant_id else "platform"


class CaseTypeModel(Base):
    __tablename__ = "case_types"
    # Unique per tenant since migration 212 (Oct 8i); was (name, version)
    # across the whole install.
    __table_args__ = (
        UniqueConstraint("tenant_key", "name", "version",
                         name="ux_case_types_tenant_name_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=_new_uuid
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("tenants.id"), nullable=True, index=True
    )
    #: `tenant_id` as text, or 'platform' for a default (NULL tenant) — NULL <>
    #: NULL, so a UNIQUE over the nullable column would not constrain defaults.
    #: Kept in step with `tenant_id` by the default below and the `set` listener
    #: after this class; never written by hand.
    tenant_key: Mapped[str] = mapped_column(
        String(36), nullable=False,
        default=lambda ctx: case_type_tenant_key(
            ctx.get_current_parameters().get("tenant_id")),
    )
    #: A tenant's copy-on-write fork of a default records where it came from
    #: and when, so a newer default is offered, never applied over its edits.
    forked_from_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    forked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
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
        MicroDateTime(), default=_utcnow
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


@event.listens_for(CaseTypeModel.tenant_id, "set")
def _case_type_tenant_key_follows(target, value, _old, _initiator):
    # An ORM write that changes the owner (a backfill, a tenant transfer) moves
    # the uniqueness key with it. A Core UPDATE of `tenant_id` must set
    # `tenant_key` itself.
    target.tenant_key = case_type_tenant_key(value)


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
        Index("idx_cases_created_by", "created_by"),  # migration 228
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
    # Event sourcing cutover marker (#30a, mig 146). TRUE only for cases created
    # after the event store was switched on — §7.5 is explicit that pre-cutover
    # history cannot be honestly reconstructed, so it is never backfilled.
    # HxReplay and stream verification apply to these cases only.
    event_sourced: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_by: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        MicroDateTime(), default=_utcnow
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
    # 199 — soft delete. The row and its history stay; the case disappears from
    # every list and every read. A hard delete would break the hash-linked audit
    # chain and would be a second, weaker erasure than the one this platform
    # already implements (`anonymize`, which scrubs the subject and KEEPS the
    # case). Deliberately NOT a status: a status says where a case is in its
    # process, and modelling deletion as one would let a tenant delete a case by
    # editing a dropdown.
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deleted_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

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
        MicroDateTime(), default=_utcnow
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
        MicroDateTime(), default=_utcnow
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
        MicroDateTime(), nullable=False
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
        MicroDateTime(), default=_utcnow
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
        MicroDateTime(), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


# ═══════════════════════════════════════════════════════════════════════
# DESIGN-TIME ARTIFACT STORAGE
# ═══════════════════════════════════════════════════════════════════════


class DataModelModel(Base):
    __tablename__ = "data_models"
    __table_args__ = (UniqueConstraint("tenant_key", "name", "version", name="ux_data_models_tenant_key"),)  # 8e, mig 214

    id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=_new_uuid
    )

    # 8e (mig 214): NULL tenant = a default every tenant reads; a tenant's

    # edit of a default is its fork (`forked_from_id`). See tenancy/inherit.py.

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(

        GUID(), ForeignKey("tenants.id"), nullable=True, index=True)

    tenant_key: Mapped[str] = mapped_column(

        String(36), nullable=False,

        default=lambda ctx: case_type_tenant_key(ctx.get_current_parameters().get("tenant_id")))

    forked_from_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)

    forked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[str] = mapped_column(String(50), nullable=False)
    definition_json: Mapped[dict] = mapped_column(PortableJSON(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        MicroDateTime(), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class FormDefinitionModel(Base):
    __tablename__ = "form_definitions"
    __table_args__ = (UniqueConstraint("tenant_key", "name", "version", name="ux_form_definitions_tenant_key"),)  # 8e, mig 214

    id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=_new_uuid
    )

    # 8e (mig 214): NULL tenant = a default every tenant reads; a tenant's

    # edit of a default is its fork (`forked_from_id`). See tenancy/inherit.py.

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(

        GUID(), ForeignKey("tenants.id"), nullable=True, index=True)

    tenant_key: Mapped[str] = mapped_column(

        String(36), nullable=False,

        default=lambda ctx: case_type_tenant_key(ctx.get_current_parameters().get("tenant_id")))

    forked_from_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)

    forked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[str] = mapped_column(String(50), nullable=False)
    data_model_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(),
        ForeignKey("data_models.id"),
        nullable=True,
    )
    definition_json: Mapped[dict] = mapped_column(PortableJSON(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        MicroDateTime(), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class RuleDefinitionModel(Base):
    __tablename__ = "rule_definitions"
    __table_args__ = (UniqueConstraint("tenant_key", "name", "version", name="ux_rule_definitions_tenant_key"),)  # 8e, mig 214

    id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=_new_uuid
    )

    # 8e (mig 214): NULL tenant = a default every tenant reads; a tenant's

    # edit of a default is its fork (`forked_from_id`). See tenancy/inherit.py.

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(

        GUID(), ForeignKey("tenants.id"), nullable=True, index=True)

    tenant_key: Mapped[str] = mapped_column(

        String(36), nullable=False,

        default=lambda ctx: case_type_tenant_key(ctx.get_current_parameters().get("tenant_id")))

    forked_from_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)

    forked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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
        MicroDateTime(), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class BusinessCalendarModel(Base):
    """Business calendar for SLA calculations (Phase 8)."""
    __tablename__ = "business_calendars"
    __table_args__ = (UniqueConstraint("tenant_key", "name", name="ux_business_calendars_tenant_key"),)  # 8e, mig 214

    id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=_new_uuid
    )

    # 8e (mig 214): NULL tenant = a default every tenant reads; a tenant's

    # edit of a default is its fork (`forked_from_id`). See tenancy/inherit.py.

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(

        GUID(), ForeignKey("tenants.id"), nullable=True, index=True)

    tenant_key: Mapped[str] = mapped_column(

        String(36), nullable=False,

        default=lambda ctx: case_type_tenant_key(ctx.get_current_parameters().get("tenant_id")))

    forked_from_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)

    forked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)  # unique per tenant since 8e (mig 214)
    timezone: Mapped[str] = mapped_column(String(100), default="UTC")
    work_days: Mapped[list] = mapped_column(PortableIntArray(), default=lambda: [1,2,3,4,5])  # INT[] on PG
    work_start_hour: Mapped[int] = mapped_column(Integer, default=9)
    work_end_hour: Mapped[int] = mapped_column(Integer, default=17)
    holidays: Mapped[list] = mapped_column(PortableJSON(), default=list)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        MicroDateTime(), default=_utcnow
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
    created_at: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
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
    created_at: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)


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
    timestamp: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(50), nullable=True)
    extra_metadata: Mapped[dict] = mapped_column("metadata", PortableJSON(), default=dict)


class MigrationScanModel(Base):
    """Scout migration scan record (Phase 16)."""
    __tablename__ = "migration_scans"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)

    #: Owning tenant (8e, migration 213). Existing rows are the default tenant's.

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(

        GUID(), ForeignKey("tenants.id"), nullable=True, index=True)
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
    created_at: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
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
    created_at: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
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
    created_at: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)


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
    created_at: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
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
    created_at: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
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
    started_at: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
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
    ran_at: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)


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
    created_at: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)


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
    timestamp: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
    #: Migration 230: the TRUSTED tenant of the signed-in request that raised it;
    #: NULL = the platform's (raised outside a request, or before 2026-11-04).
    tenant_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: Migration 231: how many identical events this row stands for. 1 = one
    #: event (every row before 231). >1 = a summary of refused repeats inside a
    #: minute (AuditMiddleware). Count events as SUM(repeat_count), not rows.
    repeat_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1,
                                              server_default="1")


class RetentionPolicyModel(Base):
    """Data retention policy (Phase 20)."""
    __tablename__ = "retention_policies"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # NULL = platform default, applied to any tenant with no policy of its own.
    # Uniqueness moved to (tenant_id, resource_type) in migration 153: a bare
    # UNIQUE on resource_type made one tenant's retention period everyone's,
    # and retention is a per-customer contractual commitment.
    tenant_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False)
    retention_days: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(20), default="archive")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)


class GDPRRequestModel(Base):
    """GDPR data subject request (Phase 20)."""
    __tablename__ = "gdpr_requests"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)

    #: Owning tenant (8e, migration 213). Existing rows are the default tenant's.

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(

        GUID(), ForeignKey("tenants.id"), nullable=True, index=True)
    subject_id: Mapped[str] = mapped_column(String(255), nullable=False)
    request_type: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    requested_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_file: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)


class MigrationProjectModel(Base):
    """Migration project — tracks a full app migration end-to-end (Phase 21)."""
    __tablename__ = "migration_projects"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)

    #: Owning tenant (8e, migration 213). Existing rows are the default tenant's.

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(

        GUID(), ForeignKey("tenants.id"), nullable=True, index=True)
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
    created_at: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
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
    created_at: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)


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
        MicroDateTime(), nullable=False, default=_utcnow, index=True,
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

    created_at: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow, nullable=False)
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
    created_at: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow, nullable=False)

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
        MicroDateTime(), default=_utcnow, nullable=False,
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
        Index("ix_user_directory_external", "external_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    # Mirrors velaris_users.external_id — the two rows are one principal and the
    # spine writes both in the same transaction (mig 157).
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    manager_user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    access_group_ids: Mapped[list] = mapped_column(PortableJSON(), default=list)  # deprecated — use operator_access_groups
    roles: Mapped[list] = mapped_column(PortableJSON(), default=list)             # deprecated — use access_roles via group
    skills: Mapped[list] = mapped_column(PortableJSON(), default=list)            # ["claims","fraud"] — skill_based routing (mig 139)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    metadata_json: Mapped[dict] = mapped_column(PortableJSON(), default=dict)
    # P37 — persists the operator's active access group across sessions
    current_access_group_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow,
    )
# <<< P34b user directory


# >>> P70 Calendar — people time

class UserWorkPatternModel(Base):
    """A person's normal week, when it differs from the organisation's.

    `business_calendars` describes the org — work days, hours, holidays — and the
    SLA clock counts against it. This describes one person, and every field is
    optional: **no row means "the tenant's default business calendar"**, which is
    what almost everyone will be.
    """

    __tablename__ = "user_work_patterns"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", name="user_work_patterns_uq"),
        Index("ix_user_work_patterns_cal", "business_calendar_id"),
    )

    id:        Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[str]       = mapped_column(String(255), nullable=False)
    user_id:   Mapped[str]       = mapped_column(String(255), nullable=False)
    business_calendar_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("business_calendars.id", ondelete="SET NULL"), nullable=True)
    #: ISO weekday numbers, 1 = Monday. NULL inherits from the calendar above.
    work_days:       Mapped[list | None] = mapped_column(PortableJSON(), nullable=True)
    work_start_hour: Mapped[int | None]  = mapped_column(Integer, nullable=True)
    work_end_hour:   Mapped[int | None]  = mapped_column(Integer, nullable=True)
    timezone:        Mapped[str | None]  = mapped_column(String(64), nullable=True)
    updated_by: Mapped[str]      = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class UserAbsenceModel(Base):
    """When a person is not available. The table every consumer reads.

    Only an **approved** absence changes anything. A request that rerouted work
    the moment it was typed would let anybody quietly stop their own queue.

    `ends_at` is a timestamp rather than a date because half days exist, and a
    date-only column makes every caller invent its own convention for them.

    `delegate_user_id` is on the absence, not on the user: cover is a property of
    *this* absence — a person may hand over to somebody different in December
    than in July.
    """

    __tablename__ = "user_absences"
    __table_args__ = (
        Index("ix_user_absences_lookup", "tenant_id", "user_id", "status",
              "starts_at", "ends_at"),
        Index("ix_user_absences_window", "tenant_id", "starts_at", "ends_at"),
        Index("ix_user_absences_status", "tenant_id", "status"),
    )

    id:        Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[str]       = mapped_column(String(255), nullable=False)
    user_id:   Mapped[str]       = mapped_column(String(255), nullable=False)
    #: holiday | sick | training | other
    kind:      Mapped[str]       = mapped_column(String(20), nullable=False, default="holiday")
    starts_at: Mapped[datetime]  = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at:   Mapped[datetime]  = mapped_column(DateTime(timezone=True), nullable=False)
    all_day:   Mapped[bool]      = mapped_column(Boolean, nullable=False, default=True)
    #: requested | approved | cancelled
    status:    Mapped[str]       = mapped_column(String(20), nullable=False, default="requested")
    approved_by: Mapped[str | None]      = mapped_column(String(255), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delegate_user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: manual | case | import — `case` means a Leave Request case type created it.
    source:    Mapped[str]       = mapped_column(String(20), nullable=False, default="manual")
    case_id:   Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    #: The iCalendar UID an out-of-office event was imported from (mig 175).
    #: NULL for everything created here, which is what stops an import adopting
    #: a row somebody typed.
    external_uid: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: Which subscription put it here (mig 176). NULL means an uploaded file or
    #: something typed — a feed refresh never touches those.
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    #: Personal. The person, their manager and an admin — never a team view:
    #: "sick" is health data.
    note:      Mapped[str]       = mapped_column(Text, nullable=False, default="")
    created_by: Mapped[str]      = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

class CalendarEventModel(Base):
    """A team meeting, or anything else with a time and attendees.

    HxMeet already runs meetings, but every one of them hangs off a **case**. A
    stand-up belongs to no case, so there was nowhere to put it.

    **Calendar owns when and who; HxMeet owns the room.** An event may point at
    a meet session through `meet_session_id`; it never becomes one, and nothing
    here starts video — a second room would mean two places to look for a
    recording and two retention rules.

    `attendee_user_ids` is a JSON array rather than a join table: attendance is
    read whole every time. No per-attendee response status, deliberately — an
    accept/decline flow is a case type's job, not a column.
    """

    __tablename__ = "calendar_events"
    __table_args__ = (
        Index("ix_calendar_events_window", "tenant_id", "starts_at", "ends_at"),
        Index("ix_calendar_events_organiser", "tenant_id", "organiser_user_id"),
    )

    id:          Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:   Mapped[str]       = mapped_column(String(255), nullable=False)
    title:       Mapped[str]       = mapped_column(String(255), nullable=False)
    description: Mapped[str]       = mapped_column(Text, nullable=False, default="")
    starts_at:   Mapped[datetime]  = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at:     Mapped[datetime]  = mapped_column(DateTime(timezone=True), nullable=False)
    timezone:    Mapped[str]       = mapped_column(String(64), nullable=False, default="UTC")
    organiser_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    attendee_user_ids: Mapped[list] = mapped_column(PortableJSON(), nullable=False, default=list)
    case_id:         Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    meet_session_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    #: A rule, not fifty rows a year: {"freq": "daily|weekly|monthly",
    #: "interval": 1, "until": "<iso>"}. Occurrences are expanded on READ inside
    #: the window asked for, so an endless daily meeting costs one row and can
    #: never fill a table.
    recurrence:  Mapped[dict | None] = mapped_column(PortableJSON(), nullable=True)
    #: How one occurrence is cancelled without destroying the series. Without it
    #: "we skip next Monday" means rebuilding the series and losing its history.
    excluded_dates: Mapped[list] = mapped_column(PortableJSON(), nullable=False, default=list)
    #: Guests with no account here. Deliberately NOT in `attendee_user_ids`:
    #: every availability lookup would otherwise try to find a working pattern
    #: for an email address.
    external_emails: Mapped[list] = mapped_column(PortableJSON(), nullable=False, default=list)
    #: A link somebody pasted (Teams, Zoom, a bridge number). `meet_session_id`
    #: stays the anchor for a room this platform created.
    meeting_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_by:  Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: team | private — the title is the whole payload, so this is the only
    #: privacy control the object needs.
    visibility:  Mapped[str]       = mapped_column(String(20), nullable=False, default="team")
    #: scheduled | cancelled
    status:      Mapped[str]       = mapped_column(String(20), nullable=False, default="scheduled")
    #: manual | import (mig 175) — the same vocabulary `user_absences` has used
    #: since it was created, so both halves of the calendar answer "where did
    #: this come from" the same way.
    source:      Mapped[str]       = mapped_column(String(20), nullable=False, default="manual")
    #: The iCalendar UID this row was imported from. What makes a second import
    #: of the same feed an update rather than a duplicate.
    external_uid: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: Which subscription put it here (mig 176). Two feeds may legitimately
    #: carry the same UID — a meeting a person and their team are both invited
    #: to — so the UID alone cannot identify a row.
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    created_by:  Mapped[str]       = mapped_column(String(255), nullable=False)
    created_at:  Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)
    updated_at:  Mapped[datetime]  = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class CalendarSubscriptionModel(Base):
    """A calendar somewhere else, re-read on a timetable (mig 176).

    **Read-only, deliberately** — which is exactly what Google and Outlook offer
    when you add a calendar by URL, and why that feature is cheap: with no
    write-back there is no question of who wins when both sides change.

    `url` is not a secret but it is usually a **capability**: a Google or
    Outlook private address embeds a token, so whoever holds the link can read
    the calendar. It is shown back only to the owner, their manager and an admin.
    """

    __tablename__ = "calendar_subscriptions"
    __table_args__ = (
        Index("ix_calendar_subscriptions_user", "tenant_id", "user_id"),
        Index("ix_calendar_subscriptions_due", "tenant_id", "enabled", "last_fetched_at"),
    )

    id:        Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[str]       = mapped_column(String(255), nullable=False)
    user_id:   Mapped[str]       = mapped_column(String(255), nullable=False)
    name:      Mapped[str]       = mapped_column(String(255), nullable=False)
    url:       Mapped[str]       = mapped_column(Text, nullable=False)
    enabled:   Mapped[bool]      = mapped_column(Boolean, nullable=False, default=True)
    interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    #: The polite half of polling: an unchanged feed answers 304 and costs both
    #: sides nothing, which is what makes a short interval reasonable.
    etag:          Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_modified: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: ok | unchanged | error | never. A sync that silently stops is worse than
    #: one that never started — the calendar still looks full.
    last_status: Mapped[str]  = mapped_column(String(20), nullable=False, default="never")
    last_error:  Mapped[str]  = mapped_column(Text, nullable=False, default="")
    last_event_count:   Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    consecutive_errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by: Mapped[str]      = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)



# <<< P70 Calendar


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
        MicroDateTime(), default=_utcnow, nullable=False, index=True,
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
        Index("idx_audit_anchors_chain_time", "chain", "anchored_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    #: Which chain this receipt covers — `audit` (the case audit chain) or
    #: `arbiter` (the policy decision chain). Defaults to `audit` because every
    #: row that existed before mig 180 is one, and describing history as
    #: anything else would be a lie in the evidence table.
    chain: Mapped[str] = mapped_column(String(32), nullable=False, default="audit")
    tip_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    tip_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    tsa_url: Mapped[str] = mapped_column(String(512), nullable=False)
    tsr_der: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    anchored_at: Mapped[datetime] = mapped_column(
        MicroDateTime(), default=_utcnow, nullable=False, index=True,
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
        MicroDateTime(), default=_utcnow, nullable=False,
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
    created_at: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
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
    created_at: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
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
    created_at: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
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
    created_at: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
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

    #: Owning tenant (8e, migration 213). Existing rows are the default tenant's.

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(

        GUID(), ForeignKey("tenants.id"), nullable=True, index=True)
    device_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)        # delivered | failed
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
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
    created_at: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)


class DocumentIndexJobModel(Base):
    """A document waiting to be indexed, off the request (mig 203).

    **Why the queue exists.** `index_document` is awaited inside the request and
    the case-wide route loops every document, so with OCR on, one slow scan is
    multiplied by the number of scans rather than paid once.

    **The synchronous path is still the default.** A row appears here only when
    a caller asks for `background: true`, and it is drained only by a
    `document_index` schedule an operator created. An install that changes
    nothing behaves exactly as it did.

    **`active_key` is the portable partial index.** It holds the document id
    while the job is pending or running and is NULL once it is not, so the
    unique index over it constrains exactly the active rows on all three
    engines — MySQL has no partial index, and NULL <> NULL everywhere.
    """

    __tablename__ = "document_index_jobs"
    __table_args__ = (
        Index("ux_document_index_jobs_active", "active_key", unique=True),
        Index("idx_document_index_jobs_status", "status", "created_at"),
        Index("idx_document_index_jobs_case", "case_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    #: The CASE's tenant, captured at enqueue: the runner has no caller to ask,
    #: so the boundary travels on the row.
    tenant_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    case_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    document_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    active_key: Mapped[str | None] = mapped_column(String(36), nullable=True)

    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: A document that cannot be read does not become readable by being retried,
    #: and every attempt costs an OCR timeout.
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    reindex: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    chunks_indexed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Who asked. The runner acts on their behalf, so "why was this document
    #: read" has an answer on the row that caused it.
    requested_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

    #: Mig 206 — a background UPLOAD has no document row to read back, so its
    #: bytes wait under this storage key until the drain indexes them, and are
    #: deleted once the job ends. NULL for every job queued from stored documents.
    staged_storage_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    staged_filename: Mapped[str | None] = mapped_column(String(512), nullable=True)
    staged_content_type: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
    started_at: Mapped[datetime | None] = mapped_column(MicroDateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(MicroDateTime(), nullable=True)


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
    created_at: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
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
    created_at: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
# <<< P30 HxNexus AI Copilot


# >>> P37 Operator & Access Group Model

class PortalModel(Base):
    """Named portal configuration — each access group has exactly one portal.

    portal_type controls which UI shell is rendered for operators in that group:
      staff    → full Velaris Studio
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
    created_at: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
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
    created_at: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
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
    created_at: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
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
    assigned_at: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)

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
        MicroDateTime(), default=_utcnow, nullable=False,
    )
    session_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

# <<< P46 HxStream


# >>> P41 HxGraph — Velaris Native Knowledge Graph

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
    created_at:     Mapped[datetime]       = mapped_column(MicroDateTime(), default=_utcnow)

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
    created_at:   Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)

    from_node: Mapped["GraphNodeModel"] = relationship(
        "GraphNodeModel", foreign_keys=[from_node_id], back_populates="outgoing_edges",
    )
    to_node: Mapped["GraphNodeModel"] = relationship(
        "GraphNodeModel", foreign_keys=[to_node_id], back_populates="incoming_edges",
    )

# <<< P41 HxGraph


# >>> P42 HxNexus Polyglot Intelligence

class BpmConceptModel(Base):
    """One BPM concept → Velaris mapping in the polyglot knowledge base."""

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
    created_at:      Mapped[datetime]      = mapped_column(MicroDateTime(), default=_utcnow)


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
    """A versioned snapshot of the full Velaris platform state."""

    __tablename__ = "app_packages"
    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_app_packages_name_version"),
        Index("ix_app_packages_status",  "status"),
        Index("ix_app_packages_created", "created_at"),
    )

    id:          Mapped[uuid.UUID]     = mapped_column(GUID(), primary_key=True, default=_new_uuid)

    #: Owning tenant (8e, migration 213). Existing rows are the default tenant's.

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(

        GUID(), ForeignKey("tenants.id"), nullable=True, index=True)
    name:        Mapped[str]           = mapped_column(String(255), nullable=False)
    version:     Mapped[str]           = mapped_column(String(50),  nullable=False)
    description: Mapped[str | None]    = mapped_column(Text, nullable=True)
    bundle:      Mapped[dict]          = mapped_column(PortableJSON(), default=dict)
    manifest:    Mapped[dict]          = mapped_column(PortableJSON(), default=dict)
    status:      Mapped[str]           = mapped_column(String(20), nullable=False, default="draft")
    created_by:  Mapped[str | None]    = mapped_column(String(255), nullable=True)
    created_at:  Mapped[datetime]      = mapped_column(MicroDateTime(), default=_utcnow)

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
    deployed_at:      Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)
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

    #: Owning tenant (8e, migration 213). Existing rows are the default tenant's.

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(

        GUID(), ForeignKey("tenants.id"), nullable=True, index=True)
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
    created_at:   Mapped[datetime]      = mapped_column(MicroDateTime(), default=_utcnow)
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
    created_at:            Mapped[datetime]      = mapped_column(MicroDateTime(), default=_utcnow)
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
    created_at:   Mapped[datetime]      = mapped_column(MicroDateTime(), default=_utcnow)
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
    created_at:    Mapped[datetime]      = mapped_column(MicroDateTime(), default=_utcnow)
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
    created_at:   Mapped[datetime]       = mapped_column(MicroDateTime(), default=_utcnow)
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
    received_at:  Mapped[datetime]     = mapped_column(MicroDateTime(), default=_utcnow)

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
    created_at:               Mapped[datetime]      = mapped_column(MicroDateTime(), default=_utcnow)

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
    created_at:       Mapped[datetime]     = mapped_column(MicroDateTime(), default=_utcnow)
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
    created_at:    Mapped[datetime]     = mapped_column(MicroDateTime(), default=_utcnow)
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
    created_at:     Mapped[datetime]     = mapped_column(MicroDateTime(), default=_utcnow)
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
    created_at:     Mapped[datetime]     = mapped_column(MicroDateTime(), default=_utcnow)
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
    created_at:  Mapped[datetime]   = mapped_column(MicroDateTime(), default=_utcnow)
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
    created_at:    Mapped[datetime]      = mapped_column(MicroDateTime(), default=_utcnow)

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
    # HxConsent gate (mig 154 / mysql 0027). Off by default: turning it on
    # changes what an existing destination receives, so it is a deliberate act.
    consent_gated:      Mapped[bool]       = mapped_column(Boolean, nullable=False, default=False)
    tenant_id:          Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by:         Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_synced_at:     Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_sync_status:   Mapped[str | None] = mapped_column(String(20), default="never")
    created_at:         Mapped[datetime]   = mapped_column(MicroDateTime(), default=_utcnow)
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
    created_at:     Mapped[datetime]       = mapped_column(MicroDateTime(), default=_utcnow)

    destination: Mapped["SyncDestinationModel"] = relationship(back_populates="runs")


class SyncFieldMappingModel(Base):
    """Maps a Velaris case field to a DWH column with optional transform."""

    __tablename__ = "sync_field_mappings"

    id:             Mapped[uuid.UUID]  = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    destination_id: Mapped[uuid.UUID]  = mapped_column(GUID(), ForeignKey("sync_destinations.id", ondelete="CASCADE"), nullable=False)
    case_type_id:   Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_field:   Mapped[str]        = mapped_column(String(255), nullable=False)
    dest_column:    Mapped[str]        = mapped_column(String(255), nullable=False)
    transform:      Mapped[str]        = mapped_column(String(30),  nullable=False, default="passthrough")
    # transform="wasm" → the installed package whose module runs (#17, mig 141).
    transform_module_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pii:            Mapped[bool]       = mapped_column(Boolean, default=False)
    created_at:     Mapped[datetime]   = mapped_column(MicroDateTime(), default=_utcnow)

    destination: Mapped["SyncDestinationModel"] = relationship(back_populates="mappings")


class SyncRedactionRuleModel(Base):
    """PII redaction rule: hash/drop/mask a field before it leaves Velaris."""

    __tablename__ = "sync_redaction_rules"

    id:             Mapped[uuid.UUID]  = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    destination_id: Mapped[uuid.UUID]  = mapped_column(GUID(), ForeignKey("sync_destinations.id", ondelete="CASCADE"), nullable=False)
    case_type_id:   Mapped[str | None] = mapped_column(String(255), nullable=True)
    field_path:     Mapped[str]        = mapped_column(String(255), nullable=False)
    action:         Mapped[str]        = mapped_column(String(10),  nullable=False, default="hash")
    reason:         Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at:     Mapped[datetime]   = mapped_column(MicroDateTime(), default=_utcnow)

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
    created_at:        Mapped[datetime]   = mapped_column(MicroDateTime(), default=_utcnow)
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
    created_at:   Mapped[datetime]   = mapped_column(MicroDateTime(), default=_utcnow)

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
    created_at:      Mapped[datetime]       = mapped_column(MicroDateTime(), default=_utcnow)

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
    recorded_at:        Mapped[datetime]   = mapped_column(MicroDateTime(), default=_utcnow)

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
    recorded_at: Mapped[datetime]   = mapped_column(MicroDateTime(), default=_utcnow)

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
    created_at:     Mapped[datetime]   = mapped_column(MicroDateTime(), default=_utcnow)
    updated_at:     Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    incidents: Mapped[list["SecurityIncidentModel"]] = relationship(back_populates="rule")


class SecurityIncidentModel(Base):
    """A detected fraud/abuse event — a first-class Velaris incident record."""

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
    recorded_at:      Mapped[datetime]   = mapped_column(MicroDateTime(), default=_utcnow)

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
    created_at:   Mapped[datetime]   = mapped_column(MicroDateTime(), default=_utcnow)
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
    started_at:    Mapped[datetime]       = mapped_column(MicroDateTime(), default=_utcnow)
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
    created_at:   Mapped[datetime]       = mapped_column(MicroDateTime(), default=_utcnow)
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
    started_at:    Mapped[datetime]       = mapped_column(MicroDateTime(), default=_utcnow)
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
    created_at:   Mapped[datetime]       = mapped_column(MicroDateTime(), default=_utcnow)
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
    created_at:   Mapped[datetime]       = mapped_column(MicroDateTime(), default=_utcnow)
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
    created_at:       Mapped[datetime]       = mapped_column(MicroDateTime(), default=_utcnow)
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
    created_at:    Mapped[datetime]       = mapped_column(MicroDateTime(), default=_utcnow)
    uploaded_at:   Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# <<< P53 HxConnect: Developer & Custom Connectors

class WebhookReceiverRuleModel(Base):
    """Routing rule: map inbound webhook payload to a Velaris case."""

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
    created_at:         Mapped[datetime]       = mapped_column(MicroDateTime(), default=_utcnow)


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
    received_at:     Mapped[datetime]       = mapped_column(MicroDateTime(), default=_utcnow)
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
    received_at:      Mapped[datetime]         = mapped_column(MicroDateTime(), default=_utcnow)
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
    created_at:      Mapped[datetime]       = mapped_column(MicroDateTime(), default=_utcnow)
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
    started_at:  Mapped[datetime | None] = mapped_column(MicroDateTime(), nullable=True)
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
    created_at:             Mapped[datetime]         = mapped_column(MicroDateTime(), default=_utcnow)


class DeploymentPackageModel(Base):
    """A frozen promotion package — the RAP of this platform.

    Built once from an explicit selection, **sealed**, and it is those bytes that
    promote. Before this existed the push called `build_bundle()` with no
    selection and rebuilt the whole environment at execute time, so the Promote
    screen's artifact picker was cosmetic and the approver approved a risk
    summary rather than a payload.

    A sealed package is immutable — a change is a new version, which is why the
    unique key carries one.
    """

    __tablename__ = "deployment_packages"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", "version", name="deployment_packages_name_uq"),
        Index("ix_deployment_packages_tenant", "tenant_id", "status"),
        Index("ix_deployment_packages_created", "created_at"),
    )

    id:          Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:   Mapped[str]       = mapped_column(String(255), nullable=False)
    name:        Mapped[str]       = mapped_column(String(255), nullable=False)
    version:     Mapped[str]       = mapped_column(String(50), nullable=False)
    description: Mapped[str]       = mapped_column(Text, nullable=False, default="")
    #: What was asked for, kept beside what it resolved to — a package that turns
    #: out to hold the wrong thing has to be explainable after the fact.
    selection:   Mapped[dict]      = mapped_column(PortableJSON(), nullable=False, default=dict)
    #: The resolved bundle, frozen at seal time and never rebuilt.
    bundle:      Mapped[dict]      = mapped_column(PortableJSON(), nullable=False, default=dict)
    #: Section counts, so the list view never loads the bundle.
    summary:     Mapped[dict]      = mapped_column(PortableJSON(), nullable=False, default=dict)
    #: SHA-256 over the canonical JSON of `bundle`. NULL until sealed.
    checksum:    Mapped[str | None] = mapped_column(String(64), nullable=True)
    bundle_schema_version: Mapped[str] = mapped_column(String(10), nullable=False, default="2")
    #: draft | sealed
    status:      Mapped[str]       = mapped_column(String(20), nullable=False, default="draft")
    source_env_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("environment_registry.id", ondelete="SET NULL"), nullable=True)
    created_by:  Mapped[str]       = mapped_column(String(255), nullable=False)
    created_at:  Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)
    updated_at:  Mapped[datetime]  = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
    sealed_by:   Mapped[str | None] = mapped_column(String(255), nullable=True)
    sealed_at:   Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


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
    package_id:       Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("deployment_packages.id", ondelete="SET NULL"), nullable=True)
    #: The checksum this run was approved against, recorded beside the package
    #: rather than only inside it: if the two disagree, the package was replaced
    #: after approval, and the run row can say so on its own.
    package_checksum: Mapped[str | None]     = mapped_column(String(64), nullable=True)
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
    created_at:       Mapped[datetime]       = mapped_column(MicroDateTime(), default=_utcnow)
    completed_at:     Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # ── F-04 simulation gate (mig 186) ──────────────────────────────────────
    # All four nullable, and null on every run written before the gate existed —
    # which is what those runs are: never gated.
    simulation_id:        Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    #: An override is a signed, reasoned, audited act. The reason is mandatory
    #: at the call site, not by a NOT NULL here, because a run that was never
    #: overridden has no reason to give.
    gate_override_reason: Mapped[str | None]       = mapped_column(Text, nullable=True)
    gate_overridden_by:   Mapped[str | None]       = mapped_column(String(255), nullable=True)
    gate_overridden_at:   Mapped[datetime | None]  = mapped_column(DateTime(timezone=True), nullable=True)


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
    """A Kanban/Sprint board backed by a Velaris case type."""

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
    created_at:    Mapped[datetime]       = mapped_column(MicroDateTime(), default=_utcnow)

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
    created_at:   Mapped[datetime]       = mapped_column(MicroDateTime(), default=_utcnow)
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
    created_at:    Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)

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

    #: Owning tenant (8e, migration 213). Existing rows are the default tenant's.

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(

        GUID(), ForeignKey("tenants.id"), nullable=True, index=True)
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
    created_at:          Mapped[datetime]       = mapped_column(MicroDateTime(), default=_utcnow)
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
    created_at:  Mapped[datetime]       = mapped_column(MicroDateTime(), default=_utcnow)
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
    created_at: Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)
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
    created_at:  Mapped[datetime]   = mapped_column(MicroDateTime(), default=_utcnow)

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
    created_at:      Mapped[datetime]   = mapped_column(MicroDateTime(), default=_utcnow)
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
    """A named branch of a Velaris artifact (case type, form, etc.) or full app package,
    pulled from a remote environment for review and merge into dev main."""

    __tablename__ = "artifact_branches"
    __table_args__ = (
        Index("ix_artifact_branches_status",     "status"),
        Index("ix_artifact_branches_source_env", "source_env_id"),
        Index("ix_artifact_branches_type",       "branch_type", "artifact_type"),
        Index("ix_artifact_branches_created",    "created_at"),
    )

    id:                    Mapped[uuid.UUID]      = mapped_column(GUID(), primary_key=True, default=_new_uuid)

    #: Owning tenant (8e, migration 213). Existing rows are the default tenant's.

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(

        GUID(), ForeignKey("tenants.id"), nullable=True, index=True)
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
    created_at:            Mapped[datetime]       = mapped_column(MicroDateTime(), default=_utcnow)
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
    created_at:  Mapped[datetime]   = mapped_column(MicroDateTime(), default=_utcnow)

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
    created_at:     Mapped[datetime]   = mapped_column(MicroDateTime(), default=_utcnow)

# <<< P60 HxBranch


# ═══════════════════════════════════════════════════════════════════
# P64 — Real Authentication (ENH-10)
# ═══════════════════════════════════════════════════════════════════

class HelixUserModel(Base):
    """A real authenticated user with bcrypt password, lockout, and MFA support."""

    __tablename__ = "velaris_users"
    __table_args__ = (
        Index("ix_velaris_users_email",    "email",    unique=True),
        Index("ix_velaris_users_username", "username", unique=True),
        Index("ix_velaris_users_sso",      "sso_provider", "sso_subject"),
        Index("ix_velaris_users_external", "external_id"),
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
    # The stable key an external directory asserts (SCIM externalId, SAML
    # NameID). Deliberately NOT username: that changes on rename, and an
    # identity keyed on it forks one person into two accounts. Not unique —
    # two providers may assert the same opaque id (mig 157).
    external_id:              Mapped[str | None]     = mapped_column(String(255), nullable=True)
    created_at:               Mapped[datetime]       = mapped_column(MicroDateTime(), default=_utcnow)
    updated_at:               Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)
    last_login_at:            Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # A deleted account (mig 223): refused everywhere at once, kept — username
    # reserved — until purge_after, then purged (velaris_shared.identity.lifecycle).
    # NULL = not deleted.
    deleted_at:               Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by:               Mapped[str | None]     = mapped_column(String(255), nullable=True)
    purge_after:              Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    deleted_meta:             Mapped[str | None]     = mapped_column(Text, nullable=True)
    # Tokens issued before this are refused — "sign out everywhere" (a delete
    # sets it, so an undo does not revive the sessions it ended). NULL = none.
    tokens_valid_after:       Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    otps: Mapped[list["AuthOtpModel"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class DeletionLedgerModel(Base):
    """The deletion ledger's outbox (mig 224): a purged account's signed record,
    queued in the purge's own transaction, then written to .deletions/ — the
    ledger itself lives outside the database (velaris_shared.identity.ledger)."""

    __tablename__ = "deletion_ledger"

    id:         Mapped[uuid.UUID]       = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    account_id: Mapped[uuid.UUID]       = mapped_column(GUID(), nullable=False, unique=True)
    record:     Mapped[str]             = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime]        = mapped_column(MicroDateTime(), default=_utcnow)
    written_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)


class AuthOtpModel(Base):
    """Short-lived single-use OTP for password reset and MFA enrolment."""

    __tablename__ = "auth_otp"
    __table_args__ = (
        Index("ix_auth_otp_user", "user_id"),
    )

    id:         Mapped[uuid.UUID]      = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    user_id:    Mapped[uuid.UUID]      = mapped_column(GUID(), ForeignKey("velaris_users.id", ondelete="CASCADE"), nullable=False)
    otp_hash:   Mapped[str]            = mapped_column(Text, nullable=False)
    purpose:    Mapped[str]            = mapped_column(String(30), nullable=False)
    expires_at: Mapped[datetime]       = mapped_column(DateTime(timezone=True), nullable=False)
    used_at:    Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime]       = mapped_column(MicroDateTime(), default=_utcnow)

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
    created_at:        Mapped[datetime]    = mapped_column(MicroDateTime(), default=_utcnow)

    # SAML SP key material (mig 162). Columns rather than keys inside `config`,
    # for the same reason as `client_secret_enc`: `config` is returned wholesale
    # to the admin console, and key material must never be in a response body.
    # The private half is sealed with the tenant's HxVault DEK; the certificate
    # is public by design — it is published in SP metadata for the IdP to import.
    sp_key_enc:        Mapped[dict | None] = mapped_column(PortableJSON(), nullable=True)
    sp_cert:           Mapped[str | None]  = mapped_column(Text, nullable=True)
    sp_key_created_at: Mapped[datetime | None] = mapped_column(MicroDateTime(), nullable=True)

# <<< P64 Real Auth


# >>> Enterprise identity P0 — provisioning spine (mig 157)

class IdentityGroupMappingModel(Base):
    """An IdP group name → a Velaris access group.

    An unmapped group grants NOTHING. There is deliberately no fallback that
    matches an access group by name: the IdP administrator controls those
    strings, so a name fallback would let them mint Velaris authorization by
    naming a directory group after one of ours.

    ``tenant_id`` holds the RESOLVED tenant UUID, never the caller's token.
    The platform stores a slug in some tenant columns and a UUID in others
    (``tenancy.identity.resolve_tenant``); keying this table on the ambiguous
    form would make a mapping resolve differently depending on how it was
    written.
    """

    __tablename__ = "identity_group_mappings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source", "external_group",
                         name="identity_group_mappings_uq"),
        Index("ix_igm_tenant_source", "tenant_id", "source"),
    )

    id:              Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:       Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    #: Which entry point may use this mapping — saml | scim | oidc | bulk_sync.
    source:          Mapped[str]       = mapped_column(String(20), nullable=False)
    external_group:  Mapped[str]       = mapped_column(String(255), nullable=False)
    access_group_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("access_groups.id", ondelete="CASCADE"), nullable=False,
    )
    priority:        Mapped[int]       = mapped_column(Integer, nullable=False, default=100)
    created_by:      Mapped[str]       = mapped_column(String(255), nullable=False, default="system")
    created_at:      Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)


class IdentityProvisionLogModel(Base):
    """One row per provisioning decision, from every entry point.

    Exists to answer "why is this user in this group?" and, just as important,
    "the IdP sent twelve groups and three of them map to nothing" — an unmapped
    group is recorded here rather than silently dropped.
    """

    __tablename__ = "identity_provision_log"
    __table_args__ = (
        Index("ix_ipl_tenant_time", "tenant_id", "created_at"),
        Index("ix_ipl_external", "external_id"),
    )

    id:          Mapped[uuid.UUID]  = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:   Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: scim | saml_jit | oidc | bulk_sync
    source:      Mapped[str]        = mapped_column(String(20), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    username:    Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: create | update | deactivate | group_add | group_remove | rejected
    action:      Mapped[str]        = mapped_column(String(30), nullable=False)
    #: ok | error. A refusal is action=rejected, result=error, reason in detail.
    result:      Mapped[str]        = mapped_column(String(20), nullable=False)
    detail:      Mapped[dict]       = mapped_column(PortableJSON(), nullable=False, default=dict)
    created_at:  Mapped[datetime]   = mapped_column(MicroDateTime(), default=_utcnow)

class SamlAssertionReplayModel(Base):
    """One row per consumed SAML assertion ID (mig 158).

    **The primary key is the replay defence.** Every other control on an
    assertion is a time window five or so minutes wide, inside which the same
    signed bytes log in as often as they are posted. The first POST takes this
    row; a second collides on the unique insert and is refused. A
    SELECT-then-INSERT would race in exactly the case that matters.

    Rows past ``not_on_or_after`` are inert — the assertion fails its own time
    window by then — so the retention runner may delete them freely.
    """

    __tablename__ = "saml_assertion_replay"
    __table_args__ = (
        Index("ix_saml_replay_expiry", "not_on_or_after"),
    )

    assertion_id:    Mapped[str]       = mapped_column(String(255), primary_key=True)
    provider_id:     Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    not_on_or_after: Mapped[datetime]  = mapped_column(DateTime(timezone=True), nullable=False)
    seen_at:         Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)


class SamlAuthnRequestModel(Base):
    """One row per outstanding SP-initiated AuthnRequest (mig 163).

    Recorded when `/login` builds the request and **deleted** when an assertion
    quotes it, so `InResponseTo` is a check rather than a field we merely read.
    A second assertion quoting the same request finds nothing — consumption is
    the DELETE, not a SELECT that could race with it.

    Absent `InResponseTo` is fine: an IdP-initiated sign-in answers no request.
    """

    __tablename__ = "saml_authn_requests"
    __table_args__ = (
        Index("ix_saml_authn_req_expiry", "expires_at"),
    )

    request_id:  Mapped[str]       = mapped_column(String(255), primary_key=True)
    provider_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    expires_at:  Mapped[datetime]  = mapped_column(DateTime(timezone=True), nullable=False)
    created_at:  Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)


class ScimTokenModel(Base):
    """A machine credential for an IdP's SCIM client (mig 159).

    Stored as SHA-256 only: the plaintext is shown once at mint, so a database
    read yields nothing that works. **The tenant comes from this row and never
    from the request** — a client that could name its own tenant could
    enumerate and rewrite every other one.
    """

    __tablename__ = "scim_tokens"
    __table_args__ = (
        Index("ix_scim_tokens_hash", "token_hash", unique=True),
        Index("ix_scim_tokens_tenant", "tenant_id"),
    )

    id:           Mapped[uuid.UUID]      = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:    Mapped[str | None]     = mapped_column(String(255), nullable=True)
    name:         Mapped[str]            = mapped_column(String(255), nullable=False)
    token_hash:   Mapped[str]            = mapped_column(String(64), nullable=False)
    scopes:       Mapped[list]           = mapped_column(PortableJSON(), nullable=False, default=list)
    created_by:   Mapped[str]            = mapped_column(String(255), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at:   Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at:   Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at:   Mapped[datetime]       = mapped_column(MicroDateTime(), default=_utcnow)

# <<< Enterprise identity P0


# >>> Omnichannel inbound (mig 160)
#
# Modelled on `email_messages`, which already proves the bidirectional,
# nullable-case, threaded shape works. `sms_messages` is deliberately NOT
# widened: it is step-bound (`case_id` and `step_id` both NOT NULL) because it
# records "the workflow sent this", and an inbound message has neither.

class ChannelEndpointModel(Base):
    """The addressable thing a customer contacts — the "email account" equivalent."""

    __tablename__ = "channel_endpoints"
    __table_args__ = (
        UniqueConstraint("provider", "channel", "address",
                         name="channel_endpoints_addr_uq"),
        Index("ix_chep_tenant", "tenant_id", "channel"),
    )

    id:                     Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:              Mapped[str]       = mapped_column(String(255), nullable=False)
    #: sms | whatsapp | voice
    channel:                Mapped[str]       = mapped_column(String(20), nullable=False)
    #: twilio | meta | generic
    provider:               Mapped[str]       = mapped_column(String(30), nullable=False)
    address:                Mapped[str]       = mapped_column(String(64), nullable=False)
    connector_id:           Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    #: The webhook signing secret. The inbound route authenticates on this and
    #: nothing else — there is no token, and the caller is Twilio or Meta.
    signing_secret_enc:     Mapped[dict | None] = mapped_column(PortableJSON(), nullable=True)
    default_case_type_id:   Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    auto_create_case:       Mapped[bool]      = mapped_column(Boolean, nullable=False, default=True)
    conversation_ttl_hours: Mapped[int]       = mapped_column(Integer, nullable=False, default=72)
    routing:                Mapped[dict]      = mapped_column(PortableJSON(), nullable=False, default=dict)
    is_active:              Mapped[bool]      = mapped_column(Boolean, nullable=False, default=True)
    created_at:             Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)
    updated_at:             Mapped[datetime]  = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class ChannelConversationModel(Base):
    """One thread with one peer on one endpoint."""

    __tablename__ = "channel_conversations"
    __table_args__ = (
        Index("ix_chconv_lookup", "endpoint_id", "peer_address", "status"),
        Index("ix_chconv_case", "case_id"),
        Index("ix_chconv_tenant", "tenant_id", "last_message_at"),
    )

    id:                Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:         Mapped[str]       = mapped_column(String(255), nullable=False)
    endpoint_id:       Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("channel_endpoints.id", ondelete="CASCADE"), nullable=False)
    peer_address:      Mapped[str]       = mapped_column(String(128), nullable=False)
    case_id:           Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    customer_id:       Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    #: open | assigned | closed
    status:            Mapped[str]       = mapped_column(String(20), nullable=False, default="open")
    assigned_to:       Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: WhatsApp's 24-hour session window. Outside it, only a pre-approved
    #: template may be sent — unmodelled, replies fail silently at the provider.
    window_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_message_at:   Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    unread_count:      Mapped[int]       = mapped_column(Integer, nullable=False, default=0)
    metadata_json:     Mapped[dict]      = mapped_column(PortableJSON(), nullable=False, default=dict)
    created_at:        Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)


class ChannelMessageModel(Base):
    """One message, either direction, on a channel conversation.

    ``provider_message_id`` is UNIQUE and that **is** the idempotency guarantee:
    Twilio and Meta both retry deliveries, and without it a retry creates a
    second case. It is the role `message_id` plays for email.
    """

    __tablename__ = "channel_messages"
    __table_args__ = (
        Index("ix_chmsg_provider_id", "provider_message_id", unique=True),
        Index("ix_chmsg_conv", "conversation_id", "created_at"),
        Index("ix_chmsg_case", "case_id"),
        Index("ix_chmsg_unread", "tenant_id", "is_read"),
    )

    id:                  Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:           Mapped[str]       = mapped_column(String(255), nullable=False)
    conversation_id:     Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("channel_conversations.id", ondelete="CASCADE"), nullable=False)
    case_id:             Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    channel:             Mapped[str]       = mapped_column(String(20), nullable=False)
    #: inbound | outbound
    direction:           Mapped[str]       = mapped_column(String(10), nullable=False)
    author:              Mapped[str | None] = mapped_column(String(255), nullable=True)
    peer_address:        Mapped[str]       = mapped_column(String(128), nullable=False)
    body:                Mapped[str]       = mapped_column(Text, nullable=False, default="")
    media:               Mapped[list]      = mapped_column(PortableJSON(), nullable=False, default=list)
    provider_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    template_name:       Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: received | queued | sent | delivered | read | failed
    status:              Mapped[str]       = mapped_column(String(30), nullable=False, default="received")
    error:               Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_payload:         Mapped[dict]      = mapped_column(PortableJSON(), nullable=False, default=dict)
    is_read:             Mapped[bool]      = mapped_column(Boolean, nullable=False, default=False)
    created_at:          Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)
    sent_at:             Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_at:         Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WhatsAppTemplateModel(Base):
    """A pre-approved WhatsApp template — the only thing sendable outside the 24h window."""

    __tablename__ = "whatsapp_templates"
    __table_args__ = (
        UniqueConstraint("endpoint_id", "name", "language", name="whatsapp_templates_uq"),
    )

    id:                   Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:            Mapped[str]       = mapped_column(String(255), nullable=False)
    endpoint_id:          Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("channel_endpoints.id", ondelete="CASCADE"), nullable=False)
    name:                 Mapped[str]       = mapped_column(String(255), nullable=False)
    language:             Mapped[str]       = mapped_column(String(10), nullable=False)
    provider_template_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    body_params:          Mapped[list]      = mapped_column(PortableJSON(), nullable=False, default=list)
    approval_status:      Mapped[str]       = mapped_column(String(20), nullable=False, default="pending")
    created_at:           Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)

# <<< Omnichannel inbound


# >>> RPA / bot runner (mig 161)
#
# A bot is a container that performs a task and returns a result. It runs on the
# Marketplace Layer-2 substrate unchanged — digest-pinned, registry-allowlisted,
# internal network with no route out, non-root, read-only rootfs, zero database
# credentials — because an RPA bot is a different kind of tenant of that
# substrate, not a different substrate.

class BotDefinitionModel(Base):
    """What a bot *is*. Deliberately close to an L2 app manifest, because the
    review a platform admin performs is the same review."""

    __tablename__ = "bot_definitions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="bot_definitions_name_uq"),
    )

    id:                 Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:          Mapped[str]       = mapped_column(String(255), nullable=False)
    name:               Mapped[str]       = mapped_column(String(255), nullable=False)
    description:        Mapped[str]       = mapped_column(Text, nullable=False, default="")
    image:              Mapped[str]       = mapped_column(String(512), nullable=False)
    #: Pinned. The tag is display-only and is never what gets pulled.
    digest:             Mapped[str]       = mapped_column(String(80), nullable=False)
    signature_verified: Mapped[bool]      = mapped_column(Boolean, nullable=False, default=False)
    declared_command:   Mapped[list]      = mapped_column(PortableJSON(), nullable=False, default=list)
    #: NON-SECRET ONLY. A password here would be static, identical for every job,
    #: and readable by anyone who can read this table.
    declared_env:       Mapped[dict]      = mapped_column(PortableJSON(), nullable=False, default=dict)
    credential_refs:    Mapped[list]      = mapped_column(PortableJSON(), nullable=False, default=list)
    input_schema:       Mapped[dict]      = mapped_column(PortableJSON(), nullable=False, default=dict)
    output_schema:      Mapped[dict]      = mapped_column(PortableJSON(), nullable=False, default=dict)
    granted_domains:    Mapped[list]      = mapped_column(PortableJSON(), nullable=False, default=list)
    timeout_seconds:    Mapped[int]       = mapped_column(Integer, nullable=False, default=300)
    max_concurrency:    Mapped[int]       = mapped_column(Integer, nullable=False, default=1)
    #: 'light' keeps the L2 pid cap; 'browser' raises it — a Chromium process
    #: tree exceeds 64 immediately.
    resource_profile:   Mapped[str]       = mapped_column(String(20), nullable=False, default="light")
    #: NULL = no fan-out. Names the key in a run's input holding the collection
    #: to spread over. It lives on the DEFINITION, not on the run request, so the
    #: approver reviewing this bot can see that one call may start `fanout_max`
    #: containers.
    fanout_input_key:   Mapped[str | None] = mapped_column(String(255), nullable=True)
    fanout_max:         Mapped[int]       = mapped_column(Integer, nullable=False, default=25)
    #: draft until an explicit approval by an admin who is not the creator.
    status:             Mapped[str]       = mapped_column(String(20), nullable=False, default="draft")
    created_by:         Mapped[str]       = mapped_column(String(255), nullable=False)
    #: Who last changed the definition. Approval refuses the author AND this
    #: person: an edit never changed `created_by`, so a reviewer could approve a
    #: bot, repoint it at another image, and approve their own change.
    last_edited_by:     Mapped[str | None] = mapped_column(String(255), nullable=True)
    approved_by:        Mapped[str | None] = mapped_column(String(255), nullable=True)
    approved_at:        Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at:         Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)
    updated_at:         Mapped[datetime]  = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class BotJobModel(Base):
    """One execution.

    ``result_token_hash`` + ``result_token_used_at`` are how a container returns
    its work **without** the broker gaining write scopes: a per-job, single-use
    credential over its own row and nothing else.
    """

    __tablename__ = "bot_jobs"
    __table_args__ = (
        Index("ix_botjobs_queue", "status", "queued_at"),
        Index("ix_botjobs_case", "case_id"),
        Index("ix_botjobs_bot", "bot_id", "status"),
        Index("ix_botjobs_tenant", "tenant_id", "queued_at"),
        Index("ix_botjobs_token", "result_token_hash"),
        Index("ix_botjobs_batch", "batch_id"),
        Index("ix_botjobs_runafter", "status", "run_after"),
    )

    id:                   Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:            Mapped[str]       = mapped_column(String(255), nullable=False)
    bot_id:               Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("bot_definitions.id", ondelete="CASCADE"), nullable=False)
    case_id:              Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    step_id:              Mapped[str | None] = mapped_column(String(255), nullable=True)
    process_instance_id:  Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    node_id:              Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: workflow | agent | api | schedule
    trigger:              Mapped[str]       = mapped_column(String(20), nullable=False, default="api")
    requested_by:         Mapped[str | None] = mapped_column(String(255), nullable=True)
    input:                Mapped[dict]      = mapped_column(PortableJSON(), nullable=False, default=dict)
    output:               Mapped[dict | None] = mapped_column(PortableJSON(), nullable=True)
    artifacts:            Mapped[list]      = mapped_column(PortableJSON(), nullable=False, default=list)
    #: queued | leased | running | succeeded | failed | timed_out | cancelled
    status:               Mapped[str]       = mapped_column(String(20), nullable=False, default="queued")
    attempt:              Mapped[int]       = mapped_column(Integer, nullable=False, default=0)
    #: A crashed runner's lease expires and the job requeues — this is what makes
    #: two runners safe without a distributed lock.
    lease_expires_at:     Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    container_id:         Mapped[str | None] = mapped_column(String(80), nullable=True)
    result_token_hash:    Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_token_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error:                Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Not before this instant. What makes the retry backoff real — it used to be
    #: written into a run event only, where nothing could read it, so a failed job
    #: came back on the next five-second cycle.
    run_after:            Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: A fan-out run is N ordinary jobs sharing a batch id — one queue, one lease
    #: path, one set of limits. The batch reports them together; it is not a
    #: second execution mechanism.
    batch_id:             Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    batch_index:          Mapped[int | None] = mapped_column(Integer, nullable=True)
    batch_size:           Mapped[int | None] = mapped_column(Integer, nullable=True)
    queued_at:            Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)
    started_at:           Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at:             Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BotRunEventModel(Base):
    """The job's own timeline — leased, started, progress, artifact, result, error."""

    __tablename__ = "bot_run_events"
    __table_args__ = (
        Index("ix_botevents_job", "job_id", "created_at"),
    )

    id:         Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    job_id:     Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("bot_jobs.id", ondelete="CASCADE"), nullable=False)
    kind:       Mapped[str]       = mapped_column(String(30), nullable=False)
    detail:     Mapped[dict]      = mapped_column(PortableJSON(), nullable=False, default=dict)
    created_at: Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)


class BotCredentialLeaseModel(Base):
    """A per-job credential lease (P4).

    A bot signing into a supplier portal needs that portal's password. It gets a
    TTL-bounded lease revoked at teardown — never a value stored on the
    definition row, which would be static, identical for every job, and readable
    by anyone who can read the table.
    """

    __tablename__ = "bot_credential_leases"
    __table_args__ = (
        Index("ix_botleases_job", "job_id"),
    )

    id:         Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    job_id:     Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("bot_jobs.id", ondelete="CASCADE"), nullable=False)
    vault_ref:  Mapped[str]       = mapped_column(String(512), nullable=False)
    leased_at:  Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)
    expires_at: Mapped[datetime]  = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

class ScheduleModel(Base):
    """One timed action.

    The schedule is the row; **what** it fires is ``action_kind`` +
    ``action_config``. Cron on `bot_definitions` would have meant copying those
    columns onto the next table that wanted a timer, and then "what is scheduled
    on this platform?" would have as many answers as there are features.

    ``next_run_at`` is also the claim. A due schedule is taken with a conditional
    UPDATE whose WHERE still names the timestamp that was read, so two workers
    racing for one firing cannot both win — the same mechanism as a bot-job
    lease, for the same reason.
    """

    __tablename__ = "schedules"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="schedules_name_uq"),
        Index("ix_schedules_due", "status", "next_run_at"),
    )

    id:            Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:     Mapped[str]       = mapped_column(String(255), nullable=False)
    name:          Mapped[str]       = mapped_column(String(255), nullable=False)
    description:   Mapped[str]       = mapped_column(Text, nullable=False, default="")
    #: Five fields or a macro, validated on write — an expression that can never
    #: fire is refused rather than stored.
    cron:          Mapped[str]       = mapped_column(String(120), nullable=False)
    timezone:      Mapped[str]       = mapped_column(String(64), nullable=False, default="UTC")
    #: bot | email | case | connector | case_bulk | report | sync
    action_kind:   Mapped[str]       = mapped_column(String(30), nullable=False)
    action_config: Mapped[dict]      = mapped_column(PortableJSON(), nullable=False, default=dict)
    #: draft until approved by somebody other than the author.
    status:        Mapped[str]       = mapped_column(String(20), nullable=False, default="draft")
    created_by:    Mapped[str]       = mapped_column(String(255), nullable=False)
    #: Who last changed it. Approval refuses the author AND this person — see
    #: `BotDefinitionModel.last_edited_by` for what that closes.
    last_edited_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    approved_by:   Mapped[str | None] = mapped_column(String(255), nullable=True)
    approved_at:   Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_run_at:   Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_run_at:   Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status:   Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_error:    Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at:    Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)
    updated_at:    Mapped[datetime]  = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class ScheduleRunModel(Base):
    """Every firing, including the ones that did nothing.

    "Did the nightly job run?" has to be answerable from a row rather than from a
    log file that has since rotated — and a skipped window is recorded as
    `skipped`, because silence and "it deliberately did not fire" are different
    answers.
    """

    __tablename__ = "schedule_runs"
    __table_args__ = (
        Index("ix_schedule_runs_schedule", "schedule_id", "fired_at"),
    )

    id:          Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    schedule_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("schedules.id", ondelete="CASCADE"), nullable=False)
    fired_at:    Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)
    #: ok | error | skipped
    status:      Mapped[str]       = mapped_column(String(20), nullable=False)
    detail:      Mapped[dict]      = mapped_column(PortableJSON(), nullable=False, default=dict)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at:  Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)


class BotTenantLimitModel(Base):
    """A tenant-wide ceiling on concurrent bot jobs.

    ``BotDefinitionModel.max_concurrency`` bounds one definition. Nothing bounded
    a *tenant*, so twenty definitions each at their own limit could fill the host
    between them — and a bot is a container, not a coroutine. Absent a row the
    platform default applies (``VELARIS_CASE_BOT_TENANT_MAX_CONCURRENT``), so
    this table is an override, never the thing that switches the cap on.
    """

    __tablename__ = "bot_tenant_limits"

    tenant_id:           Mapped[str] = mapped_column(String(255), primary_key=True)
    max_concurrent_jobs: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_by:          Mapped[str] = mapped_column(String(255), nullable=False)
    updated_at:          Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class BotCredentialModel(Base):
    """The store the per-job leases draw from.

    The design assumed HxVault was a path-addressed secret store; it is envelope
    encryption for data at rest. So the store is this table and the property
    that matters is preserved — the value is encrypted under the tenant's DEK
    (``encrypt_credentials(..., vault=True)``), so a database dump yields
    nothing and crypto-shredding a tenant destroys its bot credentials with
    everything else.

    ``version`` is what makes "rotating the secret invalidates outstanding
    leases" true rather than aspirational.
    """

    __tablename__ = "bot_credentials"
    __table_args__ = (
        UniqueConstraint("tenant_id", "ref", name="bot_credentials_ref_uq"),
    )

    id:         Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:  Mapped[str]       = mapped_column(String(255), nullable=False)
    ref:        Mapped[str]       = mapped_column(String(512), nullable=False)
    value_enc:  Mapped[dict]      = mapped_column(PortableJSON(), nullable=False, default=dict)
    created_by: Mapped[str]       = mapped_column(String(255), nullable=False)
    version:    Mapped[int]       = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)
    updated_at: Mapped[datetime]  = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

# <<< RPA / bot runner


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
    created_at:      Mapped[datetime]       = mapped_column(MicroDateTime(), default=_utcnow)
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
    populated_at:  Mapped[datetime]       = mapped_column(MicroDateTime(), default=_utcnow)

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
    created_at:           Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)


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
    created_at:   Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)
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
    installed_at:    Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)
    approved_at:     Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # #29 — the package's OWN declaration, resolved from its verified manifest
    # (v2 tuples, or a v1 manifest read through the §6.3 translation) and
    # carried forward from the workspace item at approval.
    #
    # It is a CEILING, never a grant: it can only narrow what an operator's
    # capability grant allows. A package that never declared an object type
    # cannot reach it even if the grant would have permitted it.
    #
    # NULL means "installed before enforcement existed" and falls back to
    # grant-only behaviour. That is deliberate: an upgrade must not revoke
    # access from packages that are working today, and the value is captured
    # the next time the package is verified.
    declared_permissions: Mapped[list | None] = mapped_column(PortableJSON(), nullable=True)


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
    installed_at:    Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)
    revoked_at:      Mapped[datetime | None]  = mapped_column(DateTime(timezone=True), nullable=True)
    # #29 — the package's OWN declaration, resolved from its verified manifest
    # (v2 tuples, or a v1 manifest read through the §6.3 translation) and
    # carried forward from the workspace item at approval.
    #
    # It is a CEILING, never a grant: it can only narrow what an operator's
    # capability grant allows. A package that never declared an object type
    # cannot reach it even if the grant would have permitted it.
    #
    # NULL means "installed before enforcement existed" and falls back to
    # grant-only behaviour. That is deliberate: an upgrade must not revoke
    # access from packages that are working today, and the value is captured
    # the next time the package is verified.
    declared_permissions: Mapped[list | None] = mapped_column(PortableJSON(), nullable=True)


class MarketplaceCapabilityGrantModel(Base):
    """Marketplace Layer-1 (mig 122) — the capability grant behind a remote app.

    Freedom to publish is not freedom to execute: install creates the grant
    `pending` alongside an INERT connector; admin approval writes the ticked
    subset into `granted` and activates exactly that. One active (non-revoked)
    grant per (tenant, package) — enforced in code, not by partial index
    (MySQL portability).
    """
    __tablename__ = "marketplace_capability_grants"
    __table_args__ = (
        Index("ix_mcg_tenant_package", "tenant_id", "package_id"),
        Index("ix_mcg_status", "status"),
    )

    id:                Mapped[uuid.UUID]      = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:         Mapped[str]            = mapped_column(String(255), nullable=False)
    package_id:        Mapped[str]            = mapped_column(String(255), nullable=False)
    install_id:        Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("marketplace_installs.id", ondelete="SET NULL"), nullable=True)
    workspace_id:      Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("marketplace_workspaces.id", ondelete="SET NULL"), nullable=True)
    connector_id:      Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("connector_registry.id", ondelete="SET NULL"), nullable=True)
    requested:         Mapped[dict]           = mapped_column(PortableJSON(), default=dict)
    granted:           Mapped[dict]           = mapped_column(PortableJSON(), default=dict)
    status:            Mapped[str]            = mapped_column(String(32), nullable=False, default="pending")  # pending|granted|revoked|pending_reapproval
    # P2 drift (mig 123): the regenerated mapping + requested set held for
    # admin re-approval; the live mapping keeps running untouched meanwhile.
    proposed:          Mapped[dict | None]    = mapped_column(PortableJSON(), nullable=True)
    descriptor_sha256: Mapped[str | None]     = mapped_column(String(96), nullable=True)  # bare hex (L1) or sha256:<hex> (L2)
    descriptor_format: Mapped[str | None]     = mapped_column(String(32), nullable=True)
    mapping_version:   Mapped[int]            = mapped_column(Integer, nullable=False, default=1)
    requested_by:      Mapped[str]            = mapped_column(String(255), nullable=False)
    requested_at:      Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow)
    granted_by:        Mapped[str | None]     = mapped_column(String(255), nullable=True)
    granted_at:        Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by:        Mapped[str | None]     = mapped_column(String(255), nullable=True)
    revoked_at:        Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    note:              Mapped[str | None]     = mapped_column(Text, nullable=True)


class MarketplaceContainerModel(Base):
    """Marketplace Layer-2 (mig 124) — one publisher app container.

    Digest-pinned identity, full sandbox hardening, zero DB credentials
    (the scoped broker is the only data path). Starts only on a granted
    capability grant; revocation stops it instantly.
    """
    __tablename__ = "marketplace_containers"
    __table_args__ = (
        Index("ix_mc_tenant_package", "tenant_id", "package_id"),
        Index("ix_mc_grant", "grant_id"),
    )

    id:            Mapped[uuid.UUID]      = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:     Mapped[str]            = mapped_column(String(255), nullable=False)
    package_id:    Mapped[str]            = mapped_column(String(255), nullable=False)
    grant_id:      Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("marketplace_capability_grants.id", ondelete="SET NULL"), nullable=True)
    install_id:    Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("marketplace_installs.id", ondelete="SET NULL"), nullable=True)
    image:         Mapped[str]            = mapped_column(String(512), nullable=False)
    image_digest:  Mapped[str]            = mapped_column(String(96), nullable=False)
    registry:      Mapped[str | None]     = mapped_column(String(255), nullable=True)
    container_id:  Mapped[str | None]     = mapped_column(String(128), nullable=True)
    status:        Mapped[str]            = mapped_column(String(32), nullable=False, default="declared")  # declared|provisioned|running|stopped|destroyed|failed
    port:          Mapped[int | None]     = mapped_column(Integer, nullable=True)
    signature_verified: Mapped[bool]      = mapped_column(Boolean, nullable=False, default=False)
    pulled_at:     Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at:    Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stopped_at:    Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at:    Mapped[datetime]       = mapped_column(MicroDateTime(), default=_utcnow)
    error:         Mapped[str | None]     = mapped_column(Text, nullable=True)


class MarketplaceNetworkLogModel(Base):
    """Every outbound call attempt from marketplace code — blocked or allowed.
    Sandbox container traffic (workspace_id), production Layer-1 connector
    calls (grant_id, mig 122) and RPA bot jobs (job_id, mig 168); any anchor may
    be NULL, never all three.

    A bot job needs its OWN anchor. Stamping its id into `grant_id` looked
    harmless and was not: that column keys `marketplace_capability_grants`, so
    every bot's egress line failed to insert and the audit view answered "this
    bot reached no hosts" when what it meant was "I could not store what it
    reached"."""
    __tablename__ = "marketplace_network_log"
    __table_args__ = (
        Index("ix_mnl_workspace", "workspace_id"),
        Index("ix_mnl_package",   "package_id"),
        Index("ix_mnl_job",       "job_id"),
    )

    id:              Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    workspace_id:    Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("marketplace_workspaces.id", ondelete="CASCADE"), nullable=True)
    grant_id:        Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("marketplace_capability_grants.id", ondelete="SET NULL"), nullable=True)
    job_id:          Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("bot_jobs.id", ondelete="CASCADE"), nullable=True)
    package_id:      Mapped[str]       = mapped_column(String(255), nullable=False)
    destination_url: Mapped[str]       = mapped_column(String(1024), nullable=False)
    destination_ip:  Mapped[str | None]= mapped_column(String(64), nullable=True)
    http_method:     Mapped[str | None]= mapped_column(String(16), nullable=True)
    bytes_sent:      Mapped[int]       = mapped_column(Integer, default=0)
    bytes_received:  Mapped[int]       = mapped_column(Integer, default=0)
    status:          Mapped[str]       = mapped_column(String(16), nullable=False)           # blocked | allowed
    http_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_declared:     Mapped[bool]      = mapped_column(Boolean, default=True)               # False = undeclared domain → SECURITY VIOLATION
    created_at:      Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)


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
    created_at:    Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)
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
    created_at:      Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)


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
    created_at:      Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)
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
    created_at:  Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)


class BackupRecordModel(Base):
    """HxDR — one recorded backup (§8.9, mig 151).

    The platform already *takes* backups: `update-velaris.sh` dumps the
    database before every update and aborts if the dump fails. What was missing
    is the record — without one, "when did we last have a good backup" and
    "does that meet our RPO" are unanswerable, which is the entire point of a
    DR posture.

    Deliberately a **registry, not an executor**. Taking a dump belongs to ops
    tooling that already holds the credentials and can block for minutes; a
    request path must not. This records what was taken, where it landed, and
    whether it has ever been proven restorable.

    `restore_tested_at` is the load-bearing column. An untested backup is a
    hope, not a recovery plan, and reporting one as green is precisely the
    failure DR exists to prevent — so the report treats untested and missing
    as different states.
    """

    __tablename__ = "backup_records"
    __table_args__ = (
        Index("ix_backup_records_region", "region_id", "taken_at"),
        Index("ix_backup_records_status", "status"),
    )

    id:          Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    region_id:   Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("region_registry.id", ondelete="SET NULL"), nullable=True)
    scope:       Mapped[str]       = mapped_column(String(32), nullable=False, default="database")
    status:      Mapped[str]       = mapped_column(String(16), nullable=False, default="succeeded")
    location:    Mapped[str]       = mapped_column(String(512), nullable=False)
    #: Where the bytes physically live. Sovereignty is about residency, so a
    #: backup of an EU region parked in another jurisdiction is a violation
    #: even though the primary data never moved.
    stored_region_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    size_bytes:  Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    checksum:    Mapped[str | None] = mapped_column(String(128), nullable=True)
    encrypted:   Mapped[bool]      = mapped_column(Boolean, nullable=False, default=False)
    error:       Mapped[str | None] = mapped_column(Text, nullable=True)
    tenant_id:   Mapped[str | None] = mapped_column(String(64), nullable=True)
    taken_at:    Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)
    #: NULL means never proven restorable — reported as its own state, never
    #: as healthy.
    restore_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    restore_test_result: Mapped[str | None] = mapped_column(String(16), nullable=True)
    restore_test_notes:  Mapped[str | None] = mapped_column(Text, nullable=True)
    #: How long the drill actually took (mig 155). Until this existed the RTO
    #: was *declared* and never checked against anything, so a backup that
    #: restored in twelve hours against a four-hour objective reported the same
    #: healthy green as one that restored in ten minutes — the posture answered
    #: "can we recover?" but never "in time?".
    restore_test_duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recorded_by: Mapped[str | None] = mapped_column(String(255), nullable=True)


class StandbyEventModel(Base):
    """HxDR — one event of the standby link: link, unlink, promote (mig 222).

    Recorded by the host tool (standby-velaris.sh → compliance/standby_cli.py).
    In the database, so the history travels with the data across a switch-over.
    `detail` is JSON: replication states, LSNs, what a promote could not bring.
    """

    __tablename__ = "dr_standby_events"
    __table_args__ = (Index("ix_dr_standby_events_at", "occurred_at"),)

    id:          Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    event:       Mapped[str]       = mapped_column(String(32), nullable=False)
    peer:        Mapped[str | None] = mapped_column(String(255), nullable=True)
    detail:      Mapped[str]       = mapped_column(Text, nullable=False, default="{}")
    recorded_by: Mapped[str]       = mapped_column(String(64), nullable=False, default="standby-velaris.sh")
    occurred_at: Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)


class LedgerAccountModel(Base):
    """HxLedger — a chart-of-accounts entry (§8.4, mig 150)."""

    __tablename__ = "ledger_accounts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_ledger_account_tenant_code"),
    )

    id:         Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    code:       Mapped[str]       = mapped_column(String(32), nullable=False)
    name:       Mapped[str]       = mapped_column(String(255), nullable=False)
    # asset | liability | income | expense | equity — determines whether a
    # debit increases or decreases the balance.
    account_type: Mapped[str]     = mapped_column(String(16), nullable=False)
    currency:   Mapped[str]       = mapped_column(String(10), nullable=False, default="usd")
    tenant_id:  Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)


class LedgerTransactionModel(Base):
    """HxLedger — one balanced transaction (§8.4, mig 150).

    Exists as its own row for two reasons.

    **Idempotency belongs here, not on a line.** A balanced transaction has at
    least two lines sharing the same source, so a UNIQUE on the entries would
    reject the second half of every posting. `UNIQUE (source_type, source_id,
    purpose)` on the transaction is what makes a payment webhook that fires
    twice post once.

    **A transaction is a real thing.** It carries the date the books record,
    the description, and the assertion that it balanced when written.
    """

    __tablename__ = "ledger_transactions"
    __table_args__ = (
        UniqueConstraint("source_type", "source_id", "purpose",
                         name="uq_ledger_txn_source_purpose"),
        Index("ix_ledger_txn_tenant", "tenant_id", "posted_at"),
    )

    id:          Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    currency:    Mapped[str]       = mapped_column(String(10), nullable=False, default="usd")
    source_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_id:   Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    purpose:     Mapped[str | None] = mapped_column(String(32), nullable=True)
    case_id:     Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    tenant_id:   Mapped[str | None] = mapped_column(String(64), nullable=True)
    # A reversal points at what it reverses — corrections are new rows, never
    # edits, so the books can still say what they said on any past date.
    reverses_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    posted_by:   Mapped[str | None] = mapped_column(String(255), nullable=True)
    posted_at:   Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)


class LedgerEntryModel(Base):
    """HxLedger — one line of a double-entry transaction (§8.4, mig 150).

    The platform had `PaymentRequestModel`, `PaymentDisbursementModel` and
    `InvoiceRecordModel` but no reconciling ledger: money movement was recorded
    but never balanced, which is an audit gap rather than a reporting one.

    **Lines never stand alone.** Every entry belongs to a `transaction_id`, and
    a transaction is only ever written whole and balanced — total debits equal
    total credits, or nothing is stored. That invariant is the entire value of
    double-entry, so it is enforced at write time rather than checked later.

    **Append-only.** A mistake is corrected by posting a reversing transaction,
    never by editing or deleting a line. An edited ledger cannot answer "what
    did the books say on the 3rd", which is the question an auditor asks.

    **Integer cents.** Money is never a float here, matching the existing
    payment models.
    """

    __tablename__ = "ledger_entries"
    __table_args__ = (
        Index("ix_ledger_entries_txn", "transaction_id"),
        Index("ix_ledger_entries_account", "account_id", "posted_at"),
    )

    id:             Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    transaction_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    account_id:     Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("ledger_accounts.id", ondelete="RESTRICT"), nullable=False)
    direction:      Mapped[str]       = mapped_column(String(6), nullable=False)   # debit | credit
    amount_cents:   Mapped[int]       = mapped_column(BigInteger, nullable=False)
    currency:       Mapped[str]       = mapped_column(String(10), nullable=False, default="usd")
    description:    Mapped[str | None] = mapped_column(Text, nullable=True)
    # What in the world this line records — used for reconciliation and to make
    # posting idempotent.
    source_type:    Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_id:      Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    purpose:        Mapped[str | None] = mapped_column(String(32), nullable=True)
    case_id:        Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    tenant_id:      Mapped[str | None] = mapped_column(String(64), nullable=True)
    posted_by:      Mapped[str | None] = mapped_column(String(255), nullable=True)
    posted_at:      Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)


class PIIFindingModel(Base):
    """HxPrivacy — a field that probably holds personal data (§8.10, mig 149).

    A finding is a **proposal**, not a verdict. Detection is heuristic (field
    names, labels, declared input types — never values), so every row carries a
    confidence and the reason it was raised, and a human confirms or dismisses
    it before it drives retention or residency policy.

    That review step matters in both directions: a false positive silently
    over-restricts a harmless field until a feature mysteriously breaks, and a
    false negative leaves personal data untagged while the report looks clean.

    `UNIQUE (resource_type, resource_id, field_path)` keeps a rescan
    idempotent — re-running the scanner updates a finding rather than stacking
    duplicates and burying the reviewer's decision.
    """

    __tablename__ = "pii_findings"
    __table_args__ = (
        UniqueConstraint("resource_type", "resource_id", "field_path",
                         name="uq_pii_finding_resource_field"),
        Index("ix_pii_findings_status", "status"),
    )

    id:            Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    resource_type: Mapped[str]       = mapped_column(String(32), nullable=False)   # form | data_model
    resource_id:   Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    resource_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    field_path:    Mapped[str]       = mapped_column(String(512), nullable=False)
    field_label:   Mapped[str | None] = mapped_column(String(255), nullable=True)
    category:      Mapped[str]       = mapped_column(String(32), nullable=False)
    confidence:    Mapped[float]     = mapped_column(Float, nullable=False, default=0.0)
    reason:        Mapped[str | None] = mapped_column(Text, nullable=True)
    special_category: Mapped[bool]   = mapped_column(Boolean, nullable=False, default=False)
    status:        Mapped[str]       = mapped_column(String(16), nullable=False, default="detected")
    reviewed_by:   Mapped[str | None] = mapped_column(String(255), nullable=True)
    reviewed_at:   Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    tenant_id:     Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_seen_at: Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)
    last_seen_at:  Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)


class ConsentRecordModel(Base):
    """HxConsent — append-only consent & purpose-limitation ledger (§8.1, mig 148).

    GDPR Article 15/17 is reactive: it answers "what do you hold on me" after
    the fact. A ledger is proactive — it records *why* each use of a subject's
    data was lawful, at the moment it became lawful.

    **Append-only.** A withdrawal is a new row, never an update to the grant.
    The question a regulator asks is "was this use lawful **at the time**",
    which an overwritten row cannot answer. Current state is derived by reading
    the latest row per (subject, purpose).

    `basis` is the lawful basis, not a synonym for consent: contract, legal
    obligation and legitimate interest are bases that do **not** require
    consent and cannot be withdrawn by the subject. Collapsing them into a
    boolean would misrepresent the legal position.
    """

    __tablename__ = "consent_records"
    __table_args__ = (
        Index("ix_consent_subject_purpose", "subject_ref", "purpose", "recorded_at"),
        Index("ix_consent_tenant", "tenant_id"),
    )

    id:           Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    subject_ref:  Mapped[str]       = mapped_column(String(255), nullable=False)
    subject_type: Mapped[str]       = mapped_column(String(32), nullable=False, default="email")
    purpose:      Mapped[str]       = mapped_column(String(64), nullable=False)
    action:       Mapped[str]       = mapped_column(String(16), nullable=False)   # granted | withdrawn
    basis:        Mapped[str]       = mapped_column(String(32), nullable=False, default="consent")
    source:       Mapped[str]       = mapped_column(String(64), nullable=False, default="api")
    evidence:     Mapped[dict]      = mapped_column(PortableJSON(), default=dict)
    case_id:      Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    tenant_id:    Mapped[str | None] = mapped_column(String(64), nullable=True)
    actor_id:     Mapped[str | None] = mapped_column(String(255), nullable=True)
    recorded_at:  Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)


class CaseEventModel(Base):
    """Append-only event stream for the Case aggregate (#30a / §2.2, mig 146).

    The relational `case_instances` row stays exactly as it is — it becomes a
    **materialised projection** of this stream rather than being replaced. That
    keeps every existing read path working while making the history verifiable.

    `UNIQUE (case_id, seq)` is the load-bearing constraint. It gives a per-case
    monotonic sequence, which is simultaneously the ordering guarantee, the
    optimistic-concurrency check (two writers racing on the same `seq` — one
    loses), and the gap detector. Nothing in the codebase updates or deletes a
    row here; append-only is enforced in `core/event_store.py` and there is no
    other writer.

    **Clean cutover, no backfill (§7.5).** A case's history before the event
    store existed cannot be honestly reconstructed — `case_step_completions`
    and `case_event_log` record what happened but were never an append-only
    stream. Only cases flagged `case_instances.event_sourced` have a complete
    stream; the rest keep working exactly as before and simply cannot be
    replayed.
    """

    __tablename__ = "case_events"
    __table_args__ = (
        UniqueConstraint("case_id", "seq", name="uq_case_events_case_seq"),
        Index("ix_case_events_case_seq", "case_id", "seq"),
        Index("ix_case_events_type", "event_type"),
    )

    id:          Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    case_id:     Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("case_instances.id", ondelete="CASCADE"), nullable=False)
    seq:         Mapped[int]       = mapped_column(BigInteger, nullable=False)
    event_type:  Mapped[str]       = mapped_column(String(64), nullable=False)
    data:        Mapped[dict]      = mapped_column(PortableJSON(), default=dict)
    actor_id:    Mapped[str | None] = mapped_column(String(255), nullable=True)
    actor_type:  Mapped[str]       = mapped_column(String(20), nullable=False, default="system")
    occurred_at: Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)


class AIActionProposalModel(Base):
    """An `ai_action` step's output, awaiting a human (#33, §5.1, mig 143).

    The step type makes AI a workflow participant, and the participant is a
    *proposer*: unless the step explicitly opts out, nothing it produces
    reaches case data until a person accepts or overrides it. That is the
    HITL requirement in §5.1, and keeping the proposal as a row is what makes
    "who approved this, and what did the model actually say" answerable later.

    `model` / `prompt_sha256` mirror the §5.5 Article-22 provenance recorded
    for DecisionPoints — same reason: a decision nobody can reproduce is not
    auditable.
    """

    __tablename__ = "ai_action_proposals"
    __table_args__ = (
        Index("ix_ai_proposals_case", "case_id", "status"),
    )

    id:            Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    case_id:       Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("case_instances.id", ondelete="CASCADE"), nullable=False)
    stage_id:      Mapped[str | None] = mapped_column(String(255), nullable=True)
    step_id:       Mapped[str]       = mapped_column(String(255), nullable=False)
    status:        Mapped[str]       = mapped_column(String(20), nullable=False, default="pending")
    proposed:      Mapped[dict]      = mapped_column(PortableJSON(), default=dict)
    final_output:  Mapped[dict]      = mapped_column(PortableJSON(), default=dict)
    provider:      Mapped[str | None] = mapped_column(String(64), nullable=True)
    model:         Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error:         Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by:    Mapped[str | None] = mapped_column(String(255), nullable=True)
    decided_at:    Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: F-01 (mig 183): the ledger row this proposal is the ai_action half of.
    #: Nullable, and null on every proposal written before the ledger existed —
    #: which is what those proposals are. Nothing reads it unless the ledger
    #: wrote it.
    ledger_record_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    created_at:    Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)


class LLMCallModel(Base):
    """One completion, recorded once (#32 LLMOps, mig 142).

    Written by the watcher draining `hxnexus.llmops`, not by the caller: the
    recording point is `GuardedBackend.complete`, which has no session. Token
    counts are **estimates** (`tokens_estimated`) because no backend here
    returns provider usage — the column exists so a future backend that does
    can write true counts without a schema change.
    """

    __tablename__ = "llm_calls"
    __table_args__ = (
        Index("ix_llm_calls_created", "created_at"),
        Index("ix_llm_calls_model", "model"),
    )

    id:                Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:         Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider:          Mapped[str]       = mapped_column(String(64), nullable=False)
    model:             Mapped[str]       = mapped_column(String(128), nullable=False)
    route:             Mapped[str]       = mapped_column(String(16), nullable=False, default="direct")
    purpose:           Mapped[str]       = mapped_column(String(64), nullable=False, default="completion")
    prompt_tokens:     Mapped[int]       = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int]       = mapped_column(Integer, default=0)
    tokens_estimated:  Mapped[bool]      = mapped_column(Boolean, default=True)
    cost_usd:          Mapped[float]     = mapped_column(Float, default=0.0)
    latency_ms:        Mapped[float]     = mapped_column(Float, default=0.0)
    status:            Mapped[str]       = mapped_column(String(16), nullable=False, default="ok")
    error_type:        Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at:        Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)


class HxSandboxModuleModel(Base):
    """HxSandbox (#17, mig 140) — a customer-authored WASM module, per tenant.

    The bytes are extracted from the `.hxapp`, validated and stored **at install
    time**, not fetched when a transform runs. Two reasons: a sync pipeline
    calling a transform per row must not depend on the publisher being
    reachable, and `validate_module()` is designed to reject a malformed guest
    before first run rather than mid-pipeline.

    `sha256` is the digest of `wasm_bytes` as stored, so a module can be
    re-verified without re-downloading. Rows are tenant-scoped: revoking one
    tenant's install must not disarm another's.
    """

    __tablename__ = "hxsandbox_modules"
    __table_args__ = (
        UniqueConstraint("tenant_id", "package_id", name="uq_hxsandbox_module_tenant_pkg"),
    )

    id:         Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:  Mapped[str]       = mapped_column(String(255), nullable=False)
    package_id: Mapped[str]       = mapped_column(String(255), nullable=False)
    version:    Mapped[str]       = mapped_column(String(64), nullable=False)
    wasm_bytes: Mapped[bytes]     = mapped_column(LargeBinary, nullable=False)
    sha256:     Mapped[str]       = mapped_column(String(64), nullable=False)
    installed_at: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)


class MarketplacePackInstallModel(Base):
    """Accelerator Packs P1 (mig 134) — a vertical content pack provisioned into a tenant.

    Installing a pack and PROVISIONING it are separate events: install records
    entitlement, provisioning is when case types actually appear in Studio.

    P1 provisions once. `apply_bundle` upserts, so re-provisioning would
    overwrite whatever the tenant has since edited — the failure P2's merge
    exists to prevent. "One row with status='provisioned'" is a partial unique
    constraint, unsupported on MySQL, so it is enforced in
    `packs.already_provisioned` rather than by an index that would silently do
    nothing on one of the three supported engines.
    """
    __tablename__ = "marketplace_pack_installs"
    __table_args__ = (
        # One row per (tenant, package) — mig 136. The rule wanted is "one
        # SUCCESSFUL provision", but that is a partial unique index and MySQL
        # cannot express it, so the row is reused instead: a retry after a
        # failed provision UPDATEs this row rather than inserting a second.
        # Without a DB-level constraint the check-then-insert in provision() is
        # racy, and two concurrent calls would both provision.
        UniqueConstraint("tenant_id", "package_id", name="ux_pack_installs_tenant_pkg"),
        Index("ix_pack_installs_lookup", "tenant_id", "package_id", "status"),
        Index("ix_pack_installs_tenant", "tenant_id", "provisioned_at"),
    )

    id:             Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:      Mapped[str]       = mapped_column(String(255), nullable=False)
    package_id:     Mapped[str]       = mapped_column(String(255), nullable=False)
    pack_version:   Mapped[str]       = mapped_column(String(64), nullable=False)
    vertical:       Mapped[str | None]= mapped_column(String(64), nullable=True)
    status:         Mapped[str]       = mapped_column(String(16), nullable=False, default="provisioned")
    # Straight from apply_bundle's return value — what landed, not what was predicted.
    imported_count: Mapped[int]       = mapped_column(Integer, nullable=False, default=0)
    skipped_count:  Mapped[int]       = mapped_column(Integer, nullable=False, default=0)
    needs_config:   Mapped[list]      = mapped_column(PortableJSON(), default=list)
    errors_json:    Mapped[list]      = mapped_column(PortableJSON(), default=list)
    # Opt-in: silently creating demo cases in a live tenant is not a default.
    sample_data:    Mapped[bool]      = mapped_column(Boolean, nullable=False, default=False)
    sample_count:   Mapped[int]       = mapped_column(Integer, nullable=False, default=0)
    provisioned_by: Mapped[str]       = mapped_column(String(255), nullable=False)
    provisioned_at: Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)
    # P2 (mig 135) — what the pack shipped when it was provisioned. This is the
    # BASE of the three-way merge; without it an upgrade cannot tell a tenant
    # customisation from a change the pack itself made, and would have to either
    # overwrite the customer's work or refuse every upgrade.
    snapshot_json:  Mapped[dict]      = mapped_column(PortableJSON(), default=dict)


class MarketplacePackUpgradeModel(Base):
    """Accelerator Packs P2 (mig 135) — one pack upgrade attempt and its outcome.

    Separate from the install row because an upgrade is an event with a result:
    what applied, and what was skipped because the pack and the tenant had both
    changed the same artifact. Conflicts are never auto-resolved — silently
    picking a side loses somebody's work.
    """
    __tablename__ = "marketplace_pack_upgrades"
    __table_args__ = (
        Index("ix_pack_upgrades_lookup", "tenant_id", "package_id", "upgraded_at"),
    )

    id:             Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:      Mapped[str]       = mapped_column(String(255), nullable=False)
    package_id:     Mapped[str]       = mapped_column(String(255), nullable=False)
    from_version:   Mapped[str]       = mapped_column(String(64), nullable=False)
    to_version:     Mapped[str]       = mapped_column(String(64), nullable=False)
    status:         Mapped[str]       = mapped_column(String(16), nullable=False, default="applied")
    applied_count:  Mapped[int]       = mapped_column(Integer, nullable=False, default=0)
    skipped_count:  Mapped[int]       = mapped_column(Integer, nullable=False, default=0)
    conflicts_json: Mapped[list]      = mapped_column(PortableJSON(), default=list)
    errors_json:    Mapped[list]      = mapped_column(PortableJSON(), default=list)
    upgraded_by:    Mapped[str]       = mapped_column(String(255), nullable=False)
    upgraded_at:    Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)


class MarketplaceConformanceModel(Base):
    """Roadmap #27 (mig 133) — one structural-conformance result per package version.

    The checks themselves are `testsuite.conformance.run_structural` — deterministic
    static analysis, never AI, never live execution, so the gate cannot be flaky.
    This table is only the record the gate reads.

    Three states matter, and the third is the one the grandfather window exists for:
    a `passed` row, a `failed` row, and **no row at all** (never submitted).
    """
    __tablename__ = "marketplace_conformance"
    __table_args__ = (
        # The gate's only question: passed result for this package at this version?
        Index("ix_mkt_conf_lookup", "tenant_id", "package_id", "package_version", "status"),
        Index("ix_mkt_conf_pkg_recent", "tenant_id", "package_id", "created_at"),
    )

    id:              Mapped[uuid.UUID]  = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:       Mapped[str]        = mapped_column(String(255), nullable=False)
    package_id:      Mapped[str]        = mapped_column(String(255), nullable=False)
    package_version: Mapped[str]        = mapped_column(String(64), nullable=False)
    status:          Mapped[str]        = mapped_column(String(16), nullable=False)   # passed|failed
    total:           Mapped[int]        = mapped_column(Integer, nullable=False, default=0)
    passed_count:    Mapped[int]        = mapped_column(Integer, nullable=False, default=0)
    failed_count:    Mapped[int]        = mapped_column(Integer, nullable=False, default=0)
    # Verbatim per-check detail: a refusal must always be explainable to the
    # publisher without re-running anything.
    checks_json:     Mapped[list]       = mapped_column(PortableJSON(), default=list)
    run_id:          Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    # MicroDateTime, not second-precision: the current result per package is
    # resolved with ORDER BY created_at DESC, and same-second ties would fall
    # through to a random UUID — "latest result wins" must be deterministic.
    created_at:      Mapped[datetime]   = mapped_column(MicroDateTime(), default=_utcnow)


class MarketplaceConformanceWindowModel(Base):
    """Roadmap #27 (mig 133) — the platform's grandfather window (report §6.5).

    Singleton. Stored rather than derived from "when the flag was switched on":
    a computed cutover would move on every toggle or restart, silently granting a
    publisher a fresh 60 days or expiring them early — both wrong, both invisible
    until someone is locked out with no explanation.
    """
    __tablename__ = "marketplace_conformance_window"
    __table_args__ = (
        # `singleton` is always 1 and means nothing by itself; it exists so one
        # plain UNIQUE works identically on Postgres and MySQL/MariaDB.
        UniqueConstraint("singleton", name="ux_mkt_conf_window_singleton"),
    )

    id:           Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    singleton:    Mapped[int]       = mapped_column(Integer, nullable=False, default=1)
    cutover_at:   Mapped[datetime]  = mapped_column(MicroDateTime(), nullable=False)
    window_days:  Mapped[int]       = mapped_column(Integer, nullable=False, default=60)
    activated_by: Mapped[str]       = mapped_column(String(255), nullable=False)
    activated_at: Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)


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
    ran_at:        Mapped[datetime]       = mapped_column(MicroDateTime(), default=_utcnow)


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
    created_at:  Mapped[datetime]      = mapped_column(MicroDateTime(), default=_utcnow)
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
    created_at:      Mapped[datetime]      = mapped_column(MicroDateTime(), default=_utcnow)
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
    created_at:    Mapped[datetime]      = mapped_column(MicroDateTime(), default=_utcnow)
    last_used_at:  Mapped[datetime|None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at:    Mapped[datetime|None] = mapped_column(DateTime(timezone=True), nullable=True)


# >>> Case Variables Phase 1 (spec v2 — docs/Future/case_variables.md)
class VariableNamespaceModel(Base):
    """Registered variable namespace — the trust boundary left of the dot.

    Namespaces are rows, not strings: a write to an unregistered namespace is
    rejected, and the namespace a caller writes to is derived from its
    authenticated identity (owner_type + owner_ref), never from a parameter.

    **Tenant-scoped since migration 202.** `name` used to be globally unique
    with no tenant column, so on a box with more than one tenant the first to
    register `crm` owned that name for everybody — and `namespace_grants`, which
    hangs off this row, could not be tenant-scoped either. A NULL `tenant_id`
    now means a PLATFORM namespace, which every tenant resolves when it has none
    of its own; that is what every pre-202 row became, so nothing that resolved
    before stopped resolving.
    """
    __tablename__ = "variable_namespaces"
    __table_args__ = (
        Index("ux_variable_namespaces_tenant_name", "tenant_key", "name", unique=True),
        Index("idx_variable_namespaces_lookup", "name", "tenant_key"),
    )

    id:          Mapped[uuid.UUID]     = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    #: The unique rule is (tenant_key, name), not name — see the class docstring.
    name:        Mapped[str]           = mapped_column(String(100), nullable=False)
    #: NULL = a platform namespace, inherited by every tenant without its own.
    tenant_id:   Mapped[str|None]      = mapped_column(String(255), nullable=True)
    #: `tenant_id` or the literal 'platform'. Written by the application on both
    #: dialects, because NULL <> NULL would leave the platform rows unconstrained.
    tenant_key:  Mapped[str]           = mapped_column(String(255), nullable=False, default="platform")
    owner_type:  Mapped[str]           = mapped_column(String(20), nullable=False)
    owner_ref:   Mapped[uuid.UUID|None] = mapped_column(GUID(), nullable=True)
    sensitivity: Mapped[str]           = mapped_column(String(10), default="internal", nullable=False)
    status:      Mapped[str]           = mapped_column(String(10), default="active", nullable=False)
    created_by:  Mapped[str|None]      = mapped_column(String(255), nullable=True)
    created_at:  Mapped[datetime]      = mapped_column(MicroDateTime(), default=_utcnow)


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
    created_at:           Mapped[datetime]   = mapped_column(MicroDateTime(), default=_utcnow)


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
    # CRDT version stamp (#38, mig 144). Offline clients cannot agree on wall
    # time, so convergence is decided on (lamport, origin), never written_at.
    lamport:    Mapped[int]         = mapped_column(BigInteger, nullable=False, default=0)
    origin:     Mapped[str]         = mapped_column(String(64), nullable=False, default="")
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
    created_at: Mapped[datetime]   = mapped_column(MicroDateTime(), default=_utcnow)
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
    created_at:    Mapped[datetime]       = mapped_column(MicroDateTime(), default=_utcnow)
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
    #: Wrong guesses against the current code (mig 164). Six digits is a small
    #: space and `verify-otp` is anonymous, so the code is bounded by this
    #: rather than by how fast someone can send requests.
    otp_attempts:    Mapped[int]             = mapped_column(Integer, nullable=False, default=0)
    notify_email:    Mapped[bool]            = mapped_column(Boolean, nullable=False, default=True)  # P4 (mig 116)
    created_at:      Mapped[datetime]        = mapped_column(MicroDateTime(), default=_utcnow)
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
    created_at:     Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)


class PortalCsatModel(Base):
    """Portal v2 P5 (mig 117) — one post-resolution rating per case."""
    __tablename__ = "portal_csat"

    case_id:     Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("case_instances.id", ondelete="CASCADE"), primary_key=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("portal_customers.id", ondelete="CASCADE"), nullable=False)
    rating:      Mapped[int]       = mapped_column(Integer, nullable=False)
    comment:     Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at:  Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)


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
    created_at:  Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)


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
    created_at:  Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)


class HxGuardAuthzGenerationModel(Base):
    """One authorization generation for the whole platform (migration 173).

    HxGuard Phase C. Every service runs its own decision point with a 30-second
    cache, and `invalidate_cache()` only ever cleared the cache of the process
    that called it — so after #13c-e a revoke made in case-service left the
    other three serving the old answer until their entries aged out.

    A single row, `id = 1` by CHECK constraint. Services read it on a short
    interval and clear their local cache when the number moves; mutation sites
    bump it in the same transaction as the change itself, so a rolled-back
    mutation cannot leave a bumped generation behind.
    """
    __tablename__ = "hxguard_authz_generation"

    id:         Mapped[int]  = mapped_column(SmallInteger, primary_key=True, default=1)
    generation: Mapped[int]  = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reason:     Mapped[str | None] = mapped_column(String(100), nullable=True)


class PlatformGenerationModel(Base):
    """Change counters for in-process caches, one row per topic (migration 219).

    Generalises `HxGuardAuthzGenerationModel` (R5e step 5): a change bumps its
    topic in the same transaction; every process polls all topics and reloads
    the caches whose counter moved. See `velaris_shared/generations.py`.
    """
    __tablename__ = "platform_generations"

    topic:      Mapped[str] = mapped_column(String(40), primary_key=True)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow)
    reason:     Mapped[str | None] = mapped_column(String(100), nullable=True)


class CommerceCaseRequestModel(Base):
    """A case commerce-service asked case-service to open (migration 220, R5e step 7).

    The request key makes the internal call idempotent; case-service's order
    sweep uses it to find an order-case whose order was never written.
    """
    __tablename__ = "commerce_case_requests"
    __table_args__ = (
        Index("ix_commerce_case_requests_case", "case_id"),
        Index("ix_commerce_case_requests_kind_created", "kind", "created_at"),
    )

    request_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    kind:        Mapped[str] = mapped_column(String(20), nullable=False)
    case_id:     Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    created_at:  Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow)


class JobLeaseModel(Base):
    """Which process may run a background job right now (migration 221, R5e step 9).

    One row per job. Taken when free or expired, renewed while the job runs,
    released when it stops — so a job runs in one process however many run the
    platform. See `velaris_shared/jobs/leases.py`.
    """
    __tablename__ = "job_leases"

    job:         Mapped[str] = mapped_column(String(80), primary_key=True)
    holder:      Mapped[str] = mapped_column(String(200), nullable=False)
    expires_at:  Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow)
    renewed_at:  Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow)


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
    created_at:   Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)


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
    created_at:      Mapped[datetime]        = mapped_column(MicroDateTime(), default=_utcnow)
    claimed_at:      Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at:    Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts:        Mapped[int]             = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Durable-log fan-out (§2.1, mig 145). A SEPARATE cursor from delivered_at
    # so publishing to the event bus cannot disturb HTTP webhook delivery, and
    # either sink can be enabled, disabled or replayed without the other.
    published_at:       Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    publish_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


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
    created_at:       Mapped[datetime]        = mapped_column(MicroDateTime(), default=_utcnow)
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
    created_at:     Mapped[datetime]        = mapped_column(MicroDateTime(), default=_utcnow)


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
    __tablename__ = "velaris_settings"

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
    created_at:         Mapped[datetime]       = mapped_column(MicroDateTime(), default=_utcnow)
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
    created_at:   Mapped[datetime]      = mapped_column(MicroDateTime(), default=_utcnow)


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
    created_at:      Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)


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
    created_at:     Mapped[datetime]       = mapped_column(MicroDateTime(), default=_utcnow)


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
    created_at: Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)


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
    created_at: Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)
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
    created_at:          Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)
    updated_at:          Mapped[datetime]  = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class StorefrontProductImageModel(Base):
    """A product image (held in document storage). First by display_order = primary."""

    __tablename__ = "storefront_product_images"
    __table_args__ = (Index("ix_storefront_images_product", "product_id"),)

    id:            Mapped[uuid.UUID] = mapped_column(GUID(),       primary_key=True, default=_new_uuid)
    product_id:    Mapped[uuid.UUID] = mapped_column(GUID(),       ForeignKey("storefront_products.id", ondelete="CASCADE"), nullable=False)
    media_path:    Mapped[str]       = mapped_column(String(1024), nullable=False)   # document storage key
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
    created_at:   Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)


class StorefrontThemeModel(Base):
    """Versioned theme config JSON per store (keep last 10; one active)."""

    __tablename__ = "storefront_themes"
    __table_args__ = (Index("ix_storefront_themes_store", "store_id"),)

    id:         Mapped[uuid.UUID] = mapped_column(GUID(),       primary_key=True, default=_new_uuid)
    store_id:   Mapped[uuid.UUID] = mapped_column(GUID(),       ForeignKey("storefront_stores.id", ondelete="CASCADE"), nullable=False)
    config:     Mapped[dict]      = mapped_column(PortableJSON(), default=dict)
    version:    Mapped[int]       = mapped_column(Integer,      nullable=False, default=1)
    is_active:  Mapped[bool]      = mapped_column(Boolean,      nullable=False, default=True)
    created_at: Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)


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
    created_at:   Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)
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
    created_at:        Mapped[datetime]    = mapped_column(MicroDateTime(), default=_utcnow)


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
    created_at:  Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)


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
    created_at: Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)


class StorefrontMediaModel(Base):
    """Media library item (held in document storage)."""

    __tablename__ = "storefront_media"
    __table_args__ = (Index("ix_storefront_media_store", "store_id"),)

    id:         Mapped[uuid.UUID] = mapped_column(GUID(),       primary_key=True, default=_new_uuid)
    store_id:   Mapped[uuid.UUID] = mapped_column(GUID(),       ForeignKey("storefront_stores.id", ondelete="CASCADE"), nullable=False)
    media_path: Mapped[str]       = mapped_column(String(1024), nullable=False)
    media_type: Mapped[str]       = mapped_column(String(50),   nullable=False, default="image")  # image|video|file
    size_bytes: Mapped[int]       = mapped_column(BigInteger,   nullable=False, default=0)
    alt_text:   Mapped[str]       = mapped_column(String(512),  default="")
    folder:     Mapped[str]       = mapped_column(String(512),  default="")
    created_at: Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)


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
    created_at: Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)


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
    created_at:  Mapped[datetime]   = mapped_column(MicroDateTime(), default=_utcnow)
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
    created_at:    Mapped[datetime]   = mapped_column(MicroDateTime(), default=_utcnow)


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
    created_at:     Mapped[datetime]   = mapped_column(MicroDateTime(), default=_utcnow)


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
    source_pk:      Mapped[str]        = mapped_column(String(255), nullable=False)  # single-col PK value; 255 keeps uq_hxdbmig_link_row <3072B on MySQL utf8mb4
    case_id:        Mapped[uuid.UUID]  = mapped_column(GUID(), nullable=False)
    case_type_id:   Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    row_checksum:   Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_synced_at: Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_utcnow)
    created_at:     Mapped[datetime]   = mapped_column(MicroDateTime(), default=_utcnow)


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
    created_at:     Mapped[datetime]   = mapped_column(MicroDateTime(), default=_utcnow)


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
    created_at:         Mapped[datetime]   = mapped_column(MicroDateTime(), default=_utcnow)
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
    created_at:    Mapped[datetime]   = mapped_column(MicroDateTime(), default=_utcnow)
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
    created_at:             Mapped[datetime]   = mapped_column(MicroDateTime(), default=_utcnow)


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
    created_at:  Mapped[datetime]   = mapped_column(MicroDateTime(), default=_utcnow)
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
    created_at:      Mapped[datetime]   = mapped_column(MicroDateTime(), default=_utcnow)


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
    created_at:     Mapped[datetime]   = mapped_column(MicroDateTime(), default=_utcnow)


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
    created_at:       Mapped[datetime]   = mapped_column(MicroDateTime(), default=_utcnow)


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
    created_at: Mapped[datetime]   = mapped_column(MicroDateTime(), default=_utcnow)


class AgentModel(Base):
    """F-00: an agent principal — a first-class identity for AI actors.

    Before this, an AI action was attributed to whichever human's request
    happened to trigger it, and a marketplace container acted as the synthetic
    string `app-<row id>` invented at the call site. Both are useless as
    evidence, which is the whole point of the ledger built on top of this.

    `tenant_id` is mandatory and there is deliberately no platform scope: an
    agent with no tenant would be a principal able to act across every tenant
    on the box. A worker serving several tenants gets one row per tenant.
    """
    __tablename__ = "agents"
    __table_args__ = (
        UniqueConstraint("tenant_id", "slug", name="uq_agents_tenant_slug"),
        Index("ix_agents_tenant_status", "tenant_id", "status"),
        Index("ix_agents_package", "package_id"),
    )

    id:            Mapped[uuid.UUID]      = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:     Mapped[uuid.UUID]      = mapped_column(GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    kind:          Mapped[str]            = mapped_column(String(20), nullable=False)   # internal|marketplace|external
    slug:          Mapped[str]            = mapped_column(String(128), nullable=False)
    display_name:  Mapped[str]            = mapped_column(String(255), nullable=False, default="")
    #: The model this agent is DECLARED to run. Attribution uses what actually
    #: answered the call, stamped per call — an agent's binding can change
    #: between two calls and the ledger must record the truth, not the config.
    model_binding: Mapped[str | None]     = mapped_column(String(255), nullable=True)
    package_id:    Mapped[str | None]     = mapped_column(String(255), nullable=True)
    status:        Mapped[str]            = mapped_column(String(20), nullable=False, default="active")
    created_by:    Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    created_at:    Mapped[datetime]       = mapped_column(MicroDateTime(), default=_utcnow)
    updated_at:    Mapped[datetime]       = mapped_column(MicroDateTime(), default=_utcnow, onupdate=_utcnow)


class AgentCapabilityModel(Base):
    """F-00: one capability an agent may ever be granted a token for.

    The ceiling, in the same shape as the manifest permission ceiling of mig
    174 and for the same reason: a ceiling validated once and then discarded is
    not a ceiling. Append-only — a revoked grant keeps its row, because "what
    could this agent do in March" is a question a DELETE cannot answer. Live
    capabilities are the rows with `revoked_at IS NULL`.
    """
    __tablename__ = "agent_capabilities"
    __table_args__ = (
        Index("ix_agent_caps_agent", "agent_id", "revoked_at"),
    )

    id:         Mapped[uuid.UUID]        = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    agent_id:   Mapped[uuid.UUID]        = mapped_column(GUID(), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    capability: Mapped[str]              = mapped_column(String(128), nullable=False)
    granted_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    granted_at: Mapped[datetime]         = mapped_column(MicroDateTime(), default=_utcnow)
    revoked_at: Mapped[datetime | None]  = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)


class AgentTokenGrantModel(Base):
    """F-00: the server-side anchor for a short-lived agent JWT.

    Modelled on `MCPTokenGrantModel`, which got this right: the JWT is
    stateless, this row is the truth, and it is re-read on EVERY call so
    revocation is instant. Time-validity is not revocation.

    `capabilities` is frozen at mint and intersected with the LIVE ceiling on
    every call, so revoking a capability narrows every outstanding token at
    once instead of waiting for expiry.
    """
    __tablename__ = "agent_token_grants"
    __table_args__ = (
        Index("ix_agent_grants_agent", "agent_id", "revoked"),
        Index("ix_agent_grants_expires", "expires_at"),
    )

    id:               Mapped[uuid.UUID]        = mapped_column(GUID(), primary_key=True, default=_new_uuid)  # = JWT jti
    agent_id:         Mapped[uuid.UUID]        = mapped_column(GUID(), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    tenant_id:        Mapped[uuid.UUID]        = mapped_column(GUID(), nullable=False)
    capabilities:     Mapped[list]             = mapped_column(PortableJSON(), nullable=False, default=list)
    #: Case-scoped grant (the F-09 handoff shape, usable now): valid for this
    #: case and no other. NULL means agent-scoped. No FK on purpose — a deleted
    #: case must not take the grant's audit trail with it.
    audience_case_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    label:            Mapped[str | None]       = mapped_column(String(255), nullable=True)
    issued_by:        Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    expires_at:       Mapped[datetime]         = mapped_column(DateTime(timezone=True), nullable=False)
    revoked:          Mapped[bool]             = mapped_column(Boolean, nullable=False, default=False)
    revoked_at:       Mapped[datetime | None]  = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by:       Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    created_at:       Mapped[datetime]         = mapped_column(MicroDateTime(), default=_utcnow)


class AgentStatusHistoryModel(Base):
    """F-00: append-only record of an agent's status changes.

    `agents.status` says what is true now; this says when it became true and
    who decided. Suspending an agent is a security event, and the row it
    mutates would otherwise carry no trace of having ever been active.
    """
    __tablename__ = "agent_status_history"
    __table_args__ = (
        Index("ix_agent_status_agent", "agent_id", "changed_at"),
    )

    id:          Mapped[uuid.UUID]        = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    agent_id:    Mapped[uuid.UUID]        = mapped_column(GUID(), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    from_status: Mapped[str | None]       = mapped_column(String(20), nullable=True)   # None = creation
    to_status:   Mapped[str]              = mapped_column(String(20), nullable=False)
    reason:      Mapped[str]              = mapped_column(Text, nullable=False, default="")
    changed_by:  Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    changed_at:  Mapped[datetime]         = mapped_column(MicroDateTime(), default=_utcnow)


class AIDecisionRecordModel(Base):
    """F-01: one AI decision, recorded as evidence.

    **Immutable.** Every column is written once; `chain_sequence` is the single
    exception and is excluded from the batch hash for exactly that reason.

    The human verdict is deliberately not here — it arrives minutes or days
    later, and a mutable field inside a hashed row would make the seal fail the
    moment somebody reviewed something, reporting an ordinary human action as
    tampering. See `AIDecisionVerdictModel`.
    """
    __tablename__ = "ai_decision_records"
    __table_args__ = (
        Index("ix_aidr_tenant_time", "tenant_id", "recorded_at"),
        Index("ix_aidr_case", "case_id", "recorded_at"),
        Index("ix_aidr_agent", "agent_id", "recorded_at"),
        Index("ix_aidr_model", "model_id"),
        Index("ix_aidr_unsealed", "chain_sequence", "recorded_at"),
    )

    id:                  Mapped[uuid.UUID]        = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:           Mapped[uuid.UUID]        = mapped_column(GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    #: No FK: the ledger outlives the agent. A retired agent must still be
    #: nameable in an older record, and any ON DELETE rule would either forbid
    #: the deletion or quietly rewrite history. Slug and kind are snapshots.
    agent_id:            Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    agent_slug:          Mapped[str | None]       = mapped_column(String(128), nullable=True)
    agent_kind:          Mapped[str | None]       = mapped_column(String(20), nullable=True)
    model_id:            Mapped[str]              = mapped_column(String(255), nullable=False)
    provider_class:      Mapped[str]              = mapped_column(String(16), nullable=False, default="local")
    purpose:             Mapped[str]              = mapped_column(String(64), nullable=False)
    case_id:             Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    step_id:             Mapped[str | None]       = mapped_column(String(255), nullable=True)
    #: The input COMMITMENT, not the input.
    input_sha256:        Mapped[str]              = mapped_column(String(64), nullable=False)
    input_data_classes:  Mapped[list]             = mapped_column(PortableJSON(), nullable=False, default=list)
    output_sha256:       Mapped[str | None]       = mapped_column(String(64), nullable=True)
    #: Written ONLY when a policy attaches the `record_prompt` obligation.
    prompt_sealed:       Mapped[str | None]       = mapped_column(Text, nullable=True)
    output_sealed:       Mapped[str | None]       = mapped_column(Text, nullable=True)
    confidence:          Mapped[float | None]     = mapped_column(Numeric(5, 4), nullable=True)
    latency_ms:          Mapped[float | None]     = mapped_column(Numeric(10, 2), nullable=True)
    #: The HxArbiter decision that gated this call, when there was one.
    arbiter_decision_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    chain_sequence:      Mapped[int | None]       = mapped_column(BigInteger, nullable=True)
    recorded_at:         Mapped[datetime]         = mapped_column(MicroDateTime(), default=_utcnow)


class AIDecisionVerdictModel(Base):
    """F-01: what a human did about one AI decision. Append-only.

    A second review inserts a second row; the current verdict is the newest and
    the earlier one stays visible. "Accepted, then reversed three days later" is
    exactly the sequence an override-rate report exists to show, and an UPDATE
    would erase it.

    There is no `unreviewed` value: an absent row IS unreviewed. Storing it
    would make "nobody has looked at this" indistinguishable from "somebody
    looked and recorded nothing".
    """
    __tablename__ = "ai_decision_verdicts"
    __table_args__ = (
        Index("ix_aidv_record", "record_id", "decided_at"),
        Index("ix_aidv_unsealed", "chain_sequence", "decided_at"),
    )

    id:             Mapped[uuid.UUID]  = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    record_id:      Mapped[uuid.UUID]  = mapped_column(GUID(), ForeignKey("ai_decision_records.id", ondelete="CASCADE"), nullable=False)
    tenant_id:      Mapped[uuid.UUID]  = mapped_column(GUID(), nullable=False)
    verdict:        Mapped[str]        = mapped_column(String(20), nullable=False)   # accepted|edited|rejected
    diff_sha256:    Mapped[str | None] = mapped_column(String(64), nullable=True)
    diff_sealed:    Mapped[str | None] = mapped_column(Text, nullable=True)
    reason:         Mapped[str]        = mapped_column(Text, nullable=False, default="")
    decided_by:     Mapped[str | None] = mapped_column(String(255), nullable=True)
    decided_at:     Mapped[datetime]   = mapped_column(MicroDateTime(), default=_utcnow)
    chain_sequence: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class AILedgerChainModel(Base):
    """F-01: one sealed window over the ledger.

    Same shape as `ArbiterDecisionChainModel`, including the tombstone pair —
    carried here from the start rather than learned again, because migration
    180 shipped a retention horizon that destroyed the arbiter chain the first
    time it ran.

    One chain covers BOTH the records and the verdicts. A verdict is part of the
    evidence, not a footnote to it, and sealing them separately would let one be
    rewritten while the other still verified.
    """
    __tablename__ = "ai_ledger_chain"
    __table_args__ = (
        Index("ix_ailc_pruned", "pruned_at"),
    )

    id:            Mapped[uuid.UUID]       = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    sequence:      Mapped[int]             = mapped_column(BigInteger, nullable=False, unique=True)
    window_start:  Mapped[datetime]        = mapped_column(DateTime(timezone=True), nullable=False)
    window_end:    Mapped[datetime]        = mapped_column(DateTime(timezone=True), nullable=False)
    record_count:  Mapped[int]             = mapped_column(Integer, nullable=False, default=0)
    verdict_count: Mapped[int]             = mapped_column(Integer, nullable=False, default=0)
    prev_hash:     Mapped[str]             = mapped_column(String(64), nullable=False)
    content_hash:  Mapped[str]             = mapped_column(String(64), nullable=False)
    sealed_at:     Mapped[datetime]        = mapped_column(MicroDateTime(), default=_utcnow)
    pruned_at:     Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    pruned_count:  Mapped[int | None]      = mapped_column(Integer, nullable=True)


class ModelRateCardModel(Base):
    """F-02: what a model's tokens cost, per million, for one tenant.

    `tenant_id IS NULL` is the PLATFORM default, inherited by any tenant with no
    row of its own. `tenant_key` collapses that NULL to the literal 'platform'
    so one ordinary UNIQUE constrains both scopes — `UNIQUE (tenant_id,
    model_id)` would not, because NULL <> NULL, and unlimited duplicate platform
    rates is the worst case since every tenant inherits them. The same fix as
    `arbiter_policies.tenant_key` (mig 177), carried over rather than
    rediscovered.

    Distinct from `llmops.MODEL_RATES`, which is a hardcoded platform dict
    pricing the `llm_calls` metric. A tenant on a negotiated contract cannot be
    priced from a constant in the source.
    """
    __tablename__ = "model_rate_cards"
    __table_args__ = (
        UniqueConstraint("tenant_key", "model_id", name="ux_model_rate_scope_model"),
        Index("ix_model_rate_model", "model_id"),
    )

    id:              Mapped[uuid.UUID]        = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:       Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True)
    tenant_key:      Mapped[str]              = mapped_column(String(36), nullable=False)
    model_id:        Mapped[str]              = mapped_column(String(255), nullable=False)
    input_per_mtok:  Mapped[float]            = mapped_column(Numeric(14, 6), nullable=False, default=0)
    output_per_mtok: Mapped[float]            = mapped_column(Numeric(14, 6), nullable=False, default=0)
    currency:        Mapped[str]              = mapped_column(String(8), nullable=False, default="USD")
    created_by:      Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    created_at:      Mapped[datetime]         = mapped_column(MicroDateTime(), default=_utcnow)
    updated_at:      Mapped[datetime]         = mapped_column(MicroDateTime(), default=_utcnow, onupdate=_utcnow)


class DecisionCostModel(Base):
    """F-02: one LINE ITEM of what a decision cost.

    A line, not a decision — an AI call priced at separate input and output
    rates produces two. That is the feature: "explainable" means an operator
    asking why a case cost what it did gets `quantity × unit_rate = amount`
    rather than an assertion.

    Written once and never updated. Re-pricing after a rate change appends new
    lines, or last month's invoice silently changes when this month's contract
    does.
    """
    __tablename__ = "decision_costs"
    __table_args__ = (
        Index("ix_dcost_case", "case_id", "priced_at"),
        Index("ix_dcost_record", "record_id"),
        Index("ix_dcost_tenant", "tenant_id", "priced_at"),
    )

    id:          Mapped[uuid.UUID]        = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:   Mapped[uuid.UUID]        = mapped_column(GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    record_id:   Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("ai_decision_records.id", ondelete="CASCADE"), nullable=True)
    case_id:     Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    step_id:     Mapped[str | None]       = mapped_column(String(255), nullable=True)
    kind:        Mapped[str]              = mapped_column(String(16), nullable=False)   # ai | labour
    model_id:    Mapped[str | None]       = mapped_column(String(255), nullable=True)
    quantity:    Mapped[float]            = mapped_column(Numeric(18, 6), nullable=False, default=0)
    unit:        Mapped[str]              = mapped_column(String(16), nullable=False)   # mtok_in|mtok_out|hour
    unit_rate:   Mapped[float]            = mapped_column(Numeric(14, 6), nullable=False, default=0)
    amount:      Mapped[float]            = mapped_column(Numeric(18, 6), nullable=False, default=0)
    currency:    Mapped[str]              = mapped_column(String(8), nullable=False, default="USD")
    #: tenant | platform | unpriced. An unpriced line is zero AND SAYS SO —
    #: reporting "we have no rate for this model" as "this was free" is how a
    #: cost report becomes a lie an operator acts on.
    rate_source: Mapped[str]              = mapped_column(String(16), nullable=False, default="unpriced")
    estimated:   Mapped[bool]             = mapped_column(Boolean, nullable=False, default=True)
    priced_at:   Mapped[datetime]         = mapped_column(MicroDateTime(), default=_utcnow)


class ComplianceProofModel(Base):
    """F-03: a counterfactual compliance proof.

    HxArbiter's what-if already answers "what would this draft change" for a
    person about to press a button — capped, windowed, kept on the draft and
    never re-derivable. An auditor asks something else: prove what WOULD have
    happened across the entire caseload, and let me re-derive it myself.

    So a proof covers the whole cohort, is content-addressed by
    `content_sha256`, and links to the proof before it — removing or back-dating
    one breaks every proof after it.

    A REFUSED proof is stored rather than discarded. "We could not prove this,
    and here is exactly why" is an answer worth keeping, and silently returning
    nothing invites the question being asked again until it accidentally
    succeeds.
    """
    __tablename__ = "compliance_proofs"
    __table_args__ = (
        UniqueConstraint("sequence", name="ux_proof_sequence"),
        Index("ix_proof_tenant", "tenant_id", "created_at"),
        Index("ix_proof_policy", "policy_id", "policy_version"),
        Index("ix_proof_content", "content_sha256"),
    )

    id:             Mapped[uuid.UUID]        = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    #: NULL = a PLATFORM-scope proof: superadmin-only and aggregate-only, since
    #: a cohort definition can leak which cases exist.
    tenant_id:      Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True)
    subject_kind:   Mapped[str]              = mapped_column(String(32), nullable=False, default="policy_version")
    policy_id:      Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    policy_version: Mapped[int | None]       = mapped_column(Integer, nullable=True)
    namespace:      Mapped[str]              = mapped_column(String(64), nullable=False)
    status:         Mapped[str]              = mapped_column(String(20), nullable=False, default="complete")
    refusal_reason: Mapped[str | None]       = mapped_column(Text, nullable=True)
    cohort:         Mapped[dict]             = mapped_column(PortableJSON(), nullable=False, default=dict)
    #: Covers the ordered decision ids AND the attributes each was replayed
    #: against, so a later edit to any of them is detectable.
    inputs_sha256:  Mapped[str]              = mapped_column(String(64), nullable=False)
    rules_sha256:   Mapped[str]              = mapped_column(String(64), nullable=False)
    result:         Mapped[dict]             = mapped_column(PortableJSON(), nullable=False, default=dict)
    #: Filtered per VIEWER at read time, not at write time: the count is the
    #: proof's finding and must not depend on who is looking, while the list is
    #: case data and must.
    divergent:      Mapped[list]             = mapped_column(PortableJSON(), nullable=False, default=list)
    aggregate_only: Mapped[bool]             = mapped_column(Boolean, nullable=False, default=False)
    prev_hash:      Mapped[str]              = mapped_column(String(64), nullable=False)
    content_sha256: Mapped[str]              = mapped_column(String(64), nullable=False)
    sequence:       Mapped[int]              = mapped_column(BigInteger, nullable=False)
    generated_by:   Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    created_at:     Mapped[datetime]         = mapped_column(MicroDateTime(), default=_utcnow)


class SimulationGateModel(Base):
    """F-04: the thresholds a promotion must clear, per tenant.

    `enabled` defaults to FALSE, and an ABSENT row means the same thing. That
    pair is the whole upgrade story: an install taking this update finds no row,
    and no row means deployments approve exactly as they did yesterday.

    Thresholds are NULL when unchecked rather than zero — zero is the strictest
    possible setting and must never be what an operator gets from a blank field.
    """
    __tablename__ = "simulation_gates"
    __table_args__ = (
        UniqueConstraint("tenant_key", name="ux_simgate_scope"),
    )

    id:                    Mapped[uuid.UUID]        = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:             Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True)
    tenant_key:            Mapped[str]              = mapped_column(String(36), nullable=False)
    enabled:               Mapped[bool]             = mapped_column(Boolean, nullable=False, default=False)
    max_divergent_pct:     Mapped[float | None]     = mapped_column(Numeric(6, 3), nullable=True)
    max_cost_increase_pct: Mapped[float | None]     = mapped_column(Numeric(6, 3), nullable=True)
    min_cohort:            Mapped[int | None]       = mapped_column(Integer, nullable=True)
    require_proof:         Mapped[bool]             = mapped_column(Boolean, nullable=False, default=False)
    #: F-04 second slice (mig 190) — the twin thresholds. NULL means NOT
    #: CHECKED, never zero: an install that already holds a gate row keeps
    #: approving exactly what it approved yesterday, because three unchecked
    #: thresholds cannot fail.
    min_twin_cases:        Mapped[int | None]       = mapped_column(Integer, nullable=True)
    max_twin_failure_pct:  Mapped[float | None]     = mapped_column(Numeric(6, 3), nullable=True)
    max_twin_p95_ms:       Mapped[int | None]       = mapped_column(Integer, nullable=True)
    created_by:            Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    created_at:            Mapped[datetime]         = mapped_column(MicroDateTime(), default=_utcnow)
    updated_at:            Mapped[datetime]         = mapped_column(MicroDateTime(), default=_utcnow, onupdate=_utcnow)


class SimulationRunModel(Base):
    """F-04: one rehearsal of one promotion.

    Append-only: a re-simulation writes a new row and the deployment run points
    at the newest. "It failed twice and then passed" is exactly what somebody
    reviewing a promotion afterwards needs to see.

    `status` distinguishes **failed** (thresholds exceeded) from **refused**
    (the simulation could not be run honestly at all). Letting the second read
    as the first would block promotions for a reason nobody can act on.
    """
    __tablename__ = "simulation_runs"
    __table_args__ = (
        Index("ix_simrun_deploy", "deployment_run_id", "created_at"),
        Index("ix_simrun_tenant", "tenant_id", "created_at"),
    )

    id:                Mapped[uuid.UUID]        = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:         Mapped[uuid.UUID]        = mapped_column(GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    deployment_run_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("deployment_runs.id", ondelete="CASCADE"), nullable=True)
    #: Pinned: a simulation of a different package is not evidence about this
    #: promotion, and the checksum is how the gate refuses to accept it as one.
    package_checksum:  Mapped[str | None]       = mapped_column(String(64), nullable=True)
    status:            Mapped[str]              = mapped_column(String(20), nullable=False)
    findings:          Mapped[dict]             = mapped_column(PortableJSON(), nullable=False, default=dict)
    #: The thresholds AS THEY WERE. Reading them live at review time would let a
    #: later edit change what a past simulation appears to have been judged by.
    thresholds:        Mapped[dict]             = mapped_column(PortableJSON(), nullable=False, default=dict)
    replay_run_id:     Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    proof_id:          Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    #: The F-00 agent the twin work is attributed to; its capability ceiling is
    #: empty by construction.
    twin_agent_id:     Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    #: F-04 second slice (mig 190): the twin execution that informed this
    #: verdict. NULL means the rehearsal was a replay only — which is what
    #: every run said before the twin existed, and what every run says on an
    #: install with no twin configured.
    twin_run_id:       Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    created_by:        Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    created_at:        Mapped[datetime]         = mapped_column(MicroDateTime(), default=_utcnow)
    completed_at:      Mapped[datetime | None]  = mapped_column(DateTime(timezone=True), nullable=True)


class TwinRunModel(Base):
    """F-04 second slice: one execution of a candidate against the twin.

    The twin is not a mode of this process. It is a SECOND case-service
    Temporal worker the operator runs, with its own task queue and its own
    database — and nothing in Velaris starts it. No twin configured means no
    twin run, and the simulation reports exactly what it reported before this
    existed.

    `isolation_verified` is recorded rather than assumed. "The twin was
    actually production" is the one failure of this feature that would be both
    catastrophic and silent, and it is checked on (host, port, database) and
    the queue name — never on the whole connection string, because two DSNs
    differing only by password point at the same database.
    """
    __tablename__ = "twin_runs"
    __table_args__ = (
        Index("ix_twinrun_tenant", "tenant_id", "created_at"),
        Index("ix_twinrun_deploy", "deployment_run_id", "created_at"),
    )

    id:                Mapped[uuid.UUID]        = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:         Mapped[uuid.UUID]        = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    deployment_run_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("deployment_runs.id", ondelete="CASCADE"), nullable=True)
    package_checksum:  Mapped[str | None]       = mapped_column(String(64), nullable=True)
    task_queue:        Mapped[str | None]       = mapped_column(String(255), nullable=True)
    #: A HASH of the twin DSN, never the DSN — a connection string carries a
    #: password and this row is readable by anybody who may read a simulation.
    database_fingerprint: Mapped[str | None]    = mapped_column(String(64), nullable=True)
    isolation_verified: Mapped[bool]            = mapped_column(Boolean, nullable=False, default=False)
    isolation_detail:  Mapped[Any | None]       = mapped_column(PortableJSON(), nullable=True)
    #: What the candidate bundle put there, through the SAME import path a real
    #: promotion uses. A twin that provisioned itself some other way would be
    #: rehearsing a different deployment from the one being approved.
    provisioned:       Mapped[Any | None]       = mapped_column(PortableJSON(), nullable=True)
    #: `batch` today. The column exists so that the day a ramped profile is
    #: built, an old row still says which question it answered.
    load_profile:      Mapped[str]              = mapped_column(String(20), nullable=False, default="batch")
    requested_cases:   Mapped[int]              = mapped_column(Integer, nullable=False, default=0)
    started_cases:     Mapped[int]              = mapped_column(Integer, nullable=False, default=0)
    terminal_cases:    Mapped[int]              = mapped_column(Integer, nullable=False, default=0)
    failed_cases:      Mapped[int]              = mapped_column(Integer, nullable=False, default=0)
    #: Started, did not fail, did not finish. A candidate that STRANDS cases is
    #: the failure this feature exists to catch, and it is invisible if
    #: terminal and failed are the only two counters.
    stuck_cases:       Mapped[int]              = mapped_column(Integer, nullable=False, default=0)
    p50_ms:            Mapped[int | None]       = mapped_column(Integer, nullable=True)
    p95_ms:            Mapped[int | None]       = mapped_column(Integer, nullable=True)
    max_ms:            Mapped[int | None]       = mapped_column(Integer, nullable=True)
    stage_profile:     Mapped[Any | None]       = mapped_column(PortableJSON(), nullable=True)
    failures:          Mapped[Any | None]       = mapped_column(PortableJSON(), nullable=True)
    #: pending | running | complete | refused | failed. `refused` is not
    #: `failed`, exactly as in the gate.
    status:            Mapped[str]              = mapped_column(String(20), nullable=False, default="pending")
    refusal_reason:    Mapped[str | None]       = mapped_column(String(64), nullable=True)
    error:             Mapped[str | None]       = mapped_column(Text, nullable=True)
    torn_down_at:      Mapped[datetime | None]  = mapped_column(DateTime(timezone=True), nullable=True)
    torn_down_rows:    Mapped[int | None]       = mapped_column(Integer, nullable=True)
    #: Set when teardown could not finish. Deliberately NOT an error on the run:
    #: the verdict is still valid, and somebody has to be told the twin needs
    #: sweeping.
    teardown_error:    Mapped[str | None]       = mapped_column(Text, nullable=True)
    created_by:        Mapped[str | None]       = mapped_column(String(255), nullable=True)
    created_at:        Mapped[datetime]         = mapped_column(MicroDateTime(), default=_utcnow)
    completed_at:      Mapped[datetime | None]  = mapped_column(DateTime(timezone=True), nullable=True)


class AgentWorkforceModel(Base):
    """F-05: an agent that is allowed to hold work.

    An F-00 agent WITHOUT one of these is not a worker — it can be attributed
    and can hold a token, and it is simply not in the roster. HxNexus's case-Q&A
    agent should never be handed a case step, and that separation is what stops
    it.

    `escalation_queue` is NOT NULL on purpose: every agent must have a human
    queue its work can fall to, and one whose target is unreachable is
    auto-benched. Fail-closed staffing, because the alternative is work stuck on
    something that cannot ask for help.
    """
    __tablename__ = "agent_workforce"
    __table_args__ = (
        UniqueConstraint("agent_id", name="ux_workforce_agent"),
        Index("ix_workforce_tenant", "tenant_id", "benched"),
    )

    id:                    Mapped[uuid.UUID]        = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    agent_id:              Mapped[uuid.UUID]        = mapped_column(GUID(), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    tenant_id:             Mapped[uuid.UUID]        = mapped_column(GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    #: The SAME taxonomy humans use (`user_directory.skills`). A skill meaning
    #: one thing for a person and another for an agent makes a mixed roster
    #: unreadable.
    skills:                Mapped[list]             = mapped_column(PortableJSON(), nullable=False, default=list)
    #: NULL means uncapped, written that way so "no cap" reads as a decision.
    max_concurrent:        Mapped[int | None]       = mapped_column(Integer, nullable=True)
    #: The cap humans have no equivalent of — what stops a looping agent
    #: spending a month's budget in an afternoon.
    token_budget_per_hour: Mapped[int | None]       = mapped_column(BigInteger, nullable=True)
    escalation_queue:      Mapped[str]              = mapped_column(String(255), nullable=False)
    #: Changed through a PROPOSAL a supervisor approves, never by the scorecard
    #: directly: a system that adjusts its own routing from its own performance
    #: data is a feedback loop nobody is steering.
    routing_weight:        Mapped[float]            = mapped_column(Numeric(5, 3), nullable=False, default=1)
    benched:               Mapped[bool]             = mapped_column(Boolean, nullable=False, default=False)
    bench_reason:          Mapped[str | None]       = mapped_column(Text, nullable=True)
    benched_at:            Mapped[datetime | None]  = mapped_column(DateTime(timezone=True), nullable=True)
    created_by:            Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    created_at:            Mapped[datetime]         = mapped_column(MicroDateTime(), default=_utcnow)
    updated_at:            Mapped[datetime]         = mapped_column(MicroDateTime(), default=_utcnow, onupdate=_utcnow)


class AgentScorecardModel(Base):
    """F-05: an agent's performance over a window, and the weight change it
    supports.

    Every number is DERIVED from F-01 and F-02 and could be recomputed. It is
    stored so a routing-weight proposal can name the evidence it rests on, and
    so a supervisor reviewing that proposal three days later sees what the
    proposer saw rather than what is true now.
    """
    __tablename__ = "agent_scorecards"
    __table_args__ = (
        Index("ix_scorecard_agent", "agent_id", "created_at"),
        Index("ix_scorecard_open", "tenant_id", "proposal_status"),
    )

    id:                Mapped[uuid.UUID]       = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    agent_id:          Mapped[uuid.UUID]       = mapped_column(GUID(), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    tenant_id:         Mapped[uuid.UUID]       = mapped_column(GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    window_start:      Mapped[datetime]        = mapped_column(DateTime(timezone=True), nullable=False)
    window_end:        Mapped[datetime]        = mapped_column(DateTime(timezone=True), nullable=False)
    decisions:         Mapped[int]             = mapped_column(Integer, nullable=False, default=0)
    reviewed:          Mapped[int]             = mapped_column(Integer, nullable=False, default=0)
    accepted:          Mapped[int]             = mapped_column(Integer, nullable=False, default=0)
    overridden:        Mapped[int]             = mapped_column(Integer, nullable=False, default=0)
    #: NULL, never 0.0, on an empty denominator — the F-01 rule, same reason.
    override_rate:     Mapped[float | None]    = mapped_column(Numeric(6, 4), nullable=True)
    cost_total:        Mapped[float]           = mapped_column(Numeric(18, 6), nullable=False, default=0)
    cost_per_decision: Mapped[float | None]    = mapped_column(Numeric(18, 6), nullable=True)
    currency:          Mapped[str | None]      = mapped_column(String(8), nullable=True)
    proposed_weight:   Mapped[float | None]    = mapped_column(Numeric(5, 3), nullable=True)
    proposal_status:   Mapped[str | None]      = mapped_column(String(20), nullable=True)
    proposal_reason:   Mapped[str | None]      = mapped_column(Text, nullable=True)
    decided_by:        Mapped[str | None]      = mapped_column(String(255), nullable=True)
    decided_at:        Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at:        Mapped[datetime]        = mapped_column(MicroDateTime(), default=_utcnow)


class CaseSessionModel(Base):
    """HxMeet P1: a real-time session (meeting) attached to a case."

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
    #: Nullable since mig 172: a session belongs to a case **or** to a calendar
    #: event, exactly one of the two. Before that, HxMeet could only hold a
    #: meeting attached to a case, so a team stand-up could never carry a link.
    case_id:               Mapped[uuid.UUID | None] = mapped_column(GUID(), ForeignKey("case_instances.id", ondelete="CASCADE"), nullable=True)
    calendar_event_id:     Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("calendar_events.id", ondelete="CASCADE"), nullable=True)
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
    created_at:            Mapped[datetime]       = mapped_column(MicroDateTime(), default=_utcnow)


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
    created_at: Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)


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
    # P4d (mig 121): distinct biometric consent — GDPR Art. 9 explicit consent,
    # never implied by the recording consent alone.
    biometric_consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at:          Mapped[datetime]         = mapped_column(MicroDateTime(), default=_utcnow)


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
    created_at:             Mapped[datetime]       = mapped_column(MicroDateTime(), default=_utcnow)
    completed_at:           Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CaseSessionChallengeModel(Base):
    """HxMeet P4c (mig 120) — one randomized liveness challenge issued in-room.

    Minted server-side (the guest can never predict the payload) on a
    RECORD-INTENT session only, so issue + response are captured in the
    sealed recording. The worker records the observed result — human
    judgment, never automated.
    """

    __tablename__ = "case_session_challenges"
    __table_args__ = (
        Index("idx_session_challenges_session", "session_id", "issued_at"),
    )

    id:           Mapped[uuid.UUID]      = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    session_id:   Mapped[uuid.UUID]      = mapped_column(GUID(), ForeignKey("case_sessions.id", ondelete="CASCADE"), nullable=False)
    tenant_id:    Mapped[str | None]     = mapped_column(String(255), nullable=True)
    kind:         Mapped[str]            = mapped_column(String(32), nullable=False)   # head_turn | phrase_readback | document_tilt
    payload:      Mapped[dict]           = mapped_column(PortableJSON(), default=dict)
    issued_by:    Mapped[str]            = mapped_column(String(255), nullable=False)
    issued_at:    Mapped[datetime]       = mapped_column(DateTime(timezone=True), default=_utcnow)
    result:       Mapped[str]            = mapped_column(String(20), nullable=False, default="pending")  # pending|passed|failed|skipped
    result_notes: Mapped[str | None]     = mapped_column(Text, nullable=True)
    result_by:    Mapped[str | None]     = mapped_column(String(255), nullable=True)
    result_at:    Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CaseSessionKycSignalsModel(Base):
    """HxMeet P4c (mig 120) — one passive-signal analysis run per session
    (re-run replaces), over the SEALED recording. Risk score + per-check
    breakdown + deterministic challenge cross-checks vs the sealed live
    transcript. Assistive only: the worker records the KYC verdict.
    """

    __tablename__ = "case_session_kyc_signals"

    session_id:       Mapped[uuid.UUID]      = mapped_column(GUID(), ForeignKey("case_sessions.id", ondelete="CASCADE"), primary_key=True)
    status:           Mapped[str]            = mapped_column(String(20), nullable=False, default="pending")  # pending|running|completed|failed
    risk_score:       Mapped[float | None]   = mapped_column(Float, nullable=True)   # 0..1, higher = riskier
    checks:           Mapped[list]           = mapped_column(PortableJSON(), default=list)
    challenge_checks: Mapped[list]           = mapped_column(PortableJSON(), default=list)
    model_versions:   Mapped[dict]           = mapped_column(PortableJSON(), default=dict)
    error:            Mapped[str | None]     = mapped_column(Text, nullable=True)
    requested_by:     Mapped[str]            = mapped_column(String(255), nullable=False)
    created_at:       Mapped[datetime]       = mapped_column(MicroDateTime(), default=_utcnow)
    completed_at:     Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


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
    created_at:  Mapped[datetime]  = mapped_column(MicroDateTime(), default=_utcnow)


# ── HxSentinel P1 (mig 125) ───────────────────────────────────────────────────


class CaseOutcomeLabelModel(Base):
    """HxSentinel P1 — a TENANT-DEFINED outcome label.

    The core taxonomy lives in ``case_service.sentinel.taxonomy`` as a code
    constant, not here: cross-tenant learning depends on a core label meaning
    the same thing everywhere, and a code constant cannot drift between the
    PostgreSQL migration track and the generated MySQL baseline. This table
    holds only a tenant's own additions.
    """

    __tablename__ = "case_outcome_labels"
    __table_args__ = (
        UniqueConstraint("tenant_id", "key", name="ux_outcome_labels_tenant_key"),
    )

    id:          Mapped[uuid.UUID]  = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:   Mapped[str]        = mapped_column(String(255), nullable=False)
    key:         Mapped[str]        = mapped_column(String(64), nullable=False)
    display:     Mapped[str]        = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_negative: Mapped[bool]       = mapped_column(Boolean, nullable=False, default=True)
    active:      Mapped[bool]       = mapped_column(Boolean, nullable=False, default=True)
    created_at:  Mapped[datetime]   = mapped_column(MicroDateTime(), default=_utcnow)


class CaseOutcomeModel(Base):
    """HxSentinel P1 — the ground truth: a confirmed outcome for a case.

    APPEND-ONLY. Re-labelling, including retroactive labelling of historical
    cases, inserts a NEW row; the current label is the most recent one. The
    label history is itself evidence — who believed what, and when — so these
    rows are never updated in place.
    """

    __tablename__ = "case_outcomes"
    __table_args__ = (
        Index("ix_case_outcomes_case", "case_id", "created_at"),
        Index("ix_case_outcomes_tenant", "tenant_id", "outcome"),
    )

    id:          Mapped[uuid.UUID]  = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:   Mapped[str | None] = mapped_column(String(255), nullable=True)
    case_id:     Mapped[uuid.UUID]  = mapped_column(GUID(), nullable=False)
    outcome:     Mapped[str]        = mapped_column(String(64), nullable=False)
    note:        Mapped[str | None] = mapped_column(Text, nullable=True)
    source:      Mapped[str]        = mapped_column(String(20), nullable=False, default="manual")
    labelled_by: Mapped[str]        = mapped_column(String(255), nullable=False)
    created_at:  Mapped[datetime]   = mapped_column(MicroDateTime(), default=_utcnow)  # µs: latest-outcome-wins


class CaseRiskSignalModel(Base):
    """HxSentinel P1 — one extracted, matchable feature of a case.

    ``signal_key`` is what makes two cases match. For identity-bearing types it
    is a keyed BLAKE2b digest computed with the tenant's own DEK, never the raw
    value: equal values match inside a tenant, the table is useless if it
    leaks, digests cannot be correlated across tenants, and shredding a
    tenant's DEK orphans its signal index (crypto-shredding that follows the
    tenant). Non-identity signals — document hashes, velocity counters, amount
    z-scores — carry no personal data and are stored literally so findings stay
    explainable.
    """

    __tablename__ = "case_risk_signals"
    __table_args__ = (
        Index("ix_risk_signals_lookup", "tenant_id", "signal_type", "signal_key"),
        Index("ix_risk_signals_case", "case_id"),
        UniqueConstraint("case_id", "signal_type", "signal_key", name="ux_risk_signals_unique"),
    )

    id:          Mapped[uuid.UUID]  = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:   Mapped[str | None] = mapped_column(String(255), nullable=True)
    case_id:     Mapped[uuid.UUID]  = mapped_column(GUID(), nullable=False)
    signal_type: Mapped[str]        = mapped_column(String(48), nullable=False)
    signal_key:  Mapped[str]        = mapped_column(String(128), nullable=False)
    hashed:      Mapped[bool]       = mapped_column(Boolean, nullable=False, default=False)
    value:       Mapped[dict]       = mapped_column(PortableJSON(), default=dict)
    computed_at: Mapped[datetime]   = mapped_column(MicroDateTime(), default=_utcnow)


class CaseRiskRuleModel(Base):
    """HxSentinel P2 — a per-tenant OVERRIDE of a code-defined rule.

    Rule definitions live in ``case_service.sentinel.rules`` as a code constant
    (like the taxonomy). A row here overrides enablement, severity, and/or
    threshold params for one rule in one tenant; an absent row means the code
    default applies.
    """

    __tablename__ = "case_risk_rules"
    __table_args__ = (
        UniqueConstraint("tenant_id", "rule_key", name="ux_risk_rules_tenant_key"),
    )

    id:         Mapped[uuid.UUID]   = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:  Mapped[str]         = mapped_column(String(255), nullable=False)
    rule_key:   Mapped[str]         = mapped_column(String(64), nullable=False)
    enabled:    Mapped[bool]        = mapped_column(Boolean, nullable=False, default=True)
    severity:   Mapped[str | None]  = mapped_column(String(16), nullable=True)
    params:     Mapped[dict | None] = mapped_column(PortableJSON(), nullable=True)
    updated_by: Mapped[str]         = mapped_column(String(255), nullable=False)
    updated_at: Mapped[datetime]    = mapped_column(DateTime(timezone=True), default=_utcnow)


class CaseRiskFindingModel(Base):
    """HxSentinel P2 — a recommendation with cited evidence.

    A CURRENT assessment, not an append-only record: the engine upserts one
    finding per (case_id, rule_key), marking a no-longer-firing rule's finding
    ``stale`` rather than deleting it. Persisted so P2b scores reference a stable
    id and the panel audits exactly what was shown; provenance is the audit log,
    not this row. Never a decision — a human reviews and acts.
    """

    __tablename__ = "case_risk_findings"
    __table_args__ = (
        UniqueConstraint("case_id", "rule_key", name="ux_risk_findings_case_rule"),
        Index("ix_risk_findings_tenant", "tenant_id", "severity", "status"),
    )

    id:         Mapped[uuid.UUID]  = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:  Mapped[str | None] = mapped_column(String(255), nullable=True)
    case_id:    Mapped[uuid.UUID]  = mapped_column(GUID(), nullable=False)
    rule_key:   Mapped[str]        = mapped_column(String(64), nullable=False)
    severity:   Mapped[str]        = mapped_column(String(16), nullable=False)
    title:      Mapped[str]        = mapped_column(String(200), nullable=False)
    message:    Mapped[str]        = mapped_column(Text, nullable=False)
    evidence:   Mapped[dict]       = mapped_column(PortableJSON(), default=dict)
    status:     Mapped[str]        = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime]   = mapped_column(MicroDateTime(), default=_utcnow)
    updated_at: Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class CaseRiskHoldModel(Base):
    """HxSentinel P2 — a verify-gate hold on a case's stage advance.

    Deliberately DECOUPLED from the finding that opened it. Opened once when a
    gating-severity finding first appears (and the tenant opted in via
    ``verify_gate_severity``); cleared ONLY by a human, with a note. A later
    re-evaluation never clears or re-opens a hold — that is what keeps the gate
    honest. The advance check fails OPEN: a case is held only when an open hold
    affirmatively exists, so a HxSentinel fault can never wedge the workflow.
    """

    __tablename__ = "case_risk_holds"
    __table_args__ = (
        Index("ix_risk_holds_case_status", "case_id", "status"),
    )

    id:              Mapped[uuid.UUID]  = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:       Mapped[str | None] = mapped_column(String(255), nullable=True)
    case_id:         Mapped[uuid.UUID]  = mapped_column(GUID(), nullable=False)
    finding_id:      Mapped[uuid.UUID]  = mapped_column(GUID(), nullable=False)
    rule_key:        Mapped[str]        = mapped_column(String(64), nullable=False)
    severity:        Mapped[str]        = mapped_column(String(16), nullable=False)
    status:          Mapped[str]        = mapped_column(String(16), nullable=False, default="open")
    opened_at:       Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_utcnow)
    resolved_by:     Mapped[str | None] = mapped_column(String(255), nullable=True)
    resolved_at:     Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class CaseRiskFeedbackModel(Base):
    """HxSentinel P2b — a human's 1–5 score + note on a finding.

    APPEND-ONLY (like `case_outcomes`): re-scoring inserts a new row, the newest
    is current, and the history is evidence. `score` is 1 (bad analysis) to 5
    (best analysis); the note explains why. References a finding's stable id.
    """

    __tablename__ = "case_risk_feedback"
    __table_args__ = (
        Index("ix_risk_feedback_finding", "finding_id", "created_at"),
        Index("ix_risk_feedback_tenant", "tenant_id", "reviewer"),
    )

    id:         Mapped[uuid.UUID]  = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:  Mapped[str | None] = mapped_column(String(255), nullable=True)
    case_id:    Mapped[uuid.UUID]  = mapped_column(GUID(), nullable=False)
    finding_id: Mapped[uuid.UUID]  = mapped_column(GUID(), nullable=False)
    reviewer:   Mapped[str]        = mapped_column(String(255), nullable=False)
    score:      Mapped[int]        = mapped_column(SmallInteger, nullable=False)
    note:       Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime]   = mapped_column(MicroDateTime(), default=_utcnow)  # µs: latest-feedback-wins


class CaseRiskModelModel(Base):
    """HxSentinel P5 — a local, versioned risk-scoring model (logistic regression).

    Trained on LABEL-INDEPENDENT features only, on-platform. Recommends, never
    decides. One version active per tenant; activation is human-gated.
    """

    __tablename__ = "case_risk_models"
    __table_args__ = (
        UniqueConstraint("tenant_id", "version", name="ux_risk_models_tenant_version"),
        Index("ix_risk_models_tenant_status", "tenant_id", "status"),
    )

    id:            Mapped[uuid.UUID]  = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:     Mapped[str]        = mapped_column(String(255), nullable=False)
    version:       Mapped[int]        = mapped_column(Integer, nullable=False)
    coefficients:  Mapped[dict]       = mapped_column(PortableJSON(), default=dict)
    intercept:     Mapped[float]      = mapped_column(Float, nullable=False, default=0.0)
    feature_means: Mapped[dict]       = mapped_column(PortableJSON(), default=dict)
    feature_stds:  Mapped[dict]       = mapped_column(PortableJSON(), default=dict)
    threshold:     Mapped[float]      = mapped_column(Float, nullable=False, default=0.5)
    trained_on:    Mapped[int]        = mapped_column(Integer, nullable=False)
    metrics:       Mapped[dict]       = mapped_column(PortableJSON(), default=dict)
    status:        Mapped[str]        = mapped_column(String(16), nullable=False, default="pending")
    created_at:    Mapped[datetime]   = mapped_column(MicroDateTime(), default=_utcnow)
    activated_by:  Mapped[str | None] = mapped_column(String(255), nullable=True)
    activated_at:  Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CaseRiskSignalPackModel(Base):
    """HxSentinel P4 — an installed marketplace signal pack (content, not code).

    The manifest holds declarative rules whose `when` conditions are CONFORMING
    safe-expression strings the in-core engine evaluates against a fixed feature
    context. Tenant-scoped: installing activates the pack's rules for one tenant.
    """

    __tablename__ = "case_risk_signal_packs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "pack_id", name="ux_risk_packs_tenant_pack"),
        Index("ix_risk_packs_tenant_enabled", "tenant_id", "enabled"),
    )

    id:           Mapped[uuid.UUID]  = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:    Mapped[str]        = mapped_column(String(255), nullable=False)
    pack_id:      Mapped[str]        = mapped_column(String(128), nullable=False)
    name:         Mapped[str]        = mapped_column(String(200), nullable=False)
    vertical:     Mapped[str | None] = mapped_column(String(64), nullable=True)
    version:      Mapped[str]        = mapped_column(String(32), nullable=False)
    manifest:     Mapped[dict]       = mapped_column(PortableJSON(), default=dict)
    enabled:      Mapped[bool]       = mapped_column(Boolean, nullable=False, default=True)
    installed_by: Mapped[str]        = mapped_column(String(255), nullable=False)
    installed_at: Mapped[datetime]   = mapped_column(MicroDateTime(), default=_utcnow)


class CaseRiskRuleProposalModel(Base):
    """HxSentinel P3c — a score-driven rule-change proposal (human-approved).

    Generated from reliability-weighted, dispute-excluded scores. Never applied
    silently: a supervisor approves it, and only then does it change
    `case_risk_rules`. Provenance for the training loop.
    """

    __tablename__ = "case_risk_rule_proposals"
    __table_args__ = (
        Index("ix_risk_proposals_tenant_status", "tenant_id", "status"),
        Index("ix_risk_proposals_rule", "tenant_id", "rule_key", "status"),
    )

    id:              Mapped[uuid.UUID]  = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:       Mapped[str]        = mapped_column(String(255), nullable=False)
    rule_key:        Mapped[str]        = mapped_column(String(64), nullable=False)
    proposed_change: Mapped[dict]       = mapped_column(PortableJSON(), default=dict)
    rationale:       Mapped[str]        = mapped_column(Text, nullable=False)
    evidence:        Mapped[dict]       = mapped_column(PortableJSON(), default=dict)
    status:          Mapped[str]        = mapped_column(String(16), nullable=False, default="pending")
    created_at:      Mapped[datetime]   = mapped_column(MicroDateTime(), default=_utcnow)
    decided_by:      Mapped[str | None] = mapped_column(String(255), nullable=True)
    decided_at:      Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_note:   Mapped[str | None] = mapped_column(Text, nullable=True)


class CaseRiskScoreDisputeModel(Base):
    """HxSentinel P3b — a supervisor's dispute of a reviewer's score.

    Append-only and separate from `case_risk_feedback` so the reviewer's score
    stays immutable. A feedback row is 'disputed' if a dispute row references it;
    disputed scores are excluded from reliability and from training.
    """

    __tablename__ = "case_risk_score_disputes"
    __table_args__ = (
        Index("ix_risk_disputes_feedback", "feedback_id"),
        Index("ix_risk_disputes_tenant", "tenant_id"),
    )

    id:          Mapped[uuid.UUID]  = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:   Mapped[str | None] = mapped_column(String(255), nullable=True)
    feedback_id: Mapped[uuid.UUID]  = mapped_column(GUID(), nullable=False)
    disputed_by: Mapped[str]        = mapped_column(String(255), nullable=False)
    note:        Mapped[str]        = mapped_column(Text, nullable=False)
    created_at:  Mapped[datetime]   = mapped_column(MicroDateTime(), default=_utcnow)


class CaseRiskConfigModel(Base):
    """HxSentinel P2 — per-tenant engine config (the optional verify gate).

    Keyed on the normalised tenant string the engine uses everywhere, so the
    gate's tenant key is identical to the findings'/signals' key. A NULL
    ``verify_gate_severity`` means the gate is OFF (the default).
    """

    __tablename__ = "case_risk_config"

    tenant_id:            Mapped[str]        = mapped_column(String(255), primary_key=True)
    verify_gate_severity: Mapped[str | None] = mapped_column(String(16), nullable=True)
    updated_by:           Mapped[str]        = mapped_column(String(255), nullable=False)
    updated_at:           Mapped[datetime]   = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class RolePrivilegeModel(Base):
    """Phase 6 — privilege entries a platform role holds, in DATA.

    `SEEDED_ROLE_PRIVILEGES` is derived in code from each action's
    `legacy_roles`, and `legacy_roles` is the parity oracle recording what
    `require_role` used to answer — not a policy knob. Without this table there
    is no way to grant a platform role an action except a code change.

    **Additive to the seed, never a replacement.** The cache serving this table
    is in-process, so an unloaded or failed cache is empty; under "replace" that
    would mean every role holds nothing — a silent platform-wide lockout for the
    first seconds of boot. Additive makes an empty cache mean "today's
    behaviour", the only acceptable way for an authorization cache to fail.
    Revocation is an explicit ``{"effect": "deny"}`` entry (deny-wins has existed
    since Phase 1 and no seeded role uses it), so a removal is always a row
    somebody wrote rather than an absence.

    ``role`` is a platform role name from ``velaris_users.roles``, NOT an
    ``access_roles`` row — those are the case-level concept and keep their own
    privileges column.
    """

    __tablename__ = "role_privileges"

    role:       Mapped[str]      = mapped_column(String(64), primary_key=True)
    privileges: Mapped[list]     = mapped_column(PortableJSON(), default=list)
    updated_by: Mapped[str]      = mapped_column(String(255), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


# ─── HxArbiter — sovereignty & security policy engine (migrations 177–178) ────
#
# Everything ships clamped to `shadow`: the engine computes and logs and changes
# no answer anybody receives until an operator raises a namespace ceiling. An
# install taking this update sees no behaviour change at all.


class ArbiterNamespaceModel(Base):
    """A registered action namespace, its default effect, and its global clamp.

    Deliberately NOT tenant-scoped. A tenant varies a default by writing a
    *policy*; the registry is the ceiling, and a ceiling a tenant could raise
    would not be one. Recorded so this is not mistaken for the oversight that
    left `variable_namespaces` globally unique.
    """

    __tablename__ = "arbiter_namespaces"

    namespace:      Mapped[str] = mapped_column(String(64), primary_key=True)
    default_effect: Mapped[str] = mapped_column(String(10), nullable=False)
    #: Every namespace ships at 'shadow'. Raising one is an operator's decision.
    max_mode:       Mapped[str] = mapped_column(String(20), nullable=False, default="shadow")
    #: How long decisions in this namespace are kept. **NULL means forever**,
    #: which is what every install did before mig 180 — purging evidence has
    #: legal weight and is an operator's decision, never an update's.
    decision_retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    description:    Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: Decision-log volume control. Every deny, every obligation-bearing allow
    #: and every legacy disagreement is kept regardless of these.
    sample_allow_rate:   Mapped[float] = mapped_column(Numeric(4, 3), nullable=False, default=1.0)
    sample_default_rate: Mapped[float] = mapped_column(Numeric(4, 3), nullable=False, default=1.0)
    #: When this namespace left shadow — an auditable fact about the platform.
    promoted_at: Mapped[datetime | None] = mapped_column(MicroDateTime(), nullable=True)
    updated_at:  Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow, onupdate=_utcnow)


class ArbiterPolicyModel(Base):
    """A policy's logical identity. Its rules live in versions.

    `tenant_id` NULL means platform-level, which is what `same_tenant()` already
    treats as platform-shared. Platform and tenant policies are evaluated
    together under deny-overrides, so a tenant tightens the baseline and can
    never loosen it.
    """

    __tablename__ = "arbiter_policies"
    __table_args__ = (
        # `tenant_key` and not `tenant_id`: NULL <> NULL in SQL, so a UNIQUE
        # over a nullable tenant column does not constrain PLATFORM policy names
        # at all — proven against a live PostgreSQL, which accepted three
        # identical ones. This column collapses NULL to 'platform'.
        UniqueConstraint("tenant_key", "name", name="ux_arbiter_policies_scope_name"),
        Index("ix_arbiter_policies_lookup", "namespace", "status", "tenant_id"),
    )

    id:         Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:  Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True)
    #: Maintained by the writer, guarded by a CHECK in the migration. A plain
    #: column rather than GENERATED because MariaDB 11.8 refuses COALESCE in a
    #: GENERATED ALWAYS AS clause where MySQL 8.4 accepts it.
    tenant_key: Mapped[str] = mapped_column(String(36), nullable=False)
    name:        Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    namespace:   Mapped[str] = mapped_column(String(64), nullable=False)
    status:      Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    active_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: `legacy_bridge` marks a policy mirroring an existing control. It retires
    #: when that control does, not when somebody decides it looks redundant.
    origin:     Mapped[str] = mapped_column(String(20), nullable=False, default="user")
    created_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow, onupdate=_utcnow)
    #: When retirement began. A retired policy keeps evaluating in shadow for a
    #: soak period, because removing enforcement is not the mirror of adding it.
    retired_at: Mapped[datetime | None] = mapped_column(MicroDateTime(), nullable=True)


class ArbiterPolicyVersionModel(Base):
    """An immutable version. An edit creates the next one; none is ever rewritten."""

    __tablename__ = "arbiter_policy_versions"
    __table_args__ = (
        Index("ix_arbiter_versions_active_window",
              "policy_id", "activated_at", "deactivated_at"),
    )

    policy_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("arbiter_policies.id", ondelete="CASCADE"), primary_key=True)
    version:   Mapped[int] = mapped_column(Integer, primary_key=True)
    rules:     Mapped[list] = mapped_column(PortableJSON(), nullable=False, default=list)
    mode:      Mapped[str] = mapped_column(String(20), nullable=False, default="shadow")
    nl_source:   Mapped[str | None] = mapped_column(Text, nullable=True)
    nl_readback: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Nullable: a legacy-bridge version is generated by the platform and has no
    #: human author. A sentinel would put a fictional name on a policy an
    #: auditor reads.
    created_by:  Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    #: Four-eyes on a single-admin install is a cooling-off delay, which is
    #: unimplementable without knowing when approval happened.
    approved_at: Mapped[datetime | None] = mapped_column(MicroDateTime(), nullable=True)
    #: SHA-256 over canonical JSON — hex, exactly 64 characters.
    signature: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
    #: The activation interval. "Prove this rule was active on 3 March" has no
    #: answer from a current-version pointer alone, and cannot be recovered from
    #: the decision log because allow-and-default rows are sampled once a
    #: namespace enforces.
    activated_at:   Mapped[datetime | None] = mapped_column(MicroDateTime(), nullable=True)
    deactivated_at: Mapped[datetime | None] = mapped_column(MicroDateTime(), nullable=True)


class ArbiterDecisionModel(Base):
    """One evaluated decision. The evidence the product is sold on."""

    __tablename__ = "arbiter_decisions"
    __table_args__ = (
        Index("ix_arb_dec_tenant_ns_time", "tenant_id", "namespace", "evaluated_at"),
        Index("ix_arb_dec_subject", "tenant_id", "subject_id", "evaluated_at"),
        Index("ix_arb_dec_disagreement",
              "legacy_effect", "tenant_id", "namespace", "evaluated_at"),
        Index("ix_arb_dec_unsealed", "chain_sequence", "evaluated_at"),
    )

    id:        Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    #: Always the resolved tenant UUID, never the slug a token happened to
    #: carry. A log holding both forms cannot be filtered.
    tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    namespace: Mapped[str] = mapped_column(String(64), nullable=False)
    action:    Mapped[str] = mapped_column(String(128), nullable=False)
    #: Denormalised out of `subject` and indexed: "every decision about this
    #: principal" is a core audit query, and from JSON it would need a different
    #: expression per backend and an index on none.
    subject_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    subject_id:   Mapped[str] = mapped_column(String(255), nullable=False)
    subject:  Mapped[dict] = mapped_column(PortableJSON(), nullable=False, default=dict)
    resource: Mapped[dict] = mapped_column(PortableJSON(), nullable=False, default=dict)
    #: Redacted per HxPrivacy before it is written. A decision log that
    #: faithfully records the personal data it was asked about becomes a second
    #: copy of that data, governed by nothing.
    context:  Mapped[dict] = mapped_column(PortableJSON(), nullable=False, default=dict)
    effect:   Mapped[str] = mapped_column(String(10), nullable=False)
    #: `enforced` alone collapses shadow and dry_run, which the product treats
    #: differently: dry_run surfaces "would have been denied" in Studio, shadow
    #: does not appear there at all.
    mode:     Mapped[str] = mapped_column(String(20), nullable=False)
    enforced:        Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    default_applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    matched:     Mapped[list] = mapped_column(PortableJSON(), nullable=False, default=list)
    obligations: Mapped[list] = mapped_column(PortableJSON(), nullable=False, default=list)
    #: What the control being replaced said. A disagreement is the whole output
    #: of a parity report, and is never sampled away.
    legacy_effect: Mapped[str | None] = mapped_column(String(10), nullable=True)
    #: Digest over the active version signatures. Needed because a
    #: default_applied row names no policy at all, and "nothing matched" is the
    #: answer most worth questioning later.
    policy_set_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Why the evaluator could not answer, when it could not — `pip_timeout` and
    #: friends. Without it a fail-closed deny is indistinguishable from a policy
    #: deny on screen.
    error:      Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Who asserted `subject`. A PEP legitimately asks on behalf of somebody
    #: else, which makes `subject` an assertion rather than an identity — and an
    #: assertion nobody signed for is unattributable. Stamped by the router from
    #: the authenticated caller, never read from the body.
    claimed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    simulated:  Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    latency_us: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: NULL means "not yet sealed", never "unsealable".
    chain_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    evaluated_at:   Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)


class ArbiterDecisionChainModel(Base):
    """One sealed batch of decisions — what makes the log evidence, not rows.

    `content_hash = sha256(prev_hash || canonical(batch))`, the same shape as
    `case_audit_log_chain`, so the existing `audit_anchors` RFC-3161 receipts
    over a chain tip apply unchanged. Batched rather than per-decision because
    chaining each one would serialise the hot path behind a single sequence.

    A gap in the sealed windows is itself evidence: it says rows were removed.
    """

    __tablename__ = "arbiter_decision_chain"
    __table_args__ = (
        Index("ix_arb_chain_window", "window_start", "window_end"),
        Index("idx_arb_chain_pruned", "pruned_at"),
    )

    id:       Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    window_start: Mapped[datetime] = mapped_column(MicroDateTime(), nullable=False)
    window_end:   Mapped[datetime] = mapped_column(MicroDateTime(), nullable=False)
    decision_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    prev_hash:    Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    sealed_at:    Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
    #: Set when retention deleted the decisions this batch covered (mig 181).
    #: The batch keeps its `content_hash`, so the chain still links and still
    #: proves nothing was edited; what is lost is the ability to re-derive this
    #: batch's content, which is inherent to deleting it. NULL means never
    #: pruned, which is every batch until an operator sets a horizon.
    pruned_at:    Mapped[datetime | None] = mapped_column(MicroDateTime(), nullable=True)
    pruned_count: Mapped[int | None] = mapped_column(Integer, nullable=True)


class ArbiterDraftModel(Base):
    """A natural-language policy draft and how it came to be proposed (mig 179).

    A draft is not a policy. It is the evidence trail behind one, and the
    provenance columns are the point: "your compliance control was written by an
    LLM" is the first question in the room, and `model_provider` / `model_name` /
    `prompt_version` are what turn "we don't know" into an answer.

    `readback` is what an approver actually signed, and it is generated WITHOUT
    the intent in context — so it describes what the rules do rather than
    agreeing with what the author meant.
    """

    __tablename__ = "arbiter_drafts"
    __table_args__ = (
        Index("ix_arbiter_drafts_tenant_state", "tenant_id", "state", "created_at"),
    )

    id:        Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    #: NULL ⇒ the draft proposes a NEW policy, not a version of an existing one.
    policy_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("arbiter_policies.id", ondelete="CASCADE"), nullable=True)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True)
    namespace: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The operator's own words, verbatim — the readback is compared against it.
    intent:          Mapped[str] = mapped_column(Text, nullable=False)
    generated_rules: Mapped[list | None] = mapped_column(PortableJSON(), nullable=True)
    compile_report:  Mapped[dict | None] = mapped_column(PortableJSON(), nullable=True)
    analysis_report: Mapped[list | None] = mapped_column(PortableJSON(), nullable=True)
    readback:        Mapped[str | None] = mapped_column(Text, nullable=True)
    readback_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    test_seeds:      Mapped[list | None] = mapped_column(PortableJSON(), nullable=True)
    whatif_report:   Mapped[dict | None] = mapped_column(PortableJSON(), nullable=True)
    model_provider:  Mapped[str | None] = mapped_column(String(64), nullable=True)
    model_name:      Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_version:  Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: A draft that only compiled on the repair round is worth a closer read.
    attempts:    Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    #: Which legal texts drove this (§19.6 regulatory change management).
    corpus_refs: Mapped[list | None] = mapped_column(PortableJSON(), nullable=True)
    state:       Mapped[str] = mapped_column(String(32), nullable=False, default="drafted")
    created_by:  Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    created_at:  Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
    updated_at:  Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow,
                                                  onupdate=_utcnow)


# ═══════════════════════════════════════════════════════════════════════
# F-06 — ambient case capture (mig 188)
# ═══════════════════════════════════════════════════════════════════════


class CaptureRunModel(Base):
    """One extraction pass over one source — a transcript, an email, a note.

    The run exists so a proposal can say what it came from. `source_sha256` is
    the commitment: the hash of the exact text the model read, so a proposal
    reviewed a week later can be checked against the source it claims to
    summarise, and a re-run over changed text is visibly a different run rather
    than an unexplained second opinion.

    `refused` is not `failed`. Consent absent, capture switched off, or a source
    with nothing in it are the gate working; recording them as faults would send
    somebody hunting a bug that is not there.
    """
    __tablename__ = "capture_runs"
    __table_args__ = (
        Index("ix_capture_runs_case", "case_id", "created_at"),
        Index("ix_capture_runs_source", "source_kind", "source_ref"),
    )

    id:             Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:      Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    case_id:        Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("case_instances.id", ondelete="CASCADE"), nullable=False)
    source_kind:    Mapped[str]  = mapped_column(String(20), nullable=False)
    source_ref:     Mapped[str]  = mapped_column(String(255), nullable=False)
    source_sha256:  Mapped[str]  = mapped_column(String(64), nullable=False)
    source_locator: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status:         Mapped[str]  = mapped_column(String(20), nullable=False, default="running")
    refusal_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error:          Mapped[str | None] = mapped_column(Text, nullable=True)
    provider:       Mapped[str | None] = mapped_column(String(64), nullable=True)
    model:          Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_sha256:  Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: A run that only parsed on the repair round is worth a closer read.
    attempts:       Mapped[int]  = mapped_column(Integer, nullable=False, default=1)
    #: A dropped proposal leaves no row by design — it is not a suggestion
    #: anybody may accept — so this count is the only trace it existed.
    proposed_count: Mapped[int]  = mapped_column(Integer, nullable=False, default=0)
    dropped_count:  Mapped[int]  = mapped_column(Integer, nullable=False, default=0)
    requested_by:   Mapped[str]  = mapped_column(String(255), nullable=False)
    created_at:     Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
    completed_at:   Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CaptureProposalModel(Base):
    """One suggested change, awaiting a person.

    Proposal-only, always: nothing here has any effect until somebody accepts
    or edits it, and the row keeps both what was suggested and what was decided
    — which is what makes the capture's real accuracy measurable later instead
    of merely felt.

    `kind` is closed **structurally**, not by asking the model nicely: a kind
    this table does not accept cannot be stored, so it cannot be accepted. That
    is the design's whitelist — capture reaches case content and nothing else,
    and no injected instruction can widen it, because widening it would take a
    migration.
    """
    __tablename__ = "capture_proposals"
    __table_args__ = (
        Index("ix_capture_prop_case", "case_id", "status"),
        Index("ix_capture_prop_run", "run_id"),
    )

    id:             Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    run_id:         Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("capture_runs.id", ondelete="CASCADE"), nullable=False)
    tenant_id:      Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    case_id:        Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("case_instances.id", ondelete="CASCADE"), nullable=False)
    kind:           Mapped[str] = mapped_column(String(20), nullable=False)
    target:         Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: The before, captured when the proposal was made. Re-reading it at
    #: decision time would show a value that may have moved since.
    current_value:  Mapped[Any | None] = mapped_column(PortableJSON(), nullable=True)
    proposed_value: Mapped[Any | None] = mapped_column(PortableJSON(), nullable=True)
    #: NULL when the model reported none — never 0, which reads as "confident it
    #: is wrong" rather than "it did not say".
    confidence:     Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    snippet:        Mapped[str | None] = mapped_column(Text, nullable=True)
    locator:        Mapped[str | None] = mapped_column(String(255), nullable=True)
    status:         Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    applied_value:  Mapped[Any | None] = mapped_column(PortableJSON(), nullable=True)
    decided_by:     Mapped[str | None] = mapped_column(String(255), nullable=True)
    decided_at:     Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: One F-01 record per PROPOSAL, not per run: the human answers each one
    #: separately, and a ledger verdict covers one decision.
    ledger_record_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    created_at:     Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)


# ═══════════════════════════════════════════════════════════════════════
# F-07 — sovereign model marketplace (mig 189)
# ═══════════════════════════════════════════════════════════════════════


class ModelPackageModel(Base):
    """One artefact this install has published, or taken in.

    Both directions live in one table because they are the same object seen
    from two ends: "what left here" and "what did we take, and from whom" are
    the same question with `direction` flipped.

    Only two artefact kinds exist, and both are weights-only by construction —
    the HxSentinel risk model (five coefficients, an intercept, five means,
    five standard deviations, a threshold) and a signal pack (declarative safe
    expressions). Neither format can carry code. That is a property of the
    format, not a promise in a manifest.

    `publisher_ref` is what the FILE claims; `tenant_id` is who holds the row.
    They are separate columns because a claim and a fact must never be read off
    the same one.
    """
    __tablename__ = "model_packages"
    __table_args__ = (
        Index("ix_model_pkg_tenant", "tenant_id", "direction", "status"),
        Index("ix_model_pkg_content", "content_sha256"),
        UniqueConstraint("tenant_id", "direction", "content_sha256",
                         name="ux_model_pkg_tenant_content"),
    )

    id:            Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:     Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    #: published | imported
    direction:     Mapped[str] = mapped_column(String(16), nullable=False)
    #: risk_model | signal_pack — closed structurally, not by convention.
    kind:          Mapped[str] = mapped_column(String(20), nullable=False)
    package_id:    Mapped[str] = mapped_column(String(255), nullable=False)
    version:       Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    #: SHA-256 over the canonical body. What the signature covers, and what an
    #: importer recomputes before reading anything inside.
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The model card — class level only, never a value from a case.
    manifest:      Mapped[Any] = mapped_column(PortableJSON(), nullable=False, default=dict)
    #: The weights. Numbers and strings; the scanner refuses anything else
    #: before this column is written.
    payload:       Mapped[Any] = mapped_column(PortableJSON(), nullable=False, default=dict)
    signature:     Mapped[str | None] = mapped_column(Text, nullable=True)
    signing_key_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    publisher_ref:  Mapped[str | None] = mapped_column(String(255), nullable=True)
    publisher_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_ref:    Mapped[str | None] = mapped_column(String(255), nullable=True)
    trained_on:    Mapped[int | None] = mapped_column(Integer, nullable=True)
    declared_metrics: Mapped[Any | None] = mapped_column(PortableJSON(), nullable=True)
    eval_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: pass | blocked | not_run. A membership-disclosure check, not a
    #: formality: feature means and standard deviations ARE summary statistics
    #: of the publisher's own cases, and over a small enough cohort a mean stops
    #: being an aggregate.
    canary_verdict: Mapped[str] = mapped_column(String(20), nullable=False, default="not_run")
    canary_detail: Mapped[Any | None] = mapped_column(PortableJSON(), nullable=True)
    data_classes:  Mapped[Any | None] = mapped_column(PortableJSON(), nullable=True)
    #: A special-category model is unpublishable. The column records the
    #: refusal, so "why can we not share this" has an answer.
    special_category: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    signed_off_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    signed_off_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: draft | published | revoked | quarantined. None of them delete the row:
    #: what a package contained is what somebody needs after it is withdrawn.
    status:        Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by:    Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at:    Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
    updated_at:    Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow,
                                                    onupdate=_utcnow)


class ModelPackageEvalModel(Base):
    """One sandbox evaluation of one package against one tenant's own holdout.

    The gate. It is its own table rather than columns on the install because a
    refusal leaves no install — and a refusal is exactly the record worth
    keeping.

    `measured` is allowed to be worse than `declared`: a model trained on
    someone else's distribution may simply not transfer, and that is a fact
    about the data, not a fault in the package. What is refused is a claim that
    cannot be reproduced within tolerance.
    """
    __tablename__ = "model_package_evals"
    __table_args__ = (
        Index("ix_model_eval_pkg", "package_ref", "evaluated_at"),
    )

    id:           Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    package_ref:  Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("model_packages.id", ondelete="CASCADE"), nullable=False)
    tenant_id:    Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    holdout_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    positives:    Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    negatives:    Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    declared:     Mapped[Any | None] = mapped_column(PortableJSON(), nullable=True)
    measured:     Mapped[Any | None] = mapped_column(PortableJSON(), nullable=True)
    tolerance:    Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    #: pass | fail | insufficient_data. The third is NOT a failure — a tenant
    #: with no labelled history cannot judge a stranger's model, and calling
    #: that "failed" sends them hunting a fault in the package.
    verdict:      Mapped[str] = mapped_column(String(20), nullable=False)
    reason:       Mapped[str | None] = mapped_column(Text, nullable=True)
    evaluated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    evaluated_at: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)


class ModelPackageInstallModel(Base):
    """An imported package placed into this tenant's runtime.

    The link between a package and what it BECAME. Without it, an operator
    looking at an active risk model has no way to learn it came from outside —
    and "which of our models are ours" is the first question sovereignty asks.

    `staged` is the resting state and the honest one: install writes a PENDING
    row into `case_risk_models` and stops. Activation is HxSentinel's existing
    human gate; this feature does not add a second one.
    """
    __tablename__ = "model_package_installs"
    __table_args__ = (
        Index("ix_model_install_tenant", "tenant_id", "status"),
        UniqueConstraint("tenant_id", "package_ref", name="ux_model_install_pkg"),
    )

    id:           Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    package_ref:  Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("model_packages.id", ondelete="CASCADE"), nullable=False)
    tenant_id:    Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    #: The attribute HxArbiter policies read. A column rather than an
    #: implication, so a policy asserts a fact instead of inferring one from
    #: the presence of a row.
    model_origin: Mapped[str] = mapped_column(String(20), nullable=False, default="marketplace")
    target_kind:  Mapped[str] = mapped_column(String(20), nullable=False)
    target_ref:   Mapped[str | None] = mapped_column(String(255), nullable=True)
    eval_ref:     Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("model_package_evals.id", ondelete="SET NULL"), nullable=True)
    #: staged | activated | uninstalled | shredded
    status:       Mapped[str] = mapped_column(String(20), nullable=False, default="staged")
    installed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    installed_at: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
    activated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    uninstalled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Uninstall-with-delete overwrites the payload and records that it did. The
    #: row survives the weights: that a tenant once ran a stranger's model is
    #: not something a delete button gets to erase.
    shredded_at:  Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    shred_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)


# ═══════════════════════════════════════════════════════════════════════
# F-08 — self-healing processes (mig 191)
# ═══════════════════════════════════════════════════════════════════════


class HealingEpisodeModel(Base):
    """One healing attempt, whole.

    HxEvolve mines deviations and proposes replay-proven changes, and then
    stops. This closes that loop — as far as a loop may honestly be closed,
    which is **one approval, not none**.

    The row holds the entire story: the diagnosis that started it, the candidate
    change, every rung of the validation ladder and where it stopped, the
    human's answer, what was applied, what happened afterwards, and whether it
    had to be undone. The roadmap asks for one chained evidence record that can
    show an auditor *why the platform changed itself*, and a story split across
    five tables is not that.

    Three structural safeties:

    · **The operator catalog is the boundary.** A healing change can only be one
      of HxEvolve's five signed-off kinds. Auth config, HxArbiter policy,
      egress, credentials and agent autonomy are not forbidden — they are
      *inexpressible*, because no operator can name them.
    · **Nothing applies without a person**, through HxBranch's existing audited
      merge rather than a second way into config.
    · **The rollback is pre-signed** at approval time, so undoing a change at
      3 a.m. replays a snapshot a named human already approved.
    """
    __tablename__ = "healing_episodes"
    __table_args__ = (
        Index("ix_heal_tenant", "tenant_id", "status"),
        Index("ix_heal_ct", "case_type_id", "created_at"),
        Index("ix_heal_watch", "status", "watch_until"),
        UniqueConstraint("sequence", name="ux_heal_sequence"),
    )

    id:           Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:    Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    case_type_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    #: An episode never invents a change: it takes one HxEvolve already proposed
    #: and gate-validated, then asks harder questions of it.
    insight_id:   Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    #: The mining signal verbatim — an auditor needs the reason as it stood.
    diagnosis:    Mapped[Any] = mapped_column(PortableJSON(), nullable=False, default=dict)
    operator:     Mapped[str] = mapped_column(String(32), nullable=False)
    proposal:     Mapped[Any] = mapped_column(PortableJSON(), nullable=False, default=dict)
    #: Every rung, in order. The ladder STOPS at the first refusal: a change
    #: that cannot be expressed safely should not then be replayed across the
    #: whole caseload to learn whether it would also have been slow.
    ladder:       Mapped[Any] = mapped_column(PortableJSON(), nullable=False, default=list)
    stopped_at:   Mapped[str | None] = mapped_column(String(32), nullable=True)
    stopped_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    proof_id:      Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    simulation_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    twin_run_id:   Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    replay_run_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    #: pending | ready | refused | rejected | applied | watching | healed
    #: | rolled_back | failed. `refused` is the ladder declining to put this in
    #: front of anybody; `rejected` is a person saying no.
    status:       Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    approved_by:  Mapped[str | None] = mapped_column(String(255), nullable=True)
    approved_at:  Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    branch_id:    Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    applied_at:   Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: The artifact and the EXACT bytes to write back. A rollback re-applies
    #: this through the same merge primitive the change used — one write path.
    rollback_artifact_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    rollback_artifact_id:   Mapped[str | None] = mapped_column(String(64), nullable=True)
    rollback_patch: Mapped[Any | None] = mapped_column(PortableJSON(), nullable=True)
    #: A change is not healed because it deployed. It is healed because the
    #: thing that was wrong stopped being wrong, and stayed that way.
    watch_until:  Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    watch_metric: Mapped[str | None] = mapped_column(String(64), nullable=True)
    watch_baseline: Mapped[Any | None] = mapped_column(PortableJSON(), nullable=True)
    watch_bound_pct: Mapped[float | None] = mapped_column(Numeric(6, 3), nullable=True)
    outcome:      Mapped[Any | None] = mapped_column(PortableJSON(), nullable=True)
    outcome_at:   Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rollback_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Somebody has to look at a process that healed itself and got worse.
    incident_case_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    #: The chain, built like F-01's ledger and F-03's proofs: an episode's
    #: address includes its predecessor, so a rewritten history breaks a hash
    #: somebody else already holds.
    sequence:     Mapped[int] = mapped_column(BigInteger, nullable=False)
    prev_hash:    Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by:   Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at:   Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
    updated_at:   Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow,
                                                   onupdate=_utcnow)


# ═══════════════════════════════════════════════════════════════════════
# F-09 — agent-to-agent case handoff (mig 192)
# ═══════════════════════════════════════════════════════════════════════


class HandoffCounterpartyModel(Base):
    """An organisation that may ever be handed a case. Not a session.

    Its identity is a public key an operator pinned OUT OF BAND — the way a SAML
    SP pins an IdP certificate — because a key that arrives with the message it
    authenticates proves only that the sender owns a key, which is not a fact
    about who they are. There is no discovery, no self-registration and no trust
    on first use: a handoff to a party nobody enrolled cannot be minted at all.
    """
    __tablename__ = "handoff_counterparties"
    __table_args__ = (
        UniqueConstraint("tenant_id", "slug", name="ux_handoff_cp_slug"),
        Index("ix_handoff_cp_key", "key_id"),
    )

    id:           Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:    Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    slug:         Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Raw Ed25519 public key, base64; `key_id` is its fingerprint and is what
    #: an operator checks against what they were told by another channel.
    public_key:   Mapped[str] = mapped_column(Text, nullable=False)
    key_id:       Mapped[str] = mapped_column(String(64), nullable=False)
    #: Suspending is the fast path: it stops new orders AND refuses every action
    #: on the ones outstanding, so an incident is not resolved order by order.
    status:       Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    #: What this organisation may EVER be granted, decided once by whoever
    #: enrolled them rather than repeatedly under time pressure.
    max_ceiling:  Mapped[str] = mapped_column(String(20), nullable=False, default="internal")
    notes:        Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by:   Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at:   Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
    updated_at:   Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow,
                                                   onupdate=_utcnow)


class HandoffWorkOrderModel(Base):
    """One case, one counterparty, one window. The order IS the authority.

    Not a session and not a login: a case-scoped, expiring, instantly revocable
    grant carried by an F-00 agent token, so revocation, the capability ceiling
    and the deployment kill switch are the ones already built rather than a
    second set that could disagree with them.

    `returned` (they handed it back) and `revoked` (we took it away) are
    different events, and a reader needs to know which happened.
    """
    __tablename__ = "handoff_work_orders"
    __table_args__ = (
        Index("ix_handoff_wo_case", "case_id", "created_at"),
        Index("ix_handoff_wo_tenant", "tenant_id", "status"),
    )

    id:              Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:       Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    case_id:         Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("case_instances.id", ondelete="CASCADE"), nullable=False)
    counterparty_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("handoff_counterparties.id", ondelete="CASCADE"), nullable=False)
    #: The F-00 agent principal, kind `external`. Every ledger row, policy
    #: decision and attribution goes through it, so an external agent is
    #: accounted for in exactly the places an internal one is.
    agent_id:        Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    #: none | internal | personal. `special_category` is NOT a value — Article 9
    #: data is never projected to an external party, and making that
    #: inexpressible is stronger than making it refusable.
    ceiling:         Mapped[str] = mapped_column(String(20), nullable=False, default="internal")
    allowed_actions: Mapped[Any] = mapped_column(PortableJSON(), nullable=False, default=list)
    obligations:     Mapped[Any | None] = mapped_column(PortableJSON(), nullable=True)
    status:          Mapped[str] = mapped_column(String(16), nullable=False, default="issued")
    expires_at:      Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Revoking this grant kills the order's authority instantly, everywhere,
    #: without this table being consulted.
    token_grant_id:  Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    #: Ours over the order, theirs over the acceptance. Together they are a
    #: cross-organisation trail neither side can rewrite alone.
    order_signature:      Mapped[str | None] = mapped_column(Text, nullable=True)
    acceptance_signature: Mapped[str | None] = mapped_column(Text, nullable=True)
    accepted_at:     Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    returned_at:     Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    return_summary:  Mapped[str | None] = mapped_column(Text, nullable=True)
    revoked_at:      Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason:  Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by:      Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at:      Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)


class HandoffActionModel(Base):
    """Every action an external party took, or was refused. Append-only.

    `projection` records WHAT WAS ACTUALLY DISCLOSED on a read — the field
    names, never their values — because "what did they see" is the question a
    data-protection incident turns on, and reconstructing it later from a
    ceiling and a case that has since changed is guesswork.

    A DENIAL is a row too: a counterparty repeatedly reaching for something it
    may not have is a signal, and one that leaves no trace is a signal nobody
    receives.
    """
    __tablename__ = "handoff_actions"
    __table_args__ = (
        Index("ix_handoff_act_order", "work_order_id", "created_at"),
    )

    id:            Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    work_order_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("handoff_work_orders.id", ondelete="CASCADE"), nullable=False)
    #: accept | read | propose | return. Closed, and there is deliberately no
    #: verb that writes to a case.
    kind:          Mapped[str] = mapped_column(String(16), nullable=False)
    decision:      Mapped[str] = mapped_column(String(10), nullable=False)
    reason:        Mapped[str | None] = mapped_column(Text, nullable=True)
    projection:    Mapped[Any | None] = mapped_column(PortableJSON(), nullable=True)
    payload_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    proposal_id:   Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    ledger_record_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    created_at:    Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)


# ═══════════════════════════════════════════════════════════════════════
# F-10 — offline-sovereign field operations, the HUB half (mig 193)
# ═══════════════════════════════════════════════════════════════════════


class FieldNodeModel(Base):
    """A node that may hold work while it is out of contact.

    **The threat model is device loss**, and three columns exist only because of
    it. `charter` bounds what the node may EVER hold — a stolen node cannot
    contain what was never dispatched to it, which is a stronger statement than
    any amount of encryption on a device somebody else has. `ceiling` is F-09's
    data-class projection: from the hub's point of view a field node is exactly
    as untrusted as an external agent. `snapshot_max_age_hours` makes it fail
    CLOSED when its policy snapshot goes stale, because the only safe direction
    is deny when the thing that would tell you otherwise is unreachable.

    `shred_ordered` is executed at next contact; a node that never returns is
    handled by the staleness lockout instead. The two together are what makes a
    lost device survivable.
    """
    __tablename__ = "field_nodes"
    __table_args__ = (
        UniqueConstraint("tenant_id", "slug", name="ux_field_node_slug"),
    )

    id:           Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:    Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    slug:         Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Pinned exactly as an F-09 counterparty's is: a key arriving with the sync
    #: it authenticates proves only that the sender owns a key.
    public_key:   Mapped[str] = mapped_column(Text, nullable=False)
    key_id:       Mapped[str] = mapped_column(String(64), nullable=False)
    #: active | suspended | shred_ordered | shredded
    status:       Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    charter:      Mapped[Any] = mapped_column(PortableJSON(), nullable=False, default=dict)
    ceiling:      Mapped[str] = mapped_column(String(20), nullable=False, default="internal")
    snapshot_max_age_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=72)
    #: The sub-chain head the hub last verified. A node resuming from anything
    #: else is either replaying or has lost work, and both need a person.
    subchain_head: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_contact_at:  Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    shred_ordered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    shred_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    shredded_at:  Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by:   Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at:   Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
    updated_at:   Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow,
                                                   onupdate=_utcnow)


class FieldDispatchModel(Base):
    """What a node was handed, and what was deliberately withheld.

    This is the record that answers "what was on that device" after it has gone
    missing. It keeps the case ids and the FIELD NAMES disclosed — never the
    values, which are on the cases themselves.

    The policy snapshot is PINNED rather than fetched, because the entire point
    is that the node cannot reach us; `stale_after` is when it must stop acting
    on it.
    """
    __tablename__ = "field_dispatches"
    __table_args__ = (
        Index("ix_field_disp_node", "node_id", "issued_at"),
    )

    id:        Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    node_id:   Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("field_nodes.id", ondelete="CASCADE"), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    case_ids:  Mapped[Any] = mapped_column(PortableJSON(), nullable=False, default=list)
    projection: Mapped[Any | None] = mapped_column(PortableJSON(), nullable=True)
    policy_snapshot: Mapped[Any | None] = mapped_column(PortableJSON(), nullable=True)
    policy_snapshot_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: The namespaces that must never run disconnected. The node applies these;
    #: the hub records what it told it.
    offline_deny: Mapped[Any] = mapped_column(PortableJSON(), nullable=False, default=list)
    #: Everything the node records while away hashes back to this, so a stream
    #: that does not start here is not this dispatch's.
    subchain_genesis: Mapped[str] = mapped_column(String(64), nullable=False)
    status:    Mapped[str] = mapped_column(String(16), nullable=False, default="issued")
    issued_at: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
    stale_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)


class FieldSyncEventModel(Base):
    """One thing a node did while it was away.

    Append-only on both sides. The node keeps a hash chain; the hub verifies it
    on arrival, so tampering with a field log breaks a hash the hub already
    holds.

    `merge_state` is where "deterministic conflict outcomes" and "a human
    reconciliation queue for the irreducible remainder" both live: `applied`
    merged cleanly, `conflict` needs a person, `rejected` could not be accepted
    at all — a broken chain, or a case outside the charter.
    """
    __tablename__ = "field_sync_events"
    __table_args__ = (
        Index("ix_field_ev_node", "node_id", "seq"),
        Index("ix_field_ev_queue", "tenant_id", "merge_state"),
        Index("ix_field_ev_case", "case_id", "received_at"),
    )

    id:          Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    node_id:     Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("field_nodes.id", ondelete="CASCADE"), nullable=False)
    dispatch_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("field_dispatches.id", ondelete="SET NULL"), nullable=True)
    tenant_id:   Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    case_id:     Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    #: The node's own ordering. A gap is a lost stream, not a late one.
    seq:         Mapped[int] = mapped_column(Integer, nullable=False)
    #: variable_set | note | step_complete. Closed, and deliberately small: a
    #: node may record what a worker did to a CASE, never what an administrator
    #: did to the platform.
    kind:        Mapped[str] = mapped_column(String(24), nullable=False)
    target:      Mapped[str | None] = mapped_column(String(255), nullable=True)
    payload:     Mapped[Any | None] = mapped_column(PortableJSON(), nullable=True)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    #: When the NODE thinks it happened. Never trusted for ordering against hub
    #: events — a disconnected clock is exactly what drifts — but kept, because
    #: it is what a field worker will describe.
    node_time:   Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    prev_hash:   Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    merge_state: Mapped[str] = mapped_column(String(16), nullable=False, default="applied")
    conflict_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: keep_field | keep_hub. Nothing decides itself when a `personal` data
    #: class is involved.
    resolution:  Mapped[str | None] = mapped_column(String(16), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_at: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)


# ═══════════════════════════════════════════════════════════════════════
# F-11 — intent-based process execution (mig 194)
# ═══════════════════════════════════════════════════════════════════════


class ProcessFragmentModel(Base):
    """A verified building block a plan may be composed from.

    **The declarations are the planner's entire vocabulary.** It matches
    `provides` to `requires` and has no opinion about what a fragment means —
    only about what it declared. A fragment is composed on the strength of its
    declaration, and the conformance gate is what makes the declaration and the
    behaviour agree.

    Only `verified` fragments compose. A `draft` is one somebody is still
    writing; a `retired` one stays readable because plans that used it must
    remain explainable.
    """
    __tablename__ = "process_fragments"
    __table_args__ = (
        UniqueConstraint("tenant_id", "slug", name="ux_fragment_slug"),
        Index("ix_fragment_status", "tenant_id", "status"),
    )

    id:           Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:    Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    slug:         Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    description:  Mapped[str] = mapped_column(Text, nullable=False, default="")
    requires:     Mapped[Any] = mapped_column(PortableJSON(), nullable=False, default=list)
    provides:     Mapped[Any] = mapped_column(PortableJSON(), nullable=False, default=list)
    #: Class level only — never a field name.
    data_classes: Mapped[Any] = mapped_column(PortableJSON(), nullable=False, default=list)
    #: A capability absent here is one the fragment may not use, whatever its
    #: steps look like.
    capabilities: Mapped[Any] = mapped_column(PortableJSON(), nullable=False, default=list)
    #: Ordinary `definition_json` stages — a composed process runs on the
    #: existing interpreter unchanged, which is the point.
    stages:       Mapped[Any] = mapped_column(PortableJSON(), nullable=False, default=list)
    step_count:   Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    signature:    Mapped[str | None] = mapped_column(Text, nullable=True)
    signing_key_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status:       Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    #: Whether the declared capabilities and data classes actually cover what
    #: the stages do.
    conformance:  Mapped[Any | None] = mapped_column(PortableJSON(), nullable=True)
    verified_at:  Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verified_by:  Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by:   Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at:   Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
    updated_at:   Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow,
                                                   onupdate=_utcnow)


class IntentPlanModel(Base):
    """One intent, planned — and the consent object for what it composed.

    A person reads the step list and confirms it, and `confirmed_sha256` binds
    that confirmation to the exact composed definition. Executing anything whose
    hash differs is refused: without the binding, "the user approved a plan"
    would mean only that they approved *a* plan.

    `intent` and `goal_spec` are both kept. The goal spec is the only thing the
    planner saw and the intent is the only thing the person said, and if the two
    diverge somebody needs to be able to see that they did.
    """
    __tablename__ = "intent_plans"
    __table_args__ = (
        Index("ix_intent_tenant", "tenant_id", "status"),
        Index("ix_intent_case", "case_id", "created_at"),
    )

    id:        Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    case_id:   Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("case_instances.id", ondelete="SET NULL"), nullable=True)
    intent:    Mapped[str] = mapped_column(Text, nullable=False)
    goal_spec: Mapped[Any] = mapped_column(PortableJSON(), nullable=False, default=dict)
    #: `model` or `human`. The planner does not care; a reviewer does.
    spec_source: Mapped[str] = mapped_column(String(16), nullable=False, default="model")
    fragment_slugs: Mapped[Any] = mapped_column(PortableJSON(), nullable=False, default=list)
    definition: Mapped[Any | None] = mapped_column(PortableJSON(), nullable=True)
    #: Computed unions, never asserted.
    capabilities: Mapped[Any] = mapped_column(PortableJSON(), nullable=False, default=list)
    data_classes: Mapped[Any] = mapped_column(PortableJSON(), nullable=False, default=list)
    step_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    plan_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ladder:    Mapped[Any] = mapped_column(PortableJSON(), nullable=False, default=list)
    stopped_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    stopped_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    twin_run_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    status:    Mapped[str] = mapped_column(String(16), nullable=False, default="planned")
    confirmed_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Kept separately from `plan_sha256`: if the two ever differ the plan was
    #: changed after a person agreed to it, and that is a fact worth being able
    #: to state rather than infer.
    confirmed_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)


# ═══════════════════════════════════════════════════════════════════════
# F-12 — process-as-negotiation (mig 195)
# ═══════════════════════════════════════════════════════════════════════


class JointContractModel(Base):
    """A shared workflow two organisations agreed, and both signed.

    **No trust is required, only verification.** The matcher is deterministic
    and runs locally on both sides from the same inputs, so each party can
    independently confirm the joint composition is the one that was agreed —
    no trusted third party, no shared database, only two local processes and a
    message contract at the seam that F-09's handoff protocol carries.

    **Deny-overrides survives negotiation by construction.**
    `effective_ceiling` is stored as the MINIMUM of what was negotiated and what
    this side's own boundary permits. A contract cannot grant what the platform
    denies — not because a check runs at the right moment, but because the wider
    number is never written down.

    Renegotiation is a NEW row (`version`, `supersedes`), never an edit: what
    both sides signed must stay exactly as it was signed.
    """
    __tablename__ = "joint_contracts"
    __table_args__ = (
        Index("ix_contract_tenant", "tenant_id", "status"),
        Index("ix_contract_party", "counterparty_id", "version"),
    )

    id:              Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:       Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    #: From F-09's registry. A contract with somebody nobody enrolled cannot
    #: exist: the pinned key is what both signatures are checked against.
    counterparty_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("handoff_counterparties.id", ondelete="CASCADE"), nullable=False)
    name:            Mapped[str] = mapped_column(String(255), nullable=False)
    version:         Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    supersedes:      Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("joint_contracts.id", ondelete="SET NULL"), nullable=True)
    our_offer:       Mapped[Any] = mapped_column(PortableJSON(), nullable=False, default=dict)
    their_offer:     Mapped[Any] = mapped_column(PortableJSON(), nullable=False, default=dict)
    our_requirements:   Mapped[Any] = mapped_column(PortableJSON(), nullable=False, default=dict)
    their_requirements: Mapped[Any] = mapped_column(PortableJSON(), nullable=False, default=dict)
    composition:     Mapped[Any | None] = mapped_column(PortableJSON(), nullable=True)
    #: Every clause the matcher checked and how it resolved. Both sides compute
    #: this independently from the same inputs and must get the same answer.
    resolution:      Mapped[Any | None] = mapped_column(PortableJSON(), nullable=True)
    effective_ceiling: Mapped[str | None] = mapped_column(String(20), nullable=True)
    #: Over the composition and the resolved terms, not the row: a hash over the
    #: row would still match if somebody edited the composition and left the
    #: bookkeeping alone.
    contract_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    our_signature:   Mapped[str | None] = mapped_column(Text, nullable=True)
    their_signature: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: draft | matched | unmatchable | approved | shadow | enforced | suspended
    #: | withdrawn. `unmatchable` is not a failure of either side — the two sets
    #: of requirements simply have no joint composition that satisfies both.
    status:          Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    unmatched_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_by:     Mapped[str | None] = mapped_column(String(255), nullable=True)
    approved_at:     Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    enforced_at:     Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Either side may suspend unilaterally. It fail-closes the seam to
    #: proposals-queued rather than data-flowing, so it is safe to press at
    #: 3 a.m. without knowing what is in flight.
    suspended_at:    Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    suspended_by:    Mapped[str | None] = mapped_column(String(255), nullable=True)
    suspended_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by:      Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at:      Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
    updated_at:      Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow,
                                                      onupdate=_utcnow)


class ContractAnchorModel(Base):
    """One side's chain head, recorded by the other.

    Each side periodically records the OTHER's head and hands over its own.
    Neither can rewrite the joint history afterwards without breaking a hash the
    other is holding — tamper-evidence over a shared story **without a shared
    ledger and without a third party**.

    Repudiation becomes symmetric: it is not that we can prove what they did, it
    is that neither of us can deny it.
    """
    __tablename__ = "contract_anchors"
    __table_args__ = (
        Index("ix_anchor_contract", "contract_id", "direction", "seq"),
    )

    id:          Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    contract_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("joint_contracts.id", ondelete="CASCADE"), nullable=False)
    tenant_id:   Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    #: ours | theirs
    direction:   Mapped[str] = mapped_column(String(8), nullable=False)
    seq:         Mapped[int] = mapped_column(Integer, nullable=False)
    chain_head:  Mapped[str] = mapped_column(String(64), nullable=False)
    signature:   Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Set when a head we were handed does not extend the one we last held. A
    #: rewritten history is what this exchange exists to detect, so the
    #: detection is a row rather than a log line.
    divergence:  Mapped[str | None] = mapped_column(Text, nullable=True)
    anchored_at: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)


# ═══════════════════════════════════════════════════════════════════════
# Item 12 — Customer Engagement: the identity spine (mig 196)
# ═══════════════════════════════════════════════════════════════════════


class CustomerProfileModel(Base):
    """A PARTY — a person, a household or an organisation.

    Not a person, and that is decision 1 of four taken with the user on
    2026-09-04: a human who is a contact on two businesses, or a household with
    three members, works without a migration on the spine everything else hangs
    off. `customer_relations` carries the edges.

    **Nothing about the spine is ever external.** `source_of_truth` says who
    masters the ATTRIBUTES — a name, an address — and it may be a CRM. Which
    cases, threads and orders belong to one party is a Velaris fact, and no CRM
    has an opinion about it.

    A merge is a record, never a deletion: the loser stays readable and
    redirects, so an audit can answer "why does this call show that person's
    cases". `anonymized` is a GDPR erasure that keeps the row, because a deleted
    profile would silently detach a call from the case it was about.
    """
    __tablename__ = "customer_profiles"
    __table_args__ = (
        Index("ix_cprofile_tenant", "tenant_id", "status"),
        Index("ix_cprofile_merged", "merged_into_id"),
    )

    id:            Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:     Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    #: person | household | organisation
    party_kind:    Mapped[str] = mapped_column(String(16), nullable=False, default="person")
    display_name:  Mapped[str] = mapped_column(String(255), nullable=False)
    #: active | merged | anonymized
    status:        Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("customer_profiles.id", ondelete="SET NULL"), nullable=True)
    #: velaris | crm_mastered | read_through — per PROFILE, because a contact
    #: centre normally has both kinds.
    source_of_truth: Mapped[str] = mapped_column(String(16), nullable=False, default="velaris")
    external_system: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Per-field provenance and fetch time. A rep reading a name is entitled to
    #: know whether it came from the CRM or from the last conversation, and
    #: **staleness is shown, never hidden**.
    attribute_source: Mapped[Any | None] = mapped_column(PortableJSON(), nullable=True)
    #: NULL on a Velaris-owned profile — and a NULL must never read as "fresh".
    refreshed_at:  Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source:        Mapped[str] = mapped_column(String(16), nullable=False, default="agent")
    notes:         Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by:    Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at:    Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
    updated_at:    Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow,
                                                    onupdate=_utcnow)


class CustomerIdentifierModel(Base):
    """How the world names this party — and the index a screen-pop needs.

    Today `channels/cti/pop` reads the last 200 conversations hunting for a
    number. This is the row that replaces that scan with a lookup.

    Normalisation is the whole value of the table: two reps typing the same
    number differently must reach the same person. And **resolution is a
    suggestion until a human or a challenge confirms it** — an identifier adopted
    from an inbound number is `verified = False`, and no disclosure tier reads an
    unverified identifier as proof.
    """
    __tablename__ = "customer_identifiers"
    __table_args__ = (
        UniqueConstraint("tenant_id", "kind", "value_normalised", name="ux_cident_value"),
        Index("ix_cident_customer", "customer_id", "kind"),
    )

    id:            Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:     Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    customer_id:   Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("customer_profiles.id", ondelete="CASCADE"), nullable=False)
    #: email | phone | portal_customer | external_crm | national_id_hash | account_no
    kind:          Mapped[str] = mapped_column(String(24), nullable=False)
    value_normalised: Mapped[str] = mapped_column(String(255), nullable=False)
    value_display: Mapped[str] = mapped_column(String(255), nullable=False)
    verified:      Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    verified_at:   Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: otp | call | document | idp
    verified_by:   Mapped[str | None] = mapped_column(String(16), nullable=True)
    is_primary:    Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source:        Mapped[str] = mapped_column(String(16), nullable=False, default="agent")
    created_at:    Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)


class CustomerRelationModel(Base):
    """Party ↔ party: a household's members, a business's contacts.

    Directional, because "is a contact of" and "has as a contact" are different
    statements and collapsing them loses which way the authority runs.
    `may_act_for` is recorded rather than inferred from the relation name: a
    household member is not automatically allowed to speak for the household.
    """
    __tablename__ = "customer_relations"
    __table_args__ = (
        UniqueConstraint("from_customer_id", "to_customer_id", "relation", name="ux_crel_edge"),
        Index("ix_crel_to", "to_customer_id"),
    )

    id:          Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:   Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    from_customer_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("customer_profiles.id", ondelete="CASCADE"), nullable=False)
    to_customer_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("customer_profiles.id", ondelete="CASCADE"), nullable=False)
    #: member_of | contact_for | authorised_by | related_to
    relation:    Mapped[str] = mapped_column(String(24), nullable=False)
    may_act_for: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    note:        Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by:  Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at:  Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)


class CustomerLinkModel(Base):
    """The party's footprint, without touching the objects.

    FK-free on the object side ON PURPOSE. A case, a conversation, an order and
    a document live in different tables with different lifecycles; adding a
    customer column to each would be five migrations on live tables plus five
    places to keep in step.

    `link_reason` matters: a link a machine guessed and a link a person made are
    different evidence, and the 360 says which.
    """
    __tablename__ = "customer_links"
    __table_args__ = (
        UniqueConstraint("customer_id", "object_kind", "object_id", name="ux_clink_object"),
        Index("ix_clink_lookup", "tenant_id", "object_kind", "object_id"),
    )

    id:          Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:   Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("customer_profiles.id", ondelete="CASCADE"), nullable=False)
    #: case | conversation | order | document | session | interaction
    object_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    object_id:   Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    #: resolved | manual | on_behalf
    link_reason: Mapped[str] = mapped_column(String(16), nullable=False, default="resolved")
    linked_by:   Mapped[str | None] = mapped_column(String(255), nullable=True)
    linked_at:   Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)


class CustomerMergeModel(Base):
    """A merge, reversible. `moved` is what an undo puts back."""
    __tablename__ = "customer_merges"
    __table_args__ = (
        Index("ix_cmerge_winner", "winner_id", "merged_at"),
    )

    id:        Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    winner_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("customer_profiles.id", ondelete="CASCADE"), nullable=False)
    loser_id:  Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("customer_profiles.id", ondelete="CASCADE"), nullable=False)
    moved:     Mapped[Any | None] = mapped_column(PortableJSON(), nullable=True)
    reason:    Mapped[str] = mapped_column(Text, nullable=False)
    merged_by: Mapped[str] = mapped_column(String(255), nullable=False)
    merged_at: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
    undone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    undone_by: Mapped[str | None] = mapped_column(String(255), nullable=True)


class CustomerInteractionModel(Base):
    """What a call IS — the piece Velaris has never had.

    Not a case and not a conversation: the contact itself. It makes handling
    time, wrap-up codes, "what did we promise last time" and repeat-contact
    analytics possible, and it is the row a screen-pop creates.

    `customer_id` is NULLABLE and that is important: an unresolved call is still
    an interaction. **Never auto-create a profile from an unverified inbound
    number** — that manufactures a person out of a wrong number.

    `verification_level` lives HERE, not on the profile: verification is
    something that happened on a call, not a property somebody acquires
    permanently. And a warm transfer carries the interaction, so the receiving
    rep inherits the tier — being re-verified is the number one complaint about
    every contact centre.
    """
    __tablename__ = "customer_interactions"
    __table_args__ = (
        Index("ix_cint_customer", "customer_id", "started_at"),
        Index("ix_cint_tenant", "tenant_id", "status", "started_at"),
        Index("ix_cint_agent", "agent_user_id", "started_at"),
    )

    id:            Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:     Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    customer_id:   Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("customer_profiles.id", ondelete="SET NULL"), nullable=True)
    channel:       Mapped[str] = mapped_column(String(16), nullable=False)
    direction:     Mapped[str] = mapped_column(String(8), nullable=False, default="inbound")
    started_at:    Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
    ended_at:      Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    agent_user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    external_call_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ani:           Mapped[str | None] = mapped_column(String(64), nullable=True)
    dnis:          Mapped[str | None] = mapped_column(String(64), nullable=True)
    queue:         Mapped[str | None] = mapped_column(String(128), nullable=True)
    transferred_from: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("customer_interactions.id", ondelete="SET NULL"), nullable=True)
    #: none | partial | full
    verification_level: Mapped[str] = mapped_column(String(8), nullable=False, default="none")
    verified_by:   Mapped[str | None] = mapped_column(String(16), nullable=True)
    verified_at:   Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disposition_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    wrap_up_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: open | wrapped | abandoned. An unclosed interaction AGES into `abandoned`;
    #: a wrap-up that blocks the next call is a wrap-up that gets faked.
    status:        Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    outcome:       Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at:    Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
    #: mig 207. How this row came to be when it is the receiving half of a
    #: handoff: transfer | warm | cold. NULL for an ordinary contact.
    handoff_kind:  Mapped[str | None] = mapped_column(String(8), nullable=True)


class CustomerInteractionJoinModel(Base):
    """A join link for the next rep on a live contact (mig 207, Oct item 1).

    Only the token's SHA-256 is stored — the link is a credential. Single-use
    (`redeemed_at`), short-lived (`expires_at`), revocable, bound to the tenant
    and to a named rep or a queue.
    """
    __tablename__ = "customer_interaction_joins"
    __table_args__ = (
        Index("ix_cij_interaction", "interaction_id", "created_at"),
        Index("ix_cij_tenant", "tenant_id", "created_at"),
    )

    id:             Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:      Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    interaction_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("customer_interactions.id", ondelete="CASCADE"), nullable=False)
    token_hash:     Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    #: warm | cold
    mode:           Mapped[str] = mapped_column(String(8), nullable=False)
    for_user_id:    Mapped[str | None] = mapped_column(String(255), nullable=True)
    for_queue:      Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_by:     Mapped[str] = mapped_column(String(255), nullable=False)
    created_at:     Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
    expires_at:     Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    redeemed_at:    Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    redeemed_by:    Mapped[str | None] = mapped_column(String(255), nullable=True)
    joined_interaction_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("customer_interactions.id", ondelete="SET NULL"), nullable=True)
    revoked_at:     Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by:     Mapped[str | None] = mapped_column(String(255), nullable=True)


class CustomerBackfillRunModel(Base):
    """One operator-started backfill scan (mig 208, Oct item 3)."""
    __tablename__ = "customer_backfill_runs"
    __table_args__ = (Index("ix_cbr_tenant", "tenant_id", "started_at"),)

    id:          Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:   Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    started_by:  Mapped[str] = mapped_column(String(255), nullable=False)
    started_at:  Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    counts:      Mapped[dict] = mapped_column(PortableJSON(), nullable=False, default=dict)


class CustomerBackfillProposalModel(Base):
    """A proposed profile, enrichment or merge — accepted or rejected by a person.

    Nothing here is ever applied by the scan itself (design §11). `fingerprint`
    is stable across runs, so a re-run refreshes pending proposals and never
    re-proposes a rejected one.
    """
    __tablename__ = "customer_backfill_proposals"
    __table_args__ = (
        UniqueConstraint("tenant_id", "fingerprint", name="uq_cbp_fingerprint"),
        Index("ix_cbp_tenant_status", "tenant_id", "status", "kind"),
    )

    id:          Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:   Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    run_id:      Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("customer_backfill_runs.id", ondelete="SET NULL"), nullable=True)
    #: create_profile | enrich_profile | merge_profiles
    kind:        Mapped[str] = mapped_column(String(16), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    payload:     Mapped[dict] = mapped_column(PortableJSON(), nullable=False, default=dict)
    evidence:    Mapped[dict] = mapped_column(PortableJSON(), nullable=False, default=dict)
    #: proposed | accepted | rejected | superseded
    status:      Mapped[str] = mapped_column(String(16), nullable=False, default="proposed")
    created_at:  Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
    updated_at:  Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
    decided_by:  Mapped[str | None] = mapped_column(String(255), nullable=True)
    decided_at:  Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason:      Mapped[str | None] = mapped_column(Text, nullable=True)
    result:      Mapped[dict | None] = mapped_column(PortableJSON(), nullable=True)


class EngageRailConfigModel(Base):
    """An industry action rail for the rep desk (mig 209, Oct item 5).

    Orders, hides and relabels the rail's actions, with guidance and closed
    defaults; may raise a tier, never lower one. `edited_at` marks a pack rail
    the tenant changed — a pack upgrade keeps it.
    """
    __tablename__ = "engage_rail_configs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "rail_key", name="uq_erc_key"),
        Index("ix_erc_tenant", "tenant_id", "enabled"),
    )

    id:           Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:    Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    rail_key:     Mapped[str] = mapped_column(String(64), nullable=False)
    name:         Mapped[str] = mapped_column(String(120), nullable=False)
    industry:     Mapped[str] = mapped_column(String(60), nullable=False, default="general")
    #: pack | tenant
    source:       Mapped[str] = mapped_column(String(8), nullable=False)
    package_id:   Mapped[str | None] = mapped_column(String(255), nullable=True)
    pack_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    config:       Mapped[dict] = mapped_column(PortableJSON(), nullable=False, default=dict)
    enabled:      Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    edited_at:    Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    edited_by:    Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at:   Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
    updated_at:   Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)


class CaseTenantTransferModel(Base):
    """A case moving between tenants, and the source's tombstone of it (mig 205).

    Requested by one source admin, approved by a different one, accepted by the
    destination — nothing moves before all three. After completion this row is
    all the source keeps. `active_key` is the portable partial unique index: the
    case id while requested/pending, NULL after.
    """
    __tablename__ = "case_tenant_transfers"
    __table_args__ = (
        Index("ux_case_tenant_transfers_active", "active_key", unique=True),
        Index("ix_case_tenant_transfers_source", "source_tenant_id", "state", "requested_at"),
        Index("ix_case_tenant_transfers_destination", "destination_tenant_id", "state",
              "requested_at"),
        Index("ix_case_tenant_transfers_case", "case_id"),
    )

    id:                    Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    case_id:               Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    case_number:           Mapped[str | None] = mapped_column(String(30), nullable=True)
    case_type_name:        Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_tenant_id:      Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    destination_tenant_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    #: requested | pending | completed | refused | cancelled
    state:                 Mapped[str] = mapped_column(String(16), nullable=False, default="requested")
    active_key:            Mapped[str | None] = mapped_column(String(36), nullable=True)
    reason:                Mapped[str] = mapped_column(Text, nullable=False)
    requested_by:          Mapped[str] = mapped_column(String(255), nullable=False)
    requested_at:          Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
    approved_by:           Mapped[str | None] = mapped_column(String(255), nullable=True)
    approved_at:           Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by:            Mapped[str | None] = mapped_column(String(255), nullable=True)
    decided_at:            Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_note:         Mapped[str | None] = mapped_column(Text, nullable=True)
    cancelled_by:          Mapped[str | None] = mapped_column(String(255), nullable=True)
    manifest:              Mapped[dict | None] = mapped_column(PortableJSON(), nullable=True)
    completed_at:          Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CustomerNextActionFeedbackModel(Base):
    """What a rep decided about a suggested next action (mig 204, item 12 P6).

    Appended, never updated — "accepted, then rejected on the next call" is the
    sequence a rule-tuning report exists to show. `action_key` is the rule's
    stable key; the rules are code, so the key is the join to what was said.
    """
    __tablename__ = "customer_next_action_feedback"
    __table_args__ = (
        Index("ix_cnaf_tenant_key", "tenant_id", "action_key", "decided_at"),
        Index("ix_cnaf_customer", "customer_id", "decided_at"),
    )

    id:             Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:      Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    customer_id:    Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("customer_profiles.id", ondelete="CASCADE"), nullable=False)
    interaction_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("customer_interactions.id", ondelete="SET NULL"), nullable=True)
    action_key:     Mapped[str] = mapped_column(String(64), nullable=False)
    #: accepted | rejected
    verdict:        Mapped[str] = mapped_column(String(16), nullable=False)
    reason:         Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by:     Mapped[str] = mapped_column(String(255), nullable=False)
    decided_at:     Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)


class CustomerDisclosureModel(Base):
    """Every field a rep unmasked, and why.

    **Reveal-on-reason**: one field at a time, each with a reason, each a row.
    Bulk unmask does not exist — a "show everything" button is how a disclosure
    tier becomes decoration.

    `at_verification` is frozen. Reading the tier live later would show what the
    caller eventually reached, not what they had reached when the field was
    shown.
    """
    __tablename__ = "customer_disclosures"
    __table_args__ = (
        Index("ix_cdisc_customer", "customer_id", "revealed_at"),
        Index("ix_cdisc_agent", "revealed_by", "revealed_at"),
    )

    id:            Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:     Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    customer_id:   Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("customer_profiles.id", ondelete="CASCADE"), nullable=False)
    interaction_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("customer_interactions.id", ondelete="SET NULL"), nullable=True)
    field_path:    Mapped[str] = mapped_column(String(128), nullable=False)
    reason:        Mapped[str] = mapped_column(Text, nullable=False)
    at_verification: Mapped[str] = mapped_column(String(8), nullable=False)
    revealed_by:   Mapped[str] = mapped_column(String(255), nullable=False)
    revealed_at:   Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)


# >>> 198 — case statuses as data, case actions as one dispatcher
#
# Design: docs/Future/Done/CASE-ACTIONS-STATUS-DESIGN.md
#
# The split these two tables encode: a STATUS is a label the customer's process
# gives meaning to, so it lives in data and an operator may invent one. An
# ACTION is behaviour — `escalate` fires a tree and notifies, which cannot be
# inferred from the word — so the behaviour stays a closed registry in code and
# only its presentation and reach are configured here.


class CaseStatusModel(Base):
    """One status in a resolved status set.

    Resolution is most-specific-first: `(tenant, case_type)` → `(tenant, NULL)`
    → `(NULL, NULL)`, the same precedence retention policies and portal display
    config use. The platform-default set (both NULL) is what a single-tenant
    install runs on.

    `case_instances.status` deliberately holds a plain string rather than a
    foreign key to this table: a per-tenant status set has to stay editable
    after it has been used, and an FK would make renaming or removing a status
    impossible on exactly the installs that need it most. Validation happens at
    the write path instead, where it can name the legal transitions.
    """
    __tablename__ = "case_statuses"
    __table_args__ = (
        UniqueConstraint("scope_key", "name", name="ux_case_statuses_scope"),
        Index("idx_case_statuses_lookup", "tenant_id", "case_type_id", "enabled"),
    )

    id:           Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:    Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True)
    case_type_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("case_types.id", ondelete="CASCADE"), nullable=True)

    #: `<tenant_id|platform>:<case_type_id|any>`, written by the application.
    #: NULL <> NULL, so a plain UNIQUE over the two nullable ids would permit
    #: unlimited duplicate platform rows for `closed` — and then "is closed
    #: terminal" depends on which row was read first. Not GENERATED because
    #: MariaDB 11.8 refuses COALESCE in that clause (error 1901) where MySQL 8.4
    #: accepts it; migration 177 met this first.
    scope_key:    Mapped[str] = mapped_column(String(80), nullable=False)

    #: The value actually stored in `case_instances.status`.
    name:         Mapped[str] = mapped_column(String(30), nullable=False)
    #: What an operator sees. Separate from `name` so it can be renamed — or
    #: translated — without rewriting every stored row.
    label:        Mapped[str] = mapped_column(String(100), nullable=False)

    #: open | working | waiting | terminal. Lets code reason about KINDS of
    #: status without knowing a customer's names, so a rule written against it
    #: survives a tenant renaming `pending` to `awaiting_customer`.
    category:     Mapped[str] = mapped_column(String(16), nullable=False, default="open")

    #: Whether reaching this ends the case. First-class rather than derived from
    #: `category`, because deriving it is the indirection that let the two
    #: hardcoded terminal tuples drift away from the data.
    is_terminal:  Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_default:   Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    #: Status names a case may arrive here FROM. Empty (or NULL on MySQL) means
    #: "from any status" — the permissive default every seeded row ships with,
    #: so migration 198 breaks no transition that works today.
    allowed_from: Mapped[list] = mapped_column(PortableJSON(), default=list)

    sort_order:   Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: A disabled status is not offered for NEW transitions but is still
    #: recognised on rows already holding it — deleting a status a case sits on
    #: would make that case unreadable.
    enabled:      Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at:   Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
    updated_at:   Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class CaseActionConfigModel(Base):
    """What a customer may change about an action — and nothing more.

    The behaviour of each action is a closed registry in code
    (`case_actions/registry.py`). A row here naming an action outside that
    registry is refused at write time; ignoring it at read time would leave an
    operator staring at a configuration that silently does nothing.
    """
    __tablename__ = "case_action_config"
    __table_args__ = (
        UniqueConstraint("scope_key", "action", name="ux_case_action_config_scope"),
        Index("idx_case_action_config_lookup", "tenant_id", "case_type_id", "enabled"),
    )

    id:           Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:    Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True)
    case_type_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("case_types.id", ondelete="CASCADE"), nullable=True)
    scope_key:    Mapped[str] = mapped_column(String(80), nullable=False)

    action:       Mapped[str] = mapped_column(String(40), nullable=False)

    #: NULL = use the registry's own label.
    label:        Mapped[str | None] = mapped_column(String(100), nullable=True)
    #: False makes the action unreachable through the DISPATCHER, not merely
    #: hidden in the UI. A switch that only hides is decoration.
    enabled:      Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order:   Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: Overrides the registry's default privilege. Must be a known HxGuard
    #: action; validated on write.
    privilege:    Mapped[str | None] = mapped_column(String(80), nullable=True)
    #: Status names this action is offered FROM. Empty = any. This is the
    #: customer's half of the state machine: the platform says what `close`
    #: does, the customer says when it may be done.
    allowed_from: Mapped[list] = mapped_column(PortableJSON(), default=list)

    created_at:   Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
    updated_at:   Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

# <<< 198


# >>> 200 — audit streaming to a SIEM (market gap A3)
class AuditStreamTargetModel(Base):
    """Where this platform's security events are shipped (mig 200).

    **A reader, not a hook.** The forwarder reads `security_events` after the
    rows are durable, by a cursor held on this row. Nothing is written into
    `log_security_event`'s path — that function is called from inside login and
    inside every authorization denial, and a network call there would make a
    SIEM outage a platform outage, which is the exact failure a SOC exists to
    catch.

    **At-least-once.** The cursor advances only after a batch is accepted. A
    duplicate event in a SIEM is an afternoon's deduplication; a missing one is
    an incident nobody sees.

    **Off until two separate acts.** A target is created, and then enabled.
    """

    __tablename__ = "audit_stream_targets"
    __table_args__ = (
        Index("ux_audit_stream_targets_name", "tenant_key", "name", unique=True),
        Index("idx_audit_stream_targets_enabled", "enabled"),
    )

    id:         Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    #: NULL = platform-wide: it receives every tenant's events, which is what a
    #: single-tenant install wants and what a shared box must opt into.
    tenant_id:  Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: `tenant_id` or the literal 'platform'. Written by the application on both
    #: dialects — MariaDB refuses COALESCE in a generated column — and it exists
    #: because NULL <> NULL makes a UNIQUE over a nullable tenant no constraint
    #: at all on the platform rows.
    tenant_key: Mapped[str] = mapped_column(String(255), nullable=False)

    name:       Mapped[str] = mapped_column(String(255), nullable=False)
    kind:       Mapped[str] = mapped_column(String(50), nullable=False, default="splunk_hec")
    endpoint_url: Mapped[str] = mapped_column(Text, nullable=False)
    #: Envelope-encrypted. Never read back to a screen, never logged.
    token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)

    verify_tls: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    enabled:    Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    #: NULL/empty = everything. The safe default for an audit stream: a SOC that
    #: receives too much can filter, one that receives too little cannot know.
    min_severity: Mapped[str | None] = mapped_column(String(20), nullable=True)
    event_types:  Mapped[str | None] = mapped_column(Text, nullable=True)

    cursor_timestamp: Mapped[datetime | None] = mapped_column(MicroDateTime(), nullable=True)
    cursor_event_id:  Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)

    last_success_at: Mapped[datetime | None] = mapped_column(MicroDateTime(), nullable=True)
    last_error:      Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error_at:   Mapped[datetime | None] = mapped_column(MicroDateTime(), nullable=True)
    forwarded_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow, onupdate=_utcnow)
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
# <<< 200


# >>> 201 — HxShield response layer (roadmap #25)
class ShieldResponseModel(Base):
    """What HxShield decided to DO about an incident (mig 201).

    The engine has computed an `action` for every incident since it shipped —
    flag, alert, suspend, block — and nothing ever performed one. This row is
    the performance, and the two properties it exists to hold are both about
    restraint:

    * **Shadow first.** A response is always recorded; it executes only when
      `shield_response_enabled` is on. An install that takes the update does
      nothing differently.
    * **A destructive response is never automatic.** Suspending an account or
      killing every session waits for a person, whatever the switch says, and
      the approval is on this row with a name and a time.

    `incident_id` is deliberately NOT a foreign key: incidents are subject to
    retention, and "why was this account suspended" must outlive the alert.
    """

    __tablename__ = "shield_responses"
    __table_args__ = (
        Index("idx_shield_responses_state", "state", "created_at"),
        Index("idx_shield_responses_incident", "incident_id"),
        Index("idx_shield_responses_target", "target_kind", "target_id", "created_at"),
    )

    id:          Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:   Mapped[str | None] = mapped_column(String(255), nullable=True)
    incident_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)

    response_type: Mapped[str] = mapped_column(String(40), nullable=False)
    target_kind:   Mapped[str] = mapped_column(String(40), nullable=False)
    target_id:     Mapped[str] = mapped_column(String(255), nullable=False)

    #: proposed | approved | executed | failed | declined | shadowed
    state:       Mapped[str] = mapped_column(String(20), nullable=False, default="proposed")
    #: Stored rather than derived, so the row still says what it was when the
    #: registry changes.
    destructive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    reason:   Mapped[str | None] = mapped_column(Text, nullable=True)
    patterns: Mapped[str | None] = mapped_column(Text, nullable=True)
    score:    Mapped[float | None] = mapped_column(Float, nullable=True)

    approved_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(MicroDateTime(), nullable=True)
    declined_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    declined_at: Mapped[datetime | None] = mapped_column(MicroDateTime(), nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(MicroDateTime(), nullable=True)
    #: What actually happened, failure included. A response that silently did
    #: nothing is the failure mode that matters here.
    result:      Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at:  Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
# <<< 201


class ContentRolloutModel(Base):
    """A staged rollout of a new content version (mig 210, Oct item 18a).

    Chooses the case type version for NEW cases only; a case keeps the one it
    was created on. `open_key` is set while open and NULL once closed — its
    UNIQUE index is the one-open-rollout-per-name lock, on every dialect.
    """
    __tablename__ = "content_rollouts"
    __table_args__ = (
        UniqueConstraint("open_key", name="uq_cr_open"),
        Index("ix_cr_baseline", "tenant_id", "baseline_id"),
        Index("ix_cr_candidate", "tenant_id", "candidate_id"),
        Index("ix_cr_state", "tenant_id", "state"),
    )

    id:            Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:     Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    artifact_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    artifact_name: Mapped[str] = mapped_column(String(255), nullable=False)
    baseline_id:   Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    candidate_id:  Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    #: immediate | flag | percentage | blue_green
    strategy:      Mapped[str] = mapped_column(String(20), nullable=False)
    percent:       Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: requested | dormant | rolling | promoted | rolled_back | superseded
    state:         Mapped[str] = mapped_column(String(20), nullable=False)
    open_key:      Mapped[str | None] = mapped_column(String(400), nullable=True)
    source:        Mapped[str] = mapped_column(String(20), nullable=False, default="local")
    request:       Mapped[Any | None] = mapped_column(PortableJSON(), nullable=True)
    requested_by:  Mapped[str | None] = mapped_column(String(255), nullable=True)
    approved_by:   Mapped[str | None] = mapped_column(String(255), nullable=True)
    closed_by:     Mapped[str | None] = mapped_column(String(255), nullable=True)
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    note:          Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at:    Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
    updated_at:    Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
    approved_at:   Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at:     Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ContentRolloutEventModel(Base):
    """Every state and percent change on a rollout (mig 210)."""
    __tablename__ = "content_rollout_events"
    __table_args__ = (Index("ix_cre_rollout", "rollout_id", "at"),)

    id:           Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    rollout_id:   Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("content_rollouts.id", ondelete="CASCADE"), nullable=False)
    tenant_id:    Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    action:       Mapped[str] = mapped_column(String(30), nullable=False)
    from_state:   Mapped[str | None] = mapped_column(String(20), nullable=True)
    to_state:     Mapped[str | None] = mapped_column(String(20), nullable=True)
    from_percent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    to_percent:   Mapped[int | None] = mapped_column(Integer, nullable=True)
    actor:        Mapped[str | None] = mapped_column(String(255), nullable=True)
    reason:       Mapped[str | None] = mapped_column(Text, nullable=True)
    at:           Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)


class ContentRolloutCaseModel(Base):
    """Which arm a new case landed in (mig 210)."""
    __tablename__ = "content_rollout_cases"
    __table_args__ = (Index("ix_crc_rollout", "rollout_id", "arm"),)

    case_id:    Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("case_instances.id", ondelete="CASCADE"), primary_key=True)
    rollout_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("content_rollouts.id", ondelete="CASCADE"), nullable=False)
    tenant_id:  Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    #: baseline | candidate
    arm:        Mapped[str] = mapped_column(String(10), nullable=False)
    bucket:     Mapped[int] = mapped_column(Integer, nullable=False)
    #: which create path resolved it
    via:        Mapped[str | None] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)


class ContentRolloutPolicyModel(Base):
    """Optional per-rollout policy: shadow, targeting, ramp, guard (mig 211).

    No row ⇒ the rollout behaves exactly as in phase 1.
    """
    __tablename__ = "content_rollout_policies"
    __table_args__ = (Index("ix_crp_next", "next_step_at"),)

    rollout_id:   Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("content_rollouts.id", ondelete="CASCADE"), primary_key=True)
    tenant_id:    Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    shadow:       Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    targeting:    Mapped[Any | None] = mapped_column(PortableJSON(), nullable=True)
    ramp:         Mapped[Any | None] = mapped_column(PortableJSON(), nullable=True)
    guard:        Mapped[Any | None] = mapped_column(PortableJSON(), nullable=True)
    next_step_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: ok | held | tripped
    guard_state:  Mapped[str] = mapped_column(String(10), nullable=False, default="ok")
    guard_detail: Mapped[Any | None] = mapped_column(PortableJSON(), nullable=True)
    updated_by:   Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_at:   Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)


class ContentRolloutShadowModel(Base):
    """One shadow evaluation of a baseline case against the candidate (mig 211)."""
    __tablename__ = "content_rollout_shadow"
    __table_args__ = (Index("ix_crs_rollout", "rollout_id", "diverged"),)

    case_id:      Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("case_instances.id", ondelete="CASCADE"), primary_key=True)
    rollout_id:   Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("content_rollouts.id", ondelete="CASCADE"), nullable=False)
    tenant_id:    Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    determinacy:  Mapped[str] = mapped_column(String(20), nullable=False)
    diverged:     Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    detail:       Mapped[Any | None] = mapped_column(PortableJSON(), nullable=True)
    evaluated_at: Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)


class AuditChainPruneModel(Base):
    """A record that sealed audit rows were removed on purpose (mig 216).

    Retention may delete old `case_audit_log` rows; the chain rows that sealed
    them stay. Without this record `verify_chain` cannot tell that from a row
    somebody deleted by hand, and reports tampering (Oct_TODO §11h).

    The record is itself SEALED INTO THE CHAIN — a chain row's `audit_log_id`
    points at it — so it is exactly as hard to forge as the chain is. It names
    the chain sequences it covers (`ranges`, inclusive pairs) and a `digest` of
    their content hashes, so it cannot be stretched over a row it did not prune.
    `cutoff` and `recorded_at` are TEXT on purpose: they are hashed, and a
    datetime does not round-trip identically on three dialects.
    """
    __tablename__ = "case_audit_log_chain_prunes"

    id:           Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    reason:       Mapped[str] = mapped_column(String(40), nullable=False)
    actor:        Mapped[str] = mapped_column(String(255), nullable=False)
    cutoff:       Mapped[str | None] = mapped_column(String(40), nullable=True)
    recorded_at:  Mapped[str] = mapped_column(String(40), nullable=False)
    pruned_count: Mapped[int] = mapped_column(Integer, nullable=False)
    ranges:       Mapped[list] = mapped_column(PortableJSON(), nullable=False)
    digest:       Mapped[str] = mapped_column(String(64), nullable=False)
    created_at:   Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)


class LegalHoldModel(Base):
    """A legal hold on one case, for one matter (mig 215).

    While a case has an `active` row here, nothing may destroy, anonymize, hide
    or move it — see `case_service/legal_hold/service.py` for the list. A case
    takes one active hold at a time (decided 2026-10-11).

    Rows are never deleted: a released hold is the record that the freeze
    existed and who lifted it. `case_id` is deliberately NOT a foreign key, so
    that record outlives the case. `active_key` is the portable partial unique
    index — the case id while active, NULL once released.
    """
    __tablename__ = "legal_holds"
    __table_args__ = (
        Index("ux_legal_holds_active", "active_key", unique=True),
        Index("ix_legal_holds_case", "case_id", "status"),
        Index("ix_legal_holds_tenant", "tenant_id", "status", "placed_at"),
    )

    id:             Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    #: The case's tenant; the placer's when the case has none.
    tenant_id:      Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True)
    case_id:        Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    case_number:    Mapped[str | None] = mapped_column(String(30), nullable=True)
    #: The matter this hold belongs to — a lawsuit, an inquiry, a reference.
    matter_ref:     Mapped[str] = mapped_column(String(120), nullable=False)
    reason:         Mapped[str] = mapped_column(Text, nullable=False)
    #: active | released
    status:         Mapped[str] = mapped_column(String(12), nullable=False, default="active")
    active_key:     Mapped[str | None] = mapped_column(String(160), nullable=True)
    placed_by:      Mapped[str] = mapped_column(String(255), nullable=False)
    placed_at:      Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
    released_by:    Mapped[str | None] = mapped_column(String(255), nullable=True)
    released_at:    Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    release_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class LegalHoldSettingModel(Base):
    """Per-tenant legal-hold settings (mig 218).

    `approver_group_id` names the access group whose member must approve a
    release someone else asked for. No row, or NULL, means a release needs no
    second person — the behaviour before 218. Deliberately not a foreign key:
    deleting the group must not switch the rule off.
    """
    __tablename__ = "legal_hold_settings"

    tenant_id:         Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True)
    approver_group_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    updated_by:        Mapped[str] = mapped_column(String(255), nullable=False)
    updated_at:        Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)


class LegalHoldReleaseRequestModel(Base):
    """A request to release a legal hold, waiting for a second person (mig 218).

    One open request per hold: `pending_key` is the hold id while pending and
    NULL once decided — the same portable partial-unique trick as
    `legal_holds.active_key`. Rows are never deleted; a decided request is the
    record of who asked and who agreed.
    """
    __tablename__ = "legal_hold_release_requests"
    __table_args__ = (
        Index("ux_lh_release_requests_pending", "pending_key", unique=True),
        Index("ix_lh_release_requests_hold", "hold_id", "status"),
    )

    id:                Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    hold_id:           Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("legal_holds.id", ondelete="CASCADE"), nullable=False)
    tenant_id:         Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True)
    #: pending | approved | rejected | withdrawn | superseded
    status:            Mapped[str] = mapped_column(String(12), nullable=False, default="pending")
    pending_key:       Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason:            Mapped[str] = mapped_column(Text, nullable=False)
    requested_by:      Mapped[str] = mapped_column(String(255), nullable=False)
    requested_at:      Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
    #: The group that had to approve, as it was when the request was made.
    approver_group_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    decided_by:        Mapped[str | None] = mapped_column(String(255), nullable=True)
    decided_at:        Mapped[datetime | None] = mapped_column(MicroDateTime(), nullable=True)
    decision_note:     Mapped[str | None] = mapped_column(Text, nullable=True)


# 8e (mig 214): an ORM change of owner moves the uniqueness key with it, as for
# case types. A Core UPDATE of `tenant_id` must set `tenant_key` itself.
def _inherited_tenant_key_follows(target, value, _old, _initiator):
    target.tenant_key = case_type_tenant_key(value)


for _m in (FormDefinitionModel, RuleDefinitionModel, DataModelModel, BusinessCalendarModel):
    event.listen(_m.tenant_id, "set", _inherited_tenant_key_follows)


class ReferenceTableModel(Base):
    """A reference table — an admin-managed list a form reads (mig 225, Form Builder v2).

    Branches, products, price bands: the same table feeds dropdowns, search
    boxes and cascading options, and can fill other fields from the chosen
    row. `columns` is `[{key, label, type}]`; the rows are `reference_rows`.
    A tenant's table is its own; a NULL tenant is a platform table every
    tenant reads and only the platform operator changes (path guard).
    """
    __tablename__ = "reference_tables"
    __table_args__ = (
        UniqueConstraint("tenant_key", "name", name="ux_reference_tables_tenant_name"),
        Index("ix_reference_tables_tenant", "tenant_id"),
    )

    id:          Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:   Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True)
    #: `str(tenant_id)` or "platform" — the namespace a name is unique in.
    tenant_key:  Mapped[str] = mapped_column(String(64), nullable=False, default="platform")
    name:        Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    columns:     Mapped[list] = mapped_column(PortableJSON(), nullable=False, default=list)
    row_count:   Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by:  Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at:  Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
    updated_at:  Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow, onupdate=_utcnow)


class ReferenceRowModel(Base):
    """One row of a reference table (mig 225). `data` is `{column key: value}`;
    `search_text` is its values, lower-cased, for the search box."""
    __tablename__ = "reference_rows"
    __table_args__ = (
        Index("ix_reference_rows_table", "table_id", "sort_order"),
    )

    id:          Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    table_id:    Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("reference_tables.id", ondelete="CASCADE"), nullable=False)
    sort_order:  Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    data:        Mapped[dict] = mapped_column(PortableJSON(), nullable=False, default=dict)
    search_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_active:   Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class FormSectionModel(Base):
    """A reusable form section (mig 226, Form Builder §5 step 6).

    `section_json` is `{title, description, fields}`. A form links it
    (`section.section_ref = {id, version}`) and holds a copy of the fields;
    saving the section bumps `version` and updates every linked form. A NULL
    tenant is a platform section every tenant reads and only the operator changes.
    """
    __tablename__ = "form_sections"
    __table_args__ = (
        UniqueConstraint("tenant_key", "name", name="ux_form_sections_tenant_name"),
        Index("ix_form_sections_tenant", "tenant_id"),
    )

    id:           Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    tenant_id:    Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True)
    tenant_key:   Mapped[str] = mapped_column(String(64), nullable=False, default="platform")
    name:         Mapped[str] = mapped_column(String(100), nullable=False)
    description:  Mapped[str] = mapped_column(Text, nullable=False, default="")
    version:      Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    section_json: Mapped[dict] = mapped_column(PortableJSON(), nullable=False, default=dict)
    updated_by:   Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at:   Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)
    updated_at:   Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow, onupdate=_utcnow)


class FormSnapshotModel(Base):
    """An unchangeable copy of a form as one or more cases used it (mig 226).
    One row per distinct content (`content_hash`); kept when the form is deleted."""
    __tablename__ = "form_snapshots"
    __table_args__ = (
        UniqueConstraint("form_id", "content_hash", name="ux_form_snapshots_form_hash"),
    )

    id:              Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=_new_uuid)
    form_id:         Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    tenant_id:       Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    content_hash:    Mapped[str] = mapped_column(String(64), nullable=False)
    name:            Mapped[str] = mapped_column(String(255), nullable=False, default="")
    definition_json: Mapped[dict] = mapped_column(PortableJSON(), nullable=False, default=dict)
    created_at:      Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)


class CaseFormVersionModel(Base):
    """Which snapshot of a form a case reads (mig 226) — set the first time the
    case uses a form whose settings say `versioning: "per_case"`."""
    __tablename__ = "case_form_versions"

    case_id:     Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("case_instances.id", ondelete="CASCADE"), primary_key=True)
    form_id:     Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("form_snapshots.id"), nullable=False)
    pinned_at:   Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow)


class FormDraftModel(Base):
    """A person's unfinished answers to a case's form (mig 227, Form Builder §5 step 7).
    Never validated, read by its owner only, removed when they submit."""
    __tablename__ = "form_drafts"
    __table_args__ = (Index("ix_form_drafts_user", "user_id"),)

    case_id:     Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("case_instances.id", ondelete="CASCADE"), primary_key=True)
    form_id:     Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True)
    user_id:     Mapped[str] = mapped_column(String(255), primary_key=True)
    values_json: Mapped[dict] = mapped_column(PortableJSON(), nullable=False, default=dict)
    updated_at:  Mapped[datetime] = mapped_column(MicroDateTime(), default=_utcnow, onupdate=_utcnow)
