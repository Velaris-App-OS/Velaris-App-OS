"""P53 Developer & Custom Connectors router."""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser
from case_service.db.session import get_session
from case_service.devconn import service
from case_service.db.models import WebhookReceiverRuleModel, OutboundConnectorRuleModel

router = APIRouter(prefix="/devconn", tags=["devconn"])


def _tenant(user: AuthenticatedUser) -> str:
    return getattr(user, "tenant_id", None) or "default"


# ── Webhook Receiver ──────────────────────────────────────────────────────────

@router.post("/webhooks/receive/{connector_id}", status_code=202)
async def receive_webhook(
    connector_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Receive any inbound webhook and route it to a Helix case."""
    try:
        payload = await request.json()
    except Exception:
        payload = {"raw": (await request.body()).decode(errors="replace")}

    # Derive tenant from connector
    from sqlalchemy import select
    from case_service.db.models import ConnectorRegistryModel
    conn = (await session.execute(
        select(ConnectorRegistryModel).where(ConnectorRegistryModel.id == connector_id)
    )).scalar_one_or_none()
    tenant_id = conn.tenant_id if conn else "default"

    event = await service.receive_webhook(session, connector_id, payload, tenant_id)
    await session.commit()
    return {"event_id": str(event.id), "status": event.status}


# ── Webhook Rules ─────────────────────────────────────────────────────────────

class RuleIn(BaseModel):
    connector_id:        uuid.UUID | None = None
    name:                str
    case_id_field:       str | None = None
    match_case_field:    str | None = None
    match_payload_field: str | None = None
    field_updates:       dict[str, str] = {}
    advance_stage:       bool = False


class RuleOut(BaseModel):
    id:                  uuid.UUID
    name:                str
    connector_id:        uuid.UUID | None
    case_id_field:       str | None
    match_case_field:    str | None
    match_payload_field: str | None
    field_updates:       dict
    advance_stage:       bool
    enabled:             bool

    model_config = {"from_attributes": True}


@router.get("/connectors/inbound", response_model=list[RuleOut])
async def list_rules(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    return await service.list_rules(session, _tenant(user))


@router.post("/connectors/inbound", response_model=RuleOut, status_code=201)
async def create_rule(
    body: RuleIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    rule = WebhookReceiverRuleModel(
        tenant_id=_tenant(user),
        connector_id=body.connector_id,
        name=body.name,
        case_id_field=body.case_id_field,
        match_case_field=body.match_case_field,
        match_payload_field=body.match_payload_field,
        field_updates=body.field_updates,
        advance_stage=body.advance_stage,
    )
    await service.create_rule(session, rule)
    await session.commit()
    return rule


@router.delete("/connectors/inbound/{rule_id}", status_code=204)
async def delete_rule(
    rule_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    from sqlalchemy import select, delete
    await session.execute(
        delete(WebhookReceiverRuleModel).where(WebhookReceiverRuleModel.id == rule_id)
    )
    await session.commit()


# ── Webhook Events ────────────────────────────────────────────────────────────

class EventOut(BaseModel):
    id:              uuid.UUID
    connector_id:    uuid.UUID | None
    rule_id:         uuid.UUID | None
    status:          str
    matched_case_id: uuid.UUID | None
    error:           str | None
    received_at:     str

    model_config = {"from_attributes": True}

    @classmethod
    def from_model(cls, e: Any) -> "EventOut":
        return cls(
            id=e.id, connector_id=e.connector_id, rule_id=e.rule_id,
            status=e.status, matched_case_id=e.matched_case_id, error=e.error,
            received_at=e.received_at.isoformat(),
        )


@router.get("/events", response_model=list[EventOut])
async def list_events(
    status: str | None = None,
    connector_id: uuid.UUID | None = None,
    limit: int = 100,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    rows = await service.list_events(session, status=status, connector_id=connector_id, limit=limit)
    return [EventOut.from_model(r) for r in rows]


# ── Custom HTTP Connector Builder ─────────────────────────────────────────────

class BuildConnectorIn(BaseModel):
    name:             str
    method:           str = "POST"
    url:              str
    headers:          dict[str, str] = {}
    auth_type:        str = "none"
    body_template:    str = ""
    response_mapping: dict[str, str] = {}
    credentials:      dict[str, str] = {}


class ConnectorOut(BaseModel):
    id:   uuid.UUID
    name: str
    connector_type: str
    enabled: bool

    model_config = {"from_attributes": True}


@router.post("/connectors/build", response_model=ConnectorOut, status_code=201)
async def build_connector(
    body: BuildConnectorIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    row = await service.build_http_connector(
        session, _tenant(user),
        body.name, body.method, body.url,
        body.headers, body.auth_type,
        body.body_template, body.response_mapping,
        body.credentials,
    )
    await session.commit()
    return row


@router.get("/connectors", response_model=list[ConnectorOut])
async def list_custom_connectors(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    from sqlalchemy import select
    from case_service.db.models import ConnectorRegistryModel
    rows = (await session.execute(
        select(ConnectorRegistryModel).where(
            ConnectorRegistryModel.connector_type == "http_custom",
            ConnectorRegistryModel.tenant_id == _tenant(user),
        )
    )).scalars().all()
    return rows


# ── Outbound Connector Rules ──────────────────────────────────────────────────

class OutboundRuleIn(BaseModel):
    name:           str
    trigger_event:  str  # stage_enter | stage_exit | step_complete | field_change | case_created
    case_type_id:   uuid.UUID | None = None
    condition_expr: dict | None = None
    connector_id:   uuid.UUID | None = None
    input_mapping:  dict = {}
    enabled:        bool = True


class OutboundRuleOut(BaseModel):
    id:             uuid.UUID
    name:           str
    trigger_event:  str
    case_type_id:   uuid.UUID | None
    condition_expr: dict | None
    connector_id:   uuid.UUID | None
    input_mapping:  dict
    enabled:        bool

    model_config = {"from_attributes": True}


_VALID_TRIGGER_EVENTS = {
    "stage_enter", "stage_exit", "step_complete", "field_change", "case_created"
}


@router.get("/connectors/outbound", response_model=list[OutboundRuleOut])
async def list_outbound_rules(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    from sqlalchemy import select
    rows = (await session.execute(
        select(OutboundConnectorRuleModel).where(
            OutboundConnectorRuleModel.tenant_id == _tenant(user)
        ).order_by(OutboundConnectorRuleModel.name)
    )).scalars().all()
    return rows


@router.post("/connectors/outbound", response_model=OutboundRuleOut, status_code=201)
async def create_outbound_rule(
    body: OutboundRuleIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    from fastapi import HTTPException as _HTTPException
    if body.trigger_event not in _VALID_TRIGGER_EVENTS:
        raise _HTTPException(400, f"Invalid trigger_event '{body.trigger_event}'. Must be one of: {sorted(_VALID_TRIGGER_EVENTS)}")
    rule = OutboundConnectorRuleModel(
        tenant_id=_tenant(user),
        name=body.name,
        trigger_event=body.trigger_event,
        case_type_id=body.case_type_id,
        condition_expr=body.condition_expr,
        connector_id=body.connector_id,
        input_mapping=body.input_mapping,
        enabled=body.enabled,
    )
    session.add(rule)
    await session.commit()
    await session.refresh(rule)
    return rule


@router.put("/connectors/outbound/{rule_id}", response_model=OutboundRuleOut)
async def update_outbound_rule(
    rule_id: uuid.UUID,
    body: OutboundRuleIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    from sqlalchemy import select
    from fastapi import HTTPException as _HTTPException
    rule = (await session.execute(
        select(OutboundConnectorRuleModel).where(
            OutboundConnectorRuleModel.id == rule_id,
            OutboundConnectorRuleModel.tenant_id == _tenant(user),
        )
    )).scalar_one_or_none()
    if not rule:
        raise _HTTPException(404, "Rule not found")
    rule.name = body.name
    rule.trigger_event = body.trigger_event
    rule.case_type_id = body.case_type_id
    rule.condition_expr = body.condition_expr
    rule.connector_id = body.connector_id
    rule.input_mapping = body.input_mapping
    rule.enabled = body.enabled
    await session.commit()
    await session.refresh(rule)
    return rule


@router.delete("/connectors/outbound/{rule_id}", status_code=204)
async def delete_outbound_rule(
    rule_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    from sqlalchemy import delete
    await session.execute(
        delete(OutboundConnectorRuleModel).where(
            OutboundConnectorRuleModel.id == rule_id,
            OutboundConnectorRuleModel.tenant_id == _tenant(user),
        )
    )
    await session.commit()


# ── OpenAPI Auto-Connector ────────────────────────────────────────────────────

class OpenAPIIn(BaseModel):
    spec:           str
    connector_name: str = "Generated Connector"


@router.post("/connectors/from-openapi")
async def from_openapi(
    body: OpenAPIIn,
    user: AuthenticatedUser = Depends(get_current_user),
):
    result = await service.generate_from_openapi(body.spec, body.connector_name)
    return result
