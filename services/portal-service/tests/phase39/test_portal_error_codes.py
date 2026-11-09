"""Error-catalog phase 4 — `VEL-PORTAL-*` codes on the customer portal.

53 of `portal.py`'s 64 raise sites now carry a catalog code. Most of them are on
`public_router`, which answers anonymous customers, so these tests pin the two
properties that matter on an unauthenticated surface:

  * the code is **present and correct** in the response body, next to the
    `detail` every existing client already reads; and
  * the code does **not say more than the message does**. Portal messages merge
    "no such case" with "not yours" on purpose so an anonymous caller cannot
    enumerate case ids. A per-condition code would hand that oracle straight
    back in a field just as easy to read, so the merged conditions must answer
    with one identical code.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient

from velaris_shared.core import error_codes as ec
from velaris_shared.db.models import CaseInstanceModel, CaseTypeModel, TenantModel


def _code(response) -> str | None:
    """The catalog code on a response, or None when it carries no code."""
    return (response.json().get("error") or {}).get("code")


# ── Fixtures ──────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def tenant(session) -> TenantModel:
    t = TenantModel(
        slug="acme", name="ACME Corp",
        settings={"portal": {"enabled": True, "welcome_text": "Hi",
                             "brand_color": "#000", "logo_text": "ACME"}},
    )
    session.add(t); await session.commit(); return t


@pytest_asyncio.fixture
async def disabled_tenant(session) -> TenantModel:
    t = TenantModel(
        slug="dormant", name="Dormant Ltd",
        settings={"portal": {"enabled": False}},
    )
    session.add(t); await session.commit(); return t


@pytest_asyncio.fixture
async def case_type(session) -> CaseTypeModel:
    ct = CaseTypeModel(
        name="Support", version="1.0",
        definition_json={"stages": []}, portal_enabled=True,
    )
    session.add(ct); await session.commit(); return ct


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
    session.add(c); await session.commit(); return c


# ── The code reaches the client ───────────────────────────────────

class TestCodeIsAttached:
    async def test_unknown_portal_carries_notfound(self, client: AsyncClient):
        r = await client.get("/api/v1/portal/nope")
        assert r.status_code == 404
        assert _code(r) == ec.PORTAL_NOTFOUND_404

    async def test_disabled_portal_is_inactive_not_notfound(
        self, client: AsyncClient, disabled_tenant,
    ):
        """'Not built' and 'turned off' have different fixes — different codes."""
        r = await client.get("/api/v1/portal/dormant")
        assert r.status_code == 403
        assert _code(r) == ec.PORTAL_INACTIVE_403

    async def test_detail_is_untouched_by_the_code(self, client: AsyncClient):
        """The code is additive: `detail` stays exactly where clients read it."""
        r = await client.get("/api/v1/portal/nope")
        assert r.json()["detail"] == "Portal 'nope' not found"

    async def test_docs_link_is_present_for_studio_to_use(self, client: AsyncClient):
        """The portal UI ignores this; Studio's catalog modal follows it."""
        r = await client.get("/api/v1/portal/nope")
        assert r.json()["error"]["docs"].endswith(ec.PORTAL_NOTFOUND_404)

    async def test_unknown_tracking_token_is_an_access_failure(
        self, client: AsyncClient, tenant,
    ):
        r = await client.get(f"/api/v1/portal/acme/track/{uuid.uuid4()}")
        assert r.status_code == 404
        assert _code(r) == ec.PORTAL_ACCESS_404


# ── The code must not become an enumeration oracle ────────────────

class TestNoOracle:
    """A code that distinguishes 'no such case' from 'not yours' would undo the
    merged message that exists precisely to hide that difference."""

    async def test_wrong_email_and_missing_case_answer_identically(
        self, client: AsyncClient, case_inst,
    ):
        real_case_wrong_email = await client.get(
            f"/api/v1/portal/acme/cases/{case_inst.id}/documents?email=mallory@evil.com")
        no_such_case = await client.get(
            f"/api/v1/portal/acme/cases/{uuid.uuid4()}/documents?email=jane@example.com")

        assert real_case_wrong_email.status_code == no_such_case.status_code == 404
        assert _code(real_case_wrong_email) == _code(no_such_case) == ec.PORTAL_ACCESS_404
        # The message was already uniform; the code must not out-resolve it.
        assert real_case_wrong_email.json()["detail"] == no_such_case.json()["detail"]

    async def test_the_same_holds_on_the_sla_surface(
        self, client: AsyncClient, case_inst,
    ):
        wrong_email = await client.get(
            f"/api/v1/portal/acme/cases/{case_inst.id}/sla?email=mallory@evil.com")
        missing = await client.get(
            f"/api/v1/portal/acme/cases/{uuid.uuid4()}/sla?email=jane@example.com")

        assert _code(wrong_email) == _code(missing) == ec.PORTAL_ACCESS_404

    async def test_a_missing_portal_stays_distinguishable_from_a_missing_case(
        self, client: AsyncClient, case_inst,
    ):
        """The portal slug is public — hiding it protects nobody and makes a
        genuine typo undiagnosable."""
        bad_slug = await client.get(
            f"/api/v1/portal/nope/cases/{case_inst.id}/documents?email=jane@example.com")
        assert _code(bad_slug) == ec.PORTAL_NOTFOUND_404


# ── The 403 that should always have been a 404 ────────────────────

class TestBankDetailsStatusAlignment:
    async def test_email_mismatch_answers_404_like_its_siblings(
        self, client: AsyncClient, case_inst,
    ):
        """Six sibling sites answer this exact condition with 404, and the
        message ("or case not found") was already written for 404. The lone 403
        told an anonymous caller that the case existed."""
        r = await client.post(
            f"/api/v1/portal/acme/cases/{case_inst.id}/bank-details/submit",
            json={"email": "mallory@evil.com", "otp": "123456",
                  "account_name": "M", "account_number": "12345678",
                  "sort_code": "112233"},
        )
        assert r.status_code == 404
        assert _code(r) == ec.PORTAL_ACCESS_404


# ── Registry hygiene ──────────────────────────────────────────────

class TestRegistry:
    def test_every_portal_code_is_documented(self):
        """`coded()` refuses an undocumented code at raise time, so a code that
        is in the module but not in KNOWN_CODES would fail only on the unlucky
        request that hits it."""
        declared = {v for k, v in vars(ec).items()
                    if k.startswith("PORTAL_") and isinstance(v, str)}
        assert declared, "no portal codes found — did the prefix change?"
        assert declared <= ec.KNOWN_CODES

    def test_bare_validation_sites_were_left_bare(self):
        """The 12 uncoded raises are pure request-shape validation. If this
        count moves, it should move deliberately — either new validation was
        added, or something that deserved a code was missed. (12th, 2026-10-28:
        a portal form's 422 with `field_errors`, Form Builder part 10.)"""
        from pathlib import Path
        src = (Path(__file__).resolve().parents[3] / "portal-service" / "portal_service"
               / "api" / "routers" / "portal.py")   # R5e step 8
        assert src.read_text().count("raise HTTPException") == 12


class TestErasedCaseTrackingLinkIsDead:
    """A tracking token is a bearer credential — erasure has to revoke it.

    `GET /portal/{slug}/track/{token}` authenticates nobody: holding the link IS
    the authorization. An old confirmation email must not still open a case that
    has been anonymized under Article 17.
    """

    async def test_the_link_works_before_erasure(
        self, client: AsyncClient, tenant, case_inst,
    ):
        """HAPPY PATH first — otherwise the 404 below proves nothing."""
        r = await client.get(
            f"/api/v1/portal/acme/track/{case_inst.portal_tracking_token}")
        assert r.status_code == 200, r.text

    async def test_the_same_link_404s_after_erasure(
        self, client: AsyncClient, session, tenant, case_inst,
    ):
        from case_service.compliance import retention

        token = case_inst.portal_tracking_token
        assert (await client.get(
            f"/api/v1/portal/acme/track/{token}")).status_code == 200

        retention._strip_submitter_columns(case_inst)
        case_inst.data = {**(case_inst.data or {}),
                          retention.ANON_MARKER: "2026-01-01T00:00:00+00:00"}
        session.add(case_inst)
        await session.commit()

        r = await client.get(f"/api/v1/portal/acme/track/{token}")
        assert r.status_code == 404, "an erased case must not open by old link"
        assert _code(r) == ec.PORTAL_ACCESS_404
