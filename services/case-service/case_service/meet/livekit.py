"""HxMeet P2 — LiveKit token minting and webhook verification (no SDK).

A LiveKit access token is a plain HS256 JWT (iss = API key, sub = identity,
a `video` grant naming the room) signed with the API secret, and LiveKit
webhooks authenticate with the same keypair (Authorization = JWT whose
`sha256` claim hashes the raw body). Both are hand-rolled on PyJWT here —
same no-SDK spine as HxMCP's JSON-RPC — so the server SDK never becomes a
dependency. The API secret is server-side only; the browser only ever sees
a minted, room-scoped, short-TTL token.

Copyright (c) 2024-2026 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import base64
import hashlib
import time
import uuid

import jwt

from case_service.config import get_settings


def configured() -> bool:
    """Embedded driver is available only with url + key + secret set (fail closed)."""
    s = get_settings()
    return bool(s.livekit_url and s.livekit_api_key and s.livekit_api_secret)


def room_name(tenant_id: str, session_id: uuid.UUID) -> str:
    """Tenant-namespaced room; the session UUID is the last 36 chars (see parse)."""
    return f"vx-{tenant_id}-{session_id}"


def parse_room_session_id(room: str) -> uuid.UUID | None:
    """Recover the session id from a room name (tenant slugs may contain '-')."""
    try:
        return uuid.UUID(room[-36:])
    except (ValueError, IndexError):
        return None


def mint_access_token(*, room: str, identity: str, display_name: str | None = None) -> str:
    """Room-scoped, identity-pinned, short-TTL LiveKit access token."""
    s = get_settings()
    now = int(time.time())
    payload = {
        "iss": s.livekit_api_key,
        "sub": identity,
        "jti": identity,
        "nbf": now - 5,
        "exp": now + s.meet_token_ttl_seconds,
        "video": {
            "room": room,
            "roomJoin": True,
            "canPublish": True,
            "canSubscribe": True,
            "canPublishData": True,
        },
    }
    if display_name:
        payload["name"] = display_name
    return jwt.encode(payload, s.livekit_api_secret, algorithm="HS256")


def verify_access_token(token: str, *, room: str) -> dict | None:
    """Verify a LiveKit access token WE minted and pin it to a room.

    Room membership proof for the caption stream: everyone in an embedded
    session (worker or guest) holds one of these, and nobody outside does.
    Returns the claims on success, None on any failure (uniform reject)."""
    s = get_settings()
    try:
        claims = jwt.decode(token, s.livekit_api_secret, algorithms=["HS256"])
    except Exception:
        return None
    if claims.get("iss") != s.livekit_api_key:
        return None
    if (claims.get("video") or {}).get("room") != room:
        return None
    return claims


def _http_url() -> str:
    """HTTP base for Twirp API calls (Egress) — ws(s) scheme swapped to http(s)."""
    s = get_settings()
    if s.livekit_http_url:
        return s.livekit_http_url.rstrip("/")
    return s.livekit_url.replace("wss://", "https://").replace("ws://", "http://").rstrip("/")


def _service_token(grants: dict) -> str:
    """Short-lived server-to-server token for LiveKit service APIs."""
    s = get_settings()
    now = int(time.time())
    return jwt.encode(
        {"iss": s.livekit_api_key, "sub": "velaris-case-service",
         "nbf": now - 5, "exp": now + 60, "video": grants},
        s.livekit_api_secret, algorithm="HS256",
    )


async def _twirp(service: str, method: str, body: dict, grants: dict) -> dict:
    """Hand-rolled LiveKit Twirp call (no SDK) — loopback server, no SSRF surface."""
    import httpx
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{_http_url()}/twirp/livekit.{service}/{method}",
            json=body,
            headers={"Authorization": f"Bearer {_service_token(grants)}"},
        )
    if resp.status_code != 200:
        raise RuntimeError(f"LiveKit {service}.{method} failed ({resp.status_code}): {resp.text[:200]}")
    return resp.json()


async def start_room_recording(room: str, filepath: str) -> str:
    """Start a RoomComposite egress writing an MP4 to the shared recordings
    dir (the egress worker's local file output). Returns the egress id."""
    info = await _twirp("Egress", "StartRoomCompositeEgress", {
        "room_name": room,
        "file_outputs": [{"file_type": "MP4", "filepath": filepath}],
    }, grants={"roomRecord": True, "room": room, "roomAdmin": True})
    egress_id = info.get("egressId") or info.get("egress_id")
    if not egress_id:
        raise RuntimeError(f"LiveKit egress returned no id: {info}")
    return egress_id


async def stop_room_recording(egress_id: str) -> None:
    await _twirp("Egress", "StopEgress", {"egress_id": egress_id},
                 grants={"roomRecord": True, "roomAdmin": True})


def verify_webhook(authorization: str | None, raw_body: bytes) -> bool:
    """Authenticate a LiveKit webhook: JWT signed with our API secret whose
    `sha256` claim matches the raw request body. Any failure = reject."""
    if not authorization or not configured():
        return False
    s = get_settings()
    try:
        claims = jwt.decode(
            authorization.strip(), s.livekit_api_secret, algorithms=["HS256"],
            options={"verify_exp": False},  # LiveKit webhook tokens carry no exp
        )
    except Exception:
        return False
    if claims.get("iss") != s.livekit_api_key:
        return False
    # LiveKit SDKs encode the body hash as base64; accept hex too — same digest.
    digest = hashlib.sha256(raw_body)
    claimed = claims.get("sha256")
    return claimed in (
        base64.b64encode(digest.digest()).decode(),
        digest.hexdigest(),
    )
