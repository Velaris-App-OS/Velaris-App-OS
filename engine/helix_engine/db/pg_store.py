"""
Process Store — PostgreSQL
===========================

Persistent store for deployed processes and running instances.
Replaces the in-memory store with real PostgreSQL persistence.

Same interface as the in-memory store — the API router doesn't need
to change.  Processes and instances survive engine restarts.

Uses async SQLAlchemy with asyncpg for non-blocking database access.

Usage::

    from helix_engine.db.pg_store import PgProcessStore

    store = PgProcessStore()
    deployed = await store.deploy(process, bpmn_xml, compiled_ir)
    instance = await store.create_instance("my_process", {"order_id": "123"})
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy import select, update, desc, func

from helix_engine.db.session import get_session
from helix_engine.db.models import ProcessDefinition, ProcessInstance
from helix_engine.api.schemas.process import InstanceStatus, ProcessStatus

logger = structlog.get_logger()


class PgProcessStore:
    """
    PostgreSQL-backed process store.

    All methods are async — they use the shared session factory
    from ``db.session``.
    """

    # ── Deploy ────────────────────────────────────────────────────

    async def deploy(
        self,
        process_id: str,
        name: str | None,
        bpmn_xml: str,
        compiled_ir: dict[str, Any],
        element_count: int,
        flow_count: int,
        warnings: list[str] | None = None,
        tags: dict[str, str] | None = None,
    ) -> ProcessDefinition:
        """
        Deploy a compiled process.  Auto-increments version.

        Previous versions are deprecated automatically.
        """
        async with get_session() as session:
            # Get the current max version for this process_id
            result = await session.execute(
                select(func.max(ProcessDefinition.version))
                .where(ProcessDefinition.process_id == process_id)
            )
            max_version = result.scalar() or 0
            new_version = max_version + 1

            # Deprecate previous active version
            if max_version > 0:
                await session.execute(
                    update(ProcessDefinition)
                    .where(ProcessDefinition.process_id == process_id)
                    .where(ProcessDefinition.status == ProcessStatus.ACTIVE.value)
                    .values(status=ProcessStatus.DEPRECATED.value)
                )

            # Insert new version
            row = ProcessDefinition(
                process_id=process_id,
                version=new_version,
                name=name,
                status=ProcessStatus.ACTIVE.value,
                bpmn_xml=bpmn_xml,
                compiled_ir=compiled_ir,
                element_count=element_count,
                flow_count=flow_count,
                warnings=warnings or [],
                tags=tags or {},
            )
            session.add(row)
            await session.flush()

            logger.info("process_deployed_pg",
                         process_id=process_id, version=new_version)
            return row

    async def get_process(
        self,
        process_id: str,
        version: int | None = None,
    ) -> ProcessDefinition | None:
        """Get a deployed process.  Returns latest version if version is None."""
        async with get_session() as session:
            if version is not None:
                result = await session.execute(
                    select(ProcessDefinition)
                    .where(ProcessDefinition.process_id == process_id)
                    .where(ProcessDefinition.version == version)
                )
            else:
                result = await session.execute(
                    select(ProcessDefinition)
                    .where(ProcessDefinition.process_id == process_id)
                    .order_by(desc(ProcessDefinition.version))
                    .limit(1)
                )
            return result.scalar_one_or_none()

    async def list_processes(self) -> list[ProcessDefinition]:
        """List the latest version of each deployed process."""
        async with get_session() as session:
            # Subquery: max version per process_id
            subq = (
                select(
                    ProcessDefinition.process_id,
                    func.max(ProcessDefinition.version).label("max_version"),
                )
                .group_by(ProcessDefinition.process_id)
                .subquery()
            )

            result = await session.execute(
                select(ProcessDefinition)
                .join(
                    subq,
                    (ProcessDefinition.process_id == subq.c.process_id)
                    & (ProcessDefinition.version == subq.c.max_version),
                )
                .order_by(ProcessDefinition.process_id)
            )
            return list(result.scalars().all())

    # ── Instances ─────────────────────────────────────────────────

    async def create_instance(
        self,
        process_id: str,
        version: int,
        variables: dict[str, Any] | None = None,
        business_key: str | None = None,
        temporal_workflow_id: str | None = None,
    ) -> ProcessInstance:
        """Create a new process instance."""
        async with get_session() as session:
            row = ProcessInstance(
                instance_id=str(uuid.uuid4()),
                process_id=process_id,
                version=version,
                status=InstanceStatus.RUNNING.value,
                business_key=business_key,
                temporal_workflow_id=temporal_workflow_id,
                variables=dict(variables or {}),
                visited_elements=[],
            )
            session.add(row)
            await session.flush()

            logger.info("instance_created_pg",
                         instance_id=row.instance_id, process_id=process_id)
            return row

    async def get_instance(self, instance_id: str) -> ProcessInstance | None:
        """Get an instance by id."""
        async with get_session() as session:
            result = await session.execute(
                select(ProcessInstance)
                .where(ProcessInstance.instance_id == instance_id)
            )
            return result.scalar_one_or_none()

    async def update_instance(
        self,
        instance_id: str,
        status: InstanceStatus | None = None,
        variables: dict[str, Any] | None = None,
        visited_elements: list[str] | None = None,
        error: str | None = None,
        temporal_workflow_id: str | None = None,
    ) -> ProcessInstance | None:
        """Update instance state."""
        async with get_session() as session:
            values: dict[str, Any] = {}
            if status is not None:
                values["status"] = status.value
            if variables is not None:
                values["variables"] = variables
            if visited_elements is not None:
                values["visited_elements"] = visited_elements
            if error is not None:
                values["error"] = error
            if temporal_workflow_id is not None:
                values["temporal_workflow_id"] = temporal_workflow_id

            if status in (InstanceStatus.COMPLETED, InstanceStatus.FAILED, InstanceStatus.CANCELLED):
                values["completed_at"] = datetime.now(timezone.utc)

            if values:
                await session.execute(
                    update(ProcessInstance)
                    .where(ProcessInstance.instance_id == instance_id)
                    .values(**values)
                )

            # Re-fetch
            result = await session.execute(
                select(ProcessInstance)
                .where(ProcessInstance.instance_id == instance_id)
            )
            return result.scalar_one_or_none()

    async def list_instances(
        self,
        process_id: str | None = None,
        status: InstanceStatus | None = None,
    ) -> list[ProcessInstance]:
        """List instances, optionally filtered."""
        async with get_session() as session:
            query = select(ProcessInstance)

            if process_id:
                query = query.where(ProcessInstance.process_id == process_id)
            if status:
                query = query.where(ProcessInstance.status == status.value)

            query = query.order_by(desc(ProcessInstance.started_at))
            result = await session.execute(query)
            return list(result.scalars().all())

    async def delete_process(self, process_id: str) -> bool:
        """Delete all versions of a process and its instances."""
        from sqlalchemy import delete as sql_delete
        async with get_session() as session:
            # Delete instances first
            await session.execute(
                sql_delete(ProcessInstance)
                .where(ProcessInstance.process_id == process_id)
            )
            # Delete process definitions
            result = await session.execute(
                sql_delete(ProcessDefinition)
                .where(ProcessDefinition.process_id == process_id)
            )
            return result.rowcount > 0
