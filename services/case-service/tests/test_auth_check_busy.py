"""The per-request sign-in check refuses (503) when the database is unreachable.

Until 2026-10-19 get_current_user skipped the access-group lookup and the
revoked-token / inactive-account check on ANY exception, so while the pool was
exhausted a revoked token was accepted. A database outage now refuses with
VEL-AUTH-CHECK-BUSY-503 + Retry-After; any other failure keeps the old
behaviour (skipped, now logged as a warning).

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
import uuid

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import exc as sa_exc
from starlette.requests import Request

from velaris_shared.auth.dependencies import get_current_user
from velaris_shared.auth.jwt_handler import create_dev_token
from velaris_shared.config import get_settings
from velaris_shared.core.error_codes import AUTH_CHECK_BUSY_503
from velaris_shared.db.commit_route import register_session


class _FailingSession:
    """A request session whose every query raises `exc`."""

    def __init__(self, exc: BaseException):
        self._exc = exc

    async def execute(self, *a, **kw):
        raise self._exc


def _request_with(session) -> Request:
    req = Request({"type": "http", "method": "GET", "path": "/", "headers": [],
                   "query_string": b"", "state": {}})
    register_session(req, session)
    return req


def _user_creds() -> HTTPAuthorizationCredentials:
    s = get_settings()
    token = create_dev_token(user_id=str(uuid.uuid4()), username="plain-user", roles=["user"],
                             secret=s.auth_secret, private_key=s.auth_rsa_private_key or "")
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


@pytest.mark.parametrize("exc", [
    sa_exc.TimeoutError("QueuePool limit of size 10 overflow 5 reached"),
    sa_exc.OperationalError("SELECT 1", {}, Exception("too many clients already")),
    ConnectionRefusedError(111, "Connection refused"),
])
async def test_database_unreachable_refuses_with_503(exc):
    with pytest.raises(HTTPException) as info:
        await get_current_user(_request_with(_FailingSession(exc)), _user_creds())
    assert info.value.status_code == 503
    assert getattr(info.value, "code", None) == AUTH_CHECK_BUSY_503
    assert info.value.headers == {"Retry-After": "5"}


async def test_other_failures_keep_the_old_behaviour():
    # Not an outage (a query bug, a missing table): skipped as before.
    exc = sa_exc.ProgrammingError("SELECT 1", {}, Exception("relation does not exist"))
    user = await get_current_user(_request_with(_FailingSession(exc)), _user_creds())
    assert user.username == "plain-user"
    assert user.active_access_group is None


# ── Callers that turn a refused sign-in check into "no caller" ───────────────
# Each must pass the busy 503 on: swallowing it skipped the tenant guard (the
# route's own check could pass a moment later), scoped reads to nothing (an
# empty page), or made an operator look anonymous.

def _busy():
    from velaris_shared.core.error_codes import coded
    return coded(503, AUTH_CHECK_BUSY_503, "busy", headers={"Retry-After": "5"})


def _bearer_request() -> Request:
    return Request({"type": "http", "method": "GET", "path": "/", "query_string": b"",
                    "headers": [(b"authorization", b"Bearer x.y.z")], "state": {}})


async def _raise_busy(*a, **kw):
    raise _busy()


async def _raise_401(*a, **kw):
    raise HTTPException(401, "bad token")


@pytest.mark.parametrize("module, fn", [
    ("case_service.core.tenant_path_guard", "_caller"),
    ("velaris_shared.tenancy.orm_scope", "staff_caller"),
    ("case_service.api.routers.documents", "_optional_user"),
])
async def test_busy_is_passed_on_and_401_still_means_no_caller(monkeypatch, module, fn):
    import importlib
    import velaris_shared.auth.dependencies as deps

    target = getattr(importlib.import_module(module), fn)
    monkeypatch.setattr(deps, "get_current_user", _raise_busy)
    with pytest.raises(HTTPException) as info:
        await target(_bearer_request())
    assert info.value.status_code == 503

    monkeypatch.setattr(deps, "get_current_user", _raise_401)
    assert await target(_bearer_request()) is None


async def test_scoped_mcp_token_refuses_when_the_grant_cannot_be_read(monkeypatch):
    from case_service.hxmcp import tokens as mcp_tokens

    async def _unreachable(*a, **kw):
        raise sa_exc.TimeoutError("QueuePool limit reached")

    monkeypatch.setattr(mcp_tokens, "_check_grant", _unreachable)
    s = get_settings()
    monkeypatch.setattr(s, "mcp_external_tokens_enabled", True)
    token = create_dev_token(user_id=str(uuid.uuid4()), username="grantor", roles=["user"],
                             secret=s.auth_secret, private_key=s.auth_rsa_private_key or "",
                             extra_claims={"token_use": "mcp", "jti": str(uuid.uuid4())})
    with pytest.raises(HTTPException) as info:
        await mcp_tokens.get_mcp_caller(
            _request_with(_FailingSession(Exception("unused"))),
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=token))
    assert info.value.status_code == 503


# ── Inside _security_check: the revocation cache and the account lookup ──────

async def _enrich_passthrough(user, session):
    return user


async def test_revocation_cache_unreadable_refuses(monkeypatch):
    import velaris_shared.auth.dependencies as deps

    monkeypatch.setattr(deps, "_enrich_with_access_group", _enrich_passthrough)
    monkeypatch.setattr(deps, "_REVOKED_CACHE_AT", 0.0)        # stale: must refresh
    exc = sa_exc.TimeoutError("QueuePool limit reached")
    with pytest.raises(HTTPException) as info:
        await get_current_user(_request_with(_FailingSession(exc)), _user_creds())
    assert info.value.status_code == 503


@pytest.mark.account_gate
async def test_account_lookup_unreadable_refuses(monkeypatch):
    import time as _time
    import velaris_shared.auth.dependencies as deps

    class _NoAccountLookup(_FailingSession):
        async def get(self, *a, **kw):
            raise self._exc

    monkeypatch.setattr(deps, "_enrich_with_access_group", _enrich_passthrough)
    monkeypatch.setattr(deps, "_REVOKED_CACHE_AT", _time.monotonic())   # fresh: no refresh
    with pytest.raises(HTTPException) as info:
        await get_current_user(
            _request_with(_NoAccountLookup(ConnectionRefusedError(111, "refused"))), _user_creds())
    assert info.value.status_code == 503
