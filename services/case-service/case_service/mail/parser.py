"""RFC 822 parser — uses Python's stdlib email module."""
from __future__ import annotations
from datetime import datetime, timezone
from email import message_from_bytes
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime, getaddresses
from typing import Any


def _decode(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="replace")
        except Exception:
            value = value.decode("latin-1", errors="replace")
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def _addrs(value) -> list[str]:
    if not value:
        return []
    return [a for _, a in getaddresses([value]) if a]


def _extract_bodies(msg) -> tuple[str, str | None]:
    text_part = None
    html_part = None
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                continue
            if ctype == "text/plain" and text_part is None:
                payload = part.get_payload(decode=True) or b""
                text_part = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            elif ctype == "text/html" and html_part is None:
                payload = part.get_payload(decode=True) or b""
                html_part = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    else:
        payload = msg.get_payload(decode=True) or b""
        body = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
        if msg.get_content_type() == "text/html":
            html_part = body; text_part = ""
        else:
            text_part = body
    return (text_part or "", html_part)


def parse_rfc822(raw: bytes) -> dict[str, Any]:
    msg = message_from_bytes(raw)

    received_at = None
    date_hdr = msg.get("Date")
    if date_hdr:
        try:
            received_at = parsedate_to_datetime(date_hdr)
            if received_at and received_at.tzinfo is None:
                received_at = received_at.replace(tzinfo=timezone.utc)
        except Exception:
            received_at = None

    text, html = _extract_bodies(msg)

    refs_raw = msg.get("References") or ""
    refs = [r for r in refs_raw.split() if r]

    headers = {k: _decode(v) for k, v in msg.items()}

    return {
        "message_id": (msg.get("Message-Id") or "").strip() or None,
        "in_reply_to": (msg.get("In-Reply-To") or "").strip() or None,
        "references": refs,
        "from_address": (_addrs(msg.get("From"))[:1] or [""])[0],
        "to_addresses": _addrs(msg.get("To")) + _addrs(msg.get("Delivered-To")),
        "cc_addresses": _addrs(msg.get("Cc")),
        "subject": _decode(msg.get("Subject")),
        "body_text": text,
        "body_html": html,
        "received_at": received_at or datetime.now(timezone.utc),
        "raw_headers": headers,
    }
