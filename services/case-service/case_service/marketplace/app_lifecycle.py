"""Marketplace app data lifecycle — the "revoke + delete data" teardown lane.

Uninstall offers two modes (see `revoke_install`):
  * revoke         — close the gate, keep the app's data (re-install is instant)
  * revoke+delete  — also delete the app's own data via the teardown registered here

Only first-party official apps register a teardown: their data shape is known and
the deletion SQL is Velaris-authored. HxTest has no tables of its own (it reuses the
core Test Suite's `hxtest_*` tables), so its "data" is the AI-generated suites it
produced — those are deleted; the core builtin/conformance suites are untouched.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from typing import Awaitable, Callable

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import TestResultModel, TestRunModel, TestSuiteModel


async def _teardown_hxtest(session: AsyncSession, tenant_id: str) -> dict:
    """Delete HxTest's own data: the AI-generated suites + their runs/results.

    Core suites (`source` in builtin/developer — smoke/component/security/
    conformance) are left intact; only `ai_generated` rows go.

    NOTE: `hxtest_suites` has no tenant_id column (generated suites are global), so
    this deletes ALL ai_generated suites, not just `tenant_id`'s. Fine for single-
    tenant; a multi-tenant deployment would need a tenant_id column on the suite to
    scope this. `tenant_id` is accepted now for that future scoping."""
    suite_ids = (await session.execute(
        select(TestSuiteModel.id).where(TestSuiteModel.source == "ai_generated")
    )).scalars().all()
    n_suites = n_runs = n_results = 0
    if suite_ids:
        run_ids = (await session.execute(
            select(TestRunModel.id).where(TestRunModel.suite_id.in_(suite_ids))
        )).scalars().all()
        if run_ids:
            n_results = (await session.execute(
                delete(TestResultModel).where(TestResultModel.run_id.in_(run_ids))
            )).rowcount or 0
            n_runs = (await session.execute(
                delete(TestRunModel).where(TestRunModel.id.in_(run_ids))
            )).rowcount or 0
        n_suites = (await session.execute(
            delete(TestSuiteModel).where(TestSuiteModel.id.in_(suite_ids))
        )).rowcount or 0
    return {"deleted": True, "suites": n_suites, "runs": n_runs, "results": n_results}


# package_id → teardown coroutine. Unlisted packages have no data teardown.
_TEARDOWNS: dict[str, Callable[[AsyncSession, str], Awaitable[dict]]] = {
    "velaris/hxtest": _teardown_hxtest,
}


async def teardown_package_data(session: AsyncSession, package_id: str, tenant_id: str) -> dict:
    """Run the registered data teardown for a package, if any. Caller commits."""
    fn = _TEARDOWNS.get(package_id)
    if fn is None:
        return {"deleted": False, "reason": "no data teardown registered for this package"}
    return await fn(session, tenant_id)
