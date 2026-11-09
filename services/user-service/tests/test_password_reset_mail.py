"""A password-reset code arrives by email, and works (R5e step 10).

Until the split the code was never sent: auth_real imported a `MailService` that
does not exist and a bare `except: pass` hid it. Now user-service asks
case-service (`/internal/v1/user/otp-email`), which composes and sends it. This
walks the whole journey: request, read the code from the mail, reset, sign in.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
import re

import pytest
from sqlalchemy import select

from velaris_shared.auth.passwords import hash_password
from velaris_shared.db.models import EmailAccountModel, HelixUserModel

EMAIL = "reset.me@company.com"


@pytest.fixture
def mailbox(monkeypatch):
    sent = []

    async def _send(self, session, **kw):  # noqa: ANN001
        sent.append(kw)

    from case_service.mail import EmailService
    monkeypatch.setattr(EmailService, "send", _send)
    return sent


async def _flush():
    """The mail is sent detached from the answer; wait for it before reading."""
    import asyncio

    from user_service.api.routers import auth_real
    await asyncio.gather(*list(auth_real._pending_mail))     # noqa: SLF001


async def _setup(session, *, outbound=True, password="original-pw"):
    session.add(HelixUserModel(username="resetme", email=EMAIL, roles=["viewer"],
                               is_active=True,
                               password_hash=hash_password(password) if password else None))
    if outbound:
        session.add(EmailAccountModel(name="out", address="noreply@company.com",
                                      smtp_host="127.0.0.1", is_default_outbound=True,
                                      is_active=True))
    await session.commit()


async def test_the_code_is_mailed_and_resets_the_password(anon_client, session, mailbox):
    await _setup(session)
    r = await anon_client.post("/api/v1/auth/real/forgot-password", json={"email": EMAIL})
    await _flush()
    assert r.status_code == 200
    assert len(mailbox) == 1 and mailbox[0]["to_addresses"] == [EMAIL]
    code = re.search(r"\b(\d{6})\b", mailbox[0]["body_text"]).group(1)

    r = await anon_client.post("/api/v1/auth/real/reset-password", json={
        "email": EMAIL, "otp": code, "new_password": "brand-new-pw-1"})
    assert r.status_code == 200, r.text

    r = await anon_client.post("/api/v1/auth/real/login",
                               json={"username": "resetme", "password": "brand-new-pw-1"})
    assert r.status_code == 200, r.text
    r = await anon_client.post("/api/v1/auth/real/login",
                               json={"username": "resetme", "password": "original-pw"})
    assert r.status_code == 401


async def test_a_wrong_code_does_not_reset(anon_client, session, mailbox):
    await _setup(session)
    await anon_client.post("/api/v1/auth/real/forgot-password", json={"email": EMAIL})
    await _flush()
    code = re.search(r"\b(\d{6})\b", mailbox[0]["body_text"]).group(1)
    wrong = f"{(int(code) + 1) % 1_000_000:06d}"
    r = await anon_client.post("/api/v1/auth/real/reset-password", json={
        "email": EMAIL, "otp": wrong, "new_password": "attacker-pw-1"})
    assert r.status_code == 400
    user = (await session.execute(select(HelixUserModel).where(HelixUserModel.email == EMAIL))).scalar_one()
    from velaris_shared.auth.passwords import verify_password
    assert verify_password("original-pw", user.password_hash)


@pytest.mark.parametrize("email,outbound,password", [
    ("nobody@company.com", True, "original-pw"),     # no such account
    (EMAIL, True, None),                              # federated: no local password
    (EMAIL, False, "original-pw"),                    # no outbound mail account
])
async def test_no_mail_and_the_same_answer(anon_client, session, mailbox, email, outbound, password):
    await _setup(session, outbound=outbound, password=password)
    r = await anon_client.post("/api/v1/auth/real/forgot-password", json={"email": email})
    await _flush()
    assert r.status_code == 200
    assert "single sign-on" in r.json()["message"]
    assert mailbox == []


async def test_case_service_unreachable_still_answers_the_same(anon_client, session, monkeypatch):
    from velaris_shared.internal import client as internal

    async def _down(*a, **kw):  # noqa: ANN002
        raise internal.InternalCallError("user/otp-email", None, "connection refused")

    monkeypatch.setattr(internal, "call", _down)
    await _setup(session)
    r = await anon_client.post("/api/v1/auth/real/forgot-password", json={"email": EMAIL})
    await _flush()
    assert r.status_code == 200 and "single sign-on" in r.json()["message"]



async def test_the_answer_never_waits_for_the_mail(anon_client, session, monkeypatch):
    """A real account's answer must not wait on case-service and SMTP while an
    unknown address answers at once — that difference names the accounts. Here
    the mail never completes until the test lets it; the answer arrives anyway."""
    import asyncio

    from user_service.api.routers import auth_real
    from velaris_shared.internal import client as internal

    release = asyncio.Event()
    calls = []

    async def _stuck(*a, **kw):  # noqa: ANN002
        calls.append(a)
        await release.wait()
        return {"sent": True}

    monkeypatch.setattr(internal, "call", _stuck)
    await _setup(session)
    r = await asyncio.wait_for(
        anon_client.post("/api/v1/auth/real/forgot-password", json={"email": EMAIL}), 10)
    assert r.status_code == 200
    assert auth_real._pending_mail                            # noqa: SLF001 — still sending
    release.set()
    await _flush()
    assert len(calls) == 1
