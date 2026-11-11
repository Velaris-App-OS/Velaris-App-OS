"""A user's tenant is set where the operator sets it — end to end (2026-11-09).

Reproduced on the DuckDNS server: the operator created a user in User Directory
with the `admin` role and put them in tenant `acme`'s access group. Sign-in
takes the tenant from the directory row, which had none — the default tenant,
i.e. the install's operator — so the user saw every tenant and every user.

Every step here goes through the routes Studio calls, in Studio's order; no row
is written to the database by the test (feedback_test_through_real_path).

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.asyncio

PW = "Tenant-Place-Pw1!"


async def _tenant(client, slug: str) -> str:
    r = await client.post("/api/v1/tenants", json={"slug": slug, "name": slug.title()})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _group(client, tenant_id: str, name: str) -> str:
    p = await client.post("/api/v1/portals", json={"name": f"{name} portal", "tenant_id": tenant_id})
    assert p.status_code == 201, p.text
    g = await client.post("/api/v1/access-groups", json={
        "name": name, "tenant_id": tenant_id, "portal_id": p.json()["id"]})
    assert g.status_code == 201, g.text
    return g.json()["id"]


async def _studio_create(client, username: str, roles: list[str], tenant_id: str | None):
    """User Directory → New user, as Studio sends it."""
    r = await client.post("/api/v1/auth/real/register", json={
        "username": username, "email": f"{username}@x.test", "password": PW,
        "roles": roles, "password_change_required": False})
    assert r.status_code == 201, r.text
    entry = {"user_id": username, "email": f"{username}@x.test", "roles": roles,
             "access_group_ids": [], "timezone": "UTC", "is_active": True,
             "metadata_json": {}, "tenant_id": tenant_id}
    r = await client.post("/api/v1/user-directory", json=entry)
    assert r.status_code == 201, r.text
    return r.json()


async def _sign_in(anon_client, username: str) -> dict:
    r = await anon_client.post("/api/v1/auth/real/login", json={"username": username, "password": PW})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def test_the_server_report_is_refused_and_the_tenant_field_fixes_it(client, anon_client):
    s = uuid.uuid4().hex[:6]
    acme = await _tenant(client, f"acme{s}")
    other = await _tenant(client, f"other{s}")
    acme_group = await _group(client, acme, f"Acme staff {s}")
    other_group = await _group(client, other, f"Other staff {s}")

    # The exact server sequence: no tenant on the user, then a tenant's group.
    entry = await _studio_create(client, f"modal{s}", ["admin"], None)
    r = await client.post(f"/api/v1/access-groups/{acme_group}/members",
                          json={"operator_id": entry["user_id"]})
    assert r.status_code == 422, r.text
    assert "Set the user's tenant" in r.json()["detail"]

    # User Directory → Edit → Tenant = acme, then the group (Studio's order).
    entry["tenant_id"] = acme
    r = await client.patch(f"/api/v1/user-directory/{entry['user_id']}", json=entry)
    assert r.status_code == 200, r.text
    assert r.json()["tenant_id"] == acme
    r = await client.post(f"/api/v1/access-groups/{acme_group}/members",
                          json={"operator_id": entry["user_id"]})
    assert r.status_code == 201, r.text
    # …and another tenant's group is refused.
    r = await client.post(f"/api/v1/access-groups/{other_group}/members",
                          json={"operator_id": entry["user_id"]})
    assert r.status_code == 422, r.text

    # Signed in, the user is acme's admin — not the operator.
    me = await _sign_in(anon_client, entry["user_id"])
    r = await anon_client.get("/api/v1/auth/me", headers=me)
    assert r.status_code == 200, r.text
    assert r.json()["is_platform_operator"] is False
    r = await anon_client.get("/api/v1/tenants", headers=me)
    assert r.status_code == 200, r.text
    assert [t["id"] for t in r.json()] == [acme]
    r = await anon_client.get("/api/v1/auth/real/users", headers=me)
    assert r.status_code == 200, r.text
    assert {u["username"] for u in r.json()["users"]} == {entry["user_id"]}

    # The directory shows the membership sign-in reads, not the dead column.
    r = await client.get(f"/api/v1/user-directory/{entry['user_id']}")
    assert r.json()["access_group_ids"] == [acme_group]

    # A move to another tenant is refused while acme's group still holds them.
    r = await client.patch(f"/api/v1/user-directory/{entry['user_id']}",
                           json={**entry, "tenant_id": other})
    assert r.status_code == 409, r.text


async def test_a_tenant_admin_cannot_move_users_or_grant_superadmin(client, anon_client):
    s = uuid.uuid4().hex[:6]
    acme = await _tenant(client, f"acme{s}")
    other = await _tenant(client, f"other{s}")
    boss = await _studio_create(client, f"boss{s}", ["admin"], acme)
    staff = await _studio_create(client, f"staff{s}", ["viewer"], acme)
    hdr = await _sign_in(anon_client, boss["user_id"])

    # Refused: moving a user out of the tenant.
    r = await anon_client.patch(f"/api/v1/user-directory/{staff['user_id']}", headers=hdr,
                                json={**staff, "tenant_id": other})
    assert r.status_code in (403, 404), r.text   # the path guard answers first
    # Refused: superadmin through the role field.
    r = await anon_client.patch(f"/api/v1/user-directory/{staff['user_id']}", headers=hdr,
                                json={**staff, "roles": ["superadmin"]})
    assert r.status_code == 403, r.text
    # Allowed: an ordinary role change — and the next sign-in carries it.
    r = await anon_client.patch(f"/api/v1/user-directory/{staff['user_id']}", headers=hdr,
                                json={**staff, "roles": ["case_worker"]})
    assert r.status_code == 200, r.text
    assert r.json()["roles"] == ["case_worker"]
    staff_hdr = await _sign_in(anon_client, staff["user_id"])
    r = await anon_client.get("/api/v1/auth/me", headers=staff_hdr)
    assert "case_worker" in r.json()["roles"]


async def test_an_unchanged_save_changes_nothing(client, anon_client):
    """An old Studio tab, or a save of another field, sends the row back as read."""
    s = uuid.uuid4().hex[:6]
    acme = await _tenant(client, f"acme{s}")
    entry = await _studio_create(client, f"same{s}", ["viewer"], acme)
    got = (await client.get(f"/api/v1/user-directory/{entry['user_id']}")).json()
    r = await client.patch(f"/api/v1/user-directory/{entry['user_id']}",
                           json={**got, "timezone": "Asia/Kolkata"})
    assert r.status_code == 200, r.text
    assert r.json()["tenant_id"] == acme and r.json()["roles"] == ["viewer"]
    # A body without tenant_id (an older client) leaves the tenant alone.
    body = {k: v for k, v in got.items() if k != "tenant_id"}
    r = await client.patch(f"/api/v1/user-directory/{entry['user_id']}", json=body)
    assert r.status_code == 200 and r.json()["tenant_id"] == acme


async def test_a_tenant_that_does_not_exist_is_refused(client):
    s = uuid.uuid4().hex[:6]
    r = await client.post("/api/v1/auth/real/register", json={
        "username": f"ghost{s}", "email": f"ghost{s}@x.test", "password": PW, "roles": ["viewer"]})
    assert r.status_code == 201
    r = await client.post("/api/v1/user-directory", json={
        "user_id": f"ghost{s}", "tenant_id": "no-such-tenant"})
    assert r.status_code == 422, r.text


async def test_a_user_made_by_a_tenant_admin_shows_its_roles_and_groups(client, anon_client):
    """Found in the browser 2026-11-09: a tenant admin's register keys the
    directory row by the ACCOUNT ID, and the screen showed no roles or groups."""
    s = uuid.uuid4().hex[:6]
    acme = await _tenant(client, f"acme{s}")
    group = await _group(client, acme, f"Acme staff {s}")
    boss = await _studio_create(client, f"boss{s}", ["admin"], acme)
    hdr = await _sign_in(anon_client, boss["user_id"])
    r = await anon_client.post("/api/v1/auth/real/register", headers=hdr, json={
        "username": f"made{s}", "email": f"made{s}@x.test", "password": PW,
        "roles": ["case_worker"], "password_change_required": False})
    assert r.status_code == 201, r.text
    acct_id = r.json()["user_id"]
    r = await anon_client.post(f"/api/v1/access-groups/{group}/members", headers=hdr,
                               json={"operator_id": acct_id})
    assert r.status_code == 201, r.text
    rows = (await anon_client.get("/api/v1/user-directory", headers=hdr)).json()
    made = next(u for u in rows if u["user_id"] == acct_id)
    assert made["roles"] == ["case_worker"] and made["access_group_ids"] == [group]


async def test_a_tenant_admin_cannot_pre_seed_an_id_into_their_group(client, anon_client):
    s = uuid.uuid4().hex[:6]
    acme = await _tenant(client, f"acme{s}")
    group = await _group(client, acme, f"Acme staff {s}")
    boss = await _studio_create(client, f"boss{s}", ["admin"], acme)
    hdr = await _sign_in(anon_client, boss["user_id"])
    r = await anon_client.post(f"/api/v1/access-groups/{group}/members", headers=hdr,
                               json={"operator_id": f"future{s}"})
    assert r.status_code in (404, 422), r.text
    # The operator may — and the name is then checked when it is created.
    r = await client.post(f"/api/v1/access-groups/{group}/members", json={"operator_id": f"future{s}"})
    assert r.status_code == 201, r.text
    other = await _tenant(client, f"other{s}")
    oboss = await _studio_create(client, f"oboss{s}", ["admin"], other)
    ohdr = await _sign_in(anon_client, oboss["user_id"])
    r = await anon_client.post("/api/v1/auth/real/register", headers=ohdr, json={
        "username": f"future{s}", "email": f"future{s}@x.test", "password": PW, "roles": ["viewer"]})
    assert r.status_code == 409, r.text


async def test_another_tenants_group_is_never_applied_at_sign_in(client, anon_client):
    """The leftover shape (the server's user): a membership of tenant acme's
    group on a user who is not acme's. Signed in, the group is ignored."""
    s = uuid.uuid4().hex[:6]
    acme = await _tenant(client, f"acme{s}")
    group = await _group(client, acme, f"Acme staff {s}")
    r = await client.post(f"/api/v1/access-groups/{group}/members", json={"operator_id": f"left{s}"})
    assert r.status_code == 201, r.text
    # The operator makes the account and no directory entry: default tenant.
    r = await client.post("/api/v1/auth/real/register", json={
        "username": f"left{s}", "email": f"left{s}@x.test", "password": PW,
        "roles": ["viewer"], "password_change_required": False})
    assert r.status_code == 201, r.text
    hdr = await _sign_in(anon_client, f"left{s}")
    me = (await anon_client.get("/api/v1/auth/me", headers=hdr)).json()
    assert not me.get("active_access_group"), me
    # Put in acme, the same membership now applies.
    r = await client.post("/api/v1/user-directory", json={"user_id": f"left{s}", "tenant_id": acme})
    assert r.status_code == 201, r.text
    me = (await anon_client.get("/api/v1/auth/me", headers=hdr)).json()
    assert (me.get("active_access_group") or {}).get("id") == group, me


async def test_the_operator_cannot_move_their_own_account(client, anon_client):
    s = uuid.uuid4().hex[:6]
    acme = await _tenant(client, f"acme{s}")
    op = await _studio_create(client, f"op{s}", ["admin"], None)   # default tenant = operator
    hdr = await _sign_in(anon_client, op["user_id"])
    r = await anon_client.patch(f"/api/v1/user-directory/{op['user_id']}", headers=hdr,
                                json={**op, "tenant_id": acme})
    assert r.status_code == 409, r.text


async def test_a_tenant_admins_new_user_is_one_entry_with_its_group(client, anon_client):
    """Found in the browser 2026-11-09: Studio's New user as a tenant admin made
    two directory entries (register's, by account id; Studio's, by username)."""
    s = uuid.uuid4().hex[:6]
    acme = await _tenant(client, f"acme{s}")
    group = await _group(client, acme, f"Acme staff {s}")
    boss = await _studio_create(client, f"boss{s}", ["admin"], acme)
    hdr = await _sign_in(anon_client, boss["user_id"])
    name = f"fresh{s}"
    r = await anon_client.post("/api/v1/auth/real/register", headers=hdr, json={
        "username": name, "email": f"{name}@x.test", "password": PW,
        "roles": ["case_worker"], "password_change_required": False})
    assert r.status_code == 201, r.text
    r = await anon_client.post("/api/v1/user-directory", headers=hdr, json={
        "user_id": name, "email": f"{name}@x.test", "display_name": "Fresh",
        "roles": ["case_worker"], "access_group_ids": [group], "timezone": "UTC",
        "is_active": True, "metadata_json": {}, "tenant_id": None})
    assert r.status_code == 201, r.text
    r = await anon_client.post(f"/api/v1/access-groups/{group}/members", headers=hdr,
                               json={"operator_id": name})
    assert r.status_code == 201, r.text
    rows = [u for u in (await anon_client.get("/api/v1/user-directory", headers=hdr)).json()
            if u.get("email") == f"{name}@x.test"]
    assert len(rows) == 1, rows
    assert rows[0]["display_name"] == "Fresh" and rows[0]["access_group_ids"] == [group]


async def _studio_create_v2(api, hdr, username: str, roles: list[str], tenant_id, groups=()):
    """Studio's order since 2026-11-09: entry (with tenant) → login → groups."""
    kw = {"headers": hdr} if hdr else {}
    r = await api.post("/api/v1/user-directory", **kw, json={
        "user_id": username, "email": f"{username}@x.test", "roles": roles,
        "access_group_ids": [], "timezone": "UTC", "is_active": True,
        "metadata_json": {}, "tenant_id": tenant_id})
    assert r.status_code == 201, r.text
    r = await api.post("/api/v1/auth/real/register", **kw, json={
        "username": username, "email": f"{username}@x.test", "password": PW,
        "roles": roles, "password_change_required": False})
    assert r.status_code == 201, r.text
    for g in groups:
        r = await api.post(f"/api/v1/access-groups/{g}/members", **kw, json={"operator_id": username})
        assert r.status_code == 201, r.text


async def test_studio_order_operator_and_tenant_admin(client, anon_client):
    s = uuid.uuid4().hex[:6]
    acme = await _tenant(client, f"acme{s}")
    group = await _group(client, acme, f"Acme staff {s}")
    # Operator: a tenant user with the tenant's group, in one pass.
    await _studio_create_v2(client, None, f"opmade{s}", ["admin"], acme, [group])
    me = await _sign_in(anon_client, f"opmade{s}")
    got = (await anon_client.get("/api/v1/auth/me", headers=me)).json()
    assert got["is_platform_operator"] is False
    assert (got.get("active_access_group") or {}).get("id") == group
    # Tenant admin: same order, ONE directory entry.
    await _studio_create_v2(anon_client, me, f"tamade{s}", ["case_worker"], None, [group])
    rows = [u for u in (await anon_client.get("/api/v1/user-directory", headers=me)).json()
            if u.get("email") == f"tamade{s}@x.test"]
    assert len(rows) == 1 and rows[0]["access_group_ids"] == [group], rows


async def test_a_tenant_admin_cannot_adopt_an_account_with_no_entry(client, anon_client):
    """An account the operator made through the API, with no directory entry,
    is the default tenant's — another tenant's admin may not give it one."""
    s = uuid.uuid4().hex[:6]
    acme = await _tenant(client, f"acme{s}")
    r = await client.post("/api/v1/auth/real/register", json={
        "username": f"opsonly{s}", "email": f"opsonly{s}@x.test", "password": PW,
        "roles": ["admin"], "password_change_required": False})
    assert r.status_code == 201, r.text
    boss = await _studio_create(client, f"boss{s}", ["admin"], acme)
    hdr = await _sign_in(anon_client, boss["user_id"])
    for uid in (f"opsonly{s}", f"opsonly{s}@x.test", r.json()["user_id"]):
        r2 = await anon_client.post("/api/v1/user-directory", headers=hdr, json={"user_id": uid})
        assert r2.status_code in (403, 404), (uid, r2.text)


async def test_new_user_with_a_taken_username_or_email_is_named_before_any_write(client, anon_client):
    """Reported 2026-11-10: New User with an email already in use answered "Not
    found" (the tenant guard's 404 on the directory step), shown behind the
    modal. Studio now asks `/register/availability` first, as a tenant admin."""
    s = uuid.uuid4().hex[:6]
    acme = await _tenant(client, f"acme{s}")
    other = await _tenant(client, f"other{s}")
    boss = await _studio_create(client, f"boss{s}", ["admin"], acme)
    taken = await _studio_create(client, f"taken{s}", ["viewer"], other)   # another tenant's
    viewer = await _studio_create(client, f"view{s}", ["viewer"], acme)
    hdr = await _sign_in(anon_client, boss["user_id"])
    url = "/api/v1/auth/real/register/availability"

    r = await anon_client.get(url, headers=hdr, params={"username": f"new{s}", "email": taken["email"]})
    assert r.status_code == 200, r.text
    assert r.json() == {"username_taken": False, "email_taken": True}
    r = await anon_client.get(url, headers=hdr, params={"username": taken["user_id"], "email": f"new{s}@x.test"})
    assert r.json() == {"username_taken": True, "email_taken": False}
    r = await anon_client.get(url, headers=hdr, params={"username": f"new{s}", "email": f"NEW{s}@x.test"})
    assert r.json() == {"username_taken": False, "email_taken": False}
    # Same answer register gives — its 409, not a 500, when username and email
    # belong to two different accounts.
    r = await anon_client.post("/api/v1/auth/real/register", headers=hdr, json={
        "username": boss["user_id"], "email": taken["email"], "password": PW})
    assert r.status_code == 409, r.text

    # Refused: a user without identity.users.write.
    vhdr = await _sign_in(anon_client, viewer["user_id"])
    r = await anon_client.get(url, headers=vhdr, params={"username": "x", "email": "x@x.test"})
    assert r.status_code == 403, r.text
