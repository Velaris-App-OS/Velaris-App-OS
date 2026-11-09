"""Omnichannel — inbound webhooks, the agent surfaces, and CTI screen-pop.

Four kinds of caller, and each authenticates differently on purpose:

* **the provider** (Twilio, Meta) posts inbound messages, and its only
  credential is the **signature** over the payload. There is no token to give
  it. Every failure — bad signature, missing header, unknown endpoint, inactive
  endpoint — answers the same `404`, so the endpoint id cannot be probed;
* **the agent** replies and browses the inbox with a normal Velaris JWT, gated
  by HxGuard;
* **the administrator** configures endpoints and templates;
* **the contact-centre platform** calls the screen-pop endpoint with an
  operator's token — mode (b) in the design, which works with every CC platform
  on the market and does not put Velaris in the media path.

Velaris never becomes a telephony platform: no SIP, no call control, no
recording storage. A recording is *referenced*, never held.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared import hxguard
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.tenancy.identity import tenant_for_write
from case_service.channels import optout, service, signatures
from velaris_shared.core.error_codes import (
    CHANNEL_NOTFOUND_404,
    CHANNEL_OPTED_OUT_403,
    CHANNEL_WINDOW_409,
    coded,
)
from velaris_shared.db.models import (
    ChannelConversationModel,
    ChannelEndpointModel,
    ChannelMessageModel,
    WhatsAppTemplateModel,
)
from velaris_shared.db.session import get_session
from velaris_shared.middleware.endpoint_rate_limit import rate_limit
from velaris_shared.db.read_routing import get_read_session

log = logging.getLogger(__name__)

router = APIRouter(prefix="/channels", tags=["channels"])

#: Inbound is unauthenticated apart from the signature and does an HMAC per
#: request. A provider's real traffic is far below this; a flood is not.
_inbound_rl = rate_limit(max_calls=600, window_seconds=60, name="channels-inbound")

#: The largest inbound webhook body we will read. Providers send a few KB.
MAX_INBOUND_BYTES = 256 * 1024


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── inbound ───────────────────────────────────────────────────────────────

def _secret_of(endpoint: ChannelEndpointModel) -> str:
    from case_service.hxbridge.encryption import decrypt_credentials

    blob = endpoint.signing_secret_enc
    if not blob:
        return ""
    try:
        return str((decrypt_credentials(blob) or {}).get("secret") or "")
    except Exception:
        log.exception("channel endpoint %s has an unreadable signing secret", endpoint.id)
        return ""


@router.post("/{provider}/inbound/{endpoint_id}", dependencies=[Depends(_inbound_rl)])
async def inbound(
    provider: str,
    endpoint_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Receive one message from a provider.

    **Everything here fails closed to the same 404.** A distinct "unknown
    endpoint" would make this an enumeration oracle, and a distinct "bad
    signature" would tell a forger their endpoint id was right.

    The body is read as **raw bytes before parsing**, because Meta signs the
    exact bytes — re-serialising parsed JSON changes whitespace and key order
    and the MAC stops matching.
    """
    refused = coded(404, CHANNEL_NOTFOUND_404, "Not found.")

    if provider not in signatures.SUPPORTED:
        raise refused
    try:
        endpoint_key = uuid.UUID(endpoint_id)
    except ValueError:
        raise refused

    body = await request.body()
    if len(body) > MAX_INBOUND_BYTES:
        raise refused

    endpoint = await session.get(ChannelEndpointModel, endpoint_key)
    if endpoint is None or not endpoint.is_active or endpoint.provider != provider:
        raise refused

    secret = _secret_of(endpoint)
    header = request.headers.get(signatures.signature_header(provider))

    form: dict[str, str] = {}
    payload: dict[str, Any] = {}
    if provider == "twilio":
        parsed = await request.form()
        form = signatures.form_to_params(parsed)
        payload = dict(form)
    else:
        import json

        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            raise refused
        if not isinstance(payload, dict):
            raise refused

    # Twilio signs the URL it was configured with. `request.url` is derived from
    # the Host header, which something upstream may set — so the endpoint's own
    # `routing.webhook_url` wins when configured, and the request URL is only a
    # fallback for deployments that have not set one.
    signed_url = str((endpoint.routing or {}).get("webhook_url") or request.url)

    if not signatures.verify(
        provider, secret, body=body, header=header, url=signed_url, form=form,
    ):
        log.warning("channel inbound refused: bad signature on endpoint %s", endpoint.id)
        raise refused

    peer, text, provider_id, media = _extract(provider, payload)
    if not peer:
        raise refused

    result = await service.ingest_inbound(
        session,
        endpoint=endpoint,
        peer_address=peer,
        body=text,
        provider_message_id=provider_id,
        raw_payload=payload,
        media=media,
    )
    await session.commit()

    # 200 with a minimal body: providers retry on anything else, and a retry of
    # a message we already stored is wasted work on both sides.
    return {"received": True, "conversation_id": result.conversation_id,
            "duplicate": result.duplicate}


def _extract(provider: str, payload: dict) -> tuple[str, str, str | None, list[dict]]:
    """Provider payload → (peer, body, provider_message_id, media).

    Kept dumb on purpose: anything clever here runs on unauthenticated input.
    """
    if provider == "twilio":
        return (
            str(payload.get("From") or "").strip(),
            str(payload.get("Body") or ""),
            str(payload.get("MessageSid") or "") or None,
            [],
        )
    if provider == "meta":
        try:
            value = payload["entry"][0]["changes"][0]["value"]
            message = (value.get("messages") or [{}])[0]
        except (KeyError, IndexError, TypeError):
            return "", "", None, []
        return (
            str(message.get("from") or "").strip(),
            str((message.get("text") or {}).get("body") or ""),
            str(message.get("id") or "") or None,
            [],
        )
    return (
        str(payload.get("from") or "").strip(),
        str(payload.get("body") or ""),
        str(payload.get("message_id") or "") or None,
        list(payload.get("media") or []),
    )


# ── agent surfaces ────────────────────────────────────────────────────────

def _conversation_dict(c: ChannelConversationModel, endpoint_address: str | None = None) -> dict:
    return {
        "id": str(c.id),
        "endpoint_id": str(c.endpoint_id),
        "endpoint_address": endpoint_address,
        "tenant_id": c.tenant_id,
        "peer_address": c.peer_address,
        "case_id": str(c.case_id) if c.case_id else None,
        "status": c.status,
        "assigned_to": c.assigned_to,
        "unread_count": c.unread_count,
        "last_message_at": c.last_message_at.isoformat() if c.last_message_at else None,
        "window_expires_at": c.window_expires_at.isoformat() if c.window_expires_at else None,
    }


def _message_dict(m: ChannelMessageModel) -> dict:
    return {
        "id": str(m.id),
        "direction": m.direction,
        "channel": m.channel,
        "author": m.author,
        "peer_address": m.peer_address,
        "body": m.body,
        "media": m.media or [],
        "template_name": m.template_name,
        "status": m.status,
        "error": m.error,
        "is_read": m.is_read,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


async def _scoped(stmt, user: AuthenticatedUser, session: AsyncSession | None = None):
    """Tenant scoping for an operator. A superadmin sees every tenant.

    A caller with no tenant claim is the default tenant, never every tenant —
    until 2026-09-17 it listed every tenant's conversations.
    """
    if "superadmin" in (user.roles or []):
        return stmt
    if user.tenant_id:
        return stmt.where(ChannelConversationModel.tenant_id == user.tenant_id)
    from sqlalchemy import or_ as _or
    from case_service.tenancy.identity import DEFAULT_TENANT, default_tenant_tokens
    tokens = await default_tenant_tokens(session) if session is not None else [DEFAULT_TENANT]
    return stmt.where(_or(ChannelConversationModel.tenant_id.is_(None),
                          ChannelConversationModel.tenant_id.in_(tokens)))


@router.get("/conversations")
async def list_conversations(
    status: Optional[str] = Query(None),
    case_id: Optional[uuid.UUID] = Query(None),
    unread_only: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("channels.conversations.read")),
):
    """The channel inbox."""
    stmt = await _scoped(select(ChannelConversationModel), user, session)
    if status:
        stmt = stmt.where(ChannelConversationModel.status == status)
    if case_id:
        stmt = stmt.where(ChannelConversationModel.case_id == case_id)
    if unread_only:
        stmt = stmt.where(ChannelConversationModel.unread_count > 0)

    rows = (await session.execute(
        stmt.order_by(desc(ChannelConversationModel.last_message_at)).limit(limit)
    )).scalars().all()

    addresses = dict((await session.execute(
        select(ChannelEndpointModel.id, ChannelEndpointModel.address)
    )).all())
    return {"conversations": [
        _conversation_dict(c, addresses.get(c.endpoint_id)) for c in rows
    ]}


@router.get("/conversations/stats")
async def inbox_stats(
    session: AsyncSession = Depends(get_read_session),
    user: AuthenticatedUser = Depends(hxguard.guard("channels.conversations.read")),
):
    """Mirrors `GET /email/inbox/stats` so the two inboxes read alike."""
    stmt = await _scoped(select(ChannelConversationModel), user, session)
    rows = (await session.execute(stmt)).scalars().all()
    return {
        "total": len(rows),
        "open": sum(1 for r in rows if r.status == "open"),
        "assigned": sum(1 for r in rows if r.status == "assigned"),
        "unread": sum(r.unread_count or 0 for r in rows),
        "unlinked": sum(1 for r in rows if r.case_id is None),
    }


async def _load_conversation(
    session: AsyncSession, conversation_id: uuid.UUID, user: AuthenticatedUser,
) -> tuple[ChannelConversationModel, ChannelEndpointModel]:
    conversation = await session.get(ChannelConversationModel, conversation_id)
    if conversation is None:
        raise coded(404, CHANNEL_NOTFOUND_404, "Conversation not found.")
    # 404 rather than 403 across the tenant boundary: a 403 confirms the id.
    if "superadmin" not in (user.roles or []):
        if user.tenant_id:
            outside = conversation.tenant_id != user.tenant_id
        else:
            # No tenant claim = the default tenant, never every tenant (2026-09-17).
            from case_service.tenancy.identity import default_tenant_tokens
            outside = (conversation.tenant_id is not None and conversation.tenant_id
                       not in await default_tenant_tokens(session))
        if outside:
            raise coded(404, CHANNEL_NOTFOUND_404, "Conversation not found.")
    endpoint = await session.get(ChannelEndpointModel, conversation.endpoint_id)
    if endpoint is None:
        raise coded(404, CHANNEL_NOTFOUND_404, "Conversation not found.")
    return conversation, endpoint


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: uuid.UUID,
    mark_read: bool = Query(True),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("channels.conversations.read")),
):
    conversation, endpoint = await _load_conversation(session, conversation_id, user)
    messages = (await session.execute(
        select(ChannelMessageModel)
        .where(ChannelMessageModel.conversation_id == conversation.id)
        .order_by(ChannelMessageModel.created_at)
    )).scalars().all()

    if mark_read and conversation.unread_count:
        conversation.unread_count = 0
        for m in messages:
            m.is_read = True
        await session.commit()

    return {
        "conversation": _conversation_dict(conversation, endpoint.address),
        "messages": [_message_dict(m) for m in messages],
    }


class ReplyRequest(BaseModel):
    body: str = Field("", max_length=4096)
    template_name: Optional[str] = None
    template_params: list[str] = []


@router.post("/conversations/{conversation_id}/reply")
async def reply(
    conversation_id: uuid.UUID,
    payload: ReplyRequest,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("channels.conversations.write")),
):
    """Send an outbound message on this conversation.

    The two refusals below are checked **before** anything is dispatched, and
    each is a distinct code because the fix differs: an opt-out is a legal
    instruction the agent may not override, and a closed WhatsApp window is a
    clock they can work around with a template.
    """
    conversation, endpoint = await _load_conversation(session, conversation_id, user)

    if not payload.body.strip() and not payload.template_name:
        raise HTTPException(400, "A reply needs a body or a template.")

    try:
        await service.check_can_send(
            session, conversation=conversation, endpoint=endpoint,
            template_name=payload.template_name,
        )
    except service.ChannelSendError as exc:
        code = CHANNEL_OPTED_OUT_403 if exc.reason == "opted_out" else CHANNEL_WINDOW_409
        raise coded(exc.status, code, str(exc)) from exc

    if payload.template_name:
        template = (await session.execute(
            select(WhatsAppTemplateModel).where(
                WhatsAppTemplateModel.endpoint_id == endpoint.id,
                WhatsAppTemplateModel.name == payload.template_name,
            )
        )).scalars().first()
        if template is None:
            raise HTTPException(404, "No such template on this endpoint.")
        if template.approval_status != "approved":
            # Meta rejects an unapproved template at send time; refusing here
            # means the agent finds out now rather than from a silent non-delivery.
            raise HTTPException(
                409, f"Template '{template.name}' is {template.approval_status}, "
                     "so the provider will not send it yet.")

    sent_id, status, error = await _dispatch(session, endpoint, conversation, payload)

    message = await service.record_outbound(
        session,
        conversation=conversation, endpoint=endpoint,
        body=payload.body, author=f"user:{user.user_id}",
        template_name=payload.template_name,
        provider_message_id=sent_id, status=status, error=error,
    )
    await session.commit()
    return _message_dict(message)


async def _dispatch(
    session: AsyncSession,
    endpoint: ChannelEndpointModel,
    conversation: ChannelConversationModel,
    payload: ReplyRequest,
) -> tuple[str | None, str, str | None]:
    """Hand the message to the endpoint's connector.

    A dispatch failure is recorded on the message rather than raised: the agent
    wrote it, it belongs on the thread, and a lost outbound with a 500 is how a
    customer stops getting answers without anyone noticing.
    """
    if endpoint.connector_id is None:
        return None, "queued", "No connector configured on this endpoint."

    try:
        from velaris_shared.db.models import ConnectorRegistryModel
        from case_service.hxbridge.executor import execute_connector

        connector = await session.get(ConnectorRegistryModel, endpoint.connector_id)
        if connector is None:
            return None, "failed", "The endpoint's connector no longer exists."

        result = await execute_connector(
            session, connector,
            {
                "to_number": conversation.peer_address,
                "from_number": endpoint.address,
                "body": payload.body,
                "template_name": payload.template_name,
                "template_params": payload.template_params,
            },
        )
    except Exception as exc:                # a provider fault is not a 500 here
        log.exception("channel dispatch failed on endpoint %s", endpoint.id)
        return None, "failed", str(exc)[:500]

    data = result if isinstance(result, dict) else {}
    return data.get("message_sid"), data.get("status") or "sent", None


class AssignRequest(BaseModel):
    case_id: Optional[uuid.UUID] = None
    assigned_to: Optional[str] = None
    status: Optional[str] = None


@router.patch("/conversations/{conversation_id}")
async def update_conversation(
    conversation_id: uuid.UUID,
    payload: AssignRequest,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("channels.conversations.write")),
):
    """Attach a parked conversation to a case, assign it, or close it.

    Attaching backfills `case_id` onto the messages already on the thread, so
    the case record shows the whole conversation rather than only what arrived
    after somebody linked it.
    """
    conversation, _endpoint = await _load_conversation(session, conversation_id, user)

    if payload.case_id is not None:
        from velaris_shared.db.models import CaseInstanceModel

        case = await session.get(CaseInstanceModel, payload.case_id)
        if case is None:
            raise coded(404, CHANNEL_NOTFOUND_404, "Case not found.")
        # A conversation cannot be attached across the tenant boundary — that
        # would put one tenant's customer messages on another's case record.
        # `same_tenant`, not a raw compare: `case_instances.tenant_id` is a GUID
        # while `channel_conversations.tenant_id` is a free-text token, and
        # comparing those directly is a mistake this platform has already made.
        from case_service.tenancy.identity import same_tenant

        if not await same_tenant(session, case.tenant_id, conversation.tenant_id):
            raise coded(404, CHANNEL_NOTFOUND_404, "Case not found.")
        conversation.case_id = payload.case_id
        for message in (await session.execute(
            select(ChannelMessageModel).where(
                ChannelMessageModel.conversation_id == conversation.id)
        )).scalars().all():
            message.case_id = payload.case_id

    if payload.assigned_to is not None:
        conversation.assigned_to = payload.assigned_to or None
        if payload.assigned_to and conversation.status == "open":
            conversation.status = "assigned"
    if payload.status is not None:
        if payload.status not in ("open", "assigned", "closed"):
            raise HTTPException(400, "status must be open, assigned or closed")
        conversation.status = payload.status

    await session.commit()
    return _conversation_dict(conversation)


@router.get("/cases/{case_id}/conversations")
async def conversations_for_case(
    case_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("channels.conversations.read")),
):
    """The case's Channels tab."""
    stmt = await _scoped(
        select(ChannelConversationModel).where(
            ChannelConversationModel.case_id == case_id), user, session)
    rows = (await session.execute(stmt)).scalars().all()
    return {"conversations": [_conversation_dict(c) for c in rows]}


# ── CTI screen-pop ────────────────────────────────────────────────────────

class CtiPopRequest(BaseModel):
    ani: str = Field(..., max_length=128, description="Calling number")
    dnis: Optional[str] = Field(None, max_length=128, description="Number dialled")
    agent_id: str = Field(..., max_length=255)
    external_call_id: Optional[str] = None
    direction: str = "inbound"


@router.post("/cti/pop")
async def cti_pop(
    payload: CtiPopRequest,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("channels.cti.pop")),
):
    """Resolve a caller and push a screen-pop to one agent.

    Mode (b) of the design, and deliberately the thin one: Genesys, Amazon
    Connect, Avaya and Five9 all already have a screen-pop hook, so this works
    with every contact centre on the market and keeps Velaris out of the media
    path entirely.

    The event goes to the **named agent only**. A pop broadcast to every session
    would show one customer's identity to the whole floor.
    """
    ani = payload.ani.strip()
    digits = service._normalise_phone(ani)

    conversation = None
    if digits:
        candidates = (await session.execute(
            select(ChannelConversationModel)
            .order_by(desc(ChannelConversationModel.last_message_at))
            .limit(200)
        )).scalars().all()
        for row in candidates:
            if service._normalise_phone(row.peer_address) != digits:
                continue
            if (user.tenant_id and "superadmin" not in (user.roles or [])
                    and row.tenant_id != user.tenant_id):
                continue
            conversation = row
            break

    case_id = str(conversation.case_id) if conversation and conversation.case_id else None
    event = {
        "type": "screen_pop",
        "ani": ani,
        "dnis": payload.dnis,
        "direction": payload.direction,
        "external_call_id": payload.external_call_id,
        "case_id": case_id,
        "conversation_id": str(conversation.id) if conversation else None,
        "url": f"/cases/{case_id}" if case_id else "/channels",
    }

    try:
        from case_service.realtime.publisher import publish_assignment_event

        # `assignments.{user_id}` is the agent's own channel — a broadcast would
        # show one customer's identity to the whole floor.
        await publish_assignment_event(payload.agent_id, "screen_pop", event)
    except Exception:
        # A realtime fault must not fail the call: the contact centre is waiting
        # on this response to route a live customer.
        log.exception("screen-pop delivery failed for agent %s", payload.agent_id)

    return event


# ── administration ────────────────────────────────────────────────────────

class EndpointIn(BaseModel):
    tenant_id: str
    channel: str
    provider: str
    address: str = Field(..., max_length=64)
    signing_secret: Optional[str] = None
    connector_id: Optional[uuid.UUID] = None
    default_case_type_id: Optional[uuid.UUID] = None
    auto_create_case: bool = True
    conversation_ttl_hours: int = Field(72, ge=1, le=8760)
    routing: dict = {}
    is_active: bool = True


def _endpoint_dict(e: ChannelEndpointModel) -> dict:
    return {
        "id": str(e.id), "tenant_id": e.tenant_id, "channel": e.channel,
        "provider": e.provider, "address": e.address,
        "connector_id": str(e.connector_id) if e.connector_id else None,
        "default_case_type_id": str(e.default_case_type_id) if e.default_case_type_id else None,
        "auto_create_case": e.auto_create_case,
        "conversation_ttl_hours": e.conversation_ttl_hours,
        "routing": e.routing or {},
        "is_active": e.is_active,
        # Never the secret itself, not even to an admin — `has_secret` is all
        # the screen needs, and it lets an edit leave the field blank to mean
        # "keep the existing one".
        "has_secret": bool(e.signing_secret_enc),
        "inbound_url": f"/api/v1/channels/{e.provider}/inbound/{e.id}",
        "created_at": e.created_at.isoformat() if e.created_at else None,
    }


@router.get("/endpoints")
async def list_endpoints(
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("channels.endpoints.read")),
):
    # Every tenant's rows until 2026-09-29 (8d).
    from case_service.tenancy.identity import scope_to_caller
    rows = (await session.execute(await scope_to_caller(
        select(ChannelEndpointModel).order_by(ChannelEndpointModel.address),
        ChannelEndpointModel, session, _user))).scalars().all()
    return {"endpoints": [_endpoint_dict(e) for e in rows]}


@router.post("/endpoints", status_code=201)
async def create_endpoint(
    body: EndpointIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("channels.endpoints.write")),
):
    from case_service.hxbridge.encryption import encrypt_credentials_vaulted

    if body.channel not in ("sms", "whatsapp", "voice"):
        raise HTTPException(400, "channel must be sms, whatsapp or voice")
    if body.provider not in signatures.SUPPORTED:
        raise HTTPException(
            400, f"provider must be one of: {', '.join(signatures.SUPPORTED)}. "
                 "A provider whose signature scheme we do not implement would "
                 "leave the inbound route unauthenticated.")
    if not body.signing_secret:
        # Without a secret the signature check can only fail, so the endpoint
        # would accept nothing — better to refuse than to ship a dead webhook.
        raise HTTPException(
            400, "A signing secret is required: it is the only authentication "
                 "the inbound webhook has.")

    # An endpoint's tenant owns every conversation and case its inbound
    # messages create. Stored as sent until 2026-09-30.
    try:
        body.tenant_id = await tenant_for_write(session, user, body.tenant_id)
    except PermissionError as exc:
        raise HTTPException(403, str(exc))
    row = ChannelEndpointModel(
        tenant_id=body.tenant_id, channel=body.channel, provider=body.provider,
        address=body.address,
        signing_secret_enc=await encrypt_credentials_vaulted(
            session, {"secret": body.signing_secret}, tenant_id=body.tenant_id),
        connector_id=body.connector_id,
        default_case_type_id=body.default_case_type_id,
        auto_create_case=body.auto_create_case,
        conversation_ttl_hours=body.conversation_ttl_hours,
        routing=body.routing or {}, is_active=body.is_active,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)

    from velaris_shared.db.security_events import log_security_event
    await log_security_event(
        session, event_type="channel_endpoint_created", severity="medium",
        user_id=user.user_id, resource_type="channel_endpoint", resource_id=str(row.id),
        action="create", outcome="success",
        details={"channel": row.channel, "provider": row.provider,
                 "auto_create_case": row.auto_create_case},
    )
    await session.commit()
    return _endpoint_dict(row)


@router.delete("/endpoints/{endpoint_id}", status_code=204)
async def delete_endpoint(
    endpoint_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("channels.endpoints.write")),
):
    row = await session.get(ChannelEndpointModel, endpoint_id)
    if row is not None:
        await session.delete(row)
        await session.commit()
    return Response(status_code=204)


class TemplateIn(BaseModel):
    endpoint_id: uuid.UUID
    name: str = Field(..., max_length=255)
    language: str = Field("en_US", max_length=10)
    provider_template_id: Optional[str] = None
    body_params: list[str] = []
    approval_status: str = "pending"


@router.get("/templates")
async def list_templates(
    endpoint_id: Optional[uuid.UUID] = Query(None),
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("channels.endpoints.read")),
):
    stmt = select(WhatsAppTemplateModel)
    if endpoint_id:
        stmt = stmt.where(WhatsAppTemplateModel.endpoint_id == endpoint_id)
    # Every tenant's rows until 2026-09-29 (8d).
    from case_service.tenancy.identity import scope_to_caller
    stmt = await scope_to_caller(stmt, WhatsAppTemplateModel, session, _user)
    rows = (await session.execute(stmt.order_by(WhatsAppTemplateModel.name))).scalars().all()
    return {"templates": [{
        "id": str(t.id), "endpoint_id": str(t.endpoint_id), "name": t.name,
        "language": t.language, "provider_template_id": t.provider_template_id,
        "body_params": t.body_params or [], "approval_status": t.approval_status,
    } for t in rows]}


@router.post("/templates", status_code=201)
async def create_template(
    body: TemplateIn,
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("channels.endpoints.write")),
):
    endpoint = await session.get(ChannelEndpointModel, body.endpoint_id)
    if endpoint is None:
        raise coded(404, CHANNEL_NOTFOUND_404, "Endpoint not found.")
    if body.approval_status not in ("pending", "approved", "rejected"):
        raise HTTPException(400, "approval_status must be pending, approved or rejected")

    row = WhatsAppTemplateModel(
        tenant_id=endpoint.tenant_id, endpoint_id=endpoint.id, name=body.name,
        language=body.language, provider_template_id=body.provider_template_id,
        body_params=body.body_params, approval_status=body.approval_status,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return {"id": str(row.id), "name": row.name, "language": row.language,
            "approval_status": row.approval_status}


# ── opt-out visibility ────────────────────────────────────────────────────

@router.get("/opt-out/{address}")
async def opt_out_status(
    address: str,
    tenant_id: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("channels.conversations.read")),
):
    """Whether we may message this number, and why.

    Read from the consent ledger rather than a private list, so what an agent
    sees here is the same record a subject-access request would return.
    """
    # The caller's own ledger. Until 2026-09-29 (8d) the caller named any
    # tenant's — or none, which read every tenant's at once.
    from case_service.tenancy.identity import is_superadmin

    if not is_superadmin(_user):
        tenant_id = getattr(_user, "tenant_id", None) or "default"
    allowed = await optout.may_send(session, address, tenant_id=tenant_id)
    return {"address": address, "may_send": allowed,
            "purpose": optout.PURPOSE,
            "note": "Silence means permission: only an explicit STOP suppresses."}
