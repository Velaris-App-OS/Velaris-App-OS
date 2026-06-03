"""Tests for P58 HxDocs — Living Documentation."""
from __future__ import annotations

import uuid
import pytest
from httpx import AsyncClient

from tests.conftest import client, session  # type: ignore[attr-defined]


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _space(client: AsyncClient, name: str = "Test Space", is_public: bool = False) -> dict:
    r = await client.post("/api/v1/hxdocs/spaces", json={"name": name, "description": "Test", "is_public": is_public})
    assert r.status_code == 201, r.text
    return r.json()


async def _article(client: AsyncClient, space_id: str, title: str = "Test Article", content: list | None = None) -> dict:
    r = await client.post(f"/api/v1/hxdocs/spaces/{space_id}/articles", json={
        "title": title,
        "content": content or [
            {"id": "b1", "type": "heading", "level": 1, "text": title},
            {"id": "b2", "type": "paragraph", "text": "This is test content."},
        ],
        "tags": ["test"],
    })
    assert r.status_code == 201, r.text
    return r.json()


# ── Spaces ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_space(client: AsyncClient):
    r = await client.post("/api/v1/hxdocs/spaces", json={"name": "Engineering Docs", "description": "Tech docs"})
    assert r.status_code == 201
    data = r.json()
    assert data["name"] == "Engineering Docs"
    assert data["slug"] == "engineering-docs"
    assert data["is_public"] is False


@pytest.mark.asyncio
async def test_create_public_space(client: AsyncClient):
    r = await client.post("/api/v1/hxdocs/spaces", json={"name": "Public Docs", "is_public": True})
    assert r.status_code == 201
    assert r.json()["is_public"] is True


@pytest.mark.asyncio
async def test_list_spaces(client: AsyncClient):
    await _space(client, "Space A")
    await _space(client, "Space B")
    r = await client.get("/api/v1/hxdocs/spaces")
    assert r.status_code == 200
    names = [s["name"] for s in r.json()]
    assert "Space A" in names and "Space B" in names


@pytest.mark.asyncio
async def test_get_space(client: AsyncClient):
    s = await _space(client, "Get Space")
    r = await client.get(f"/api/v1/hxdocs/spaces/{s['id']}")
    assert r.status_code == 200
    assert r.json()["id"] == s["id"]


@pytest.mark.asyncio
async def test_get_space_not_found(client: AsyncClient):
    r = await client.get(f"/api/v1/hxdocs/spaces/{uuid.uuid4()}")
    assert r.status_code == 404


# ── Articles ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_article(client: AsyncClient):
    s = await _space(client)
    r = await client.post(f"/api/v1/hxdocs/spaces/{s['id']}/articles", json={
        "title": "My First Article",
        "content": [{"id": "b1", "type": "paragraph", "text": "Hello world"}],
        "tags": ["intro"],
    })
    assert r.status_code == 201
    data = r.json()
    assert data["title"] == "My First Article"
    assert data["status"] == "draft"
    assert data["version"] == 1
    assert "intro" in data["tags"]


@pytest.mark.asyncio
async def test_list_articles(client: AsyncClient):
    s = await _space(client)
    await _article(client, s["id"], "Article 1")
    await _article(client, s["id"], "Article 2")
    r = await client.get(f"/api/v1/hxdocs/spaces/{s['id']}/articles")
    assert r.status_code == 200
    assert len(r.json()) >= 2


@pytest.mark.asyncio
async def test_list_articles_filter_status(client: AsyncClient):
    s = await _space(client)
    a = await _article(client, s["id"], "Draft Article")
    await client.post(f"/api/v1/hxdocs/articles/{a['id']}/publish", json={"is_public": False})
    r_draft = await client.get(f"/api/v1/hxdocs/spaces/{s['id']}/articles?status=draft")
    r_pub   = await client.get(f"/api/v1/hxdocs/spaces/{s['id']}/articles?status=published")
    assert r_draft.status_code == 200
    assert r_pub.status_code == 200
    pub_ids = [a["id"] for a in r_pub.json()]
    assert a["id"] in pub_ids


@pytest.mark.asyncio
async def test_get_article(client: AsyncClient):
    s = await _space(client)
    a = await _article(client, s["id"])
    r = await client.get(f"/api/v1/hxdocs/articles/{a['id']}")
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == a["id"]
    assert "content" in data
    assert len(data["content"]) == 2


@pytest.mark.asyncio
async def test_get_article_not_found(client: AsyncClient):
    r = await client.get(f"/api/v1/hxdocs/articles/{uuid.uuid4()}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_patch_article_title(client: AsyncClient):
    s = await _space(client)
    a = await _article(client, s["id"], "Old Title")
    r = await client.patch(f"/api/v1/hxdocs/articles/{a['id']}", json={"title": "New Title"})
    assert r.status_code == 200
    assert r.json()["title"] == "New Title"


@pytest.mark.asyncio
async def test_patch_article_content_updates_word_count(client: AsyncClient):
    s = await _space(client)
    a = await _article(client, s["id"])
    r = await client.patch(f"/api/v1/hxdocs/articles/{a['id']}", json={
        "content": [{"id": "b1", "type": "paragraph", "text": "One two three four five six"}]
    })
    assert r.status_code == 200
    assert r.json()["word_count"] == 6


@pytest.mark.asyncio
async def test_save_version(client: AsyncClient):
    s = await _space(client)
    a = await _article(client, s["id"])
    r = await client.patch(f"/api/v1/hxdocs/articles/{a['id']}", json={"title": "Updated", "save_version": True})
    assert r.status_code == 200
    assert r.json()["version"] == 2
    # Check version was saved
    rv = await client.get(f"/api/v1/hxdocs/articles/{a['id']}/versions")
    assert rv.status_code == 200
    assert len(rv.json()) == 1
    assert rv.json()[0]["version"] == 1


@pytest.mark.asyncio
async def test_publish_article(client: AsyncClient):
    s = await _space(client)
    a = await _article(client, s["id"])
    r = await client.post(f"/api/v1/hxdocs/articles/{a['id']}/publish", json={"is_public": False})
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "published"
    assert data["is_public"] is False


@pytest.mark.asyncio
async def test_publish_article_public(client: AsyncClient):
    s = await _space(client)
    a = await _article(client, s["id"])
    r = await client.post(f"/api/v1/hxdocs/articles/{a['id']}/publish", json={"is_public": True})
    assert r.status_code == 200
    assert r.json()["is_public"] is True


@pytest.mark.asyncio
async def test_delete_article(client: AsyncClient):
    s = await _space(client)
    a = await _article(client, s["id"])
    r = await client.delete(f"/api/v1/hxdocs/articles/{a['id']}")
    assert r.status_code == 204
    r2 = await client.get(f"/api/v1/hxdocs/articles/{a['id']}")
    assert r2.status_code == 404


# ── Block types ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_all_block_types(client: AsyncClient):
    s = await _space(client)
    content = [
        {"id": "b1", "type": "heading",   "level": 1, "text": "Main Title"},
        {"id": "b2", "type": "heading",   "level": 2, "text": "Section"},
        {"id": "b3", "type": "paragraph", "text": "Some paragraph text here."},
        {"id": "b4", "type": "callout",   "text": "Important note!"},
        {"id": "b5", "type": "code",      "language": "python", "text": "print('hello')"},
        {"id": "b6", "type": "live_data", "embed_type": "case_count", "label": "Active Cases"},
    ]
    r = await client.post(f"/api/v1/hxdocs/spaces/{s['id']}/articles", json={"title": "All Blocks", "content": content})
    assert r.status_code == 201
    a_id = r.json()["id"]
    r2 = await client.get(f"/api/v1/hxdocs/articles/{a_id}")
    assert len(r2.json()["content"]) == 6


# ── Search ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_search_articles(client: AsyncClient):
    s = await _space(client)
    await _article(client, s["id"], "Insurance Claim Workflow")
    await _article(client, s["id"], "Mortgage Application Guide")
    r = await client.get("/api/v1/hxdocs/search?q=Insurance")
    assert r.status_code == 200
    results = r.json()
    assert any("Insurance" in a["title"] for a in results)


@pytest.mark.asyncio
async def test_search_returns_empty_for_no_match(client: AsyncClient):
    r = await client.get("/api/v1/hxdocs/search?q=zzznomatchzzz")
    assert r.status_code == 200
    assert r.json() == []


# ── Version history ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_versions_empty_initially(client: AsyncClient):
    s = await _space(client)
    a = await _article(client, s["id"])
    r = await client.get(f"/api/v1/hxdocs/articles/{a['id']}/versions")
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.asyncio
async def test_multiple_versions(client: AsyncClient):
    s = await _space(client)
    a = await _article(client, s["id"])
    await client.patch(f"/api/v1/hxdocs/articles/{a['id']}", json={"title": "v2", "save_version": True})
    await client.patch(f"/api/v1/hxdocs/articles/{a['id']}", json={"title": "v3", "save_version": True})
    r = await client.get(f"/api/v1/hxdocs/articles/{a['id']}/versions")
    assert r.status_code == 200
    assert len(r.json()) == 2
    versions = [v["version"] for v in r.json()]
    assert 1 in versions and 2 in versions
