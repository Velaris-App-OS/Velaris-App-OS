"""Tests for P57 HxCanvas — Visual Whiteboard."""
from __future__ import annotations

import uuid
import pytest
from httpx import AsyncClient

from case_service.db.models import HxCanvasBoardModel, HxCanvasItemModel

from tests.conftest import client, session  # type: ignore[attr-defined]


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _board(client: AsyncClient, name: str = "Test Canvas", description: str = "") -> dict:
    r = await client.post("/api/v1/hxcanvas/boards", json={"name": name, "description": description})
    assert r.status_code == 201, r.text
    return r.json()


async def _item(client: AsyncClient, board_id: str, item_type: str = "sticky_note", **kwargs) -> dict:
    payload = {
        "type": item_type,
        "x": kwargs.get("x", 100),
        "y": kwargs.get("y", 100),
        "width": kwargs.get("width", 160),
        "height": kwargs.get("height", 120),
        "data": kwargs.get("data", {"color": "#fde68a", "content": "Hello"}),
        "z_index": kwargs.get("z_index", 0),
    }
    r = await client.post(f"/api/v1/hxcanvas/boards/{board_id}/items", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


# ── Board CRUD ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_board(client: AsyncClient):
    r = await client.post("/api/v1/hxcanvas/boards", json={"name": "My Canvas", "description": "Architecture diagram"})
    assert r.status_code == 201
    data = r.json()
    assert data["name"] == "My Canvas"
    assert data["description"] == "Architecture diagram"
    assert data["item_count"] == 0
    assert data["created_by"] is not None


@pytest.mark.asyncio
async def test_create_board_minimal(client: AsyncClient):
    r = await client.post("/api/v1/hxcanvas/boards", json={"name": "Minimal"})
    assert r.status_code == 201
    assert r.json()["name"] == "Minimal"


@pytest.mark.asyncio
async def test_list_boards_empty(client: AsyncClient):
    r = await client.get("/api/v1/hxcanvas/boards")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


@pytest.mark.asyncio
async def test_list_boards_after_create(client: AsyncClient):
    await _board(client, "Listed Canvas")
    r = await client.get("/api/v1/hxcanvas/boards")
    assert r.status_code == 200
    assert any(b["name"] == "Listed Canvas" for b in r.json())


@pytest.mark.asyncio
async def test_get_board(client: AsyncClient):
    b = await _board(client, "Detailed Canvas")
    r = await client.get(f"/api/v1/hxcanvas/boards/{b['id']}")
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == b["id"]
    assert "items" in data
    assert isinstance(data["items"], list)


@pytest.mark.asyncio
async def test_get_board_not_found(client: AsyncClient):
    r = await client.get(f"/api/v1/hxcanvas/boards/{uuid.uuid4()}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_patch_board(client: AsyncClient):
    b = await _board(client, "Old Name")
    r = await client.patch(f"/api/v1/hxcanvas/boards/{b['id']}", json={"name": "New Name"})
    assert r.status_code == 200
    assert r.json()["name"] == "New Name"


@pytest.mark.asyncio
async def test_delete_board(client: AsyncClient):
    b = await _board(client, "To Delete")
    r = await client.delete(f"/api/v1/hxcanvas/boards/{b['id']}")
    assert r.status_code == 204
    r2 = await client.get(f"/api/v1/hxcanvas/boards/{b['id']}")
    assert r2.status_code == 404


# ── Item CRUD ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_add_sticky_note(client: AsyncClient):
    b = await _board(client)
    item = await _item(client, b["id"], "sticky_note", data={"color": "#fde68a", "content": "Note content"})
    assert item["type"] == "sticky_note"
    assert item["data"]["content"] == "Note content"
    assert item["x"] == 100
    assert item["board_id"] == b["id"]


@pytest.mark.asyncio
async def test_add_shape(client: AsyncClient):
    b = await _board(client)
    item = await _item(client, b["id"], "shape", data={"shape_type": "circle", "color": "#6366f1", "label": "Process"})
    assert item["type"] == "shape"
    assert item["data"]["shape_type"] == "circle"
    assert item["data"]["label"] == "Process"


@pytest.mark.asyncio
async def test_add_text_item(client: AsyncClient):
    b = await _board(client)
    item = await _item(client, b["id"], "text", data={"content": "Title", "fontSize": 24})
    assert item["type"] == "text"
    assert item["data"]["fontSize"] == 24


@pytest.mark.asyncio
async def test_add_connector(client: AsyncClient):
    b = await _board(client)
    src = await _item(client, b["id"], "sticky_note")
    dst = await _item(client, b["id"], "shape", data={"shape_type": "rect", "color": "#6366f1", "label": ""})
    conn = await _item(client, b["id"], "connector",
                       x=0, y=0, width=0, height=0,
                       data={"from_item_id": src["id"], "to_item_id": dst["id"], "label": "leads to", "color": "#94a3b8"})
    assert conn["type"] == "connector"
    assert conn["data"]["from_item_id"] == src["id"]
    assert conn["data"]["to_item_id"] == dst["id"]


@pytest.mark.asyncio
async def test_add_freehand(client: AsyncClient):
    b = await _board(client)
    item = await _item(client, b["id"], "freehand",
                       x=0, y=0, width=0, height=0,
                       data={"points": [[10, 10], [20, 20], [30, 15]], "color": "#94a3b8", "strokeWidth": 2})
    assert item["type"] == "freehand"
    assert len(item["data"]["points"]) == 3


@pytest.mark.asyncio
async def test_add_graph_node_embed(client: AsyncClient):
    b = await _board(client)
    fake_node_id = str(uuid.uuid4())
    item = await _item(client, b["id"], "graph_node_embed",
                       data={"graph_node_id": fake_node_id, "node_label": "Insurance Claim", "node_type": "case_type"})
    assert item["type"] == "graph_node_embed"
    assert item["data"]["graph_node_id"] == fake_node_id


@pytest.mark.asyncio
async def test_get_board_includes_items(client: AsyncClient):
    b = await _board(client)
    await _item(client, b["id"], "sticky_note", data={"color": "#93c5fd", "content": "Item 1"})
    await _item(client, b["id"], "text", data={"content": "Item 2", "fontSize": 16})
    r = await client.get(f"/api/v1/hxcanvas/boards/{b['id']}")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 2
    types = {i["type"] for i in items}
    assert "sticky_note" in types and "text" in types


@pytest.mark.asyncio
async def test_patch_item_position(client: AsyncClient):
    b = await _board(client)
    item = await _item(client, b["id"])
    r = await client.patch(
        f"/api/v1/hxcanvas/boards/{b['id']}/items/{item['id']}",
        json={"x": 250.0, "y": 300.0},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["x"] == 250.0
    assert data["y"] == 300.0


@pytest.mark.asyncio
async def test_patch_item_data(client: AsyncClient):
    b = await _board(client)
    item = await _item(client, b["id"], "sticky_note", data={"color": "#fde68a", "content": "Original"})
    r = await client.patch(
        f"/api/v1/hxcanvas/boards/{b['id']}/items/{item['id']}",
        json={"data": {"color": "#86efac", "content": "Updated"}},
    )
    assert r.status_code == 200
    assert r.json()["data"]["content"] == "Updated"


@pytest.mark.asyncio
async def test_delete_item(client: AsyncClient):
    b = await _board(client)
    item = await _item(client, b["id"])
    r = await client.delete(f"/api/v1/hxcanvas/boards/{b['id']}/items/{item['id']}")
    assert r.status_code == 204
    detail = await client.get(f"/api/v1/hxcanvas/boards/{b['id']}")
    assert not any(i["id"] == item["id"] for i in detail.json()["items"])


@pytest.mark.asyncio
async def test_delete_item_not_found(client: AsyncClient):
    b = await _board(client)
    r = await client.delete(f"/api/v1/hxcanvas/boards/{b['id']}/items/{uuid.uuid4()}")
    assert r.status_code == 404


# ── Bulk upsert ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bulk_create_items(client: AsyncClient):
    b = await _board(client)
    r = await client.post(f"/api/v1/hxcanvas/boards/{b['id']}/items/bulk", json={
        "items": [
            {"type": "sticky_note", "x": 0, "y": 0, "width": 160, "height": 120, "data": {"color": "#fde68a", "content": "A"}, "z_index": 0},
            {"type": "text",        "x": 200, "y": 0, "width": 120, "height": 40, "data": {"content": "B", "fontSize": 16},      "z_index": 1},
            {"type": "shape",       "x": 400, "y": 0, "width": 120, "height": 80, "data": {"shape_type": "rect", "color": "#6366f1", "label": "C"}, "z_index": 2},
        ]
    })
    assert r.status_code == 200
    assert len(r.json()) == 3


@pytest.mark.asyncio
async def test_bulk_upsert_updates_existing(client: AsyncClient):
    b = await _board(client)
    item = await _item(client, b["id"], "sticky_note", data={"color": "#fde68a", "content": "Before"})
    r = await client.post(f"/api/v1/hxcanvas/boards/{b['id']}/items/bulk", json={
        "items": [{"id": item["id"], "type": "sticky_note", "x": 50, "y": 50, "width": 160, "height": 120, "data": {"color": "#fde68a", "content": "After"}, "z_index": 0}]
    })
    assert r.status_code == 200
    assert r.json()[0]["data"]["content"] == "After"


# ── List item count in board list ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_boards_shows_item_count(client: AsyncClient):
    b = await _board(client, "Counted Canvas")
    await _item(client, b["id"])
    await _item(client, b["id"])
    r = await client.get("/api/v1/hxcanvas/boards")
    assert r.status_code == 200
    found = next((x for x in r.json() if x["id"] == b["id"]), None)
    assert found is not None
    assert found["item_count"] == 2


# ── Board isolation (tenant scoping) ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_board_wrong_id_returns_404(client: AsyncClient):
    r = await client.get(f"/api/v1/hxcanvas/boards/{uuid.uuid4()}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_cascading_delete(client: AsyncClient):
    b = await _board(client, "Cascade Test")
    await _item(client, b["id"], "sticky_note")
    await _item(client, b["id"], "text", data={"content": "X", "fontSize": 16})
    del_r = await client.delete(f"/api/v1/hxcanvas/boards/{b['id']}")
    assert del_r.status_code == 204
    r = await client.get(f"/api/v1/hxcanvas/boards/{b['id']}")
    assert r.status_code == 404
