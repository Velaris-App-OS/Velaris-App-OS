"""P47 HxFusion — Adaptive Execution Engine API."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import (
    ProcessDefinitionModel, ProcessInstanceModel,
    ProcessCaseBindingModel, ProcessTaskLogModel,
)
from case_service.auth.dependencies import require_role
from case_service.db.session import get_session
from case_service.api.schemas.hxfusion import (
    ProcessDefinitionCreate, ProcessDefinitionUpdate, ProcessDefinitionOut,
    ProcessInstanceOut, StartProcessRequest, ResumeRequest,
    ProcessCaseBindingOut, ProcessTaskLogOut,
    AIDirectorRequest, AIDirectorResponse,
)

router = APIRouter(prefix="/fusion", tags=["HxFusion"], dependencies=[Depends(require_role("integration", "admin", "designer"))])


# ── Process Definitions ───────────────────────────────────────────────────────

@router.get("/definitions", response_model=list[ProcessDefinitionOut])
async def list_definitions(
    status: str | None = None,
    case_type_id: str | None = None,
    tenant_id: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    q = select(ProcessDefinitionModel)
    if status:
        q = q.where(ProcessDefinitionModel.status == status)
    if case_type_id:
        q = q.where(ProcessDefinitionModel.case_type_id == case_type_id)
    if tenant_id:
        q = q.where(ProcessDefinitionModel.tenant_id == tenant_id)
    result = await session.execute(q.order_by(ProcessDefinitionModel.created_at.desc()))
    return result.scalars().all()


@router.post("/definitions", response_model=ProcessDefinitionOut, status_code=201)
async def create_definition(
    body: ProcessDefinitionCreate,
    session: AsyncSession = Depends(get_session),
):
    # Auto-increment version for same name + tenant
    from sqlalchemy import func
    ver_q = await session.execute(
        select(func.max(ProcessDefinitionModel.version))
        .where(ProcessDefinitionModel.name == body.name)
        .where(ProcessDefinitionModel.tenant_id == body.tenant_id)
    )
    max_ver = ver_q.scalar() or 0

    defn = ProcessDefinitionModel(**body.model_dump(), version=max_ver + 1)
    session.add(defn)
    await session.commit()
    await session.refresh(defn)
    return defn


@router.get("/definitions/{defn_id}", response_model=ProcessDefinitionOut)
async def get_definition(defn_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    defn = await session.get(ProcessDefinitionModel, defn_id)
    if not defn:
        raise HTTPException(404, "Definition not found")
    return defn


@router.patch("/definitions/{defn_id}", response_model=ProcessDefinitionOut)
async def update_definition(
    defn_id: uuid.UUID,
    body: ProcessDefinitionUpdate,
    session: AsyncSession = Depends(get_session),
):
    defn = await session.get(ProcessDefinitionModel, defn_id)
    if not defn:
        raise HTTPException(404, "Definition not found")
    for k, v in body.model_dump(exclude_none=True).items():
        setattr(defn, k, v)
    await session.commit()
    await session.refresh(defn)
    return defn


@router.delete("/definitions/{defn_id}", status_code=204)
async def delete_definition(defn_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    defn = await session.get(ProcessDefinitionModel, defn_id)
    if not defn:
        raise HTTPException(404, "Definition not found")
    await session.delete(defn)
    await session.commit()


# ── BPMN XML validation ───────────────────────────────────────────────────────

@router.post("/definitions/validate")
async def validate_bpmn(body: dict):
    from case_service.hxfusion.parser import parse
    bpmn_xml = body.get("bpmn_xml", "")
    try:
        process = parse(bpmn_xml)
        return {
            "valid": True,
            "process_id": process.id,
            "process_name": process.name,
            "node_count": len(process.nodes),
            "flow_count": len(process.flows),
            "start_events": process.start_events,
            "node_types": list({n.node_type for n in process.nodes.values()}),
        }
    except Exception as exc:
        return {"valid": False, "error": str(exc)}


# ── Process Instances ─────────────────────────────────────────────────────────

@router.get("/instances", response_model=list[ProcessInstanceOut])
async def list_instances(
    status: str | None = None,
    case_id: uuid.UUID | None = None,
    definition_id: uuid.UUID | None = None,
    limit: int = Query(50, le=200),
    session: AsyncSession = Depends(get_session),
):
    q = select(ProcessInstanceModel)
    if status:
        q = q.where(ProcessInstanceModel.status == status)
    if case_id:
        q = q.where(ProcessInstanceModel.case_id == case_id)
    if definition_id:
        q = q.where(ProcessInstanceModel.definition_id == definition_id)
    result = await session.execute(q.order_by(ProcessInstanceModel.started_at.desc()).limit(limit))
    return result.scalars().all()


@router.post("/instances", response_model=ProcessInstanceOut, status_code=201)
async def start_instance(
    body: StartProcessRequest,
    session: AsyncSession = Depends(get_session),
):
    from case_service.hxfusion.engine import start_instance as _start
    try:
        instance = await _start(
            definition_id=body.definition_id,
            case_id=body.case_id,
            context=body.context,
            tenant_id=body.tenant_id,
            stage_id=body.stage_id,
            step_id=body.step_id,
            session=session,
        )
        await session.commit()
        await session.refresh(instance)
        return instance
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get("/instances/{instance_id}", response_model=ProcessInstanceOut)
async def get_instance(instance_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    inst = await session.get(ProcessInstanceModel, instance_id)
    if not inst:
        raise HTTPException(404, "Instance not found")
    return inst


@router.post("/instances/{instance_id}/resume", response_model=ProcessInstanceOut)
async def resume_instance(
    instance_id: uuid.UUID,
    body: ResumeRequest,
    session: AsyncSession = Depends(get_session),
):
    from case_service.hxfusion.engine import resume_instance as _resume
    try:
        inst = await _resume(
            instance_id=instance_id,
            resolution=body.resolution,
            resumed_by=body.resumed_by,
            session=session,
        )
        await session.commit()
        await session.refresh(inst)
        return inst
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/instances/{instance_id}/cancel", response_model=ProcessInstanceOut)
async def cancel_instance(
    instance_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
):
    inst = await session.get(ProcessInstanceModel, instance_id)
    if not inst:
        raise HTTPException(404, "Instance not found")
    if inst.status not in ("running", "suspended", "failed"):
        raise HTTPException(400, f"Cannot cancel instance in status '{inst.status}'")
    from case_service.hxfusion.engine import STATUS_CANCELLED
    from case_service.hxfusion.engine import _resolve_bindings
    inst.status = STATUS_CANCELLED
    await _resolve_bindings(session, inst)
    await session.commit()
    await session.refresh(inst)
    return inst


# ── Task Log ──────────────────────────────────────────────────────────────────

@router.get("/instances/{instance_id}/log", response_model=list[ProcessTaskLogOut])
async def get_task_log(instance_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(ProcessTaskLogModel)
        .where(ProcessTaskLogModel.instance_id == instance_id)
        .order_by(ProcessTaskLogModel.started_at)
    )
    return result.scalars().all()


# ── Case Bindings ─────────────────────────────────────────────────────────────

@router.get("/bindings", response_model=list[ProcessCaseBindingOut])
async def list_bindings(
    case_id: uuid.UUID | None = None,
    instance_id: uuid.UUID | None = None,
    status: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    q = select(ProcessCaseBindingModel)
    if case_id:
        q = q.where(ProcessCaseBindingModel.case_id == case_id)
    if instance_id:
        q = q.where(ProcessCaseBindingModel.instance_id == instance_id)
    if status:
        q = q.where(ProcessCaseBindingModel.status == status)
    result = await session.execute(q.order_by(ProcessCaseBindingModel.created_at.desc()))
    return result.scalars().all()


# ── AI Director ───────────────────────────────────────────────────────────────

@router.post("/director/advise", response_model=AIDirectorResponse)
async def director_advise(
    body: AIDirectorRequest,
    session: AsyncSession = Depends(get_session),
):
    from case_service.hxfusion.director import advise

    # Fetch available definitions for this case type
    q = select(ProcessDefinitionModel).where(ProcessDefinitionModel.status == "active")
    if body.case_type_id:
        from sqlalchemy import or_
        q = q.where(or_(
            ProcessDefinitionModel.case_type_id == body.case_type_id,
            ProcessDefinitionModel.case_type_id.is_(None),
        ))
    result = await session.execute(q.limit(20))
    definitions = [
        {"id": str(d.id), "name": d.name, "case_type_id": d.case_type_id}
        for d in result.scalars().all()
    ]

    advice = await advise(
        case_id=body.case_id,
        stage_id=body.stage_id,
        case_type_id=body.case_type_id,
        context=body.context,
        available_definitions=definitions,
        session=session,
    )
    return AIDirectorResponse(**advice)


# ── Stats ─────────────────────────────────────────────────────────────────────

@router.get("/stats")
async def stats(session: AsyncSession = Depends(get_session)):
    from sqlalchemy import func
    total_defs = await session.execute(select(func.count()).select_from(ProcessDefinitionModel))
    by_status = await session.execute(
        select(ProcessInstanceModel.status, func.count().label("c"))
        .group_by(ProcessInstanceModel.status)
    )
    total_tasks = await session.execute(select(func.count()).select_from(ProcessTaskLogModel))
    return {
        "total_definitions": total_defs.scalar(),
        "total_tasks_executed": total_tasks.scalar(),
        "instances_by_status": {row.status: row.c for row in by_status},
    }
