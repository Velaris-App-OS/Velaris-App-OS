"""Website demo sign-ups get a tenant of their own (2026-11-04, Nov_TODO §0 "SECURITY").

Before: every sign-up was `admin` of the DEFAULT tenant — a platform operator,
able to list every tenant's users and create or delete tenants. Now
user-service's `/internal/v1/demo/tenants` makes a tenant per sign-up, and the
account is `admin` there only.

Tested as the demo admin (should succeed in its own tenant), the same demo
admin reaching other tenants (must be refused), and the install's operator
(control). Plus the guard, limits, the cap, expiry and the extension.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from tests.conftest import TestSessionFactory, _headers, deploy_case_type
from user_service.main import app as user_app
from velaris_shared.config import get_settings
from velaris_shared.db.models import (
    AccessGroupModel, AccessRoleModel, HelixUserModel, OperatorAccessGroupModel,
    PortalModel, TenantModel, UserDirectoryModel,
)
from velaris_shared.internal.auth import DEMO_HEADER as HEADER
from velaris_shared.internal.auth import HEADER as INTERNAL_HEADER
from velaris_shared.tenancy import demo
from velaris_shared.tenancy import identity as tenancy

SECRET = "d" * 40
PATH = "/internal/v1/demo/tenants"


@pytest.fixture
def secret():
    get_settings().demo_provision_secret = SECRET
    yield SECRET


@pytest.fixture
async def raw():
    async with AsyncClient(transport=ASGITransport(app=user_app), base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
async def seeded():
    """What migration 033 seeds on a real install: the staff portal and the
    built-in `admin` access role (full access)."""
    tenancy.clear_cache()
    async with TestSessionFactory() as db:
        db.add(PortalModel(id=demo._STAFF_PORTAL, name="Staff", portal_type="staff"))
        db.add(AccessRoleModel(id=uuid.UUID(demo._ADMIN_ROLE), name="admin", privileges=[
            {"resource": "case", "case_type_id": "*", "actions": ["create", "read", "update"]}]))
        db.add(TenantModel(id=uuid.uuid4(), slug="default", name="Default"))
        await db.commit()


def _body(n: str = "ann", **kw):
    return {"request_key": f"key-{n}-0001", "username": f"{n}_demo", "email": f"{n}@vx.test",
            "password": "Pw-123456789!", "display_name": n.title(), "kind": "free", **kw}


def _token(user_id: str, username: str) -> dict[str, str]:
    from velaris_shared.auth.jwt_handler import create_dev_token
    s = get_settings()
    t = create_dev_token(user_id=user_id, username=username, roles=["admin"],
                         secret=s.auth_secret, private_key=s.auth_rsa_private_key or "")
    return {"Authorization": f"Bearer {t}"}


async def _make(raw, n="ann", **kw) -> dict:
    r = await raw.post(PATH, json=_body(n, **kw), headers={HEADER: SECRET})
    assert r.status_code == 201, r.text
    return r.json()


# ── the guard ─────────────────────────────────────────────────────────────────

async def test_unconfigured_install_refuses(raw):
    get_settings().demo_provision_secret = ""
    assert (await raw.post(PATH, json=_body(), headers={HEADER: "x"})).status_code == 503


async def test_wrong_secret_and_proxied_calls_are_refused(raw, secret):
    assert (await raw.post(PATH, json=_body(), headers={HEADER: "w" * 40})).status_code == 403
    assert (await raw.post(PATH, json=_body())).status_code == 403
    r = await raw.post(PATH, json=_body(), headers={HEADER: SECRET, "X-Forwarded-For": "1.2.3.4"})
    assert r.status_code == 404
    async with TestSessionFactory() as db:
        assert (await db.execute(select(TenantModel).where(
            TenantModel.slug.like("demo-%")))).scalars().first() is None


async def test_the_demo_secret_opens_only_the_demo_routes(raw, secret):
    """The website holds this secret and nothing else (user, 2026-11-04)."""
    from case_service.main import app as case_app
    # The internal service secret is not accepted on the demo routes…
    get_settings().internal_service_secret = "i" * 40
    r = await raw.post(PATH, json=_body(), headers={INTERNAL_HEADER: "i" * 40})
    assert r.status_code == 403
    # …and the demo secret opens no other internal route.
    async with AsyncClient(transport=ASGITransport(app=case_app), base_url="http://test") as c:
        r = await c.post("/internal/v1/user/otp-email", headers={HEADER: SECRET},
                         json={"purpose": "password_reset", "to": "a@vx.test", "code": "123456",
                               "valid_minutes": 10})
        assert r.status_code == 403


async def test_the_gateway_identity_check_lets_only_the_demo_call_through(secret):
    from starlette.requests import Request
    from velaris_shared.internal.auth import is_demo_caller

    def req(path, headers):
        return Request({"type": "http", "method": "POST", "path": path, "query_string": b"",
                        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]})
    assert is_demo_caller(req(PATH, {HEADER: SECRET}))
    assert not is_demo_caller(req("/internal/v1/user/otp-email", {HEADER: SECRET}))
    assert not is_demo_caller(req("/api/v1/auth/real/users", {HEADER: SECRET}))
    assert not is_demo_caller(req(PATH, {HEADER: "x" * 40}))
    assert not is_demo_caller(req(PATH, {HEADER: SECRET, "X-Forwarded-For": "1.2.3.4"}))
    get_settings().demo_provision_secret = ""
    assert not is_demo_caller(req(PATH, {HEADER: ""}))


# ── what one call makes ───────────────────────────────────────────────────────

async def test_a_sign_up_gets_its_own_tenant(raw, secret):
    out = await _make(raw)
    assert out["tenant_slug"].startswith("demo-")
    async with TestSessionFactory() as db:
        t = await db.get(TenantModel, uuid.UUID(out["tenant_id"]))
        assert t.status == "active" and t.max_users == 5 and t.max_cases == 200
        assert t.settings["demo"]["kind"] == "free"
        u = await db.get(HelixUserModel, uuid.UUID(out["user_id"]))
        assert u.roles == ["admin"] and not u.is_superadmin and u.password_change_required
        d = (await db.execute(select(UserDirectoryModel).where(
            UserDirectoryModel.user_id == out["user_id"]))).scalar_one()
        assert d.tenant_id == t.slug
        g = (await db.execute(select(AccessGroupModel).where(
            AccessGroupModel.tenant_id == t.slug))).scalar_one()
        assert g.role_ids == [demo._ADMIN_ROLE]
        assert (await db.execute(select(OperatorAccessGroupModel).where(
            OperatorAccessGroupModel.access_group_id == g.id))).scalar_one().operator_id == "ann_demo"


async def test_a_retry_finds_the_same_tenant_and_sets_the_new_password(raw, secret):
    first = await _make(raw)
    async with TestSessionFactory() as db:
        before = (await db.get(HelixUserModel, uuid.UUID(first["user_id"]))).password_hash
    again = await _make(raw, password="Other-98765432!")
    assert again["tenant_id"] == first["tenant_id"] and again["user_id"] == first["user_id"]
    async with TestSessionFactory() as db:
        assert (await db.get(HelixUserModel, uuid.UUID(first["user_id"]))).password_hash != before
        assert len((await db.execute(select(TenantModel).where(
            TenantModel.slug.like("demo-%")))).scalars().all()) == 1


async def test_an_existing_account_is_never_touched(raw, secret):
    async with TestSessionFactory() as db:
        owner = HelixUserModel(username="owner", email="ann@vx.test", password_hash="keep",
                               roles=["superadmin"])
        db.add(owner)
        await db.commit()
    r = await raw.post(PATH, json=_body(), headers={HEADER: SECRET})
    assert r.status_code == 409
    async with TestSessionFactory() as db:
        assert (await db.execute(select(HelixUserModel).where(
            HelixUserModel.username == "owner"))).scalar_one().password_hash == "keep"


async def test_the_cap_on_active_demo_tenants(raw, secret):
    get_settings().demo_max_tenants = 1
    await _make(raw, "ann")
    r = await raw.post(PATH, json=_body("bob"), headers={HEADER: SECRET})
    assert r.status_code == 503


# ── what the demo admin can and cannot do ─────────────────────────────────────

async def test_demo_admin_is_admin_of_its_own_tenant_only(client, raw, secret):
    ann = await _make(raw, "ann")
    bob = await _make(raw, "bob")
    me = _headers(ann["user_id"], ["admin"])

    # Succeeds in its own tenant: reads it, sees its own account.
    r = await client.get(f"/api/v1/tenants/{ann['tenant_id']}", headers=me)
    assert r.status_code == 200, r.text
    r = await client.get("/api/v1/tenants", headers=me)
    assert r.status_code == 200
    assert {t["slug"] for t in r.json()} == {ann["tenant_slug"]}
    r = await client.get("/api/v1/auth/real/users", headers=me)
    assert r.status_code == 200, r.text
    names = {u["username"] for u in r.json()["users"]}
    assert "ann_demo" in names and "bob_demo" not in names

    # Refused everywhere else: another tenant, and every platform-operator power.
    assert (await client.get(f"/api/v1/tenants/{bob['tenant_id']}", headers=me)).status_code in (403, 404)
    assert (await client.post("/api/v1/tenants", headers=me,
                              json={"slug": "mine", "name": "Mine"})).status_code == 403
    assert (await client.patch(f"/api/v1/tenants/{ann['tenant_id']}", headers=me,
                               json={"max_users": 1000})).status_code == 403
    assert (await client.delete(f"/api/v1/tenants/{bob['tenant_id']}/permanent",
                                headers=me)).status_code in (403, 404)

    # Control: the install's operator still sees every tenant.
    r = await client.get("/api/v1/tenants", headers=_headers("root", ["superadmin"]))
    assert {ann["tenant_slug"], bob["tenant_slug"]} <= {t["slug"] for t in r.json()}


async def test_max_users_is_enforced_for_the_demo_admin(client, raw, secret):
    ann = await _make(raw, "ann")
    async with TestSessionFactory() as db:
        (await db.get(TenantModel, uuid.UUID(ann["tenant_id"]))).max_users = 1
        await db.commit()
    tenancy.clear_cache()
    r = await client.post("/api/v1/auth/real/register", headers=_headers(ann["user_id"], ["admin"]),
                          json={"username": "ann_two", "email": "two@vx.test",
                                "password": "Pw-123456789!", "roles": ["staff"]})
    assert r.status_code == 403 and "VEL-TENANT-LIMIT-USERS" in r.text


async def test_max_cases_is_enforced(client, raw, secret):
    ann = await _make(raw, "ann")
    ct = await deploy_case_type(client)
    me = _token(ann["user_id"], "ann_demo")      # as sign-in mints it: the real username
    r = await client.post("/api/v1/cases", headers=me, json={"case_type_id": ct["id"], "data": {}})
    assert r.status_code == 201, r.text
    async with TestSessionFactory() as db:
        (await db.get(TenantModel, uuid.UUID(ann["tenant_id"]))).max_cases = 1
        await db.commit()
    tenancy.clear_cache()
    r = await client.post("/api/v1/cases", headers=me, json={"case_type_id": ct["id"], "data": {}})
    assert r.status_code == 403 and "VEL-TENANT-LIMIT-CASES" in r.text


async def test_a_tenant_without_limits_is_unchanged(session):
    from velaris_shared.tenancy.limits import require_case_room, require_user_room
    await require_user_room(session, "default")
    await require_case_room(session, "default")
    await require_user_room(session, None)


# ── expiry and the extension ──────────────────────────────────────────────────

async def _age(tenant_id: str, *, expires_days_ago: float, archived_days_ago: float | None = None):
    now = datetime.now(timezone.utc)
    async with TestSessionFactory() as db:
        t = await db.get(TenantModel, uuid.UUID(tenant_id))
        d = dict(t.settings["demo"])
        d["expires_at"] = (now - timedelta(days=expires_days_ago)).isoformat()
        if archived_days_ago is not None:
            d["archived_at"] = (now - timedelta(days=archived_days_ago)).isoformat()
        t.settings = {**t.settings, "demo": d}
        await db.commit()


async def _none(session, tid):
    return 0


async def test_expiry_archives_then_deletes_only_demo_tenants(raw, secret):
    ann = await _make(raw, "ann")
    keep = await _make(raw, "bob")
    async with TestSessionFactory() as db:   # an ordinary tenant whose name looks like one
        db.add(TenantModel(id=uuid.uuid4(), slug="demo-customer", name="Real customer",
                           settings={"demo": "not a mark"}))
        await db.commit()
    await _age(ann["tenant_id"], expires_days_ago=1)

    async with TestSessionFactory() as db:
        assert (await demo.expire_due(db, _none))["archived"] == 1
    async with TestSessionFactory() as db:
        assert (await db.get(TenantModel, uuid.UUID(ann["tenant_id"]))).status == "archived"
        assert (await db.get(TenantModel, uuid.UUID(keep["tenant_id"]))).status == "active"

    await _age(ann["tenant_id"], expires_days_ago=5, archived_days_ago=4)
    async with TestSessionFactory() as db:
        assert (await demo.expire_due(db, _none))["deleted"] == 1
    async with TestSessionFactory() as db:
        assert await db.get(TenantModel, uuid.UUID(ann["tenant_id"])) is None
        assert await db.get(HelixUserModel, uuid.UUID(ann["user_id"])) is None
        assert (await db.execute(select(TenantModel).where(
            TenantModel.slug == "demo-customer"))).scalar_one() is not None
        assert await db.get(TenantModel, uuid.UUID(keep["tenant_id"])) is not None


async def test_a_legal_hold_keeps_an_expired_demo_tenant(raw, secret):
    ann = await _make(raw, "ann")
    await _age(ann["tenant_id"], expires_days_ago=5, archived_days_ago=4)
    async with TestSessionFactory() as db:
        t = await db.get(TenantModel, uuid.UUID(ann["tenant_id"]))
        t.status = "archived"
        await db.commit()

    async def held(session, tid):
        return 1
    async with TestSessionFactory() as db:
        assert (await demo.expire_due(db, held))["blocked"] == 1
    async with TestSessionFactory() as db:
        assert await db.get(TenantModel, uuid.UUID(ann["tenant_id"])) is not None


async def test_the_sales_extension_reopens_an_archived_demo(raw, secret):
    ann = await _make(raw, "ann")
    await _age(ann["tenant_id"], expires_days_ago=1)
    async with TestSessionFactory() as db:
        await demo.expire_due(db, _none)
    r = await raw.post(f"{PATH}/{ann['tenant_id']}/extend", json={"days": 14},
                       headers={HEADER: SECRET})
    assert r.status_code == 200, r.text
    async with TestSessionFactory() as db:
        t = await db.get(TenantModel, uuid.UUID(ann["tenant_id"]))
        assert t.status == "active"
        assert datetime.fromisoformat(t.settings["demo"]["expires_at"]) > \
            datetime.now(timezone.utc) + timedelta(days=13)
    # Only demo tenants can be extended.
    r = await raw.post(f"{PATH}/default/extend", json={"days": 14}, headers={HEADER: SECRET})
    assert r.status_code == 404


# ── a website build's trial tenant (2026-11-10) ──────────────────────────────
# (deleting one now — trial "Delete everything now", account erasure — lives on
# case-service with the tenant's cases: case-service tests/test_erase_lead.py)

async def test_a_trial_is_deleted_at_expiry_without_a_grace_period(raw, secret):
    trial = await _make(raw, "tri", kind="trial")
    signup = await _make(raw, "sig")                      # an ordinary demo sign-up
    await _age(trial["tenant_id"], expires_days_ago=0.01)
    await _age(signup["tenant_id"], expires_days_ago=0.01)
    async with TestSessionFactory() as db:
        out = await demo.expire_due(db, _none)
    assert out["deleted"] == 1 and out["archived"] == 1
    async with TestSessionFactory() as db:
        assert await db.get(TenantModel, uuid.UUID(trial["tenant_id"])) is None
        assert await db.get(HelixUserModel, uuid.UUID(trial["user_id"])) is None
        # the sign-up's tenant keeps its archived grace period, as before
        assert (await db.get(TenantModel, uuid.UUID(signup["tenant_id"]))).status == "archived"


async def test_a_trial_under_a_legal_hold_is_kept(raw, secret):
    trial = await _make(raw, "tri", kind="trial")
    await _age(trial["tenant_id"], expires_days_ago=1)

    async def held(session, tid):
        return 1
    async with TestSessionFactory() as db:
        assert (await demo.expire_due(db, held))["blocked"] == 1
    async with TestSessionFactory() as db:
        assert await db.get(TenantModel, uuid.UUID(trial["tenant_id"])) is not None


async def test_the_trial_admin_is_admin_of_its_own_tenant_only(client, raw, secret):
    """The builder signs in as the trial's admin: it must reach nothing else."""
    trial = await _make(raw, "tri", kind="trial")
    other = await _make(raw, "oth")
    h = _token(trial["user_id"], "tri_demo")
    r = await client.get("/api/v1/tenants", headers=h)
    if r.status_code == 200:
        slugs = {t.get("slug") for t in (r.json() if isinstance(r.json(), list) else r.json().get("items", []))}
        assert other["tenant_slug"] not in slugs and "default" not in slugs


# ── moving the accounts made before this fix ──────────────────────────────────

async def _old_style(username: str, *, superadmin: bool = False) -> HelixUserModel:
    """An account as the website made it before 2026-11-04: `admin`, no
    directory row (= the default tenant), in the default tenant's Demo Users."""
    async with TestSessionFactory() as db:
        u = HelixUserModel(username=username, email=f"{username}@vx.test", password_hash="x",
                           roles=["admin"], is_superadmin=superadmin)
        g = AccessGroupModel(id=uuid.uuid4(), name="Demo Users", tenant_id="default",
                             portal_id=demo._STAFF_PORTAL, role_ids=[demo._ADMIN_ROLE])
        db.add_all([u, g])
        await db.flush()
        db.add(OperatorAccessGroupModel(operator_id=username, access_group_id=g.id, is_primary=True))
        await db.commit()
        return u


async def test_an_old_demo_account_moves_and_loses_operator_powers(client, raw, secret):
    old = await _old_style("olga")
    me = _headers(str(old.id), ["admin"])
    # Before: a platform operator — it can create tenants.
    r = await client.post("/api/v1/tenants", headers=me, json={"slug": "olga-made", "name": "x"})
    assert r.status_code == 201, r.text

    r = await raw.post(f"{PATH[:-len('/tenants')]}/move", headers={HEADER: SECRET},
                       json={"request_key": "key-olga-0001", "username": "olga", "kind": "work"})
    assert r.status_code == 200 and r.json()["moved"] is True, r.text
    tenancy.clear_cache()

    # After: admin of its own tenant only.
    r = await client.post("/api/v1/tenants", headers=me, json={"slug": "olga-two", "name": "x"})
    assert r.status_code == 403
    r = await client.get("/api/v1/tenants", headers=me)
    assert {t["slug"] for t in r.json()} == {demo.slug_for("key-olga-0001")}
    async with TestSessionFactory() as db:
        m = (await db.execute(select(OperatorAccessGroupModel).where(
            OperatorAccessGroupModel.operator_id == "olga"))).scalars().all()
        groups = [await db.get(AccessGroupModel, x.access_group_id) for x in m]
        assert [g.tenant_id for g in groups] == [demo.slug_for("key-olga-0001")]

    # Idempotent.
    r = await raw.post(f"{PATH[:-len('/tenants')]}/move", headers={HEADER: SECRET},
                       json={"request_key": "key-olga-0001", "username": "olga"})
    assert r.status_code == 200 and r.json()["moved"] is False


async def test_a_superadmin_or_another_tenants_account_is_never_moved(raw, secret):
    await _old_style("root", superadmin=True)
    move = f"{PATH[:-len('/tenants')]}/move"
    r = await raw.post(move, headers={HEADER: SECRET},
                       json={"request_key": "key-root-0001", "username": "root"})
    assert r.status_code == 409
    async with TestSessionFactory() as db:
        t = TenantModel(id=uuid.uuid4(), slug="acme", name="Acme")
        u = HelixUserModel(username="acme_admin", email="a@acme.test", password_hash="x",
                           roles=["admin"])
        db.add_all([t, u])
        await db.flush()
        db.add(UserDirectoryModel(id=uuid.uuid4(), user_id=str(u.id), tenant_id="acme"))
        await db.commit()
    r = await raw.post(move, headers={HEADER: SECRET},
                       json={"request_key": "key-acme-0001", "username": "acme_admin"})
    assert r.status_code == 409
    async with TestSessionFactory() as db:
        assert (await db.execute(select(TenantModel).where(
            TenantModel.slug.like("demo-%")))).scalars().first() is None


# ── found by the live sweep as a demo admin (2026-11-04) ──────────────────────

async def test_portal_submissions_are_the_callers_tenants_only(client, raw, secret):
    from velaris_shared.db.models import CaseInstanceModel, CaseTypeModel
    ann = await _make(raw, "ann")
    async with TestSessionFactory() as db:
        default = (await db.execute(select(TenantModel).where(TenantModel.slug == "default"))).scalar_one()
        ct = CaseTypeModel(name="Claims", version="1.0.0", definition_json={"stages": []})
        db.add(ct)
        await db.flush()
        db.add(CaseInstanceModel(case_type_id=ct.id, case_type_version="1.0.0", status="new",
                                 priority="medium", data={}, tenant_id=default.id,
                                 portal_tracking_token=uuid.uuid4(),
                                 portal_submitter_email="victim@default.test"))
        await db.commit()
    r = await client.get("/api/v1/portal-admin/submissions", headers=_token(ann["user_id"], "ann_demo"))
    assert r.status_code == 200, r.text
    assert "victim@default.test" not in r.text
    # The platform operator still sees every tenant's.
    r = await client.get("/api/v1/portal-admin/submissions", headers=_headers("root", ["superadmin"]))
    assert "victim@default.test" in r.text


async def test_a_tenant_admin_cannot_change_a_default_case_types_portal(client, raw, secret):
    from velaris_shared.db.models import CaseTypeModel
    ann = await _make(raw, "ann")
    async with TestSessionFactory() as db:
        ct = CaseTypeModel(name="Default type", version="1.0.0", portal_enabled=False,
                           definition_json={"stages": []})
        db.add(ct)
        await db.commit()
    me = _token(ann["user_id"], "ann_demo")
    r = await client.patch(f"/api/v1/portal-admin/case-types/{ct.id}/portal?enabled=true", headers=me)
    assert r.status_code == 403
    r = await client.patch(f"/api/v1/portal-admin/case-types/{ct.id}/portal-display", headers=me,
                           json={"expected_days": 3})
    assert r.status_code == 403
    async with TestSessionFactory() as db:
        assert (await db.get(CaseTypeModel, ct.id)).portal_enabled is False
    # Reading it stays allowed; the operator may change it.
    assert (await client.get(f"/api/v1/portal-admin/case-types/{ct.id}/portal-display",
                             headers=me)).status_code == 200
    r = await client.patch(f"/api/v1/portal-admin/case-types/{ct.id}/portal?enabled=true",
                           headers=_headers("root", ["superadmin"]))
    assert r.status_code == 200


# ── second sweep (2026-11-04): every route a tenant admin holds ───────────────

async def test_a_tenant_admin_cannot_link_another_tenants_account_to_an_idp(client, raw, secret):
    """The takeover: bind the operator's account to an SSO identity you own."""
    ann = await _make(raw, "ann")
    async with TestSessionFactory() as db:
        op = HelixUserModel(username="operator", email="op@vx.test", password_hash="x",
                            roles=["admin"])          # default tenant, not superadmin
        db.add(op)
        await db.commit()
    me = _token(ann["user_id"], "ann_demo")
    r = await client.post("/api/v1/identity/provisioning/links", headers=me,
                          json={"identifier": "operator", "sso_provider": "google",
                                "sso_subject": "attacker"})
    assert r.status_code == 404, r.text
    r = await client.post("/api/v1/identity/provisioning/links/unlink", headers=me,
                          json={"identifier": "operator"})
    assert r.status_code == 404
    async with TestSessionFactory() as db:
        assert (await db.get(HelixUserModel, op.id)).sso_subject is None
    # Its own account it may link.
    r = await client.post("/api/v1/identity/provisioning/links", headers=me,
                          json={"identifier": "ann_demo", "sso_provider": "google",
                                "sso_subject": "ann-sub"})
    assert r.status_code == 200, r.text
    # And the provisioning list shows only its tenant's accounts.
    r = await client.get("/api/v1/identity/provisioning/links?linked_only=false", headers=me)
    assert {x["username"] for x in r.json()["links"]} == {"ann_demo"}


async def test_a_group_mapping_must_be_the_callers_tenant_and_group(client, raw, secret):
    ann = await _make(raw, "ann")
    async with TestSessionFactory() as db:
        sysgrp = AccessGroupModel(id=uuid.uuid4(), name="Administrators", tenant_id="system",
                                  portal_id=demo._STAFF_PORTAL, role_ids=[demo._ADMIN_ROLE])
        db.add(sysgrp)
        await db.commit()
    me = _token(ann["user_id"], "ann_demo")
    for tenant, group in (("default", sysgrp.id), (ann["tenant_slug"], sysgrp.id)):
        r = await client.post("/api/v1/identity/provisioning/group-mappings", headers=me,
                              json={"tenant": tenant, "source": "saml", "external_group": "g",
                                    "access_group_id": str(group)})
        assert r.status_code == 404, (tenant, r.text)


async def test_tenantless_records_are_the_default_tenants(client, raw, secret):
    """A log row from before tenancy is not shown to a new tenant."""
    from velaris_shared.db.models import IdentityProvisionLogModel
    ann = await _make(raw, "ann")
    async with TestSessionFactory() as db:
        db.add(IdentityProvisionLogModel(source="admin_link", external_id="secret@default.test",
                                         username="secret@default.test", action="link",
                                         result="ok", detail={}))
        await db.commit()
    r = await client.get("/api/v1/identity/provisioning/log", headers=_token(ann["user_id"], "ann_demo"))
    assert r.status_code == 200 and "secret@default.test" not in r.text
    r = await client.get("/api/v1/identity/provisioning/log", headers=_headers("op", ["admin"]))
    assert "secret@default.test" in r.text          # the default tenant's own


async def test_saved_reports_are_tenant_and_owner_scoped(raw, secret):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "analytics-service"))
    from analytics_service.api.routers.analytics import _rpt_visible
    from velaris_shared.auth.models import AuthenticatedUser
    from velaris_shared.db.models import SavedReportModel
    ann = await _make(raw, "ann")
    demo_user = AuthenticatedUser(user_id=ann["user_id"], username="ann_demo", roles=["admin"],
                                  tenant_id=ann["tenant_slug"])
    op = AuthenticatedUser(user_id="op", username="op", roles=["admin"], tenant_id="default")
    private_default = SavedReportModel(name="Board pack", is_public=False, created_by="op", tenant_id=None)
    public_default = SavedReportModel(name="Shared", is_public=True, created_by="op", tenant_id=None)
    mine = SavedReportModel(name="Mine", is_public=False, created_by=ann["user_id"],
                            tenant_id=ann["tenant_slug"])
    async with TestSessionFactory() as db:
        assert not await _rpt_visible(db, demo_user, private_default)
        assert not await _rpt_visible(db, demo_user, public_default)   # another tenant's
        assert await _rpt_visible(db, demo_user, mine)
        assert await _rpt_visible(db, op, private_default)


async def test_no_database_or_mail_server_on_this_network(client, raw, secret):
    ann = await _make(raw, "ann")
    me = _token(ann["user_id"], "ann_demo")
    r = await client.post("/api/v1/hxdbmigrate/sources", headers=me, json={
        "name": "p", "source_type": "postgresql", "host": "127.0.0.1", "port": 5432,
        "database": "velaris", "username": "velaris", "password": "guess"})
    assert r.status_code == 400 and "private" in r.text, r.text
    for host in ("127.0.0.1", "10.0.0.5", "192.168.0.1", "localhost", "0.0.0.0", "169.254.169.254"):
        r = await client.post("/api/v1/email/accounts", headers=me, json={
            "name": "m", "address": "m@x.org", "smtp_host": host, "smtp_port": 25})
        assert r.status_code == 400, (host, r.text)


async def test_platform_wide_features_are_the_operators(client, raw, secret):
    ann = await _make(raw, "ann")
    me = _token(ann["user_id"], "ann_demo")
    # Audit stream: a tenant's target is its own tenant's (tested below); it
    # may not point at this install's own network.
    r = await client.post("/api/v1/audit-stream/targets", headers=me, json={
        "name": "exfil", "endpoint_url": "https://127.0.0.1/hec",
        "acknowledge_platform_scope": True})
    assert r.status_code == 400, r.text
    assert (await client.get("/api/v1/audit-stream/targets", headers=me)).status_code == 200
    for path in ("/api/v1/status/internal", "/api/v1/status/authz-role-collisions",
                 "/api/v1/status/authz-parity"):
        assert (await client.get(path, headers=me)).status_code == 403, path
    # The operator still reaches them.
    assert (await client.get("/api/v1/audit-stream/targets",
                             headers=_headers("op", ["admin"]))).status_code == 200


async def test_the_operators_connectors_are_not_another_tenants(client, raw, secret):
    from velaris_shared.db.models import ConnectorRegistryModel
    ann = await _make(raw, "ann")
    async with TestSessionFactory() as db:
        c = ConnectorRegistryModel(name="Operator CRM", connector_type="http", config={},
                                   credentials={}, tenant_id=None, enabled=True)
        db.add(c)
        await db.commit()
    me = _token(ann["user_id"], "ann_demo")
    r = await client.get("/api/v1/hxbridge/connectors", headers=me)
    assert r.status_code == 200 and "Operator CRM" not in r.text
    for method, path in (("GET", f"/api/v1/hxbridge/connectors/{c.id}"),
                         ("POST", f"/api/v1/hxbridge/connectors/{c.id}/test")):
        r = await client.request(method, path, headers=me)
        assert r.status_code == 404, (path, r.status_code)
    # The default tenant still has them.
    r = await client.get("/api/v1/hxbridge/connectors", headers=_headers("op", ["admin"]))
    assert "Operator CRM" in r.text


async def test_no_deployment_target_on_this_network(client, raw, secret):
    ann = await _make(raw, "ann")
    me = _token(ann["user_id"], "ann_demo")
    for url in ("http://127.0.0.1:8201", "http://192.168.0.10", "http://169.254.169.254"):
        r = await client.post("/api/v1/deploy/environments", headers=me,
                              json={"name": "x", "label": "x", "url": url})
        assert r.status_code == 400, (url, r.text)
    r = await client.post("/api/v1/deploy/environments", headers=me,
                          json={"name": "x", "label": "x", "webhook_url": "http://10.1.1.1/hook"})
    assert r.status_code == 400


async def test_platform_security_rows_are_the_operators(client, raw, secret):
    """Shield: a tenant admin could approve a platform-scope response (revoke the
    operator's sessions) and dismiss a platform incident (hide an attack)."""
    from velaris_shared.db.models import SecurityIncidentModel, ShieldResponseModel
    ann = await _make(raw, "ann")
    async with TestSessionFactory() as db:
        resp = ShieldResponseModel(tenant_id=None, response_type="revoke_sessions",
                                   target_kind="user", target_id="operator-id", state="pending")
        inc = SecurityIncidentModel(tenant_id=None, pattern_type="credential_stuffing",
                                    severity="high", status="open")
        db.add_all([resp, inc])
        await db.commit()
    me = _token(ann["user_id"], "ann_demo")
    r = await client.get("/api/v1/shield/responses", headers=me)
    assert r.status_code == 200 and str(resp.id) not in r.text
    assert (await client.post(f"/api/v1/shield/responses/{resp.id}/approve", headers=me,
                              json={})).status_code == 404
    assert (await client.post(f"/api/v1/shield/incidents/{inc.id}/dismiss", headers=me,
                              json={})).status_code == 404
    async with TestSessionFactory() as db:
        assert (await db.get(ShieldResponseModel, resp.id)).state == "pending"
        assert (await db.get(SecurityIncidentModel, inc.id)).status == "open"
    # The operator sees the queue.
    r = await client.get("/api/v1/shield/responses", headers=_headers("op", ["admin"]))
    assert str(resp.id) in r.text


async def test_auth_me_says_who_is_the_platform_operator(client, raw, secret):
    ann = await _make(raw, "ann")
    r = await client.get("/api/v1/auth/me", headers=_token(ann["user_id"], "ann_demo"))
    assert r.status_code == 200 and r.json()["is_platform_operator"] is False
    r = await client.get("/api/v1/auth/me", headers=_headers("op", ["admin"]))
    assert r.json()["is_platform_operator"] is True


# ── tenant views (2026-11-04): each tenant's admin sees its own slice ────────

async def test_a_security_event_carries_the_trusted_tenant_and_actor(client, raw, secret):
    from velaris_shared.db.models import SecurityEventModel
    ann = await _make(raw, "ann")
    me = _token(ann["user_id"], "ann_demo")
    r = await client.post("/api/v1/enterprise/security-events", headers={**me, "X-Tenant-Id": "default"},
                          json={"event_type": "probe", "severity": "high", "user_id": "operator-id"})
    assert r.status_code == 201, r.text
    async with TestSessionFactory() as db:
        ev = (await db.execute(select(SecurityEventModel).where(
            SecurityEventModel.event_type == "probe"))).scalar_one()
        assert ev.user_id == ann["user_id"]                 # not the named victim
        assert ev.tenant_id in (ann["tenant_slug"], ann["tenant_id"])   # not the header


async def test_security_events_and_overview_are_the_tenants_own(client, raw, secret):
    from velaris_shared.db.models import SecurityEventModel
    ann = await _make(raw, "ann")
    async with TestSessionFactory() as db:
        db.add_all([SecurityEventModel(event_type="mine", tenant_id=ann["tenant_slug"]),
                    SecurityEventModel(event_type="theirs", tenant_id="default"),
                    SecurityEventModel(event_type="platform", tenant_id=None)])
        await db.commit()
    me = _token(ann["user_id"], "ann_demo")
    r = await client.get("/api/v1/enterprise/security-events", headers=me)
    assert r.status_code == 200, r.text
    assert {e["event_type"] for e in r.json()} == {"mine"}
    r = await client.get("/api/v1/enterprise/system-info", headers=me)
    assert r.status_code == 200, r.text
    r = await client.get("/api/v1/enterprise/security-events", headers=_headers("op", ["admin"]))
    assert {"mine", "theirs", "platform"} <= {e["event_type"] for e in r.json()}


async def test_ai_usage_is_the_tenants_own(client, raw, secret):
    from velaris_shared.db.models import LLMCallModel
    ann = await _make(raw, "ann")
    async with TestSessionFactory() as db:
        for t, n in ((ann["tenant_slug"], 2), ("default", 5), (None, 7)):
            for _ in range(n):
                db.add(LLMCallModel(provider="p", model="m", route="local", purpose="x",
                                    prompt_tokens=1, completion_tokens=1, tokens_estimated=True,
                                    cost_usd=0.0, latency_ms=1.0, status="ok", tenant_id=t))
        await db.commit()
    r = await client.get("/api/v1/hxnexus/llmops/metrics", headers=_token(ann["user_id"], "ann_demo"))
    assert r.status_code == 200, r.text
    assert r.json()["total_calls"] == 2 and r.json()["scope"] == "tenant"
    r = await client.get("/api/v1/hxnexus/llmops/metrics", headers=_headers("op", ["admin"]))
    assert r.json()["total_calls"] == 14


def test_a_log_line_is_a_tenants_only_where_the_filter_tagged_it():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "case-service"))
    from case_service.api.routers.hxlogs import _mine
    lines = [
        "2026-11-04 10:00:00 INFO [t=acme] case created",
        "2026-11-04 10:00:01 INFO [t=evil] note: [t=acme] planted",
        "2026-11-04 10:00:02 INFO job ran [t=acme] by a job",           # untagged: the operator's
        "2026-11-04 10:00:03 ERROR [t=acme] failed\nTraceback (most recent call last):",
    ]
    got = _mine([{"raw": r} for r in lines], {"acme"})
    assert [g["raw"][:25] for g in got] == [lines[0][:25], lines[3][:25]]
    assert len(_mine([{"raw": r} for r in lines], None)) == 4


def test_the_log_filter_tags_with_the_trusted_tenant_and_keeps_one_line():
    import logging
    from velaris_shared.observability.context import tenant_id_var
    from velaris_shared.observability.logging_config import TenantTagFilter
    rec = logging.LogRecord("x", logging.INFO, "f", 1, "hello\n2026-11-04 10:00:00 INFO [t=other] x",
                            None, None)
    tok = tenant_id_var.set("acme")
    try:
        TenantTagFilter().filter(rec)
    finally:
        tenant_id_var.reset(tok)
    assert rec.getMessage().startswith("[t=acme] hello")
    assert "\n" not in rec.getMessage()


async def test_a_tenants_audit_target_carries_its_own_events_only(raw, secret):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "case-service"))
    from case_service.audit_stream.forwarder import _read_batch
    from velaris_shared.db.models import AuditStreamTargetModel, SecurityEventModel
    ann = await _make(raw, "ann")
    async with TestSessionFactory() as db:
        db.add_all([SecurityEventModel(event_type="mine", tenant_id=ann["tenant_slug"]),
                    SecurityEventModel(event_type="theirs", tenant_id="default"),
                    SecurityEventModel(event_type="platform", tenant_id=None)])
        mine = AuditStreamTargetModel(id=uuid.uuid4(), tenant_id=ann["tenant_slug"],
                                      tenant_key=ann["tenant_slug"], name="t", kind="splunk_hec",
                                      endpoint_url="https://siem.example/hec", enabled=True)
        plat = AuditStreamTargetModel(id=uuid.uuid4(), tenant_id=None, tenant_key="platform",
                                      name="p", kind="splunk_hec",
                                      endpoint_url="https://siem.example/hec", enabled=True)
        db.add_all([mine, plat])
        await db.commit()
        assert {e["event_type"] for e in await _read_batch(db, mine)} == {"mine"}
        assert {"mine", "theirs", "platform"} <= {e["event_type"] for e in await _read_batch(db, plat)}


# ── streams sweep (2026-11-04) ────────────────────────────────────────────────

async def test_hxstream_shows_a_tenants_admin_its_own_tenant_only(client, raw, secret):
    from velaris_shared.db.models import TraceEventModel
    ann = await _make(raw, "ann")
    async with TestSessionFactory() as db:
        db.add_all([TraceEventModel(tenant_id=ann["tenant_slug"], event_type="mine", actor_user_id="ann-x"),
                    TraceEventModel(tenant_id="default", event_type="theirs", actor_user_id="victim-x")])
        await db.commit()
    me = _token(ann["user_id"], "ann_demo")
    r = await client.get("/api/v1/hxstream/events", headers=me)
    if r.status_code == 403:            # no stream access in this harness's roles
        pytest.skip("hxstream access not granted to the demo admin here")
    assert r.status_code == 200, r.text
    assert {e["event_type"] for e in r.json()["events"]} <= {"mine"}
    r = await client.get("/api/v1/hxstream/actors", headers=me)
    assert "victim-x" not in r.text
    assert (await client.get("/api/v1/hxstream/bus/backlog", headers=me)).status_code == 403
    r = await client.get("/api/v1/hxstream/events", headers=_headers("op", ["admin", "superadmin"]))
    assert "theirs" in r.text


async def test_intake_events_on_a_shared_case_type_are_the_default_tenants(session):
    from sqlalchemy import select as _select
    from velaris_shared.auth.models import AuthenticatedUser
    from velaris_shared.db.models import CaseTypeModel, IntakeEventModel
    from velaris_shared.tenancy.identity import scope_to_caller
    async with TestSessionFactory() as db:
        acme = TenantModel(id=uuid.uuid4(), slug="acme", name="Acme")
        shared = CaseTypeModel(name="Shared", version="1.0.0", definition_json={"stages": []})
        own = CaseTypeModel(name="Acme's", version="1.0.0", definition_json={"stages": []},
                            tenant_id=acme.id)
        db.add_all([acme, shared, own])
        await db.flush()
        db.add_all([IntakeEventModel(case_type_id=shared.id, raw_payload={"who": "default"}),
                    IntakeEventModel(case_type_id=own.id, raw_payload={"who": "acme"})])
        await db.commit()
    tenancy.clear_cache()
    acme_admin = AuthenticatedUser(user_id="a", username="a", roles=["admin"], tenant_id="acme")
    op = AuthenticatedUser(user_id="o", username="o", roles=["admin"], tenant_id="default")
    for user, want in ((acme_admin, {"acme"}), (op, {"default"})):
        q = await scope_to_caller(_select(IntakeEventModel), IntakeEventModel, session, user,
                                  via=(IntakeEventModel.case_type_id, CaseTypeModel))
        got = {e.raw_payload["who"] for e in (await session.execute(q)).scalars().all()}
        assert want <= got and (got <= want), (user.tenant_id, got)


async def test_a_tenantless_case_channel_follows_strict_reads(monkeypatch):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "case-service"))
    from case_service.api.routers.realtime import _Caller, authorize_channel
    from velaris_shared.db.models import CaseInstanceModel, CaseTypeModel
    async with TestSessionFactory() as db:
        acme = TenantModel(id=uuid.uuid4(), slug="acme2", name="Acme")
        ct = CaseTypeModel(name="T", version="1.0.0", definition_json={"stages": []})
        db.add_all([acme, ct])
        await db.flush()
        c = CaseInstanceModel(case_type_id=ct.id, case_type_version="1.0.0", status="new",
                              priority="medium", data={}, tenant_id=None)
        db.add(c)
        await db.commit()
    tenancy.clear_cache()
    caller = _Caller("u", ["admin"], "acme2")
    monkeypatch.setenv("VELARIS_CASE_TENANT_STRICT_READS", "true")
    assert await authorize_channel(caller, f"cases.{c.id}") is False


# ── connector SSRF (2026-11-04): a tenant's connector reaches the internet only ─

async def test_a_tenant_cannot_build_a_connector_to_this_network(client, raw, secret):
    ann = await _make(raw, "ann")
    me = _token(ann["user_id"], "ann_demo")
    for url in ("http://127.0.0.1:5173/", "http://10.0.0.5/x", "http://169.254.169.254/latest/"):
        r = await client.post("/api/v1/devconn/connectors/build", headers=me, json={
            "name": f"p-{uuid.uuid4().hex[:6]}", "method": "GET", "url": url, "headers": {},
            "auth_type": "none"})
        assert r.status_code == 400, (url, r.status_code, r.text)


async def test_http_custom_checks_the_target_unless_the_operator_owns_it():
    import sys
    import httpx
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "case-service"))
    from case_service.hxbridge.connectors.http_custom_connector import HttpCustomConnector
    from velaris_shared.hxbridge.security import runtime_config
    cfg = {"method": "GET", "url": "http://127.0.0.1:{port}/", "_platform_owned": True}  # a saved value…
    tenant_conn = HttpCustomConnector(runtime_config(cfg, False), {})                  # …is dropped
    with pytest.raises(ValueError):
        await tenant_conn.execute({"port": "1"})
    # A placeholder in the authority (the port here) lets the form's input pick
    # the target, so even the operator's connector is then held to public ones.
    op_templated = HttpCustomConnector(runtime_config(cfg, True), {})
    with pytest.raises(ValueError):
        await op_templated.execute({"port": "1"})
    # The operator's fixed LAN target is tried (nothing listens on :1).
    op_conn = HttpCustomConnector(runtime_config({**cfg, "url": "http://127.0.0.1:1/"}, True), {})
    with pytest.raises(httpx.HTTPError):
        await op_conn.execute({})
    assert await tenant_conn.test() is False
