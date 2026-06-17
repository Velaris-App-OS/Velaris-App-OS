"""Test Suite (#27) Phase B — built-in Component + Security suites execute green.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import pytest

from case_service.testsuite import runner, builtin, dsl


def _build_identity_clients(client, anon_client, nonadmin_client):
    return {"admin": client, "none": anon_client,
            "non_admin": nonadmin_client, "tenant_a": client, "tenant_b": client}


@pytest.fixture
def nonadmin_client(client):
    """A client whose token has no admin role (for 403 conformance tests)."""
    from case_service.testsuite.isolation import mint_token
    from httpx import ASGITransport, AsyncClient
    from case_service.main import app
    token = mint_token([])  # no roles
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                       headers={"Authorization": f"Bearer {token}"})


@pytest.mark.parametrize("suite_name", ["platform-smoke", "component", "security"])
def test_builtin_suite_is_valid_dsl(suite_name):
    dsl.parse_suite(builtin.get_builtin_suite(suite_name))


@pytest.mark.asyncio
async def test_component_suite_runs_green(session, client, anon_client, nonadmin_client):
    clients = _build_identity_clients(client, anon_client, nonadmin_client)
    run = await runner.run_suite(session, builtin.get_builtin_suite("component"),
                                 suite_name="component", triggered_by="t", clients=clients)
    assert run.status == "passed", f"{run.status} p={run.passed} f={run.failed}"


@pytest.mark.asyncio
async def test_security_suite_runs_green(session, client, anon_client, nonadmin_client):
    clients = _build_identity_clients(client, anon_client, nonadmin_client)
    run = await runner.run_suite(session, builtin.get_builtin_suite("security"),
                                 suite_name="security", triggered_by="t", clients=clients)
    assert run.status == "passed", f"{run.status} p={run.passed} f={run.failed}"
    await nonadmin_client.aclose()
