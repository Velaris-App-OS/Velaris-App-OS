"""Test Suite (#27, Part B) — regeneration + staleness hooks.

Wired into case-type / rule / connector / email-account change paths. Per the
"auto structural / manual AI" decision:
  * structural tests depend only on the case-type definition → auto-regenerated
    when the case type is (re)published;
  * AI scenarios depend on rules/integrations/email → those changes only FLAG the
    suite's AI layer stale (a Studio badge); the user regenerates AI manually.

Every entrypoint is wrapped so a regen failure can NEVER break the host request
(case-type save, rule edit, etc.). Call AFTER the host has committed its change.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


def _as_uuid(v) -> uuid.UUID | None:
    if v is None:
        return None
    if isinstance(v, uuid.UUID):
        return v
    try:
        return uuid.UUID(str(v))
    except (ValueError, AttributeError, TypeError):
        return None


def _has_ai(definition: list | None) -> bool:
    return any(t.get("generated_by") not in (None, "structural") for t in (definition or []))


async def regenerate_structural(session: AsyncSession, case_type_id) -> bool:
    """Rebuild the structural test(s) of a case type's EXISTING generated suite,
    preserving any AI scenarios and flagging them stale. No-op if no generated
    suite exists yet (nothing has been generated for this case type)."""
    from case_service.db.models import CaseTypeModel, TestSuiteModel
    from case_service.testsuite.generator import generate_structural

    ctid = _as_uuid(case_type_id)
    if ctid is None:
        return False
    suite = (await session.execute(select(TestSuiteModel).where(
        TestSuiteModel.case_type_id == ctid,
        TestSuiteModel.suite_type == "generated"))).scalar_one_or_none()
    if suite is None:
        return False
    ct = await session.get(CaseTypeModel, ctid)
    if ct is None:
        return False
    structural = generate_structural(str(ctid), ct.definition_json or {})
    ai_tests = [t for t in (suite.definition or []) if t.get("generated_by") not in (None, "structural")]
    suite.definition = structural + ai_tests
    suite.version = ct.version
    if ai_tests:
        suite.ai_stale = True
    await session.commit()
    return True


async def mark_ai_stale(session: AsyncSession, case_type_id=None) -> int:
    """Flag AI scenarios stale on generated suite(s) that actually contain AI tests.
    `case_type_id=None` flags ALL generated suites (used for connector/email changes,
    which can affect any case type's scenarios). Returns the count flagged."""
    from case_service.db.models import TestSuiteModel

    q = select(TestSuiteModel).where(TestSuiteModel.suite_type == "generated")
    ctid = _as_uuid(case_type_id)
    if ctid is not None:
        q = q.where(TestSuiteModel.case_type_id == ctid)
    suites = (await session.execute(q)).scalars().all()
    n = 0
    for s in suites:
        if _has_ai(s.definition) and not s.ai_stale:
            s.ai_stale = True
            n += 1
    if n:
        await session.commit()
    return n


async def on_case_type_changed(session: AsyncSession, case_type_id) -> None:
    """Case-type (re)published: structural depends on the definition → regenerate it."""
    try:
        await regenerate_structural(session, case_type_id)
    except Exception:
        logger.warning("test-suite structural regen failed (non-fatal)", exc_info=True)


async def on_scenario_source_changed(session: AsyncSession, case_type_id=None) -> None:
    """Rule/connector/email change: structural is unaffected → flag AI scenarios stale
    (manual AI regen). `case_type_id=None` → all generated suites."""
    try:
        await mark_ai_stale(session, case_type_id)
    except Exception:
        logger.warning("test-suite AI-stale flagging failed (non-fatal)", exc_info=True)


# ── Background-task entrypoints (open a fresh session; run AFTER the response) ──
# Used via FastAPI BackgroundTasks so the host request commits first and the regen
# reads committed data on its OWN engine/session — never the host's. We build the
# session from get_engine() (same pattern as case_types._sync_lifecycle_docs), NOT
# get_session_factory(): the test harness overrides the factory globals to a shared
# StaticPool connection, and a second session on that shared connection corrupts the
# host request's committed state. get_engine() gives an independent connection.

def _bg_session_factory():
    from case_service.db.session import get_engine
    from sqlalchemy.ext.asyncio import AsyncSession as _S, async_sessionmaker
    return async_sessionmaker(get_engine(), class_=_S, expire_on_commit=False)


async def bg_case_type_changed(case_type_id) -> None:
    try:
        async with _bg_session_factory()() as s:
            await on_case_type_changed(s, case_type_id)
    except Exception:
        logger.warning("test-suite regen (bg, case-type) failed (non-fatal)", exc_info=True)


async def bg_scenario_source_changed(case_type_id=None) -> None:
    try:
        async with _bg_session_factory()() as s:
            await on_scenario_source_changed(s, case_type_id)
    except Exception:
        logger.warning("test-suite regen (bg, scenario-source) failed (non-fatal)", exc_info=True)
