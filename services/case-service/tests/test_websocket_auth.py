"""WebSockets run the full sign-in check — on open, and again while open.

Until 2026-10-19 /realtime/ws and /hxstream/ws verified the token's signature
only: a revoked token or a suspended account kept a live socket, and an agent
token (refused by every HTTP route) was accepted.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
import asyncio
import json
import uuid

import pytest
from fastapi import WebSocketDisconnect
from sqlalchemy import exc as sa_exc
from starlette.datastructures import State

import velaris_shared.auth.dependencies as deps
from velaris_shared.auth.jwt_handler import create_dev_token
from velaris_shared.config import get_settings
from velaris_shared.db.models import HelixUserModel

from tests.conftest import TestSessionFactory


class _FakeSocket:
    """Enough of a WebSocket for the handlers: records what it was told."""

    def __init__(self, hold_open: float = 0.0):
        self.state = State()
        self.closed_with: int | None = None
        self.sent: list = []
        self._hold = hold_open

    async def accept(self):
        pass

    async def close(self, code: int = 1000, reason: str = ""):
        if self.closed_with is None:
            self.closed_with = code

    async def send_json(self, data):
        self.sent.append(data)

    async def send_text(self, data):
        self.sent.append(data)

    async def receive_text(self):
        # Stay "open" until closed (by the watcher) or the hold runs out.
        waited = 0.0
        while self.closed_with is None and waited < self._hold:
            await asyncio.sleep(0.02)
            waited += 0.02
        raise WebSocketDisconnect(code=self.closed_with or 1000)


# The real account check runs here: suspended / deleted accounts are the point.
pytestmark = pytest.mark.account_gate


async def _user(active: bool = True) -> HelixUserModel:
    async with TestSessionFactory() as db:
        u = HelixUserModel(username=f"ws-{uuid.uuid4().hex[:8]}", email=f"{uuid.uuid4().hex[:8]}@t.local",
                           roles=["user"], is_active=active)
        db.add(u)
        await db.commit()
        return u


def _token(user, extra=None) -> str:
    s = get_settings()
    return create_dev_token(user_id=str(user.id), username=user.username, roles=["user"],
                            secret=s.auth_secret, private_key=s.auth_rsa_private_key or "",
                            extra_claims=extra)


async def _realtime(ws, token):
    from case_service.api.routers.realtime import websocket_endpoint
    await websocket_endpoint(ws, token=token, user_id="anonymous")


async def _hxstream(ws, token):
    from case_service.api.routers.hxstream import hxstream_ws
    await hxstream_ws(ws, token=token, case_id=None, event_types=None)


ENDPOINTS = [_realtime, _hxstream]


@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_plain_user_gets_a_socket(endpoint):
    u = await _user()
    ws = _FakeSocket()
    await endpoint(ws, _token(u))
    assert ws.closed_with in (None, 1000)
    assert any((m.get("type") if isinstance(m, dict) else json.loads(m).get("type")) == "connected"
               for m in ws.sent)


@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_no_token_is_refused(endpoint):
    ws = _FakeSocket()
    await endpoint(ws, None)
    assert ws.closed_with == deps.WS_CLOSE_UNAUTHORIZED


@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_suspended_account_is_refused(endpoint):
    u = await _user(active=False)
    ws = _FakeSocket()
    await endpoint(ws, _token(u))
    assert ws.closed_with == deps.WS_CLOSE_UNAUTHORIZED


@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_agent_token_is_refused(endpoint):
    u = await _user()
    ws = _FakeSocket()
    await endpoint(ws, _token(u, {"token_use": "agent"}))
    assert ws.closed_with == deps.WS_CLOSE_UNAUTHORIZED


@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_busy_database_closes_with_try_again(endpoint, monkeypatch):
    u = await _user()

    async def _unreachable(*a, **kw):
        raise sa_exc.TimeoutError("QueuePool limit reached")

    monkeypatch.setattr(deps, "_enrich_with_access_group", _unreachable)
    ws = _FakeSocket()
    await endpoint(ws, _token(u))
    assert ws.closed_with == deps.WS_CLOSE_TRY_AGAIN


@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_open_socket_is_closed_when_the_account_is_suspended(endpoint, monkeypatch):
    monkeypatch.setattr(deps, "WS_RECHECK_SECONDS", 0.05)
    u = await _user()
    ws = _FakeSocket(hold_open=3.0)
    task = asyncio.create_task(endpoint(ws, _token(u)))
    await asyncio.sleep(0.1)
    assert ws.closed_with is None                      # signed in, still open
    async with TestSessionFactory() as db:
        row = await db.get(HelixUserModel, u.id)
        row.is_active = False
        await db.commit()
    await asyncio.wait_for(task, timeout=3)
    assert ws.closed_with == deps.WS_CLOSE_UNAUTHORIZED


@pytest.mark.parametrize("endpoint", ENDPOINTS)
async def test_open_socket_is_closed_when_the_account_is_deleted(endpoint, monkeypatch):
    """2026-10-23: a deleted account's token was still accepted; an open socket
    must close at the next re-check, like a suspended one."""
    monkeypatch.setattr(deps, "WS_RECHECK_SECONDS", 0.05)
    u = await _user()
    ws = _FakeSocket(hold_open=3.0)
    task = asyncio.create_task(endpoint(ws, _token(u)))
    await asyncio.sleep(0.1)
    assert ws.closed_with is None
    async with TestSessionFactory() as db:
        await db.delete(await db.get(HelixUserModel, u.id))
        await db.commit()
    await asyncio.wait_for(task, timeout=3)
    assert ws.closed_with == deps.WS_CLOSE_UNAUTHORIZED
