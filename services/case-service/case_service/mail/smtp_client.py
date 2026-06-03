"""Outbound SMTP — async via aiosmtplib."""
from __future__ import annotations
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate
from typing import Optional


def build_mime_message(
    *, from_address: str, to_addresses: list[str], cc_addresses: list[str] | None,
    subject: str, body_text: str, body_html: Optional[str],
    message_id: str, in_reply_to: Optional[str], references: list[str] | None,
) -> MIMEMultipart:
    msg = MIMEMultipart("alternative")
    msg["From"] = from_address
    msg["To"] = ", ".join(to_addresses)
    if cc_addresses:
        msg["Cc"] = ", ".join(cc_addresses)
    msg["Subject"] = subject
    msg["Message-Id"] = message_id
    msg["Date"] = formatdate(localtime=True)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = " ".join(references)
    msg.attach(MIMEText(body_text or "", "plain", "utf-8"))
    if body_html:
        msg.attach(MIMEText(body_html, "html", "utf-8"))
    return msg


async def send_via_smtp(
    *, mime_message, host: str, port: int,
    username: Optional[str], password: Optional[str],
    use_tls: bool, timeout: float = 30.0,
) -> None:
    import aiosmtplib
    kwargs = {"hostname": host, "port": port, "timeout": timeout}
    if use_tls and port == 465:
        kwargs["use_tls"] = True
    elif use_tls:
        kwargs["start_tls"] = True
    if username:
        kwargs["username"] = username
        kwargs["password"] = password or ""
    await aiosmtplib.send(mime_message, **kwargs)
