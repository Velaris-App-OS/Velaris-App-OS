"""No route reaches another tenant's rows — 8d, 2026-09-29.

Seeds ONE row in every table, owned by tenant `acme` wherever the table has a
tenant column and parented on acme's rows wherever it has a foreign key, then
calls every route of all four services as tenant `evil`'s admin:

* every id-keyed route with acme's ids in the path — none may answer 2xx (a
  DELETE counts only if acme's row is really gone) or 5xx (a 5xx means the
  handler got past any tenant check and worked on acme's row);
* every parameterless GET — no response may carry the id of a row acme owns.

The first run found ~110 id routes and ~45 lists that did; the platform's HxDB
Manager read every table raw; analytics counted every tenant's cases. What is
still allowed below is allowed for a stated reason, and a table that has NO
tenant column cannot be protected by a check at all — those are listed in
`UNTENANTED` and are a data-model decision, not a missed check.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import datetime as dt
import json
import re
import uuid

import pytest_asyncio
import sqlalchemy as sa
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

from velaris_shared.db.models import Base

from case_service.main import app
from case_service.tenancy import identity as tenancy
from tests.conftest import TestSessionFactory  # type: ignore[attr-defined]
from velaris_shared.auth.jwt_handler import create_dev_token
from velaris_shared.config import get_settings

# (was imported from test_payments_authz.py, which moved to commerce-service)
def _headers(user_id: str, roles: list[str]) -> dict[str, str]:
    s = get_settings()
    token = create_dev_token(user_id=user_id, username=user_id, roles=roles,
                             secret=s.auth_secret,
                             private_key=s.auth_rsa_private_key or "")
    return {"Authorization": f"Bearer {token}"}


ACME = uuid.UUID("00000000-0000-0000-0000-00000000acbe")
EVIL = uuid.UUID("00000000-0000-0000-0000-0000000000e1")
import os
import tempfile

OUT = os.path.join(tempfile.gettempdir(), "velaris_probe8d.json")

#: (method, route) that may answer another tenant, and why.
ALLOWED = {
    # Signed public receivers: the connector's own secret is the credential.
    ("POST", "/api/v1/webhooks/{connector_id}/receive"),
    ("POST", "/api/v1/devconn/webhooks/receive/{connector_id}"),
    # Releases a step lock only where the CALLER holds it.
    ("POST", "/api/v1/cases/{case_id}/steps/{step_id}/unlock"),
    # Filtered to the caller's tenant in the query: the answer is empty.
    ("GET", "/api/v1/costing/cases/{case_id}/cost"),
    ("GET", "/api/v1/channels/cases/{case_id}/conversations"),
    ("GET", "/api/v1/compliance/consent/{subject_ref}"),
    ("GET", "/api/v1/arbiter/parity/{namespace}"),
    # The caller's OWN preference; the id is an event type, not a row.
    ("PUT", "/api/v1/push/preferences/{event_type}"),
}

#: Tables with no tenant column: a check cannot know whose a row is. Recorded
#: so the decision is visible (Oct_TODO 8e), not so it is forgotten.
UNTENANTED: tuple[str, ...] = ()   # 8e (2026-10-01): branches now have an owner


def _value(col, table, seeded):
    t = type(col.type).__name__
    name = col.name
    if name == "tenant_id":
        return ACME if t == "GUID" else "acme"
    if col.foreign_keys:
        fk = next(iter(col.foreign_keys))
        parent = seeded.get(fk.column.table.name)
        if parent is not None and fk.column.name in parent:
            return parent[fk.column.name]
    if t == "GUID":
        return uuid.uuid4()
    if t in ("Integer", "BigInteger", "SmallInteger", "Numeric", "Float"):
        return 1
    if t == "Boolean":
        return False
    if t in ("DateTime", "MicroDateTime", "Date"):
        return dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    if t in ("PortableJSON", "JSON", "JSONB"):
        return {}
    if t == "LargeBinary":
        return b"x"
    length = getattr(col.type, "length", None) or 32
    return f"acme-{name}"[:length]


async def _seed():
    seeded: dict[str, dict] = {}
    async with TestSessionFactory() as db:
        await db.execute(sa.text("DELETE FROM tenants WHERE slug IN ('acme','evil')"))
        from velaris_shared.db.models import TenantModel, UserDirectoryModel
        db.add_all([TenantModel(id=ACME, slug="acme", name="Acme"),
                    TenantModel(id=EVIL, slug="evil", name="Evil"),
                    UserDirectoryModel(id=uuid.uuid4(), user_id="mallory", tenant_id="evil")])
        await db.commit()
        for table in Base.metadata.sorted_tables:
            if table.name in ("tenants",):
                continue
            row = {}
            for col in table.columns:
                needed = (col.primary_key or not col.nullable or col.name == "tenant_id"
                          or col.foreign_keys)
                if needed and not (col.default is not None and not col.primary_key
                                   and col.name != "tenant_id" and not col.foreign_keys):
                    row[col.name] = _value(col, table, seeded)
            try:
                async with db.begin_nested():
                    await db.execute(table.insert().values(**row))
                seeded[table.name] = row
            except Exception as exc:                            # noqa: BLE001
                seeded.setdefault("__failed__", {})[table.name] = str(exc)[:120]
        await db.commit()
    return seeded


def _owned(seeded):
    """Tables whose seeded row is acme's: tenanted, or parented on one that is."""
    owned = set()
    for table in Base.metadata.sorted_tables:
        if table.name not in seeded:
            continue
        if "tenant_id" in table.columns:
            owned.add(table.name)
        elif any(fk.column.table.name in owned
                 for c in table.columns for fk in c.foreign_keys):
            owned.add(table.name)
    return owned


ALIASES = {
    "case_id": "case_instances", "connector_id": "connector_registry",
    "dest_id": "sync_destinations", "env_id": "environment_registry",
    "webhook_id": "webhook_subscriptions", "region_id": "tenant_region_assignments",
    "transfer_id": "case_tenant_transfers", "release_id": "marketplace_release_requests",
    "bot_id": "bot_definitions", "msg_id": "email_messages", "tpl_id": "email_templates",
    "defn_id": "process_definitions", "device_id": "push_device_tokens",
    "payment_id": "payment_requests", "story_id": "hxwork_stories",
    "promo_id": "storefront_promotions", "category_id": "storefront_categories",
    "backup_id": "backup_records", "analysis_id": "hxdbmigrate_analyses",
    "entry_id": "case_time_entries", "form_id": "forms", "calendar_id": "calendar_events",
    "policy_id": ("case_sla_instances", "sla_policy_id"),
    "instance_id": "process_instances", "group_id": "access_groups",
    "user_id": ("tenant_memberships", "user_id"), "operator_id": ("operator_access_groups", "operator_id"),
    "name": ("case_statuses", "name"), "rule_key": ("case_risk_rules", "rule_key"),
    "capability": ("agent_capabilities", "capability"),
}


def _candidates(param, seeded, owned, path=""):
    if param in ("tenant_id", "tenant"):
        return [("tenants", str(ACME) if param == "tenant_id" else "acme")]
    if param == "slug":
        if "storefront" in path and "storefronts" in seeded:
            return [("storefronts", seeded["storefronts"].get("slug"))]
        return [("tenants", "acme")]
    if param in ALIASES:
        a = ALIASES[param]
        tname, col = (a, None) if isinstance(a, str) else a
        if tname in seeded:
            if col is None:
                col = [c.name for c in Base.metadata.tables[tname].primary_key.columns][0]
            return [(tname, str(seeded[tname].get(col)))]
        return []
    stem = re.sub(r"_(id|uuid|key|slug|name)$", "", param)
    if param in ("id",):
        return []
    segs = [x for x in path.split("/") if x and not x.startswith("{")][2:]
    exact = sorted(t for t in owned if t == stem + "s" or t.endswith("_" + stem + "s"))
    if exact:
        # EVERY table the name could mean, in a fixed order — a single pick let
        # set ordering decide what was probed (and let a real leak pass).
        out = []
        for t in exact:
            pk = [c.name for c in Base.metadata.tables[t].primary_key.columns]
            if len(pk) == 1 and seeded[t].get(pk[0]) is not None:
                out.append((t, str(seeded[t][pk[0]])))
        if out:
            return out
    out = []
    for tname in sorted(owned):
        toks = tname.split("_")
        if (tname.endswith(stem + "s") or tname.endswith(stem + "es") or tname == stem
                or toks[-1] in (stem, stem + "s") or tname.endswith(stem.replace("_", "") + "s")
                or tname.endswith(stem.rsplit("_", 1)[-1] + "s")):
            pk = [c.name for c in Base.metadata.tables[tname].primary_key.columns]
            if len(pk) == 1:
                col = pk[0] if not param.endswith(("_slug", "_name", "_key")) else None
                val = seeded[tname].get(col) if col else None
                if param.endswith(("_slug", "slug")) and "slug" in seeded[tname]:
                    val = seeded[tname]["slug"]
                if val is not None:
                    out.append((tname, str(val)))
    return out


@pytest_asyncio.fixture
async def api():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


#: (method, route) in the other services that may answer evil's admin, and why.
#: Both were in case-service until the commerce split (R5e step 7), where the
#: main probe's rules already let them pass; this sweep is stricter.
OTHER_APPS_ALLOWED = {
    # Install-level, no tenant column: the install's package sources. Adding one
    # is operator-only (8e); any admin reads the list (tokens are never shown).
    ("GET", "/api/v1/marketplace/sources"),
    # The probe offers every table whose name fits `access_group_id`, including
    # `operator_access_groups` — whose id is not an access group, so resetting a
    # rule for it is a no-op 200. With another tenant's REAL access group the
    # path guard answers 404 — pinned in commerce-service's
    # test_marketplace_access_rules_tenant.py.
    ("DELETE", "/api/v1/marketplace/access-rules/{access_group_id}"),
    # user-service (R5e step 10). The login page's list of SSO buttons is
    # anonymous by design (also allowed in test_probe_lists). The two deletes
    # answer 204 to ids the probe takes from OTHER tables (checkout / push
    # tokens, sync field mappings) — a no-op; acme's real SCIM token and group
    # mapping are answered 404 by the path guard and left untouched. Both routes
    # are in the main probe too (USER_ROUTES), which checks the row itself.
    ("GET", "/api/v1/auth/real/sso/providers"),
    ("DELETE", "/api/v1/scim/tokens/{token_id}"),
    ("DELETE", "/api/v1/identity/provisioning/group-mappings/{mapping_id}"),
}


def _other_apps():
    from analytics_service.main import app as a2
    from hxgraph_service.main import app as a3
    from hxmigrate_service.main import app as a4
    from commerce_service.main import app as a5   # R5e step 7
    from portal_service.main import app as a6     # R5e step 8
    from user_service.main import app as a7       # R5e step 10
    from velaris_shared.db import session as sess
    from tests.conftest import override_get_session  # type: ignore[attr-defined]
    for a in (a2, a3, a4, a5):
        # Only the session. Another test's leftover fake-user override would
        # impersonate `evil` as whoever it faked, and the probe would test that.
        a.dependency_overrides.clear()
        a.dependency_overrides[sess.get_session] = override_get_session
        if hasattr(sess, "get_analytics_session"):
            a.dependency_overrides[sess.get_analytics_session] = override_get_session
    # portal-service and user-service share case-service's override dict
    # (conftest's gateway): clearing it would clear case-service's too, and its
    # session is already set.
    return [a2, a3, a4, a5, a6, a7]


async def test_probe_other_apps():
    tenancy.clear_cache()
    seeded = await _seed()
    owned = _owned(seeded)
    h = _headers("mallory", ["admin"])
    out = []
    for other in _other_apps():
        async with AsyncClient(transport=ASGITransport(app=other), base_url="http://test") as c:
            for r in other.routes:
                if not isinstance(r, APIRoute):
                    continue
                # The public portal is anonymous by design (its own session
                # model) — skipped here as the main probe skips it; the
                # portal-admin routes are probed.
                if r.path.startswith("/api/v1/portal/"):
                    continue
                params = re.findall(r"\{([^}:]+)(?::path)?\}", r.path)
                if params:
                    cands = {p: _candidates(p, seeded, owned, r.path) for p in params}
                    if any(not v for v in cands.values()):
                        out.append(("UNMAPPED", r.path, [p for p, v in cands.items() if not v]))
                        continue
                    # EVERY candidate for the last id, in a fixed order. Picking
                    # one let set ordering decide which table was probed, and a
                    # real leak (hxmigrate runs, 2026-09-29) passed most runs.
                    paths = []
                    for _t, val in sorted(cands[params[-1]]):
                        path = r.path
                        for p_ in params:
                            v = val if p_ == params[-1] else sorted(cands[p_])[0][1]
                            path = re.sub(r"\{" + p_ + r"(:path)?\}", v, path)
                        paths.append(path)
                    methods = sorted(r.methods - {"HEAD", "OPTIONS"})
                else:
                    paths, methods = [r.path], (["GET"] if "GET" in r.methods else [])
                for m, path in [(m, p) for m in methods for p in paths]:
                    try:
                        resp = await c.request(m, path, headers=h, json={})
                    except Exception as exc:                        # noqa: BLE001
                        out.append(("EXC", m, r.path, str(exc)[:80]))
                        continue
                    body = resp.text
                    leak = resp.status_code < 300 and (str(ACME) in body or "acme-" in body
                           or any(str(seeded[t].get(next(iter(Base.metadata.tables[t].primary_key.columns)).name, "")) in body
                                  for t in owned if len(str(seeded[t].get(next(iter(Base.metadata.tables[t].primary_key.columns)).name, ""))) > 8))
                    if leak or (params and resp.status_code < 300 and m != "GET"):
                        out.append(("HIT", m, r.path, resp.status_code, body[:90]))
    # The migration tools (importer, scout, orchestrator) store no tenant.
    untenanted = ("/api/v1/importer/", "/api/v1/scout", "/api/v1/orchestrator/")
    # `/metrics` counts requests by path — including the ids this probe sent.
    hits = [o for o in out if o[0] == "HIT" and not o[2].startswith(untenanted)
            and o[2] != "/metrics" and (o[1], o[2]) not in OTHER_APPS_ALLOWED]
    assert hits == [], hits


async def test_probe(api):
    tenancy.clear_cache()
    seeded = await _seed()
    owned = _owned(seeded)
    h = _headers("mallory", ["admin"])
    results, unmapped = [], []
    # user-service's routes too (R5e step 10): they were in this probe while
    # case-service served them, and `api` reaches them through the test gateway.
    from user_service.main import app as user_app
    routes = [r for r in [*app.routes, *user_app.routes] if isinstance(r, APIRoute)
              and re.search(r"\{", r.path)
              and not r.path.startswith(("/api/v1/portal/", "/api/v1/broker/"))
              # /internal/* is never a tenant-facing route: the gateway refuses it
              # (live-checked 2026-11-10: 404 for GET/POST/DELETE) and each router
              # demands its own secret (internal service / demo). The test gateway
              # already refused user-service's; case-service's demo door is
              # called here directly, so it is left out explicitly.
              and not r.path.startswith("/internal/")]
    order = {"GET": 0, "POST": 1, "PUT": 1, "PATCH": 1, "DELETE": 2}
    jobs = []
    for r in routes:
        params = re.findall(r"\{([^}:]+)(?::path)?\}", r.path)
        cands = {p: _candidates(p, seeded, owned, r.path) for p in params}
        if any(not v for v in cands.values()):
            unmapped.append((sorted(r.methods), r.path, [p for p, v in cands.items() if not v]))
            continue
        last = params[-1]
        for tname, val in sorted(cands[last]):
            path = r.path
            for p in params:
                v = val if p == last else sorted(cands[p])[0][1]
                path = re.sub(r"\{" + p + r"(:path)?\}", v, path)
            for m in r.methods - {"HEAD", "OPTIONS"}:
                jobs.append((order.get(m, 1), m, r.path, path, tname))
    for _, m, tpl, path, tname in sorted(jobs):
        try:
            resp = await api.request(m, path, headers=h, json={})
            code, body = resp.status_code, resp.text[:160]
        except Exception as exc:                                  # noqa: BLE001
            code, body = 599, f"{type(exc).__name__}: {exc}"[:160]
        gone = None
        if m == "DELETE" and code < 300 and tname in seeded:
            table = Base.metadata.tables[tname]
            pk = list(table.primary_key.columns)[0]
            async with TestSessionFactory() as db:
                left = (await db.execute(sa.select(sa.func.count()).select_from(table)
                                         .where(pk == seeded[tname][pk.name]))).scalar()
            gone = left == 0
        full = resp.text if code != 599 else ""
        # Did acme's data come back? Seeded strings all start "acme-", and the
        # tenant's own id/token mark any row that reports whose it is.
        carries = ("acme-" in full or str(ACME) in full or '"acme"' in full)
        results.append({"m": m, "route": tpl, "table": tname, "code": code,
                        "body": body, "deleted_acme_row": gone, "carries_acme": carries})
    json.dump({"results": results, "unmapped": unmapped,
               "seed_failed": seeded.get("__failed__", {}),
               "owned": sorted(owned)}, open(OUT, "w"), indent=1, default=str)
    bad = []
    for x in results:
        reached = x["code"] < 300 or x["code"] >= 500
        # A READ that answered 2xx but returned nothing of acme's (the id named a
        # row this route does not serve, so it listed nothing) is not a reach.
        if x["m"] == "GET" and x["code"] < 300 and not x.get("carries_acme"):
            reached = False
        if x["m"] == "DELETE" and x["code"] < 300 and x.get("deleted_acme_row") is False:
            reached = False
        if (reached and (x["m"], x["route"]) not in ALLOWED
                and not x["route"].startswith(UNTENANTED)):
            bad.append((x["m"], x["route"], x["code"], x["body"][:80]))
    assert not seeded.get("__failed__"), seeded.get("__failed__")
    assert bad == [], bad


async def test_probe_lists(api):
    tenancy.clear_cache()
    seeded = await _seed()
    owned = _owned(seeded)
    markers = {str(ACME)}
    for t in owned:
        pk = [c.name for c in Base.metadata.tables[t].primary_key.columns]
        if len(pk) == 1 and seeded[t].get(pk[0]) is not None:
            v = str(seeded[t][pk[0]])
            if len(v) >= 8:
                markers.add(v)
    h = _headers("mallory", ["admin"])
    hits = []
    from user_service.main import app as user_app      # R5e step 10, as above
    for r in [*app.routes, *user_app.routes]:
        if not isinstance(r, APIRoute) or "{" in r.path or "GET" not in r.methods:
            continue
        if r.path.startswith(("/api/v1/portal/", "/api/v1/broker/", "/api/v1/storefront/public")):
            continue
        try:
            resp = await api.get(r.path, headers=h)
        except Exception as exc:                                   # noqa: BLE001
            continue
        if resp.status_code >= 300:
            continue
        body = resp.text
        found = [m for m in markers if m in body] or (["acme-"] if "acme-" in body else [])
        if found:
            hits.append((r.path, resp.status_code, found[:2], body[:100]))
    json.dump(hits, open(OUT.replace(".json", "_lists.json"), "w"), indent=1, default=str)
    #: Lists that may show another tenant's row id, and why.
    ok = {
        # Unauthenticated login-page list: which SSO buttons to draw.
        "/api/v1/auth/real/sso/providers",
        # Platform namespaces (`tenant_key` = platform) are everyone's by design.
        "/api/v1/variables/namespaces",
        # The operator's Prometheus scrape (per-tenant business metrics). No
        # tenant reaches it: the gateway answers 404 to any request that came
        # through a front (2026-11-04), and the service to any forwarded from
        # outside; this probe calls the app in-process, as a local scraper.
        "/metrics",
        # The caller's OWN request spans (2026-11-04: a tenant's admin sees only
        # spans stamped with its trusted tenant). The ids in them are the ones
        # this probe made the caller put in its own URLs (test_probe_paths).
        "/api/v1/observability/traces/recent",
    }
    leaks = [h_ for h_ in hits if h_[2] != ["acme-"] and h_[0] not in ok
             and not h_[0].startswith(UNTENANTED)]
    assert leaks == [], leaks


# ── ids named in the BODY or the QUERY STRING ────────────────────────────────

#: Body keys that name another tenant by design.
BODY_ALLOWED = {
    # A platform-wide policy question with a tenant as its INPUT, operator-only.
    ("POST", "/api/v1/global/sovereignty-rules/resolve", "tenant_id"),
}


async def test_no_write_route_accepts_another_tenants_id_in_its_body(api):
    """The path guard alone let a caller CREATE inside another tenant: a case
    type id, a connector id, an access group, or plainly `"tenant_id": "acme"`
    in the body. Every write route whose body model has such a field is sent
    acme's value by evil's admin, and must refuse."""
    from velaris_shared.tenancy.path_guard import CASE, FIELD_RULES, TENANT, USER

    tenancy.clear_cache()
    seeded = await _seed()
    acme_value = {
        id(TENANT): "acme",
        id(USER): None,
        id(CASE): str(seeded["case_instances"]["id"]),
    }
    h = _headers("mallory", ["admin"])
    missed, tried = [], 0
    # commerce-service's write routes too (R5e step 7), portal-service's
    # (R5e step 8 — portal-admin) and user-service's (step 10): all carry the
    # same guard.
    others = _other_apps()
    commerce, portal, user = others[3], others[4], others[5]
    commerce_client = AsyncClient(transport=ASGITransport(app=commerce), base_url="http://test")
    portal_client = AsyncClient(transport=ASGITransport(app=portal), base_url="http://test")
    user_client = AsyncClient(transport=ASGITransport(app=user), base_url="http://test")
    targets = ([(r, api) for r in app.routes] + [(r, commerce_client) for r in commerce.routes]
               + [(r, portal_client) for r in portal.routes]
               + [(r, user_client) for r in user.routes])
    for r, client in targets:
        if not isinstance(r, APIRoute) or not (r.methods & {"POST", "PUT", "PATCH"}):
            continue
        if r.path.startswith(("/api/v1/portal/", "/api/v1/broker/", "/api/v1/storefront/public")):
            continue
        fields = {}
        for bp in r.dependant.body_params:
            fields.update(getattr(bp.field_info.annotation, "model_fields", {}) or {})
        for key, spec in FIELD_RULES.items():
            if key not in fields:
                continue
            if id(spec) in acme_value:
                value = acme_value[id(spec)]
            else:
                table = spec.model.__table__.name
                if table not in seeded:
                    continue
                pk = next(iter(spec.model.__table__.primary_key.columns)).name
                value = str(seeded[table][pk])
            if value is None:
                continue
            method = sorted(r.methods & {"POST", "PUT", "PATCH"})[0]
            if (method, r.path, key) in BODY_ALLOWED:
                continue
            from velaris_shared.tenancy.path_guard import FIELD_EXEMPT
            if (r.path, key) in FIELD_EXEMPT:
                continue
            path = re.sub(r"\{[^}:]+(:path)?\}", str(uuid.uuid4()), r.path)
            is_list = "list" in str(fields[key].annotation).lower()
            resp = await client.request(method, path, headers=h,
                                        json={key: [value] if is_list else value})
            tried += 1
            if resp.status_code < 300 or resp.status_code >= 500:
                missed.append((method, r.path, key, resp.status_code))
    await commerce_client.aclose()
    await portal_client.aclose()
    await user_client.aclose()
    assert tried > 50, tried
    assert missed == [], missed


async def test_a_query_id_of_another_tenant_is_refused(api):
    tenancy.clear_cache()
    seeded = await _seed()
    h = _headers("mallory", ["admin"])
    ct = str(seeded["case_types"]["id"])
    r = await api.get("/api/v1/hxbridge/calls", params={"case_id": str(seeded["case_instances"]["id"])},
                      headers=h)
    assert r.status_code == 404, r.text
    r = await api.get("/api/v1/intake/events", params={"case_type_id": ct}, headers=h)
    assert r.status_code == 404, r.text


async def test_the_owning_tenant_still_creates_with_its_own_ids(api):
    """The guard must not refuse the tenant its own rows."""
    tenancy.clear_cache()
    seeded = await _seed()
    from velaris_shared.db.models import UserDirectoryModel
    async with TestSessionFactory() as db:
        db.add(UserDirectoryModel(id=uuid.uuid4(), user_id="ann", tenant_id="acme"))
        await db.commit()
    h = _headers("ann", ["admin"])
    r = await api.post("/api/v1/escalation-trees", headers=h,
                       json={"name": "mine", "tenant_id": "acme",
                             "case_type_id": str(seeded["case_types"]["id"])})
    assert r.status_code != 404, r.text


async def test_an_mcp_tool_cannot_name_another_tenants_row():
    """MCP tools call handler functions directly, so the app's guard never runs
    for them; the MCP server applies the same check to the tool's arguments."""
    from velaris_shared.auth.models import AuthenticatedUser
    from case_service.hxmcp import server

    tenancy.clear_cache()
    seeded = await _seed()
    mallory = AuthenticatedUser(user_id="mallory", roles=["admin"], tenant_id="evil")
    ct = str(seeded["case_types"]["id"])
    async with TestSessionFactory() as db:
        for name, args in (("case_type_get", {"case_type_id": ct}),
                           ("case_get", {"case_id": str(seeded["case_instances"]["id"])})):
            out = await server.handle_message(db, mallory, {
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": name, "arguments": args}})
            body = str(out)
            assert "acme-name" not in body, (name, body[:300])
            assert "404" in body or "Not found" in body or "not found" in body, (name, body[:300])


async def test_a_socket_cannot_listen_to_another_tenants_channels():
    """Until 2026-09-29 any signed-in socket could subscribe to any channel:
    another tenant's `cases.<id>`, another user's worklist, or `cases.*`."""
    from case_service.api.routers.realtime import _Caller, authorize_channel

    tenancy.clear_cache()
    seeded = await _seed()
    acme_case = str(seeded["case_instances"]["id"])
    mallory = _Caller("mallory", ["admin"], "evil")
    ann = _Caller("ann", ["admin"], "acme")
    assert not await authorize_channel(mallory, f"cases.{acme_case}")
    assert not await authorize_channel(mallory, f"presence.case:{acme_case}")
    assert not await authorize_channel(mallory, "cases.*")
    assert not await authorize_channel(mallory, "assignments.ann")
    assert await authorize_channel(mallory, "assignments.mallory")
    assert await authorize_channel(ann, f"cases.{acme_case}")
    # The install's operator (default tenant) keeps the Live Activity view.
    assert await authorize_channel(_Caller("admin", ["admin"], None), "cases.*")


async def test_only_the_operator_may_broadcast(api):
    tenancy.clear_cache()
    await _seed()             # mallory's directory row puts her in tenant evil
    r = await api.post("/api/v1/realtime/broadcast", params={"channel": "cases.x"},
                       json={"fake": True}, headers=_headers("mallory", ["admin"]))
    assert r.status_code == 403, r.text


async def test_analytics_scope_fails_closed_when_the_caller_cannot_be_resolved(monkeypatch):
    """If the scope's own caller lookup fails while the route's authentication
    passes, the session must NOT be left unscoped (that reads every tenant)."""
    from velaris_shared.tenancy import orm_scope

    class Req:
        headers = {"authorization": "Bearer something"}

    class S:
        info: dict = {}

    async def none(_request):
        return None
    monkeypatch.setattr(orm_scope, "staff_caller", none)
    s = S()
    await orm_scope.scope_session_to_caller(Req(), s)
    assert s.info[orm_scope.SCOPE_KEY] == {"uuids": [], "tokens": []}
