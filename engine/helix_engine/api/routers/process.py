"""
Process API Router
===================

FastAPI routes for process lifecycle management.

All store operations are ``await``ed — works with both the PostgreSQL
store and the in-memory store (for tests / dev without DB).

Endpoints::

    POST   /processes/deploy                             Deploy a BPMN process
    GET    /processes                                    List deployed processes
    GET    /processes/{process_id}                       Get process details
    POST   /processes/{process_id}/start                 Start a new instance
    GET    /processes/{process_id}/instances              List instances
    GET    /processes/{process_id}/instances/{id}         Get instance status
    POST   /processes/{process_id}/instances/{id}/cancel  Cancel instance
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from fastapi import APIRouter, HTTPException

from helix_engine.compiler import BPMNCompiler, CompilationError
from helix_engine.compiler.parser import ParseError
from helix_engine.api.schemas.process import (
    CompleteTaskRequest,
    DeployRequest,
    DeployResponse,
    ErrorResponse,
    InstanceListResponse,
    InstanceStatus,
    InstanceStatusResponse,
    InstanceSummary,
    PendingUserTask,
    ProcessListResponse,
    ProcessStatus,
    ProcessSummary,
    ScheduleRequest,
    ScheduleResponse,
    StartRequest,
    StartResponse,
)

logger = structlog.get_logger()

router = APIRouter(prefix="/processes", tags=["processes"])

# ── Shared state (set during lifespan) ────────────────────────────────

_store: Any = None
_compiler: BPMNCompiler | None = None


def init_router(store: Any, compiler: BPMNCompiler, is_pg: bool = False) -> None:
    """Called during FastAPI lifespan to inject dependencies."""
    global _store, _compiler
    _store = store
    _compiler = compiler
    logger.info("process_router_initialized")


def _get_store() -> Any:
    if _store is None:
        raise RuntimeError("Store not initialized")
    return _store


def _get_compiler() -> BPMNCompiler:
    if _compiler is None:
        raise RuntimeError("Compiler not initialized")
    return _compiler


# ═══════════════════════════════════════════════════════════════════════
#  POST /processes/deploy
# ═══════════════════════════════════════════════════════════════════════

@router.post(
    "/deploy",
    response_model=DeployResponse,
    status_code=201,
    responses={422: {"model": ErrorResponse}},
    summary="Deploy a BPMN process",
)
async def deploy_process(request: DeployRequest) -> DeployResponse:
    """Compile and deploy a BPMN 2.0 process definition."""
    compiler = _get_compiler()
    store = _get_store()

    try:
        result = compiler.compile(request.bpmn_xml)
    except (CompilationError, ParseError) as e:
        raise HTTPException(status_code=422, detail={
            "error": "compilation_error",
            "detail": str(e),
        })

    # Serialize the process IR for storage
    from helix_engine.temporal.workflows import _serialize_process
    process_data = _serialize_process(result.process)

    deployed = await store.deploy(
        compiled_ir=process_data,
        bpmn_xml=request.bpmn_xml,
        process_id=result.process.id,
        element_count=len(result.process.elements),
        flow_count=len(result.process.flows),
        warnings=result.validation.warnings,
        name=request.name or result.process.name,
        tags=request.tags,
    )

    return DeployResponse(
        process_id=deployed.process_id,
        version=deployed.version,
        name=deployed.name,
        status=ProcessStatus(deployed.status),
        element_count=deployed.element_count,
        flow_count=deployed.flow_count,
        warnings=deployed.warnings,
        deployed_at=deployed.deployed_at,
    )


# ═══════════════════════════════════════════════════════════════════════
#  GET /processes
# ═══════════════════════════════════════════════════════════════════════

@router.get("", response_model=ProcessListResponse, summary="List deployed processes")
async def list_processes() -> ProcessListResponse:
    store = _get_store()
    deployed = await store.list_processes()

    return ProcessListResponse(
        processes=[
            ProcessSummary(
                process_id=d.process_id,
                version=d.version,
                name=d.name,
                status=ProcessStatus(d.status),
                element_count=d.element_count,
                flow_count=d.flow_count,
                tags=d.tags,
                deployed_at=d.deployed_at,
            )
            for d in deployed
        ],
        total=len(deployed),
    )


# ═══════════════════════════════════════════════════════════════════════
#  GET /processes/{process_id}
# ═══════════════════════════════════════════════════════════════════════

@router.get(
    "/{process_id}",
    response_model=DeployResponse,
    responses={404: {"model": ErrorResponse}},
    summary="Get process details",
)
async def get_process(process_id: str) -> DeployResponse:
    store = _get_store()
    deployed = await store.get_process(process_id)

    if deployed is None:
        raise HTTPException(status_code=404, detail={
            "error": "not_found", "detail": f"Process '{process_id}' not found",
        })

    return DeployResponse(
        process_id=deployed.process_id,
        version=deployed.version,
        name=deployed.name,
        status=ProcessStatus(deployed.status),
        element_count=deployed.element_count,
        flow_count=deployed.flow_count,
        warnings=deployed.warnings,
        deployed_at=deployed.deployed_at,
        bpmn_xml=deployed.bpmn_xml,
    )


# ═══════════════════════════════════════════════════════════════════════
#  POST /processes/{process_id}/start
# ═══════════════════════════════════════════════════════════════════════

@router.post(
    "/{process_id}/start",
    response_model=StartResponse,
    status_code=201,
    responses={404: {"model": ErrorResponse}},
    summary="Start a process instance",
)
async def start_instance(process_id: str, request: StartRequest) -> StartResponse:
    store = _get_store()
    deployed = await store.get_process(process_id)

    if deployed is None:
        raise HTTPException(status_code=404, detail={
            "error": "not_found", "detail": f"Process '{process_id}' not found",
        })

    if deployed.status != "active":
        raise HTTPException(status_code=409, detail={
            "error": "process_not_active",
            "detail": f"Process '{process_id}' is {deployed.status}",
        })

    instance = await store.create_instance(
        process_id=process_id,
        version=deployed.version,
        variables=request.variables,
        business_key=request.business_key,
    )

    # Execute via Temporal if connected, otherwise inline
    from helix_engine.temporal.client import is_connected

    workflow_id = f"helix-{process_id}-{instance.instance_id}"

    if is_connected():
        asyncio.create_task(
            _start_temporal_workflow(
                instance_id=instance.instance_id,
                workflow_id=workflow_id,
                process_data=deployed.compiled_ir,
                variables=request.variables,
                business_key=request.business_key,
                store=store,
            )
        )
    else:
        asyncio.create_task(
            _run_process_inline(
                instance_id=instance.instance_id,
                process_data=deployed.compiled_ir,
                variables=request.variables,
                store=store,
            )
        )

    return StartResponse(
        instance_id=instance.instance_id,
        process_id=instance.process_id,
        version=instance.version,
        status=InstanceStatus(instance.status),
        business_key=instance.business_key,
        started_at=instance.started_at,
    )


async def _start_temporal_workflow(
    instance_id: str,
    workflow_id: str,
    process_data: dict[str, Any],
    variables: dict,
    business_key: str | None,
    store: Any,
) -> None:
    """Start the process as a real Temporal workflow."""
    try:
        from helix_engine.temporal.client import get_client
        from helix_engine.temporal.workflows import ProcessWorkflow, WorkflowInput

        client = get_client()

        wf_input = WorkflowInput(
            process_data=process_data,
            variables=dict(variables),
            business_key=business_key,
        )

        handle = await client.start_workflow(
            ProcessWorkflow.run,
            wf_input.to_dict(),
            id=workflow_id,
            task_queue="helix-engine",
        )

        # Persist the workflow id so we can signal it later
        await store.update_instance(instance_id, temporal_workflow_id=handle.id)

        logger.info("temporal_workflow_started",
                     instance_id=instance_id, workflow_id=handle.id)

        result = await handle.result()

        if isinstance(result, dict):
            await store.update_instance(
                instance_id,
                status=InstanceStatus.COMPLETED if result.get("status") == "completed" else InstanceStatus.FAILED,
                variables=result.get("variables", {}),
                visited_elements=result.get("visited", []),
                error=result.get("error"),
            )

    except Exception as e:
        logger.error("temporal_workflow_failed",
                      instance_id=instance_id, error=str(e))
        await store.update_instance(instance_id, status=InstanceStatus.FAILED, error=str(e))


async def _run_process_inline(
    instance_id: str,
    process_data: dict[str, Any],
    variables: dict,
    store: Any,
) -> None:
    """Fallback: run without Temporal (no durability)."""
    try:
        from helix_engine.temporal.workflows import (
            ProcessWorkflow, WorkflowInput,
        )

        workflow = ProcessWorkflow()
        wf_input = WorkflowInput(process_data=process_data, variables=dict(variables))
        result = await workflow.run(wf_input.to_dict())

        if isinstance(result, dict):
            await store.update_instance(
                instance_id,
                status=InstanceStatus.COMPLETED if result.get("status") == "completed" else InstanceStatus.FAILED,
                variables=result.get("variables", {}),
                visited_elements=result.get("visited", []),
                error=result.get("error"),
            )

    except Exception as e:
        logger.error("inline_execution_failed",
                      instance_id=instance_id, error=str(e))
        await store.update_instance(instance_id, status=InstanceStatus.FAILED, error=str(e))


# ═══════════════════════════════════════════════════════════════════════
#  GET /processes/{process_id}/instances
# ═══════════════════════════════════════════════════════════════════════

@router.get(
    "/{process_id}/instances",
    response_model=InstanceListResponse,
    summary="List instances of a process",
)
async def list_instances(process_id: str) -> InstanceListResponse:
    store = _get_store()
    instances = await store.list_instances(process_id=process_id)

    return InstanceListResponse(
        instances=[
            InstanceSummary(
                instance_id=i.instance_id,
                process_id=i.process_id,
                version=i.version,
                status=InstanceStatus(i.status),
                business_key=i.business_key,
                started_at=i.started_at,
                completed_at=i.completed_at,
            )
            for i in instances
        ],
        total=len(instances),
    )


# ═══════════════════════════════════════════════════════════════════════
#  GET /processes/{process_id}/instances/{instance_id}
# ═══════════════════════════════════════════════════════════════════════

@router.get(
    "/{process_id}/instances/{instance_id}",
    response_model=InstanceStatusResponse,
    responses={404: {"model": ErrorResponse}},
    summary="Get instance status",
)
async def get_instance_status(process_id: str, instance_id: str) -> InstanceStatusResponse:
    store = _get_store()
    instance = await store.get_instance(instance_id)

    if instance is None or instance.process_id != process_id:
        raise HTTPException(status_code=404, detail={
            "error": "not_found",
            "detail": f"Instance '{instance_id}' not found for process '{process_id}'",
        })

    # Query Temporal for live pending user tasks when workflow is running
    pending_user_task = None
    if instance.temporal_workflow_id and instance.status == "running":
        try:
            from helix_engine.temporal.client import get_client, is_connected
            from helix_engine.temporal.workflows import ProcessWorkflow
            if is_connected():
                client = get_client()
                handle = client.get_workflow_handle(instance.temporal_workflow_id)
                state = await handle.query(ProcessWorkflow.get_state)
                pending = state.get("pending_user_tasks", [])
                if pending:
                    t = pending[0]
                    pending_user_task = PendingUserTask(
                        task_id=t.get("task_id", ""),
                        task_name=t.get("task_name"),
                        form_key=t.get("form_key"),
                    )
        except Exception:
            pass

    return InstanceStatusResponse(
        instance_id=instance.instance_id,
        process_id=instance.process_id,
        version=instance.version,
        status=InstanceStatus(instance.status),
        business_key=instance.business_key,
        variables=instance.variables,
        visited_elements=instance.visited_elements,
        pending_user_task=pending_user_task,
        error=instance.error,
        started_at=instance.started_at,
        completed_at=instance.completed_at,
    )


# ═══════════════════════════════════════════════════════════════════════
#  POST /processes/{process_id}/instances/{instance_id}/cancel
# ═══════════════════════════════════════════════════════════════════════

@router.post(
    "/{process_id}/instances/{instance_id}/cancel",
    response_model=InstanceStatusResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    summary="Cancel a running instance",
)
async def cancel_instance(process_id: str, instance_id: str) -> InstanceStatusResponse:
    store = _get_store()
    instance = await store.get_instance(instance_id)

    if instance is None or instance.process_id != process_id:
        raise HTTPException(status_code=404, detail={
            "error": "not_found",
            "detail": f"Instance '{instance_id}' not found for process '{process_id}'",
        })

    if instance.status != "running":
        raise HTTPException(status_code=409, detail={
            "error": "not_cancellable",
            "detail": f"Instance is {instance.status}, not running",
        })

    await store.update_instance(instance_id, status=InstanceStatus.CANCELLED)
    instance = await store.get_instance(instance_id)

    return InstanceStatusResponse(
        instance_id=instance.instance_id,
        process_id=instance.process_id,
        version=instance.version,
        status=InstanceStatus(instance.status),
        business_key=instance.business_key,
        variables=instance.variables,
        visited_elements=instance.visited_elements,
        error=instance.error,
        started_at=instance.started_at,
        completed_at=instance.completed_at,
    )


# ═══════════════════════════════════════════════════════════════════════
#  DELETE /processes/{process_id}
# ═══════════════════════════════════════════════════════════════════════

@router.delete(
    "/{process_id}",
    status_code=204,
    responses={404: {"model": ErrorResponse}},
    summary="Delete a deployed process and all its instances",
)
async def delete_process(process_id: str):
    """Delete a process definition and all its instances."""
    store = _get_store()
    deleted = await store.delete_process(process_id)
    if not deleted:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "detail": f"Process '{process_id}' not found"},
        )
    return None


# ═══════════════════════════════════════════════════════════════════════
#  POST /processes/{process_id}/instances/{instance_id}/complete-task
# ═══════════════════════════════════════════════════════════════════════

@router.post(
    "/{process_id}/instances/{instance_id}/complete-task",
    response_model=InstanceStatusResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    summary="Complete a pending user task by submitting form data",
)
async def complete_user_task(
    process_id: str,
    instance_id: str,
    request: CompleteTaskRequest,
) -> InstanceStatusResponse:
    """Send a user_task_completed signal to the Temporal workflow."""
    store = _get_store()
    instance = await store.get_instance(instance_id)

    if instance is None or instance.process_id != process_id:
        raise HTTPException(status_code=404, detail={
            "error": "not_found",
            "detail": f"Instance '{instance_id}' not found for process '{process_id}'",
        })

    if not instance.temporal_workflow_id:
        raise HTTPException(status_code=409, detail={
            "error": "no_workflow",
            "detail": "Instance has no associated Temporal workflow",
        })

    from helix_engine.temporal.client import get_client, is_connected
    from helix_engine.temporal.workflows import ProcessWorkflow

    if not is_connected():
        raise HTTPException(status_code=409, detail={
            "error": "temporal_unavailable",
            "detail": "Temporal is not connected",
        })

    try:
        client = get_client()
        handle = client.get_workflow_handle(instance.temporal_workflow_id)
        await handle.signal(
            ProcessWorkflow.on_user_task_completed,
            {"task_id": request.task_id, "variables": request.variables},
        )
        logger.info("user_task_completed_signal_sent",
                    instance_id=instance_id, task_id=request.task_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail={
            "error": "signal_failed",
            "detail": str(e),
        })

    instance = await store.get_instance(instance_id)
    return InstanceStatusResponse(
        instance_id=instance.instance_id,
        process_id=instance.process_id,
        version=instance.version,
        status=InstanceStatus(instance.status),
        business_key=instance.business_key,
        variables=instance.variables,
        visited_elements=instance.visited_elements,
        error=instance.error,
        started_at=instance.started_at,
        completed_at=instance.completed_at,
    )


# ═══════════════════════════════════════════════════════════════════════
#  POST /processes/{process_id}/schedules
# ═══════════════════════════════════════════════════════════════════════

@router.post(
    "/{process_id}/schedules",
    response_model=ScheduleResponse,
    status_code=201,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
    summary="Create a recurring schedule for this process",
)
async def create_schedule(process_id: str, request: ScheduleRequest) -> ScheduleResponse:
    """Create a Temporal schedule that starts process instances on a cron."""
    from helix_engine.temporal.client import get_client, is_connected
    from helix_engine.temporal.workflows import ProcessWorkflow, WorkflowInput

    store = _get_store()
    deployed = await store.get_process(process_id)
    if deployed is None:
        raise HTTPException(status_code=404, detail={
            "error": "not_found", "detail": f"Process '{process_id}' not found",
        })

    if not is_connected():
        raise HTTPException(status_code=409, detail={
            "error": "temporal_unavailable",
            "detail": "Temporal is not connected — schedules require Temporal",
        })

    try:
        import uuid as _uuid
        from temporalio.client import (
            Schedule,
            ScheduleActionStartWorkflow,
            ScheduleSpec,
            ScheduleIntervalSpec,
        )

        client = get_client()
        schedule_id = f"helix-sched-{process_id}-{str(_uuid.uuid4())[:8]}"
        wf_input = WorkflowInput(
            process_data=deployed.compiled_ir,
            variables=dict(request.variables),
        )

        await client.create_schedule(
            schedule_id,
            Schedule(
                action=ScheduleActionStartWorkflow(
                    ProcessWorkflow.run,
                    wf_input.to_dict(),
                    id=f"{schedule_id}-run",
                    task_queue="helix-engine",
                ),
                spec=ScheduleSpec(cron_expressions=[request.cron]),
            ),
        )
        logger.info("schedule_created", schedule_id=schedule_id, process_id=process_id)
        return ScheduleResponse(
            schedule_id=schedule_id,
            process_id=process_id,
            cron=request.cron,
            status="active",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail={
            "error": "schedule_creation_failed",
            "detail": str(e),
        })
