"""DB SDK Phase 1 — case-create endpoint smoke (the end-to-end MySQL proof).

Runs the real POST /api/v1/cases path through the ASGI app. Its job is twofold:
  1. Prove the portable case-number sequence (`next_case_seq`) fires through the
     actual endpoint on the active backend — not just in isolation.
  2. Surface any OTHER raw-SQL site in the create path that breaks on a non-Postgres
     backend (fire_outbound_rules, event log, webhook outbox, …). Several are
     best-effort try/except'd, so they no-op rather than 500; the un-wrapped ones
     would fail the request and that is exactly what we want this smoke to catch.

On SQLite (default harness) the sequence call can't run (no helix_case_seq / no
counter mechanism wired), so the case-number assertion is gated to EXTERNAL_MODE
(Postgres or MySQL). The 201 assertion always holds.
"""
from __future__ import annotations

import re

import pytest

from tests.conftest import EXTERNAL_MODE, create_case, deploy_case_type


@pytest.mark.asyncio
async def test_create_case_endpoint_smoke(client):
    ct = await deploy_case_type(client, name="Claims")
    case = await create_case(client, ct["id"], data={"amount": 100})

    # Request succeeded end-to-end (no un-wrapped raw-SQL site 500'd on this backend).
    assert case["id"]

    if EXTERNAL_MODE:
        # The portable sequence ran through the endpoint. Prefix = first 3 letters of
        # the case-type name upper-cased ("Claims" → CLA).
        assert case.get("case_number"), "case_number not populated on external backend"
        assert re.fullmatch(r"HLX-CLA-\d{6}", case["case_number"]), case["case_number"]


@pytest.mark.asyncio
async def test_case_numbers_are_monotonic_via_endpoint(client):
    if not EXTERNAL_MODE:
        pytest.skip("case-number sequence requires the external-DB harness")
    ct = await deploy_case_type(client, name="Support")
    n1 = (await create_case(client, ct["id"]))["case_number"]
    n2 = (await create_case(client, ct["id"]))["case_number"]
    assert n1 and n2 and n1 != n2
    seq1 = int(n1.rsplit("-", 1)[-1])
    seq2 = int(n2.rsplit("-", 1)[-1])
    assert seq2 == seq1 + 1, (n1, n2)
