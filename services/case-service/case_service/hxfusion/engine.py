"""HxFusion execution engine.

Runs a BpmnProcess step-by-step against a ProcessInstanceModel.
Each node execution is logged to process_task_log and emitted to HxStream.

Design principles:
- Non-blocking: runs as an asyncio task, never in the request path.
- Self-healing: any serviceTask failure → instance enters `failed_at_node`
  status and waits for human resolution via /fusion/instances/{id}/resume.
- Connector calls delegate to HxBridge (P28) when a `connector_id` extension
  is present; otherwise the node is a no-op stub.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from case_service.hxfusion.parser import BpmnProcess, BpmnNode, parse

logger = logging.getLogger(__name__)

# Status constants
STATUS_RUNNING    = "running"
STATUS_COMPLETED  = "completed"
STATUS_FAILED     = "failed"
STATUS_SUSPENDED  = "suspended"  # waiting for human (userTask / self-healing)
STATUS_CANCELLED  = "cancelled"


async def start_instance(
    *,
    definition_id: uuid.UUID,
    case_id: uuid.UUID | None,
    context: dict[str, Any],
    tenant_id: str | None,
    stage_id: str | None,
    step_id: str | None,
    session,
) -> "ProcessInstanceModel":  # type: ignore[name-defined]
    """Create a ProcessInstance and launch execution as a background task."""
    from case_service.db.models import (
        ProcessDefinitionModel, ProcessInstanceModel, ProcessCaseBindingModel,
    )
    defn = await session.get(ProcessDefinitionModel, definition_id)
    if not defn:
        raise ValueError(f"Process definition {definition_id} not found")
    if defn.status != "active":
        raise ValueError(f"Process definition {definition_id} is not active")

    instance = ProcessInstanceModel(
        definition_id=definition_id,
        case_id=case_id,
        status=STATUS_RUNNING,
        context=dict(context),
        tenant_id=tenant_id,
    )
    session.add(instance)

    if case_id:
        binding = ProcessCaseBindingModel(
            case_id=case_id,
            instance_id=instance.id,
            binding_type="embedded_subprocess",
            direction="case_to_process",
            status="active",
            stage_id=stage_id,
            step_id=step_id,
        )
        session.add(binding)

    await session.flush()

    # Parse the BPMN
    try:
        process = parse(defn.bpmn_xml)
    except Exception as exc:
        instance.status = STATUS_FAILED
        instance.error_message = f"BPMN parse error: {exc}"
        await session.flush()
        return instance

    # Commit NOW so the instance row is visible to the background session
    # before we schedule the execution task (avoids a read-committed race).
    await session.commit()

    asyncio.ensure_future(
        _run_instance(
            instance_id=instance.id,
            process=process,
            initial_context=dict(context),
        )
    )
    return instance


async def resume_instance(
    *,
    instance_id: uuid.UUID,
    resolution: dict[str, Any],
    resumed_by: str | None,
    session,
) -> "ProcessInstanceModel":  # type: ignore[name-defined]
    """Resume a suspended/failed instance from the node it stopped at."""
    from case_service.db.models import ProcessInstanceModel, ProcessDefinitionModel

    instance = await session.get(ProcessInstanceModel, instance_id)
    if not instance:
        raise ValueError(f"Instance {instance_id} not found")
    if instance.status not in (STATUS_SUSPENDED, STATUS_FAILED):
        raise ValueError(f"Instance {instance_id} is not suspended or failed (status={instance.status})")

    defn = await session.get(ProcessDefinitionModel, instance.definition_id)
    process = parse(defn.bpmn_xml)

    # Merge resolution data into context
    merged_context = {**instance.context, **resolution}
    instance.context = merged_context
    instance.status = STATUS_RUNNING
    instance.error_node = None
    instance.error_message = None

    # For a suspended userTask: the human completed it, so advance PAST it.
    # Compute next nodes using the merged context so gateway conditions resolve.
    suspended_node = instance.current_node
    if suspended_node:
        next_nodes = process.next_nodes(suspended_node, merged_context)
        start_node = next_nodes[0] if next_nodes else None
    else:
        start_node = None

    # Commit so the updated context and status are visible to the background task
    await session.commit()

    asyncio.ensure_future(
        _run_instance(
            instance_id=instance.id,
            process=process,
            initial_context=merged_context,
            start_node=start_node,
        )
    )
    return instance


# ── Internal execution loop ───────────────────────────────────────────────────

async def _run_instance(
    *,
    instance_id: uuid.UUID,
    process: BpmnProcess,
    initial_context: dict,
    start_node: str | None = None,
) -> None:
    """Execute the process instance node-by-node until end or suspension."""
    from case_service.db.session import get_session_factory as _get_sf
    from case_service.db.models import ProcessInstanceModel

    async with _get_sf()() as session:
        try:
            instance = await session.get(ProcessInstanceModel, instance_id)
            if not instance or instance.status != STATUS_RUNNING:
                return

            context = dict(initial_context)

            # Determine starting node
            if start_node:
                queue = [start_node]
            elif process.start_events:
                queue = list(process.start_events)
            else:
                await _fail(session, instance, "no_start", None, "No startEvent found")
                await session.commit()
                return

            visited: set[str] = set()
            parallel_pending: dict[str, int] = {}  # parallelGateway join tracking

            while queue:
                node_id = queue.pop(0)
                if node_id in visited:
                    continue

                node = process.nodes.get(node_id)
                if not node:
                    continue

                instance.current_node = node_id
                await session.flush()

                result, suspended, error = await _execute_node(
                    session, instance, node, context,
                )

                if error:
                    await _fail(session, instance, node_id, node.name, error)
                    await session.commit()
                    return

                if suspended:
                    instance.status = STATUS_SUSPENDED
                    instance.current_node = node_id
                    await session.commit()
                    return

                if result:
                    context.update(result)
                    instance.context = dict(context)

                visited.add(node_id)

                if node.node_type == "endEvent":
                    instance.status = STATUS_COMPLETED
                    instance.ended_at = datetime.now(timezone.utc)
                    await _resolve_bindings(session, instance)
                    await session.commit()
                    await _emit("process_completed", instance, context)
                    return

                # Parallel gateway join: only proceed when all incoming flows arrived
                if node.node_type == "parallelGateway" and len(node.incoming) > 1:
                    parallel_pending[node_id] = parallel_pending.get(node_id, 0) + 1
                    if parallel_pending[node_id] < len(node.incoming):
                        continue

                next_ids = process.next_nodes(node_id, context)
                queue.extend(next_ids)

            # Fell off the end without hitting an endEvent — mark complete anyway
            instance.status = STATUS_COMPLETED
            instance.ended_at = datetime.now(timezone.utc)
            await _resolve_bindings(session, instance)
            await session.commit()
            await _emit("process_completed", instance, context)

        except Exception as exc:
            logger.exception("Unhandled error in HxFusion execution loop")
            async with _get_sf()() as err_session:
                inst = await err_session.get(ProcessInstanceModel, instance_id)
                if inst:
                    inst.status = STATUS_FAILED
                    inst.error_message = str(exc)
                    await err_session.commit()


async def _execute_node(
    session,
    instance: Any,
    node: BpmnNode,
    context: dict,
) -> tuple[dict | None, bool, str | None]:
    """Execute a single BPMN node.

    Returns (result_dict | None, suspended: bool, error_msg | None).
    """
    from case_service.db.models import ProcessTaskLogModel

    log = ProcessTaskLogModel(
        instance_id=instance.id,
        node_id=node.id,
        node_name=node.name,
        node_type=node.node_type,
        status=STATUS_RUNNING,
        input_context=dict(context),
    )
    session.add(log)
    await session.flush()

    result: dict | None = None
    suspended = False
    error: str | None = None

    try:
        if node.node_type in ("startEvent", "endEvent"):
            pass  # no-op

        elif node.node_type == "serviceTask":
            result, error = await _run_service_task(instance, node, context)

        elif node.node_type == "userTask":
            # Suspend and wait for human completion
            suspended = True

        elif node.node_type in (
            "exclusiveGateway", "parallelGateway",
            "inclusiveGateway", "eventBasedGateway",
        ):
            pass  # routing handled by process.next_nodes()

        elif node.node_type == "scriptTask":
            result, error = _run_script_task(node, context)

        elif node.node_type == "spawnCase":
            # Spawn a child case (fire-and-forget for now)
            asyncio.ensure_future(_spawn_child_case(instance, node, context))

        # else: boundary events, subProcesses — treated as pass-through

    except Exception as exc:
        error = str(exc)

    log.status = "suspended" if suspended else ("failed" if error else "completed")
    log.result = result
    log.error = error
    log.ended_at = datetime.now(timezone.utc)
    await session.flush()

    # Emit HxStream for every node
    node_event = "user_task_pending" if suspended else (
        "automation_run" if node.node_type == "serviceTask" else "process_node"
    )
    asyncio.ensure_future(_emit(node_event, instance, {
        "node_id": node.id, "node_name": node.name,
        "node_type": node.node_type, "result": result, "error": error,
    }))

    return result, suspended, error


async def _run_service_task(
    instance: Any,
    node: BpmnNode,
    context: dict,
) -> tuple[dict | None, str | None]:
    """Execute a serviceTask by delegating to HxBridge if connector_id is set."""
    connector_id = node.extensions.get("connector_id")
    if not connector_id:
        # No connector wired — log and pass through
        return {"_service_task_stub": node.id}, None

    try:
        from case_service.db.session import get_session_factory as _get_sf
        from case_service.hxbridge.protocol import get_connector

        async with _get_sf()() as session:
            connector = await get_connector(connector_id, session)
            if connector is None:
                return None, f"Connector '{connector_id}' not found"
            payload = {k: v for k, v in context.items() if not k.startswith("_")}
            result = await connector.execute(payload)
            return result, None
    except Exception as exc:
        return None, str(exc)


def _run_script_task(node: BpmnNode, context: dict) -> tuple[dict | None, str | None]:
    """Execute an inline script (Python expression in extensions.script)."""
    script = node.extensions.get("script", "")
    if not script:
        return None, None
    try:
        local_ctx = dict(context)
        exec(script, {"__builtins__": {}}, local_ctx)  # noqa: S102
        result = {k: v for k, v in local_ctx.items() if k not in context}
        return result or None, None
    except Exception as exc:
        return None, f"Script error: {exc}"


async def _spawn_child_case(instance: Any, node: BpmnNode, context: dict) -> None:
    """Spawn a child case from a spawnCase (BPMN callActivity) node."""
    case_type_id = node.extensions.get("case_type_id")
    if not case_type_id:
        return
    try:
        from case_service.db.session import get_session_factory as _get_sf
        from case_service.db.models import CaseInstanceModel, ProcessCaseBindingModel, ProcessInstanceModel
        async with _get_sf()() as session:
            child = CaseInstanceModel(
                case_type_id=case_type_id,
                tenant_id=instance.tenant_id,
                data={**context, "parent_process_instance_id": str(instance.id)},
            )
            session.add(child)
            await session.flush()
            binding = ProcessCaseBindingModel(
                case_id=child.id,
                instance_id=instance.id,
                binding_type="node_spawn",
                direction="process_to_case",
                status="active",
            )
            session.add(binding)
            await session.commit()
    except Exception:
        logger.exception("Failed to spawn child case from callActivity")


async def _fail(session, instance: Any, node_id: str, node_name: str | None, error: str) -> None:
    instance.status = STATUS_FAILED
    instance.error_node = node_id
    instance.error_message = error
    await session.flush()
    asyncio.ensure_future(_emit("process_failed", instance, {
        "node_id": node_id, "node_name": node_name, "error": error,
    }))


async def _resolve_bindings(session, instance: Any) -> None:
    from sqlalchemy import select, update
    from case_service.db.models import ProcessCaseBindingModel
    await session.execute(
        update(ProcessCaseBindingModel)
        .where(ProcessCaseBindingModel.instance_id == instance.id)
        .where(ProcessCaseBindingModel.status == "active")
        .values(status="resolved", resolved_at=datetime.now(timezone.utc))
    )


async def _emit(event_type: str, instance: Any, payload: dict) -> None:
    try:
        from case_service.hxstream.emitter import emit_trace
        await emit_trace(
            event_type,
            {**payload, "instance_id": str(instance.id), "definition_id": str(instance.definition_id)},
            case_id=instance.case_id,
            tenant_id=instance.tenant_id or "default",
        )
    except Exception:
        pass
