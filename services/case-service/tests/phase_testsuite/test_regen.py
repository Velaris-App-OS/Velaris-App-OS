"""#27 Part B — regeneration + AI-staleness helpers.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest

from sqlalchemy import select

from case_service.db.models import TestSuiteModel
from case_service.testsuite import regen


def _gen_suite(ct_id, *, with_ai: bool):
    tests = [{"id": "structural-x", "generated_by": "structural", "steps": []}]
    if with_ai:
        tests.append({"id": "scenario-1", "generated_by": "hxnexus", "steps": []})
    return TestSuiteModel(name="Generated · X", suite_type="generated",
                          source="ai_generated" if with_ai else "structural",
                          case_type_id=ct_id, definition=tests, version="1.0.0", ai_stale=False)


@pytest.mark.asyncio
async def test_mark_ai_stale_only_flags_suites_with_ai(session):
    ct_ai, ct_plain = uuid.uuid4(), uuid.uuid4()
    session.add_all([_gen_suite(ct_ai, with_ai=True), _gen_suite(ct_plain, with_ai=False)])
    await session.commit()
    n = await regen.mark_ai_stale(session, None)        # all generated suites
    assert n == 1                                        # only the one WITH ai scenarios
    rows = {s.case_type_id: s.ai_stale for s in (await session.execute(
        select(TestSuiteModel))).scalars().all()}
    assert rows[ct_ai] is True and rows[ct_plain] is False


@pytest.mark.asyncio
async def test_mark_ai_stale_scoped_to_case_type(session):
    ct_a, ct_b = uuid.uuid4(), uuid.uuid4()
    session.add_all([_gen_suite(ct_a, with_ai=True), _gen_suite(ct_b, with_ai=True)])
    await session.commit()
    n = await regen.mark_ai_stale(session, ct_a)         # only ct_a
    assert n == 1


@pytest.mark.asyncio
async def test_regenerate_structural_preserves_ai_and_flags_stale(session, client):
    r = await client.post("/api/v1/case-types", json={
        "name": "Regen CT", "version": "2.0.0", "lifecycle_process_id": str(uuid.uuid4()),
        "definition_json": {"stages": [{"id": "s1", "name": "Open"}]}, "default_priority": "medium"})
    ct_id = uuid.UUID(r.json()["id"])
    session.add(_gen_suite(ct_id, with_ai=True))
    await session.commit()

    ok = await regen.regenerate_structural(session, ct_id)
    assert ok is True
    suite = (await session.execute(
        select(TestSuiteModel).where(
            TestSuiteModel.case_type_id == ct_id))).scalar_one()
    # structural rebuilt from the (single-stage) definition; AI scenario preserved
    assert any(t["generated_by"] == "structural" for t in suite.definition)
    assert any(t["generated_by"] == "hxnexus" for t in suite.definition)
    assert suite.version == "2.0.0"
    assert suite.ai_stale is True                        # AI now stale vs new structural


@pytest.mark.asyncio
async def test_regenerate_structural_noop_without_suite(session):
    assert await regen.regenerate_structural(session, uuid.uuid4()) is False
