"""
Process Store — In-Memory
==========================

Holds deployed process definitions and running instance state.

This is the **in-memory implementation** for development and testing.
In production, swap it for a database-backed store by implementing
the same interface against your DatabaseBackend Protocol.

Why in-memory first?
  - Zero dependencies — works immediately, no DB setup needed.
  - Fast iteration — restart the engine, deploy, test, repeat.
  - Easy to debug — inspect state directly in Python.
  - Same interface — switching to Postgres/MySQL later is just a new class.

Thread safety:
  This store is used by async FastAPI handlers running on a single event loop,
  so dict operations are safe.  For multi-worker deployments, replace with a
  proper database.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import structlog

from helix_ir.models.process import BPMNProcess
from helix_engine.api.schemas.process import (
    InstanceStatus,
    ProcessStatus,
)

logger = structlog.get_logger()


@dataclass
class DeployedProcess:
    """A compiled process definition stored in the engine."""
    process_id: str
    version: int
    name: str | None
    status: ProcessStatus
    process: BPMNProcess              # The compiled IR
    bpmn_xml: str                     # Original XML (for re-export / versioning)
    element_count: int
    flow_count: int
    warnings: list[str]
    tags: dict[str, str]
    deployed_at: datetime


@dataclass
class ProcessInstance:
    """A running or completed process instance."""
    instance_id: str
    process_id: str
    version: int
    status: InstanceStatus
    business_key: str | None
    variables: dict                   # Current process variables
    visited_elements: list[str]       # Execution trace
    error: str | None = None
    temporal_workflow_id: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None


class ProcessStore:
    """
    In-memory store for deployed processes and running instances.

    Usage::

        store = ProcessStore()

        # Deploy
        deployed = store.deploy(process, bpmn_xml, warnings)

        # Start instance
        instance = store.create_instance("my_process", {"order_id": "123"})

        # Update instance
        store.update_instance(instance.instance_id, status=InstanceStatus.COMPLETED)

        # Query
        store.get_process("my_process")       → latest version
        store.get_instance("instance-uuid")   → instance state
        store.list_processes()                 → all deployed
        store.list_instances("my_process")     → instances of a process
    """

    def __init__(self) -> None:
        # process_id → list of versions (latest last)
        self._processes: dict[str, list[DeployedProcess]] = {}
        # instance_id → ProcessInstance
        self._instances: dict[str, ProcessInstance] = {}

    # ── Deploy ────────────────────────────────────────────────────

    async def deploy(
        self,
        process: BPMNProcess,
        bpmn_xml: str,
        warnings: list[str] | None = None,
        name: str | None = None,
        tags: dict[str, str] | None = None,
    ) -> DeployedProcess:
        """
        Deploy a compiled process.  Auto-increments version number.

        If a process with the same id already exists, a new version is created.
        The previous version is automatically deprecated.
        """
        versions = self._processes.get(process.id, [])
        new_version = len(versions) + 1

        # Deprecate previous version
        if versions:
            versions[-1].status = ProcessStatus.DEPRECATED

        deployed = DeployedProcess(
            process_id=process.id,
            version=new_version,
            name=name or process.name,
            status=ProcessStatus.ACTIVE,
            process=process,
            bpmn_xml=bpmn_xml,
            element_count=len(process.elements),
            flow_count=len(process.flows),
            warnings=warnings or [],
            tags=tags or {},
            deployed_at=datetime.now(timezone.utc),
        )

        if process.id not in self._processes:
            self._processes[process.id] = []
        self._processes[process.id].append(deployed)

        logger.info("process_deployed",
                     process_id=process.id,
                     version=new_version,
                     elements=deployed.element_count)
        return deployed

    async def get_process(
        self,
        process_id: str,
        version: int | None = None,
    ) -> DeployedProcess | None:
        """
        Get a deployed process by id.

        If version is None, returns the latest version.
        """
        versions = self._processes.get(process_id)
        if not versions:
            return None
        if version is None:
            return versions[-1]  # Latest
        for v in versions:
            if v.version == version:
                return v
        return None

    async def list_processes(self) -> list[DeployedProcess]:
        """List the latest version of each deployed process."""
        return [versions[-1] for versions in self._processes.values()]

    # ── Instances ─────────────────────────────────────────────────

    async def create_instance(
        self,
        process_id: str,
        version: int | None = None,
        variables: dict | None = None,
        business_key: str | None = None,
        temporal_workflow_id: str | None = None,
    ) -> ProcessInstance | None:
        """
        Create a new process instance.

        Returns None if the process doesn't exist or isn't active.
        """
        deployed = self.get_process(process_id)
        if deployed is None or deployed.status != ProcessStatus.ACTIVE:
            return None

        instance = ProcessInstance(
            instance_id=str(uuid.uuid4()),
            process_id=process_id,
            version=deployed.version,
            status=InstanceStatus.RUNNING,
            business_key=business_key,
            variables=dict(variables or {}),
            visited_elements=[],
            temporal_workflow_id=temporal_workflow_id,
        )

        self._instances[instance.instance_id] = instance

        logger.info("instance_created",
                     instance_id=instance.instance_id,
                     process_id=process_id,
                     version=deployed.version)
        return instance

    async def get_instance(self, instance_id: str) -> ProcessInstance | None:
        """Get an instance by id."""
        return self._instances.get(instance_id)

    async def update_instance(
        self,
        instance_id: str,
        status: InstanceStatus | None = None,
        variables: dict | None = None,
        visited_elements: list[str] | None = None,
        error: str | None = None,
        temporal_workflow_id: str | None = None,
    ) -> ProcessInstance | None:
        """Update instance state.  Returns None if not found."""
        instance = self._instances.get(instance_id)
        if instance is None:
            return None

        if status is not None:
            instance.status = status
        if variables is not None:
            instance.variables = variables
        if visited_elements is not None:
            instance.visited_elements = visited_elements
        if error is not None:
            instance.error = error
        if temporal_workflow_id is not None:
            instance.temporal_workflow_id = temporal_workflow_id

        if status in (InstanceStatus.COMPLETED, InstanceStatus.FAILED, InstanceStatus.CANCELLED):
            instance.completed_at = datetime.now(timezone.utc)

        return instance

    async def list_instances(
        self,
        process_id: str | None = None,
        status: InstanceStatus | None = None,
    ) -> list[ProcessInstance]:
        """List instances, optionally filtered by process and/or status."""
        instances = self._instances.values()
        if process_id:
            instances = [i for i in instances if i.process_id == process_id]
        if status:
            instances = [i for i in instances if i.status == status]
        return sorted(instances, key=lambda i: i.started_at, reverse=True)

    async def delete_process(self, process_id: str) -> bool:
        """Delete all versions of a process and its instances."""
        if process_id not in self._processes:
            return False
        del self._processes[process_id]
        # Remove related instances
        to_remove = [iid for iid, inst in self._instances.items() if inst.process_id == process_id]
        for iid in to_remove:
            del self._instances[iid]
        return True
