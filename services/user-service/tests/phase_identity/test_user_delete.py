"""Deleting a user (Nov_TODO "User deletion v2", 2026-10-23): refused and signed
out at once, recoverable for a grace period, then purged — nothing left that
lets them, or someone given their name later, back in. History stays.

Found live before this: a deleted user's token still answered 200, and their
access-group memberships (keyed by username) stayed behind for the next account
with that name. These run the REAL account check (`account_gate`) with real
accounts signed in through the real login: an ordinary admin deletes (the least
privilege that may), a viewer is refused.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, insert, select

from velaris_shared.auth.passwords import hash_password
from velaris_shared.db.models import HelixUserModel

pytestmark = pytest.mark.account_gate

PW = "a-long-test-password-1"
USERS = "/api/v1/auth/real/users"
ME = "/api/v1/auth/real/me/profile"


def _md():
    from velaris_shared.db import models as M
    for v in vars(M).values():
        if hasattr(v, "metadata") and hasattr(v, "__tablename__"):
            return v.metadata


async def _user(session, username, roles, *, tenant=None):
    u = HelixUserModel(username=username, email=f"{username}@example.com", roles=roles,
                       is_active=True, password_hash=hash_password(PW))
    session.add(u)
    await session.commit()
    if tenant:
        now = datetime.now(timezone.utc)
        await session.execute(insert(_md().tables["user_directory"]).values(
            id=uuid.uuid4(), user_id=username, tenant_id=tenant, is_active=True,
            created_at=now, updated_at=now))
        await session.commit()
    return u


async def _login(client, username) -> dict:
    r = await client.post("/api/v1/auth/real/login", json={"username": username, "password": PW})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _count(session, table, column, values) -> int:
    t = _md().tables[table]
    return (await session.execute(select(func.count()).select_from(t)
                                  .where(t.c[column].in_(values)))).scalar()


def _names(u):
    return [str(u.id), u.username, u.email]


async def _seed(session, bob) -> None:
    """What a working account accumulates: memberships, devices, open work, and
    history that must survive."""
    t = _md().tables
    now = datetime.now(timezone.utc)
    sid, name = str(bob.id), bob.username
    # Real parents, so this also runs on PostgreSQL (foreign keys enforced —
    # VELARIS_TEST_DATABASE_URL): SQLite hid a UUID-column bug here once.
    tenant_id, portal_id, group_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    case_type_id, case_id = uuid.uuid4(), uuid.uuid4()
    await session.execute(insert(t["tenants"]).values(id=tenant_id, slug=f"t-{tenant_id.hex[:8]}", name="T"))
    await session.execute(insert(t["portals"]).values(id=portal_id, name="P"))
    await session.execute(insert(t["access_groups"]).values(
        id=group_id, name="G", tenant_id="default", portal_id=portal_id))
    await session.execute(insert(t["case_types"]).values(
        id=case_type_id, name="CT", version="1", definition_json={}))
    await session.execute(insert(t["case_instances"]).values(
        id=case_id, case_type_id=case_type_id, case_type_version="1"))
    await session.execute(insert(t["operator_access_groups"]).values(
        id=uuid.uuid4(), operator_id=name, access_group_id=group_id, is_primary=True, assigned_at=now))
    await session.execute(insert(t["tenant_memberships"]).values(
        id=uuid.uuid4(), tenant_id=tenant_id, user_id=name, role="member", created_at=now))
    await session.execute(insert(t["push_device_tokens"]).values(
        id=uuid.uuid4(), user_id=sid, channel="fcm", token="tok", is_active=True, created_at=now))
    await session.execute(insert(t["hxguard_tuples"]).values(
        id=uuid.uuid4(), object_type="case", object_id=str(uuid.uuid4()), relation="assignee",
        subject_type="user", subject_id=sid, created_by="x", created_at=now))
    await session.execute(insert(t["user_directory"]).values(
        id=uuid.uuid4(), user_id=name, tenant_id="default", is_active=True, created_at=now, updated_at=now))
    await session.execute(insert(t["case_assignments"]).values(
        id=uuid.uuid4(), case_id=case_id, step_id="review", assignee_type="user", assignee_id=sid,
        status="active", assigned_at=now))
    await session.execute(insert(t["case_audit_log"]).values(
        id=uuid.uuid4(), case_id=case_id, action="note_added", actor_id=sid, timestamp=now))
    await session.execute(insert(t["user_absences"]).values(
        id=uuid.uuid4(), tenant_id="default", user_id="someone.else", kind="vacation",
        starts_at=now, ends_at=now + timedelta(days=1), delegate_user_id=name,
        created_by="someone.else", created_at=now))
    await session.commit()


async def _two_admins(session):
    await _user(session, "del.admin", ["admin"])          # least privilege that may delete
    await _user(session, "other.admin", ["admin"])        # so del.admin is not the last one


async def test_delete_signs_out_at_once_reserves_the_name_and_undo_brings_it_back(anon_client, session):
    await _two_admins(session)
    bob = await _user(session, "bob", ["case_worker"])
    admin_h, bob_h = await _login(anon_client, "del.admin"), await _login(anon_client, "bob")
    await _seed(session, bob)

    assert (await anon_client.delete(f"{USERS}/{bob.id}", headers=admin_h)).status_code == 204
    # Refused at once — the token he holds, his password.
    r = await anon_client.get(ME, headers=bob_h)
    assert r.status_code == 401 and "no longer exists" in r.text
    r = await anon_client.post("/api/v1/auth/real/login", json={"username": "bob", "password": PW})
    assert r.status_code == 401
    # Sessions and credentials gone; open work back to the queue; history kept.
    assert await _count(session, "refresh_tokens", "user_id", _names(bob)) == 0
    assert await _count(session, "push_device_tokens", "user_id", _names(bob)) == 0
    t = _md().tables
    a = (await session.execute(select(t["case_assignments"]))).one()
    assert a.assignee_type == "queue" and a.claimed_at is None
    assert await _count(session, "case_audit_log", "actor_id", [str(bob.id)]) == 1
    # The row stays — the username RESERVED, so its access cannot be inherited.
    session.expunge_all()
    row = await session.get(HelixUserModel, bob.id)
    assert row.deleted_at is not None
    purge_after = row.purge_after if row.purge_after.tzinfo else row.purge_after.replace(tzinfo=timezone.utc)
    assert purge_after > datetime.now(timezone.utc) + timedelta(days=29)
    assert await _count(session, "operator_access_groups", "operator_id", ["bob"]) == 1
    d = (await session.execute(select(t["user_directory"]).where(t["user_directory"].c.user_id == "bob"))).one()
    assert d.is_active is False                                       # hidden from pickers
    r = await anon_client.post("/api/v1/auth/real/register", headers=admin_h, json={
        "username": "bob", "email": "bob2@example.com", "password": PW, "roles": ["viewer"]})
    assert r.status_code in (400, 409), r.text                        # nobody takes the name
    r = await anon_client.patch(f"{USERS}/{bob.id}", headers=admin_h, json={"roles": ["admin"]})
    assert r.status_code == 409                                       # no edits, only an undo
    listed = {u["username"]: u for u in (await anon_client.get(USERS, headers=admin_h)).json()["users"]}
    assert listed["bob"]["deleted_at"] and listed["bob"]["purge_after"]

    # Undo: back as it was. The sessions the delete ended stay ended.
    r = await anon_client.post(f"{USERS}/{bob.id}/restore", headers=admin_h)
    assert r.status_code == 200 and r.json()["deleted_at"] is None
    r = await anon_client.get(ME, headers=bob_h)
    assert r.status_code == 401 and "ended" in r.text
    session.expunge_all()
    d = (await session.execute(select(t["user_directory"]).where(t["user_directory"].c.user_id == "bob"))).one()
    assert d.is_active is True
    # Signing in again right after the undo works at once — no wait for the
    # next whole second (clean box 2026-10-23: the new token was refused).
    assert (await anon_client.get(ME, headers=await _login(anon_client, "bob"))).status_code == 200
    # A token with whole seconds only (no `iat_ms`) from the delete's own
    # second stays refused: the cutoff rounds up for it.
    from velaris_shared.auth.dependencies import _issued_at
    import base64, json as _json
    def _tok(claims):
        b = lambda d: base64.urlsafe_b64encode(_json.dumps(d).encode()).decode().rstrip("=")
        return f"{b({'alg': 'none'})}.{b(claims)}.x"
    cut = row.tokens_valid_after.replace(tzinfo=row.tokens_valid_after.tzinfo or timezone.utc).timestamp()
    assert _issued_at(_tok({"iat": int(cut)})) == (int(cut), False)
    assert _issued_at(_tok({"iat": int(cut), "iat_ms": int(cut * 1000) + 5})) == ((int(cut * 1000) + 5) / 1000, True)
    ev = {e.event_type for e in (await session.execute(select(t["security_events"]))).all()}
    assert {"user_deleted", "user_delete_undone"} <= ev


async def test_the_purge_removes_access_and_the_free_name_inherits_nothing(anon_client, session):
    from velaris_shared.identity import lifecycle
    await _two_admins(session)
    bob = await _user(session, "bob", ["case_worker"])
    admin_h, bob_h = await _login(anon_client, "del.admin"), await _login(anon_client, "bob")
    await _seed(session, bob)
    assert (await anon_client.delete(f"{USERS}/{bob.id}", headers=admin_h)).status_code == 204

    assert await lifecycle.purge_due(session) == []                    # not due yet
    later = datetime.now(timezone.utc) + timedelta(days=31)
    assert await lifecycle.purge_due(session, now=later) == [str(bob.id)]
    session.expunge_all()
    assert await session.get(HelixUserModel, bob.id) is None
    for table, column in (("operator_access_groups", "operator_id"), ("tenant_memberships", "user_id"),
                          ("hxguard_tuples", "subject_id"), ("user_directory", "user_id")):
        assert await _count(session, table, column, _names(bob)) == 0, table
    assert await _count(session, "user_absences", "delegate_user_id", _names(bob)) == 0
    assert await _count(session, "case_audit_log", "actor_id", [str(bob.id)]) == 1   # history kept

    bob2 = await _user(session, "bob", ["viewer"])
    assert bob2.id != bob.id
    assert await _count(session, "operator_access_groups", "operator_id", ["bob"]) == 0
    assert (await anon_client.get(ME, headers=bob_h)).status_code == 401              # old token: no
    r = await anon_client.get(ME, headers=await _login(anon_client, "bob"))
    assert r.status_code == 200 and r.json()["user_id"] == str(bob2.id)
    assert (await anon_client.post(f"{USERS}/{bob.id}/restore", headers=admin_h)).status_code == 404


async def test_permanent_now_skips_the_grace_period(anon_client, session):
    await _two_admins(session)
    eve = await _user(session, "eve", ["case_worker"])
    admin_h, eve_h = await _login(anon_client, "del.admin"), await _login(anon_client, "eve")
    assert (await anon_client.delete(f"{USERS}/{eve.id}?permanent=true", headers=admin_h)).status_code == 204
    session.expunge_all()
    assert await session.get(HelixUserModel, eve.id) is None
    assert (await anon_client.get(ME, headers=eve_h)).status_code == 401


async def test_a_viewer_cannot_delete_and_nothing_changes(anon_client, session):
    await _user(session, "del.viewer", ["viewer"])
    bob = await _user(session, "bob.kept", ["case_worker"])
    r = await anon_client.delete(f"{USERS}/{bob.id}", headers=await _login(anon_client, "del.viewer"))
    assert r.status_code == 403
    session.expunge_all()
    assert (await session.get(HelixUserModel, bob.id)).deleted_at is None
    assert (await anon_client.get(ME, headers=await _login(anon_client, "bob.kept"))).status_code == 200


async def test_self_delete_and_the_tenants_last_admin_are_refused(anon_client, session):
    from velaris_shared.identity import lifecycle
    a1 = await _user(session, "only.admin", ["admin"], tenant="t-acme")
    r = await anon_client.delete(f"{USERS}/{a1.id}", headers=await _login(anon_client, "only.admin"))
    assert r.status_code == 409 and "your own" in r.text

    class Operator:
        user_id, username, roles = str(uuid.uuid4()), "platform.op", ["superadmin"]
    with pytest.raises(lifecycle.DeleteRefused, match="last active admin"):
        await lifecycle.check_delete_allowed(session, Operator(), a1)
    await _user(session, "second.admin", ["admin"], tenant="t-acme")
    await lifecycle.check_delete_allowed(session, Operator(), a1)       # now another admin exists


async def test_bulk_deletes_alert_then_refuse(anon_client, session, monkeypatch):
    from velaris_shared.config import get_settings
    monkeypatch.setattr(get_settings(), "user_delete_bulk_alert", 1)
    monkeypatch.setattr(get_settings(), "user_delete_bulk_limit", 2)
    await _two_admins(session)
    h = await _login(anon_client, "del.admin")
    victims = [await _user(session, f"v{i}", ["viewer"]) for i in range(3)]
    assert (await anon_client.delete(f"{USERS}/{victims[0].id}", headers=h)).status_code == 204
    assert (await anon_client.delete(f"{USERS}/{victims[1].id}", headers=h)).status_code == 204
    r = await anon_client.delete(f"{USERS}/{victims[2].id}", headers=h)
    assert r.status_code == 409 and "Too many" in r.text
    t = _md().tables
    assert (await session.execute(select(func.count()).select_from(t["security_events"]).where(
        t["security_events"].c.event_type == "user_delete_bulk"))).scalar() >= 1


async def test_a_directory_cannot_bring_a_deleted_account_back(anon_client, session):
    """SCIM / SSO during the grace period: refused, never re-activated or re-created."""
    import inspect

    from user_service.identity import provisioning
    await _two_admins(session)
    sso = await _user(session, "sso.user", ["viewer"])
    sso.external_id = "ext-1"
    await session.commit()
    await anon_client.delete(f"{USERS}/{sso.id}", headers=await _login(anon_client, "del.admin"))
    session.expunge_all()
    params = inspect.signature(provisioning.upsert_principal).parameters
    kw = {"source": "scim", "username": "sso.user", "email": "sso.user@example.com",
          "external_id": "ext-1", "active": True, "actor": "test", "tenant_token": None}
    res = await provisioning.upsert_principal(session, **{k: v for k, v in kw.items() if k in params})
    assert not res.ok and res.reason == "account_deleted"
    session.expunge_all()
    assert (await session.get(HelixUserModel, sso.id)).deleted_at is not None


async def test_a_renamed_account_ends_its_old_sessions(anon_client, session):
    carol = await _user(session, "carol", ["viewer"])
    h = await _login(anon_client, "carol")
    carol.username = "carol.new"
    await session.commit()
    r = await anon_client.get(ME, headers=h)
    assert r.status_code == 401 and "renamed" in r.text


async def test_a_token_whose_subject_is_not_an_account_is_refused(anon_client, session):
    from velaris_shared.auth.jwt_handler import create_dev_token
    from velaris_shared.config import get_settings
    s = get_settings()
    for sub in ("alice", str(uuid.uuid4())):
        tok = create_dev_token(user_id=sub, username="alice", roles=["admin"], secret=s.auth_secret,
                               private_key=s.auth_rsa_private_key or "")
        r = await anon_client.get(ME, headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 401, (sub, r.text)


async def test_an_account_check_that_fails_refuses(anon_client, session, monkeypatch):
    dave = await _user(session, "dave", ["viewer"])
    h = await _login(anon_client, "dave")
    from sqlalchemy.ext.asyncio import AsyncSession
    real_get = AsyncSession.get

    async def broken(self, model, ident, *a, **k):
        if model is HelixUserModel and ident == dave.id:
            raise RuntimeError("boom")
        return await real_get(self, model, ident, *a, **k)
    monkeypatch.setattr(AsyncSession, "get", broken)
    assert (await anon_client.get(ME, headers=h)).status_code == 401


# ── what the user set in motion (parts c and d) ──────────────────────────────

async def _in_motion(session, bob):
    """A pending legal-hold release request, a queued bot job, a report
    delivery, a schedule and a bot he authored, an AI conversation on a HELD
    case and a personal one."""
    from velaris_shared.identity.lifecycle import _t
    now = datetime.now(timezone.utc)
    name = bob.username
    tenant = uuid.uuid4()
    await session.execute(insert(_t("tenants")).values(id=tenant, slug=f"t-{tenant.hex[:8]}", name="T"))
    held_case, hold = uuid.uuid4(), uuid.uuid4()
    await session.execute(insert(_t("legal_holds")).values(
        id=hold, tenant_id=tenant, case_id=held_case, matter_ref="M-1", reason="litigation",
        placed_by="counsel", status="active"))
    await session.execute(insert(_t("legal_hold_release_requests")).values(
        id=uuid.uuid4(), hold_id=hold, tenant_id=tenant, reason="done", requested_by=name,
        status="pending", pending_key=str(hold)))
    bot = uuid.uuid4()
    await session.execute(insert(_t("bot_definitions")).values(
        id=bot, tenant_id=str(tenant), name="B", image="i", digest="sha256:x", created_by=name))
    await session.execute(insert(_t("bot_jobs")).values(
        id=uuid.uuid4(), tenant_id=str(tenant), bot_id=bot, requested_by=name, status="queued"))
    rep = uuid.uuid4()
    await session.execute(insert(_t("saved_reports")).values(id=rep, name="R"))
    await session.execute(insert(_t("report_subscriptions")).values(
        id=uuid.uuid4(), report_id=rep, destination=f"{name}@example.com", enabled=True, created_by=name))
    await session.execute(insert(_t("schedules")).values(
        id=uuid.uuid4(), tenant_id=str(tenant), name="S", cron="0 * * * *", action_kind="noop", created_by=name))
    held_conv, own_conv = uuid.uuid4(), uuid.uuid4()
    for cid, case in ((held_conv, held_case), (own_conv, None)):
        await session.execute(insert(_t("hxnexus_conversations")).values(id=cid, user_id=str(bob.id), case_id=case))
        await session.execute(insert(_t("hxnexus_messages")).values(
            id=uuid.uuid4(), conversation_id=cid, role="user", content="about it"))
    await session.commit()
    return {"held_conv": held_conv, "own_conv": own_conv}


async def test_the_delete_withdraws_his_requests_cancels_his_jobs_and_pauses_his_reports(anon_client, session):
    await _two_admins(session)
    bob = await _user(session, "bob", ["case_worker"])
    await _in_motion(session, bob)
    admin_h = await _login(anon_client, "del.admin")
    assert (await anon_client.delete(f"{USERS}/{bob.id}", headers=admin_h)).status_code == 204
    t = _md().tables
    req = (await session.execute(select(t["legal_hold_release_requests"]))).one()
    assert req.status == "withdrawn" and req.pending_key is None          # nobody approves it later
    assert (await session.execute(select(t["bot_jobs"]))).one().status == "cancelled"
    assert (await session.execute(select(t["report_subscriptions"]))).one().enabled is False
    assert (await session.execute(select(t["schedules"]))).one() is not None   # org-owned: still there
    ev = (await session.execute(select(t["security_events"]).where(
        t["security_events"].c.event_type == "user_deleted"))).one()
    removed = ev.details["removed"]
    assert removed["authored_schedules"] == 1 and removed["authored_bots"] == 1   # listed for review
    # Undo: his report deliveries come back; the withdrawn request does not.
    assert (await anon_client.post(f"{USERS}/{bob.id}/restore", headers=admin_h)).status_code == 200
    session.expunge_all()
    assert (await session.execute(select(t["report_subscriptions"]))).one().enabled is True
    assert (await session.execute(select(t["legal_hold_release_requests"]))).one().status == "withdrawn"


async def test_the_purge_keeps_his_conversations_about_a_held_case(anon_client, session):
    from velaris_shared.identity import lifecycle
    await _two_admins(session)
    bob = await _user(session, "bob", ["case_worker"])
    ids = await _in_motion(session, bob)
    await anon_client.delete(f"{USERS}/{bob.id}?permanent=true", headers=await _login(anon_client, "del.admin"))
    t = _md().tables
    convs = {r.id for r in (await session.execute(select(t["hxnexus_conversations"]))).all()}
    assert convs == {ids["held_conv"]}                    # evidence under a legal hold stays
    assert (await session.execute(select(func.count()).select_from(t["hxnexus_messages"]))).scalar() == 1
    assert lifecycle                                      # (imported for the reader)


async def test_routing_to_a_deleted_user_goes_to_the_fallback_and_is_flagged(anon_client, session):
    from case_service.core.assignment_router import resolve_assignment
    await _two_admins(session)
    bob = await _user(session, "bob", ["case_worker"])
    rule = {"strategy": "specific_user", "target": "bob"}
    assert await resolve_assignment(session, rule, {}) == ("user", "bob")          # as before
    await anon_client.delete(f"{USERS}/{bob.id}", headers=await _login(anon_client, "del.admin"))
    session.expunge_all()
    assert await resolve_assignment(session, rule, {}) == ("queue", "default-queue")
    assert await resolve_assignment(session, {**rule, "fallback_target": "claims"}, {}) == ("queue", "claims")
    t = _md().tables
    assert (await session.execute(select(func.count()).select_from(t["security_events"]).where(
        t["security_events"].c.event_type == "routing_target_deleted"))).scalar() >= 1
    # A name that never was an account is routed exactly as before.
    assert await resolve_assignment(session, {**rule, "target": "nobody.ever"}, {}) == ("user", "nobody.ever")
