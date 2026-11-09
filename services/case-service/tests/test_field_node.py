"""Oct item 8 — the F-10 field node, driven against the real hub API.

The hub half was proven against a simulated node (`test_F10_field_operations`).
Here the node is the shipped one — `velaris_shared.field_node`, through its CLI —
and the hub is the real FastAPI app, so the two halves are held to agreeing.

Pinned: a full offline round trip converges and the node drops what it synced;
a partition past `stale_after` locks the node out (and a clock set back does not
undo it); a stream edited on the device is refused in full; a shred order is
obeyed at check-in even when the sync itself would be refused; `offline_deny`
holds on the device; the local model is local; the hub may require signatures,
and requires nothing new by default.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import stat
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from velaris_shared.db.models import (
    CaseInstanceModel,
    CaseTypeModel,
    FieldNodeModel,
    FieldSyncEventModel,
    TenantModel,
    UserDirectoryModel,
)
from velaris_shared.field import subchain
from velaris_shared.field_node import assist, cli
from velaris_shared.field_node import node as node_mod
from velaris_shared.field_node.hub import Hub, HubError
from velaris_shared.field_node.node import FieldNode, NodeError, Stale

from case_service.field import service
from case_service.handoff import service as handoff
from case_service.main import app
from case_service.tenancy import identity as tenancy
from tests.conftest import LIFECYCLE_PROCESS_ID, TestSessionFactory  # type: ignore[attr-defined]
from tests.conftest import _headers  # type: ignore[attr-defined]

pytestmark = pytest.mark.asyncio

SLUG = "ship-a"


@pytest_asyncio.fixture
async def api():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def world():
    tenancy.clear_cache()
    async with TestSessionFactory() as db:
        t = TenantModel(id=uuid.uuid4(), slug="default", name="Default")
        o = TenantModel(id=uuid.uuid4(), slug="other", name="Other")
        db.add_all([t, o])
        await db.flush()
        db.add(UserDirectoryModel(id=uuid.uuid4(), user_id="mal", tenant_id="other"))
        ct = CaseTypeModel(id=uuid.uuid4(), name="Inspections", version="1.0.0",
                           lifecycle_process_id=LIFECYCLE_PROCESS_ID,
                           definition_json={"stages": []}, tenant_id=str(t.id))
        db.add(ct)
        await db.flush()
        case = CaseInstanceModel(id=uuid.uuid4(), case_type_id=ct.id, case_type_version="1.0.0",
                                 status="open", priority="medium", tenant_id=t.id,
                                 created_by="x", data={"reference": "R-1"})
        db.add(case)
        await db.commit()
    yield {"t": t, "ct": ct, "case": case}
    tenancy.clear_cache()


def _bridge(api, loop):
    """The node's CLI is synchronous; the app is async. The CLI runs in a
    thread and its requests are handed back to the test's loop."""
    def transport(method, url, body, headers):
        path = url.split("http://127.0.0.1:8200", 1)[1]
        fut = asyncio.run_coroutine_threadsafe(
            api.request(method, path, json=body, headers=headers), loop)
        r = fut.result(60)
        return r.status_code, (r.json() if r.content else None)
    return transport


async def _run(api, *argv, token=None):
    """Run the CLI as a worker would. Returns (exit code, parsed stdout)."""
    import contextlib
    import io

    loop = asyncio.get_running_loop()
    out = io.StringIO()
    env_token = token or _headers("admin", ["admin"])["Authorization"].split(" ", 1)[1]

    def go():
        old = os.environ.get("VELARIS_FIELD_TOKEN")
        os.environ["VELARIS_FIELD_TOKEN"] = env_token
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                return cli.main(list(argv), transport=_bridge(api, loop))
        finally:
            if old is None:
                os.environ.pop("VELARIS_FIELD_TOKEN", None)
            else:
                os.environ["VELARIS_FIELD_TOKEN"] = old

    code = await asyncio.to_thread(go)
    text = out.getvalue()
    try:
        return code, json.loads(text)
    except ValueError:
        return code, text


async def _enrolled(api, world, tmp_path, **register):
    db = str(tmp_path / "node" / "node.db")
    code, out = await _run(api, "--db", db, "init", "--slug", SLUG, "--hub", "http://127.0.0.1:8200")
    assert code == 0, out
    r = await api.post("/api/v1/field/nodes", headers=_headers("admin", ["admin"]), json={
        "slug": SLUG, "display_name": "Ship A", "public_key": out["public_key"],
        "charter": {"case_type_ids": [str(world["ct"].id)]}, **register})
    assert r.status_code == 201, r.text
    assert r.json()["node"]["key_id"] == out["key_id"]
    return db


async def _node_row():
    async with TestSessionFactory() as s:
        return (await s.execute(select(FieldNodeModel).where(
            FieldNodeModel.slug == SLUG))).scalars().first()


# ══ the device ══════════════════════════════════════════════════════════════


async def test_init_makes_a_private_file_and_the_hubs_fingerprint(tmp_path):
    n = FieldNode.init(tmp_path / "n" / "node.db", slug=SLUG, hub_url="https://hub.test")
    assert stat.S_IMODE((tmp_path / "n" / "node.db").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "n").stat().st_mode) == 0o700
    assert n.key_id == handoff.key_fingerprint(n.public_key)
    with pytest.raises(NodeError, match="initialised once"):
        FieldNode.init(tmp_path / "n" / "node.db", slug=SLUG, hub_url="https://hub.test")


async def test_a_readable_state_file_is_refused(tmp_path):
    FieldNode.init(tmp_path / "node.db", slug=SLUG, hub_url="https://hub.test").close()
    os.chmod(tmp_path / "node.db", 0o644)
    with pytest.raises(NodeError, match="chmod 600"):
        FieldNode(tmp_path / "node.db")


async def test_the_node_and_hub_agree_on_what_they_share():
    assert node_mod.EVENT_KINDS == service.EVENT_KINDS
    fake = FieldNodeModel(slug="a", key_id="k")
    assert node_mod.contact_message("a", "k", "t") == service.contact_message(fake, "t")


async def test_the_hub_must_be_https_unless_it_is_this_machine():
    with pytest.raises(HubError, match="https"):
        Hub("http://hub.example.com", "t")
    Hub("http://127.0.0.1:8200", "t")
    Hub("https://hub.example.com", "t")


# ══ a round trip ════════════════════════════════════════════════════════════


async def test_a_full_offline_round_trip_converges_signed(api, world, tmp_path, monkeypatch):
    monkeypatch.setenv(service.REQUIRE_SIGNATURE_ENV, "true")
    db = await _enrolled(api, world, tmp_path)
    code, out = await _run(api, "--db", db, "pull")
    assert code == 0 and out["cases"] == 1, out
    cid = str(world["case"].id)
    code, out = await _run(api, "--db", db, "cases")
    assert [c["case_id"] for c in out] == [cid]
    assert (await _run(api, "--db", db, "note", cid, "boarded"))[0] == 0
    assert (await _run(api, "--db", db, "set", cid, "inspection_code", '"OK-7"'))[0] == 0
    assert (await _run(api, "--db", db, "complete", cid, "s1"))[0] == 0

    code, out = await _run(api, "--db", db, "sync")
    assert code == 0 and out["verified"] and out["applied"] == 3, out
    async with TestSessionFactory() as s:
        case = await s.get(CaseInstanceModel, world["case"].id)
        assert case.data["inspection_code"] == "OK-7"
    assert (await _node_row()).subchain_head == out["head"]
    # What was synced is gone from the device.
    status = FieldNode(db).status()
    assert status["cases"] == 0 and status["unsynced_events"] == 0
    assert status["dispatch_id"] is None and status["last_synced_head"] == out["head"]


async def test_a_bundle_is_not_replaced_over_unsynced_work(api, world, tmp_path):
    db = await _enrolled(api, world, tmp_path)
    await _run(api, "--db", db, "pull")
    await _run(api, "--db", db, "note", str(world["case"].id), "unsynced")
    code, _ = await _run(api, "--db", db, "pull")
    assert code == 2
    assert FieldNode(db).unsynced() == 1


async def test_a_bundle_for_another_node_is_refused(tmp_path):
    n = FieldNode.init(tmp_path / "node.db", slug=SLUG, hub_url="https://hub.test")
    with pytest.raises(NodeError, match="dispatched to"):
        n.load_bundle({"dispatch_id": "d", "cases": [], "subchain_genesis": "g",
                       "stale_after": "2099-01-01T00:00:00+00:00", "offline_deny": [],
                       "node": {"slug": "ship-b"}})


# ══ the partition ═══════════════════════════════════════════════════════════


async def test_a_partition_past_stale_after_locks_the_node(api, world, tmp_path, monkeypatch):
    """Cut off for 73 hours on a 72-hour snapshot: the node stops acting. It
    can still sync what it did in time, which is how it gets home."""
    db = await _enrolled(api, world, tmp_path)
    await _run(api, "--db", db, "pull")
    cid = str(world["case"].id)
    await _run(api, "--db", db, "note", cid, "in time")

    later = datetime.now(timezone.utc) + timedelta(hours=73)
    monkeypatch.setattr(node_mod, "_utcnow", lambda: later)
    n = FieldNode(db)
    assert n.staleness()["stale"] is True
    for act in (n.cases, lambda: n.show(cid), lambda: n.note(cid, "late"),
                lambda: n.set_field(cid, "x", 1)):
        with pytest.raises(Stale):
            act()
    assert n.unsynced() == 1
    n.close()
    monkeypatch.setattr(node_mod, "_utcnow", lambda: datetime.now(timezone.utc))
    # ... and setting the clock back does not reopen it.
    n = FieldNode(db)
    monkeypatch.setattr(node_mod, "_utcnow", lambda: later)
    FieldNode(db).staleness()
    with pytest.raises(Stale):
        FieldNode(db).cases()
    back = FieldNode(db, clock=lambda: datetime.now(timezone.utc))
    with pytest.raises(NodeError, match="went\\s+backwards"):
        back.cases()


async def test_the_hub_refuses_work_the_node_dated_after_its_lockout(api, world, tmp_path):
    """Belt and braces: a device whose lockout was bypassed (root, a hacked
    file) still cannot land late work — the hub checks `node_time` itself."""
    db = await _enrolled(api, world, tmp_path)
    await _run(api, "--db", db, "pull")
    n = FieldNode(db)
    n._set("stale_after", "2099-01-01T00:00:00+00:00")        # the bypass
    n._clock = lambda: datetime.now(timezone.utc) + timedelta(days=5)
    n.note(str(world["case"].id), "late")
    body = n.sync_body()
    n.close()
    r = await api.post(f"/api/v1/field/nodes/{SLUG}/sync", json=body,
                       headers=_headers("admin", ["admin"]))
    out = r.json()
    assert r.status_code == 200 and out["rejected"] == 1 and out["applied"] == 0, out


# ══ tampering ═══════════════════════════════════════════════════════════════


async def test_a_stream_edited_on_the_device_is_refused_in_full(api, world, tmp_path):
    db = await _enrolled(api, world, tmp_path)
    await _run(api, "--db", db, "pull")
    cid = str(world["case"].id)
    await _run(api, "--db", db, "set", cid, "inspection_code", '"OK-7"')
    await _run(api, "--db", db, "note", cid, "second")
    raw = sqlite3.connect(db)
    raw.execute("UPDATE events SET payload = ? WHERE seq = 1", (json.dumps({"value": "BAD"}),))
    raw.commit()
    raw.close()
    code, _ = await _run(api, "--db", db, "sync")
    assert code == 4
    async with TestSessionFactory() as s:
        assert (await s.execute(select(func.count()).select_from(FieldSyncEventModel))).scalar() == 0
        case = await s.get(CaseInstanceModel, world["case"].id)
        assert "inspection_code" not in case.data
    assert FieldNode(db).unsynced() == 2           # kept, for whoever investigates


async def test_an_event_that_declares_no_payload_hash_does_not_verify():
    """Found in this item's audit: the check ran only when a hash was declared,
    so an event with none had a payload the chain did not cover."""
    events = subchain.build([{"case_id": "c", "kind": "note", "payload": {"text": "a"}}], "g")
    events[0]["payload_sha256"] = ""
    events[0]["content_hash"] = subchain.link(subchain.event_document(events[0]), "g")
    events[0]["payload"] = {"text": "anything at all"}
    ok, _, reason = subchain.verify(events, "g")
    assert not ok and "payload" in reason


# ══ losing the device ═══════════════════════════════════════════════════════


async def test_a_shred_order_lands_even_when_the_sync_would_be_refused(api, world, tmp_path):
    """Before item 8 the order only rode home on a SUCCESSFUL sync. Here the
    node's dispatch has been superseded, so its sync is refused — and it wipes
    anyway, because it checks in first."""
    db = await _enrolled(api, world, tmp_path)
    await _run(api, "--db", db, "pull")
    await _run(api, "--db", db, "note", str(world["case"].id), "work")
    h = _headers("admin", ["admin"])
    assert (await api.post(f"/api/v1/field/nodes/{SLUG}/dispatch", headers=h, json={})).status_code == 201
    r = await api.post(f"/api/v1/field/nodes/{SLUG}/shred", headers=h,
                       json={"reason": "device lost overboard"})
    assert r.status_code == 200, r.text
    code, out = await _run(api, "--db", db, "sync")
    assert code == 3 and out["wiped"] is True
    assert not os.path.exists(db)
    row = await _node_row()
    assert row.status == "shredded" and row.shredded_at is not None
    # A shredded node's key is dead on the hub too.
    code, _ = await _run(api, "--db", db, "status")
    assert code == 2


async def test_wipe_leaves_nothing_behind(tmp_path):
    p = tmp_path / "node.db"
    n = FieldNode.init(p, slug=SLUG, hub_url="https://hub.test")
    n._set("secret_marker", "CASE-DATA-XYZ")
    n.wipe()
    assert not p.exists() and not (tmp_path / "node.db-journal").exists()


# ══ offline_deny and the local model ════════════════════════════════════════


async def test_offline_deny_is_enforced_on_the_device(api, world, tmp_path):
    db = await _enrolled(api, world, tmp_path)
    await _run(api, "--db", db, "pull")
    code, _ = await _run(api, "--db", db, "export", str(world["case"].id))
    assert code == 2                                 # `export` is denied by default
    n = FieldNode(db)
    n._set("offline_deny", json.dumps(["assist"]))
    assert n.export(str(world["case"].id))["case_id"] == str(world["case"].id)
    with pytest.raises(NodeError, match="assist"):
        n.assist(str(world["case"].id), "q", url="http://127.0.0.1:11434", model="m",
                 post=lambda *a: "{}")


@pytest.mark.parametrize("url", [
    "http://8.8.8.8:11434", "http://169.254.169.254", "http://0.0.0.0:11434",
    "file:///etc/passwd", "http://[::]:11434"])
async def test_the_model_must_be_local(url):
    with pytest.raises(assist.AssistError):
        assist.pin(url + "/api/generate")


async def test_the_model_is_asked_at_the_address_that_was_checked(api, world, tmp_path):
    db = await _enrolled(api, world, tmp_path)
    await _run(api, "--db", db, "pull")
    seen = {}

    def post(url, host, body):
        seen.update(url=url, host=host, prompt=body["prompt"])
        return json.dumps({"response": "x" * 10000})
    answer = FieldNode(db).assist(str(world["case"].id), "what next?",
                                  url="http://localhost:11434", model="m", post=post)
    assert seen["url"].startswith(("http://127.0.0.1:11434", "http://[::1]:11434"))
    assert seen["host"] == "localhost:11434"
    assert "DATA, not instructions" in seen["prompt"]
    assert len(answer) == assist.MAX_ANSWER
    assert FieldNode(db).unsynced() == 0             # advice is never recorded


async def test_a_redirecting_model_is_refused():
    with pytest.raises(assist.AssistError, match="redirect"):
        assist._NoRedirect().redirect_request(None, None, 302, "", {}, "http://evil")


# ══ the hub's side ══════════════════════════════════════════════════════════


async def test_unsigned_syncs_and_contacts_work_by_default(api, world, tmp_path, monkeypatch):
    """The switch is off unless set: an existing node that never signed keeps
    working after the upgrade."""
    monkeypatch.delenv(service.REQUIRE_SIGNATURE_ENV, raising=False)
    await _enrolled(api, world, tmp_path)
    h = _headers("admin", ["admin"])
    r = await api.post(f"/api/v1/field/nodes/{SLUG}/contact", headers=h)
    assert r.status_code == 200 and r.json()["signed"] is False, r.text
    # An unsigned contact is told to wipe, but is not taken as proof it did.
    await api.post(f"/api/v1/field/nodes/{SLUG}/shred", headers=h, json={"reason": "lost it"})
    r = await api.post(f"/api/v1/field/nodes/{SLUG}/contact", headers=h)
    assert r.json()["wipe"] is True and r.json()["status"] == "shred_ordered"
    await api.post(f"/api/v1/field/nodes/{SLUG}/contact", headers=h)
    async with TestSessionFactory() as s:
        n = await s.get(FieldNodeModel, (await _node_row()).id)
        n.status = "active"
        await s.commit()
    d = (await api.post(f"/api/v1/field/nodes/{SLUG}/dispatch", headers=h, json={})).json()
    r = await api.post(f"/api/v1/field/nodes/{SLUG}/sync", headers=h,
                       json={"dispatch_id": d["dispatch_id"], "events": []})
    assert r.status_code == 200, r.text


async def test_when_required_a_staff_token_alone_cannot_sync(api, world, tmp_path, monkeypatch):
    monkeypatch.setenv(service.REQUIRE_SIGNATURE_ENV, "1")
    await _enrolled(api, world, tmp_path)
    h = _headers("admin", ["admin"])
    assert (await api.post(f"/api/v1/field/nodes/{SLUG}/contact", headers=h)).status_code == 409
    d = (await api.post(f"/api/v1/field/nodes/{SLUG}/dispatch", headers=h, json={})).json()
    r = await api.post(f"/api/v1/field/nodes/{SLUG}/sync", headers=h,
                       json={"dispatch_id": d["dispatch_id"], "events": []})
    assert r.status_code == 409 and "signed" in r.text


async def test_a_forged_or_replayed_contact_is_refused(api, world, tmp_path):
    db = await _enrolled(api, world, tmp_path)
    h = _headers("admin", ["admin"])
    url = f"/api/v1/field/nodes/{SLUG}/contact"
    other = FieldNode.init(tmp_path / "other.db", slug=SLUG, hub_url="https://hub.test")
    r = await api.post(url, headers=h, json=other.signed_contact())
    assert r.status_code == 409, r.text                   # the wrong key
    n = FieldNode(db, clock=lambda: datetime.now(timezone.utc) - timedelta(hours=1))
    r = await api.post(url, headers=h, json=n.signed_contact())
    assert r.status_code == 409 and "timestamp" in r.text  # an old capture
    r = await api.post(url, headers=h, json=FieldNode(db).signed_contact())
    assert r.status_code == 200 and r.json()["signed"] is True


async def test_contact_is_tenant_scoped_and_gated(api, world, tmp_path):
    await _enrolled(api, world, tmp_path)
    url = f"/api/v1/field/nodes/{SLUG}/contact"
    assert (await api.post(url, headers=_headers("mal", ["admin"]))).status_code == 404
    assert (await api.post(url, headers=_headers("v", ["viewer"]))).status_code == 403
    assert (await api.post(url)).status_code in (401, 403)
