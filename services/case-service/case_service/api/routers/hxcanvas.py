"""P57 HxCanvas — Visual Whiteboard router."""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser
from case_service.db.session import get_session
from case_service.hxcanvas import service
from case_service.hxstream.emitter import emit_trace

router = APIRouter(prefix="/hxcanvas", tags=["hxcanvas"])


def _tenant(user: AuthenticatedUser) -> str:
    return getattr(user, "tenant_id", None) or "default"


def _actor(user: AuthenticatedUser) -> str:
    return (getattr(user, "username", None)
            or getattr(user, "email", None)
            or getattr(user, "user_id", None)
            or "system")


# ── Schemas ───────────────────────────────────────────────────────────────────

class BoardIn(BaseModel):
    name:        str
    description: str = ""
    case_id:     uuid.UUID | None = None


class BoardPatch(BaseModel):
    name:        str | None = None
    description: str | None = None


class BoardOut(BaseModel):
    id:          uuid.UUID
    name:        str
    description: str | None
    case_id:     uuid.UUID | None
    created_by:  str | None
    created_at:  str
    updated_at:  str
    item_count:  int = 0

    @classmethod
    def from_model(cls, b: Any, item_count: int = 0) -> "BoardOut":
        return cls(
            id=b.id, name=b.name, description=b.description,
            case_id=b.case_id, created_by=b.created_by,
            created_at=b.created_at.isoformat(),
            updated_at=b.updated_at.isoformat(),
            item_count=item_count,
        )


class ItemIn(BaseModel):
    type:    str
    x:       float = 0
    y:       float = 0
    width:   float = 120
    height:  float = 60
    data:    dict  = {}
    z_index: int   = 0


class ItemPatch(BaseModel):
    x:       float | None = None
    y:       float | None = None
    width:   float | None = None
    height:  float | None = None
    data:    dict  | None = None
    z_index: int   | None = None


class ItemOut(BaseModel):
    id:         uuid.UUID
    board_id:   uuid.UUID
    type:       str
    x:          float
    y:          float
    width:      float
    height:     float
    data:       dict
    z_index:    int
    created_by: str | None
    created_at: str
    updated_at: str

    @classmethod
    def from_model(cls, i: Any) -> "ItemOut":
        return cls(
            id=i.id, board_id=i.board_id, type=i.type,
            x=i.x, y=i.y, width=i.width, height=i.height,
            data=i.data or {}, z_index=i.z_index,
            created_by=i.created_by,
            created_at=i.created_at.isoformat(),
            updated_at=i.updated_at.isoformat(),
        )


class BoardDetail(BaseModel):
    id:          uuid.UUID
    name:        str
    description: str | None
    case_id:     uuid.UUID | None
    created_by:  str | None
    created_at:  str
    updated_at:  str
    items:       list[ItemOut]

    @classmethod
    def from_model(cls, b: Any, graph_data: dict | None = None) -> "BoardDetail":
        items_out = []
        for i in (b.items or []):
            item_out = ItemOut.from_model(i)
            if i.type == "graph_node_embed" and graph_data:
                node_id = str((i.data or {}).get("graph_node_id", ""))
                if node_id in (graph_data or {}):
                    item_out.data = {**i.data, "_live": graph_data[node_id]}
            items_out.append(item_out)
        return cls(
            id=b.id, name=b.name, description=b.description,
            case_id=b.case_id, created_by=b.created_by,
            created_at=b.created_at.isoformat(),
            updated_at=b.updated_at.isoformat(),
            items=items_out,
        )


class BulkIn(BaseModel):
    items: list[dict]


# ── Board endpoints ───────────────────────────────────────────────────────────

@router.get("/boards")
async def list_boards(
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    boards, counts = await service.list_boards_with_counts(session, _tenant(user))
    return [BoardOut.from_model(b, item_count=counts.get(b.id, 0)) for b in boards]


@router.post("/boards", status_code=201)
async def create_board(
    body: BoardIn,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    board = await service.create_board(
        session, _tenant(user), body.name, body.description, body.case_id, _actor(user),
    )
    await session.commit()
    return BoardOut.from_model(board)


@router.get("/boards/{board_id}")
async def get_board(
    board_id: uuid.UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    board = await service.get_board(session, board_id, _tenant(user))
    if not board:
        raise HTTPException(404, "Board not found")
    graph_data = await service.resolve_graph_embeds(session, board.items or [])
    return BoardDetail.from_model(board, graph_data)


@router.patch("/boards/{board_id}")
async def update_board(
    board_id: uuid.UUID,
    body: BoardPatch,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    board = await service.get_board(session, board_id, _tenant(user))
    if not board:
        raise HTTPException(404, "Board not found")
    board = await service.update_board(session, board, body.name, body.description)
    await session.commit()
    return BoardOut.from_model(board)


@router.delete("/boards/{board_id}", status_code=204)
async def delete_board(
    board_id: uuid.UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    board = await service.get_board(session, board_id, _tenant(user))
    if not board:
        raise HTTPException(404, "Board not found")
    await service.delete_board(session, board)
    await session.commit()


# ── Item endpoints ────────────────────────────────────────────────────────────

@router.post("/boards/{board_id}/items", status_code=201)
async def add_item(
    board_id: uuid.UUID,
    body: ItemIn,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    board = await service.get_board(session, board_id, _tenant(user))
    if not board:
        raise HTTPException(404, "Board not found")
    item = await service.create_item(
        session, board, body.type, body.x, body.y, body.width, body.height,
        body.data, body.z_index, _actor(user),
    )
    await session.commit()
    out = ItemOut.from_model(item)
    await emit_trace("canvas.item_created", {"board_id": str(board_id), "item": out.model_dump(mode="json")},
                     tenant_id=_tenant(user), actor_user_id=_actor(user))
    return out


@router.patch("/boards/{board_id}/items/{item_id}")
async def update_item(
    board_id: uuid.UUID,
    item_id: uuid.UUID,
    body: ItemPatch,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    board = await service.get_board(session, board_id, _tenant(user))
    if not board:
        raise HTTPException(404, "Board not found")
    item = await service.get_item(session, item_id, board_id)
    if not item:
        raise HTTPException(404, "Item not found")
    item = await service.update_item(
        session, item, body.x, body.y, body.width, body.height, body.data, body.z_index,
    )
    await session.commit()
    out = ItemOut.from_model(item)
    await emit_trace("canvas.item_updated", {"board_id": str(board_id), "item": out.model_dump(mode="json")},
                     tenant_id=_tenant(user), actor_user_id=_actor(user))
    return out


@router.delete("/boards/{board_id}/items/{item_id}", status_code=204)
async def delete_item(
    board_id: uuid.UUID,
    item_id: uuid.UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    board = await service.get_board(session, board_id, _tenant(user))
    if not board:
        raise HTTPException(404, "Board not found")
    item = await service.get_item(session, item_id, board_id)
    if not item:
        raise HTTPException(404, "Item not found")
    await service.delete_item(session, item)
    await session.commit()
    await emit_trace("canvas.item_deleted", {"board_id": str(board_id), "item_id": str(item_id)},
                     tenant_id=_tenant(user), actor_user_id=_actor(user))


@router.post("/boards/{board_id}/items/bulk")
async def bulk_upsert(
    board_id: uuid.UUID,
    body: BulkIn,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    board = await service.get_board(session, board_id, _tenant(user))
    if not board:
        raise HTTPException(404, "Board not found")
    items = await service.bulk_upsert_items(session, board, body.items, _actor(user))
    await session.commit()
    out = [ItemOut.from_model(i) for i in items]
    await emit_trace("canvas.bulk_updated", {"board_id": str(board_id), "count": len(out)},
                     tenant_id=_tenant(user), actor_user_id=_actor(user))
    return out


# ── P57b: Graph node search ───────────────────────────────────────────────────

@router.get("/graph-nodes/search")
async def search_graph_nodes(
    q: str = Query(..., min_length=1),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    return await service.search_graph_nodes(session, q, _tenant(user))


# ── P57d: BPMN export ─────────────────────────────────────────────────────────

def _build_bpmn_xml(board_name: str, items: list) -> str:
    """Deterministic BPMN 2.0 XML generator from canvas items. No LLM required."""
    from xml.sax.saxutils import escape as xe

    flow_nodes = [i for i in items if i.type not in ("connector", "freehand")]
    connectors  = [i for i in items if i.type == "connector"]

    # Stable short IDs
    id_map: dict[str, str] = {}
    for idx, item in enumerate(flow_nodes):
        id_map[str(item.id)] = f"node_{idx}"

    def label(item) -> str:
        d = item.data or {}
        return xe(d.get("content") or d.get("label") or d.get("node_label") or item.type.replace("_", " ").title())

    def bpmn_element(item) -> str:
        nid = id_map[str(item.id)]
        lbl = label(item)
        if item.type == "shape" and item.data.get("shape_type") == "diamond":
            return f'    <exclusiveGateway id="{nid}" name="{lbl}" />'
        return f'    <task id="{nid}" name="{lbl}" />'

    # Build sequence flows from connectors
    flows: list[str] = []
    shapes: list[str] = []
    for idx, c in enumerate(connectors):
        src = id_map.get(str(c.data.get("from_item_id", "")))
        tgt = id_map.get(str(c.data.get("to_item_id", "")))
        if src and tgt:
            lbl = xe(c.data.get("label") or "")
            name_attr = f' name="{lbl}"' if lbl else ""
            flows.append(f'    <sequenceFlow id="flow_{idx}" sourceRef="{src}" targetRef="{tgt}"{name_attr} />')

    # Add start/end events if no connector covers them
    connected_src = {id_map.get(str(c.data.get("from_item_id", ""))) for c in connectors}
    connected_tgt = {id_map.get(str(c.data.get("to_item_id", ""))) for c in connectors}
    all_node_ids   = set(id_map.values())

    start_targets = all_node_ids - connected_tgt  # nodes with no incoming
    end_sources   = all_node_ids - connected_src   # nodes with no outgoing
    for i, nid in enumerate(sorted(start_targets)):
        sid = f"start_{i}"
        flows.append(f'    <sequenceFlow id="autostart_flow_{i}" sourceRef="{sid}" targetRef="{nid}" />')
    for i, nid in enumerate(sorted(end_sources)):
        eid = f"end_{i}"
        flows.append(f'    <sequenceFlow id="autoend_flow_{i}" sourceRef="{nid}" targetRef="{eid}" />')

    # BPMNDI shapes
    for item in flow_nodes:
        nid = id_map[str(item.id)]
        x, y, w, h = int(item.x), int(item.y), int(item.width), int(item.height)
        shapes.append(
            f'      <bpmndi:BPMNShape id="shape_{nid}" bpmnElement="{nid}">\n'
            f'        <dc:Bounds x="{x}" y="{y}" width="{w}" height="{h}" />\n'
            f'      </bpmndi:BPMNShape>'
        )
    for i, nid in enumerate(sorted(start_targets)):
        shapes.insert(0,
            f'      <bpmndi:BPMNShape id="shape_start_{i}" bpmnElement="start_{i}">\n'
            f'        <dc:Bounds x="50" y="50" width="36" height="36" />\n'
            f'      </bpmndi:BPMNShape>'
        )
    for i, nid in enumerate(sorted(end_sources)):
        shapes.append(
            f'      <bpmndi:BPMNShape id="shape_end_{i}" bpmnElement="end_{i}">\n'
            f'        <dc:Bounds x="900" y="50" width="36" height="36" />\n'
            f'      </bpmndi:BPMNShape>'
        )

    start_events = "\n".join(
        f'    <startEvent id="start_{i}" name="Start" />'
        for i in range(len(start_targets))
    )
    end_events = "\n".join(
        f'    <endEvent id="end_{i}" name="End" />'
        for i in range(len(end_sources))
    )
    node_elements = "\n".join(bpmn_element(i) for i in flow_nodes)
    flow_elements = "\n".join(flows)
    shape_elements = "\n".join(shapes)

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/MODEL"
             xmlns:bpmndi="http://www.omg.org/spec/BPMN/20100524/DI"
             xmlns:dc="http://www.omg.org/spec/DD/20100524/DC"
             xmlns:di="http://www.omg.org/spec/DD/20100524/DI"
             id="def_{board_name[:8].replace(' ','_')}"
             targetNamespace="http://helix.io/canvas"
             name="{xe(board_name)}">
  <process id="process_1" name="{xe(board_name)}" isExecutable="false">
{start_events}
{node_elements}
{end_events}
{flow_elements}
  </process>
  <bpmndi:BPMNDiagram id="diagram_1">
    <bpmndi:BPMNPlane id="plane_1" bpmnElement="process_1">
{shape_elements}
    </bpmndi:BPMNPlane>
  </bpmndi:BPMNDiagram>
</definitions>"""


@router.post("/boards/{board_id}/export/bpmn")
async def export_bpmn(
    board_id: uuid.UUID,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    board = await service.get_board(session, board_id, _tenant(user))
    if not board:
        raise HTTPException(404, "Board not found")

    items = board.items or []
    bpmn_xml = _build_bpmn_xml(board.name, items)
    return {"bpmn_xml": bpmn_xml, "board_name": board.name}
