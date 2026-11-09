"""Studio's Content-Security-Policy reports (2026-10-29): anonymous, capped,
and never logging a query string — some Studio URLs carry a token.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
import logging

import pytest
from httpx import ASGITransport, AsyncClient

from case_service.api.routers import csp_reports
from case_service.main import app

URL = "/api/v1/csp-reports"


@pytest.fixture(autouse=True)
def _fresh_window():
    csp_reports._window.update(start=0.0, count=0, dropped=0)


async def _post(body, ctype="application/csp-report"):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.post(URL, content=body if isinstance(body, (bytes, str)) else json.dumps(body),
                            headers={"Content-Type": ctype})


@pytest.mark.asyncio
async def test_a_report_is_logged_without_query_strings(caplog):
    caplog.set_level(logging.WARNING, logger="velaris.studio.csp")
    r = await _post({"csp-report": {
        "document-uri": "https://studio.example/cases/1?token=tok-7f3a9c#x",
        "blocked-uri": "wss://evil.example/ws?token=tok-7f3a9c",
        "effective-directive": "connect-src", "disposition": "report"}})
    assert r.status_code == 204
    text = "\n".join(r.getMessage() for r in caplog.records if r.name == "velaris.studio.csp")
    assert "connect-src" in text and "wss://evil.example/ws" in text and "https://studio.example/cases/1" in text
    assert "tok-7f3a9c" not in text


@pytest.mark.asyncio
async def test_the_reporting_api_shape_is_read_too(caplog):
    caplog.set_level(logging.WARNING, logger="velaris.studio.csp")
    await _post([{"type": "csp-violation", "body": {"documentURL": "https://s/", "blockedURL": "inline",
                                                     "effectiveDirective": "script-src-elem", "disposition": "enforce"}}],
                "application/reports+json")
    assert any("script-src-elem blocked inline" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_oversized_malformed_and_floods_are_dropped_quietly(caplog):
    caplog.set_level(logging.WARNING, logger="velaris.studio.csp")
    assert (await _post(b"x" * 20_000)).status_code == 204
    assert (await _post(b"{not json")).status_code == 204
    assert not [r for r in caplog.records if r.name == "velaris.studio.csp"]
    csp_reports._window.update(start=0.0, count=0, dropped=0)
    for _ in range(csp_reports.PER_MINUTE + 5):
        await _post({"csp-report": {"effective-directive": "img-src", "blocked-uri": "https://a/"}})
    assert csp_reports._window["dropped"] == 5
