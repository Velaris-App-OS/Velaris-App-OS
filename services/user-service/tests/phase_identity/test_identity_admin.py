"""Identity administration — mappings are the escalation path, so they are gated.

Writing a group mapping hands an IdP group whatever authorization sits behind an
access group. That makes this the one screen in the identity feature where the
privilege boundary matters more than the ergonomics.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from velaris_shared.db.models import (
    AccessGroupModel,
    HelixUserModel,
    IdentityGroupMappingModel,
    IdentityProvisionLogModel,
    PortalModel,
    TenantModel,
    UserDirectoryModel,
)
from user_service.identity import provisioning
from velaris_shared.tenancy import identity as tenant_identity

BASE = "/api/v1/identity/provisioning"


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def _tenant_and_group(session):
    tenant = TenantModel(slug=_uniq("t"), name="T")
    session.add(tenant)
    await session.flush()
    portal = PortalModel(name=_uniq("P"), portal_type="staff", modules=[],
                         homepage="/", tenant_id=tenant.slug)
    session.add(portal)
    await session.flush()
    group = AccessGroupModel(name=_uniq("G"), portal_id=portal.id, role_ids=[],
                             is_active=True, tenant_id=tenant.slug)
    session.add(group)
    await session.commit()
    tenant_identity.clear_cache()
    return tenant, group


class TestGroupMappings:
    @pytest.mark.asyncio
    async def test_a_mapping_is_stored_against_the_resolved_tenant_uuid(
        self, session, client,
    ):
        """Not the slug the admin typed: the platform keeps a slug in some
        tenant columns and a UUID in others, and a mapping keyed on the
        ambiguous form resolves differently depending on how it was written."""
        tenant, group = await _tenant_and_group(session)
        resp = await client.post(f"{BASE}/group-mappings", json={
            "tenant": tenant.slug, "source": "scim",
            "external_group": "Claims-Team", "access_group_id": str(group.id),
        })
        assert resp.status_code == 201

        row = (await session.execute(select(IdentityGroupMappingModel))).scalar_one()
        assert row.tenant_id == tenant.id

    @pytest.mark.asyncio
    async def test_a_tenant_uuid_works_as_well_as_a_slug(self, session, client):
        tenant, group = await _tenant_and_group(session)
        resp = await client.post(f"{BASE}/group-mappings", json={
            "tenant": str(tenant.id), "source": "scim",
            "external_group": "By-UUID", "access_group_id": str(group.id),
        })
        assert resp.status_code == 201

    @pytest.mark.asyncio
    async def test_a_mapping_actually_grants_on_the_next_sync(self, session, client):
        """End to end: the row this endpoint writes is the one the spine reads."""
        tenant, group = await _tenant_and_group(session)
        await client.post(f"{BASE}/group-mappings", json={
            "tenant": tenant.slug, "source": "scim",
            "external_group": "Claims-Team", "access_group_id": str(group.id),
        })

        name = _uniq("member")
        result = await provisioning.upsert_principal(
            session, source="scim", username=name, email=f"{name}@acme.com",
            tenant_token=tenant.slug, group_names=["Claims-Team"],
        )
        await session.commit()
        assert result.groups_added == [str(group.id)]

    @pytest.mark.asyncio
    async def test_a_duplicate_mapping_is_a_409(self, session, client):
        tenant, group = await _tenant_and_group(session)
        payload = {"tenant": tenant.slug, "source": "scim",
                   "external_group": "Dup", "access_group_id": str(group.id)}
        assert (await client.post(f"{BASE}/group-mappings", json=payload)).status_code == 201
        assert (await client.post(f"{BASE}/group-mappings", json=payload)).status_code == 409

    @pytest.mark.asyncio
    async def test_an_unknown_tenant_or_group_is_refused(self, session, client):
        tenant, group = await _tenant_and_group(session)
        assert (await client.post(f"{BASE}/group-mappings", json={
            "tenant": "nope", "source": "scim", "external_group": "X",
            "access_group_id": str(group.id)})).status_code == 404
        assert (await client.post(f"{BASE}/group-mappings", json={
            "tenant": tenant.slug, "source": "scim", "external_group": "X",
            "access_group_id": str(uuid.uuid4())})).status_code == 404

    @pytest.mark.asyncio
    async def test_an_unknown_source_is_refused(self, session, client):
        """A mapping recorded under a source nothing reads would look configured
        and grant nothing."""
        tenant, group = await _tenant_and_group(session)
        assert (await client.post(f"{BASE}/group-mappings", json={
            "tenant": tenant.slug, "source": "ldap", "external_group": "X",
            "access_group_id": str(group.id)})).status_code == 400

    @pytest.mark.asyncio
    async def test_listing_filters_to_nothing_for_an_unknown_tenant(self, session, client):
        """"Deny" is the only safe reading of an unknown tenant — filtering to
        everything would be the opposite."""
        tenant, group = await _tenant_and_group(session)
        await client.post(f"{BASE}/group-mappings", json={
            "tenant": tenant.slug, "source": "scim", "external_group": "X",
            "access_group_id": str(group.id)})
        body = (await client.get(f"{BASE}/group-mappings",
                                 params={"tenant": "nope"})).json()
        assert body["mappings"] == []

    @pytest.mark.asyncio
    async def test_deleting_a_mapping_leaves_existing_memberships(self, session, client):
        """Removing a mapping says "stop granting this", not "revoke what was
        granted" — and the group stops being governed, so no later sync strips
        it either."""
        from velaris_shared.db.models import OperatorAccessGroupModel

        tenant, group = await _tenant_and_group(session)
        created = (await client.post(f"{BASE}/group-mappings", json={
            "tenant": tenant.slug, "source": "scim", "external_group": "Claims",
            "access_group_id": str(group.id)})).json()

        name = _uniq("member")
        await provisioning.upsert_principal(
            session, source="scim", username=name, email=f"{name}@acme.com",
            tenant_token=tenant.slug, group_names=["Claims"],
        )
        await session.commit()

        assert (await client.delete(
            f"{BASE}/group-mappings/{created['id']}")).status_code == 204

        await provisioning.upsert_principal(
            session, source="scim", username=name, email=f"{name}@acme.com",
            tenant_token=tenant.slug, group_names=[],
        )
        await session.commit()

        held = (await session.execute(
            select(OperatorAccessGroupModel).where(
                OperatorAccessGroupModel.operator_id == name)
        )).scalars().all()
        assert [r.access_group_id for r in held] == [group.id]

    @pytest.mark.asyncio
    async def test_mapping_administration_is_gated(self, session, anon_client):
        assert (await anon_client.get(f"{BASE}/group-mappings")).status_code in (401, 403)
        assert (await anon_client.post(
            f"{BASE}/group-mappings", json={})).status_code in (401, 403, 422)


class TestProvisionLog:
    @pytest.mark.asyncio
    async def test_the_log_answers_why_a_user_is_in_a_group(self, session, client):
        tenant, group = await _tenant_and_group(session)
        await client.post(f"{BASE}/group-mappings", json={
            "tenant": tenant.slug, "source": "scim", "external_group": "Claims",
            "access_group_id": str(group.id)})

        name = _uniq("member")
        await provisioning.upsert_principal(
            session, source="scim", username=name, email=f"{name}@acme.com",
            tenant_token=tenant.slug, group_names=["Claims"],
        )
        await session.commit()

        entries = (await client.get(f"{BASE}/log",
                                    params={"username": name})).json()["entries"]
        assert entries and entries[0]["detail"]["groups_added"] == [str(group.id)]

    @pytest.mark.asyncio
    async def test_unmapped_groups_are_surfaced_for_diagnosis(self, session, client):
        """The screen that turns "SSO works but nobody can see anything" into a
        five-second answer."""
        tenant, _group = await _tenant_and_group(session)
        name = _uniq("stranger")
        await provisioning.upsert_principal(
            session, source="scim", username=name, email=f"{name}@acme.com",
            tenant_token=tenant.slug, group_names=["Ghost-Team", "Other-Team"],
        )
        await session.commit()

        body = (await client.get(f"{BASE}/unmapped-groups")).json()
        names = {row["external_group"] for row in body["unmapped"]}
        assert {"Ghost-Team", "Other-Team"} <= names

    @pytest.mark.asyncio
    async def test_the_log_is_gated(self, session, anon_client):
        assert (await anon_client.get(f"{BASE}/log")).status_code in (401, 403)
        assert (await anon_client.get(f"{BASE}/unmapped-groups")).status_code in (401, 403)


class TestAccountLinks:
    """The deliberate half of "a login may not adopt an account by email".

    The ACS refuses an assertion that matches an existing account only by
    address. Without a way to say "yes, this really is that person", the refusal
    is a dead end for every customer whose staff already had accounts before SSO
    — so these endpoints are part of the fix, not a convenience on top of it.
    """

    @staticmethod
    async def _account(session, **overrides) -> HelixUserModel:
        name = _uniq("person")
        user = HelixUserModel(
            username=name, email=f"{name}@acme.com", display_name="Person",
            roles=["viewer"], is_active=True, **overrides,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user

    @pytest.mark.asyncio
    async def test_linking_by_email_then_unlinking(self, session, client):
        user = await self._account(session)

        linked = await client.post(f"{BASE}/links", json={
            "identifier": user.email, "external_id": "idp-nameid-1"})
        assert linked.status_code == 200
        assert linked.json()["external_id"] == "idp-nameid-1"
        assert linked.json()["username"] == user.username

        await session.refresh(user)
        assert user.external_id == "idp-nameid-1"

        # The profile record carries the same field and must not disagree.
        entry = (await session.execute(select(UserDirectoryModel).where(
            UserDirectoryModel.user_id == user.username))).scalar_one_or_none()
        if entry is not None:
            assert entry.external_id == "idp-nameid-1"

        unlinked = await client.post(f"{BASE}/links/unlink",
                                     json={"identifier": user.username})
        assert unlinked.status_code == 200 and unlinked.json()["external_id"] is None
        await session.refresh(user)
        assert user.external_id is None

    @pytest.mark.asyncio
    async def test_an_account_id_and_a_username_work_too(self, session, client):
        user = await self._account(session)
        assert (await client.post(f"{BASE}/links", json={
            "identifier": str(user.id), "external_id": "by-id"})).status_code == 200
        assert (await client.post(f"{BASE}/links", json={
            "identifier": user.username, "external_id": "by-username"})).status_code == 200
        await session.refresh(user)
        assert user.external_id == "by-username"

    @pytest.mark.asyncio
    async def test_two_accounts_cannot_share_an_external_id(self, session, client):
        """`ix_velaris_users_external` is not unique and the lookup takes the
        first row — so a duplicate makes which account an assertion signs in as
        a matter of row order."""
        first = await self._account(session)
        second = await self._account(session)
        assert (await client.post(f"{BASE}/links", json={
            "identifier": first.username, "external_id": "shared"})).status_code == 200
        clash = await client.post(f"{BASE}/links", json={
            "identifier": second.username, "external_id": "shared"})
        assert clash.status_code == 409
        assert first.username in clash.json()["detail"]

        await session.refresh(second)
        assert second.external_id is None

    @pytest.mark.asyncio
    async def test_the_superadmin_cannot_be_linked(self, session, client):
        root = await self._account(session, is_superadmin=True)
        resp = await client.post(f"{BASE}/links", json={
            "identifier": root.username, "external_id": "idp-root"})
        assert resp.status_code == 403
        await session.refresh(root)
        assert root.external_id is None

    @pytest.mark.asyncio
    async def test_an_unknown_account_is_404(self, session, client):
        assert (await client.post(f"{BASE}/links", json={
            "identifier": "nobody@nowhere.test",
            "external_id": "x"})).status_code == 404

    @pytest.mark.asyncio
    async def test_the_listing_finds_an_account_by_any_identifier(self, session, client):
        user = await self._account(session, password_hash="$2b$12$local")
        await client.post(f"{BASE}/links", json={
            "identifier": user.username, "external_id": "findable-1"})

        for query in (user.username, user.email, "findable"):
            found = (await client.get(f"{BASE}/links", params={"q": query})).json()["links"]
            assert [row["username"] for row in found] == [user.username]
            # Whether the account can still sign in with a password is exactly
            # what makes linking it worth a second thought.
            assert found[0]["has_password"] is True
            assert "password_hash" not in found[0]

        unlinked_search = (await client.get(
            f"{BASE}/links", params={"q": user.username, "linked_only": "false"},
        )).json()["links"]
        assert [row["username"] for row in unlinked_search] == [user.username]

    @pytest.mark.asyncio
    async def test_every_link_is_recorded(self, session, client):
        user = await self._account(session)
        await client.post(f"{BASE}/links", json={
            "identifier": user.username, "external_id": "logged-1"})
        await client.post(f"{BASE}/links/unlink", json={"identifier": user.username})

        rows = (await session.execute(select(IdentityProvisionLogModel).where(
            IdentityProvisionLogModel.username == user.username))).scalars().all()
        assert {r.action for r in rows} == {"link", "unlink"}
        assert {r.source for r in rows} == {"admin_link"}

    @pytest.mark.asyncio
    async def test_linking_needs_the_write_privilege(self, session, anon_client):
        assert (await anon_client.get(f"{BASE}/links")).status_code in (401, 403)
        assert (await anon_client.post(f"{BASE}/links", json={
            "identifier": "x", "external_id": "y"})).status_code in (401, 403)
        assert (await anon_client.post(f"{BASE}/links/unlink", json={
            "identifier": "x"})).status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_the_oidc_pair_can_be_linked_and_must_be_complete(
        self, session, client,
    ):
        """OIDC never reads `external_id` — it matches `(provider, subject)`."""
        user = await self._account(session)
        assert (await client.post(f"{BASE}/links", json={
            "identifier": user.email,
            "sso_provider": "google", "sso_subject": "sub-1"})).status_code == 200
        await session.refresh(user)
        assert (user.sso_provider, user.sso_subject) == ("google", "sub-1")
        assert user.external_id is None

        # A half-supplied pair is the dangerous shape: `(provider, NULL)` would
        # match every account that has never used that provider.
        assert (await client.post(f"{BASE}/links", json={
            "identifier": user.email, "sso_provider": "google"})).status_code == 400
        assert (await client.post(f"{BASE}/links", json={
            "identifier": user.email})).status_code == 400

    @pytest.mark.asyncio
    async def test_two_accounts_cannot_share_an_sso_identity(self, session, client):
        first = await self._account(session)
        second = await self._account(session)
        assert (await client.post(f"{BASE}/links", json={
            "identifier": first.username,
            "sso_provider": "google", "sso_subject": "shared-sub"})).status_code == 200
        clash = await client.post(f"{BASE}/links", json={
            "identifier": second.username,
            "sso_provider": "google", "sso_subject": "shared-sub"})
        assert clash.status_code == 409 and first.username in clash.json()["detail"]

    @pytest.mark.asyncio
    async def test_unlinking_clears_both_bindings(self, session, client):
        """Clearing one and leaving the other open reads as done and is not."""
        user = await self._account(session)
        await client.post(f"{BASE}/links", json={
            "identifier": user.username, "external_id": "ext-9",
            "sso_provider": "google", "sso_subject": "sub-9"})
        await client.post(f"{BASE}/links/unlink", json={"identifier": user.username})
        await session.refresh(user)
        assert (user.external_id, user.sso_provider, user.sso_subject) == (None, None, None)

    @pytest.mark.asyncio
    async def test_an_oidc_only_account_counts_as_linked(self, session, client):
        """A customer on OIDC has no `external_id` at all; the screen must not
        call those people unlinked."""
        user = await self._account(session)
        await client.post(f"{BASE}/links", json={
            "identifier": user.username,
            "sso_provider": "google", "sso_subject": "sub-listed"})
        rows = (await client.get(f"{BASE}/links", params={"q": user.username})).json()["links"]
        assert [r["sso_subject"] for r in rows] == ["sub-listed"]
