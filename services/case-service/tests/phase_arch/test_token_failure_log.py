"""S1 (R6 soak 2026-11-02): a failed token check must not flood the journal.

An expired token wrote a WARNING per request — ~110 lines/s, +260 MB/h. Now an
expired token is DEBUG, and any other failure is one WARNING per reason per
minute, carrying how many were folded into it.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging

import jwt
import pytest

from velaris_shared.auth import dependencies as D


@pytest.fixture(autouse=True)
def _fresh():
    D._token_log.clear()
    yield
    D._token_log.clear()


def _warnings(caplog):
    return [r for r in caplog.records if r.levelno >= logging.WARNING
            and "JWT validation failed" in r.getMessage()]


def test_expired_token_is_not_a_warning(caplog):
    caplog.set_level(logging.DEBUG, logger=D.logger.name)
    for _ in range(500):
        D._log_token_failure(jwt.ExpiredSignatureError("Signature has expired"))
    assert _warnings(caplog) == []


def test_bad_tokens_warn_once_a_minute_with_the_count(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG, logger=D.logger.name)
    clock = [1000.0]
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    for _ in range(200):
        D._log_token_failure(jwt.InvalidSignatureError("Signature verification failed"))
    assert len(_warnings(caplog)) == 1                     # a forged-token flood is still seen
    clock[0] += 61
    D._log_token_failure(jwt.InvalidSignatureError("Signature verification failed"))
    w = _warnings(caplog)
    assert len(w) == 2
    assert "+199 more InvalidSignatureError" in w[1].getMessage()


def test_reasons_are_counted_separately(caplog):
    caplog.set_level(logging.DEBUG, logger=D.logger.name)
    D._log_token_failure(jwt.InvalidSignatureError("x"))
    D._log_token_failure(jwt.DecodeError("y"))
    assert len(_warnings(caplog)) == 2


async def test_expired_token_still_refused(client):
    import time
    from velaris_shared.config import get_settings
    s = get_settings()
    tok = jwt.encode({"sub": "u", "exp": int(time.time()) - 60, "iss": s.auth_issuer,
                      "aud": s.auth_audience}, s.auth_secret, algorithm="HS256")
    r = await client.get("/api/v1/cases", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 401
