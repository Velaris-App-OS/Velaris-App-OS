"""P56 HxWork — Kanban + Sprint Board service."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from case_service.db.models import (
    CaseInstanceModel,
    CaseTypeModel,
    HxWorkBoardModel,
    HxWorkCardRelationModel,
    HxWorkSprintCardModel,
    HxWorkSprintModel,
)

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Boards ────────────────────────────────────────────────────────────────────

async def create_board(
    session: AsyncSession,
    tenant_id: str,
    name: str,
    case_type_id: uuid.UUID | None,
    description: str,
    created_by: str,
) -> HxWorkBoardModel:
    column_config: list = []
    if case_type_id:
        ct = await session.get(CaseTypeModel, case_type_id)
        if ct:
            stages = (ct.definition_json or {}).get("stages", [])
            column_config = [
                {"stage_id": s["id"], "label": s.get("name", s["id"]), "wip_limit": 0}
                for s in stages
            ]

    board = HxWorkBoardModel(
        tenant_id=tenant_id, name=name, description=description,
        case_type_id=case_type_id, column_config=column_config,
        created_by=created_by,
    )
    session.add(board)
    await session.flush()
    return board


async def list_boards(session: AsyncSession, tenant_id: str) -> list[HxWorkBoardModel]:
    rows = (await session.execute(
        select(HxWorkBoardModel)
        .where(HxWorkBoardModel.tenant_id == tenant_id)
        .order_by(HxWorkBoardModel.created_at.desc())
    )).scalars().all()
    return list(rows)


async def get_board(session: AsyncSession, board_id: uuid.UUID) -> HxWorkBoardModel | None:
    return (await session.execute(
        select(HxWorkBoardModel)
        .where(HxWorkBoardModel.id == board_id)
        .options(selectinload(HxWorkBoardModel.sprints))
    )).scalar_one_or_none()


async def get_board_cards(
    session: AsyncSession, board: HxWorkBoardModel, sprint_id: uuid.UUID | None = None
) -> dict[str, list[dict]]:
    """Return cards grouped by stage_id. If sprint_id given, only those cards."""
    if not board.case_type_id:
        return {}

    q = select(CaseInstanceModel).where(CaseInstanceModel.case_type_id == board.case_type_id)

    if sprint_id:
        sprint_case_ids = (await session.execute(
            select(HxWorkSprintCardModel.case_id).where(HxWorkSprintCardModel.sprint_id == sprint_id)
        )).scalars().all()
        if not sprint_case_ids:
            return {}
        q = q.where(CaseInstanceModel.id.in_(sprint_case_ids))

    cases = (await session.execute(q.limit(500))).scalars().all()

    # Build sprint points map
    points_map: dict[str, int] = {}
    if sprint_id:
        sc_rows = (await session.execute(
            select(HxWorkSprintCardModel).where(HxWorkSprintCardModel.sprint_id == sprint_id)
        )).scalars().all()
        points_map = {str(sc.case_id): sc.story_points for sc in sc_rows}

    # Group by current_stage_id
    columns: dict[str, list[dict]] = {}
    for col in board.column_config:
        columns[col["stage_id"]] = []

    # case_vars façade read (blob fallback included) — one bulk call, no N+1
    from case_service.case_vars import service as case_vars
    vars_ctx = case_vars.CallerContext(kind="platform", actor_id="hxwork")
    vars_by_case = await case_vars.get_all_bulk(session, vars_ctx, [c.id for c in cases])

    backlog: list[dict] = []
    for case in cases:
        card = _case_to_card(case, points_map, vars_by_case.get(case.id, {}))
        stage = case.current_stage_id
        if stage and stage in columns:
            columns[stage].append(card)
        else:
            backlog.append(card)

    if backlog:
        columns["__backlog__"] = backlog
    return columns


def _case_to_card(case: CaseInstanceModel, points_map: dict = {}, data: dict | None = None) -> dict:
    data = data if data is not None else {}
    title = (data.get("title") or data.get("subject") or data.get("name")
             or (f"#{case.case_number}" if case.case_number else f"Case {str(case.id)[:8]}"))
    return {
        "id":               str(case.id),
        "title":            title,
        "case_number":      case.case_number,
        "status":           case.status,
        "priority":         case.priority,
        "current_stage_id": case.current_stage_id,
        "assigned_to":      data.get("assigned_to") or case.created_by,
        "created_at":       case.created_at.isoformat() if case.created_at else None,
        "story_points":     points_map.get(str(case.id), 0),
    }


# ── Sprints ───────────────────────────────────────────────────────────────────

async def create_sprint(
    session: AsyncSession,
    board_id: uuid.UUID,
    tenant_id: str,
    name: str,
    goal: str,
    start_date: datetime | None,
    end_date: datetime | None,
) -> HxWorkSprintModel:
    sprint = HxWorkSprintModel(
        board_id=board_id, tenant_id=tenant_id, name=name, goal=goal,
        start_date=start_date, end_date=end_date, status="planned",
    )
    session.add(sprint)
    await session.flush()
    return sprint


async def start_sprint(session: AsyncSession, sprint_id: uuid.UUID) -> HxWorkSprintModel:
    sprint = await session.get(HxWorkSprintModel, sprint_id)
    if not sprint:
        raise ValueError("Sprint not found")
    if sprint.status != "planned":
        raise ValueError(f"Sprint is already {sprint.status}")
    sprint.status = "active"
    if not sprint.start_date:
        sprint.start_date = _utcnow()
    await session.flush()
    return sprint


async def complete_sprint(session: AsyncSession, sprint_id: uuid.UUID) -> HxWorkSprintModel:
    sprint = await session.get(HxWorkSprintModel, sprint_id)
    if not sprint:
        raise ValueError("Sprint not found")
    sprint.status       = "completed"
    sprint.completed_at = _utcnow()

    # Calculate velocity: sum story points of done cases
    cards = (await session.execute(
        select(HxWorkSprintCardModel).where(HxWorkSprintCardModel.sprint_id == sprint_id)
    )).scalars().all()

    done_points = 0
    for sc in cards:
        case = await session.get(CaseInstanceModel, sc.case_id)
        if case and case.status in ("resolved", "closed", "completed"):
            done_points += sc.story_points

    sprint.velocity = done_points
    await session.flush()
    return sprint


async def add_card_to_sprint(
    session: AsyncSession,
    sprint_id: uuid.UUID,
    case_id: uuid.UUID,
    story_points: int,
) -> HxWorkSprintCardModel:
    existing = await session.get(HxWorkSprintCardModel, (sprint_id, case_id))
    if existing:
        existing.story_points = story_points
        return existing
    sc = HxWorkSprintCardModel(sprint_id=sprint_id, case_id=case_id, story_points=story_points)
    session.add(sc)
    await session.flush()
    return sc


async def remove_card_from_sprint(session: AsyncSession, sprint_id: uuid.UUID, case_id: uuid.UUID) -> None:
    sc = await session.get(HxWorkSprintCardModel, (sprint_id, case_id))
    if sc:
        await session.delete(sc)


async def list_sprints(session: AsyncSession, board_id: uuid.UUID) -> list[HxWorkSprintModel]:
    rows = (await session.execute(
        select(HxWorkSprintModel)
        .where(HxWorkSprintModel.board_id == board_id)
        .order_by(HxWorkSprintModel.created_at.desc())
        .options(selectinload(HxWorkSprintModel.cards))
    )).scalars().all()
    return list(rows)


# ── Card relations ────────────────────────────────────────────────────────────

async def add_relation(
    session: AsyncSession,
    board_id: uuid.UUID,
    from_case_id: uuid.UUID,
    to_case_id: uuid.UUID,
    relation_type: str,
) -> HxWorkCardRelationModel:
    rel = HxWorkCardRelationModel(
        board_id=board_id, from_case_id=from_case_id,
        to_case_id=to_case_id, relation_type=relation_type,
    )
    session.add(rel)
    await session.flush()
    return rel


async def list_relations(session: AsyncSession, board_id: uuid.UUID) -> list[HxWorkCardRelationModel]:
    rows = (await session.execute(
        select(HxWorkCardRelationModel).where(HxWorkCardRelationModel.board_id == board_id)
    )).scalars().all()
    return list(rows)


# ── Analytics ─────────────────────────────────────────────────────────────────

async def board_analytics(session: AsyncSession, board_id: uuid.UUID) -> dict:
    board = await get_board(session, board_id)
    if not board:
        return {}
    if not board.case_type_id:
        sprints = board.sprints or []
        return {"total_cards": 0, "by_status": {}, "by_priority": {}, "total_sprints": len(sprints), "active_sprint": None, "velocity_history": []}

    cases = (await session.execute(
        select(CaseInstanceModel).where(CaseInstanceModel.case_type_id == board.case_type_id)
    )).scalars().all()

    by_status: dict[str, int] = {}
    by_priority: dict[str, int] = {}
    for case in cases:
        by_status[case.status] = by_status.get(case.status, 0) + 1
        by_priority[case.priority or "medium"] = by_priority.get(case.priority or "medium", 0) + 1

    sprints = board.sprints or []
    active = next((s for s in sprints if s.status == "active"), None)

    sprint_velocity = [
        {"name": s.name, "velocity": s.velocity or 0}
        for s in sprints if s.status == "completed"
    ]

    return {
        "total_cards":   len(cases),
        "by_status":     by_status,
        "by_priority":   by_priority,
        "total_sprints": len(sprints),
        "active_sprint": {"id": str(active.id), "name": active.name} if active else None,
        "velocity_history": sprint_velocity,
    }


async def ask_nexus(board_id: uuid.UUID, question: str, analytics: dict) -> str:
    prompt = (
        f"You are analysing an HxWork project board.\n"
        f"Board ID: {board_id}\n"
        f"Analytics: {analytics}\n"
        f"Question: {question}\n\n"
        f"Give a concise, actionable answer in 2-4 sentences."
    )
    try:
        from case_service.hxnexus.factory import generate_json
        result = await generate_json(prompt, system="You are an Agile project management AI assistant.")
        if result and "answer" in result:
            return result["answer"]
        if result and "response" in result:
            return result["response"]
    except Exception as exc:
        logger.warning("HxNexus board query failed: %s", exc)
    return "HxNexus is not available. Check analytics data for insights."
