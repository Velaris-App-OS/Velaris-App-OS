"""Group J — device-bound refresh sessions.

Every login attaches the refresh-token chain to an auth_devices row.
Rotation checks the device on every refresh: a revoked device or a
user-agent that no longer matches kills the whole chain, so a stolen
refresh token replayed from different software dies on first use.

The user-agent hash covers browser family + OS only — never the version —
so routine browser updates do not log anyone out.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import AuthDeviceModel

log = logging.getLogger(__name__)

_BROWSERS = [  # order matters: Edge/Opera ship "Chrome/" in their UA too
    ("Edg/", "Edge"),
    ("OPR/", "Opera"),
    ("SamsungBrowser/", "Samsung Internet"),
    ("Firefox/", "Firefox"),
    ("Chrome/", "Chrome"),
    ("Safari/", "Safari"),
]
_PLATFORMS = [
    ("Windows", "Windows"),
    ("Android", "Android"),       # before Linux: Android UAs contain "Linux"
    ("iPhone", "iOS"),
    ("iPad", "iPadOS"),
    ("Mac OS X", "macOS"),
    ("CrOS", "ChromeOS"),
    ("Linux", "Linux"),
]


def parse_user_agent(user_agent: str) -> tuple[str, str]:
    """(device_name, stable_hash) from a raw User-Agent header."""
    ua = user_agent or ""
    browser = next((name for marker, name in _BROWSERS if marker in ua), "Unknown browser")
    platform = next((name for marker, name in _PLATFORMS if marker in ua), "Unknown OS")
    stable = f"{browser}|{platform}"
    return f"{browser} on {platform}", hashlib.sha256(stable.encode()).hexdigest()


async def get_or_create_device(
    session: AsyncSession,
    user_id: str,
    user_agent: str,
    client_ip: str | None,
    claimed_device_id: str | None = None,
) -> AuthDeviceModel:
    """Reuse the client's device row when it checks out, else create one.

    The claimed id is client-supplied and therefore untrusted: it is only
    reused when the row exists, belongs to this user, is not revoked, AND
    the user-agent hash matches. Anything else gets a fresh device row —
    never an error, so a tampered id cannot probe other users' devices.
    """
    name, ua_hash = parse_user_agent(user_agent)

    if claimed_device_id:
        try:
            dev = await session.get(AuthDeviceModel, uuid.UUID(claimed_device_id))
        except ValueError:
            dev = None
        if (
            dev is not None
            and dev.user_id == user_id
            and dev.revoked_at is None
            and dev.user_agent_hash == ua_hash
        ):
            dev.last_seen_at = datetime.now(timezone.utc)
            dev.last_ip = client_ip
            return dev

    dev = AuthDeviceModel(
        id=uuid.uuid4(),
        user_id=user_id,
        device_name=name,
        user_agent_hash=ua_hash,
        first_ip=client_ip,
        last_ip=client_ip,
    )
    session.add(dev)
    await session.flush()
    return dev


async def check_device_on_refresh(
    session: AsyncSession,
    device_id: uuid.UUID | None,
    user_agent: str,
    client_ip: str | None,
) -> bool:
    """Validate the device a refresh token is bound to. True = allowed.

    Pre-Group-J tokens have no device (None) and pass until natural expiry.
    A revoked device fails. A user-agent mismatch (different browser family
    or OS than the device was created with) revokes the device AND all its
    refresh tokens — the re-challenge: whoever holds the chain must log in
    again with credentials.
    """
    if device_id is None:
        return True

    dev = await session.get(AuthDeviceModel, device_id)
    if dev is None or dev.revoked_at is not None:
        return False

    _, ua_hash = parse_user_agent(user_agent)
    if dev.user_agent_hash != ua_hash:
        log.warning(
            "device %s: user-agent mismatch on refresh — revoking session chain",
            device_id,
        )
        await revoke_device(session, dev, revoked_by="ua-mismatch")
        return False

    dev.last_seen_at = datetime.now(timezone.utc)
    dev.last_ip = client_ip
    return True


async def revoke_device(
    session: AsyncSession, dev: AuthDeviceModel, revoked_by: str,
) -> int:
    """Revoke a device and every refresh token bound to it. Returns token count."""
    now = datetime.now(timezone.utc)
    dev.revoked_at = now
    dev.revoked_by = revoked_by
    result = await session.execute(
        text("""
            UPDATE refresh_tokens
            SET    revoked_at = NOW(), revoked_by = :by
            WHERE  device_id = :dev AND revoked_at IS NULL
        """),
        {"dev": str(dev.id), "by": f"device:{revoked_by}"},
    )
    return result.rowcount or 0
