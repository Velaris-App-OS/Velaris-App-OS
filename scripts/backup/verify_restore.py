#!/usr/bin/env python3
# Copyright (c) 2024-2026 Velaris Contributors
# SPDX-License-Identifier: BSL-1.1
"""Checks that a restored install is the install that was backed up.

Run by `velaris_backup.py verify` with the repo venv, PYTHONPATH set to
case-service and the working directory at the install root (so the restored
.env is what the platform code reads). Prints one JSON object:

    {"checks": [{"name", "ok", "detail"}...], "ok": bool}

Checks, each independent (one failing does not hide the others):
  row_counts   every table's row count equals the manifest's
  chains       AI ledger, HxArbiter, audit and healing hash chains verify
  document     a random stored document is read AND decrypted by the platform's
               own document service (proves the storage master key and the KEK)
  vault_keys   the tenants' HxVault keys open with the restored KEK — every
               encrypted field (credentials, ledger transcripts) is under them
  login        a short-lived, role-less token signed with the restored key is
               accepted by the running platform (GET /auth/me through the
               gateway) — only with --gateway; skipped otherwise
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import traceback


async def _row_counts(session, manifest: dict) -> tuple[bool, str]:
    from sqlalchemy import text
    want: dict[str, int] = manifest.get("database", {}).get("row_counts", {})
    if not want:
        return False, "manifest has no row counts"
    exact = manifest["database"].get("snapshot_consistent_counts", False)
    backend = manifest["database"].get("backend", "postgresql")
    diffs = []
    for table, n in sorted(want.items()):
        q = f'SELECT COUNT(*) FROM "{table}"' if backend == "postgresql" else f"SELECT COUNT(*) FROM `{table}`"
        try:
            got = (await session.execute(text(q))).scalar_one()
        except Exception as exc:
            await session.rollback()
            diffs.append(f"{table}: missing ({type(exc).__name__})")
            continue
        if got != n:
            diffs.append(f"{table}: {got} != {n}")
    if not diffs:
        return True, f"{len(want)} tables, {sum(want.values())} rows, all equal"
    if not exact:
        # MySQL/MariaDB counts are not from the dump's own snapshot.
        return True, f"{len(diffs)} table(s) differ (counts not snapshot-exact on this backend): " + "; ".join(diffs[:5])
    return False, f"{len(diffs)} table(s) differ: " + "; ".join(diffs[:10])


async def _chain_states(session) -> dict:
    """{name: {"verified": bool, "breaks": int, "detail": str}} for every chain."""
    from case_service.compliance.audit_chain import verify_chain as audit
    from case_service.hxarbiter.chain import verify_chain as arbiter
    from case_service.ledger.chain import verify_chain as ledger
    from case_service.healing.service import verify_chain as healing
    out = {}
    for name, fn in (("ai-ledger", ledger), ("hxarbiter", arbiter), ("audit", audit), ("healing", healing)):
        try:
            r = await fn(session)
            if isinstance(r, tuple):
                out[name] = {"verified": bool(r[0]), "breaks": 0 if r[0] else 1, "detail": str(r[1] or "")[:120]}
            else:
                br = r.get("breaks") or []
                out[name] = {"verified": bool(r.get("verified", False)),
                             "breaks": len(br) if isinstance(br, list) else (0 if r.get("verified") else 1),
                             "detail": str(br[:1] if isinstance(br, list) else br)[:120]}
        except Exception as exc:
            await session.rollback()
            out[name] = {"verified": False, "breaks": -1, "detail": f"{type(exc).__name__}: {str(exc)[:80]}"}
    return out


async def _chains(session, manifest: dict) -> tuple[bool, str]:
    """Every chain must be exactly as it was when the backup was taken.

    A chain that was already broken at backup time is reproduced faithfully by
    a good restore, so the test is equality with the backup-time state (the
    manifest's probe), not "verified". A set without a probe (older engine)
    falls back to "verified"."""
    now = await _chain_states(session)
    then = (manifest.get("probe") or {}).get("chains")
    parts, ok = [], True
    for name, st in now.items():
        if then and name in then:
            same = (st["verified"], st["breaks"]) == (then[name]["verified"], then[name]["breaks"])
            ok = ok and same
            tag = ("ok" if st["verified"] else f"{st['breaks']} break(s), as at backup time") if same else \
                  f"CHANGED: now verified={st['verified']} breaks={st['breaks']}, at backup " \
                  f"verified={then[name]['verified']} breaks={then[name]['breaks']}"
        else:
            ok = ok and st["verified"]
            tag = "ok" if st["verified"] else f"BROKEN ({st['detail']})"
        parts.append(f"{name}={tag}")
    return ok, ", ".join(parts)


async def _vault_state(session) -> dict:
    """{"active": n, "unwrapped": m} — the tenants' HxVault keys (DEKs) and how
    many the KEK in this .env opens. Every encrypted field of a tenant (connector,
    bot and migration credentials, AI ledger transcripts) is under its DEK."""
    from sqlalchemy import select
    from velaris_shared.db.models import TenantDekModel
    from velaris_shared.hxvault import crypto, keyring
    rows = (await session.execute(
        select(TenantDekModel).where(TenantDekModel.status == "active"))).scalars().all()
    kek, opened = crypto.get_kek(), 0
    for row in rows:
        try:
            crypto.open_(kek, keyring._b64d(row.wrapped_dek), keyring._wrap_aad(row.tenant_id))
            opened += 1
        except Exception:  # noqa: BLE001 — counted, not raised
            pass
    return {"active": len(rows), "unwrapped": opened}


async def _vault(session, manifest: dict) -> tuple[bool, str]:
    """The tenant keys that opened at backup time must open now. Found in the
    R5b drill (2026-10-30): with the KEK lost, every encrypted field was
    unreadable and the restore still said "Verified" — nothing checked them."""
    now = await _vault_state(session)
    then = (manifest.get("probe") or {}).get("vault")
    need = then["unwrapped"] if then else now["active"]
    if not now["active"] and not need:
        return True, "no tenant keys (no encrypted fields yet)"
    if now["unwrapped"] >= need:
        return True, f"{now['unwrapped']} of {now['active']} tenant keys open with the restored KEK"
    return False, (f"only {now['unwrapped']} of {now['active']} tenant keys open (at backup time "
                   f"{need}) — the KEK in .env is not the one they were made with: every "
                   "encrypted field of those tenants is unreadable")


async def _readable_documents(session, limit: int = 5, before=None) -> list[str]:
    """Up to `limit` stored documents that the platform reads AND decrypts now.
    `before`: only documents created before then (the dump's start), so the
    probe never picks one the dump cannot contain."""
    from sqlalchemy import select, func
    from velaris_shared.db.models import DocumentModel
    from case_service.documents import DocumentService
    q = select(DocumentModel.id).where(DocumentModel.is_deleted.is_(False))
    if before is not None:
        q = q.where(DocumentModel.created_at < before)
    ids = (await session.execute(q.order_by(func.random()).limit(limit * 4))).scalars().all()
    good = []
    for i in ids:
        try:
            data, _, _ = await DocumentService().download(session, i)
            if data:
                good.append(str(i))
        except Exception:
            await session.rollback()
        if len(good) >= limit:
            break
    return good


async def _document(session, manifest: dict) -> tuple[bool, str]:
    """The documents that opened at backup time must open now — proving the
    store, the storage master key and the KEK all came back together."""
    import uuid
    from case_service.documents import DocumentService
    probe = (manifest.get("probe") or {}).get("documents")
    if probe is None:
        got = await _readable_documents(session, 1)
        return (True, f"document {got[0]} read and decrypted") if got else (True, "no readable document to try")
    if not probe:
        return True, "no documents were readable at backup time — nothing to compare"
    bad = []
    for i in probe:
        try:
            data, _, _ = await DocumentService().download(session, uuid.UUID(i))
            if not data:
                bad.append(f"{i}: empty")
        except Exception as exc:
            await session.rollback()
            bad.append(f"{i}: {type(exc).__name__}")
    if bad:
        return False, f"{len(bad)}/{len(probe)} documents that opened at backup time do not now: " + "; ".join(bad[:3])
    return True, f"all {len(probe)} documents that opened at backup time read and decrypt"


async def _login(session, gateway: str) -> tuple[bool, str]:
    import httpx
    from sqlalchemy import select
    from velaris_shared.config import get_settings
    from velaris_shared.auth.jwt_handler import create_dev_token
    from velaris_shared.db.models import HelixUserModel as UserModel
    s = get_settings()
    user = (await session.execute(
        select(UserModel).where(UserModel.is_active.is_(True)).limit(1))).scalars().first()
    if user is None:
        return False, "no users in the restored database"
    key = getattr(s, "auth_rsa_private_key", "") or None
    token = create_dev_token(str(user.id), getattr(user, "username", "verify"), roles=[],
                             private_key=key, expire_minutes=2) if key else None
    if token is None:
        return False, "no RS256 signing key in the restored .env"
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{gateway.rstrip('/')}/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    return r.status_code == 200, f"GET /auth/me as an existing user → {r.status_code}"


async def probe(before: str | None = None) -> int:
    """Backup-time state, stored in the manifest so a restore can be compared
    with what was there rather than with an ideal."""
    from velaris_shared.db.session import get_session_factory
    factory = get_session_factory()
    async with factory() as session:
        chains = await _chain_states(session)
    async with factory() as session:
        try:
            vault = await _vault_state(session)
        except Exception:  # noqa: BLE001 — an older schema: checked against ideals
            await session.rollback()
            vault = None
    async with factory() as session:
        import datetime as _dt
        b = _dt.datetime.fromisoformat(before) if before else None
        docs = await _readable_documents(session, 5, b)
    print(json.dumps({"chains": chains, "documents": docs, "vault": vault}))
    return 0


async def main(manifest_path: str, gateway: str | None, only: set[str] | None = None,
               skip: set[str] | None = None) -> int:
    from velaris_shared.db.session import get_session_factory
    manifest = json.load(open(manifest_path))
    checks = []
    factory = get_session_factory()
    for name, fn in (("row_counts", lambda s: _row_counts(s, manifest)),
                     ("chains", lambda s: _chains(s, manifest)),
                     ("document", lambda s: _document(s, manifest)),
                     ("vault_keys", lambda s: _vault(s, manifest)),
                     ("login", (lambda s: _login(s, gateway)) if gateway else None)):
        if (only and name not in only) or (skip and name in skip):
            continue
        if fn is None:
            checks.append({"name": name, "ok": None, "detail": "skipped (platform not started)"})
            continue
        async with factory() as session:
            try:
                ok, detail = await fn(session)
            except Exception as exc:
                ok, detail = False, f"{type(exc).__name__}: {exc}"[:300]
                if "--trace" in sys.argv:
                    traceback.print_exc()
        checks.append({"name": name, "ok": ok, "detail": detail})
    result = {"checks": checks, "ok": all(c["ok"] is not False for c in checks)}
    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    if sys.argv[1:2] == ["--probe"]:
        sys.exit(asyncio.run(probe(sys.argv[2] if len(sys.argv) > 2 else None)))
    p = argparse.ArgumentParser()
    p.add_argument("manifest")
    p.add_argument("--gateway", help="e.g. http://127.0.0.1:8200 — runs the login check")
    p.add_argument("--trace", action="store_true")
    p.add_argument("--only", help="comma-separated checks to run")
    p.add_argument("--skip", help="comma-separated checks to leave out")
    a = p.parse_args()
    sys.exit(asyncio.run(main(a.manifest, a.gateway,
                              set(a.only.split(",")) if a.only else None,
                              set(a.skip.split(",")) if a.skip else None)))
