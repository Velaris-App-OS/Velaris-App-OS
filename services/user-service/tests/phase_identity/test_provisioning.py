"""Enterprise identity P0 — the provisioning spine.

The defect this module exists to make unrepresentable is *divergence*: before
it, `sso_callback` wrote `velaris_users` and `/user-directory/bulk-sync` wrote
`user_directory`, so one person had two `is_active` flags and no code path kept
them equal. `update_entry` still carries the one-way patch written when that
started hurting.

The tests below pin the properties that make the spine a single writer rather
than a fifth one: both records move together, an external directory cannot
reach a superadmin or grant itself authorization, and deactivation actually
ends the sessions it claims to end.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from velaris_shared.db.models import (
    AccessGroupModel,
    AuthDeviceModel,
    HelixUserModel,
    IdentityGroupMappingModel,
    IdentityProvisionLogModel,
    OperatorAccessGroupModel,
    PortalModel,
    RefreshTokenModel,
    TenantMembershipModel,
    TenantModel,
    UserDirectoryModel,
)
from user_service.identity import provisioning
from velaris_shared.tenancy import identity as tenant_identity


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def _tenant(session) -> TenantModel:
    t = TenantModel(slug=_uniq("acme"), name="Acme")
    session.add(t)
    await session.commit()
    # The resolver caches negative lookups too, so a tenant created mid-test is
    # invisible until the cache is dropped.
    tenant_identity.clear_cache()
    return t


async def _access_group(session, tenant_token: str) -> AccessGroupModel:
    portal = PortalModel(name=_uniq("P"), portal_type="staff", modules=[],
                         homepage="/", tenant_id=tenant_token)
    session.add(portal)
    await session.flush()
    group = AccessGroupModel(name=_uniq("G"), portal_id=portal.id, role_ids=[],
                             is_active=True, tenant_id=tenant_token)
    session.add(group)
    await session.commit()
    return group


async def _mapping(session, *, tenant_id, source, external_group, group_id):
    session.add(IdentityGroupMappingModel(
        tenant_id=tenant_id, source=source,
        external_group=external_group, access_group_id=group_id,
    ))
    await session.commit()


async def _directory(session, username: str) -> UserDirectoryModel | None:
    return (await session.execute(
        select(UserDirectoryModel).where(UserDirectoryModel.user_id == username)
    )).scalar_one_or_none()


async def _login(session, username: str) -> HelixUserModel | None:
    return (await session.execute(
        select(HelixUserModel).where(HelixUserModel.username == username)
    )).scalar_one_or_none()


class TestBothRecordsMoveTogether:
    """The invariant. `is_active` has one value per person, not two."""

    @pytest.mark.asyncio
    async def test_an_upsert_writes_the_login_record_and_the_profile_record(self, session):
        name = _uniq("joiner")
        res = await provisioning.upsert_principal(
            session, source="scim", external_id=_uniq("ext"),
            username=name, email=f"{name}@acme.com", display_name="A Joiner",
        )
        await session.commit()

        assert res.ok and res.action == "create"
        assert await _login(session, name) is not None
        assert await _directory(session, name) is not None

    @pytest.mark.asyncio
    async def test_deactivation_moves_both_records(self, session):
        name = _uniq("leaver")
        await provisioning.upsert_principal(
            session, source="scim", username=name, email=f"{name}@acme.com",
        )
        await session.commit()

        res = await provisioning.deactivate_principal(
            session, source="scim", username=name,
        )
        await session.commit()

        assert res.ok and res.action == "deactivate"
        assert (await _login(session, name)).is_active is False
        assert (await _directory(session, name)).is_active is False

    @pytest.mark.asyncio
    async def test_reactivation_moves_both_records(self, session):
        """A returning joiner is not half-enabled."""
        name = _uniq("rejoiner")
        await provisioning.upsert_principal(
            session, source="scim", username=name, email=f"{name}@acme.com",
        )
        await provisioning.deactivate_principal(session, source="scim", username=name)
        await session.commit()

        await provisioning.upsert_principal(
            session, source="scim", username=name, email=f"{name}@acme.com", active=True,
        )
        await session.commit()

        assert (await _login(session, name)).is_active is True
        assert (await _directory(session, name)).is_active is True

    @pytest.mark.asyncio
    async def test_active_none_does_not_reactivate_a_disabled_account(self, session):
        """The JIT-login case, and it was a live defect before it was a test.

        SAML passes `active=None` because a successful authentication says the
        person still exists at the IdP — not that an administrator's decision to
        disable them here should be silently undone. With `active=True` every
        SAML login reactivated the account, and the handler's own `is_active`
        check then never fired because provisioning had just cleared it.
        """
        name = _uniq("disabled")
        await provisioning.upsert_principal(
            session, source="saml_jit", username=name, email=f"{name}@acme.com",
        )
        await provisioning.deactivate_principal(session, source="scim", username=name)
        await session.commit()

        await provisioning.upsert_principal(
            session, source="saml_jit", username=name, email=f"{name}@acme.com",
            active=None,
        )
        await session.commit()

        assert (await _login(session, name)).is_active is False
        assert (await _directory(session, name)).is_active is False

    @pytest.mark.asyncio
    async def test_active_none_still_creates_a_new_account_active(self, session):
        """"Leave it alone" can only mean active when nothing was decided yet."""
        name = _uniq("fresh")
        await provisioning.upsert_principal(
            session, source="saml_jit", username=name, email=f"{name}@acme.com",
            active=None,
        )
        await session.commit()
        assert (await _login(session, name)).is_active is True


class TestSuperadminIsOutOfReach:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("source", ["scim", "saml_jit", "bulk_sync"])
    async def test_no_source_may_deactivate_a_superadmin(self, session, source):
        name = _uniq("root")
        session.add(HelixUserModel(
            username=name, email=f"{name}@acme.com", roles=["admin"],
            is_superadmin=True, is_active=True,
        ))
        await session.commit()

        res = await provisioning.deactivate_principal(
            session, source=source, username=name,
        )
        await session.commit()

        assert res.reason == "superadmin_protected"
        assert (await _login(session, name)).is_active is True

    @pytest.mark.asyncio
    async def test_no_source_may_edit_a_superadmin(self, session):
        name = _uniq("root")
        session.add(HelixUserModel(
            username=name, email=f"{name}@acme.com", roles=["admin"],
            is_superadmin=True, is_active=True, display_name="Real",
        ))
        await session.commit()

        res = await provisioning.upsert_principal(
            session, source="scim", username=name, email=f"{name}@acme.com",
            display_name="Impostor",
        )
        await session.commit()

        assert res.reason == "superadmin_protected"
        assert (await _login(session, name)).display_name == "Real"

    @pytest.mark.asyncio
    async def test_superadmin_cannot_be_granted_to_a_new_account(self, session):
        """`roles` arrives from outside; `superadmin` is filtered, not trusted."""
        name = _uniq("climber")
        await provisioning.upsert_principal(
            session, source="scim", username=name, email=f"{name}@acme.com",
            roles=["superadmin", "admin"],
        )
        await session.commit()

        user = await _login(session, name)
        assert "superadmin" not in (user.roles or [])
        assert user.is_superadmin is False


class TestExternalRolesDoNotOverwriteLocalOnes:
    @pytest.mark.asyncio
    async def test_an_update_leaves_platform_roles_alone(self, session):
        """A local revocation must survive the next sync.

        Authorization from a directory arrives as group membership, which an
        administrator maps here. If `roles` were rewritten on every upsert, the
        IdP would route around the mapping table entirely.
        """
        name = _uniq("mover")
        await provisioning.upsert_principal(
            session, source="scim", username=name, email=f"{name}@acme.com",
            roles=["viewer"],
        )
        await session.commit()

        user = await _login(session, name)
        user.roles = ["admin"]
        await session.commit()

        await provisioning.upsert_principal(
            session, source="scim", username=name, email=f"{name}@acme.com",
            roles=["viewer"],
        )
        await session.commit()

        assert (await _login(session, name)).roles == ["admin"]


class TestLookupPrecedence:
    @pytest.mark.asyncio
    async def test_external_id_wins_over_a_matching_username(self, session):
        """The stable key decides, so a rename at either end updates one account.

        Matching the three identifiers in a single OR would let the database's
        row order pick which account an external system rewrites.
        """
        ext = _uniq("ext")
        held = _uniq("held")
        decoy = _uniq("decoy")
        session.add(HelixUserModel(
            username=held, email=f"{held}@acme.com", roles=[], external_id=ext,
        ))
        session.add(HelixUserModel(username=decoy, email=f"{decoy}@acme.com", roles=[]))
        await session.commit()

        res = await provisioning.upsert_principal(
            session, source="scim", external_id=ext, username=decoy,
            display_name="Renamed",
        )
        await session.commit()

        assert res.username == held
        assert (await _login(session, decoy)).display_name != "Renamed"

    @pytest.mark.asyncio
    async def test_username_wins_over_a_matching_email(self, session):
        """An address reissued to a joiner must not update the leaver's account.

        The refusal is what proves the order: the call names `target` by username
        and carries an address `other` holds. Had the email matched first,
        `other` would have been the subject and there would have been no clash to
        report — the collision only exists because the username won.
        """
        target = _uniq("target")
        other = _uniq("other")
        session.add(HelixUserModel(username=target, email=f"{target}@acme.com", roles=[]))
        session.add(HelixUserModel(username=other, email=f"{other}@acme.com", roles=[]))
        await session.commit()

        res = await provisioning.upsert_principal(
            session, source="scim", username=target, email=f"{other}@acme.com",
        )
        await session.commit()
        assert res.reason == "email_taken"
        assert res.username == target

    @pytest.mark.asyncio
    async def test_email_matching_is_case_insensitive(self, session):
        """Otherwise one person forks into two accounts on a capitalisation."""
        name = _uniq("case")
        session.add(HelixUserModel(username=name, email=f"{name}@acme.com", roles=[]))
        await session.commit()

        res = await provisioning.upsert_principal(
            session, source="scim", email=f"{name}@ACME.com".upper(),
        )
        await session.commit()
        assert res.username == name


class TestGroupMapping:
    @pytest.mark.asyncio
    async def test_a_mapped_group_grants_membership(self, session):
        tenant = await _tenant(session)
        group = await _access_group(session, tenant.slug)
        await _mapping(session, tenant_id=tenant.id, source="scim",
                       external_group="Claims-Team", group_id=group.id)

        name = _uniq("member")
        res = await provisioning.upsert_principal(
            session, source="scim", username=name, email=f"{name}@acme.com",
            tenant_token=tenant.slug, group_names=["Claims-Team"],
        )
        await session.commit()

        assert res.groups_added == [str(group.id)]
        rows = (await session.execute(
            select(OperatorAccessGroupModel).where(
                OperatorAccessGroupModel.operator_id == name
            )
        )).scalars().all()
        assert [r.access_group_id for r in rows] == [group.id]

    @pytest.mark.asyncio
    async def test_an_unmapped_group_grants_nothing_and_is_recorded(self, session):
        """No name fallback. The IdP admin owns those strings; if a matching
        access-group name were enough, they could mint authorization here by
        naming a directory group after one of ours."""
        tenant = await _tenant(session)
        group = await _access_group(session, tenant.slug)

        name = _uniq("stranger")
        res = await provisioning.upsert_principal(
            session, source="scim", username=name, email=f"{name}@acme.com",
            tenant_token=tenant.slug, group_names=[group.name],
        )
        await session.commit()

        assert res.groups_added == []
        assert res.unmapped_groups == [group.name]
        rows = (await session.execute(
            select(OperatorAccessGroupModel).where(
                OperatorAccessGroupModel.operator_id == name
            )
        )).scalars().all()
        assert rows == []

    @pytest.mark.asyncio
    async def test_a_locally_assigned_group_is_never_stripped(self, session):
        """The IdP governs only the groups it can map to.

        Without this, the first sync of a user would silently revoke every group
        an administrator had granted by hand inside Velaris.
        """
        tenant = await _tenant(session)
        governed = await _access_group(session, tenant.slug)
        local = await _access_group(session, tenant.slug)
        await _mapping(session, tenant_id=tenant.id, source="scim",
                       external_group="Claims-Team", group_id=governed.id)

        name = _uniq("hybrid")
        session.add(OperatorAccessGroupModel(
            operator_id=name, access_group_id=local.id, is_primary=True,
        ))
        await session.commit()

        await provisioning.upsert_principal(
            session, source="scim", username=name, email=f"{name}@acme.com",
            tenant_token=tenant.slug, group_names=["Claims-Team"],
        )
        await session.commit()

        held = {r.access_group_id for r in (await session.execute(
            select(OperatorAccessGroupModel).where(
                OperatorAccessGroupModel.operator_id == name
            )
        )).scalars().all()}
        assert held == {local.id, governed.id}

    @pytest.mark.asyncio
    async def test_a_governed_group_is_removed_when_no_longer_asserted(self, session):
        tenant = await _tenant(session)
        group = await _access_group(session, tenant.slug)
        await _mapping(session, tenant_id=tenant.id, source="scim",
                       external_group="Claims-Team", group_id=group.id)

        name = _uniq("moved")
        await provisioning.upsert_principal(
            session, source="scim", username=name, email=f"{name}@acme.com",
            tenant_token=tenant.slug, group_names=["Claims-Team"],
        )
        await session.commit()

        res = await provisioning.upsert_principal(
            session, source="scim", username=name, email=f"{name}@acme.com",
            tenant_token=tenant.slug, group_names=[],
        )
        await session.commit()

        assert res.groups_removed == [str(group.id)]

    @pytest.mark.asyncio
    async def test_a_mapping_for_one_source_is_not_honoured_for_another(self, session):
        tenant = await _tenant(session)
        group = await _access_group(session, tenant.slug)
        await _mapping(session, tenant_id=tenant.id, source="scim",
                       external_group="Claims-Team", group_id=group.id)

        name = _uniq("crosser")
        res = await provisioning.upsert_principal(
            session, source="saml_jit", username=name, email=f"{name}@acme.com",
            tenant_token=tenant.slug, group_names=["Claims-Team"],
        )
        await session.commit()
        assert res.groups_added == []
        assert res.unmapped_groups == ["Claims-Team"]


class TestTenant:
    @pytest.mark.asyncio
    async def test_a_supplied_tenant_must_resolve(self, session):
        res = await provisioning.upsert_principal(
            session, source="scim", username=_uniq("ghost"),
            email="ghost@acme.com", tenant_token="no-such-tenant",
        )
        await session.commit()
        assert res.reason == "unknown_tenant"

    @pytest.mark.asyncio
    async def test_an_absent_tenant_is_not_an_error(self, session):
        """Single-tenant deployments provision tenantless principals and always
        have. `resolve_tenant(None)` falls back to the "default" slug, which
        need not exist — so the absent case must never reach it."""
        name = _uniq("tenantless")
        res = await provisioning.upsert_principal(
            session, source="scim", username=name, email=f"{name}@acme.com",
        )
        await session.commit()
        assert res.ok

    @pytest.mark.asyncio
    async def test_membership_is_created_and_is_idempotent(self, session):
        tenant = await _tenant(session)
        name = _uniq("member")
        for _ in range(2):
            await provisioning.upsert_principal(
                session, source="scim", username=name, email=f"{name}@acme.com",
                tenant_token=tenant.slug,
            )
            await session.commit()

        rows = (await session.execute(
            select(TenantMembershipModel).where(
                TenantMembershipModel.tenant_id == tenant.id,
                TenantMembershipModel.user_id == name,
            )
        )).scalars().all()
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_a_tenant_uuid_resolves_as_well_as_a_slug(self, session):
        tenant = await _tenant(session)
        name = _uniq("byuuid")
        res = await provisioning.upsert_principal(
            session, source="scim", username=name, email=f"{name}@acme.com",
            tenant_token=str(tenant.id),
        )
        await session.commit()
        assert res.ok


class TestDeactivationEndsSessions:
    @pytest.mark.asyncio
    async def test_refresh_tokens_and_devices_are_revoked(self, session):
        """A live refresh chain keeps minting access tokens for as long as it is
        rotated, so a leaver whose tokens survive keeps working sessions."""
        name = _uniq("session")
        await provisioning.upsert_principal(
            session, source="scim", username=name, email=f"{name}@acme.com",
        )
        await session.commit()
        user = await _login(session, name)

        # Both tables key on the UUID, unlike the directory tables which key on
        # the username — revoking by the wrong one silently revokes nothing.
        device = AuthDeviceModel(
            user_id=str(user.id), device_name="laptop",
            user_agent_hash="x" * 64,
        )
        session.add(device)
        await session.flush()
        session.add(RefreshTokenModel(
            token_hash="a" * 64, user_id=str(user.id), jti=str(uuid.uuid4()),
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
            device_id=device.id,
        ))
        await session.commit()

        res = await provisioning.deactivate_principal(
            session, source="scim", username=name,
        )
        await session.commit()

        assert res.detail["revoked_refresh_tokens"] == 1
        assert res.detail["revoked_devices"] == 1
        token = (await session.execute(
            select(RefreshTokenModel).where(RefreshTokenModel.user_id == str(user.id))
        )).scalar_one()
        assert token.revoked_at is not None

    @pytest.mark.asyncio
    async def test_deactivation_is_never_a_delete(self, session):
        """Cases reference their creator by user id; deleting the row orphans
        every audit trail pointing at it."""
        name = _uniq("kept")
        await provisioning.upsert_principal(
            session, source="scim", username=name, email=f"{name}@acme.com",
        )
        await session.commit()
        await provisioning.deactivate_principal(session, source="scim", username=name)
        await session.commit()

        assert await _login(session, name) is not None
        assert await _directory(session, name) is not None


class TestAudit:
    @pytest.mark.asyncio
    async def test_every_decision_is_logged_including_refusals(self, session):
        name = _uniq("audited")
        await provisioning.upsert_principal(
            session, source="scim", username=name, email=f"{name}@acme.com",
        )
        await provisioning.deactivate_principal(session, source="scim", username=name)
        await provisioning.deactivate_principal(session, source="scim", username=_uniq("nobody"))
        await session.commit()

        rows = (await session.execute(
            select(IdentityProvisionLogModel).where(
                IdentityProvisionLogModel.source == "scim"
            )
        )).scalars().all()
        actions = {r.action for r in rows}
        assert {"create", "deactivate", "rejected"} <= actions
        rejected = [r for r in rows if r.action == "rejected"]
        assert all(r.result == "error" and r.detail.get("reason") for r in rejected)

    @pytest.mark.asyncio
    async def test_unmapped_groups_are_recorded_not_dropped(self, session):
        tenant = await _tenant(session)
        name = _uniq("unmapped")
        await provisioning.upsert_principal(
            session, source="scim", username=name, email=f"{name}@acme.com",
            tenant_token=tenant.slug, group_names=["Ghost-Team"],
        )
        await session.commit()

        row = (await session.execute(
            select(IdentityProvisionLogModel).where(
                IdentityProvisionLogModel.username == name
            )
        )).scalars().first()
        assert row.detail["unmapped_groups"] == ["Ghost-Team"]


class TestBulkSyncCompatibility:
    """`bulk-sync` predates this module and its shipped behaviour is preserved."""

    @pytest.mark.asyncio
    async def test_an_entry_with_no_email_still_records_a_person(self, session):
        """A manager named only as an escalation target has no login account."""
        name = _uniq("portal:someone@example.com")
        res = await provisioning.upsert_principal(
            session, source="bulk_sync", username=name,
            display_name="Someone", allow_directory_only=True,
        )
        await session.commit()

        assert res.ok and res.detail["directory_only"] is True
        assert await _directory(session, name) is not None
        assert await _login(session, name) is None

    @pytest.mark.asyncio
    async def test_a_directory_only_person_can_be_deactivated(self, session):
        name = _uniq("portal:leaver@example.com")
        await provisioning.upsert_principal(
            session, source="bulk_sync", username=name, allow_directory_only=True,
        )
        await session.commit()

        res = await provisioning.deactivate_principal(
            session, source="bulk_sync", username=name, allow_directory_only=True,
        )
        await session.commit()

        assert res.ok
        assert (await _directory(session, name)).is_active is False

    @pytest.mark.asyncio
    async def test_bulk_sync_now_reaches_the_login_record(self, session, client):
        """The behaviour change: this endpoint used to write only the directory,
        so a leaver disabled through it kept their login."""
        name = _uniq("api")
        session.add(HelixUserModel(
            username=name, email=f"{name}@acme.com", roles=["viewer"], is_active=True,
        ))
        await session.commit()

        resp = await client.post("/api/v1/user-directory/bulk-sync", json=[{
            "user_id": name, "email": f"{name}@acme.com", "is_active": False,
        }])
        assert resp.status_code == 200, resp.text
        assert resp.json()["deactivated"] == 1

        await session.commit()
        assert (await _login(session, name)).is_active is False
