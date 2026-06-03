"""
Database Models — SQLAlchemy ORM
"""
from __future__ import annotations
from datetime import datetime, timezone
from sqlalchemy import Column, DateTime, Integer, String, Text, Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ProcessDefinition(Base):
    __tablename__ = "helix_processes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    process_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    name: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="active")
    bpmn_xml: Mapped[str] = mapped_column(Text, nullable=False)
    compiled_ir: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    element_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    flow_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    warnings: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    tags: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    deployed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    __table_args__ = (
        Index("ix_helix_processes_id_version", "process_id", "version", unique=True),
    )


class ProcessInstance(Base):
    __tablename__ = "helix_process_instances"
    instance_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    process_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="running")
    business_key: Mapped[str | None] = mapped_column(String(500), nullable=True, index=True)
    temporal_workflow_id: Mapped[str | None] = mapped_column(String(500), nullable=True)
    variables: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    visited_elements: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        Index("ix_helix_instances_process_status", "process_id", "status"),
    )
