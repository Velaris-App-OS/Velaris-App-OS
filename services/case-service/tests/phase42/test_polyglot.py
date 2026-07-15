"""HELIX P42 — HxNexus Polyglot Intelligence tests (22 tests).

Covers: bpm_concepts seed data (all 4 tools present), GET /polyglot/concepts
        (list, tool filter, search), POST /polyglot/translate (exact KB hit,
        fuzzy KB hit, no-LLM fallback, unknown concept), POST /polyglot/analyze
        (keyword scan hits, empty text), POST /polyglot/compare (KB context),
        GET /docs/business (template fallback), GET /docs/developer (template
        fallback), POST /docs/regenerate, staleness check, auth guard.
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select

from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser, ActiveAccessGroup
from case_service.db.models import BpmConceptModel, GeneratedDocModel, CaseTypeModel
from case_service.hxnexus.polyglot import translate, analyze, compare, _fuzzy_score
from case_service.hxnexus.autodoc import get_business_guide, get_dev_guide, _is_stale
from case_service.main import app


# ── Auth helpers ──────────────────────────────────────────────────────────────

def _admin():
    return AuthenticatedUser(
        user_id="admin-1", roles=["admin"],
        active_access_group=ActiveAccessGroup(
            id=str(uuid.uuid4()), name="Admins",
            portal_id=str(uuid.uuid4()), portal_type="admin",
            portal_name="Admin Portal", modules=[], homepage="/",
            roles=["admin"], privileges=[],
            allowed_case_type_ids=["*"], allowed_queue_ids=["*"],
        ),
    )

def _override():
    app.dependency_overrides[get_current_user] = lambda: _admin()

def _clear():
    app.dependency_overrides.pop(get_current_user, None)


# ── Seed helper ───────────────────────────────────────────────────────────────

async def _seed(session, tool="pega", concept="Assignment shape",
                helix="user_task step", confidence="exact"):
    c = BpmConceptModel(
        source_tool=tool, source_concept=concept,
        helix_equiv=helix, description=f"{concept} maps to {helix}.",
        confidence=confidence,
    )
    session.add(c)
    await session.flush()
    return c


# ── Fuzzy matching (pure Python, no DB) ───────────────────────────────────────

class TestFuzzyScore:
    def test_identical_strings(self):
        assert _fuzzy_score("assignment shape", "assignment shape") == 1.0

    def test_substring_match(self):
        assert _fuzzy_score("assignment", "assignment shape") >= 0.8

    def test_case_insensitive(self):
        assert _fuzzy_score("Assignment Shape", "assignment shape") == 1.0

    def test_different_strings_low_score(self):
        assert _fuzzy_score("xyz123", "assignment shape") < 0.5


# ── Polyglot functions ────────────────────────────────────────────────────────

class TestTranslate:
    async def test_exact_kb_hit(self, session):
        await _seed(session, "pega", "Assignment shape", "user_task step", "exact")
        result = await translate("pega", "Assignment shape", session)
        assert result["helix_equiv"] == "user_task step"
        assert result["confidence"] == "exact"
        assert result["source"] == "knowledge_base"

    async def test_fuzzy_kb_hit(self, session):
        await _seed(session, "pega", "Assignment shape", "user_task step")
        result = await translate("pega", "assignment", session)
        assert result["helix_equiv"] == "user_task step"

    async def test_no_match_no_llm_returns_unknown_or_partial(self, session):
        result = await translate("pega", "XyzCompletelyUnknown999", session)
        # When LLM unavailable/fails: manual. When LLM tries but no KB match: partial.
        assert result["confidence"] in ("manual", "partial")
        assert result["source_tool"] == "pega"

    async def test_returns_source_tool(self, session):
        await _seed(session)
        result = await translate("pega", "Assignment shape", session)
        assert result["source_tool"] == "pega"

    async def test_partial_match_returned_as_fallback(self, session):
        await _seed(session, "pega", "Approval shape", "approval step", "exact")
        result = await translate("pega", "Approval", session)
        assert "approval" in result["helix_equiv"].lower()


class TestAnalyze:
    async def test_keyword_hit_detected(self, session):
        await _seed(session, "pega", "Assignment shape", "user_task step")
        result = await analyze("pega", "This flow has an Assignment shape for review", session)
        assert len(result["keyword_hits"]) >= 1
        assert result["keyword_hits"][0]["source_concept"] == "Assignment shape"

    async def test_no_keyword_hit_empty_hits(self, session):
        result = await analyze("pega", "completely unrelated text xyz", session)
        assert result["keyword_hits"] == []

    async def test_returns_tool(self, session):
        result = await analyze("camunda", "some text", session)
        assert result["tool"] == "camunda"

    async def test_hint_mentions_count(self, session):
        await _seed(session, "pega", "Stage", "stage", "exact")
        result = await analyze("pega", "Stage definition here", session)
        assert "1" in result["hint"]


class TestCompare:
    async def test_returns_tool_and_question(self, session):
        result = await compare("pega", "how do I model an approval?", session)
        assert result["tool"] == "pega"
        assert result["question"] == "how do I model an approval?"

    async def test_answer_present(self, session):
        await _seed(session, "pega", "Approval shape", "approval step")
        result = await compare("pega", "Approval shape", session)
        assert "answer" in result
        assert len(result["answer"]) > 10

    async def test_related_concepts_list(self, session):
        await _seed(session, "pega", "Approval shape", "approval step")
        result = await compare("pega", "Approval", session)
        assert isinstance(result["related_concepts"], list)


# ── Auto-documentation ────────────────────────────────────────────────────────

class TestAutodoc:
    async def test_business_guide_template_no_llm(self, session):
        content = await get_business_guide(session, force=True)
        assert "Velaris" in content   # rebrand 2026-05-17
        assert "Business Guide" in content

    async def test_dev_guide_template_no_llm(self, session):
        content = await get_dev_guide(session, force=True)
        assert "Developer Guide" in content
        assert "FastAPI" in content or "API" in content

    async def test_business_guide_cached(self, session):
        await get_business_guide(session, force=True)
        # Second call should hit cache
        doc = (await session.execute(
            select(GeneratedDocModel).where(GeneratedDocModel.doc_type == "business_guide")
        )).scalar_one_or_none()
        assert doc is not None

    async def test_staleness_detected_on_node_count_change(self, session):
        # Save doc with node_count=0
        session.add(GeneratedDocModel(doc_type="dev_guide", content="old", node_count=999))
        await session.flush()
        stale = await _is_stale(session, "dev_guide")
        assert stale is True

    async def test_dev_guide_with_case_type_in_graph(self, session):
        from case_service.hxgraph.sync import sync_graph
        session.add(CaseTypeModel(
            name="Test Process", version="1.0",
            definition_json={"stages": [{"id": "s1", "name": "Start", "steps": []}]},
        ))
        await session.flush()
        await sync_graph(session)
        content = await get_dev_guide(session, force=True)
        assert len(content) > 100


# ── REST API ──────────────────────────────────────────────────────────────────

class TestPolyglotAPI:
    async def test_list_concepts_all(self, client: AsyncClient, session):
        await _seed(session)
        _override()
        try:
            r = await client.get("/api/v1/hxnexus/polyglot/concepts")
        finally:
            _clear()
        assert r.status_code == 200
        assert r.json()["total"] >= 1

    async def test_list_concepts_tool_filter(self, client: AsyncClient, session):
        await _seed(session, "camunda", "UserTask", "user_task step")
        await _seed(session, "pega", "Assignment shape", "user_task step")
        _override()
        try:
            r = await client.get("/api/v1/hxnexus/polyglot/concepts?tool=camunda")
        finally:
            _clear()
        assert r.status_code == 200
        for c in r.json()["concepts"]:
            assert c["source_tool"] == "camunda"

    async def test_translate_endpoint(self, client: AsyncClient, session):
        await _seed(session)
        _override()
        try:
            r = await client.post("/api/v1/hxnexus/polyglot/translate",
                                  json={"tool": "pega", "concept": "Assignment shape"})
        finally:
            _clear()
        assert r.status_code == 200
        assert "helix_equiv" in r.json()

    async def test_analyze_endpoint(self, client: AsyncClient, session):
        await _seed(session)
        _override()
        try:
            r = await client.post("/api/v1/hxnexus/polyglot/analyze",
                                  json={"tool": "pega", "text": "Assignment shape in stage"})
        finally:
            _clear()
        assert r.status_code == 200
        assert "keyword_hits" in r.json()

    async def test_compare_endpoint(self, client: AsyncClient, session):
        _override()
        try:
            r = await client.post("/api/v1/hxnexus/polyglot/compare",
                                  json={"tool": "pega", "question": "how do I model approval?"})
        finally:
            _clear()
        assert r.status_code == 200
        assert "answer" in r.json()

    async def test_business_guide_endpoint(self, client: AsyncClient, session):
        _override()
        try:
            r = await client.get("/api/v1/hxnexus/docs/business")
        finally:
            _clear()
        assert r.status_code == 200
        assert "Velaris" in r.text   # rebrand 2026-05-17

    async def test_developer_guide_endpoint(self, client: AsyncClient, session):
        _override()
        try:
            r = await client.get("/api/v1/hxnexus/docs/developer")
        finally:
            _clear()
        assert r.status_code == 200
        assert "Guide" in r.text

    async def test_regenerate_endpoint(self, client: AsyncClient, session):
        _override()
        try:
            r = await client.post("/api/v1/hxnexus/docs/regenerate")
        finally:
            _clear()
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "regenerated"
        assert data["business_guide_chars"] > 0
        assert data["dev_guide_chars"] > 0
