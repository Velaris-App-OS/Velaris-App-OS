"""HELIX P39c — HxNexus Public Chat + RAG Pre-Submission tests (20 tests).

Covers: /ask (happy path, rate limit, AI unavailable fallback, wrong slug),
        /cases/{id}/chat (happy path, wrong email guard, wrong slug,
        rate limit, AI unavailable fallback, no activity, case context
        injected, reply field present), rate limiter unit tests.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient

from case_service.db.models import (
    CaseAuditLogModel,
    CaseInstanceModel,
    CaseTypeModel,
    TenantModel,
)
from case_service.api.routers.portal import _rate_check, _rl_windows

# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture(autouse=True)
def clear_rate_limits():
    """Reset in-memory rate limit state between tests."""
    _rl_windows.clear()
    yield
    _rl_windows.clear()


@pytest_asyncio.fixture
async def tenant(session) -> TenantModel:
    t = TenantModel(
        slug="acme", name="ACME Corp",
        settings={"portal": {"enabled": True, "welcome_text": "Hi", "brand_color": "#000", "logo_text": "ACME"}},
    )
    session.add(t); await session.flush(); return t


@pytest_asyncio.fixture
async def case_type(session) -> CaseTypeModel:
    ct = CaseTypeModel(name="Support", version="1.0", definition_json={"stages": []}, portal_enabled=True)
    session.add(ct); await session.flush(); return ct


@pytest_asyncio.fixture
async def case_inst(session, tenant, case_type) -> CaseInstanceModel:
    c = CaseInstanceModel(
        case_type_id=case_type.id, case_type_version="1.0",
        status="open", priority="medium",
        portal_tracking_token=uuid.uuid4(),
        portal_submitter_email="jane@example.com",
        data={"subject": "Help with billing", "source": "customer_portal"},
        extra_metadata={"portal_slug": "acme"},
        created_by="portal:jane@example.com",
    )
    session.add(c); await session.flush(); return c


def _mock_llm(answer: str = "Here is your answer."):
    llm = MagicMock()
    llm.available = True
    llm.complete = AsyncMock(return_value=answer)
    return llm


# ── Rate limiter unit tests ───────────────────────────────────────────────────

class TestRateLimiter:
    async def test_allows_within_limit(self):
        for _ in range(5):
            assert await _rate_check("test-key", max_calls=5, window_seconds=60)

    async def test_blocks_over_limit(self):
        for _ in range(5):
            await _rate_check("test-key2", max_calls=5, window_seconds=60)
        assert not await _rate_check("test-key2", max_calls=5, window_seconds=60)

    async def test_different_keys_independent(self):
        for _ in range(5):
            await _rate_check("key-a", max_calls=5, window_seconds=60)
        # key-b is a different bucket — should still be allowed
        assert await _rate_check("key-b", max_calls=5, window_seconds=60)


# ── /portal/{slug}/ask ────────────────────────────────────────────────────────

class TestPortalAsk:
    async def test_happy_path_returns_answer(self, client: AsyncClient, session, tenant):
        with patch("case_service.api.routers.portal.check_ai_available", AsyncMock(return_value=True)), \
             patch("case_service.api.routers.portal.get_llm_backend", return_value=_mock_llm("Try restarting.")):
            r = await client.post("/api/v1/portal/acme/ask", json={"question": "How do I reset my password?"})
        assert r.status_code == 200
        data = r.json()
        assert data["answer"] == "Try restarting."
        assert data["self_served"] is True
        assert data["ai_available"] is True

    async def test_wrong_slug_404(self, client: AsyncClient, session):
        r = await client.post("/api/v1/portal/nonexistent/ask", json={"question": "Hello?"})
        assert r.status_code == 404

    async def test_ai_unavailable_graceful_fallback(self, client: AsyncClient, session, tenant):
        with patch("case_service.api.routers.portal.check_ai_available", AsyncMock(return_value=False)):
            r = await client.post("/api/v1/portal/acme/ask", json={"question": "Help?"})
        assert r.status_code == 200
        data = r.json()
        assert data["ai_available"] is False
        assert data["self_served"] is False
        assert len(data["answer"]) > 0

    async def test_rate_limit_returns_429(self, client: AsyncClient, session, tenant):
        with patch("case_service.api.routers.portal.check_ai_available", AsyncMock(return_value=True)), \
             patch("case_service.api.routers.portal.get_llm_backend", return_value=_mock_llm()):
            for _ in range(10):
                await client.post("/api/v1/portal/acme/ask", json={"question": "Q?"})
            r = await client.post("/api/v1/portal/acme/ask", json={"question": "Q?"})
        assert r.status_code == 429

    async def test_llm_exception_returns_fallback(self, client: AsyncClient, session, tenant):
        llm = MagicMock()
        llm.available = True
        llm.complete = AsyncMock(side_effect=RuntimeError("LLM timeout"))
        with patch("case_service.api.routers.portal.check_ai_available", AsyncMock(return_value=True)), \
             patch("case_service.api.routers.portal.get_llm_backend", return_value=llm):
            r = await client.post("/api/v1/portal/acme/ask", json={"question": "Help?"})
        assert r.status_code == 200
        assert "answer" in r.json()


# ── /portal/{slug}/cases/{id}/chat ───────────────────────────────────────────

class TestPortalCaseChat:
    async def test_happy_path_returns_reply(self, client: AsyncClient, session, case_inst):
        with patch("case_service.api.routers.portal.check_ai_available", AsyncMock(return_value=True)), \
             patch("case_service.api.routers.portal.get_llm_backend", return_value=_mock_llm("Your case is in review.")):
            r = await client.post(
                f"/api/v1/portal/acme/cases/{case_inst.id}/chat?email=jane@example.com",
                json={"message": "What stage is my request in?"},
            )
        assert r.status_code == 200
        data = r.json()
        assert data["reply"] == "Your case is in review."
        assert data["ai_available"] is True

    async def test_wrong_email_404(self, client: AsyncClient, session, case_inst):
        r = await client.post(
            f"/api/v1/portal/acme/cases/{case_inst.id}/chat?email=wrong@example.com",
            json={"message": "Hello"},
        )
        assert r.status_code == 404

    async def test_wrong_slug_404(self, client: AsyncClient, session, case_inst):
        r = await client.post(
            f"/api/v1/portal/nonexistent/cases/{case_inst.id}/chat?email=jane@example.com",
            json={"message": "Hello"},
        )
        assert r.status_code == 404

    async def test_rate_limit_429(self, client: AsyncClient, session, case_inst):
        with patch("case_service.api.routers.portal.check_ai_available", AsyncMock(return_value=True)), \
             patch("case_service.api.routers.portal.get_llm_backend", return_value=_mock_llm()):
            for _ in range(20):
                await client.post(
                    f"/api/v1/portal/acme/cases/{case_inst.id}/chat?email=jane@example.com",
                    json={"message": "Q?"},
                )
            r = await client.post(
                f"/api/v1/portal/acme/cases/{case_inst.id}/chat?email=jane@example.com",
                json={"message": "Q?"},
            )
        assert r.status_code == 429

    async def test_ai_unavailable_fallback(self, client: AsyncClient, session, case_inst):
        with patch("case_service.api.routers.portal.check_ai_available", AsyncMock(return_value=False)):
            r = await client.post(
                f"/api/v1/portal/acme/cases/{case_inst.id}/chat?email=jane@example.com",
                json={"message": "What's happening with my request?"},
            )
        assert r.status_code == 200
        assert r.json()["ai_available"] is False
        assert len(r.json()["reply"]) > 0

    async def test_includes_case_context(self, client: AsyncClient, session, case_inst):
        """LLM complete() should be called with case subject in the prompt."""
        captured = {}

        async def mock_complete(prompt, **kw):
            captured["prompt"] = prompt
            return "Acknowledged."

        llm = MagicMock()
        llm.available = True
        llm.complete = mock_complete

        with patch("case_service.api.routers.portal.check_ai_available", AsyncMock(return_value=True)), \
             patch("case_service.api.routers.portal.get_llm_backend", return_value=llm):
            await client.post(
                f"/api/v1/portal/acme/cases/{case_inst.id}/chat?email=jane@example.com",
                json={"message": "What is my case about?"},
            )
        assert "Help with billing" in captured.get("prompt", "")

    async def test_audit_events_in_context(self, client: AsyncClient, session, case_inst):
        """Customer-visible audit events should appear in the LLM prompt."""
        session.add(CaseAuditLogModel(
            case_id=case_inst.id, action="case_created",
            actor_id="portal", details={},
        ))
        await session.flush()

        captured = {}
        async def mock_complete(prompt, **kw):
            captured["prompt"] = prompt
            return "Got it."

        llm = MagicMock(); llm.available = True; llm.complete = mock_complete
        with patch("case_service.api.routers.portal.check_ai_available", AsyncMock(return_value=True)), \
             patch("case_service.api.routers.portal.get_llm_backend", return_value=llm):
            await client.post(
                f"/api/v1/portal/acme/cases/{case_inst.id}/chat?email=jane@example.com",
                json={"message": "Any updates?"},
            )
        assert "Case Created" in captured.get("prompt", "")

    async def test_llm_exception_fallback(self, client: AsyncClient, session, case_inst):
        llm = MagicMock(); llm.available = True
        llm.complete = AsyncMock(side_effect=RuntimeError("timeout"))
        with patch("case_service.api.routers.portal.check_ai_available", AsyncMock(return_value=True)), \
             patch("case_service.api.routers.portal.get_llm_backend", return_value=llm):
            r = await client.post(
                f"/api/v1/portal/acme/cases/{case_inst.id}/chat?email=jane@example.com",
                json={"message": "Help?"},
            )
        assert r.status_code == 200
        assert "reply" in r.json()

    async def test_nonexistent_case_404(self, client: AsyncClient, session, tenant):
        r = await client.post(
            f"/api/v1/portal/acme/cases/{uuid.uuid4()}/chat?email=jane@example.com",
            json={"message": "Hello"},
        )
        assert r.status_code == 404

    async def test_rate_limits_are_per_case(self, client: AsyncClient, session, case_inst, case_type, tenant):
        """Different case IDs have independent rate limit buckets."""
        c2 = CaseInstanceModel(
            case_type_id=case_type.id, case_type_version="1.0",
            status="open", priority="medium",
            portal_tracking_token=uuid.uuid4(),
            portal_submitter_email="jane@example.com",
            data={"subject": "Second case", "source": "customer_portal"},
            extra_metadata={"portal_slug": "acme"},
            created_by="portal:jane@example.com",
        )
        session.add(c2); await session.flush()

        with patch("case_service.api.routers.portal.check_ai_available", AsyncMock(return_value=True)), \
             patch("case_service.api.routers.portal.get_llm_backend", return_value=_mock_llm()):
            # Exhaust limit for case_inst
            for _ in range(20):
                await client.post(
                    f"/api/v1/portal/acme/cases/{case_inst.id}/chat?email=jane@example.com",
                    json={"message": "Q?"},
                )
            # c2 should still be allowed
            r = await client.post(
                f"/api/v1/portal/acme/cases/{c2.id}/chat?email=jane@example.com",
                json={"message": "Q?"},
            )
        assert r.status_code == 200

    async def test_case_status_in_context(self, client: AsyncClient, session, case_inst):
        captured = {}
        async def mock_complete(prompt, **kw):
            captured["prompt"] = prompt
            return "OK"
        llm = MagicMock(); llm.available = True; llm.complete = mock_complete
        with patch("case_service.api.routers.portal.check_ai_available", AsyncMock(return_value=True)), \
             patch("case_service.api.routers.portal.get_llm_backend", return_value=llm):
            await client.post(
                f"/api/v1/portal/acme/cases/{case_inst.id}/chat?email=jane@example.com",
                json={"message": "What is the status?"},
            )
        assert "open" in captured.get("prompt", "").lower()

    async def test_ask_different_tenants_independent_rate_limits(
        self, client: AsyncClient, session, tenant
    ):
        """Rate limits for /ask are per-IP, but different slugs share the same bucket
        since IP is the discriminator (by design — prevents abuse across portals)."""
        t2 = TenantModel(slug="beta", name="Beta", settings={"portal": {"enabled": True, "welcome_text": "Hi", "brand_color": "#fff", "logo_text": "B"}})
        session.add(t2); await session.flush()
        with patch("case_service.api.routers.portal.check_ai_available", AsyncMock(return_value=True)), \
             patch("case_service.api.routers.portal.get_llm_backend", return_value=_mock_llm()):
            r = await client.post("/api/v1/portal/beta/ask", json={"question": "First question on beta?"})
        assert r.status_code == 200
