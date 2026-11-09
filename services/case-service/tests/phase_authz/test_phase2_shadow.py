"""Privilege authorization Phase 2 — the shadow.

`require_role` still decides. The privilege model answers alongside it and
disagreements are recorded. The Phase-1 parity table ran against *constructed*
subjects; this runs against real requests, real routes and real access groups,
which is where the cases the table cannot reach actually live.

Two things must hold, and the second matters more than the first:

1. The comparison is **accurate** — it resolves the right action for the route
   and reaches the same subject the gate just judged.
2. The comparison is **harmless** — no request outcome changes, and nothing the
   shadow can do (an unmapped route, a broken backend, a dead database) can turn
   an observation feature into an outage.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import pytest

from velaris_shared.hxguard import shadow
from velaris_shared.hxguard.actions import ACTIONS


@pytest.fixture(autouse=True)
def _reset():
    shadow.reset()
    yield
    shadow.reset()


def _headers(*roles: str) -> dict:
    from velaris_shared.auth.jwt_handler import create_dev_token
    from velaris_shared.config import get_settings
    s = get_settings()
    token = create_dev_token(
        user_id=str(uuid.uuid4()), username="shadow-tester", roles=list(roles),
        secret=s.auth_secret, private_key=s.auth_rsa_private_key or "")
    return {"Authorization": f"Bearer {token}"}


# ── it actually runs ──────────────────────────────────────────────
#
# Phase 3 finished on 2026-08-13, so there is no longer a live route whose gate
# is `require_role` — the shadow's HTTP-level tests retired with the gates they
# watched. What remains is exercised directly, which is also what the original
# comment on `_SHADOWED_ROUTE` promised would happen.
#
# The shadow itself is deliberately NOT deleted: it is the mechanism a future
# migration re-uses, and deleting it would mean rebuilding it from the design
# doc the next time a gate changes hands.


def _router_module(file: str):
    """The router module for an indexed gate, in whichever service now holds it.

    R5e step 7 moved checkout and storefront into commerce-service; importing
    them from `case_service` failed for every gate after the move.
    """
    import importlib
    from case_service.hxguard_ledger.legacy_sites import find_router
    path = find_router(file)
    return importlib.import_module(f"{path.parents[2].name}.api.routers.{path.stem}")

class _User:
    def __init__(self, roles, privileges=None):
        self.roles = list(roles)
        self.is_admin = "admin" in self.roles
        self.user_id = "u-1"
        self.active_access_group = _AG(privileges) if privileges is not None else None


class _AG:
    def __init__(self, privileges):
        self.privileges = privileges


def _request_for(handler, path="/x"):
    class Route:
        endpoint = handler

    class URL:
        pass

    url = URL()
    url.path = path

    class Req:
        scope = {"route": Route()}

    r = Req()
    r.url = url
    return r


class TestItRuns:
    @pytest.mark.asyncio
    async def test_it_compares_and_agrees_on_an_allow(self):
        from user_service.api.routers import auth_real
        await shadow.compare(_request_for(auth_real.list_users),
                             _User(["admin"]), ("admin",), True)
        snap = shadow.snapshot()
        assert snap["compared"] == 1 and snap["agreed"] == 1
        assert snap["mismatch_would_allow"] == 0 and snap["mismatch_would_deny"] == 0

    @pytest.mark.asyncio
    async def test_it_compares_and_agrees_on_a_deny(self):
        from user_service.api.routers import auth_real
        await shadow.compare(_request_for(auth_real.list_users),
                             _User(["viewer"]), ("admin",), False)
        snap = shadow.snapshot()
        assert snap["compared"] == 1 and snap["agreed"] == 1

    @pytest.mark.asyncio
    async def test_a_router_level_gate_resolves_and_compares(self):
        """The 12 module gates have no per-handler dependency to notice, so they
        are the ones a comparison most easily skips."""
        from case_service.api.routers import tenants
        handler = next(r.endpoint for r in tenants.router.routes
                       if getattr(r, "endpoint", None))
        await shadow.compare(_request_for(handler), _User(["admin"]), ("admin",), True)
        assert shadow.snapshot()["compared"] == 1

    @pytest.mark.asyncio
    async def test_a_real_spread_of_handlers_agrees(self):
        """The Phase-2 sweep, kept: every indexed gate, every role shape, the
        privilege model against the rule `require_role` used to apply."""
        import importlib
        from velaris_shared.hxguard.actions import ACTION_BY_GATE, ACTIONS
        from velaris_shared.hxguard.privileges import legacy_role_gate_allows

        for gate, action in ACTION_BY_GATE.items():
            file, fn = gate.split("::")
            mod = _router_module(file)
            handler = getattr(mod, fn)
            legacy = ACTIONS[action].legacy_roles
            for roles in (("admin",), ("viewer",), ("designer",), ("security",),
                          ("devops",), ("integration",)):
                expected = legacy_role_gate_allows(roles, legacy)
                await shadow.compare(_request_for(handler), _User(roles), legacy, expected)

        snap = shadow.snapshot()
        assert snap["compared"] == len(ACTION_BY_GATE) * 6, snap
        assert snap["mismatch_would_allow"] == 0, snap
        assert snap["mismatch_would_deny"] == 0, snap
        assert snap["unresolved_route"] == 0, snap
        assert snap["error"] == 0, snap


# ── it is harmless ────────────────────────────────────────────────

class TestItIsHarmless:
    @pytest.mark.asyncio
    async def test_a_broken_backend_is_swallowed(self, monkeypatch):
        from user_service.api.routers import auth_real

        def explode(*a, **k):
            raise RuntimeError("backend is down")
        monkeypatch.setattr(shadow._backend, "evaluate", explode)

        await shadow.compare(_request_for(auth_real.list_users),
                             _User(["admin"]), ("admin",), True)
        assert shadow.snapshot()["error"] == 1

    @pytest.mark.asyncio
    async def test_a_failing_recorder_is_swallowed(self, monkeypatch):
        """The SecurityEvent write opens its own session. A dead database must
        not turn an observability feature into an outage."""
        from user_service.api.routers import auth_real

        async def explode(*a, **k):
            raise RuntimeError("no database")
        monkeypatch.setattr(shadow, "_record", explode)
        monkeypatch.setattr(shadow._backend, "evaluate",
                            lambda *a, **k: _FakeDecision(False))

        await shadow.compare(_request_for(auth_real.list_users),
                             _User(["admin"]), ("admin",), True)
        assert shadow.snapshot()["error"] == 1

    @pytest.mark.asyncio
    async def test_a_mismatch_is_counted_by_direction(self, monkeypatch):
        """Which way a mismatch points decides how urgent it is: more permissive
        is a security regression, less permissive breaks users at cutover."""
        from user_service.api.routers import auth_real
        recorded = []

        async def capture(*a, **k):
            recorded.append(a[-1])
        monkeypatch.setattr(shadow, "_record", capture)
        monkeypatch.setattr(shadow._backend, "evaluate",
                            lambda *a, **k: _FakeDecision(True))

        await shadow.compare(_request_for(auth_real.list_users),
                             _User(["viewer"]), ("admin",), False)
        assert shadow.snapshot()["mismatch_would_allow"] == 1
        assert recorded == ["mismatch_would_allow"]


class _FakeDecision:
    def __init__(self, allow):
        self.allow = allow
        self.reason = "fake"


# ── route → action resolution ─────────────────────────────────────

class TestResolution:
    def test_an_unresolvable_route_is_counted_not_raised(self):
        class Req:
            scope = {}
        assert shadow.resolve_action(Req(), ("admin",)) is None

    def test_a_route_with_no_endpoint_is_survivable(self):
        class Req:
            scope = {"route": object()}
        assert shadow.resolve_action(Req(), ("admin",)) is None

    def test_the_two_gates_on_one_route_resolve_by_role_tuple(self):
        """`admin.py` carries a router gate AND endpoint gates. The dependency
        knows only its own role tuple, so that is what disambiguates."""
        from case_service.api.routers import admin as admin_router

        class Route:
            endpoint = admin_router.create_calendar

        class Req:
            scope = {"route": Route()}

        got = shadow.resolve_action(Req(), ("admin",))
        assert got in ("platform.calendars.write", "platform.admin.access")
        assert ACTIONS[got].legacy_roles == ("admin",)

    def test_every_indexed_gate_names_a_real_action(self):
        from velaris_shared.hxguard.actions import ACTION_BY_GATE, MODULE_ACTION_BY_FILE
        for gate, action in ACTION_BY_GATE.items():
            assert action in ACTIONS, gate
        for f, action in MODULE_ACTION_BY_FILE.items():
            assert action in ACTIONS, f

    def test_every_indexed_gate_still_exists_in_the_router(self):
        """A renamed handler would silently stop being compared. This is the
        only thing that notices."""
        import importlib
        from velaris_shared.hxguard.actions import ACTION_BY_GATE
        missing = []
        for gate in ACTION_BY_GATE:
            file, fn = gate.split("::")
            mod = _router_module(file)
            if not hasattr(mod, fn):
                missing.append(gate)
        assert not missing, f"gates no longer in the router: {missing}"


# ── the namespace boundary ────────────────────────────────────────

class TestNamespaceBoundary:
    def test_a_legacy_wildcard_does_not_grant_platform_actions(self):
        """The seeded `admin` access role carries {"resource":"*","actions":["*"]},
        written for case-level resources years before platform actions existed.
        Letting it satisfy identity.users.write would mean an access role
        silently conferring platform administration — invisible today only
        because the role-name merge already does, and therefore invisible right
        up until Phase 4 removes that merge."""
        from velaris_shared.hxguard.privileges import effective_privileges, evaluate_privileges
        legacy_admin = [{"resource": "*", "actions": ["*"], "case_type_id": "*"}]
        privs = effective_privileges(["viewer"], legacy_admin)
        assert not evaluate_privileges(privs, "identity.users.write")

    def test_legacy_case_vocabulary_grants_no_platform_action(self):
        from velaris_shared.hxguard.privileges import effective_privileges, evaluate_privileges
        legacy = [
            {"resource": "case", "actions": ["create", "read", "update"]},
            {"resource": "case_type", "actions": ["*"]},
            {"resource": "workflow", "actions": ["execute"]},
        ]
        privs = effective_privileges(["viewer"], legacy)
        for name in ACTIONS:
            assert not evaluate_privileges(privs, name), name

    def test_an_entry_written_in_the_new_namespace_does_grant(self):
        """The boundary refuses legacy vocabulary, not access groups themselves
        — otherwise Phase 3 could never grant anything through one."""
        from velaris_shared.hxguard.privileges import effective_privileges, evaluate_privileges
        modern = [{"resource": "identity.users", "actions": ["read"]}]
        privs = effective_privileges(["viewer"], modern)
        assert evaluate_privileges(privs, "identity.users.read")
        assert not evaluate_privileges(privs, "identity.users.write")


# ── still no behaviour change ─────────────────────────────────────

class TestShadowCoverage:
    def test_migrated_routers_are_no_longer_shadowed(self):
        """Once a router decides by privilege there is nothing to compare it
        against — the shadow exists only where require_role still rules."""
        from velaris_shared.hxguard.actions import MIGRATED_ROUTERS

        from case_service.hxguard_ledger.legacy_sites import find_router
        for name in sorted(MIGRATED_ROUTERS):
            assert "require_role(" not in find_router(name).read_text(), name

    def test_the_shadow_still_covers_every_unmigrated_gate(self):
        """Phase 3 shrinks the shadow's job one router at a time. What must stay
        true is that a gate is either migrated or shadowed — never neither."""
        from velaris_shared.hxguard.actions import (
            ACTION_BY_GATE, MIGRATED_ROUTERS, MODULE_ACTION_BY_FILE)

        from case_service.hxguard_ledger.legacy_sites import find_router
        known = {g.split("::")[0] for g in ACTION_BY_GATE} | set(MODULE_ACTION_BY_FILE)
        for name in sorted(known - MIGRATED_ROUTERS):
            assert "require_role(" in find_router(name).read_text(), name
