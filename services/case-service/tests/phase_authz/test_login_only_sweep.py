"""The login-only sweep (2026-09-30) — three shapes, found by accident three times.

8c, 8g and 8h each turned up by chance while building something else: a route
whose only gate was a login, a route that trusted the tenant in its URL, and a
write that stored a case with no tenant. This sweep went looking for all three.

1. **Login-only.** A scan of the built route table — every route whose only
   gate is `get_current_user`, helper calls followed three deep — found 270.
   Proven in-process as a VIEWER before the fix: created a platform-wide region,
   created a work queue stored with NO tenant, and listed connectors, sync
   destinations, platform logs and branches. `DELETE /case-types/{id}` took no
   identity parameter at all, so any login hard-deleted a case type.
2. **Tenant from the caller's input.** SSO providers, SCIM tokens, channel
   endpoints, directory entries, access groups and portals stored the body's
   `tenant_id` as sent: a tenant admin could register an identity provider — or
   mint a SCIM token — for another tenant. The SAML tester trusted the URL.
3. **Tenantless writes.** A sync run exported EVERY tenant's cases to any
   destination, whoever owned it. Cases made by a caller with no tenant claim,
   by an SMS on a tenant's number, by a schedule or by a DB migration onto a
   shared case type were stored with NO tenant — readable by every tenant.

`test_the_login_only_set_does_not_grow` is the guard for shape 1: a route that
is still login-only is listed below with the reason it may be, and a new one
fails the build until somebody decides.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import inspect
import re
import sys
import uuid

import pytest
import pytest_asyncio
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from velaris_shared.db.models import (
    CaseInstanceModel, CaseTypeModel, SyncDestinationModel, TenantModel,
    UserDirectoryModel, WorkQueueModel,
)

from case_service.main import app
from case_service.tenancy import identity as tenancy
from tests.conftest import LIFECYCLE_PROCESS_ID, TestSessionFactory  # type: ignore[attr-defined]
from tests.conftest import _headers  # type: ignore[attr-defined]

VIEWER = _headers("vic", ["viewer"])
DEV_ADMIN = _headers("devadmin", ["admin"])
DEV_MANAGER = _headers("devmgr", ["manager"])
OPERATOR = _headers("op", ["admin"])            # no directory row: the default tenant


# ── shape 1: the static guard ────────────────────────────────────────────────

#: Dependencies that are a gate in themselves.
_GATE_DEPS = {"platform_operator_only", "_platform_operator", "_require_superadmin"}
#: An in-body or helper call that authorizes.
_BODY_GATE = re.compile(
    r"require_user\(|require_case\(|hxguard\.require\(|\bawait require\(|user_allows\(|"
    r"subject_allows\(|is_superadmin\(|is_platform_operator\(|require_role|require_admin|"
    r"_require_superadmin")
_PLUMBING = {"get_current_user", "get_current_user_optional", "get_session",
             "tenant_path_guard", "get_analytics_session", "get_auth_session",
             "get_replica_session"}


def _src(fn) -> str:
    try:
        return inspect.getsource(fn)
    except (OSError, TypeError):
        return ""


def _deep(fn, depth: int = 3, seen: set | None = None) -> str:
    """The handler's source plus every same-package helper it calls, 3 deep —
    a gate in `_load_or_404(session, user, id)` is still a gate."""
    seen = seen if seen is not None else set()
    if fn in seen or depth < 0:
        return ""
    seen.add(fn)
    text = _src(fn)
    mod = sys.modules.get(getattr(fn, "__module__", ""), None)
    if mod is None:
        return text
    for name in set(re.findall(r"\b([A-Za-z_]\w*)\(", text)):
        g = getattr(mod, name, None)
        if inspect.isfunction(g) and g.__module__.startswith(
                ("case_service", "analytics_service", "hxgraph_service",
                 "hxmigrate_service", "commerce_service", "portal_service",
                 "user_service", "velaris_shared")):
            text += _deep(g, depth - 1, seen)
    return text


def _calls(dep, acc):
    if dep.call is not None:
        acc.append(dep.call)
    for d in dep.dependencies:
        _calls(d, acc)
    return acc


def login_only_routes() -> set[tuple[str, str]]:
    from analytics_service.main import app as analytics_app
    from hxgraph_service.main import app as hxgraph_app
    from hxmigrate_service.main import app as hxmigrate_app
    # The R5e splits (steps 7, 8, 10) — routes that left case-service stay swept.
    # `app` is the test gateway; its `.routes` are case-service's alone.
    from commerce_service.main import app as commerce_app
    from portal_service.main import app as portal_app
    from user_service.main import app as user_app

    out: set[tuple[str, str]] = set()
    for a in (app, analytics_app, hxgraph_app, hxmigrate_app, commerce_app, portal_app, user_app):
        for r in a.routes:
            if not isinstance(r, APIRoute):
                continue
            calls = _calls(r.dependant, [])
            names = {getattr(c, "__name__", "") for c in calls}
            if not names & {"get_current_user", "get_current_user_optional"}:
                continue                      # test_ungated_routes owns these
            if names & _GATE_DEPS or any(getattr(c, "hxguard_action", None) for c in calls):
                continue
            if any(getattr(c, "__qualname__", "").startswith(("guard.", "require_role", "require_admin"))
                   for c in calls):
                continue
            helpers = "".join(_src(c) for c in calls
                              if getattr(c, "__name__", "") not in _PLUMBING)
            if _BODY_GATE.search(_deep(r.endpoint)) or _BODY_GATE.search(helpers):
                continue
            out |= {(m, r.path) for m in r.methods}
    return out


def _paths(prefix: str, *methods_paths: str) -> set[tuple[str, str]]:
    out = set()
    for mp in methods_paths:
        m, p = mp.split(" ", 1)
        out.add((m, prefix + p))
    return out


V = "/api/v1"

#: Still login-only ON PURPOSE. Each group says why a login is the right gate.
ACCEPTED_LOGIN_ONLY: set[tuple[str, str]] = set()

# The caller's OWN things: their password, MFA, devices, profile, work,
# notification devices, MCP tokens and conversations with HxNexus. Every one is
# filtered to `user.user_id` in the handler.
ACCEPTED_LOGIN_ONLY |= _paths(V,
    "POST /auth/switch-context",   # GET /auth/me: reads the operator flag since 2026-11-04
    "POST /auth/real/change-password", "GET /auth/real/devices",
    "DELETE /auth/real/devices/{device_id}", "GET /auth/real/me/profile",
    "PATCH /auth/real/me/profile", "POST /auth/real/mfa/disable",
    "POST /auth/real/mfa/enrol", "POST /auth/real/mfa/verify-enrol",
    "GET /auth/real/webauthn/credentials",
    "DELETE /auth/real/webauthn/credentials/{credential_id}",
    "POST /auth/real/webauthn/register/options", "POST /auth/real/webauthn/register/verify",
    "POST /auth/real/webauthn/stepup/options", "POST /auth/saml/{tenant}/logout",
    "GET /my/assignments", "GET /my/workload",
    "GET /push/devices", "DELETE /push/devices/{device_id}",
    "GET /push/preferences", "PUT /push/preferences/{event_type}", "GET /push/vapid-public-key",
    "GET /mcp/tokens", "POST /mcp/tokens/{grant_id}/revoke",
    "GET /hxnexus/conversations", "DELETE /hxnexus/conversations/{conversation_id}",
    "GET /hxnexus/conversations/{conversation_id}/messages",
    "POST /hxnexus/conversations/{conversation_id}/summarize",
    "GET /hxnexus/conversations/{conversation_id}/transcript",
    "POST /cases/{case_id}/steps/{step_id}/unlock",
    "GET /admin/permissions",              # the caller's own permission map
)

# Case work. Creating a case has never needed more than a login on this
# platform (the case type's tenant is checked), and changing that is a product
# decision, not a fix. Assignment actions act on the caller's own work item and
# a form submission on the assignment it names.
ACCEPTED_LOGIN_ONLY |= _paths(V,
    "POST /cases", "POST /cases/bulk",
    "GET /queues/{queue_id}", "GET /queues/{queue_id}/items",
    "GET /queues/{queue_id}/items/count", "GET /queues/{queue_id}/stats",
    "POST /mcp/proposals/{proposal_id}/confirm",   # executes AS the confirmer, case re-checked
    "POST /mcp/proposals/{proposal_id}/reject", "GET /mcp/proposals",
)

# Reads of design metadata every worker's screen renders from — the form, the
# rules it evaluates, the escalation tree, the case type. Writes are gated.
ACCEPTED_LOGIN_ONLY |= _paths(V,
    "GET /forms", "GET /forms/{form_id}", "GET /rules", "GET /rules/lint",
    # 2026-10-29: the caller's own tenant's approved embed sites — every form
    # renderer checks an embed against them; changing them is admin-only.
    "GET /forms/embed-settings",
    # (8e 2026-10-01: the by-id rule reads gained a tenant check, so the scan
    # now counts GET /rules/{rule_id}, its /dmn, /evaluate and evaluate/batch
    # as gated; they left this list.)
    "POST /rules/validate-data",
    "GET /escalation-trees", "GET /case-types/{case_type_id}",
    "GET /case-types/{case_type_id}/migration-history",
    "GET /case-types/{case_type_id}/sla/distribution",
    "GET /variables/case-types/{case_type_id}", "GET /variables/namespaces",
    "POST /global/sovereignty-rules/resolve", "GET /hxbridge/connector-types",
    "GET /hxmigrate/auth-options",
)

# Tenant-scoped lists (8d, `scope_to_caller`) of records a case worker handles.
ACCEPTED_LOGIN_ONLY |= _paths(V,
    "GET /comms/connectors", "GET /comms/slack/notifications", "GET /comms/sms/messages",
    "GET /crm/records", "GET /docintel/connectors", "GET /docintel/extractions",
    "GET /docintel/storage", "GET /esign/requests", "GET /identity/verifications",
)

# Collaboration spaces and assistants every Studio user has (nav roles `[]`):
# HxDocs, HxCanvas, HxWork boards, HxNexus drafting and translation, the
# knowledge base and sitemap. Their rows carry no tenant yet — that is 8e.
ACCEPTED_LOGIN_ONLY |= _paths(V,
    "DELETE /hxdocs/articles/{article_id}", "GET /hxdocs/articles/{article_id}",
    "PATCH /hxdocs/articles/{article_id}", "POST /hxdocs/articles/{article_id}/publish",
    "POST /hxdocs/articles/{article_id}/regenerate-lifecycle",
    "GET /hxdocs/articles/{article_id}/versions", "POST /hxdocs/generate",
    "POST /hxdocs/generate-lifecycle", "POST /hxdocs/resolve-block", "GET /hxdocs/search",
    "GET /hxdocs/spaces", "POST /hxdocs/spaces", "GET /hxdocs/spaces/{space_id}",
    "GET /hxdocs/spaces/{space_id}/articles", "POST /hxdocs/spaces/{space_id}/articles",
    "GET /hxcanvas/boards", "POST /hxcanvas/boards", "DELETE /hxcanvas/boards/{board_id}",
    "GET /hxcanvas/boards/{board_id}", "PATCH /hxcanvas/boards/{board_id}",
    "POST /hxcanvas/boards/{board_id}/export/bpmn", "POST /hxcanvas/boards/{board_id}/items",
    "POST /hxcanvas/boards/{board_id}/items/bulk",
    "DELETE /hxcanvas/boards/{board_id}/items/{item_id}",
    "PATCH /hxcanvas/boards/{board_id}/items/{item_id}", "GET /hxcanvas/graph-nodes/search",
    "GET /hxwork/boards", "POST /hxwork/boards", "DELETE /hxwork/boards/{board_id}",
    "GET /hxwork/boards/{board_id}", "PATCH /hxwork/boards/{board_id}",
    "GET /hxwork/boards/{board_id}/analytics", "POST /hxwork/boards/{board_id}/ask",
    "GET /hxwork/boards/{board_id}/cards", "POST /hxwork/boards/{board_id}/generate-stories",
    "GET /hxwork/boards/{board_id}/relations", "POST /hxwork/boards/{board_id}/relations",
    "GET /hxwork/boards/{board_id}/sprints", "POST /hxwork/boards/{board_id}/sprints",
    "GET /hxwork/boards/{board_id}/stories", "POST /hxwork/boards/{board_id}/stories",
    "DELETE /hxwork/boards/{board_id}/stories/{story_id}",
    "PATCH /hxwork/boards/{board_id}/stories/{story_id}",
    "GET /hxwork/boards/{board_id}/story-relations",
    "POST /hxwork/boards/{board_id}/story-relations",
    "POST /hxwork/sprints/{sprint_id}/cards", "POST /hxwork/sprints/{sprint_id}/complete",
    "POST /hxwork/sprints/{sprint_id}/start", "DELETE /hxwork/story-relations/{relation_id}",
    "GET /hxnexus/docs/business", "GET /hxnexus/docs/developer", "POST /hxnexus/draft",
    "POST /hxnexus/draft/stage", "POST /hxnexus/polyglot/analyze",
    "POST /hxnexus/polyglot/compare", "GET /hxnexus/polyglot/concepts",
    "POST /hxnexus/polyglot/translate", "GET /hxnexus/status",
    "GET /knowledge/case-types", "GET /knowledge/glossary", "GET /knowledge/modules",
    "GET /knowledge/overview", "GET /sitemap/ai-status", "GET /sitemap/categories",
    "GET /sitemap/modules", "GET /sitemap/phases", "GET /sitemap/search",
    "GET /stt/status", "GET /voicefill/status", "GET /meet/providers",
)

# HxGraph reads (design knowledge, no case data) and analytics over the
# ORM-scoped analytics session (8d: fails closed). Rebuilding the graph is gated.
ACCEPTED_LOGIN_ONLY |= _paths(V,
    "GET /graph/explain", "GET /graph/export", "GET /graph/nodes", "GET /graph/nodes/{node_id}",
    "GET /graph/nodes/{node_id}/impact", "GET /graph/path", "POST /graph/query",
    "GET /graph/report", "GET /graph/similar", "GET /graph/visualize",
    "GET /analytics/metrics/funnel/{case_type_id}", "GET /analytics/metrics/sla-performance",
    "GET /analytics/metrics/snapshot", "GET /analytics/metrics/time-series",
    "GET /analytics/odata", "POST /analytics/query",
    "POST /analytics/reports",
    # 2026-11-04: list / get / run / export / delete of saved reports are no
    # longer login-only — a report is read in its own tenant, by its creator or
    # by everyone when public; deleted by its creator (analytics._rpt_visible).
)


def test_the_login_only_set_does_not_grow():
    found = login_only_routes()
    new = sorted(found - ACCEPTED_LOGIN_ONLY)
    assert not new, (
        "Route(s) gated by a login and nothing else. Give each a privilege "
        "(hxguard.guard), or — if a login really is the right gate — add it to "
        f"ACCEPTED_LOGIN_ONLY under the group whose reason it shares:\n{new}")


def test_the_accepted_list_does_not_rot():
    """A route that gained a gate leaves the list, so the list stays an
    inventory of what is still open rather than of what once was."""
    stale = sorted(ACCEPTED_LOGIN_ONLY - login_only_routes())
    assert not stale, f"now gated (or gone) — remove from ACCEPTED_LOGIN_ONLY: {stale}"


# ── the fixtures for shapes 1–3 ──────────────────────────────────────────────

@pytest_asyncio.fixture(autouse=True)
async def _case_creators(setup_db):
    """Creating a case needs an access group granting it (2026-10-18)."""
    from tests.conftest import grant_case_create
    await grant_case_create('op')


@pytest_asyncio.fixture
async def api():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def world():
    tenancy.clear_cache()
    async with TestSessionFactory() as db:
        d = TenantModel(id=uuid.uuid4(), slug="default", name="Default")
        v = TenantModel(id=uuid.uuid4(), slug="dev", name="Dev")
        db.add_all([d, v])
        await db.flush()
        for uid, t in (("devadmin", "dev"), ("devmgr", "dev"), ("vic", "dev")):
            db.add(UserDirectoryModel(id=uuid.uuid4(), user_id=uid, tenant_id=t))
        shared = CaseTypeModel(id=uuid.uuid4(), name="Shared", version="1",
                               lifecycle_process_id=LIFECYCLE_PROCESS_ID,
                               definition_json={}, tenant_id=None)
        db.add(shared)
        await db.flush()
        for tenant in (d.id, v.id, None):
            db.add(CaseInstanceModel(id=uuid.uuid4(), case_type_id=shared.id,
                                     case_type_version="1", tenant_id=tenant,
                                     status="new", data={}, created_by="seed"))
        await db.commit()
    yield {"default": d, "dev": v, "shared": shared}
    tenancy.clear_cache()


# ── shape 1: a viewer is refused ─────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,body", [
    ("POST", "/api/v1/global/regions", {"name": "x", "provider": "local"}),
    ("GET", "/api/v1/global/regions", None),
    ("GET", "/api/v1/hxlogs/entries", None),
    ("GET", "/api/v1/sync/destinations", None),
    ("POST", "/api/v1/sync/destinations", {"name": "exfil", "dest_type": "duckdb"}),
    ("GET", "/api/v1/hxbridge/connectors", None),
    ("GET", "/api/v1/devconn/connectors/outbound", None),
    ("GET", "/api/v1/branches", None),
    ("GET", "/api/v1/compliance/reports", None),
    ("POST", "/api/v1/queues", {"name": "vq"}),
    ("POST", "/api/v1/email/send", {"to_addresses": ["x@example.com"], "subject": "s"}),
    ("GET", "/api/v1/realtime/stats", None),
])
async def test_a_viewer_is_refused(api, world, method, path, body):
    r = await api.request(method, path, headers=VIEWER, json=body)
    assert r.status_code == 403, (path, r.status_code, r.text[:200])


@pytest.mark.asyncio
async def test_a_viewer_cannot_delete_a_case_type(api, world):
    """The route took no identity at all: any login hard-deleted one."""
    ct = world["shared"].id
    r = await api.delete(f"/api/v1/case-types/{ct}", headers=VIEWER)
    assert r.status_code == 403
    async with TestSessionFactory() as db:
        assert (await db.get(CaseTypeModel, ct)).is_deleted in (False, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/v1/importer/jobs", "/api/v1/hxmigrate/runs"])
async def test_a_viewer_cannot_read_migrations(path):
    from tests.phase_authz.test_tenant_isolation_probe import _other_apps
    hxmigrate_app = _other_apps()[2]       # only the test session, no leftover fake user
    async with AsyncClient(transport=ASGITransport(app=hxmigrate_app), base_url="http://test") as c:
        r = await c.get(path, headers=VIEWER)
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_a_viewer_cannot_rebuild_the_graph():
    from tests.phase_authz.test_tenant_isolation_probe import _other_apps
    hxgraph_app = _other_apps()[1]
    async with AsyncClient(transport=ASGITransport(app=hxgraph_app), base_url="http://test") as c:
        r = await c.post("/api/v1/graph/sync", headers=VIEWER)
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_a_tenant_admin_cannot_manage_regions(api, world):
    """Regions belong to the install; the privilege alone is not enough."""
    r = await api.post("/api/v1/global/regions", headers=DEV_ADMIN,
                       json={"name": "r", "provider": "local"})
    assert r.status_code == 403
    r = await api.post("/api/v1/global/regions", headers=OPERATOR,
                       json={"name": "r", "provider": "local"})
    assert r.status_code == 201, r.text


@pytest.mark.asyncio
async def test_a_queue_lands_in_its_makers_tenant(api, world):
    r = await api.post("/api/v1/queues", headers=DEV_MANAGER, json={"name": "floor"})
    assert r.status_code == 201, r.text
    async with TestSessionFactory() as db:
        q = await db.get(WorkQueueModel, uuid.UUID(r.json()["id"]))
        assert q.tenant_id == world["dev"].id
    r = await api.post("/api/v1/queues", headers=DEV_MANAGER,
                       json={"name": "planted", "tenant_id": str(world["default"].id)})
    assert r.status_code in (403, 404)       # the app-wide path guard answers first


# ── shape 2: the tenant in the body or URL is the caller's ──────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("path,body", [
    ("/api/v1/auth/real/sso/providers",
     {"tenant_id": "default", "provider": "google", "client_id": "c", "client_secret": "s"}),
    ("/api/v1/scim/tokens", {"name": "t", "tenant_id": "default"}),
    ("/api/v1/user-directory", {"user_id": "planted", "tenant_id": "default", "roles": ["admin"]}),
    ("/api/v1/channels/endpoints",
     {"tenant_id": "default", "channel": "sms", "provider": "twilio",
      "address": "+15550001", "signing_secret": "s"}),
])
async def test_a_tenant_admin_cannot_write_into_another_tenant(api, world, path, body):
    """8d's app-wide field rule refuses a body naming another tenant (404)
    before the handler runs; `tenant_for_write` refuses it again inside (403)
    for any caller that reaches the handler another way."""
    r = await api.post(path, headers=DEV_ADMIN, json=body)
    assert r.status_code in (403, 404), (path, r.status_code, r.text[:200])


@pytest.mark.asyncio
async def test_a_tenant_admins_scim_token_is_their_own(api, world):
    r = await api.post("/api/v1/scim/tokens", headers=DEV_ADMIN, json={"name": "mine"})
    assert r.status_code == 201, r.text
    from velaris_shared.db.models import ScimTokenModel
    async with TestSessionFactory() as db:
        row = (await db.execute(select(ScimTokenModel).where(ScimTokenModel.name == "mine"))).scalar_one()
        assert row.tenant_id == "dev"


@pytest.mark.asyncio
async def test_the_operator_keeps_writing_as_before(api, world):
    """Nothing the install's operator does today changes: a NULL stays NULL."""
    r = await api.post("/api/v1/scim/tokens", headers=OPERATOR, json={"name": "platform"})
    assert r.status_code == 201, r.text
    from velaris_shared.db.models import ScimTokenModel
    async with TestSessionFactory() as db:
        row = (await db.execute(select(ScimTokenModel).where(ScimTokenModel.name == "platform"))).scalar_one()
        assert row.tenant_id is None


# ── shape 3: tenantless writes ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_sync_run_exports_only_its_owners_cases(world):
    from case_service.hxsync.pipeline import _scope_cases_to_destination

    base = select(CaseInstanceModel.tenant_id)
    async with TestSessionFactory() as db:
        mine = SyncDestinationModel(name="d", dest_type="duckdb", tenant_id="dev")
        legacy = SyncDestinationModel(name="l", dest_type="duckdb", tenant_id=None)
        got = set((await db.execute(await _scope_cases_to_destination(base, mine, db))).scalars())
        assert got == {world["dev"].id, None}          # its own + platform-shared
        got = set((await db.execute(await _scope_cases_to_destination(base, legacy, db))).scalars())
        assert got == {world["dev"].id, world["default"].id, None}   # as every release did


@pytest.mark.asyncio
async def test_a_destination_records_its_owner(api, world):
    r = await api.post("/api/v1/sync/destinations", headers=DEV_ADMIN,
                       json={"name": "dw", "dest_type": "duckdb"})
    assert r.status_code == 201, r.text
    assert r.json()["tenant_id"] == "dev"
    # The operator's stays platform-wide, as every release made it.
    r = await api.post("/api/v1/sync/destinations", headers=OPERATOR,
                       json={"name": "op", "dest_type": "duckdb"})
    assert r.status_code == 201, r.text
    assert r.json()["tenant_id"] is None


@pytest.mark.asyncio
async def test_a_no_claim_callers_case_belongs_to_the_default_tenant(api, world):
    r = await api.post("/api/v1/cases", headers=OPERATOR,
                       json={"case_type_id": str(world["shared"].id), "data": {}})
    assert r.status_code == 201, r.text
    async with TestSessionFactory() as db:
        case = await db.get(CaseInstanceModel, uuid.UUID(r.json()["id"]))
        assert case.tenant_id == world["default"].id


@pytest.mark.asyncio
async def test_tenant_for_write_refuses_inside_the_handler_too(world):
    """The second line, for a caller that reaches a handler past the path guard."""
    from velaris_shared.auth.models import AuthenticatedUser
    from velaris_shared.tenancy.identity import tenant_for_write

    dev = AuthenticatedUser(user_id="devadmin", username="devadmin", roles=["admin"], tenant_id="dev")
    op = AuthenticatedUser(user_id="op", username="op", roles=["admin"], tenant_id=None)
    async with TestSessionFactory() as db:
        with pytest.raises(PermissionError):
            await tenant_for_write(db, dev, str(world["default"].id))
        # The literal "default" is what a UI sends when it has no opinion, and
        # `caller_tenant_or_refuse` has always read it as "mine".
        assert await tenant_for_write(db, dev, "default") == "dev"
        assert await tenant_for_write(db, dev, None) == "dev"
        assert await tenant_for_write(db, op, None) is None       # as before
        assert await tenant_for_write(db, op, "dev") == "dev"     # the operator's choice


@pytest.mark.asyncio
async def test_a_tenant_admin_cannot_read_regions(api, world):
    """Found LIVE after the first fix: the privilege alone let a `dev` admin
    read every region's `connection_config`."""
    for path in ("/api/v1/global/regions", "/api/v1/global/health"):
        r = await api.get(path, headers=DEV_ADMIN)
        assert r.status_code == 403, (path, r.status_code)
    r = await api.get("/api/v1/global/regions", headers=OPERATOR)
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_the_saml_tester_answers_only_for_the_callers_tenant(api, world):
    """Found LIVE after the first fix: `tenant_for_write` reads the literal
    "default" as "mine", so a `dev` admin still reached `default`'s IdP."""
    for tenant in ("default", str(world["default"].id)):
        r = await api.post(f"/api/v1/auth/saml/{tenant}/test", headers=DEV_ADMIN,
                           json={"saml_response": "x"})
        assert r.status_code == 404, (tenant, r.status_code, r.text[:120])
