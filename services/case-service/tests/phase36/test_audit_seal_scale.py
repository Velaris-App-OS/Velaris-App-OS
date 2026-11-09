"""Audit-chain sealer at scale (vx-load, 2026-11-06).

`_unsealed_audit_rows` was `id NOT IN (every sealed id)` with no limit — one
call ran 50 min on 3 cores at 1.58M rows, holding the seal lock — and the daily
anchor sealed one 10,000-row batch a day, so a busy install never caught up.
Now: an anti-join with the limit in the query, and the anchor seals every
pending row in committed batches first.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql

from case_service.compliance import audit_chain as ac
from velaris_shared.db import repository as repo
from velaris_shared.db.models import (
    AuditChainModel, CaseAuditLogModel, CaseInstanceModel, CaseTypeModel,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def case(session):
    ct = CaseTypeModel(name="Seal-Scale", version="1.0.0", lifecycle_process_id="lp-seal",
                       definition_json={"stages": []})
    session.add(ct); await session.commit()
    c = CaseInstanceModel(case_type_id=ct.id, case_type_version="1.0.0",
                          status="new", priority="medium", data={})
    session.add(c); await session.commit()
    return c


async def _emit(session, case_id, n, action="a"):
    for i in range(n):
        await repo.append_audit_entry(session, data={
            "case_id": case_id, "action": f"{action}{i}", "actor_id": "alice",
            "actor_type": "user", "details": {},
        })


async def _chained(session) -> int:
    return (await session.execute(select(func.count()).select_from(AuditChainModel))).scalar_one()


async def _unsealed(session) -> int:
    return len(await ac._unsealed_audit_rows(session, 1_000_000))


async def test_query_is_an_anti_join_with_the_limit_in_sql(session, monkeypatch):
    seen = []
    real = session.execute

    async def spy(stmt, *a, **kw):
        seen.append(str(stmt.compile(dialect=postgresql.dialect())))
        return await real(stmt, *a, **kw)

    monkeypatch.setattr(session, "execute", spy)
    await ac._unsealed_audit_rows(session, 7)
    sql = seen[-1].upper()
    assert "NOT (EXISTS" in sql or "NOT EXISTS" in sql
    assert " NOT IN " not in sql
    assert "LIMIT" in sql


async def test_a_batch_takes_the_oldest_rows_only(session, case):
    await _emit(session, case.id, 5)
    rows = await ac._unsealed_audit_rows(session, 3)
    assert len(rows) == 3
    keys = [(r.timestamp, r.id) for r in rows]
    assert keys == sorted(keys)
    everything = await ac._unsealed_audit_rows(session, 1_000_000)
    assert [r.id for r in rows] == [r.id for r in everything[:3]]


async def test_a_late_older_row_is_still_sealed(session, case):
    await _emit(session, case.id, 3)
    await ac.seal_new_entries(session); await session.commit()
    # Committed after the seal, but stamped earlier than every sealed row.
    session.add(CaseAuditLogModel(
        case_id=case.id, action="late", actor_id="bob", actor_type="user", details={},
        timestamp=datetime.now(timezone.utc) - timedelta(days=1)))
    await session.commit()
    assert (await ac.seal_new_entries(session))["sealed"] == 1
    await session.commit()
    assert await _unsealed(session) == 0
    assert (await ac.verify_chain(session))["verified"] is True


async def test_seal_all_pending_clears_a_backlog_bigger_than_a_batch(session, case):
    from tests.conftest import TestSessionFactory

    await _emit(session, case.id, 11)
    await session.commit()
    before = await _chained(session)
    sealed = await ac.seal_all_pending(TestSessionFactory, batch=4)
    assert sealed >= 11
    assert await _unsealed(session) == 0
    assert await _chained(session) - before == sealed
    assert (await ac.verify_chain(session))["verified"] is True
    # Nothing left: a second run seals nothing.
    assert await ac.seal_all_pending(TestSessionFactory, batch=4) == 0


async def test_verify_across_blocks_finds_tampering_and_honours_limit(session, case, monkeypatch):
    monkeypatch.setattr(ac, "_VERIFY_BLOCK", 3)
    await _emit(session, case.id, 10)
    await ac.seal_new_entries(session); await session.commit()
    base = await ac.verify_chain(session)
    assert base["verified"] is True
    n = base["chain_length"]
    assert n >= 10 and base["tip_sequence"] == n

    # Tamper with a row sealed in the middle of the chain (not in block 1).
    link = (await session.execute(
        select(AuditChainModel).where(AuditChainModel.sequence == n - 4))).scalar_one()
    row = await session.get(CaseAuditLogModel, link.audit_log_id)
    row.action = "rewritten"
    await session.commit()
    out = await ac.verify_chain(session)
    assert out["verified"] is False
    assert [b["sequence"] for b in out["breaks"]] == [n - 4]
    assert out["breaks"][0]["type"] == "content_tampered"

    # A limit stops at that many links, across blocks.
    lim = await ac.verify_chain(session, limit=5)
    assert lim["chain_length"] == 5 and lim["tip_sequence"] == 5


async def test_verify_keeps_nothing_in_the_session(session, case):
    await _emit(session, case.id, 6)
    await ac.seal_new_entries(session); await session.commit()
    session.expunge_all()
    await ac.verify_chain(session)
    assert not [o for o in session.identity_map.values()
                if isinstance(o, (AuditChainModel, CaseAuditLogModel))]
