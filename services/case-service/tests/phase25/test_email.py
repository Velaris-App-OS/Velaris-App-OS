"""HELIX P25 — Email integration tests."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from httpx import AsyncClient

from case_service.mail.templates import render_template, TemplateError
from case_service.mail.threader import (
    build_subject_tag, extract_subject_tag, build_message_id,
    resolve_case_id_from_message,
)
from case_service.mail.parser import parse_rfc822
from case_service.mail.service import EmailService
from case_service.db.models import (
    CaseTypeModel, CaseInstanceModel,
    EmailAccountModel, EmailTemplateModel, EmailMessageModel,
)


# ── Fixtures ─────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def ct(session):
    x = CaseTypeModel(
        name="P25-Type", version="1.0.0",
        lifecycle_process_id="lp-p25",
        definition_json={"stages": []},
    )
    session.add(x); await session.flush(); return x


@pytest_asyncio.fixture
async def case(session, ct):
    c = CaseInstanceModel(
        case_type_id=ct.id, case_type_version="1.0.0",
        status="new", priority="medium", data={},
    )
    session.add(c); await session.flush(); return c


@pytest_asyncio.fixture
async def account(session):
    a = EmailAccountModel(
        name="Test", address="bot@helix.test",
        smtp_host="smtp.test", smtp_port=587, smtp_use_tls=True,
        smtp_username="bot", smtp_password="x",
        is_active=True, is_default_outbound=True,
    )
    session.add(a); await session.flush(); return a


# ── Templates ────────────────────────────────────────────────────────

def test_01_jinja2_basic_substitution():
    out = render_template("Hello {{ name }}", {"name": "Alice"}, "jinja2")
    assert out == "Hello Alice"


def test_02_jinja2_conditionals():
    tmpl = "{% if vip %}VIP {% endif %}{{ name }}"
    assert render_template(tmpl, {"vip": True, "name": "B"}, "jinja2") == "VIP B"
    assert render_template(tmpl, {"vip": False, "name": "B"}, "jinja2") == "B"


def test_03_jinja2_strict_undefined_raises():
    with pytest.raises(TemplateError):
        render_template("Hello {{ missing }}", {}, "jinja2")


def test_04_jinja2_sandbox_blocks_dunder_access():
    # Sandbox should prevent attribute access to __class__, __mro__, etc.
    with pytest.raises(TemplateError):
        render_template("{{ ''.__class__.__mro__ }}", {}, "jinja2")


def test_05_fstring_basic():
    out = render_template("Hello {name}", {"name": "Alice"}, "fstring")
    assert out == "Hello Alice"


def test_06_fstring_dotted_path():
    out = render_template("Hi {user.name}", {"user": {"name": "Bob"}}, "fstring")
    assert out == "Hi Bob"


def test_07_fstring_missing_renders_empty():
    out = render_template("X{a}Y", {}, "fstring")
    assert out == "XY"


def test_08_fstring_no_logic_evaluation():
    # f-string mode treats {if ...} as a literal-ish key, no condition
    out = render_template("{a}", {"a": "ok"}, "fstring")
    assert out == "ok"


# ── Threading ────────────────────────────────────────────────────────

def test_09_build_subject_tag_format():
    cid = uuid.UUID("abcdef12-3456-7890-abcd-ef1234567890")
    assert build_subject_tag(cid) == "[HELIX-abcdef12]"


def test_10_extract_subject_tag_finds_in_reply():
    assert extract_subject_tag("Re: [HELIX-deadbeef] Order confirmation") == "deadbeef"


def test_11_extract_subject_tag_returns_none():
    assert extract_subject_tag("No tag here") is None


def test_12_build_message_id_uniqueness():
    cid = uuid.uuid4()
    a = build_message_id(cid)
    b = build_message_id(cid)
    assert a != b
    assert a.startswith("<helix-")
    assert "@helix.local>" in a


@pytest.mark.asyncio
async def test_13_resolve_via_in_reply_to(session, case, account):
    # Seed a prior outbound with a known Message-Id
    msg_id = "<helix-prior-xxxx@helix.local>"
    out = EmailMessageModel(
        case_id=case.id, direction="outbound", account_id=account.id,
        message_id=msg_id, from_address="bot@helix.test",
        to_addresses=["a@x.com"], subject="Hello", body_text="hi",
    )
    session.add(out); await session.flush()

    cid = await resolve_case_id_from_message(
        session, in_reply_to=msg_id, references=[], subject="some unrelated",
    )
    assert cid == case.id


@pytest.mark.asyncio
async def test_14_resolve_via_subject_tag_when_headers_miss(session, case):
    short = str(case.id)[:8]
    cid = await resolve_case_id_from_message(
        session, in_reply_to=None, references=[],
        subject=f"Re: [HELIX-{short}] Help",
    )
    assert cid == case.id


@pytest.mark.asyncio
async def test_15_resolve_returns_none_when_nothing_matches(session):
    cid = await resolve_case_id_from_message(
        session, in_reply_to=None, references=[], subject="Random",
    )
    assert cid is None


# ── Parser ───────────────────────────────────────────────────────────

def test_16_parse_simple_text_email():
    raw = (
        b"From: alice@example.com\r\n"
        b"To: bot@helix.test\r\n"
        b"Subject: Test subject\r\n"
        b"Message-Id: <abc@example.com>\r\n"
        b"Date: Wed, 1 Jan 2026 12:00:00 +0000\r\n"
        b"\r\n"
        b"Hello, this is the body.\r\n"
    )
    parsed = parse_rfc822(raw)
    assert parsed["from_address"] == "alice@example.com"
    assert "bot@helix.test" in parsed["to_addresses"]
    assert parsed["subject"] == "Test subject"
    assert parsed["message_id"] == "<abc@example.com>"
    assert "Hello, this is the body" in parsed["body_text"]


def test_17_parse_multipart_attachment_stripped():
    raw = (
        b"From: a@b.com\r\nTo: c@d.com\r\nSubject: M\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="BOUND"\r\n'
        b"\r\n"
        b"--BOUND\r\nContent-Type: text/plain\r\n\r\nBody text\r\n"
        b"--BOUND\r\nContent-Type: application/octet-stream\r\n"
        b'Content-Disposition: attachment; filename="x.bin"\r\n\r\n'
        b"BINARY-PAYLOAD-SHOULD-NOT-APPEAR\r\n"
        b"--BOUND--\r\n"
    )
    parsed = parse_rfc822(raw)
    assert "Body text" in parsed["body_text"]
    assert "BINARY-PAYLOAD" not in parsed["body_text"]


def test_18_parse_charset_decoding():
    raw = (
        b"From: a@b.com\r\nTo: c@d.com\r\nSubject: Encoded\r\n"
        b'Content-Type: text/plain; charset="utf-8"\r\n\r\n'
        b"\xe2\x9c\x93 done\r\n"
    )
    parsed = parse_rfc822(raw)
    assert "✓" in parsed["body_text"]


def test_19_parse_references_chain():
    raw = (
        b"From: a@b.com\r\nTo: c@d.com\r\nSubject: Re: x\r\n"
        b"In-Reply-To: <one@x>\r\n"
        b"References: <one@x> <two@x>\r\n\r\n"
        b"Body\r\n"
    )
    parsed = parse_rfc822(raw)
    assert parsed["in_reply_to"] == "<one@x>"
    assert parsed["references"] == ["<one@x>", "<two@x>"]


# ── ingest_raw + threading ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_20_ingest_links_to_case_via_subject(session, case):
    short = str(case.id)[:8]
    raw = (
        f"From: u@x.com\r\nTo: bot@helix.test\r\n"
        f"Subject: Re: [HELIX-{short}] Question\r\n"
        f"Message-Id: <new1@x.com>\r\n\r\n"
        f"Reply body\r\n"
    ).encode()
    svc = EmailService()
    msg = await svc.ingest_raw(session, raw)
    assert msg.case_id == case.id
    assert msg.direction == "inbound"
    assert msg.status == "received"


@pytest.mark.asyncio
async def test_21_ingest_unmatched_when_no_thread_info(session):
    raw = (
        b"From: u@x.com\r\nTo: bot@helix.test\r\nSubject: Random\r\n"
        b"Message-Id: <stranger1@x.com>\r\n\r\nNo idea what case\r\n"
    )
    svc = EmailService()
    msg = await svc.ingest_raw(session, raw)
    assert msg.case_id is None
    assert msg.status == "unmatched"


@pytest.mark.asyncio
async def test_22_ingest_idempotent(session):
    raw = (
        b"From: u@x.com\r\nTo: bot@helix.test\r\nSubject: X\r\n"
        b"Message-Id: <dup@x.com>\r\n\r\nBody\r\n"
    )
    svc = EmailService()
    a = await svc.ingest_raw(session, raw)
    b = await svc.ingest_raw(session, raw)
    assert a.id == b.id  # second ingest returns the existing


# ── send (mocked SMTP) ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_23_send_with_inline_subject_body(session, account, case, monkeypatch):
    sent: list = []
    async def fake_send(*, mime_message, **kwargs):
        sent.append(mime_message["Subject"])
    monkeypatch.setattr(
        "case_service.mail.service.send_via_smtp", fake_send,
    )
    svc = EmailService()
    msg = await svc.send(
        session, case_id=case.id, account=account,
        to_addresses=["a@b.com"], subject="Hello", body_text="hi",
    )
    assert msg.status == "sent"
    assert sent and "[HELIX-" in sent[0]  # subject got tagged
    assert msg.message_id and msg.message_id.startswith("<helix-")


@pytest.mark.asyncio
async def test_24_send_dry_run_does_not_call_smtp(session, account, monkeypatch):
    called = []
    async def fake_send(**kwargs): called.append(1)
    monkeypatch.setattr("case_service.mail.service.send_via_smtp", fake_send)
    svc = EmailService()
    msg = await svc.send(
        session, case_id=None, account=account,
        to_addresses=["a@b.com"], subject="Hi", body_text="x", dry_run=True,
    )
    assert msg.status == "dry_run"
    assert called == []


@pytest.mark.asyncio
async def test_25_send_records_failure_on_smtp_error(session, account, monkeypatch):
    async def boom(**kwargs):
        raise RuntimeError("connection refused")
    monkeypatch.setattr("case_service.mail.service.send_via_smtp", boom)
    svc = EmailService()
    msg = await svc.send(
        session, case_id=None, account=account,
        to_addresses=["a@b.com"], subject="Hi", body_text="x",
    )
    assert msg.status == "failed"
    assert "connection refused" in (msg.error_message or "")


# ── API ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_26_api_template_render_preview(client: AsyncClient):
    r = await client.post("/api/v1/email/templates/render-preview", json={
        "subject": "Hello {{ n }}",
        "body_text": "Body {{ n }}",
        "engine": "jinja2",
        "ctx": {"n": "World"},
    })
    assert r.status_code == 200
    body = r.json()
    assert body["subject"] == "Hello World"
    assert body["body_text"] == "Body World"


@pytest.mark.asyncio
async def test_27_api_render_preview_rejects_bad_template(client: AsyncClient):
    r = await client.post("/api/v1/email/templates/render-preview", json={
        "subject": "Hello {{ missing }}",
        "body_text": "x",
        "engine": "jinja2",
        "ctx": {},
    })
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_28_api_create_template(client: AsyncClient):
    r = await client.post("/api/v1/email/templates", json={
        "name": "Welcome",
        "subject": "Welcome {{ name }}",
        "body_text": "Hi {{ name }}",
        "engine": "jinja2", "scope": "global",
    })
    assert r.status_code == 201
    assert r.json()["name"] == "Welcome"


@pytest.mark.asyncio
async def test_29_api_inbox_stats(client: AsyncClient):
    r = await client.get("/api/v1/email/inbox/stats")
    assert r.status_code == 200
    body = r.json()
    for k in ("unread_inbound", "unmatched_inbound", "failed_outbound"):
        assert k in body


@pytest.mark.asyncio
async def test_30_api_list_messages_filters(client: AsyncClient, session, case):
    # Seed one inbound unmatched
    m = EmailMessageModel(
        case_id=None, direction="inbound", from_address="x@y.com",
        to_addresses=["bot@helix.test"], subject="hi",
        body_text="x", status="unmatched", is_read=False,
    )
    session.add(m); await session.commit()

    r = await client.get("/api/v1/email/messages?direction=inbound&unmatched_only=true")
    assert r.status_code == 200
    rows = r.json()
    assert any(row["id"] == str(m.id) for row in rows)
