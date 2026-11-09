"""Who may create a case — the access roles defined in Admin Studio (2026-10-18).

What was wrong: `POST /api/v1/cases` checked only that the caller was signed in,
so a `viewer` created cases. Now a caller needs an access group whose roles
grant `case` → `create` for that case type, with the type among the group's
allowed case types. No access group = refused. Superadmin is break-glass.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient

from case_service.main import app
from velaris_shared.auth.dependencies import get_current_user
from velaris_shared.auth.models import ActiveAccessGroup, AuthenticatedUser
from velaris_shared.hxguard.case_create import NO_GROUP, case_create_decision

from tests.conftest import deploy_case_type

STAFF_ROLE = [{"actions": ["create", "read", "update", "assign"], "resource": "case", "case_type_id": "*"}]
VIEWER_ROLE = [{"actions": ["read", "update"], "resource": "case", "case_type_id": "*"}]


def _group(privileges, allowed=("*",), name="G") -> ActiveAccessGroup:
    return ActiveAccessGroup(
        id="g1", name=name, portal_id="", portal_type="staff", portal_name="",
        modules=[], homepage="/", roles=[], privileges=list(privileges),
        allowed_case_type_ids=list(allowed), allowed_queue_ids=["*"])


def _user(group=None, roles=("case_worker",), uid="u-1") -> AuthenticatedUser:
    u = AuthenticatedUser(user_id=uid, email=f"{uid}@test.local", roles=list(roles))
    u.active_access_group = group
    return u


# ── the decision ──────────────────────────────────────────────────────────────

CT = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"


class TestDecision:
    def test_no_access_group_is_refused(self):
        ok, why = case_create_decision(_user(None), CT)
        assert not ok and why == NO_GROUP

    def test_platform_admin_role_alone_is_not_enough(self):
        assert not case_create_decision(_user(None, roles=("admin",)), CT)[0]

    def test_superadmin_passes_without_a_group(self):
        assert case_create_decision(_user(None, roles=("superadmin",)), CT)[0]

    def test_role_granting_case_create_passes(self):
        assert case_create_decision(_user(_group(STAFF_ROLE)), CT)[0]

    def test_viewer_role_is_refused(self):
        ok, why = case_create_decision(_user(_group(VIEWER_ROLE)), CT)
        assert not ok and "Cases > Create" in why

    def test_wildcard_role_passes(self):
        assert case_create_decision(_user(_group([{"resource": "*", "actions": ["*"]}])), CT)[0]

    def test_grant_scoped_to_another_case_type_is_refused(self):
        role = [{"resource": "case", "actions": ["create"], "case_type_id": OTHER}]
        assert not case_create_decision(_user(_group(role)), CT)[0]
        assert case_create_decision(_user(_group(role)), OTHER)[0]

    def test_case_type_outside_the_groups_allowed_list_is_refused(self):
        ok, why = case_create_decision(_user(_group(STAFF_ROLE, allowed=(OTHER,))), CT)
        assert not ok and "does not include this case type" in why

    def test_deny_wins_over_allow(self):
        role = STAFF_ROLE + [{"resource": "case", "actions": ["create"], "case_type_id": CT,
                              "effect": "deny"}]
        assert not case_create_decision(_user(_group(role)), CT)[0]
        assert case_create_decision(_user(_group(role)), OTHER)[0]

    def test_other_resources_do_not_grant_it(self):
        role = [{"resource": "case_type", "actions": ["create"]},
                {"resource": "document", "actions": ["create"]}]
        assert not case_create_decision(_user(_group(role)), CT)[0]


# ── the routes ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_route_follows_the_access_group(client: AsyncClient):
    ct = await deploy_case_type(client)              # as the superadmin test client

    app.dependency_overrides[get_current_user] = lambda: _user(_group(STAFF_ROLE), uid="worker")
    ok = await client.post("/api/v1/cases", json={"case_type_id": ct["id"], "data": {}})
    assert ok.status_code == 201, ok.text

    app.dependency_overrides[get_current_user] = lambda: _user(_group(VIEWER_ROLE), uid="viewer")
    refused = await client.post("/api/v1/cases", json={"case_type_id": ct["id"], "data": {}})
    assert refused.status_code == 403 and "Cases > Create" in refused.text

    app.dependency_overrides[get_current_user] = lambda: _user(None, uid="nogroup")
    refused = await client.post("/api/v1/cases", json={"case_type_id": ct["id"], "data": {}})
    assert refused.status_code == 403 and "not in an access group" in refused.text


@pytest.mark.asyncio
async def test_unknown_case_type_is_still_404_not_403(client: AsyncClient):
    app.dependency_overrides[get_current_user] = lambda: _user(None, uid="nogroup")
    r = await client.post("/api/v1/cases", json={"case_type_id": CT, "data": {}})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_child_case_needs_create_on_the_child_type(client: AsyncClient):
    parent_ct = await deploy_case_type(client, name="Parent")
    child_ct = await deploy_case_type(client, name="Child")
    owner = _user(_group([{"resource": "case", "actions": ["create"], "case_type_id": parent_ct["id"]}]),
                  uid="owner")
    app.dependency_overrides[get_current_user] = lambda: owner
    parent = await client.post("/api/v1/cases", json={"case_type_id": parent_ct["id"], "data": {}})
    assert parent.status_code == 201, parent.text

    r = await client.post(f"/api/v1/cases/{parent.json()['id']}/children",
                          json={"case_type_id": child_ct["id"], "data": {}})
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_split_action_needs_create_on_the_child_type(client: AsyncClient):
    from case_service.case_actions import dispatcher as disp
    ct = await deploy_case_type(client, name="Splittable")
    other = await deploy_case_type(client, name="Other")
    parent = await client.post("/api/v1/cases", json={"case_type_id": ct["id"], "data": {}})
    assert parent.status_code == 201
    user = _user(_group([{"resource": "case", "actions": ["create"], "case_type_id": ct["id"]}]),
                 roles=("superadmin",))
    # superadmin passes the action privilege; drop it to reach the create rule
    user.roles = ["admin"]
    from tests.conftest import TestSessionFactory
    async with TestSessionFactory() as s:
        res = await disp.perform(s, case_id=__import__("uuid").UUID(parent.json()["id"]),
                                 action_name="split", params={"case_type_id": other["id"]},
                                 user=user, tenant_uuid=None)
    assert not res.ok and res.code == "VEL-CASE-ACTION-DENIED-403"
    assert "Cases > Create" in (res.error or "")
