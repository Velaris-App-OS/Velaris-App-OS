"""P57 HxCanvas — Visual Whiteboard service."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, or_, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from case_service.db.models import HxCanvasBoardModel, HxCanvasItemModel, GraphNodeModel

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Boards ────────────────────────────────────────────────────────────────────

async def list_boards(session: AsyncSession, tenant_id: str) -> list[HxCanvasBoardModel]:
    rows = (await session.execute(
        select(HxCanvasBoardModel)
        .where(HxCanvasBoardModel.tenant_id == tenant_id)
        .order_by(HxCanvasBoardModel.updated_at.desc())
    )).scalars().all()
    return list(rows)


async def list_boards_with_counts(
    session: AsyncSession,
    tenant_id: str,
) -> tuple[list[HxCanvasBoardModel], dict]:
    boards = await list_boards(session, tenant_id)
    if not boards:
        return boards, {}
    board_ids = [b.id for b in boards]
    count_rows = (await session.execute(
        select(HxCanvasItemModel.board_id, func.count(HxCanvasItemModel.id).label("cnt"))
        .where(HxCanvasItemModel.board_id.in_(board_ids))
        .group_by(HxCanvasItemModel.board_id)
    )).all()
    counts = {row.board_id: row.cnt for row in count_rows}
    return boards, counts


async def create_board(
    session: AsyncSession,
    tenant_id: str,
    name: str,
    description: str,
    case_id: uuid.UUID | None,
    created_by: str,
) -> HxCanvasBoardModel:
    board = HxCanvasBoardModel(
        tenant_id=tenant_id, name=name, description=description,
        case_id=case_id, created_by=created_by,
    )
    session.add(board)
    await session.flush()
    return board


async def get_board(
    session: AsyncSession,
    board_id: uuid.UUID,
    tenant_id: str,
) -> HxCanvasBoardModel | None:
    result = await session.execute(
        select(HxCanvasBoardModel)
        .options(selectinload(HxCanvasBoardModel.items))
        .where(
            HxCanvasBoardModel.id == board_id,
            HxCanvasBoardModel.tenant_id == tenant_id,
        )
    )
    return result.scalar_one_or_none()


async def update_board(
    session: AsyncSession,
    board: HxCanvasBoardModel,
    name: str | None,
    description: str | None,
) -> HxCanvasBoardModel:
    if name is not None:
        board.name = name
    if description is not None:
        board.description = description
    board.updated_at = _utcnow()
    await session.flush()
    return board


async def delete_board(session: AsyncSession, board: HxCanvasBoardModel) -> None:
    await session.delete(board)
    await session.flush()


# ── Items ─────────────────────────────────────────────────────────────────────

async def create_item(
    session: AsyncSession,
    board: HxCanvasBoardModel,
    item_type: str,
    x: float,
    y: float,
    width: float,
    height: float,
    data: dict,
    z_index: int,
    created_by: str,
) -> HxCanvasItemModel:
    item = HxCanvasItemModel(
        board_id=board.id,
        tenant_id=board.tenant_id,
        type=item_type,
        x=x, y=y, width=width, height=height,
        data=data, z_index=z_index,
        created_by=created_by,
    )
    session.add(item)
    board.updated_at = _utcnow()
    await session.flush()
    return item


async def get_item(
    session: AsyncSession,
    item_id: uuid.UUID,
    board_id: uuid.UUID,
) -> HxCanvasItemModel | None:
    result = await session.execute(
        select(HxCanvasItemModel).where(
            HxCanvasItemModel.id == item_id,
            HxCanvasItemModel.board_id == board_id,
        )
    )
    return result.scalar_one_or_none()


async def update_item(
    session: AsyncSession,
    item: HxCanvasItemModel,
    x: float | None,
    y: float | None,
    width: float | None,
    height: float | None,
    data: dict | None,
    z_index: int | None,
) -> HxCanvasItemModel:
    if x is not None:
        item.x = x
    if y is not None:
        item.y = y
    if width is not None:
        item.width = width
    if height is not None:
        item.height = height
    if data is not None:
        item.data = data
    if z_index is not None:
        item.z_index = z_index
    item.updated_at = _utcnow()
    await session.flush()
    return item


async def delete_item(session: AsyncSession, item: HxCanvasItemModel) -> None:
    await session.delete(item)
    await session.flush()


async def bulk_upsert_items(
    session: AsyncSession,
    board: HxCanvasBoardModel,
    items_data: list[dict],
    actor: str,
) -> list[HxCanvasItemModel]:
    result = []
    for d in items_data:
        item_id = d.get("id")
        if item_id:
            existing = await get_item(session, uuid.UUID(str(item_id)), board.id)
            if existing:
                await update_item(
                    session, existing,
                    d.get("x"), d.get("y"), d.get("width"), d.get("height"),
                    d.get("data"), d.get("z_index"),
                )
                result.append(existing)
                continue
        item = await create_item(
            session, board,
            item_type=d.get("type", "sticky_note"),
            x=d.get("x", 0), y=d.get("y", 0),
            width=d.get("width", 120), height=d.get("height", 60),
            data=d.get("data", {}),
            z_index=d.get("z_index", 0),
            created_by=actor,
        )
        result.append(item)
    board.updated_at = _utcnow()
    await session.flush()
    return result


# ── P57b: HxGraph node search + embed resolution ──────────────────────────────

async def search_graph_nodes(
    session: AsyncSession,
    q: str,
    tenant_id: str,
    limit: int = 20,
) -> list[dict]:
    stmt = (
        select(GraphNodeModel)
        .where(
            or_(
                GraphNodeModel.name.ilike(f"%{q}%"),
                GraphNodeModel.label.ilike(f"%{q}%"),
            ),
            or_(GraphNodeModel.tenant_id == tenant_id, GraphNodeModel.tenant_id.is_(None)),
        )
        .order_by(GraphNodeModel.name)
        .limit(limit)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            "id": str(n.id),
            "node_type": n.node_type,
            "name": n.name,
            "label": n.label,
            "summary": n.summary,
        }
        for n in rows
    ]


async def resolve_graph_embeds(
    session: AsyncSession,
    items: list[HxCanvasItemModel],
) -> dict[str, dict]:
    """Return live graph node data keyed by node id for all graph_node_embed items."""
    node_ids = [
        uuid.UUID(str(item.data["graph_node_id"]))
        for item in items
        if item.type == "graph_node_embed" and item.data.get("graph_node_id")
    ]
    if not node_ids:
        return {}
    rows = (await session.execute(
        select(GraphNodeModel).where(GraphNodeModel.id.in_(node_ids))
    )).scalars().all()
    return {
        str(n.id): {
            "name": n.name,
            "label": n.label,
            "node_type": n.node_type,
            "summary": n.summary,
            "properties": n.properties or {},
        }
        for n in rows
    }
