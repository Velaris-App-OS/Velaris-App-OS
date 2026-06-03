"""HELIX P30 — HxNexus AI Copilot tests (25 tests).

All LLM HTTP calls are mocked — no real Ollama/OpenAI/Anthropic traffic.
"""
from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient

from case_service.db.models import (
    CaseTypeModel, CaseInstanceModel,
    CopilotConversationModel, CopilotMessageModel,
    DocumentChunkModel,
)
from case_service.hxnexus.chunker import chunk_text
from case_service.hxnexus.text_extractor import extract_text
from case_service.hxnexus.vector_store import InMemoryVectorStore, _cosine
from case_service.hxnexus.ollama_backend import OllamaBackend
from case_service.hxnexus.openai_backend import OpenAIBackend
from case_service.hxnexus.anthropic_backend import AnthropicBackend
from case_service.hxnexus.service import next_best_action, qa_over_documents, chat, index_document


# ── Helpers ───────────────────────────────────────────────────────────

def _fake_user(user_id: str = "user-1", roles: list[str] | None = None):
    from case_service.auth.models import AuthenticatedUser
    return AuthenticatedUser(user_id=user_id, email=f"{user_id}@test.local",
                             roles=roles or ["viewer"])


def _mock_backend(complete_return: str = "", embed_return: list | None = None):
    b = MagicMock()
    b.backend_name = "mock"
    b.available = True
    b.complete = AsyncMock(return_value=complete_return)
    b.embed = AsyncMock(return_value=embed_return or [0.1] * 8)
    return b


@pytest_asyncio.fixture
async def ct(session):
    c = CaseTypeModel(name="P30-Type", version="1.0",
                      lifecycle_process_id="lp-p30", definition_json={"stages": []})
    session.add(c); await session.flush(); return c


@pytest_asyncio.fixture
async def case(session, ct):
    c = CaseInstanceModel(case_type_id=ct.id, case_type_version="1.0",
                          status="open", priority="high", data={"subject": "Test case"})
    session.add(c); await session.flush(); return c


# ── Chunker unit tests ────────────────────────────────────────────────

def test_01_chunk_text_basic():
    text = "A" * 600
    chunks = chunk_text(text, chunk_size=200, overlap=20)
    assert len(chunks) > 1
    assert all(len(c) <= 200 for c in chunks)


def test_02_chunk_text_empty():
    assert chunk_text("") == []
    assert chunk_text("   ") == []


def test_03_chunk_text_short():
    chunks = chunk_text("Hello world", chunk_size=512)
    assert chunks == ["Hello world"]


def test_04_chunk_text_overlap():
    text = "ABCDEFGHIJ" * 10    # 100 chars
    chunks = chunk_text(text, chunk_size=30, overlap=10)
    # Second chunk should start 20 chars into first chunk
    assert len(chunks) >= 3


# ── Text extractor unit tests ─────────────────────────────────────────

def test_05_extract_plain_text():
    data = b"Hello, this is plain text."
    result = extract_text(data, "text/plain")
    assert result == "Hello, this is plain text."


def test_06_extract_unknown_binary_returns_empty():
    result = extract_text(b"\x00\x01\x02", "application/octet-stream")
    assert result == ""


def test_07_extract_html():
    data = b"<html><body>Hello</body></html>"
    result = extract_text(data, "text/html")
    assert "Hello" in result


def test_08_extract_pdf_fitz(monkeypatch):
    mock_page = MagicMock()
    mock_page.get_text.return_value = "PDF content here"
    mock_doc = MagicMock()
    mock_doc.__iter__ = MagicMock(return_value=iter([mock_page]))

    with patch("fitz.open", return_value=mock_doc):
        result = extract_text(b"%PDF-1.4", "application/pdf")
    assert "PDF content here" in result


# ── Vector store unit tests ───────────────────────────────────────────

def test_09_cosine_identical_vectors():
    v = [1.0, 0.0, 0.0]
    assert abs(_cosine(v, v) - 1.0) < 1e-5


def test_10_cosine_orthogonal_vectors():
    assert abs(_cosine([1.0, 0.0], [0.0, 1.0])) < 1e-5


def test_11_cosine_empty_vector():
    assert _cosine([], [1.0]) == 0.0


@pytest.mark.asyncio
async def test_12_in_memory_store_upsert_and_query():
    vs = InMemoryVectorStore()
    await vs.upsert("c1", "foo bar", [1.0, 0.0], {"tenant_id": "t1"})
    await vs.upsert("c2", "baz qux", [0.0, 1.0], {"tenant_id": "t1"})
    results = await vs.query([1.0, 0.0], top_k=1)
    assert results[0]["chunk_id"] == "c1"
    assert results[0]["score"] > 0.9


@pytest.mark.asyncio
async def test_13_in_memory_store_tenant_filter():
    vs = InMemoryVectorStore()
    await vs.upsert("c1", "alpha", [1.0, 0.0], {"tenant_id": "t1"})
    await vs.upsert("c2", "beta",  [1.0, 0.0], {"tenant_id": "t2"})
    results = await vs.query([1.0, 0.0], filter_metadata={"tenant_id": "t1"})
    assert all(r["metadata"]["tenant_id"] == "t1" for r in results)


@pytest.mark.asyncio
async def test_14_in_memory_store_delete():
    vs = InMemoryVectorStore()
    await vs.upsert("c1", "text", [1.0], {})
    await vs.delete("c1")
    results = await vs.query([1.0])
    assert not results


# ── Backend unit tests ────────────────────────────────────────────────

def test_15_openai_unavailable_without_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    b = OpenAIBackend()
    assert b.available is False


def test_16_anthropic_unavailable_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    b = AnthropicBackend()
    assert b.available is False


def test_17_openai_available_with_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    b = OpenAIBackend()
    assert b.available is True


@pytest.mark.asyncio
async def test_18_openai_complete_returns_empty_when_unavailable():
    b = OpenAIBackend()
    b._api_key = ""
    result = await b.complete("hello")
    assert result == ""


# ── Service-level tests ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_19_next_best_action_parses_json():
    suggestions = [{"action": "Escalate", "reason": "SLA at risk", "priority": "high"}]
    backend = _mock_backend(complete_return=json.dumps({"suggestions": suggestions}))
    result = await next_best_action({"status": "open", "priority": "high"}, backend=backend)
    assert len(result) == 1
    assert result[0]["action"] == "Escalate"


@pytest.mark.asyncio
async def test_20_next_best_action_empty_when_backend_unavailable():
    b = MagicMock(); b.available = False
    result = await next_best_action({"status": "open"}, backend=b)
    assert result == []


@pytest.mark.asyncio
async def test_21_next_best_action_handles_bad_json():
    backend = _mock_backend(complete_return="not json at all")
    result = await next_best_action({"status": "open"}, backend=backend)
    assert result == []


@pytest.mark.asyncio
async def test_22_index_document_creates_chunks(session, case):
    backend = _mock_backend(embed_return=[0.1] * 8)
    vs = InMemoryVectorStore()
    text = "This is test content. " * 30   # ~660 chars → 2+ chunks
    n = await index_document(session, uuid.uuid4(), case.id,
                             text.encode(), "text/plain", None,
                             vector_store=vs, backend=backend)
    assert n > 0


@pytest.mark.asyncio
async def test_23_qa_returns_unavailable_when_no_backend(session, case):
    b = MagicMock(); b.available = False
    result = await qa_over_documents(session, case.id, "What happened?",
                                     tenant_id=None, backend=b,
                                     vector_store=InMemoryVectorStore())
    assert "unavailable" in result["answer"].lower()


@pytest.mark.asyncio
async def test_24_chat_creates_conversation(session, case):
    backend = _mock_backend(complete_return="Here is my reply.")
    with patch("case_service.hxnexus.service.get_llm_backend", return_value=backend):
        result = await chat(session, None, "user-1", case.id, "Hello HxNexus", None, backend=backend)

    assert "conversation_id" in result
    assert result["reply"] == "Here is my reply."


@pytest.mark.asyncio
async def test_25_chat_continues_existing_conversation(session, case):
    backend = _mock_backend(complete_return="Follow-up reply.")
    r1 = await chat(session, None, "user-1", case.id, "First message", None, backend=backend)
    conv_id = uuid.UUID(r1["conversation_id"])

    r2 = await chat(session, conv_id, "user-1", case.id, "Second message", None, backend=backend)
    assert r2["conversation_id"] == str(conv_id)

    msgs = (await session.execute(
        __import__("sqlalchemy", fromlist=["select"]).select(CopilotMessageModel)
        .where(CopilotMessageModel.conversation_id == conv_id)
    )).scalars().all()
    assert len(msgs) == 4   # 2 user + 2 assistant
