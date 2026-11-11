"""Canonical tenant identity — one answer for both platform conventions.

`case_instances.tenant_id` is a GUID FK; `case_sessions.tenant_id` and every
settings lookup use a slug; and `user_directory.tenant_id` is a bare String(64)
that may hold EITHER. These tests pin that a caller resolves correctly whichever
form their directory row carries, and that an unresolvable token denies rather
than matching everything.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest

from velaris_shared.db.models import TenantModel
from case_service.tenancy import identity
from tests.conftest import TestSessionFactory

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clear_cache():
    identity.clear_cache()
    yield
    identity.clear_cache()


async def _tenant(db, slug="acme") -> TenantModel:
    row = TenantModel(slug=slug, name=slug.title())
    db.add(row)
    await db.commit()
    return row


class TestResolve:
    async def test_resolves_a_slug(self):
        async with TestSessionFactory() as db:
            row = await _tenant(db)
            assert await identity.resolve_tenant(db, "acme") == (row.id, "acme")

    async def test_resolves_a_uuid_string(self):
        async with TestSessionFactory() as db:
            row = await _tenant(db)
            assert await identity.resolve_tenant(db, str(row.id)) == (row.id, "acme")

    async def test_unknown_token_resolves_to_nothing(self):
        async with TestSessionFactory() as db:
            assert await identity.resolve_tenant(db, "no-such-tenant") is None
            assert await identity.resolve_tenant(db, str(uuid.uuid4())) is None

    async def test_slug_helper_returns_canonical_slug_for_both_forms(self):
        async with TestSessionFactory() as db:
            row = await _tenant(db)
            assert await identity.tenant_slug(db, "acme") == "acme"
            assert await identity.tenant_slug(db, str(row.id)) == "acme"

    async def test_unknown_token_keeps_its_own_slug(self):
        """Falls back to the token, so an unregistered tenant matches nothing
        rather than silently becoming 'default'."""
        async with TestSessionFactory() as db:
            assert await identity.tenant_slug(db, "ghost") == "ghost"


class TestSameTenant:
    async def test_null_tenant_row_is_shared(self):
        """Platform-shared rows predate multi-tenancy; several features rely
        on them staying visible."""
        async with TestSessionFactory() as db:
            assert await identity.same_tenant(db, None, "anyone") is True

    async def test_uuid_token_deployment(self):
        async with TestSessionFactory() as db:
            row = await _tenant(db)
            assert await identity.same_tenant(db, row.id, str(row.id)) is True

    async def test_slug_token_deployment(self):
        """The case that used to fail closed: a slug on the user, a GUID on
        the case."""
        async with TestSessionFactory() as db:
            row = await _tenant(db)
            assert await identity.same_tenant(db, row.id, "acme") is True

    async def test_other_tenant_is_denied_in_both_forms(self):
        async with TestSessionFactory() as db:
            mine = await _tenant(db, "acme")
            theirs = await _tenant(db, "globex")
            assert await identity.same_tenant(db, theirs.id, "acme") is False
            assert await identity.same_tenant(db, theirs.id, str(mine.id)) is False

    async def test_unresolvable_token_denies(self):
        """Fail closed: an unknown token must never read as 'any tenant'."""
        async with TestSessionFactory() as db:
            row = await _tenant(db)
            assert await identity.same_tenant(db, row.id, "ghost") is False
            assert await identity.same_tenant(db, row.id, None) is False

    async def test_tenantless_caller_maps_to_the_default_tenant(self):
        """A deliberate widening. The broken GUID compare denied a tenant-less
        caller everything; the slug world (case_sessions, messages) has always
        treated a missing token as the `default` tenant. This makes the two
        agree rather than leaving one of them wrong."""
        async with TestSessionFactory() as db:
            default = await _tenant(db, "default")
            other = await _tenant(db, "acme")
            assert await identity.same_tenant(db, default.id, None) is True
            assert await identity.same_tenant(db, other.id, None) is False

    async def test_cache_is_dropped_on_demand(self):
        async with TestSessionFactory() as db:
            assert await identity.resolve_tenant(db, "acme") is None   # cached miss
            row = await _tenant(db)
            identity.clear_cache()
            assert await identity.resolve_tenant(db, "acme") == (row.id, "acme")


class TestEnforcementDefault:
    async def test_case_gates_enforce_unless_told_otherwise(self):
        """Silence must not mean 'open' — in shadow every case gate is
        advisory, including meet.recording.view."""
        from velaris_shared.config import Settings
        assert Settings.model_fields["hxguard_case_enforcement"].default == "enforce"


class TestActorIdentity:
    """The same two-conventions problem, one column over (Sep item 39).

    Fifteen `GUID` columns record *who did this*, and `user.user_id` is a UUID
    only for a password login — an SSO or service principal carries a username
    there. Passing it through raised *"...is not a valid id"*, so `POST /agents`
    and `PUT /costing/model-rates` answered 422 to a request that was correct in
    every respect; HxArbiter, which already met this, stored NULL and lost the
    author instead.
    """

    @staticmethod
    async def _user(db, username="jo.smith", email="jo.smith@example.test"):
        from velaris_shared.db.models import HelixUserModel
        row = HelixUserModel(username=username, email=email)
        db.add(row)
        await db.commit()
        return row

    class _Caller:
        def __init__(self, user_id=None, username=None, email=None):
            self.user_id, self.username, self.email = user_id, username, email

    @pytest.fixture(autouse=True)
    def _clear_actor_cache(self):
        identity.clear_actor_cache()
        yield
        identity.clear_actor_cache()

    async def test_a_uuid_is_returned_untouched(self):
        """The load-bearing compatibility claim: an install that works today
        keeps storing exactly the value it stores today. No query, no lookup —
        not even a check that the user row exists, because the call sites did
        not make one before this function existed."""
        async with TestSessionFactory() as db:
            known = uuid.uuid4()
            caller = self._Caller(user_id=str(known))
            assert await identity.actor_uuid(db, caller) == known

    async def test_a_username_resolves_to_the_user_row(self):
        async with TestSessionFactory() as db:
            row = await self._user(db)
            caller = self._Caller(user_id="jo.smith")
            assert await identity.actor_uuid(db, caller) == row.id

    async def test_an_email_resolves_to_the_user_row(self):
        async with TestSessionFactory() as db:
            row = await self._user(db)
            caller = self._Caller(user_id="jo.smith@example.test")
            assert await identity.actor_uuid(db, caller) == row.id

    async def test_username_on_the_user_object_is_tried_too(self):
        """An SSO login puts an opaque subject in `user_id` and the real name in
        `username`; both are legitimate places for the identifier to arrive."""
        async with TestSessionFactory() as db:
            row = await self._user(db)
            caller = self._Caller(user_id="sso|opaque-subject", username="jo.smith")
            assert await identity.actor_uuid(db, caller) == row.id

    async def test_a_principal_with_no_user_row_is_absent_not_refused(self):
        """The degrade, and it is deliberate. Every one of these columns is
        nullable and already means "not recorded"; a made-up id on an audit row
        is worse than an empty one. What must NOT happen is the 422."""
        async with TestSessionFactory() as db:
            await self._user(db)
            assert await identity.actor_uuid(
                db, self._Caller(user_id="intake_webhook")) is None
            assert await identity.actor_uuid(db, self._Caller()) is None

    async def test_identifiers_are_tried_in_precedence_not_merged(self):
        """Merging them into one `IN (...)` hands one person's row to another
        the moment a username equals somebody else's email. `user_id` wins."""
        async with TestSessionFactory() as db:
            mine = await self._user(db, "alex", "alex@example.test")
            theirs = await self._user(db, "alex@example.test", "other@example.test")
            caller = self._Caller(user_id="alex", email="alex@example.test")
            assert await identity.actor_uuid(db, caller) == mine.id
            assert mine.id != theirs.id

    async def test_cache_is_dropped_on_demand(self):
        async with TestSessionFactory() as db:
            caller = self._Caller(user_id="jo.smith")
            assert await identity.actor_uuid(db, caller) is None   # cached miss
            row = await self._user(db)
            identity.clear_actor_cache()
            assert await identity.actor_uuid(db, caller) == row.id


async def test_a_tenant_created_after_a_miss_is_found_at_once():
    """Live run 2026-11-10: case-service looked a trial up just before
    user-service created it; the cached "no such tenant" made the new trial's
    case list answer 403 for up to a minute. Only found tenants are cached."""
    import uuid as _uuid
    from velaris_shared.db.models import TenantModel
    from velaris_shared.tenancy import identity
    from tests.conftest import TestSessionFactory
    slug = f"late-{_uuid.uuid4().hex[:6]}"
    identity.clear_cache()
    async with TestSessionFactory() as db:
        assert await identity.resolve_tenant(db, slug) is None
    async with TestSessionFactory() as db:      # another service creates it
        db.add(TenantModel(id=_uuid.uuid4(), slug=slug, name="Late"))
        await db.commit()
    async with TestSessionFactory() as db:
        hit = await identity.resolve_tenant(db, slug)
    assert hit is not None and hit[1] == slug
