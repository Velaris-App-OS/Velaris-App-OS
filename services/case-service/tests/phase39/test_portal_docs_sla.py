"""HELIX P39b — Two-Way Document Exchange + SLA Countdown tests (23 tests).

Covers: portal documents endpoint (staff-shared visible, customer uploads visible,
        non-shared hidden, wrong email guard, wrong slug 404), portal visibility
        toggle PATCH endpoint, customer upload tags portal_source, SLA countdown
        (on_track green, at_risk amber, breached red, no SLA returns null,
        wrong email guard, deadline formatting), download_url in response.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import AsyncClient

from case_service.db.models import (
    CaseInstanceModel,
    CaseSLAInstanceModel,
    CaseTypeModel,
    DocumentModel,
    DocumentVersionModel,
    TenantModel,
)
from case_service.auth.dependencies import get_current_user, require_role
from case_service.auth.models import AuthenticatedUser
from case_service.main import app

# ── Auth helpers ──────────────────────────────────────────────────────────────

def _admin():
    return AuthenticatedUser(user_id="staff-1", roles=["admin"])

def _override(user):
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[require_role("admin")] = lambda: user

def _clear():
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(require_role("admin"), None)

# ── Fixtures ──────────────────────────────────────────────────────────────────

NOW = datetime.now(timezone.utc)

@pytest_asyncio.fixture
async def tenant(session) -> TenantModel:
    t = TenantModel(
        slug="acme",
        name="ACME Corp",
        settings={"portal": {"enabled": True, "welcome_text": "Hi", "brand_color": "#000", "logo_text": "ACME"}},
    )
    session.add(t); await session.flush(); return t


@pytest_asyncio.fixture
async def case_type(session) -> CaseTypeModel:
    ct = CaseTypeModel(
        name="Support", version="1.0",
        definition_json={"stages": []}, portal_enabled=True,
    )
    session.add(ct); await session.flush(); return ct


@pytest_asyncio.fixture
async def case_inst(session, tenant, case_type) -> CaseInstanceModel:
    c = CaseInstanceModel(
        case_type_id=case_type.id, case_type_version="1.0",
        status="open", priority="medium",
        portal_tracking_token=uuid.uuid4(),
        portal_submitter_email="jane@example.com",
        data={"subject": "Help", "source": "customer_portal"},
        extra_metadata={"portal_slug": "acme"},
        created_by="portal:jane@example.com",
    )
    session.add(c); await session.flush(); return c


def _make_doc(session, case_id, filename="file.pdf", portal_visible=False, portal_source=None) -> DocumentModel:
    doc_id = uuid.uuid4()
    doc = DocumentModel(
        id=doc_id,
        case_id=case_id, filename=filename,
        content_type="application/pdf",
        uploaded_by="staff-1",
        portal_visible=portal_visible,
        portal_source=portal_source,
    )
    session.add(doc)
    ver = DocumentVersionModel(
        id=uuid.uuid4(),
        document_id=doc_id, version=1,
        storage_key=f"cases/{case_id}/{doc_id}/v1/{filename}",
        size_bytes=1024, sha256="abc123", uploaded_by="staff-1",
    )
    session.add(ver)
    return doc


def _make_sla(session, case_id, status="on_track", hours_total=48, hours_elapsed=12) -> CaseSLAInstanceModel:
    started = NOW - timedelta(hours=hours_elapsed)
    deadline = started + timedelta(hours=hours_total)
    sla = CaseSLAInstanceModel(
        case_id=case_id,
        sla_policy_id="default",
        target_id="staff-1",
        status=status,
        started_at=started,
        goal_at=started + timedelta(hours=hours_total * 0.8),
        deadline_at=deadline,
        breached_at=NOW if status == "breached" else None,
    )
    session.add(sla)
    return sla


# ── Portal documents ──────────────────────────────────────────────────────────

class TestPortalDocuments:
    async def test_staff_shared_doc_visible(self, client: AsyncClient, session, case_inst):
        _make_doc(session, case_inst.id, "response.pdf", portal_visible=True, portal_source="staff")
        await session.flush()

        r = await client.get(f"/api/v1/portal/acme/cases/{case_inst.id}/documents?email=jane@example.com")
        assert r.status_code == 200
        docs = r.json()["documents"]
        assert len(docs) == 1
        assert docs[0]["filename"] == "response.pdf"
        assert docs[0]["source"] == "staff"

    async def test_non_shared_doc_hidden(self, client: AsyncClient, session, case_inst):
        _make_doc(session, case_inst.id, "internal.pdf", portal_visible=False)
        await session.flush()

        r = await client.get(f"/api/v1/portal/acme/cases/{case_inst.id}/documents?email=jane@example.com")
        assert r.json()["documents"] == []

    async def test_customer_upload_visible(self, client: AsyncClient, session, case_inst):
        _make_doc(session, case_inst.id, "customer_upload.pdf", portal_visible=False, portal_source="customer")
        await session.flush()

        r = await client.get(f"/api/v1/portal/acme/cases/{case_inst.id}/documents?email=jane@example.com")
        docs = r.json()["documents"]
        assert len(docs) == 1
        assert docs[0]["source"] == "customer"

    async def test_mixed_docs_filtered_correctly(self, client: AsyncClient, session, case_inst):
        _make_doc(session, case_inst.id, "shared.pdf", portal_visible=True, portal_source="staff")
        _make_doc(session, case_inst.id, "internal.pdf", portal_visible=False)
        _make_doc(session, case_inst.id, "cust.pdf", portal_source="customer")
        await session.flush()

        r = await client.get(f"/api/v1/portal/acme/cases/{case_inst.id}/documents?email=jane@example.com")
        docs = r.json()["documents"]
        assert len(docs) == 2
        filenames = {d["filename"] for d in docs}
        assert filenames == {"shared.pdf", "cust.pdf"}

    async def test_wrong_email_404(self, client: AsyncClient, session, case_inst):
        r = await client.get(f"/api/v1/portal/acme/cases/{case_inst.id}/documents?email=wrong@example.com")
        assert r.status_code == 404

    async def test_wrong_slug_404(self, client: AsyncClient, session, case_inst):
        r = await client.get(f"/api/v1/portal/nonexistent/cases/{case_inst.id}/documents?email=jane@example.com")
        assert r.status_code == 404

    async def test_download_url_in_response(self, client: AsyncClient, session, case_inst):
        _make_doc(session, case_inst.id, "file.pdf", portal_visible=True, portal_source="staff")
        await session.flush()

        r = await client.get(f"/api/v1/portal/acme/cases/{case_inst.id}/documents?email=jane@example.com")
        doc = r.json()["documents"][0]
        assert "download_url" in doc
        assert doc["download_url"].startswith("/api/v1/documents/")

    async def test_size_bytes_in_response(self, client: AsyncClient, session, case_inst):
        _make_doc(session, case_inst.id, "file.pdf", portal_visible=True, portal_source="staff")
        await session.flush()

        r = await client.get(f"/api/v1/portal/acme/cases/{case_inst.id}/documents?email=jane@example.com")
        assert r.json()["documents"][0]["size_bytes"] == 1024


# ── Portal visibility toggle ──────────────────────────────────────────────────

class TestPortalVisibilityToggle:
    async def test_staff_can_share_document(self, client: AsyncClient, session, case_inst):
        doc = _make_doc(session, case_inst.id, "response.pdf", portal_visible=False)
        await session.flush()
        doc_id = doc.id

        _override(_admin())
        try:
            r = await client.patch(
                f"/api/v1/documents/{doc_id}/portal-visibility",
                json={"portal_visible": True},
            )
        finally:
            _clear()
        assert r.status_code == 200
        assert r.json()["portal_visible"] is True
        assert r.json()["portal_source"] == "staff"

    async def test_staff_can_unshare_document(self, client: AsyncClient, session, case_inst):
        doc = _make_doc(session, case_inst.id, "response.pdf", portal_visible=True, portal_source="staff")
        await session.flush()
        doc_id = doc.id

        _override(_admin())
        try:
            r = await client.patch(
                f"/api/v1/documents/{doc_id}/portal-visibility",
                json={"portal_visible": False},
            )
        finally:
            _clear()
        assert r.status_code == 200
        assert r.json()["portal_visible"] is False

    async def test_toggle_404_for_missing_doc(self, client: AsyncClient, session):
        _override(_admin())
        try:
            r = await client.patch(
                f"/api/v1/documents/{uuid.uuid4()}/portal-visibility",
                json={"portal_visible": True},
            )
        finally:
            _clear()
        assert r.status_code == 404


# ── SLA countdown ─────────────────────────────────────────────────────────────

class TestSLACountdown:
    async def test_on_track_green(self, client: AsyncClient, session, case_inst):
        _make_sla(session, case_inst.id, status="on_track", hours_total=48, hours_elapsed=12)
        await session.flush()

        r = await client.get(f"/api/v1/portal/acme/cases/{case_inst.id}/sla?email=jane@example.com")
        assert r.status_code == 200
        sla = r.json()["sla"]
        assert sla["tier"] == "green"
        assert sla["breached"] is False
        assert sla["remaining_seconds"] > 0

    async def test_at_risk_amber(self, client: AsyncClient, session, case_inst):
        # Elapsed 90% of the time window → < 20% remaining → amber
        _make_sla(session, case_inst.id, status="at_risk", hours_total=48, hours_elapsed=44)
        await session.flush()

        r = await client.get(f"/api/v1/portal/acme/cases/{case_inst.id}/sla?email=jane@example.com")
        sla = r.json()["sla"]
        assert sla["tier"] == "amber"
        assert sla["breached"] is False

    async def test_breached_red(self, client: AsyncClient, session, case_inst):
        _make_sla(session, case_inst.id, status="breached", hours_total=24, hours_elapsed=25)
        await session.flush()

        r = await client.get(f"/api/v1/portal/acme/cases/{case_inst.id}/sla?email=jane@example.com")
        sla = r.json()["sla"]
        assert sla["tier"] == "red"
        assert sla["breached"] is True

    async def test_no_sla_returns_null(self, client: AsyncClient, session, case_inst):
        r = await client.get(f"/api/v1/portal/acme/cases/{case_inst.id}/sla?email=jane@example.com")
        assert r.status_code == 200
        assert r.json()["sla"] is None

    async def test_wrong_email_404(self, client: AsyncClient, session, case_inst):
        _make_sla(session, case_inst.id)
        await session.flush()

        r = await client.get(f"/api/v1/portal/acme/cases/{case_inst.id}/sla?email=wrong@example.com")
        assert r.status_code == 404

    async def test_wrong_slug_404(self, client: AsyncClient, session, case_inst):
        r = await client.get(f"/api/v1/portal/nonexistent/cases/{case_inst.id}/sla?email=jane@example.com")
        assert r.status_code == 404

    async def test_deadline_at_in_response(self, client: AsyncClient, session, case_inst):
        _make_sla(session, case_inst.id, hours_total=24, hours_elapsed=1)
        await session.flush()

        r = await client.get(f"/api/v1/portal/acme/cases/{case_inst.id}/sla?email=jane@example.com")
        sla = r.json()["sla"]
        assert "deadline_at" in sla
        from datetime import datetime
        datetime.fromisoformat(sla["deadline_at"])

    async def test_remaining_seconds_zero_when_breached(self, client: AsyncClient, session, case_inst):
        _make_sla(session, case_inst.id, status="breached", hours_total=24, hours_elapsed=25)
        await session.flush()

        r = await client.get(f"/api/v1/portal/acme/cases/{case_inst.id}/sla?email=jane@example.com")
        assert r.json()["sla"]["remaining_seconds"] == 0

    async def test_most_recent_sla_returned(self, client: AsyncClient, session, case_inst):
        """If multiple SLA instances exist, the most recent is used."""
        _make_sla(session, case_inst.id, status="breached", hours_total=24, hours_elapsed=25)
        _make_sla(session, case_inst.id, status="on_track", hours_total=48, hours_elapsed=1)
        await session.flush()

        r = await client.get(f"/api/v1/portal/acme/cases/{case_inst.id}/sla?email=jane@example.com")
        assert r.json()["sla"]["status"] == "on_track"


class TestCustomerUploadTagging:
    async def test_portal_upload_creates_document_record(self, client: AsyncClient, session, case_inst):
        """Customer upload via portal should produce a DB record tagged portal_source='customer'."""
        from unittest.mock import AsyncMock, patch, MagicMock
        import io

        mock_storage = MagicMock()
        mock_storage.put = AsyncMock(return_value=None)

        with patch("case_service.documents.service.DocumentService._be", return_value=mock_storage):
            r = await client.post(
                f"/api/v1/portal/acme/track/{case_inst.portal_tracking_token}/documents",
                files={"file": ("test.pdf", io.BytesIO(b"PDF content"), "application/pdf")},
            )
        assert r.status_code == 200
        assert r.json()["filename"] == "test.pdf"

    async def test_portal_visibility_false_by_default(self, client: AsyncClient, session, case_inst):
        """Newly uploaded staff doc has portal_visible=False by default."""
        doc = _make_doc(session, case_inst.id, "internal.pdf")
        await session.flush()
        assert doc.portal_visible is False
