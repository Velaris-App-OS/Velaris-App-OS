"""HxGuard Phase C (#16-C) — one authorization generation across every service.

**What this is guarding against, concretely.** Before this, each service's
policy decision point was an island:

* `role_privileges_cron` was started only in case-service's lifespan, so the
  role → privilege store was permanently EMPTY in analytics-, hxgraph- and
  hxmigrate-service. Proven live on 2026-08-23: one grant in the Platform Roles
  store took case-service's `/status/authz-parity` from 403 to 200 while
  analytics-service's `/process-mining/variants` stayed 403 forever. An admin
  granting a privilege saw it work on some routes and silently not on others.
* `invalidate_cache()` clears the calling process's cache only, so a revoke made
  in case-service left the other three answering from stale entries.

The fix is a counter in one row. These tests hold its three properties: a
mutation bumps it, a poll notices, and every service runs the loops that make
those two things meet.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import pytest

from velaris_shared.hxguard import generation as gen
from velaris_shared.hxguard import service as hxguard_service
from velaris_shared.db.models import HxGuardAuthzGenerationModel


@pytest.fixture(autouse=True)
def _reset_seen():
    """The module remembers the last generation it saw; tests must not share it."""
    gen._seen = None
    yield
    gen._seen = None


async def _seed(session) -> None:
    from sqlalchemy import select

    row = (await session.execute(
        select(HxGuardAuthzGenerationModel)
        .where(HxGuardAuthzGenerationModel.id == gen.ROW_ID)
    )).scalar_one_or_none()
    if row is None:
        session.add(HxGuardAuthzGenerationModel(id=gen.ROW_ID, generation=0))
        await session.flush()


class TestGenerationPropagation:

    async def test_bump_increments_the_shared_counter(self, session):
        await _seed(session)
        before = await gen.read_generation(session)
        await gen.bump(session, reason="test.bump", actor="tester")
        after = await gen.read_generation(session)
        assert after == before + 1

    async def test_first_poll_does_not_invalidate(self, session):
        """A service starting up must not clear a cache it just built.

        `_seen` is None until the first successful read, and adopting that value
        is not a change — otherwise every service would drop its cache once at
        boot for no reason, which looks like a thundering herd and is one.
        """
        await _seed(session)
        assert await gen.poll_once(session) is False
        assert gen._seen is not None

    async def test_a_bump_elsewhere_clears_this_process_cache(self, session):
        """The whole point: a change made by another process reaches this one."""
        await _seed(session)
        await gen.poll_once(session)               # adopt the current value

        # Something this process cannot see happens — another service's commit.
        await gen.bump(session, reason="test.remote", actor="other-service")

        hxguard_service._cache[("probe",)] = ("stale", 1e18)
        assert await gen.poll_once(session) is True
        assert hxguard_service._cache == {}, "the decision cache was not cleared"

    async def test_a_repeat_poll_is_a_no_op(self, session):
        await _seed(session)
        await gen.poll_once(session)
        hxguard_service._cache[("probe",)] = ("kept", 1e18)
        assert await gen.poll_once(session) is False
        assert ("probe",) in hxguard_service._cache, "an unchanged generation cleared the cache"

    async def test_an_unreadable_generation_keeps_the_cache(self, session):
        """Fail OPEN on a read error.

        Clearing on failure would turn a database blip into every service
        re-evaluating every decision at once. The staleness bound in that window
        is the 30-second TTL that existed before this mechanism.
        """
        await _seed(session)
        await gen.poll_once(session)
        hxguard_service._cache[("probe",)] = ("kept", 1e18)

        class _Boom:
            async def execute(self, *a, **k):
                raise RuntimeError("database gone")

        assert await gen.poll_once(_Boom()) is False
        assert ("probe",) in hxguard_service._cache

    async def test_a_failed_bump_does_not_break_the_mutation(self, session):
        """A mutation must never fail because the notification failed.

        The change is still correct where it was made; the other services fall
        back to their TTL. Refusing the write would be strictly worse.
        """
        class _Boom:
            async def execute(self, *a, **k):
                raise RuntimeError("database gone")

        await gen.bump(_Boom(), reason="test.failure")   # must not raise


class TestEveryServiceRunsTheLoops:
    """The loops are the fix. A service that does not run them is the defect."""

    @pytest.mark.parametrize("package", [
        "analytics_service", "hxgraph_service", "hxmigrate_service",
    ])
    def test_extracted_service_uses_the_shared_lifespan(self, package):
        import importlib
        import inspect

        main = importlib.import_module(f"{package}.main")
        src = inspect.getsource(main)
        assert "platform_lifespan(" in src, (
            f"{package} does not use velaris_shared.app.platform_lifespan, so it "
            f"never starts the role-privilege store or the generation poll — its "
            f"authorization data would be permanently stale."
        )

    def test_case_service_polls_the_generation_too(self):
        """case-service makes the mutations, but it must still hear the others.

        HxGraph writes tuples on sync, and a connector deleted in another
        service is an authorization input change here.
        """
        import inspect

        import case_service.main as main

        src = inspect.getsource(main)
        assert "generation_cron" in src, (
            "case-service does not poll the shared authz generation."
        )

    async def test_the_loops_start(self):
        from velaris_shared.app import start_authz_loops

        tasks = start_authz_loops("test-service")
        try:
            # role store + authz generation + platform generations (step 5).
            assert len(tasks) == 3, f"expected all three loops, got {len(tasks)}"
        finally:
            for t in tasks:
                t.cancel()

    async def test_the_shared_lifespan_actually_starts_them(self, monkeypatch):
        """Not "the function exists" — that the lifespan calls it.

        The first version of this file checked only that each service passed
        `platform_lifespan(...)` and that `start_authz_loops()` returned two
        tasks. Both stayed green when the lifespan's call to it was replaced
        with an empty list, which is precisely the regression that would put
        every extracted service back to a permanently empty role store.
        """
        from fastapi import FastAPI

        import velaris_shared.app as shared_app

        called: list[str] = []

        def _record(service_name: str):
            called.append(service_name)
            return []

        monkeypatch.setattr(shared_app, "start_authz_loops", _record)

        app = FastAPI()
        async with shared_app.platform_lifespan("probe-service")(app):
            pass

        assert called == ["probe-service"], (
            "platform_lifespan did not start the authz loops — the role-privilege "
            "store and the generation poll would never run in any extracted service."
        )


class TestEveryDecisionInputMutationBumps:
    """The counter is only worth having if every mutation reaches it.

    The 2026-08-24 re-verification found role privileges, namespace grants,
    tuples and devconn all bumping — and access-group writes bumping nothing,
    even though the evaluator reads `OperatorAccessGroupModel` and
    `AccessGroupModel.allowed_case_type_ids` on the decision path and caches
    the result for 30 seconds in every service. Removing somebody from a group
    left them working for half a minute, platform-wide.

    A handler that clears its own cache but tells nobody else is the exact
    shape of that bug, so that is what this asserts — structurally, over the
    real source, rather than route by route.
    """

    ROUTER_DIR = "services/case-service/case_service/api/routers"

    def _handlers(self):
        import ast
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[4]
        for path in sorted((root / self.ROUTER_DIR).glob("*.py")):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    calls = {
                        ast.unparse(c.func) for c in ast.walk(node)
                        if isinstance(c, ast.Call)
                    }
                    yield path.name, node.name, calls

    def test_no_handler_invalidates_locally_without_bumping(self):
        offenders = [
            f"{f}::{name}" for f, name, calls in self._handlers()
            if any(c.endswith("invalidate_cache") for c in calls)
            and not any(c.endswith("bump") for c in calls)
        ]
        assert not offenders, (
            "these handlers clear their OWN decision cache and leave every other "
            f"service stale for up to 30s: {offenders}. Add a generation bump on "
            "the same session, so the change rides the mutation's transaction."
        )

    @pytest.mark.parametrize("handler", [
        "update_access_group", "delete_access_group",
        "add_member", "remove_member", "put_role_privileges",
    ])
    def test_access_group_writes_bump(self, handler):
        found = [
            calls for f, name, calls in self._handlers()
            if f == "access_groups.py" and name == handler
        ]
        if not found:
            pytest.skip(f"{handler} not present under that name")
        assert any(c.endswith("bump") for c in found[0]), (
            f"{handler} changes an input the evaluator reads but does not bump "
            "the authz generation — the other services keep the old answer."
        )
