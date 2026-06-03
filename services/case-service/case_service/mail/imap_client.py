"""Inbound IMAP — uses stdlib imaplib (thread executor) for broad server compatibility."""
from __future__ import annotations
import asyncio
import imaplib
import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)


def _imap_mailbox(name: str) -> str:
    """Return an IMAP-safe mailbox name.

    Python's imaplib on this platform does not auto-quote folder names, so
    any name containing spaces (e.g. '[Gmail]/All Mail') must be wrapped in
    double-quotes before being passed to select().
    """
    if name.startswith('"') and name.endswith('"'):
        return name
    if ' ' in name or any(c in name for c in '\\()%*"'):
        return '"' + name.replace('\\', '\\\\').replace('"', '\\"') + '"'
    return name


def _fetch_sync(
    *, host: str, port: int, username: str, password: str,
    folder: str = "INBOX", use_ssl: bool = True, max_messages: int = 200,
    since_days: int = 30,
) -> list[bytes]:
    out: list[bytes] = []
    try:
        M = imaplib.IMAP4_SSL(host, port) if use_ssl else imaplib.IMAP4(host, port)
    except Exception as e:
        log.warning("IMAP connect failed (%s:%s): %s", host, port, e)
        return out
    try:
        typ, detail = M.login(username, password)
        if typ != "OK":
            log.warning("IMAP login failed for %s: %s %s", username, typ, detail)
            return out

        typ, _ = M.select(_imap_mailbox(folder))
        if typ != "OK":
            log.warning("IMAP SELECT %r failed for %s", folder, username)
            M.logout()
            return out

        # Search for all messages since N days ago — not just UNSEEN.
        # Already-ingested messages are deduplicated in ingest_raw by message_id.
        # Newest messages have the highest sequence numbers, so we take the tail.
        since_str = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%d-%b-%Y")
        typ, data = M.search(None, "SINCE", since_str)
        if typ != "OK" or not data or not data[0]:
            M.close()
            M.logout()
            return out

        ids = data[0].split()
        # Newest first — ensures recent mail is always picked up even if window is large
        ids = ids[::-1][:max_messages]

        for msg_num in ids:
            # BODY.PEEK[] fetches the full message WITHOUT setting the \Seen flag,
            # so polling does not mark emails as read in Gmail.
            typ2, msg_data = M.fetch(msg_num, "(BODY.PEEK[])")
            if typ2 != "OK" or not msg_data:
                continue
            for part in msg_data:
                if isinstance(part, tuple) and len(part) == 2 and isinstance(part[1], bytes):
                    out.append(part[1])
                    break

        M.close()
        M.logout()
    except Exception as e:
        log.warning("IMAP fetch failed (%s:%s folder=%s): %s", host, port, folder, e)
        try:
            M.logout()
        except Exception:
            pass
    return out


async def fetch_new_messages(
    *, host: str, port: int, username: str, password: str,
    folder: str = "INBOX", use_ssl: bool = True, max_messages: int = 200,
) -> list[bytes]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        lambda: _fetch_sync(
            host=host, port=port, username=username, password=password,
            folder=folder, use_ssl=use_ssl, max_messages=max_messages,
        ),
    )
