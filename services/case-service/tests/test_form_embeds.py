"""Embed field — another site's page in a form, approved sites only (2026-10-29).

A tenant administrator approves sites; a designer places an embed from one of
them; a form naming any other site is not saved; the portal shows embeds only
when the tenant allows it. Tested as the least-privileged users who should
pass, and as users who must be refused.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from velaris_shared.db.models import TenantModel, UserDirectoryModel
from velaris_shared.forms import embed, validate
from velaris_shared.forms.portal import portal_definition

from case_service.main import app
from case_service.tenancy import identity as tenancy
from tests.conftest import TestSessionFactory  # type: ignore[attr-defined]
from tests.conftest import _headers  # type: ignore[attr-defined]

ADMIN = _headers("ada", ["admin"])               # tenant "acme"
DESIGNER = _headers("dora", ["designer"])        # tenant "acme"
WORKER = _headers("wendy", ["case_worker"])      # tenant "acme"
OTHER_ADMIN = _headers("otto", ["admin"])        # tenant "other"
SETTINGS = "/api/v1/forms/embed-settings"
VIDEO = "https://www.youtube-nocookie.com/embed/abc"


def _form(url: str) -> dict:
    return {"sections": [{"id": "s", "title": "S", "order": 0, "fields": [
        {"id": "v", "type": "embed", "label": "Intro video", "field_key": "v", "url": url, "height": 315},
        {"id": "n", "type": "text", "label": "Name", "field_key": "n"},
    ]}]}


@pytest_asyncio.fixture
async def api():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def world():
    tenancy.clear_cache()
    async with TestSessionFactory() as db:
        db.add_all([TenantModel(id=uuid.uuid4(), slug="default", name="Default"),
                    TenantModel(id=uuid.uuid4(), slug="acme", name="Acme"),
                    TenantModel(id=uuid.uuid4(), slug="other", name="Other")])
        await db.flush()
        for uid, t in (("ada", "acme"), ("dora", "acme"), ("wendy", "acme"), ("otto", "other")):
            db.add(UserDirectoryModel(id=uuid.uuid4(), user_id=uid, tenant_id=t))
        await db.commit()
    yield
    tenancy.clear_cache()


# ── the rules ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url,origin", [
    ("https://www.youtube-nocookie.com/embed/x?y=1", "https://www.youtube-nocookie.com"),
    ("https://Example.COM:8443/a", "https://example.com:8443"),
    ("https://example.com:443/", "https://example.com"),
    ("http://example.com/", None),                      # not https
    ("javascript:alert(1)", None),
    ("https://user:pw@example.com/", None),             # credentials in the address
    ("https://127.0.0.1/", None),                       # an address, not a name
    ("https://localhost/", None),
    ("https://exa mple.com/", None),
    ("//example.com/", None),
    ("data:text/html,<script>", None),
])
def test_only_https_sites_with_a_real_name_have_an_origin(url, origin):
    assert embed.origin_of(url) == origin


def test_an_embed_holds_no_value():
    clean, errors = validate(_form(VIDEO), {"v": "<script>", "n": "Ann"})
    assert errors == [] and clean == {"n": "Ann"}


def test_the_portal_shows_an_embed_only_when_the_tenant_allows_it():
    keep = lambda origins: [f["type"] for f in portal_definition(_form(VIDEO), embed_origins=origins)["sections"][0]["fields"]]
    assert keep(None) == ["text"]                                   # unset: never on the portal
    assert keep(["https://example.com"]) == ["text"]                # allowed, but not this site
    assert keep(["https://www.youtube-nocookie.com"]) == ["embed", "text"]


# ── who may approve sites ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unset_means_no_site_and_any_user_can_read_it(api, world):
    r = await api.get(SETTINGS, headers=WORKER)
    assert r.status_code == 200
    d = r.json()
    assert d["allowed_origins"] == [] and d["portal"] is False
    # No platform policy set (2026-11-04): tenants keep their own lists, typing allowed — as before.
    assert d["may_add"] is True and d["allow_forms"] is True and d["platform_origins"] == [] and d["blocked"] == []


@pytest.mark.asyncio
async def test_an_admin_approves_sites_and_a_designer_or_worker_cannot(api, world):
    body = {"allowed_origins": [VIDEO, "https://maps.example.org/x"], "portal": False}
    for who in (DESIGNER, WORKER):
        assert (await api.put(SETTINGS, headers=who, json=body)).status_code == 403
    r = await api.put(SETTINGS, headers=ADMIN, json=body)
    assert r.status_code == 200, r.text
    assert r.json()["allowed_origins"] == ["https://www.youtube-nocookie.com", "https://maps.example.org"]
    assert (await api.get(SETTINGS, headers=WORKER)).json()["allowed_origins"] == r.json()["allowed_origins"]
    # another tenant's list is its own
    assert (await api.get(SETTINGS, headers=OTHER_ADMIN)).json()["allowed_origins"] == []


@pytest.mark.asyncio
async def test_unsafe_sites_and_our_own_address_are_refused(api, world):
    for bad in ("http://example.com", "https://127.0.0.1", "javascript:alert(1)"):
        r = await api.put(SETTINGS, headers=ADMIN, json={"allowed_origins": [bad]})
        assert r.status_code == 422, bad
    r = await api.put(SETTINGS, headers={**ADMIN, "Origin": "https://studio.example.com"},
                      json={"allowed_origins": ["https://studio.example.com/anything"]})
    assert r.status_code == 422 and "own address" in r.text


# ── saving a form ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_form_saves_an_embed_only_from_an_approved_site(api, world):
    r = await api.post("/api/v1/forms", headers=DESIGNER, json={"name": "Intro", "version": "1", "definition_json": _form(VIDEO)})
    assert r.status_code == 422 and "Intro video" in r.text          # nothing approved yet
    await api.put(SETTINGS, headers=ADMIN, json={"allowed_origins": ["https://www.youtube-nocookie.com"]})
    r = await api.post("/api/v1/forms", headers=DESIGNER, json={"name": "Intro", "version": "1", "definition_json": _form(VIDEO)})
    assert r.status_code == 201, r.text
    form_id = r.json()["id"]
    r = await api.patch(f"/api/v1/forms/{form_id}", headers=DESIGNER,
                        json={"definition_json": _form("https://evil.example.net/x")})
    assert r.status_code == 422
    # a form without embeds is saved as before
    r = await api.patch(f"/api/v1/forms/{form_id}", headers=DESIGNER, json={"definition_json": {"sections": []}})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_a_platform_form_is_checked_against_the_savers_tenant(api, world):
    """A default-tenant account saves platform defaults (no tenant): the check
    must not skip them (found live 2026-10-29)."""
    op = _headers("opal", ["designer"])                 # no directory row: the default tenant
    r = await api.post("/api/v1/forms", headers=op, json={"name": "Platform intro", "version": "1", "definition_json": _form(VIDEO)})
    assert r.status_code == 422, r.text




# ── Allowed sites: the platform operator's policy (2026-11-04) ─────────────────

OPERATOR = _headers("opal", ["admin"])          # tenant "default" — the platform operator
POLICY = "/api/v1/forms/allowed-sites/policy"


@pytest_asyncio.fixture
async def operator(world):
    async with TestSessionFactory() as db:
        db.add(UserDirectoryModel(id=uuid.uuid4(), user_id="opal", tenant_id="default"))
        await db.commit()


@pytest.mark.asyncio
async def test_only_the_operator_reads_or_sets_the_policy(api, operator):
    for who in (ADMIN, DESIGNER, WORKER):
        assert (await api.get(POLICY, headers=who)).status_code == 403
        assert (await api.put(POLICY, headers=who, json={"platform_origins": [VIDEO]})).status_code == 403
    r = await api.get(POLICY, headers=OPERATOR)
    assert r.status_code == 200 and r.json() == {"platform_origins": [], "blocked": [],
                                                 "tenants_may_add": True, "allow_forms": True}


@pytest.mark.asyncio
async def test_platform_sites_reach_every_tenant_and_blocked_ones_none(api, operator):
    r = await api.put(SETTINGS, headers=ADMIN, json={"allowed_origins": ["https://maps.example.org"]})
    assert r.status_code == 200
    r = await api.put(POLICY, headers=OPERATOR, json={
        "platform_origins": [VIDEO], "blocked": ["https://maps.example.org"],
        "tenants_may_add": True, "allow_forms": True})
    assert r.status_code == 200, r.text
    acme = (await api.get(SETTINGS, headers=WORKER)).json()
    assert acme["allowed_origins"] == ["https://www.youtube-nocookie.com"]       # its blocked own site is gone
    assert acme["own_origins"] == ["https://maps.example.org"]
    assert (await api.get(SETTINGS, headers=OTHER_ADMIN)).json()["allowed_origins"] == ["https://www.youtube-nocookie.com"]
    # A blocked site cannot be approved again by a tenant.
    r = await api.put(SETTINGS, headers=ADMIN, json={"allowed_origins": ["https://maps.example.org"]})
    assert r.status_code == 422 and "Blocked" in r.text
    # Approved AND blocked is refused.
    r = await api.put(POLICY, headers=OPERATOR, json={"platform_origins": [VIDEO], "blocked": [VIDEO]})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_tenants_may_be_kept_from_adding_their_own(api, operator):
    await api.put(POLICY, headers=OPERATOR, json={"platform_origins": [], "blocked": [],
                                                   "tenants_may_add": False, "allow_forms": True})
    r = await api.put(SETTINGS, headers=ADMIN, json={"allowed_origins": [VIDEO]})
    assert r.status_code == 403
    assert (await api.get(SETTINGS, headers=WORKER)).json()["may_add"] is False


@pytest.mark.asyncio
async def test_a_demo_tenant_never_keeps_its_own_list(world):
    settings = {"demo": {"expires_at": "2099-01-01T00:00:00+00:00"},
                "form_embeds": {"allowed_origins": ["https://evil.example"], "portal": True}}
    eff = embed.effective(settings, embed.policy_of(None))
    assert eff["may_add"] is False and eff["allowed_origins"] == []


@pytest.mark.asyncio
async def test_typing_off_refuses_a_form_and_the_portal_drops_it(api, operator):
    await api.put(SETTINGS, headers=ADMIN, json={"allowed_origins": [VIDEO], "portal": True})
    await api.put(POLICY, headers=OPERATOR, json={"platform_origins": [], "blocked": [],
                                                   "tenants_may_add": True, "allow_forms": False})
    form = _form(VIDEO)
    form["sections"][0]["fields"][0]["permissions"] = ["forms"]
    r = await api.post("/api/v1/forms", headers=DESIGNER, json={"name": "Typed", "version": "1", "definition_json": form})
    assert r.status_code == 422 and "Typing" in r.text
    form["sections"][0]["fields"][0]["permissions"] = []
    r = await api.post("/api/v1/forms", headers=DESIGNER, json={"name": "Plain", "version": "1", "definition_json": form})
    assert r.status_code == 201, r.text


@pytest.mark.asyncio
async def test_every_change_is_on_the_record(api, operator):
    from sqlalchemy import select
    from velaris_shared.db.models import SecurityEventModel
    await api.put(SETTINGS, headers=ADMIN, json={"allowed_origins": [VIDEO]})
    await api.put(POLICY, headers=OPERATOR, json={"platform_origins": [], "blocked": [VIDEO]})
    async with TestSessionFactory() as db:
        rows = (await db.execute(select(SecurityEventModel).where(
            SecurityEventModel.event_type == "allowed_sites_changed"))).scalars().all()
    scopes = sorted(r.details["scope"] for r in rows)
    assert scopes == ["platform", "tenant"]
    tenant_row = next(r for r in rows if r.details["scope"] == "tenant")
    assert tenant_row.details["added"] == ["https://www.youtube-nocookie.com"] and tenant_row.user_id == "ada"


# ── every form-writing path asks the same check (2026-11-04) ───────────────────

@pytest.mark.asyncio
async def test_the_shared_check_answers_for_every_writer(world):
    from velaris_shared.forms.embed import form_embed_problem
    async with TestSessionFactory() as db:
        acme = (await db.execute(__import__("sqlalchemy").select(TenantModel).where(TenantModel.slug == "acme"))).scalar_one()
        assert await form_embed_problem(db, acme.id, _form(VIDEO)) is not None          # nothing approved
        assert await form_embed_problem(db, acme.id, {"sections": []}) is None          # no embed: nothing to check
        acme.settings = {"form_embeds": {"allowed_origins": ["https://www.youtube-nocookie.com"]}}
        await db.commit()
        assert await form_embed_problem(db, acme.id, _form(VIDEO)) is None
        assert await form_embed_problem(db, acme.id, _form("https://evil.example/x")) is not None


@pytest.mark.asyncio
async def test_an_app_bundle_cannot_bring_an_unapproved_embed(api, world):
    """App import wrote forms without the embed check (an unapproved embed was
    still never drawn — the renderer checks — but it is now refused at the door)."""
    r = await api.post("/api/v1/apps/import", headers=ADMIN, json={"bundle": {
        "forms": [{"name": "Imported", "version": "1", "definition_json": _form("https://evil.example/x")}]}})
    assert r.status_code == 200, r.text
    assert r.json()["forms"][0]["action"] == "refused"
