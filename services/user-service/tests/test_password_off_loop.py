"""bcrypt runs on a worker thread, not the event loop (R5e step 10e, 2026-10-22).

On a clean box, 150 users signing in together waited 15 s at the median and
28 s at p95, and every other request to user-service queued behind them: each
cost-12 bcrypt held the loop for ~0.2 s. Here a password check is made to take
a full second; a request that needs no password must not wait for it.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
import asyncio
import time

from velaris_shared.auth import passwords
from velaris_shared.db.models import HelixUserModel


async def test_a_slow_password_check_does_not_hold_other_requests(anon_client, session, monkeypatch):
    session.add(HelixUserModel(username="slowpw", email="slow@pw.test", roles=["viewer"],
                               is_active=True, password_hash=passwords.hash_password("right-pw-1")))
    await session.commit()

    real = passwords.verify_password

    def _slow(plain, hashed):
        time.sleep(1.0)                    # blocking, like bcrypt itself
        return real(plain, hashed)

    monkeypatch.setattr(passwords, "verify_password", _slow)

    start = time.monotonic()

    async def _login():
        r = await anon_client.post("/api/v1/auth/real/login",
                                   json={"username": "slowpw", "password": "right-pw-1"})
        return r, time.monotonic() - start

    async def _other():
        await asyncio.sleep(0.2)           # the login is already inside bcrypt
        r = await anon_client.get("/api/v1/auth/real/sso/providers")
        return r, time.monotonic() - start

    (login, login_done), (other, other_done) = await asyncio.gather(_login(), _other())
    assert login.status_code == 200, login.text
    assert other.status_code == 200
    # Measured from one start: inline bcrypt would finish the other request only
    # after the second-long check.
    assert other_done < 0.8 < login_done, (other_done, login_done)
