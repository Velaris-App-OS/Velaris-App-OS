"""The sequences Entra ID and Okta actually run against a new SCIM server.

Both products drive provisioning from a fixed script and stop at the first
surprise — and Entra *quarantines* a job after repeated 4xx, which silently ends
leaver deprovisioning as well as joiner creation. So the shapes matter as much
as the semantics, and each test here is one step of their real sequence or one
requirement of RFC 7644 that they enforce.

What this cannot be: a run of Microsoft's or Okta's own validator against a live
tenant. What it is: their documented request shapes — capitalised `op` values,
string booleans, quoted filters, `count=0` counting, the error body — asserted
against the running app.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import hashlib
import uuid

import pytest
from sqlalchemy import select

from velaris_shared.db.models import HelixUserModel, ScimTokenModel
from user_service.identity import scim

SCIM = "/api/v1/scim/v2"


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def _token(session) -> dict:
    plaintext = f"scim-{uuid.uuid4().hex}"
    session.add(ScimTokenModel(
        tenant_id=None, name=_uniq("idp"),
        token_hash=hashlib.sha256(plaintext.encode()).hexdigest(),
        scopes=["users:write"], created_by="tester",
    ))
    await session.commit()
    return {"Authorization": f"Bearer {plaintext}"}


def _user_payload(username: str, **over) -> dict:
    payload = {
        "schemas": [scim.SCHEMA_USER],
        "userName": username,
        "externalId": f"ext-{username}",
        "name": {"givenName": "Ada", "familyName": "Lovelace"},
        "emails": [{"primary": True, "type": "work", "value": f"{username}@acme.com"}],
        "displayName": "Ada Lovelace",
        "active": True,
    }
    payload.update(over)
    return payload


class TestDiscovery:
    """Step 0 for both products: read the three discovery documents."""

    @pytest.mark.asyncio
    async def test_every_document_is_scim_json(self, session, anon_client):
        auth = await _token(session)
        for path in ("/ServiceProviderConfig", "/ResourceTypes", "/Schemas"):
            resp = await anon_client.get(f"{SCIM}{path}", headers=auth)
            assert resp.status_code == 200, path
            assert resp.headers["content-type"].startswith("application/scim+json"), path

    @pytest.mark.asyncio
    async def test_the_service_provider_config_declares_what_we_do(
        self, session, anon_client,
    ):
        auth = await _token(session)
        body = (await anon_client.get(f"{SCIM}/ServiceProviderConfig", headers=auth)).json()
        assert body["schemas"] == [scim.SCHEMA_SP_CONFIG]
        assert body["patch"]["supported"] is True
        assert body["filter"]["supported"] is True
        # Declaring bulk or changePassword support we do not have is how a client
        # ends up sending requests we answer with a 4xx it treats as fatal.
        assert body["bulk"]["supported"] is False
        assert isinstance(body["authenticationSchemes"], list)
        assert body["authenticationSchemes"], "Okta reads this before authenticating"

    @pytest.mark.asyncio
    async def test_resource_types_name_both_endpoints(self, session, anon_client):
        auth = await _token(session)
        body = (await anon_client.get(f"{SCIM}/ResourceTypes", headers=auth)).json()
        by_id = {r["id"]: r for r in body["Resources"]}
        assert set(by_id) == {"User", "Group"}
        assert by_id["User"]["endpoint"] == "/Users"
        assert by_id["User"]["schema"] == scim.SCHEMA_USER


class TestEntraSequence:
    """Entra's joiner → mover → leaver script, in order."""

    @pytest.mark.asyncio
    async def test_the_filter_probe_for_an_unknown_user_is_an_empty_list(
        self, session, anon_client,
    ):
        """**Not a 404.** Entra asks "do you have this person?" with a filter and
        expects an empty `ListResponse`; a 404 here reads as a broken server."""
        auth = await _token(session)
        resp = await anon_client.get(
            f"{SCIM}/Users", params={"filter": 'userName eq "nobody@acme.com"'},
            headers=auth)
        assert resp.status_code == 200
        body = resp.json()
        assert body["schemas"] == [scim.SCHEMA_LIST]
        assert body["totalResults"] == 0
        assert body["Resources"] == []

    @pytest.mark.asyncio
    async def test_count_zero_is_a_count_not_a_page(self, session, anon_client):
        """Entra sends `count=0` to size a sync before running it."""
        auth = await _token(session)
        name = _uniq("joiner")
        await anon_client.post(f"{SCIM}/Users", json=_user_payload(name), headers=auth)
        resp = await anon_client.get(
            f"{SCIM}/Users", params={"filter": f'userName eq "{name}"', "count": 0},
            headers=auth)
        body = resp.json()
        assert body["totalResults"] == 1
        assert body["Resources"] == []
        assert body["itemsPerPage"] == 0

    @pytest.mark.asyncio
    async def test_create_then_fetch_by_the_id_we_returned(self, session, anon_client):
        auth = await _token(session)
        name = _uniq("joiner")
        created = await anon_client.post(
            f"{SCIM}/Users", json=_user_payload(name), headers=auth)
        assert created.status_code == 201
        body = created.json()
        assert body["meta"]["resourceType"] == "User"
        assert body["meta"]["location"].endswith(f"/Users/{body['id']}")
        assert body["externalId"] == f"ext-{name}"
        # RFC 7644 §3.1: the response to a create SHOULD carry Location, and
        # Okta's validator checks it.
        assert created.headers.get("location") == body["meta"]["location"]

        fetched = await anon_client.get(f"{SCIM}/Users/{body['id']}", headers=auth)
        assert fetched.status_code == 200 and fetched.json()["id"] == body["id"]

    @pytest.mark.asyncio
    async def test_a_duplicate_create_is_409_with_the_uniqueness_keyword(
        self, session, anon_client,
    ):
        """Entra treats `409 + scimType: uniqueness` as "fetch the existing one";
        anything else it retries forever."""
        auth = await _token(session)
        name = _uniq("joiner")
        await anon_client.post(f"{SCIM}/Users", json=_user_payload(name), headers=auth)
        again = await anon_client.post(
            f"{SCIM}/Users", json=_user_payload(name), headers=auth)
        assert again.status_code == 409
        body = again.json()
        assert body["schemas"] == [scim.SCHEMA_ERROR]
        assert body["scimType"] == "uniqueness"
        assert body["status"] == "409", "RFC 7644: status is a *string*"

    @pytest.mark.asyncio
    async def test_the_leaver_patch_entra_actually_sends(self, session, anon_client):
        """Capitalised `Replace`, and `"False"` as a **string**.

        This is the request that disables a leaver. Reading it as anything other
        than false leaves them able to sign in.
        """
        auth = await _token(session)
        name = _uniq("leaver")
        created = (await anon_client.post(
            f"{SCIM}/Users", json=_user_payload(name), headers=auth)).json()

        resp = await anon_client.patch(
            f"{SCIM}/Users/{created['id']}", headers=auth, json={
                "schemas": [scim.SCHEMA_PATCH],
                "Operations": [{"op": "Replace", "path": "active", "value": "False"}],
            })
        assert resp.status_code == 200
        assert resp.json()["active"] is False

        user = (await session.execute(select(HelixUserModel).where(
            HelixUserModel.username == name))).scalar_one()
        assert user.is_active is False

    @pytest.mark.asyncio
    async def test_a_pathless_replace_of_the_whole_object(self, session, anon_client):
        auth = await _token(session)
        name = _uniq("mover")
        created = (await anon_client.post(
            f"{SCIM}/Users", json=_user_payload(name), headers=auth)).json()
        resp = await anon_client.patch(
            f"{SCIM}/Users/{created['id']}", headers=auth, json={
                "schemas": [scim.SCHEMA_PATCH],
                "Operations": [{"op": "Replace", "value": {"active": "True",
                                                           "displayName": "Ada L."}}],
            })
        assert resp.status_code == 200
        assert resp.json()["displayName"] == "Ada L."

    @pytest.mark.asyncio
    async def test_a_rename_of_one_name_half(self, session, anon_client):
        """Entra sends a surname change as `name.familyName` and nothing else."""
        auth = await _token(session)
        name = _uniq("mover")
        created = (await anon_client.post(
            f"{SCIM}/Users", json=_user_payload(name), headers=auth)).json()
        resp = await anon_client.patch(
            f"{SCIM}/Users/{created['id']}", headers=auth, json={
                "schemas": [scim.SCHEMA_PATCH],
                "Operations": [{"op": "Replace", "path": "name.familyName",
                                "value": "Byron"}],
            })
        assert resp.status_code == 200
        assert resp.json()["displayName"] == "Ada Byron"


class TestOktaSequence:
    """Okta's script differs in three places that have broken servers before."""

    @pytest.mark.asyncio
    async def test_lowercase_ops_and_json_booleans(self, session, anon_client):
        auth = await _token(session)
        name = _uniq("leaver")
        created = (await anon_client.post(
            f"{SCIM}/Users", json=_user_payload(name), headers=auth)).json()
        resp = await anon_client.patch(
            f"{SCIM}/Users/{created['id']}", headers=auth, json={
                "schemas": [scim.SCHEMA_PATCH],
                "Operations": [{"op": "replace", "value": {"active": False}}],
            })
        assert resp.status_code == 200 and resp.json()["active"] is False

    @pytest.mark.asyncio
    async def test_the_email_filter_okta_uses_to_reconcile(self, session, anon_client):
        auth = await _token(session)
        name = _uniq("person")
        await anon_client.post(f"{SCIM}/Users", json=_user_payload(name), headers=auth)
        resp = await anon_client.get(
            f"{SCIM}/Users",
            params={"filter": f'emails.value eq "{name}@acme.com"'}, headers=auth)
        assert resp.status_code == 200
        assert [r["userName"] for r in resp.json()["Resources"]] == [name]

    @pytest.mark.asyncio
    async def test_a_replace_of_the_whole_user_keeps_the_id(self, session, anon_client):
        """Okta sends PUT, not PATCH, when its "push profile" runs."""
        auth = await _token(session)
        name = _uniq("person")
        created = (await anon_client.post(
            f"{SCIM}/Users", json=_user_payload(name), headers=auth)).json()
        resp = await anon_client.put(
            f"{SCIM}/Users/{created['id']}", headers=auth,
            json=_user_payload(name, displayName="Ada Byron King"))
        assert resp.status_code == 200
        assert resp.json()["id"] == created["id"]
        assert resp.json()["displayName"] == "Ada Byron King"

    @pytest.mark.asyncio
    async def test_an_unsupported_filter_says_so_in_the_rfc_shape(
        self, session, anon_client,
    ):
        """`co`/`sw`/`or` are refused — but the refusal has to be a SCIM error,
        because a bare 500 or an HTML body stops the job dead."""
        auth = await _token(session)
        resp = await anon_client.get(
            f"{SCIM}/Users", params={"filter": 'userName co "ada"'}, headers=auth)
        assert resp.status_code == 400
        body = resp.json()
        assert body["schemas"] == [scim.SCHEMA_ERROR]
        assert body["scimType"] == "invalidFilter"
        assert body["status"] == "400"


class TestErrorShapes:
    """Every refusal a client can provoke, in the one shape it understands."""

    @pytest.mark.asyncio
    async def test_unknown_id_is_a_scim_404(self, session, anon_client):
        auth = await _token(session)
        resp = await anon_client.get(f"{SCIM}/Users/{uuid.uuid4()}", headers=auth)
        assert resp.status_code == 404
        assert resp.headers["content-type"].startswith("application/scim+json")
        assert resp.json()["schemas"] == [scim.SCHEMA_ERROR]
        assert resp.json()["status"] == "404"

    @pytest.mark.asyncio
    async def test_the_401_is_a_scim_body_too(self, anon_client):
        """The auth dependency runs before the route, so its refusal has to be
        shaped by the app-level handler rather than FastAPI's default."""
        resp = await anon_client.get(f"{SCIM}/Users")
        assert resp.status_code == 401
        assert resp.headers["content-type"].startswith("application/scim+json")
        assert resp.json()["schemas"] == [scim.SCHEMA_ERROR]
        assert resp.json()["status"] == "401"
