"""The deletion ledger (Nov_TODO "User deletion v2" part b): a purged account
stays purged — even when a backup taken before the purge is restored.

The records live outside the database (a restore rolls it back), are signed,
hold no username, and are replayed by account id. These cover the purge →
record path, the restore → refusal → replay path, the directory sync, and the
backup tool's off-box copy and collection.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
import importlib.util
import json
import os
import pathlib
import uuid

import pytest
from sqlalchemy import select

from velaris_shared.auth.passwords import hash_password
from velaris_shared.db.models import DeletionLedgerModel, HelixUserModel
from velaris_shared.identity import ledger, lifecycle

pytestmark = pytest.mark.account_gate

PW = "a-long-test-password-1"
ROOT = pathlib.Path(__file__).resolve().parents[4]


async def _user(session, username, roles=("viewer",)):
    u = HelixUserModel(username=username, email=f"{username}@example.com", roles=list(roles),
                       is_active=True, password_hash=hash_password(PW))
    session.add(u)
    await session.commit()
    return u


async def _login(client, username) -> dict:
    r = await client.post("/api/v1/auth/real/login", json={"username": username, "password": PW})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def test_a_purge_writes_one_signed_record_without_the_name(session):
    bob = await _user(session, "bob.secret")
    bid = str(bob.id)
    await lifecycle.purge_user(session, bob, actor="admin")
    await session.commit()
    assert await ledger.flush(session) == (1, 0)
    d = ledger.ledger_dir()
    files = list(d.glob("*.json"))
    assert len(files) == 1 and files[0].name.endswith(f"-{bid}.json")
    assert oct(d.stat().st_mode & 0o777) == "0o700" and oct(files[0].stat().st_mode & 0o777) == "0o600"
    text = files[0].read_text()
    rec = json.loads(text)
    assert rec["account_id"] == bid and ledger.verify(rec)
    assert "bob.secret" not in text and rec["name_hash"] == ledger.name_hash("bob.secret")
    row = (await session.execute(select(DeletionLedgerModel))).scalar_one()
    assert row.written_at is not None
    assert await ledger.flush(session) == (0, 0)               # written once


async def test_a_record_queues_with_the_purge_and_survives_a_failed_write(session, monkeypatch):
    """Same transaction as the purge; a file that cannot be written is
    reported and retried, never lost."""
    bob = await _user(session, "bob.retry")
    await lifecycle.purge_user(session, bob, actor="admin")
    await session.commit()
    monkeypatch.setattr(ledger, "_write", lambda text: (_ for _ in ()).throw(OSError("disk full")))
    assert await ledger.flush(session) == (0, 1)
    from velaris_shared.db.models import SecurityEventModel
    ev = (await session.execute(select(SecurityEventModel).where(
        SecurityEventModel.event_type == "deletion_ledger_write_failed"))).scalars().all()
    assert len(ev) == 1
    monkeypatch.undo()
    assert await ledger.flush(session) == (1, 0)


def test_an_edited_or_planted_record_is_refused(tmp_path):
    good = ledger.make_record(type("U", (), {"id": uuid.uuid4(), "username": "x"})(), actor="a")
    assert ledger.verify(good)
    for change in ({"account_id": str(uuid.uuid4())}, {"by": "someone"}, {"sig": "0" * 64}, {"kind": "other"}):
        assert not ledger.verify({**good, **change}), change
    d = tmp_path / "d"
    d.mkdir()
    (d / "a.json").write_text(json.dumps(good))
    (d / "b.json").write_text(json.dumps({**good, "account_id": str(uuid.uuid4())}))
    (d / "c.json").write_text("not json")
    got, bad = ledger.load([d])
    assert list(got) == [good["account_id"]] and len(bad) == 2


async def test_after_a_restore_the_account_is_refused_at_once_then_purged_again(anon_client, session):
    """A set taken before the purge brings the row back. Sign-in refuses it from
    the first request (the ledger files), and the replay purges it again."""
    bob = await _user(session, "bob.back")
    h = await _login(anon_client, "bob.back")
    bid = bob.id
    rec = ledger.make_record(bob, actor="admin")             # the record a purge wrote …
    d = ledger.ledger_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"x-{bid}.json").write_text(json.dumps(rec))         # … and the row is still here (restored)
    ledger.reset_cache_for_tests()
    r = await anon_client.get("/api/v1/auth/real/me/profile", headers=h)
    assert r.status_code == 401 and "no longer exists" in r.text
    r = await anon_client.post("/api/v1/auth/real/login", json={"username": "bob.back", "password": PW})
    assert r.status_code == 200                               # (the token it gets is refused, below)
    assert (await anon_client.get("/api/v1/auth/real/me/profile",
                                  headers={"Authorization": "Bearer " + r.json()["access_token"]})).status_code == 401
    assert await ledger.replay_local(session) == [str(bid)]
    session.expunge_all()
    assert await session.get(HelixUserModel, bid) is None
    assert await ledger.replay_local(session) == []           # idempotent


async def test_the_restore_cli_replays_by_id_and_reports(session, tmp_path):
    from velaris_shared.identity import ledger_cli
    keep = await _user(session, "keep.me")
    gone = await _user(session, "gone.again")
    d = tmp_path / "fetched"
    d.mkdir()
    for u in (gone,):
        (d / f"r-{u.id}.json").write_text(json.dumps(ledger.make_record(u, actor="admin")))
    (d / f"r-{uuid.uuid4()}.json").write_text(json.dumps(
        ledger.make_record(type("U", (), {"id": uuid.uuid4(), "username": "n"})(), actor="a")))
    (d / "forged.json").write_text(json.dumps({**ledger.make_record(keep, actor="a"), "sig": "f" * 64}))
    out = await ledger_cli._replay([d])
    assert out["replayed"] == [str(gone.id)] and out["already_gone"] == 1
    assert len(out["refused_records"]) == 1 and out["failed"] == []
    session.expunge_all()
    assert await session.get(HelixUserModel, keep.id) is not None      # the forged record named it
    assert await session.get(HelixUserModel, gone.id) is None


async def test_the_directory_sync_does_not_bring_a_purged_id_back(anon_client, session):
    from velaris_shared.db.models import UserDirectoryModel
    await _user(session, "dir.admin", ["admin"])
    bob = await _user(session, "bob.ghost")
    bid = str(bob.id)
    await lifecycle.purge_user(session, bob, actor="admin")
    await session.commit()
    # History still names him (by id and by name), as it does after a purge.
    from sqlalchemy import insert
    from velaris_shared.identity.lifecycle import _t
    ct = uuid.uuid4()
    await session.execute(insert(_t("case_types")).values(id=ct, name="CT", version="1", definition_json={}))
    await session.execute(insert(_t("case_instances")).values(
        id=uuid.uuid4(), case_type_id=ct, case_type_version="1", created_by=bid))
    await session.commit()
    r = await anon_client.post("/api/v1/user-directory/sync-from-db", headers=await _login(anon_client, "dir.admin"))
    assert r.status_code == 200, r.text
    assert r.json()["skipped_purged"] >= 1 and "bob.ghost" not in r.json()["new_user_ids"]
    rows = (await session.execute(select(UserDirectoryModel.user_id))).scalars().all()
    assert "bob.ghost" not in rows and bid not in rows


def _backup_tool(monkeypatch, tmp_path, env: dict):
    spec = importlib.util.spec_from_file_location("velaris_backup", ROOT / "scripts/backup/velaris_backup.py")
    vb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vb)
    cfg = vb.Config.__new__(vb.Config)
    cfg.env = env
    cfg.nas_dir = pathlib.Path(env["VELARIS_BACKUP_PATH"]) if env.get("VELARIS_BACKUP_PATH") else None
    cfg.s3 = {k: "" for k in ("endpoint", "bucket", "access_key", "secret_key", "region", "prefix")}
    return vb, cfg


def test_the_backup_tool_copies_records_off_the_box_append_only_and_collects_them(monkeypatch, tmp_path):
    local, nas = tmp_path / "local", tmp_path / "nas"
    local.mkdir()
    vb, cfg = _backup_tool(monkeypatch, tmp_path, {"VELARIS_CASE_DELETION_LEDGER_DIR": str(local),
                                                   "VELARIS_BACKUP_PATH": str(nas)})
    (local / "a.json").write_text("{}")
    res = vb.push_deletions(cfg)
    assert res["path"] == {"ok": True, "new": 1, "where": str(nas / "deletions")}
    (local / "b.json").write_text("{}")
    assert vb.push_deletions(cfg)["path"]["new"] == 1                    # only what is new
    assert oct((nas / "deletions" / "a.json").stat().st_mode & 0o777) == "0o600"
    # The box is lost: a new one collects them from the NAS.
    new_local = tmp_path / "new"
    vb2, cfg2 = _backup_tool(monkeypatch, tmp_path, {"VELARIS_CASE_DELETION_LEDGER_DIR": str(new_local),
                                                     "VELARIS_BACKUP_PATH": str(nas)})
    assert vb2.collect_deletions(cfg2) == (2, [])
    assert sorted(p.name for p in new_local.glob("*.json")) == ["a.json", "b.json"]
    assert vb2.collect_deletions(cfg2) == (0, [])                        # nothing twice


def test_the_path_unit_pushes_at_once_and_only_the_installer_enables_it():
    path = (ROOT / "deploy/systemd/velaris-deletions-push.path").read_text()
    svc = (ROOT / "deploy/systemd/velaris-deletions-push.service").read_text()
    assert "PathChanged=__VELARIS_DIR__/.deletions" in path
    assert "backup-velaris.sh --push-deletions --quiet" in svc and "User=__VELARIS_USER__" in svc
    assert "velaris-deletions-push.path" in (ROOT / "deploy/systemd/install-backup-timer.sh").read_text()
    for f in ("setup-velaris.sh", "update-velaris.sh"):
        assert "velaris-deletions-push" not in (ROOT / f).read_text()
