"""Test Suite (#27) — the runner: orchestrate a suite, enforce isolation, persist.

`iter_run` is an async generator that yields progress events (for SSE) while
persisting the run + results; `run_suite` drives it to completion. Mutating runs
provision ephemeral tenants and force-teardown in a finally block (decision D1).

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import TestRunModel, TestResultModel
from case_service.testsuite import dsl, executor

logger = logging.getLogger(__name__)


async def iter_run(
    session: AsyncSession,
    suite_def: list,
    *,
    suite_name: str,
    triggered_by: str,
    suite_id: uuid.UUID | None = None,
    app_package_id: uuid.UUID | None = None,
    clients: dict | None = None,
    ephemeral_tenant_id: str | None = None,
) -> AsyncIterator[dict]:
    """Run a parsed suite, yielding {event,...} records and persisting as it goes.

    Pure orchestration: the caller (API layer or test) supplies per-identity
    `clients` and owns isolation (provision before / force-teardown after). This
    keeps the runner free of transport/provisioning concerns.
    """
    tests = dsl.parse_suite(suite_def)  # validates; raises DslError on bad defs

    run = TestRunModel(
        suite_id=suite_id, suite_name=suite_name, triggered_by=triggered_by,
        app_package_id=app_package_id, status="running", total=len(tests),
        ephemeral_tenant_id=ephemeral_tenant_id,
    )
    session.add(run)
    # Commit eagerly: the executor drives app sub-requests that commit/rollback on
    # their own sessions; a 4xx (e.g. the security suite's 401/403 checks) rolls
    # back the request transaction. Persisting the run/results in their own
    # committed transactions keeps them independent of the tests being run.
    await session.commit()
    yield {"event": "run_started", "run_id": str(run.id), "total": len(tests)}

    try:
        passed = failed = skipped = errored = 0
        for test in tests:
            res = await executor.execute_test(clients or {}, test)
            session.add(TestResultModel(
                run_id=run.id, test_id=res["test_id"], test_name=res["test_name"],
                status=res["status"], duration_ms=res["duration_ms"],
                error_detail=res["error_detail"], step_results=res["step_results"],
            ))
            await session.commit()
            if res["status"] == "passed":
                passed += 1
            elif res["status"] == "failed":
                failed += 1
            elif res["status"] == "skipped":
                skipped += 1
            else:
                errored += 1
            yield {"event": "test_result", "test_id": res["test_id"],
                   "status": res["status"], "error_detail": res["error_detail"]}

        run.passed, run.failed, run.skipped = passed, failed, skipped + errored
        if failed == 0 and errored == 0:
            run.status = "passed"
        elif passed == 0:
            run.status = "failed"
        else:
            run.status = "partial"
    except Exception as e:  # noqa: BLE001
        run.status = "error"
        logger.exception("Test Suite run errored")
        yield {"event": "run_error", "detail": f"{type(e).__name__}: {e}"}
    finally:
        from datetime import datetime, timezone
        run.completed_at = datetime.now(timezone.utc)
        await session.commit()

    yield {"event": "run_complete", "run_id": str(run.id), "status": run.status,
           "passed": run.passed, "failed": run.failed, "skipped": run.skipped}


async def run_suite(session: AsyncSession, suite_def: list, **kw) -> TestRunModel:
    """Drive iter_run to completion; return the persisted TestRunModel."""
    run_id = None
    async for ev in iter_run(session, suite_def, **kw):
        if ev["event"] == "run_started":
            run_id = uuid.UUID(ev["run_id"])
    return await session.get(TestRunModel, run_id)
