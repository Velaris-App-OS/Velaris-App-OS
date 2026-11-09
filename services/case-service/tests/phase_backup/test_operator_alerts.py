"""Operator alert email — off when unset, never fatal, password never logged.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import types

import pytest

from case_service.mail import operator_alerts as oa


def _s(**kw):
    base = dict(alert_email="", alert_smtp_host="", alert_smtp_port="", alert_smtp_username="",
                alert_smtp_password="", alert_smtp_from="", alert_smtp_tls="starttls")
    base.update(kw)
    return types.SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_unset_sends_nothing(monkeypatch):
    called = []
    monkeypatch.setattr("aiosmtplib.send", lambda *a, **k: called.append(1))
    assert await oa.send_operator_alert("x", "y", settings=_s()) is False
    assert await oa.send_operator_alert("x", "y", settings=_s(alert_smtp_host="relay")) is False
    assert called == []


def test_only_wellformed_recipients_count():
    s = _s(alert_email="ops@example.com, not-an-address, , a@b.co")
    assert oa.recipients(s) == ["ops@example.com", "a@b.co"]


def test_port_defaults_follow_tls_and_a_bad_port_does_not_fail():
    assert oa._port(_s(alert_smtp_tls="starttls")) == 587
    assert oa._port(_s(alert_smtp_tls="ssl")) == 465
    assert oa._port(_s(alert_smtp_tls="none")) == 25
    assert oa._port(_s(alert_smtp_port="abc")) == 587


@pytest.mark.asyncio
async def test_a_failing_relay_is_logged_not_raised_and_hides_the_password(monkeypatch, caplog):
    async def boom(*a, **k):
        raise ConnectionRefusedError("refused")
    monkeypatch.setattr("aiosmtplib.send", boom)
    s = _s(alert_email="ops@example.com", alert_smtp_host="relay.test",
           alert_smtp_username="u", alert_smtp_password="hunter2")
    with caplog.at_level(logging.WARNING):
        assert await oa.send_operator_alert("Backup failed", "disk full", settings=s) is False
    assert "NOT sent" in caplog.text and "hunter2" not in caplog.text


@pytest.mark.asyncio
async def test_message_shape(monkeypatch):
    got = {}

    async def fake(msg, **k):
        got.update(msg=msg, kw=k)
    monkeypatch.setattr("aiosmtplib.send", fake)
    s = _s(alert_email="ops@example.com", alert_smtp_host="relay.test", alert_smtp_from="velaris@corp.test")
    assert await oa.send_operator_alert("Backup failed", "disk full", settings=s) is True
    assert got["msg"]["Subject"] == "[Velaris] Backup failed"
    assert got["msg"]["To"] == "ops@example.com" and got["msg"]["From"] == "velaris@corp.test"
    assert got["kw"]["start_tls"] is True and got["kw"]["port"] == 587


@pytest.mark.asyncio
async def test_no_cleartext_password_to_a_remote_relay(monkeypatch):
    sent = []

    async def fake(msg, **k):
        sent.append(k)
    monkeypatch.setattr("aiosmtplib.send", fake)
    remote = _s(alert_email="ops@example.com", alert_smtp_host="smtp.example.com", alert_smtp_tls="none",
                alert_smtp_username="u", alert_smtp_password="p")
    assert await oa.send_operator_alert("x", "y", settings=remote) is False and sent == []
    local = _s(alert_email="ops@example.com", alert_smtp_host="127.0.0.1", alert_smtp_tls="none",
               alert_smtp_username="u", alert_smtp_password="p")
    assert await oa.send_operator_alert("x", "y", settings=local) is True
