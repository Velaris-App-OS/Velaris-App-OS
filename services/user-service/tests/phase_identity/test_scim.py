"""SCIM 2.0 server — joiner, mover, leaver.

Two things are being pinned here. The first is protocol conformance: Entra and
Okta run a fixed sequence against a new SCIM server and stop at the first
surprise, so the shapes have to be right. The second matters more — **a SCIM
token is a standing credential that provisions and deprovisions accounts**, so
the refusals around it get the same attention as the happy path.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from velaris_shared.db.models import (
    AccessGroupModel,
    HelixUserModel,
    OperatorAccessGroupModel,
    PortalModel,
    RefreshTokenModel,
    ScimTokenModel,
    TenantModel,
    UserDirectoryModel,
)
from user_service.identity import scim
from velaris_shared.tenancy import identity as tenant_identity

SCIM = "/api/v1/scim/v2"


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def _token(session, *, tenant_id: str | None = None, **overrides) -> str:
    plaintext = f"scim-{uuid.uuid4().hex}"
    session.add(ScimTokenModel(
        tenant_id=tenant_id,
        name=_uniq("idp"),
        token_hash=hashlib.sha256(plaintext.encode()).hexdigest(),
        scopes=["users:write"],
        created_by="tester",
        **overrides,
    ))
    await session.commit()
    return plaintext


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _user_payload(username: str, **overrides) -> dict:
    payload = {
        "schemas": [scim.SCHEMA_USER],
        "userName": username,
        "externalId": f"ext-{username}",
        "displayName": "Ada Lovelace",
        "emails": [{"value": f"{username}@acme.com", "primary": True, "type": "work"}],
        "active": True,
    }
    payload.update(overrides)
    return payload


# ── the filter subset ─────────────────────────────────────────────────────

class TestFilterParsing:
    def test_the_filters_entra_and_okta_send_all_parse(self):
        for expression, attribute in [
            ('userName eq "ada"', "username"),
            ('externalId eq "ext-1"', "externalid"),
            ("active eq true", "active"),
            ('displayName eq "Ada"', "displayname"),
        ]:
            terms = scim.parse_filter(expression)
            assert len(terms) == 1 and terms[0].attribute == attribute

    def test_an_and_of_two_terms_parses(self):
        terms = scim.parse_filter('userName eq "ada" and active eq true')
        assert [t.attribute for t in terms] == ["username", "active"]

    def test_an_empty_filter_is_no_filter(self):
        assert scim.parse_filter(None) == [] and scim.parse_filter("  ") == []

    @pytest.mark.parametrize("expression", [
        'userName co "ada"',
        'userName sw "ad"',
        "userName pr",
        'userName eq "ada" or userName eq "bob"',
        '(userName eq "ada")',
        'nickName eq "ada"',
        'userName eq null',
        "userName",
    ])
    def test_anything_outside_the_subset_is_refused_not_guessed(self, expression):
        """Silently ignoring an operator returns the wrong set, and an IdP acting
        on the wrong set deprovisions the wrong people."""
        with pytest.raises(scim.ScimError) as caught:
            scim.parse_filter(expression)
        assert caught.value.scim_type == "invalidFilter"
        assert caught.value.status == 400

    def test_a_quoted_value_may_contain_spaces_and_escapes(self):
        terms = scim.parse_filter(r'displayName eq "Ada \"The First\" Lovelace"')
        assert terms[0].value == 'Ada "The First" Lovelace'


class TestPatchParsing:
    def test_the_leaver_signal_is_understood_in_every_shape_idps_send_it(self):
        shapes = [
            {"Operations": [{"op": "replace", "path": "active", "value": False}]},
            {"Operations": [{"op": "Replace", "path": "active", "value": "False"}]},
            {"Operations": [{"op": "replace", "value": {"active": False}}]},
            {"Operations": [{"op": "remove", "path": "active"}]},
        ]
        for body in shapes:
            assert scim.parse_user_patch(body).active is False, body

    def test_an_unknown_path_is_ignored_rather_than_failing_the_operation(self):
        """IdPs send their whole mapped attribute set on every change; refusing
        because one of them is `nickName` breaks provisioning over a field
        nobody uses."""
        patch = scim.parse_user_patch({"Operations": [
            {"op": "replace", "path": "nickName", "value": "Ada"},
            {"op": "replace", "path": "displayName", "value": "Ada L"},
        ]})
        assert patch.display_name == "Ada L" and patch.active is None

    def test_a_patch_that_changes_nothing_we_store_is_refused(self):
        """A path in neither table — not something we store, and not something
        an IdP maps by default either."""
        with pytest.raises(scim.ScimError):
            scim.parse_user_patch({"Operations": [
                {"op": "replace", "path": "x509Certificates", "value": []}]})

    @pytest.mark.parametrize("path", [
        "nickName", "title", "userType",
        f"{scim.SCHEMA_ENTERPRISE_USER}:department",
    ])
    def test_an_attribute_idps_map_but_we_do_not_store_is_acknowledged(self, path):
        """Answering these `400` is the quarantine trap `name.givenName` was:
        Entra suspends a provisioning job after repeated 4xx, and a suspended
        job stops deactivating leavers as well as renaming them. They change
        nothing here — they just do not fail the request."""
        patch = scim.parse_user_patch({"Operations": [
            {"op": "replace", "path": path, "value": "anything"}]})
        assert patch.touched is True
        assert (patch.active, patch.display_name, patch.email) == (None, None, None)

    def test_a_rename_is_understood_in_both_shapes_the_two_products_send(self):
        entra = scim.parse_user_patch({"Operations": [
            {"op": "Replace", "path": "name.givenName", "value": "Priya"},
            {"op": "Replace", "path": "name.familyName", "value": "Raman"}]})
        okta = scim.parse_user_patch({"Operations": [
            {"op": "replace",
             "value": {"name": {"givenName": "Priya", "familyName": "Raman"}}}]})
        for patch in (entra, okta):
            assert (patch.given_name, patch.family_name) == ("Priya", "Raman")
            assert scim.compose_display_name(patch, None)[0] == "Priya Raman"

    def test_half_a_name_joins_the_half_we_remembered(self):
        patch = scim.parse_user_patch({"Operations": [
            {"op": "Replace", "path": "name.familyName", "value": "Raman-Okafor"}]})
        name, parts = scim.compose_display_name(
            patch, {"given": "Priya", "family": "Raman"},
            current_display_name="Priya Raman")
        assert name == "Priya Raman-Okafor"
        assert parts == {"given": "Priya", "family": "Raman-Okafor"}

    def test_half_a_name_never_replaces_a_whole_one(self):
        """An account provisioned before the halves were recorded has no
        counterpart to join to. Answering a surname change with the surname
        alone would turn 'Priya Raman' into 'Raman'."""
        patch = scim.parse_user_patch({"Operations": [
            {"op": "Replace", "path": "name.familyName", "value": "Raman"}]})
        name, parts = scim.compose_display_name(
            patch, None, current_display_name="Priya Raman")
        assert name is None
        assert parts == {"family": "Raman"}

    def test_an_explicit_display_name_still_wins_over_the_parts(self):
        patch = scim.parse_user_patch({"Operations": [
            {"op": "Replace", "path": "name.givenName", "value": "Priya"},
            {"op": "Replace", "path": "displayName", "value": "Dr P. Raman"}]})
        assert scim.compose_display_name(patch, {"family": "Raman"})[0] == "Dr P. Raman"

    def test_an_empty_operations_list_is_refused(self):
        with pytest.raises(scim.ScimError):
            scim.parse_user_patch({"Operations": []})

    def test_an_unsupported_op_is_refused(self):
        with pytest.raises(scim.ScimError):
            scim.parse_user_patch({"Operations": [{"op": "merge", "path": "active",
                                                   "value": True}]})


# ── authentication ────────────────────────────────────────────────────────

class TestTokenAuthentication:
    @pytest.mark.asyncio
    async def test_no_token_is_refused(self, session, anon_client):
        resp = await anon_client.get(f"{SCIM}/Users")
        assert resp.status_code == 401
        assert resp.json()["schemas"] == [scim.SCHEMA_ERROR]

    @pytest.mark.asyncio
    async def test_a_velaris_jwt_is_not_a_scim_credential(self, session, client):
        """The default `client` fixture carries an admin JWT. SCIM is a machine
        surface and an operator's browser session is the wrong key for it."""
        assert (await client.get(f"{SCIM}/Users")).status_code == 401

    @pytest.mark.asyncio
    async def test_every_failure_answers_identically(self, session, anon_client):
        """A different body for "revoked" tells an attacker their stolen token
        was real, and a different one for "expired" tells them when it died."""
        good = await _token(session)
        revoked = await _token(session, revoked_at=datetime.now(timezone.utc))
        expired = await _token(
            session, expires_at=datetime.now(timezone.utc) - timedelta(days=1))

        bodies = set()
        for header in [
            {},
            {"Authorization": "Bearer not-a-token"},
            {"Authorization": f"Basic {good}"},
            _auth(revoked),
            _auth(expired),
        ]:
            resp = await anon_client.get(f"{SCIM}/Users", headers=header)
            assert resp.status_code == 401
            bodies.add(resp.text)
        assert len(bodies) == 1, bodies

        assert (await anon_client.get(f"{SCIM}/Users", headers=_auth(good))).status_code == 200

    @pytest.mark.asyncio
    async def test_last_used_is_recorded(self, session, anon_client):
        """An admin needs to see which integrations are live before rotating."""
        token = await _token(session)
        await anon_client.get(f"{SCIM}/Users", headers=_auth(token))
        row = (await session.execute(
            select(ScimTokenModel).where(
                ScimTokenModel.token_hash
                == hashlib.sha256(token.encode()).hexdigest())
        )).scalar_one()
        assert row.last_used_at is not None


# ── discovery ─────────────────────────────────────────────────────────────

class TestDiscovery:
    @pytest.mark.asyncio
    async def test_service_provider_config_declares_what_we_do_and_do_not(
        self, session, anon_client,
    ):
        token = await _token(session)
        body = (await anon_client.get(
            f"{SCIM}/ServiceProviderConfig", headers=_auth(token))).json()
        assert body["patch"]["supported"] is True
        assert body["bulk"]["supported"] is False
        assert body["sort"]["supported"] is False

    @pytest.mark.asyncio
    async def test_resource_types_and_schemas_are_served(self, session, anon_client):
        token = await _token(session)
        for path in ("ResourceTypes", "Schemas"):
            body = (await anon_client.get(f"{SCIM}/{path}", headers=_auth(token))).json()
            assert body["schemas"] == [scim.SCHEMA_LIST]
            assert body["totalResults"] >= 2


# ── the lifecycle ─────────────────────────────────────────────────────────

class TestUserLifecycle:
    @pytest.mark.asyncio
    async def test_create_returns_201_with_an_id_the_idp_can_store(
        self, session, anon_client,
    ):
        token = await _token(session)
        name = _uniq("ada")
        resp = await anon_client.post(
            f"{SCIM}/Users", json=_user_payload(name), headers=_auth(token))
        assert resp.status_code == 201
        body = resp.json()
        assert body["userName"] == name
        assert body["externalId"] == f"ext-{name}"
        assert body["active"] is True
        assert uuid.UUID(body["id"])

    @pytest.mark.asyncio
    async def test_create_reaches_both_records(self, session, anon_client):
        """The whole point of the P0 spine: SCIM is not a directory-only writer."""
        token = await _token(session)
        name = _uniq("ada")
        await anon_client.post(f"{SCIM}/Users", json=_user_payload(name),
                               headers=_auth(token))

        login = (await session.execute(
            select(HelixUserModel).where(HelixUserModel.username == name)
        )).scalar_one_or_none()
        entry = (await session.execute(
            select(UserDirectoryModel).where(UserDirectoryModel.user_id == name)
        )).scalar_one_or_none()
        assert login is not None and entry is not None
        assert login.is_active and entry.is_active

    @pytest.mark.asyncio
    async def test_creating_the_same_person_twice_is_a_409_with_uniqueness(
        self, session, anon_client,
    ):
        """Entra reads `scimType: uniqueness` as "fetch the existing one" rather
        than as a failure to retry forever."""
        token = await _token(session)
        first = _uniq("ada")
        await anon_client.post(f"{SCIM}/Users", json=_user_payload(first),
                               headers=_auth(token))
        clash = _user_payload(_uniq("bob"), emails=[
            {"value": f"{first}@acme.com", "primary": True}])
        resp = await anon_client.post(f"{SCIM}/Users", json=clash, headers=_auth(token))
        assert resp.status_code == 409
        assert resp.json()["scimType"] == "uniqueness"

    @pytest.mark.asyncio
    async def test_a_create_never_rewrites_an_existing_account(self, session, anon_client):
        """`upsert_principal` matches by email as a last resort, which is right
        for a sync and wrong for a POST: it would rewrite somebody else's record
        and hand the IdP their id as the new user's."""
        token = await _token(session)
        held = _uniq("held")
        first = (await anon_client.post(
            f"{SCIM}/Users", json=_user_payload(held), headers=_auth(token))).json()

        await anon_client.post(
            f"{SCIM}/Users",
            json=_user_payload(_uniq("impostor"), displayName="Impostor",
                               emails=[{"value": f"{held}@acme.com", "primary": True}]),
            headers=_auth(token))

        victim = await session.get(HelixUserModel, uuid.UUID(first["id"]))
        await session.refresh(victim)
        assert victim.username == held
        assert victim.display_name == "Ada Lovelace"

    @pytest.mark.asyncio
    async def test_patch_active_false_deactivates_and_ends_sessions(
        self, session, anon_client,
    ):
        token = await _token(session)
        name = _uniq("leaver")
        created = (await anon_client.post(
            f"{SCIM}/Users", json=_user_payload(name), headers=_auth(token))).json()

        user = await session.get(HelixUserModel, uuid.UUID(created["id"]))
        session.add(RefreshTokenModel(
            token_hash="b" * 64, user_id=str(user.id), jti=str(uuid.uuid4()),
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        ))
        await session.commit()

        resp = await anon_client.patch(
            f"{SCIM}/Users/{created['id']}",
            json={"schemas": [scim.SCHEMA_PATCH],
                  "Operations": [{"op": "replace", "path": "active", "value": False}]},
            headers=_auth(token),
        )
        assert resp.status_code == 200 and resp.json()["active"] is False

        await session.refresh(user)
        assert user.is_active is False
        refresh = (await session.execute(
            select(RefreshTokenModel).where(RefreshTokenModel.user_id == str(user.id))
        )).scalar_one()
        assert refresh.revoked_at is not None

    @pytest.mark.asyncio
    async def test_a_later_patch_does_not_reactivate_a_leaver(self, session, anon_client):
        """A display-name change arriving after the disable must not undo it."""
        token = await _token(session)
        name = _uniq("leaver")
        created = (await anon_client.post(
            f"{SCIM}/Users", json=_user_payload(name), headers=_auth(token))).json()

        await anon_client.patch(
            f"{SCIM}/Users/{created['id']}",
            json={"Operations": [{"op": "replace", "path": "active", "value": False}]},
            headers=_auth(token))
        await anon_client.patch(
            f"{SCIM}/Users/{created['id']}",
            json={"Operations": [{"op": "replace", "path": "displayName",
                                  "value": "New Name"}]},
            headers=_auth(token))

        user = await session.get(HelixUserModel, uuid.UUID(created["id"]))
        await session.refresh(user)
        assert user.is_active is False

    @pytest.mark.asyncio
    async def test_delete_deactivates_and_keeps_the_row(self, session, anon_client):
        """Cases reference their creator by user id; deleting orphans history."""
        token = await _token(session)
        name = _uniq("deleted")
        created = (await anon_client.post(
            f"{SCIM}/Users", json=_user_payload(name), headers=_auth(token))).json()

        resp = await anon_client.delete(f"{SCIM}/Users/{created['id']}",
                                        headers=_auth(token))
        assert resp.status_code == 204

        user = await session.get(HelixUserModel, uuid.UUID(created["id"]))
        await session.refresh(user)
        assert user is not None and user.is_active is False

    @pytest.mark.asyncio
    async def test_put_with_active_false_deactivates_too(self, session, anon_client):
        token = await _token(session)
        name = _uniq("replaced")
        created = (await anon_client.post(
            f"{SCIM}/Users", json=_user_payload(name), headers=_auth(token))).json()

        resp = await anon_client.put(
            f"{SCIM}/Users/{created['id']}",
            json=_user_payload(name, active=False), headers=_auth(token))
        assert resp.status_code == 200 and resp.json()["active"] is False

    @pytest.mark.asyncio
    async def test_the_enterprise_manager_extension_round_trips(self, session, anon_client):
        token = await _token(session)
        name = _uniq("reports-to")
        payload = _user_payload(name)
        payload[scim.SCHEMA_ENTERPRISE_USER] = {"manager": {"value": "the-boss"}}
        created = (await anon_client.post(
            f"{SCIM}/Users", json=payload, headers=_auth(token))).json()

        fetched = (await anon_client.get(f"{SCIM}/Users/{created['id']}",
                                         headers=_auth(token))).json()
        assert fetched[scim.SCHEMA_ENTERPRISE_USER]["manager"]["value"] == "the-boss"

    @pytest.mark.asyncio
    async def test_a_missing_username_is_a_400_not_a_500(self, session, anon_client):
        token = await _token(session)
        resp = await anon_client.post(
            f"{SCIM}/Users", json={"schemas": [scim.SCHEMA_USER]}, headers=_auth(token))
        assert resp.status_code == 400
        assert resp.json()["scimType"] == "invalidValue"


class TestSearch:
    @pytest.mark.asyncio
    async def test_a_username_filter_finds_exactly_one(self, session, anon_client):
        token = await _token(session)
        name = _uniq("findme")
        await anon_client.post(f"{SCIM}/Users", json=_user_payload(name),
                               headers=_auth(token))

        body = (await anon_client.get(
            f"{SCIM}/Users", params={"filter": f'userName eq "{name}"'},
            headers=_auth(token))).json()
        assert body["totalResults"] == 1
        assert body["Resources"][0]["userName"] == name

    @pytest.mark.asyncio
    async def test_an_external_id_filter_is_how_an_idp_reconciles(self, session, anon_client):
        token = await _token(session)
        name = _uniq("recon")
        await anon_client.post(f"{SCIM}/Users", json=_user_payload(name),
                               headers=_auth(token))

        body = (await anon_client.get(
            f"{SCIM}/Users", params={"filter": f'externalId eq "ext-{name}"'},
            headers=_auth(token))).json()
        assert body["totalResults"] == 1

    @pytest.mark.asyncio
    async def test_an_unsupported_filter_is_a_400_with_invalid_filter(
        self, session, anon_client,
    ):
        token = await _token(session)
        resp = await anon_client.get(
            f"{SCIM}/Users", params={"filter": 'userName co "ad"'}, headers=_auth(token))
        assert resp.status_code == 400
        assert resp.json()["scimType"] == "invalidFilter"

    @pytest.mark.asyncio
    async def test_paging_reports_what_it_actually_returned(self, session, anon_client):
        token = await _token(session)
        for _ in range(3):
            await anon_client.post(f"{SCIM}/Users", json=_user_payload(_uniq("page")),
                                   headers=_auth(token))
        body = (await anon_client.get(
            f"{SCIM}/Users", params={"startIndex": 1, "count": 2},
            headers=_auth(token))).json()
        assert body["itemsPerPage"] == len(body["Resources"]) <= 2
        assert body["startIndex"] == 1


class TestBoundaries:
    @pytest.mark.asyncio
    async def test_a_superadmin_is_invisible_and_untouchable(self, session, anon_client):
        """It exists so a human can recover the platform. An IdP that could see
        it could try to change it."""
        token = await _token(session)
        name = _uniq("root")
        root = HelixUserModel(
            username=name, email=f"{name}@acme.com", roles=["admin"],
            is_superadmin=True, is_active=True,
        )
        session.add(root)
        await session.commit()
        await session.refresh(root)

        listed = (await anon_client.get(
            f"{SCIM}/Users", params={"filter": f'userName eq "{name}"'},
            headers=_auth(token))).json()
        assert listed["totalResults"] == 0

        assert (await anon_client.get(f"{SCIM}/Users/{root.id}",
                                      headers=_auth(token))).status_code == 404
        assert (await anon_client.delete(f"{SCIM}/Users/{root.id}",
                                         headers=_auth(token))).status_code == 404

        await session.refresh(root)
        assert root.is_active is True

    @pytest.mark.asyncio
    async def test_a_token_cannot_read_another_tenants_user(self, session, anon_client):
        """404, never 403 — a 403 confirms the id exists."""
        tenant_a = TenantModel(slug=_uniq("ta"), name="A")
        tenant_b = TenantModel(slug=_uniq("tb"), name="B")
        session.add_all([tenant_a, tenant_b])
        await session.commit()
        tenant_identity.clear_cache()

        token_a = await _token(session, tenant_id=tenant_a.slug)
        token_b = await _token(session, tenant_id=tenant_b.slug)

        created = (await anon_client.post(
            f"{SCIM}/Users", json=_user_payload(_uniq("theirs")),
            headers=_auth(token_a))).json()

        assert (await anon_client.get(f"{SCIM}/Users/{created['id']}",
                                      headers=_auth(token_b))).status_code == 404
        assert (await anon_client.get(f"{SCIM}/Users/{created['id']}",
                                      headers=_auth(token_a))).status_code == 200

    @pytest.mark.asyncio
    async def test_an_unknown_user_id_is_404(self, session, anon_client):
        token = await _token(session)
        assert (await anon_client.get(f"{SCIM}/Users/{uuid.uuid4()}",
                                      headers=_auth(token))).status_code == 404

    @pytest.mark.asyncio
    async def test_a_malformed_user_id_is_404_not_a_crash(self, session, anon_client):
        token = await _token(session)
        assert (await anon_client.get(f"{SCIM}/Users/not-a-uuid",
                                      headers=_auth(token))).status_code == 404


class TestGroups:
    @pytest.mark.asyncio
    async def test_groups_list_with_their_members(self, session, anon_client):
        token = await _token(session)
        name = _uniq("member")
        created = (await anon_client.post(
            f"{SCIM}/Users", json=_user_payload(name), headers=_auth(token))).json()

        portal = PortalModel(name=_uniq("P"), portal_type="staff", modules=[],
                             homepage="/", tenant_id="default")
        session.add(portal)
        await session.flush()
        group = AccessGroupModel(name=_uniq("G"), portal_id=portal.id, role_ids=[],
                                 is_active=True, tenant_id="default")
        session.add(group)
        await session.flush()
        session.add(OperatorAccessGroupModel(operator_id=name, access_group_id=group.id))
        await session.commit()

        body = (await anon_client.get(f"{SCIM}/Groups/{group.id}",
                                      headers=_auth(token))).json()
        assert body["displayName"] == group.name
        assert [m["value"] for m in body["members"]] == [created["id"]]

    @pytest.mark.asyncio
    async def test_groups_are_not_creatable_over_scim(self, session, anon_client):
        """An access group binds a portal, roles and case-type scope — Velaris
        decisions an IdP has no way to express."""
        token = await _token(session)
        resp = await anon_client.post(
            f"{SCIM}/Groups", json={"displayName": "New"}, headers=_auth(token))
        assert resp.status_code == 405


class TestTokenAdministration:
    @pytest.mark.asyncio
    async def test_minting_shows_the_token_once_and_stores_only_its_hash(
        self, session, client,
    ):
        resp = await client.post("/api/v1/scim/tokens", json={"name": "Entra"})
        assert resp.status_code == 201
        plaintext = resp.json()["token"]

        row = (await session.execute(
            select(ScimTokenModel).where(ScimTokenModel.name == "Entra")
        )).scalar_one()
        assert row.token_hash == hashlib.sha256(plaintext.encode()).hexdigest()
        assert plaintext not in row.token_hash

        listed = (await client.get("/api/v1/scim/tokens")).json()["tokens"]
        assert all("token" not in entry for entry in listed)

    @pytest.mark.asyncio
    async def test_revoking_takes_effect_immediately(self, session, client, anon_client):
        minted = (await client.post(
            "/api/v1/scim/tokens", json={"name": "Okta"})).json()
        assert (await anon_client.get(
            f"{SCIM}/Users", headers=_auth(minted["token"]))).status_code == 200

        assert (await client.delete(
            f"/api/v1/scim/tokens/{minted['id']}")).status_code == 204
        assert (await anon_client.get(
            f"{SCIM}/Users", headers=_auth(minted["token"]))).status_code == 401

    @pytest.mark.asyncio
    async def test_token_administration_needs_a_privilege(self, session, anon_client):
        assert (await anon_client.get("/api/v1/scim/tokens")).status_code in (401, 403)
        assert (await anon_client.post(
            "/api/v1/scim/tokens", json={"name": "x"})).status_code in (401, 403)


class TestDirectorylessAccounts:
    """A user with no `user_directory` row belongs to nobody — not to everybody.

    The list query outer-joins the directory, so such a user also produced
    `tenant_id IS NULL`, and `_visible_user` skipped its tenant check entirely
    when the row was missing. Every account created outside the provisioning
    spine — local admins, anything predating it — was therefore readable and
    **deactivatable** by any tenant's SCIM token. Proven live before the fix.
    """

    @pytest.mark.asyncio
    async def test_a_tenant_token_cannot_see_a_user_with_no_directory_row(
        self, session, anon_client,
    ):
        tenant = TenantModel(slug=_uniq("tn"), name="T")
        session.add(tenant)
        await session.commit()
        tenant_identity.clear_cache()
        token = await _token(session, tenant_id=tenant.slug)

        name = _uniq("localadmin")
        local = HelixUserModel(
            username=name, email=f"{name}@acme.com", roles=["admin"],
            is_active=True, password_hash="$2b$12$local",
        )
        session.add(local)
        await session.commit()
        await session.refresh(local)

        listed = (await anon_client.get(
            f"{SCIM}/Users", params={"filter": f'userName eq "{name}"'},
            headers=_auth(token))).json()
        assert listed["totalResults"] == 0

        assert (await anon_client.get(
            f"{SCIM}/Users/{local.id}", headers=_auth(token))).status_code == 404
        assert (await anon_client.delete(
            f"{SCIM}/Users/{local.id}", headers=_auth(token))).status_code == 404
        assert (await anon_client.patch(
            f"{SCIM}/Users/{local.id}", headers=_auth(token), json={
                "schemas": [scim.SCHEMA_PATCH],
                "Operations": [{"op": "replace", "path": "active", "value": False}],
            })).status_code == 404

        await session.refresh(local)
        assert local.is_active is True

    @pytest.mark.asyncio
    async def test_a_tenantless_token_still_sees_everything(self, session, anon_client):
        """The single-tenant deployment's token is unchanged by the fix."""
        token = await _token(session)
        name = _uniq("nodir")
        local = HelixUserModel(
            username=name, email=f"{name}@acme.com", roles=["viewer"], is_active=True,
        )
        session.add(local)
        await session.commit()
        await session.refresh(local)

        assert (await anon_client.get(
            f"{SCIM}/Users/{local.id}", headers=_auth(token))).status_code == 200

    @pytest.mark.asyncio
    async def test_an_untagged_directory_row_is_still_visible(self, session, anon_client):
        """A row the directory holds with no tenant stays shared — deliberately."""
        tenant = TenantModel(slug=_uniq("tn"), name="T")
        session.add(tenant)
        await session.commit()
        tenant_identity.clear_cache()
        token = await _token(session, tenant_id=tenant.slug)

        name = _uniq("legacy")
        legacy = HelixUserModel(
            username=name, email=f"{name}@acme.com", roles=["viewer"], is_active=True,
        )
        session.add(legacy)
        session.add(UserDirectoryModel(
            user_id=name, email=f"{name}@acme.com", display_name="Legacy",
            tenant_id=None, is_active=True,
        ))
        await session.commit()
        await session.refresh(legacy)

        assert (await anon_client.get(
            f"{SCIM}/Users/{legacy.id}", headers=_auth(token))).status_code == 200
