"""Tests for P56 HxWork — Kanban + Sprint Board."""
from __future__ import annotations

import uuid
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import HxWorkBoardModel, HxWorkSprintModel

from tests.conftest import client, session, deploy_case_type, create_case  # type: ignore[attr-defined]


async def _board(client: AsyncClient, name: str = "Test Board", case_type_id: str | None = None) -> dict:
    r = await client.post("/api/v1/hxwork/boards", json={
        "name": name, "description": "Test board", "case_type_id": case_type_id,
    })
    assert r.status_code == 201
    return r.json()


# ── Boards ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_board(client: AsyncClient):
    r = await client.post("/api/v1/hxwork/boards", json={"name": "Dev Board", "description": "Development"})
    assert r.status_code == 201
    data = r.json()
    assert data["name"] == "Dev Board"
    assert data["created_by"] is not None


@pytest.mark.asyncio
async def test_create_board_with_case_type(client: AsyncClient):
    ct = await deploy_case_type(client, name="Sprint CT", definition_json={
        "stages": [
            {"id": "backlog", "name": "Backlog", "order": 1, "steps": []},
            {"id": "in_progress", "name": "In Progress", "order": 2, "steps": []},
            {"id": "done", "name": "Done", "order": 3, "steps": []},
        ]
    })
    b = await _board(client, "Sprint Board", ct["id"])
    assert b["case_type_id"] == ct["id"]
    assert len(b["column_config"]) == 3
    stage_ids = [c["stage_id"] for c in b["column_config"]]
    assert "backlog" in stage_ids and "in_progress" in stage_ids and "done" in stage_ids


@pytest.mark.asyncio
async def test_list_boards_empty(client: AsyncClient):
    r = await client.get("/api/v1/hxwork/boards")
    assert r.status_code == 200 and isinstance(r.json(), list)


@pytest.mark.asyncio
async def test_list_boards_after_create(client: AsyncClient):
    await _board(client, "My Board")
    r = await client.get("/api/v1/hxwork/boards")
    assert any(b["name"] == "My Board" for b in r.json())


@pytest.mark.asyncio
async def test_get_board(client: AsyncClient):
    b = await _board(client, "Get Test")
    r = await client.get(f"/api/v1/hxwork/boards/{b['id']}")
    assert r.status_code == 200
    assert r.json()["id"] == b["id"]


@pytest.mark.asyncio
async def test_get_board_not_found(client: AsyncClient):
    r = await client.get(f"/api/v1/hxwork/boards/{uuid.uuid4()}")
    assert r.status_code == 404


# ── Cards ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_board_cards(client: AsyncClient):
    ct = await deploy_case_type(client, name="Card CT", definition_json={
        "stages": [{"id": "todo", "name": "To Do", "order": 1, "steps": []}]
    })
    b = await _board(client, "Cards Board", ct["id"])
    r = await client.get(f"/api/v1/hxwork/boards/{b['id']}/cards")
    assert r.status_code == 200
    data = r.json()
    assert "columns" in data and "cards" in data


@pytest.mark.asyncio
async def test_cards_include_cases(client: AsyncClient):
    ct = await deploy_case_type(client, name="Case CT", definition_json={
        "stages": [{"id": "s1", "name": "Open", "order": 1, "steps": []}]
    })
    await create_case(client, ct["id"])
    b = await _board(client, "Case Board", ct["id"])
    r = await client.get(f"/api/v1/hxwork/boards/{b['id']}/cards")
    assert r.status_code == 200
    all_cards = [c for col_cards in r.json()["cards"].values() for c in col_cards]
    assert len(all_cards) >= 1


# ── Sprints ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_sprint(client: AsyncClient):
    b = await _board(client)
    r = await client.post(f"/api/v1/hxwork/boards/{b['id']}/sprints", json={
        "name": "Sprint 1", "goal": "Ship P56", "start_date": "2026-05-01T00:00:00", "end_date": "2026-05-14T00:00:00",
    })
    assert r.status_code == 201
    data = r.json()
    assert data["name"] == "Sprint 1"
    assert data["status"] == "planned"
    assert data["goal"] == "Ship P56"


@pytest.mark.asyncio
async def test_list_sprints(client: AsyncClient):
    b = await _board(client)
    await client.post(f"/api/v1/hxwork/boards/{b['id']}/sprints", json={"name": "Sprint A"})
    r = await client.get(f"/api/v1/hxwork/boards/{b['id']}/sprints")
    assert r.status_code == 200
    assert any(s["name"] == "Sprint A" for s in r.json())


@pytest.mark.asyncio
async def test_start_sprint(client: AsyncClient):
    b = await _board(client)
    s = (await client.post(f"/api/v1/hxwork/boards/{b['id']}/sprints", json={"name": "Sprint X"})).json()
    r = await client.post(f"/api/v1/hxwork/sprints/{s['id']}/start")
    assert r.status_code == 200
    assert r.json()["status"] == "active"


@pytest.mark.asyncio
async def test_start_already_active_sprint_returns_400(client: AsyncClient):
    b = await _board(client)
    s = (await client.post(f"/api/v1/hxwork/boards/{b['id']}/sprints", json={"name": "S"})).json()
    await client.post(f"/api/v1/hxwork/sprints/{s['id']}/start")
    r = await client.post(f"/api/v1/hxwork/sprints/{s['id']}/start")
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_complete_sprint(client: AsyncClient):
    b = await _board(client)
    s = (await client.post(f"/api/v1/hxwork/boards/{b['id']}/sprints", json={"name": "S2"})).json()
    await client.post(f"/api/v1/hxwork/sprints/{s['id']}/start")
    r = await client.post(f"/api/v1/hxwork/sprints/{s['id']}/complete")
    assert r.status_code == 200
    assert r.json()["status"] == "completed"


# ── Sprint cards ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_add_card_to_sprint(client: AsyncClient):
    ct = await deploy_case_type(client, name="Work CT", definition_json={"stages": []})
    case = await create_case(client, ct["id"])
    b = await _board(client)
    s = (await client.post(f"/api/v1/hxwork/boards/{b['id']}/sprints", json={"name": "S"})).json()
    r = await client.post(f"/api/v1/hxwork/sprints/{s['id']}/cards",
                          json={"case_id": case["id"], "story_points": 5})
    assert r.status_code == 201
    assert r.json()["story_points"] == 5


@pytest.mark.asyncio
async def test_remove_card_from_sprint(client: AsyncClient):
    ct = await deploy_case_type(client, name="Work2 CT", definition_json={"stages": []})
    case = await create_case(client, ct["id"])
    b = await _board(client)
    s = (await client.post(f"/api/v1/hxwork/boards/{b['id']}/sprints", json={"name": "S"})).json()
    await client.post(f"/api/v1/hxwork/sprints/{s['id']}/cards", json={"case_id": case["id"], "story_points": 3})
    r = await client.delete(f"/api/v1/hxwork/sprints/{s['id']}/cards/{case['id']}")
    assert r.status_code == 204


# ── Relations ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_add_card_relation(client: AsyncClient):
    b = await _board(client)
    c1, c2 = uuid.uuid4(), uuid.uuid4()
    r = await client.post(f"/api/v1/hxwork/boards/{b['id']}/relations", json={
        "from_case_id": str(c1), "to_case_id": str(c2), "relation_type": "blocks",
    })
    assert r.status_code == 201
    assert r.json()["relation_type"] == "blocks"


@pytest.mark.asyncio
async def test_list_relations(client: AsyncClient):
    b = await _board(client)
    await client.post(f"/api/v1/hxwork/boards/{b['id']}/relations", json={
        "from_case_id": str(uuid.uuid4()), "to_case_id": str(uuid.uuid4()), "relation_type": "depends_on",
    })
    r = await client.get(f"/api/v1/hxwork/boards/{b['id']}/relations")
    assert r.status_code == 200
    assert len(r.json()) >= 1


# ── Analytics ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_board_analytics(client: AsyncClient):
    b = await _board(client)
    r = await client.get(f"/api/v1/hxwork/boards/{b['id']}/analytics")
    assert r.status_code == 200
    data = r.json()
    assert "total_cards" in data
    assert "total_sprints" in data


@pytest.mark.asyncio
async def test_analytics_not_found_returns_empty(client: AsyncClient):
    r = await client.get(f"/api/v1/hxwork/boards/{uuid.uuid4()}/analytics")
    assert r.status_code == 404
