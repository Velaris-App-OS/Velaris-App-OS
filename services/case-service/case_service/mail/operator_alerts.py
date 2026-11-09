"""Operator alerts by email — the backup and update watchers' second channel.

Push reaches only admins who registered a device; an operator who never did
learned of a failed backup from nobody. This sends the same alert to the
addresses in VELARIS_CASE_ALERT_EMAIL through a SYSTEM mailbox
(VELARIS_CASE_ALERT_SMTP_*), separate from any tenant's own mail account.

Unset = off, exactly as before: no host or no recipients means nothing is
sent and nothing fails. A bad port or an unreachable relay is logged, never
raised — an alert path must not take down the loop that raises alerts. The
password is never logged.

    python -m case_service.mail.operator_alerts --test     # send one test alert

Placement: case-service, beside the mail client and the two watchers; it
moves to worker-service with them (Nov_TODO R5e).

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import re
import socket
import uuid

logger = logging.getLogger(__name__)

_ADDR = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")


def configured(s=None) -> bool:
    if s is None:
        from velaris_shared.config import get_settings
        s = get_settings()
    return bool(s.alert_smtp_host.strip() and recipients(s))


def recipients(s) -> list[str]:
    return [a.strip() for a in (s.alert_email or "").split(",") if _ADDR.match(a.strip())]


def _port(s) -> int:
    tls = (s.alert_smtp_tls or "starttls").lower()
    default = 465 if tls == "ssl" else (25 if tls == "none" else 587)
    try:
        return int(str(s.alert_smtp_port).strip() or default)
    except ValueError:
        logger.warning("VELARIS_CASE_ALERT_SMTP_PORT=%r is not a number — using %d", s.alert_smtp_port, default)
        return default


async def send_operator_alert(subject: str, body: str, *, settings=None) -> bool:
    """Email an operator alert. True if handed to the relay; False if not
    configured or not delivered (logged)."""
    from email.mime.text import MIMEText
    from email.utils import formatdate, make_msgid

    if settings is None:
        from velaris_shared.config import get_settings
        settings = get_settings()
    s = settings
    to = recipients(s)
    if not (s.alert_smtp_host.strip() and to):
        return False
    host = socket.gethostname()
    sender = s.alert_smtp_from.strip() or (s.alert_smtp_username.strip() if "@" in s.alert_smtp_username
                                           else f"velaris@{host}")
    msg = MIMEText(f"{body}\n\n— Velaris on {host}\n", "plain", "utf-8")
    msg["Subject"] = f"[Velaris] {subject}"
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-Id"] = make_msgid(idstring=uuid.uuid4().hex[:12], domain=host)

    tls = (s.alert_smtp_tls or "starttls").lower()
    relay = s.alert_smtp_host.strip()
    # "none" is for a relay on THIS host. Credentials in clear text to anything
    # else would put the mailbox password on the network — refused, loudly.
    if tls == "none" and s.alert_smtp_username.strip() and relay not in ("127.0.0.1", "localhost", "::1"):
        logger.error("operator alert email NOT sent: VELARIS_CASE_ALERT_SMTP_TLS=none with a username "
                     "to a remote relay (%s) would send the password unencrypted — use starttls or ssl", relay)
        return False
    kwargs = {"hostname": relay, "port": _port(s), "timeout": 20.0}
    if tls == "ssl":
        kwargs["use_tls"] = True
    elif tls != "none":
        kwargs["start_tls"] = True
    else:
        kwargs["start_tls"] = False
    if s.alert_smtp_username.strip():
        kwargs["username"] = s.alert_smtp_username.strip()
        kwargs["password"] = s.alert_smtp_password or ""
    try:
        import aiosmtplib
        await aiosmtplib.send(msg, **kwargs)
        logger.info("operator alert emailed to %d recipient(s): %s", len(to), subject)
        return True
    except Exception as exc:
        # The exception text never includes the password; the host and port do.
        logger.warning("operator alert email NOT sent (%s:%s): %s",
                       kwargs["hostname"], kwargs["port"], type(exc).__name__ + ": " + str(exc)[:200])
        return False


if __name__ == "__main__":   # pragma: no cover — operator tool
    import argparse
    import asyncio
    import sys

    p = argparse.ArgumentParser(description="Send a test operator alert email.")
    p.add_argument("--test", action="store_true", required=True)
    a = p.parse_args()
    from velaris_shared.config import get_settings
    st = get_settings()
    if not configured(st):
        print("✗ not configured — set VELARIS_CASE_ALERT_SMTP_HOST and VELARIS_CASE_ALERT_EMAIL")
        sys.exit(1)
    ok = asyncio.run(send_operator_alert(
        "Test alert", "This is a test of Velaris operator alerts. Backup and update "
        "problems will arrive here.", settings=st))
    print("✓ sent to " + ", ".join(recipients(st)) if ok else "✗ not sent — see the log line above")
    sys.exit(0 if ok else 1)
