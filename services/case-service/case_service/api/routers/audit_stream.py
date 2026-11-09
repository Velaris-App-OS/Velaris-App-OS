"""Audit streaming to a SIEM — targets, test, status (market gap A3).

Every route here is an administrator's. The endpoint an install ships its audit
to is a security control in its own right: whoever can change it can decide who
sees the platform's security events, and whoever can read the token can write
into the SOC's index.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.auth.dependencies import get_current_user
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.core.error_codes import coded
from velaris_shared.db.models import AuditStreamTargetModel
from velaris_shared.db.session import get_session
from velaris_shared.hxguard import service as hxguard

# 2026-11-04: a target is the platform operator's (no tenant — EVERY event) or
# a tenant's (its own tenant's events only, by security_events.tenant_id,
# migration 230). Until then any tenant's admin, holding `audit.stream.write`,
# could ship the whole install's events to an endpoint of their own. The path
# guard keeps each target to its owner (`null_is_default`).
router = APIRouter(prefix="/audit-stream", tags=["audit-stream"],
                   dependencies=[Depends(get_current_user)])


async def _tenant_of_caller(session: AsyncSession, user) -> str | None:
    """None for the platform operator (a platform target); else the caller's
    tenant slug (a tenant target)."""
    from case_service.tenancy.identity import is_platform_operator, tenant_slug
    if await is_platform_operator(session, user):
        return None
    return await tenant_slug(session, getattr(user, "tenant_id", None))


async def _public_endpoint(url: str) -> None:
    """A tenant's SIEM is on the internet: a tenant may not make this install
    send to its own network (the operator's internal SIEM is unaffected)."""
    from velaris_shared.hxbridge.security import validate_outbound_url
    if urlsplit(url).scheme.lower() != "https":
        raise coded(400, "VEL-AUDIT-STREAM-URL-400", "A tenant's SIEM endpoint must be https.")
    try:
        await validate_outbound_url(url)
    except ValueError:
        raise coded(400, "VEL-AUDIT-STREAM-URL-400",
                    "That endpoint is not reachable from this platform: private and "
                    "internal addresses are refused.")

#: The transports that exist. `splunk_hec` is built and tested; CEF-over-syslog
#: and Azure Monitor are the two named as not built, and adding one is an
#: adapter, not a migration.
KINDS = ("splunk_hec",)


class TargetCreate(BaseModel):
    name: str
    endpoint_url: str
    token: str | None = None
    kind: str = "splunk_hec"
    verify_tls: bool = True
    min_severity: str | None = None
    event_types: list[str] | None = None
    #: Deliberately NOT `enabled`. A target is created, looked at, tested, and
    #: only then switched on: an endpoint that starts receiving the platform's
    #: security events the moment somebody pastes a URL is a decision nobody
    #: made yet.
    acknowledge_platform_scope: bool = Field(
        default=False,
        description="Must be true. The stream carries EVERY tenant's security "
                    "events, because security_events has no tenant column.",
    )


class TargetUpdate(BaseModel):
    name: str | None = None
    endpoint_url: str | None = None
    token: str | None = None
    verify_tls: bool | None = None
    enabled: bool | None = None
    min_severity: str | None = None
    event_types: list[str] | None = None


def _serialise(row: AuditStreamTargetModel) -> dict:
    """What a screen may see.

    The token is **absent**, not masked: a masked field invites a client to
    round-trip it back, and then the stars are the secret.
    """
    return {
        "id": str(row.id),
        "name": row.name,
        "kind": row.kind,
        "endpoint_url": row.endpoint_url,
        "has_token": bool(row.token_encrypted),
        "verify_tls": row.verify_tls,
        "enabled": row.enabled,
        "scope": "platform",
        "min_severity": row.min_severity,
        "event_types": json.loads(row.event_types) if row.event_types else [],
        "cursor_timestamp": row.cursor_timestamp.isoformat() if row.cursor_timestamp else None,
        "last_success_at": row.last_success_at.isoformat() if row.last_success_at else None,
        "last_error": row.last_error,
        "last_error_at": row.last_error_at.isoformat() if row.last_error_at else None,
        "forwarded_count": row.forwarded_count,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _validate_endpoint(url: str) -> str:
    """An https URL naming a host.

    **The calendar's private-address refusal is deliberately NOT applied here.**
    There the URL comes from an end user, so an address inside the estate is an
    SSRF. A SIEM is almost always inside the estate — refusing `10.0.0.0/8`
    would refuse the normal case. What stands in its place is the privilege:
    only `audit.stream.write` reaches this, which is an administrator.
    """
    cleaned = (url or "").strip()
    parts = urlsplit(cleaned)
    if parts.scheme.lower() not in ("http", "https"):
        raise coded(400, "VEL-AUDIT-STREAM-URL-400",
                    "A SIEM endpoint must be an http or https URL.")
    if not parts.hostname:
        raise coded(400, "VEL-AUDIT-STREAM-URL-400", "That URL names no host.")
    if parts.username or parts.password:
        raise coded(400, "VEL-AUDIT-STREAM-URL-400",
                    "Credentials in the URL are refused — the HEC token is a "
                    "field of its own and is stored encrypted.")
    if parts.scheme.lower() == "http" and not (
            parts.hostname == "localhost" or parts.hostname.startswith("127.")):
        raise coded(400, "VEL-AUDIT-STREAM-URL-400",
                    "Plain http would put an authentication token and every "
                    "security event on the wire in clear. Use https; if the "
                    "SIEM has an internal certificate, turn verify_tls off "
                    "rather than turning TLS off.")
    return cleaned


def _encrypt(token: str) -> str:
    from case_service.hxbridge.encryption import encrypt_credentials
    return json.dumps(encrypt_credentials({"token": token}))


async def _get(session: AsyncSession, target_id: uuid.UUID) -> AuditStreamTargetModel:
    row = await session.get(AuditStreamTargetModel, target_id)
    if row is None:
        raise coded(404, "VEL-AUDIT-STREAM-404", "No such audit stream target.")
    return row


@router.get("/targets")
async def list_targets(
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("audit.stream.read")),
):
    # Every tenant's rows until 2026-09-29 (8d).
    from case_service.tenancy.identity import scope_to_caller
    rows = (await session.execute(await scope_to_caller(
        select(AuditStreamTargetModel).order_by(AuditStreamTargetModel.created_at),
        AuditStreamTargetModel, session, _user))).scalars().all()
    return {"items": [_serialise(r) for r in rows], "kinds": list(KINDS)}


@router.post("/targets", status_code=201)
async def create_target(
    body: TargetCreate,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("audit.stream.write")),
):
    """Create a target. It is created DISABLED, always.

    Two acts, not one: create, then enable. The second is where an operator
    says "yes, ship our security events there", and it is worth its own click.
    """
    if body.kind not in KINDS:
        raise coded(400, "VEL-AUDIT-STREAM-KIND-400",
                    f"'{body.kind}' is not a transport this platform speaks. "
                    f"Known: {', '.join(KINDS)}. CEF-over-syslog and Azure "
                    "Monitor are named as not built rather than half-built.")
    tenant = await _tenant_of_caller(session, user)
    if tenant is None and not body.acknowledge_platform_scope:
        raise coded(400, "VEL-AUDIT-STREAM-SCOPE-400",
                    "This stream carries EVERY tenant's security events. Confirm "
                    "with acknowledge_platform_scope. (A tenant's admin creates a "
                    "target carrying its own tenant's events only.)")
    name = (body.name or "").strip()
    if not name:
        raise coded(400, "VEL-AUDIT-STREAM-URL-400", "A target needs a name.")

    endpoint = _validate_endpoint(body.endpoint_url)
    if tenant is not None:
        await _public_endpoint(endpoint)
    tenant_key = "platform" if tenant is None else tenant
    existing = (await session.execute(
        select(AuditStreamTargetModel).where(
            AuditStreamTargetModel.tenant_key == tenant_key,
            AuditStreamTargetModel.name == name,
        )
    )).scalars().first()
    if existing is not None:
        raise coded(409, "VEL-AUDIT-STREAM-409",
                    f"A target named '{name}' already exists.")

    now = datetime.now(timezone.utc)
    row = AuditStreamTargetModel(
        id=uuid.uuid4(),
        tenant_id=tenant,
        tenant_key=tenant_key,
        name=name,
        kind=body.kind,
        endpoint_url=endpoint,
        token_encrypted=_encrypt(body.token) if body.token else None,
        verify_tls=body.verify_tls,
        enabled=False,
        min_severity=(body.min_severity or None),
        event_types=json.dumps(body.event_types) if body.event_types else None,
        # A NEW target starts at NOW, not at the beginning of time. Enabling one
        # on an install with two years of history would otherwise ship two years
        # of events as this morning's backlog, and the SOC would drown on the
        # day they turned it on. History is a deliberate act: clear the cursor.
        cursor_timestamp=now,
        created_at=now,
        updated_at=now,
        created_by=str(getattr(user, "user_id", "") or "") or None,
    )
    session.add(row)
    await session.commit()
    return _serialise(row)


@router.patch("/targets/{target_id}")
async def update_target(
    target_id: uuid.UUID,
    body: TargetUpdate,
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("audit.stream.write")),
):
    row = await _get(session, target_id)
    if body.name is not None:
        row.name = body.name.strip() or row.name
    if body.endpoint_url is not None:
        row.endpoint_url = _validate_endpoint(body.endpoint_url)
        if row.tenant_id is not None:
            await _public_endpoint(row.endpoint_url)
    if body.token is not None:
        # An empty string clears the token; a value replaces it. There is no
        # way to read the old one back, here or anywhere.
        row.token_encrypted = _encrypt(body.token) if body.token else None
    if body.verify_tls is not None:
        row.verify_tls = body.verify_tls
    if body.min_severity is not None:
        row.min_severity = body.min_severity or None
    if body.event_types is not None:
        row.event_types = json.dumps(body.event_types) if body.event_types else None
    if body.enabled is not None:
        if body.enabled and not row.token_encrypted:
            raise coded(409, "VEL-AUDIT-STREAM-409",
                        "This target has no token, so every push would be "
                        "refused by the SIEM. Set one before enabling it.")
        row.enabled = body.enabled
    row.updated_at = datetime.now(timezone.utc)
    await session.commit()
    return _serialise(row)


@router.delete("/targets/{target_id}", status_code=204)
async def delete_target(
    target_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("audit.stream.write")),
):
    row = await _get(session, target_id)
    await session.delete(row)
    await session.commit()


@router.post("/targets/{target_id}/test")
async def test_target(
    target_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("audit.stream.write")),
):
    """Send one synthetic event and report what the SIEM said.

    It does not move the cursor and does not forward a real event: the point is
    to prove the URL, the token and the certificate before anything real is
    trusted to them.
    """
    from case_service.audit_stream.forwarder import AuditStreamError, push

    row = await _get(session, target_id)
    probe = {
        "id": uuid.uuid4(),
        "event_type": "audit_stream.test",
        "severity": "info",
        "user_id": "system",
        "resource_type": "audit_stream",
        "resource_id": str(row.id),
        "action": "test",
        "outcome": "success",
        "ip_address": None,
        "user_agent": None,
        "details": {"note": "Connectivity test from Velaris. No real event was sent."},
        "timestamp": datetime.now(timezone.utc),
    }
    try:
        await push(row, [probe])
    except AuditStreamError as exc:
        row.last_error = str(exc)[:1000]
        row.last_error_at = datetime.now(timezone.utc)
        await session.commit()
        return {"ok": False, "error": str(exc)[:500]}
    row.last_error = None
    row.last_error_at = None
    await session.commit()
    return {"ok": True, "message": "The SIEM accepted a test event."}


@router.post("/targets/{target_id}/run")
async def run_target(
    target_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("audit.stream.write")),
):
    """Forward now, rather than waiting for the schedule.

    Present because the alternative — an operator wondering whether the thing
    works — is how a security control ends up unverified.
    """
    from case_service.audit_stream.forwarder import AuditStreamError, forward_target

    row = await _get(session, target_id)
    if not row.enabled:
        raise coded(409, "VEL-AUDIT-STREAM-409",
                    "This target is disabled. Enable it, or use /test.")
    try:
        # Bounded far tighter than the scheduled run: this one is inside an HTTP
        # request, and 20 batches × a 15s push timeout is a five-minute request.
        result = await forward_target(session, row, max_batches=2)
    except AuditStreamError as exc:
        await session.commit()
        return {"ok": False, "error": str(exc)[:500]}
    await session.commit()
    return {"ok": True, **result}
