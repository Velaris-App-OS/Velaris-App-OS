"""The deletion ledger: a purged account stays purged, even after a restore
(Nov_TODO "User deletion v2" part b, 2026-10-23).

A backup taken before a purge brings the account back — with its access. The
ledger that undoes that must live OUTSIDE the database, because a restore rolls
the database back:

* **Queued with the purge.** `enqueue` adds the signed record to the
  `deletion_ledger` table in the purge's own transaction (mig 224): a purge
  never lacks its record, a record never names an account that was not purged.
* **Written to files.** `flush` writes each queued record to
  `<install>/.deletions/<when>-<account id>.json` (owner only) and marks it
  written. The purge does it at once; the hourly purge job retries any that
  failed. Copied off the box by the backup tool (every run, and at once through
  `velaris-deletions-push.path`) and pulled by the standby each minute.
* **Replayed by id.** `restore-velaris.sh` collects every record it can reach
  and purges each account id that the restored database still holds. Account
  ids are random and never reused, and an undo exists only BEFORE a record is
  written — so replaying every record is always right: no clock, no database
  position, nothing to order.
* **Signed.** An HMAC with a key derived from this install's own signing secret
  (it comes back with every restore): a planted or edited record — which would
  otherwise delete whatever account it names — is refused.
* **Minimal.** The account id, a keyed hash of the username (never the name),
  when, and who; the history of what that account did stays in the database.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

KIND = "account_purged"
INSTALL_ROOT = Path(__file__).resolve().parents[4]


def ledger_dir() -> Path:
    from velaris_shared.config import get_settings
    d = (get_settings().deletion_ledger_dir or "").strip()
    return Path(d) if d else INSTALL_ROOT / ".deletions"


def _key() -> bytes:
    from velaris_shared.config import get_settings
    s = get_settings()
    secret = (s.auth_rsa_private_key or s.auth_secret or "").encode()
    if not secret:
        raise RuntimeError("no signing secret on this install — the deletion ledger cannot sign")
    return hashlib.sha256(b"velaris-deletion-ledger|" + secret).digest()


def _mac(data: str) -> str:
    return hmac.new(_key(), data.encode(), hashlib.sha256).hexdigest()


def name_hash(username: str) -> str:
    """A keyed hash: lets this install recognise a purged name without the
    ledger holding it."""
    return _mac("name|" + (username or "").lower())[:32]


def _canonical(body: dict) -> str:
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def make_record(user: Any, *, actor: str) -> dict:
    body = {"v": 1, "kind": KIND, "account_id": str(user.id), "name_hash": name_hash(user.username),
            "at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(), "by": actor}
    return {**body, "sig": _mac(_canonical(body))}


def verify(record: dict) -> bool:
    body = {k: v for k, v in record.items() if k != "sig"}
    try:
        uuid.UUID(str(body.get("account_id")))
    except ValueError:
        return False
    return body.get("kind") == KIND and hmac.compare_digest(str(record.get("sig", "")), _mac(_canonical(body)))


async def enqueue(session: AsyncSession, user: Any, *, actor: str) -> None:
    """In the purge's own transaction."""
    from velaris_shared.db.models import DeletionLedgerModel
    rec = make_record(user, actor=actor)
    session.add(DeletionLedgerModel(account_id=user.id, record=_canonical(rec)))


def _write(rec_text: str) -> Path:
    rec = json.loads(rec_text)
    d = ledger_dir()
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    stamp = rec["at"].replace(":", "").replace("-", "")[:15]
    path = d / f"{stamp}-{rec['account_id']}.json"
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(rec_text + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return path


async def flush(session: AsyncSession) -> tuple[int, int]:
    """Write every queued record to its file; (written, failed). A failure is
    reported (security event, high) and retried at the next run."""
    from velaris_shared.db.models import DeletionLedgerModel
    rows = (await session.execute(select(DeletionLedgerModel).where(
        DeletionLedgerModel.written_at.is_(None)))).scalars().all()
    written = failed = 0
    for row in rows:
        try:
            _write(row.record)
            row.written_at = datetime.now(timezone.utc)
            written += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            log.error("deletion ledger: could not write the record of %s: %s", row.account_id, exc)
            try:
                from velaris_shared.db.security_events import log_security_event
                await log_security_event(
                    session, event_type="deletion_ledger_write_failed", severity="high",
                    resource_type="user", resource_id=str(row.account_id), action="ledger",
                    outcome="failure", details={"error": str(exc)[:300], "dir": str(ledger_dir())})
            except Exception:  # noqa: BLE001
                pass
    await session.commit()
    return written, failed


def load(dirs: list[Path]) -> tuple[dict[str, dict], list[str]]:
    """Every verified record under `dirs` by account id, and what was refused."""
    good: dict[str, dict] = {}
    bad: list[str] = []
    for d in dirs:
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.json")):
            try:
                rec = json.loads(p.read_text())
            except (OSError, ValueError):
                bad.append(f"{p.name}: unreadable")
                continue
            if verify(rec):
                good[rec["account_id"]] = rec
            else:
                bad.append(f"{p.name}: signature or content does not verify")
    return good, bad


async def purged_ids(session: AsyncSession) -> set[str]:
    """Account ids this database knows were purged (for the directory sync)."""
    from velaris_shared.db.models import DeletionLedgerModel
    return {str(a) for a in (await session.execute(select(DeletionLedgerModel.account_id))).scalars().all()}


async def purged_name_hashes(session: AsyncSession) -> set[str]:
    from velaris_shared.db.models import DeletionLedgerModel
    out = set()
    for text in (await session.execute(select(DeletionLedgerModel.record))).scalars().all():
        try:
            out.add(json.loads(text)["name_hash"])
        except (ValueError, KeyError):
            continue
    return out


# ── the sign-in check's view: purged ids, from the files ────────────────────

_CACHE: dict[str, Any] = {"at": 0.0, "ids": frozenset()}
CACHE_SECONDS = 60


def purged_set() -> frozenset[str]:
    """Account ids the ledger FILES say were purged — read by every sign-in
    check (cached CACHE_SECONDS). After a restore, a purged account that came
    back with the set is refused from the first request, before the replay has
    run. A folder that cannot be read is an empty set (the replay still runs)."""
    import time
    now = time.monotonic()
    if now - _CACHE["at"] >= CACHE_SECONDS:
        try:
            ids, _bad = load([ledger_dir()])
            _CACHE["ids"] = frozenset(ids)
        except Exception as exc:  # noqa: BLE001
            log.warning("deletion ledger unreadable (%s): sign-in checks fall back to the database", exc)
        _CACHE["at"] = now
    return _CACHE["ids"]


def reset_cache_for_tests() -> None:
    _CACHE.update(at=0.0, ids=frozenset())


async def replay_local(session: AsyncSession) -> list[str]:
    """Purge every account the local ledger names that the database still holds
    (a restore brought it back). Run by user-service at start, then each hour."""
    from velaris_shared.db.models import HelixUserModel
    from velaris_shared.identity import lifecycle
    records, bad = load([ledger_dir()])
    for b in bad:
        log.error("deletion ledger: refused %s", b)
    done = []
    for account_id in sorted(records):
        user = await session.get(HelixUserModel, uuid.UUID(account_id))
        if user is None:
            continue
        try:
            await lifecycle.purge_user(session, user, actor="replay:deletion-ledger")
            await session.commit()
            done.append(account_id)
            log.warning("deletion ledger: purged %s again — a restore had brought it back", account_id)
        except Exception:  # noqa: BLE001
            await session.rollback()
            log.exception("deletion ledger: replaying %s failed — retried next run", account_id)
    return done
