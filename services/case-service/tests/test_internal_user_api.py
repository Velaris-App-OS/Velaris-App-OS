"""user-service's internal case API (R5e step 10 — the sign-in split).

`/internal/v1/user/otp-email` sends the password-reset code. Same guard as the
portal's routes; it composes the mail itself, only to an account's address, and
never logs the code.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
import logging
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from case_service.internal_api import user as internal_user
from case_service.main import app
from tests.conftest import TestSessionFactory
from velaris_shared.config import get_settings
from velaris_shared.db.models import EmailAccountModel, HelixUserModel
from velaris_shared.internal import client as internal
from velaris_shared.internal.auth import HEADER

SECRET = "u" * 40
PATH = "/internal/v1/user/otp-email"


@pytest.fixture
def secret():
    get_settings().internal_service_secret = SECRET
    yield SECRET


@pytest.fixture
async def raw():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
def sent(monkeypatch):
    calls = []

    async def _send(self, session, **kw):  # noqa: ANN001
        calls.append(kw)

    from case_service.mail import EmailService
    monkeypatch.setattr(EmailService, "send", _send)
    return calls


async def _user(email="ann@vx.test", display="Ann"):
    async with TestSessionFactory() as db:
        u = HelixUserModel(username=f"ann-{uuid.uuid4().hex[:6]}", email=email,
                           display_name=display, password_hash="x", roles=["user"])
        db.add(u)
        await db.commit()
        return u


async def _outbound():
    async with TestSessionFactory() as db:
        db.add(EmailAccountModel(name="out", address="noreply@vx.test", smtp_host="127.0.0.1",
                                 is_default_outbound=True, is_active=True))
        await db.commit()


def _body(**kw):
    return {"purpose": "password_reset", "to": "ann@vx.test", "code": "123456",
            "valid_minutes": 10, **kw}


# ── the guard ─────────────────────────────────────────────────────────────────

def test_one_route():
    assert sorted(internal_user.ROUTES) == ["user/otp-email"]
    assert "user/otp-email" in internal._LOCAL


async def test_unconfigured_install_refuses(raw):
    get_settings().internal_service_secret = ""
    assert (await raw.post(PATH, json=_body(), headers={HEADER: "x"})).status_code == 503


async def test_missing_or_wrong_secret_is_refused(raw, secret):
    assert (await raw.post(PATH, json=_body())).status_code == 403
    assert (await raw.post(PATH, json=_body(), headers={HEADER: "no-" + secret})).status_code == 403


async def test_a_proxied_call_is_refused_even_with_the_secret(raw, secret):
    r = await raw.post(PATH, json=_body(), headers={HEADER: secret, "X-Forwarded-For": "203.0.113.9"})
    assert r.status_code == 404


def test_not_in_the_openapi_schema():
    assert not [p for p in app.openapi()["paths"] if p.startswith("/internal")]


# ── it sends only what case-service composes ─────────────────────────────────

@pytest.mark.parametrize("bad", [
    {"purpose": "login"}, {"purpose": "newsletter"},          # the portal's / unknown
    {"code": "hello!"}, {"code": "1234567"},
    {"valid_minutes": 0}, {"valid_minutes": 600},
])
async def test_refuses_anything_but_a_reset_code(raw, secret, bad):
    assert (await raw.post(PATH, json=_body(**bad), headers={HEADER: secret})).status_code == 422


def test_has_no_subject_or_body_field():
    assert set(internal_user.UserOtpEmailIn.model_fields) == {"purpose", "to", "code", "valid_minutes"}


async def test_sends_the_code_to_the_account(raw, secret, sent):
    await _user()
    await _outbound()
    r = await raw.post(PATH, json=_body(), headers={HEADER: secret})
    assert r.status_code == 200 and r.json() == {"sent": True}
    assert len(sent) == 1
    assert sent[0]["to_addresses"] == ["ann@vx.test"]
    assert "123456" in sent[0]["body_text"] and "10 minutes" in sent[0]["body_text"]
    assert "Hi Ann" in sent[0]["body_text"]


async def test_mails_no_one_who_is_not_an_account(raw, secret, sent):
    await _outbound()
    r = await raw.post(PATH, json=_body(to="stranger@elsewhere.test"), headers={HEADER: secret})
    assert r.json() == {"sent": False} and sent == []


async def test_without_smtp_it_says_so_and_never_logs_the_code(raw, secret, sent, caplog):
    await _user()
    with caplog.at_level(logging.DEBUG):
        r = await raw.post(PATH, json=_body(code="987654"), headers={HEADER: secret})
    assert r.json() == {"sent": False} and sent == []
    assert "987654" not in caplog.text


# ── a stored message never keeps the code (R5e step 10, 2026-10-22) ───────────
# `email_messages` is readable through the mail API by anyone holding
# `platform.email.read`; a reset code stored there let such a holder take over
# the account it was sent to — the superadmin's included. Real EmailService,
# SMTP replaced: the code must go OUT, and must not stay IN.

@pytest.mark.parametrize("path,payload", [
    (PATH, _body(code="246813")),
    ("/internal/v1/portal/otp-email", {"purpose": "login", "to": "ann@vx.test", "code": "246813"}),
    ("/internal/v1/portal/otp-email", {"purpose": "bank_details", "to": "ann@vx.test",
                                       "code": "246813"}),
])
async def test_the_stored_message_keeps_no_code(raw, secret, monkeypatch, path, payload):
    from sqlalchemy import select

    from case_service.mail import service as mail_service
    from velaris_shared.db.models import EmailMessageModel

    wire = []

    async def _smtp(*, mime_message, **kw):  # noqa: ANN003
        wire.append("".join(part.get_payload(decode=True).decode()
                            for part in mime_message.walk()
                            if part.get_content_type() == "text/plain"))

    monkeypatch.setattr(mail_service, "send_via_smtp", _smtp)
    await _user()
    await _outbound()
    r = await raw.post(path, json=payload, headers={HEADER: secret})
    assert r.status_code == 200 and r.json() == {"sent": True}, r.text
    assert len(wire) == 1 and "246813" in wire[0]
    async with TestSessionFactory() as db:
        rows = (await db.execute(select(EmailMessageModel))).scalars().all()
    assert rows, "the send is still recorded"
    for row in rows:
        assert "246813" not in (row.body_text or "") + (row.body_html or "") + row.subject
        assert row.body_text == mail_service.SECRET_BODY_PLACEHOLDER


async def test_the_sent_copy_polled_back_is_not_stored_either(raw, secret, monkeypatch):
    """An install polling `[Gmail]/All Mail` (or Sent) gets our own message back,
    tagged with the account's tenant — the per-tenant duplicate check missed the
    row stored without the code, so the copy stored it. Found live 2026-10-22."""
    from sqlalchemy import select

    from case_service.mail import EmailService
    from case_service.mail import service as mail_service
    from velaris_shared.db.models import EmailMessageModel

    wire = []

    async def _smtp(*, mime_message, **kw):  # noqa: ANN003
        wire.append(mime_message.as_bytes())

    monkeypatch.setattr(mail_service, "send_via_smtp", _smtp)
    await _user()
    await _outbound()
    r = await raw.post(PATH, json=_body(code="135792"), headers={HEADER: secret})
    assert r.json() == {"sent": True} and len(wire) == 1

    async with TestSessionFactory() as db:
        await EmailService().ingest_raw(db, wire[0], account_address="noreply@vx.test",
                                        tenant_id="default")
        await db.commit()
        rows = (await db.execute(select(EmailMessageModel))).scalars().all()
    assert len(rows) == 1
    assert "135792" not in (rows[0].body_text or "") + (rows[0].body_html or "")


def _bounce_of(original: bytes, *, enclose: str) -> bytes:
    """A delivery-failure report (RFC 3464) the way Gmail / Postfix send them."""
    from email.mime.base import MIMEBase
    from email.mime.message import MIMEMessage
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email import message_from_bytes

    rep = MIMEMultipart("report", report_type="delivery-status")
    rep["From"] = "Mail Delivery Subsystem <mailer-daemon@googlemail.com>"
    rep["To"] = "noreply@vx.test"
    rep["Subject"] = "Delivery Status Notification (Failure)"
    rep["Message-ID"] = "<bounce-1@mx.test>"
    orig = message_from_bytes(original)
    if enclose == "message":
        # Gmail's notice is HTML only, so the first text/plain part the parser
        # finds is the ENCLOSED original's — code included.
        rep.attach(MIMEText("<p>Address not found.</p>", "html"))
    else:
        # Postfix-style: the original's headers and text quoted in the notice.
        body = "".join(p.get_payload(decode=True).decode() for p in orig.walk()
                       if p.get_content_type() == "text/plain")
        hdrs = "".join(f"{k}: {v}\n" for k, v in orig.items())
        rep.attach(MIMEText("Address not found.\n\n----- Original message -----\n"
                            f"{hdrs}\n{body}"))
    from email.message import Message
    ds = MIMEBase("message", "delivery-status")
    per_msg, per_rcpt = Message(), Message()
    per_msg["Reporting-MTA"] = "dns; mx.test"
    per_rcpt["Final-Recipient"], per_rcpt["Action"], per_rcpt["Status"] = \
        "rfc822; ann@vx.test", "failed", "5.1.1"
    ds.set_payload([per_msg, per_rcpt])
    rep.attach(ds)
    if enclose == "message":
        rep.attach(MIMEMessage(orig))
    return rep.as_bytes()


def _reply_to(original: bytes) -> bytes:
    from email.mime.text import MIMEText
    from email import message_from_bytes

    orig = message_from_bytes(original)
    body = "".join(p.get_payload(decode=True).decode() for p in orig.walk()
                   if p.get_content_type() == "text/plain")
    r = MIMEText("Thanks!\n\n> " + body.replace("\n", "\n> "))
    r["From"] = "ann@vx.test"
    r["To"] = "noreply@vx.test"
    r["Subject"] = "Re: " + str(orig["Subject"])
    r["Message-ID"] = "<reply-1@vx.test>"
    r["In-Reply-To"] = orig["Message-ID"]
    return r.as_bytes()


@pytest.mark.parametrize("make", ["bounce-enclosed", "bounce-quoted", "reply"])
async def test_a_bounce_or_reply_quoting_the_code_is_stored_without_it(raw, secret, monkeypatch, make):
    """The gap left by the fix above (2026-10-22): a bounce encloses the original,
    a reply quotes it — neither has the original's Message-ID as its own."""
    from sqlalchemy import select

    from case_service.mail import EmailService
    from case_service.mail import service as mail_service
    from velaris_shared.db.models import EmailMessageModel

    wire = []

    async def _smtp(*, mime_message, **kw):  # noqa: ANN003
        wire.append(mime_message.as_bytes())

    monkeypatch.setattr(mail_service, "send_via_smtp", _smtp)
    await _user()
    await _outbound()
    r = await raw.post(PATH, json=_body(code="864209"), headers={HEADER: secret})
    assert r.json() == {"sent": True}
    incoming = {"bounce-enclosed": lambda o: _bounce_of(o, enclose="message"),
                "bounce-quoted": lambda o: _bounce_of(o, enclose="text"),
                "reply": _reply_to}[make](wire[0])

    async with TestSessionFactory() as db:
        row = await EmailService().ingest_raw(db, incoming, account_address="noreply@vx.test",
                                              tenant_id="default")
        await db.commit()
        rows = (await db.execute(select(EmailMessageModel))).scalars().all()
    assert len(rows) == 2, "the bounce / reply is still recorded"
    assert row.direction == "inbound"
    for x in rows:
        assert "864209" not in (x.body_text or "") + (x.body_html or "")


async def test_an_unrelated_reply_keeps_its_body():
    from case_service.mail import EmailService
    from email.mime.text import MIMEText

    m = MIMEText("Here is the invoice you asked for.")
    m["From"], m["To"], m["Subject"] = "bob@vx.test", "noreply@vx.test", "Re: invoice"
    m["Message-ID"], m["In-Reply-To"] = "<r2@vx.test>", "<something-else@vx.test>"
    async with TestSessionFactory() as db:
        row = await EmailService().ingest_raw(db, m.as_bytes(), account_address="noreply@vx.test")
        await db.commit()
    assert "invoice you asked for" in row.body_text
