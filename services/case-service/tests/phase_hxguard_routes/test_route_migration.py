"""HxGuard route-migration (2026-07-09) — the case sub-resource endpoints.

Previously these routes had NO authentication at all (history, relationships,
children, assignments, step-completions) or no case-level check (transition).
Now: Bearer required + require_case (case.read for reads / case.update for
writes), with the standard enforce-mode 404 anti-oracle.

Copyright (c) 2024-2026 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
from tests.conftest import deploy_case_type, create_case


def _limited_headers(roles: list[str]) -> dict:
    from case_service.auth.jwt_handler import create_dev_token
    from case_service.config import get_settings

    s = get_settings()
    token = create_dev_token(
        user_id=str(uuid.uuid4()), username="routes-limited", roles=roles,
        secret=s.auth_secret, private_key=s.auth_rsa_private_key or "",
    )
    return {"Authorization": f"Bearer {token}"}


# (method, path-template, json body or None) for every migrated route
def _routes(case_id: str, target_id: str) -> list[tuple[str, str, dict | None]]:
    return [
        ("GET",  f"/api/v1/cases/{case_id}/history", None),
        ("GET",  f"/api/v1/cases/{case_id}/relationships", None),
        ("POST", f"/api/v1/cases/{case_id}/relationships",
         {"target_case_id": target_id, "relationship_type": "related"}),
        ("GET",  f"/api/v1/cases/{case_id}/children", None),
        ("GET",  f"/api/v1/cases/{case_id}/assignments", None),
        ("POST", f"/api/v1/cases/{case_id}/assignments",
         {"step_id": "s1", "assignee_type": "user", "assignee_id": "u1"}),
        ("GET",  f"/api/v1/cases/{case_id}/step-completions", None),
    ]


class TestRouteMigration:
    async def test_anonymous_401_everywhere(self, anon_client):
        cid = str(uuid.uuid4())
        for method, path, body in _routes(cid, cid):
            resp = await anon_client.request(method, path, json=body)
            assert resp.status_code == 401, f"{method} {path} -> {resp.status_code}"
        # stage transition + child-create too
        assert (await anon_client.post(
            f"/api/v1/cases/{cid}/stage",
            json={"target_stage_id": "s2"})).status_code == 401
        assert (await anon_client.post(
            f"/api/v1/cases/{cid}/children",
            json={"case_type_id": cid, "data": {}})).status_code == 401

    async def test_authenticated_owner_still_works(self, client):
        ct = await deploy_case_type(client, name="Routes Owner CT")
        case = await create_case(client, ct["id"])
        other = await create_case(client, ct["id"])
        for method, path, body in _routes(case["id"], other["id"]):
            resp = await client.request(method, path, json=body)
            assert resp.status_code in (200, 201), f"{method} {path} -> {resp.status_code}"

    async def test_enforce_unrelated_user_404_anti_oracle(self, client):
        from case_service.config import get_settings
        ct = await deploy_case_type(client, name="Routes Enforce CT")
        case = await create_case(client, ct["id"])       # admin-owned
        other = await create_case(client, ct["id"])
        hdrs = _limited_headers(["user"])                # unrelated human
        get_settings().hxguard_case_enforcement = "enforce"
        try:
            for method, path, body in _routes(case["id"], other["id"]):
                resp = await client.request(method, path, json=body, headers=hdrs)
                assert resp.status_code == 404, f"{method} {path} -> {resp.status_code}"
            # stage transition (had auth, lacked the case check before)
            resp = await client.post(
                f"/api/v1/cases/{case['id']}/stage",
                json={"target_stage_id": "s2"}, headers=hdrs)
            assert resp.status_code == 404
        finally:
            get_settings().hxguard_case_enforcement = "shadow"

    async def test_shadow_mode_passes_unrelated_user(self, client):
        """Back-compat: in shadow (the code default) nothing is blocked."""
        ct = await deploy_case_type(client, name="Routes Shadow CT")
        case = await create_case(client, ct["id"])
        hdrs = _limited_headers(["user"])
        resp = await client.get(f"/api/v1/cases/{case['id']}/history", headers=hdrs)
        assert resp.status_code == 200
