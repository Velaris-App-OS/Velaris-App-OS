"""
Task Execution Handlers
========================

Dispatches BPMN tasks to plugin resolvers registered in the SDK.

Each task type maps to an activity type string that the plugin registry
uses for lookup:

  ┌─────────────────────┬──────────────────────────┐
  │ IR Type             │ Activity Type            │
  ├─────────────────────┼──────────────────────────┤
  │ UserTask            │ helix.task.user          │
  │ ServiceTask         │ helix.task.service       │
  │ ScriptTask          │ helix.task.script        │
  │ SendTask            │ helix.task.send          │
  │ ReceiveTask         │ helix.task.receive       │
  │ ManualTask          │ helix.task.manual        │
  │ BusinessRuleTask    │ helix.task.business_rule │
  │ GenericTask         │ helix.task.generic       │
  └─────────────────────┴──────────────────────────┘

The ``TaskDispatcher`` tries each registered resolver until one claims
the task.  If none do, the task is logged and skipped (no-op).

For multi-instance tasks, the dispatcher handles the loop logic
(parallel or sequential) and collects results.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import structlog

from helix_ir.models.process import (
    BusinessRuleTask,
    GenericTask,
    ManualTask,
    MultiInstanceType,
    ReceiveTask,
    ScriptTask,
    SendTask,
    ServiceTask,
    UserTask,
    _TaskBase,
)

logger = structlog.get_logger()


# ═══════════════════════════════════════════════════════════════════════
#  Task resolver protocol (plugins implement this)
# ═══════════════════════════════════════════════════════════════════════

@runtime_checkable
class TaskResolver(Protocol):
    """
    Plugin protocol for resolving BPMN tasks to executable logic.

    Each plugin (HTTP client, AI provider, form service, etc.) implements
    this protocol to handle specific task types or implementations.

    Example::

        class OrderServiceResolver:
            async def can_handle(self, task: _TaskBase) -> bool:
                return (isinstance(task, ServiceTask)
                        and task.implementation
                        and task.implementation.startswith("helix://order-service"))

            async def resolve(self, task: _TaskBase, variables: dict) -> dict:
                # Call the order service API
                result = await http_client.post(...)
                return {"order_status": result["status"]}
    """

    async def can_handle(self, task: _TaskBase) -> bool:
        """Return True if this resolver can execute the given task."""
        ...

    async def resolve(self, task: _TaskBase, variables: dict[str, Any]) -> dict[str, Any]:
        """
        Execute the task and return updated/new variables.

        The returned dict is merged into the process variables.
        """
        ...


# ═══════════════════════════════════════════════════════════════════════
#  Activity type mapping
# ═══════════════════════════════════════════════════════════════════════

ACTIVITY_TYPE_MAP: dict[type, str] = {
    UserTask:         "helix.task.user",
    ServiceTask:      "helix.task.service",
    ScriptTask:       "helix.task.script",
    SendTask:         "helix.task.send",
    ReceiveTask:      "helix.task.receive",
    ManualTask:       "helix.task.manual",
    BusinessRuleTask: "helix.task.business_rule",
    GenericTask:      "helix.task.generic",
}


def activity_type_for(task: _TaskBase) -> str:
    """Get the Temporal activity type string for a task IR element."""
    return ACTIVITY_TYPE_MAP.get(type(task), "helix.task.unknown")


# ═══════════════════════════════════════════════════════════════════════
#  Task result
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class TaskResult:
    """
    The output of a task execution.

    ``variables`` contains any new or updated process variables.
    ``resolved_by`` records which resolver handled the task (for debugging).
    """
    variables: dict[str, Any] = field(default_factory=dict)
    resolved_by: str | None = None


# ═══════════════════════════════════════════════════════════════════════
#  Task dispatcher
# ═══════════════════════════════════════════════════════════════════════

class TaskDispatcher:
    """
    Dispatches BPMN tasks to registered plugin resolvers.

    Usage::

        dispatcher = TaskDispatcher(resolvers=[
            OrderServiceResolver(),
            AIResolver(),
            FormResolver(),
        ])

        result = await dispatcher.dispatch(service_task, variables)
        variables.update(result.variables)
    """

    def __init__(self, resolvers: list[TaskResolver] | None = None):
        self._resolvers = resolvers or []

    def register(self, resolver: TaskResolver) -> None:
        """Add a resolver at runtime."""
        self._resolvers.append(resolver)

    async def dispatch(
        self,
        task: _TaskBase,
        variables: dict[str, Any],
    ) -> TaskResult:
        """
        Execute a single task instance.

        Tries each resolver in order until one claims the task.
        If none do, returns an empty result (no-op).
        """
        activity_type = activity_type_for(task)

        for resolver in self._resolvers:
            if await resolver.can_handle(task):
                logger.info("task_dispatching",
                             task_id=task.id,
                             activity_type=activity_type,
                             resolver=type(resolver).__name__)

                result_vars = await resolver.resolve(task, variables)

                logger.info("task_completed",
                             task_id=task.id,
                             resolver=type(resolver).__name__,
                             output_keys=list(result_vars.keys()))

                return TaskResult(
                    variables=result_vars,
                    resolved_by=type(resolver).__name__,
                )

        logger.warning("task_no_resolver",
                         task_id=task.id,
                         activity_type=activity_type,
                         task_name=task.name)
        return TaskResult()

    async def dispatch_multi_instance(
        self,
        task: _TaskBase,
        variables: dict[str, Any],
    ) -> TaskResult:
        """
        Execute a multi-instance task (parallel or sequential).

        Reads the collection from variables, iterates over it, and
        collects results from each instance.
        """
        mi = task.multi_instance
        if mi.type == MultiInstanceType.NONE:
            return await self.dispatch(task, variables)

        # Resolve the collection
        collection = variables.get(mi.collection, []) if mi.collection else []
        if not isinstance(collection, (list, tuple)):
            logger.warning("multi_instance_not_iterable",
                            task_id=task.id, collection_expr=mi.collection)
            return TaskResult()

        results: list[dict[str, Any]] = []

        if mi.type == MultiInstanceType.SEQUENTIAL:
            # Run one at a time
            for item in collection:
                loop_vars = {**variables}
                if mi.element_variable:
                    loop_vars[mi.element_variable] = item
                result = await self.dispatch(task, loop_vars)
                results.append(result.variables)

        elif mi.type == MultiInstanceType.PARALLEL:
            # Run all at once
            import asyncio

            async def _run(item: Any) -> dict[str, Any]:
                loop_vars = {**variables}
                if mi.element_variable:
                    loop_vars[mi.element_variable] = item
                r = await self.dispatch(task, loop_vars)
                return r.variables

            results = list(await asyncio.gather(*[_run(item) for item in collection]))

        logger.info("multi_instance_completed",
                     task_id=task.id,
                     instance_count=len(results),
                     mode=mi.type.name)

        return TaskResult(
            variables={f"_mi_results_{task.id}": results},
        )
