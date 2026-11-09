"""HxBridge API — P28.

Endpoints:
  GET  /hxbridge/connector-types              list all registered connector types
  POST /hxbridge/connectors                   register a connector
  GET  /hxbridge/connectors                   list connectors
  GET  /hxbridge/connectors/{id}              get connector detail
  PUT  /hxbridge/connectors/{id}              update config / credentials
  DELETE /hxbridge/connectors/{id}            remove connector
  POST /hxbridge/connectors/{id}/test         test credentials & connectivity
  POST /hxbridge/connectors/{id}/execute      sandbox: execute with sample input
  POST /hxbridge/connectors/{id}/form-lookup  form field lookup (with audit trail)
  GET  /hxbridge/events                       unified inbound + outbound events (paginated)
  GET  /hxbridge/calls                        list integration call history (legacy alias)
  GET  /hxbridge/dlq                          list dead letter queue items
  GET  /hxbridge/overflow/{call_id}           a call's full response, when it was too large to store inline
  POST /hxbridge/dlq/{id}/retry               manually retry a DLQ item
  POST /webhooks/{connector_id}/receive       inbound webhook receiver
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared import hxguard
from velaris_shared.tenancy.identity import tenant_for_write
from velaris_shared.auth.dependencies import get_current_user
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.db.models import (
    ConnectorRegistryModel, IntegrationCallModel, DeadLetterQueueModel,
    WebhookReceiverEventModel,
)
from velaris_shared.db.session import get_session
from case_service.hxbridge.encryption import encrypt_credentials, decrypt_credentials, mask_credentials
from case_service.hxbridge.executor import execute_connector, form_lookup_connector, retry_dlq_item
from case_service import hxvault
from case_service.hxbridge.protocol import list_connector_types, get_connector
import case_service.hxbridge.connectors  # noqa: F401 — triggers self-registration
from velaris_shared.core.error_codes import (
    CONN_DISABLED_400,
    CONN_NOTFOUND_404,
    CONN_ROLE_403,
    CONN_FORM_LOOKUP_403,
    CONN_UNKNOWN_TYPE_400,
    coded,
)

router = APIRouter(prefix="/hxbridge", tags=["hxbridge"])
webhook_router = APIRouter(prefix="/webhooks", tags=["hxbridge-webhooks"])


# ── Schemas ───────────────────────────────────────────────────────────────────

class ConnectorCreate(BaseModel):
    name:           str
    connector_type: str
    description:    Optional[str] = None
    config:         dict = {}
    credentials:    dict = {}
    # Phase 2 (case variables): operator-chosen namespace this connector
    # writes case variables into. Optional — connectors without one cannot
    # write variables (case_vars rejects unregistered writers).
    variable_namespace: Optional[str] = None

class ConnectorUpdate(BaseModel):
    name:        Optional[str]  = None
    description: Optional[str] = None
    config:      Optional[dict] = None
    credentials: Optional[dict] = None
    enabled:     Optional[bool] = None
    variable_namespace: Optional[str] = None

class SandboxRequest(BaseModel):
    input_data: dict = {}
    operation:  Optional[str] = None     # one of the connector's named operations


class FormLookupRequest(BaseModel):
    input_data:    dict          = {}
    form_id:       Optional[str] = None
    field_key:     Optional[str] = None
    case_id:       Optional[uuid.UUID] = None
    # Honoured only for `integration.connectors.execute` (the builder's Test);
    # everyone else runs the operation the saved form field names.
    operation:     Optional[str] = None


# ── Connector types ───────────────────────────────────────────────────────────

@router.get("/connector-types")
async def get_connector_types(_: AuthenticatedUser = Depends(get_current_user)):
    """List all registered connector types with their schemas."""
    return {"connector_types": list_connector_types()}


async def _require_namespace_role(session: AsyncSession, user: AuthenticatedUser) -> None:
    """Namespace registration is an admin-grade act (mirrors the role gate on
    POST /variables/namespaces) — plain connector CRUD must not be a path to
    squatting a namespace name another integration expects to own.
    Decided by HxGuard (action: connector.namespace.register, fail-closed)."""
    from velaris_shared import hxguard
    await hxguard.require(
        session, hxguard.subject_from_user(user), "connector.namespace.register",
    )


# ── Connector CRUD ────────────────────────────────────────────────────────────

@router.post("/connectors", status_code=201)
async def create_connector(
    body: ConnectorCreate,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("integration.connectors.write")),
):
    if body.connector_type not in {ct["connector_type"] for ct in list_connector_types()}:
        raise coded(400, CONN_UNKNOWN_TYPE_400, f"Unknown connector_type: '{body.connector_type}'")

    # Application-level duplicate check (handles SQLite NULL inequality edge case)
    existing = (await session.execute(
        select(ConnectorRegistryModel).where(ConnectorRegistryModel.name == body.name)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(409, f"Connector '{body.name}' already exists")

    await hxvault.ensure_dek(session, user.tenant_id)
    # No connector recorded an owner until 2026-09-30, and a NULL one is shared
    # with every tenant (list_connectors). The platform operator's stays that
    # way, as before; a tenant's is its own.
    owner = await tenant_for_write(session, user, None)
    from velaris_shared.hxbridge.security import check_connector_config
    try:
        await check_connector_config(session, body.connector_type, owner, body.config)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    connector = ConnectorRegistryModel(
        tenant_id=owner,
        name=body.name,
        connector_type=body.connector_type,
        description=body.description,
        config=body.config,
        credentials=encrypt_credentials(body.credentials, tenant_id=user.tenant_id, vault=True),
        created_by=user.user_id,
    )
    session.add(connector)
    try:
        await session.flush()
        if body.variable_namespace:
            await _require_namespace_role(session, user)
            from case_service.case_vars import service as case_vars
            try:
                await case_vars.register_connector_namespace(
                    session, name=body.variable_namespace,
                    owner_type="connector", owner_ref=connector.id,
                    created_by=user.user_id,
                )
            except case_vars.VariableError as ve:
                await session.rollback()
                raise HTTPException(400, str(ve))
        await session.commit()
        await session.refresh(connector)
    except HTTPException:
        raise
    except Exception as e:
        if "uq_connector" in str(e).lower() or "unique" in str(e).lower():
            raise HTTPException(409, f"Connector '{body.name}' already exists")
        raise HTTPException(500, str(e))
    return _connector_out(connector)


@router.get("/connectors")
async def list_connectors(
    connector_type: Optional[str] = Query(None),
    enabled:        Optional[bool] = Query(None),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("integration.connectors.read")),
):
    from sqlalchemy import or_
    stmt = select(ConnectorRegistryModel).order_by(ConnectorRegistryModel.name)
    if connector_type:
        stmt = stmt.where(ConnectorRegistryModel.connector_type == connector_type)
    if enabled is not None:
        stmt = stmt.where(ConnectorRegistryModel.enabled == enabled)

    # Scope to user's tenant + global connectors (tenant_id IS NULL).
    # Superadmin sees everything. A caller with NO tenant claim is the default
    # tenant — until 2026-09-17 it too saw every tenant's connectors.
    # A connector with no tenant is the operator's, carrying the operator's
    # credentials: the default tenant's alone since 2026-11-04 (it was shared
    # with every tenant, a website demo tenant included). The path guard
    # applies the same rule to every by-id route.
    if "superadmin" not in (user.roles or []):
        from case_service.tenancy.identity import default_tenant_tokens, same_tenant_as_default
        if user.tenant_id:
            mine = [str(user.tenant_id)]
        else:
            mine = await default_tenant_tokens(session)
        if await same_tenant_as_default(session, user.tenant_id or "default"):
            stmt = stmt.where(or_(ConnectorRegistryModel.tenant_id.in_(mine),
                                  ConnectorRegistryModel.tenant_id.is_(None)))
        else:
            stmt = stmt.where(ConnectorRegistryModel.tenant_id.in_(mine))

    rows = (await session.execute(stmt)).scalars().all()
    return {"connectors": [_connector_out(r) for r in rows], "total": len(rows)}


@router.get("/connectors/{connector_id}")
async def get_connector_detail(
    connector_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(hxguard.guard("integration.connectors.read")),
):
    connector = await _get_or_404(session, connector_id)
    out = _connector_out(connector, include_config=True)
    from velaris_shared.db.models import VariableNamespaceModel
    ns = (await session.execute(
        select(VariableNamespaceModel)
        .where(VariableNamespaceModel.owner_type.in_(("connector", "devconn")))
        .where(VariableNamespaceModel.owner_ref == connector.id)
        .limit(1)
    )).scalar_one_or_none()
    out["variable_namespace"] = ns.name if ns else None
    out["variable_namespace_status"] = ns.status if ns else None
    return out


@router.put("/connectors/{connector_id}")
async def update_connector(
    connector_id: uuid.UUID,
    body: ConnectorUpdate,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("integration.connectors.write")),
):
    connector = await _get_or_404(session, connector_id)
    if body.name        is not None: connector.name        = body.name
    if body.description is not None: connector.description = body.description
    if body.config      is not None:
        from velaris_shared.hxbridge.security import check_connector_config
        try:
            await check_connector_config(session, connector.connector_type, connector.tenant_id, body.config)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        connector.config = body.config
    if body.credentials is not None:
        await hxvault.ensure_dek(session, user.tenant_id)
        connector.credentials = encrypt_credentials(body.credentials, tenant_id=user.tenant_id, vault=True)
    if body.enabled     is not None: connector.enabled     = body.enabled
    if body.variable_namespace:
        await _require_namespace_role(session, user)
        from case_service.case_vars import service as case_vars
        try:
            await case_vars.register_connector_namespace(
                session, name=body.variable_namespace,
                owner_type="connector", owner_ref=connector.id,
                created_by=user.user_id,
            )
        except case_vars.VariableError as ve:
            await session.rollback()
            raise HTTPException(400, str(ve))
    connector.updated_at = datetime.now(timezone.utc)
    await session.commit()
    # #27 Part B: an integration change can affect AI scenarios across case types →
    # flag generated suites' AI layer stale (manual regen).
    from case_service.testsuite import regen
    background_tasks.add_task(regen.bg_scenario_source_changed, None)
    return _connector_out(connector, include_config=True)


@router.delete("/connectors/{connector_id}", status_code=204)
async def delete_connector(
    connector_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(hxguard.guard("integration.connectors.write")),
):
    connector = await _get_or_404(session, connector_id)
    # Retire (never delete) the connector's variable namespace — written
    # variables and their lineage stay readable; writes are closed.
    from case_service.case_vars import service as case_vars
    await case_vars.retire_connector_namespaces(
        session, owner_type="connector", owner_ref=connector.id,
    )
    await case_vars.retire_connector_namespaces(
        session, owner_type="devconn", owner_ref=connector.id,
    )
    await session.delete(connector)
    await session.commit()
    from velaris_shared import hxguard
    from velaris_shared.hxguard import generation as _hxguard_generation
    hxguard.invalidate_cache()
    # The commit above already closed the transaction, so the bump gets its own.
    await _hxguard_generation.bump(session, reason="devconn.delete")
    await session.commit()


# ── Test & sandbox ────────────────────────────────────────────────────────────

@router.post("/connectors/{connector_id}/test")
async def test_connector(
    connector_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(hxguard.guard("integration.connectors.execute")),
):
    """Test connector credentials and connectivity."""
    connector = await _get_or_404(session, connector_id)
    creds = decrypt_credentials(connector.credentials or {})
    try:
        from velaris_shared.hxbridge.security import check_connector_targets
        await check_connector_targets(session, connector.tenant_id, connector.config)
        from velaris_shared.hxbridge.security import platform_owned, runtime_config
        from velaris_shared.hxbridge.operations import apply_operation
        impl = get_connector(connector.connector_type, runtime_config(
            apply_operation(connector.connector_type, connector.config, None),
            await platform_owned(session, connector.tenant_id)), creds)
        ok = await impl.test()
    except Exception as exc:
        ok = False
        connector.last_test_ok = False
        connector.last_tested_at = datetime.now(timezone.utc)
        await session.commit()
        return {"ok": False, "error": str(exc)}

    connector.last_test_ok   = ok
    connector.last_tested_at = datetime.now(timezone.utc)
    await session.commit()
    return {"ok": ok}


@router.post("/connectors/{connector_id}/execute")
async def sandbox_execute(
    connector_id: uuid.UUID,
    body: SandboxRequest,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(hxguard.guard("integration.connectors.execute")),
):
    """Sandbox: execute a connector with sample input data."""
    connector = await _get_or_404(session, connector_id)
    if not connector.enabled:
        raise coded(400, CONN_DISABLED_400, "Connector is disabled")
    try:
        result = await execute_connector(session, connector, body.input_data,
                                         operation=body.operation)
        return {"ok": True, "result": result}
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}


@router.post("/connectors/{connector_id}/form-lookup")
async def form_lookup(
    connector_id: uuid.UUID,
    body: FormLookupRequest,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Execute a connector as a form field lookup; logs an audit trail row.

    Runs only what a saved form says (2026-10-28): the form's lookup field
    names this connector, and only its mapped inputs are sent. Until then any
    signed-in user ran any of the tenant's connectors with any input and read
    the raw response. Free input stays for `integration.connectors.execute`
    (the builder's Test, which runs before the form is saved).
    """
    connector = await _get_or_404(session, connector_id)
    if not connector.enabled:
        raise coded(400, CONN_DISABLED_400, "Connector is disabled")
    input_data, operation = body.input_data, body.operation
    if not hxguard.user_allows(user, "integration.connectors.execute"):
        input_data, operation = await _saved_form_lookup_input(session, user, connector_id, body)
    tenant_id = getattr(user, "tenant_id", None) or "default"
    try:
        result = await form_lookup_connector(
            session, connector, input_data,
            tenant_id=tenant_id,
            form_id=body.form_id,
            field_key=body.field_key,
            user_id=user.user_id,
            case_id=body.case_id,
            operation=operation,
        )
        await session.commit()
        return {"ok": True, "result": result}
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}


async def _saved_form_lookup_input(
    session: AsyncSession, user: AuthenticatedUser,
    connector_id: uuid.UUID, body: FormLookupRequest,
) -> tuple[dict, str | None]:
    """The inputs a saved form's lookup field allows — and the operation it
    names — or a 403.

    The form must be the caller's tenant's (the path guard does not see a body
    `form_id`), its field `field_key` a lookup on this connector — or a choice
    field whose options come from it. Only the parameters in the field's input
    mapping are passed on.
    """
    from velaris_shared.db.models import FormDefinitionModel
    from velaris_shared.forms import field_key_of
    from velaris_shared.tenancy.identity import may_reach_tenant

    def refuse():
        raise coded(403, CONN_FORM_LOOKUP_403, "This lookup is not part of a saved form")

    if not body.form_id or not body.field_key:
        refuse()
    try:
        form_uuid = uuid.UUID(str(body.form_id))
    except ValueError:
        refuse()
    form = await session.get(FormDefinitionModel, form_uuid)
    if form is None or not await may_reach_tenant(session, user, form.tenant_id):
        refuse()
    for section in (form.definition_json or {}).get("sections", []) or []:
        for f in (section.get("fields", []) if isinstance(section, dict) else []) or []:
            if not isinstance(f, dict) or field_key_of(f) != body.field_key:
                continue
            # A lookup field's `source`, or a choice field whose options come
            # from a connector (`options_source`, Form Builder part 4).
            if f.get("type") == "connector_lookup" and isinstance(f.get("source"), dict):
                src = f["source"]
            elif f.get("type") in ("dropdown", "radio", "multi_select") \
                    and isinstance(f.get("options_source"), dict) and f["options_source"].get("kind") == "connector":
                src = f["options_source"]
            else:
                refuse()
            if str(src.get("connector_id", "")) != str(connector_id):
                refuse()
            mapping = src.get("input_mapping")
            allowed = mapping.keys() if isinstance(mapping, dict) else ()
            op = src.get("operation") if isinstance(src.get("operation"), str) else None
            return {k: body.input_data.get(k, "") for k in allowed}, op
    refuse()


# ── Rotate credentials ────────────────────────────────────────────────────────

@router.post("/connectors/{connector_id}/rotate-credentials")
async def rotate_connector_credentials(
    connector_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """SD-6: Clear connector credentials so they can be re-entered. Requires admin or integration role."""
    if not hxguard.user_allows(user, "integration.connectors.rotate"):
        raise coded(403, CONN_ROLE_403, "admin or integration role required")
    row = await session.get(ConnectorRegistryModel, connector_id)
    if not row:
        raise coded(404, CONN_NOTFOUND_404, "Connector not found")
    row.credentials           = {}
    row.credential_expires_at = None
    row.credentials_updated_at = datetime.now(timezone.utc)
    row.updated_at            = datetime.now(timezone.utc)
    await session.commit()
    return {"ok": True, "connector_id": str(connector_id), "message": "Credentials cleared — please re-enter via the connector edit form."}


# ── Unified events (inbound + outbound) ──────────────────────────────────────

@router.get("/events")
async def list_events(
    direction:    Optional[str]       = Query(None, description="inbound | outbound"),
    connector_id: Optional[uuid.UUID] = Query(None),
    case_id:      Optional[uuid.UUID] = Query(None),
    status:       Optional[str]       = Query(None),
    page:         int                 = Query(1, ge=1),
    page_size:    int                 = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(hxguard.guard("integration.connectors.read")),
):
    """Unified paginated event list — outbound connector calls + inbound webhooks."""
    events: list[dict] = []

    if direction != "inbound":
        # Outbound: IntegrationCallModel
        stmt = select(IntegrationCallModel).order_by(desc(IntegrationCallModel.created_at))
        if connector_id: stmt = stmt.where(IntegrationCallModel.connector_id == connector_id)
        if case_id:      stmt = stmt.where(IntegrationCallModel.case_id == case_id)
        if status:       stmt = stmt.where(IntegrationCallModel.status == status)
        # Every tenant's rows until 2026-09-29 (8d).
        from case_service.tenancy.identity import scope_to_caller
        stmt = await scope_to_caller(stmt, IntegrationCallModel, session, _,
                                     via=(IntegrationCallModel.connector_id, ConnectorRegistryModel))
        rows = (await session.execute(stmt)).scalars().all()
        for r in rows:
            events.append({**_call_out(r), "direction": "outbound"})

    if direction != "outbound":
        # Inbound: WebhookReceiverEventModel
        from sqlalchemy import select as _select
        stmt2 = _select(WebhookReceiverEventModel).order_by(desc(WebhookReceiverEventModel.received_at))
        if connector_id: stmt2 = stmt2.where(WebhookReceiverEventModel.connector_id == connector_id)
        if case_id:      stmt2 = stmt2.where(WebhookReceiverEventModel.matched_case_id == case_id)
        if status:       stmt2 = stmt2.where(WebhookReceiverEventModel.status == status)
        from case_service.tenancy.identity import scope_to_caller
        stmt2 = await scope_to_caller(stmt2, WebhookReceiverEventModel, session, _)
        rows2 = (await session.execute(stmt2)).scalars().all()
        for r in rows2:
            events.append({
                "id":           str(r.id),
                "connector_id": str(r.connector_id) if r.connector_id else None,
                "case_id":      str(r.matched_case_id) if r.matched_case_id else None,
                "step_id":      None,
                "status":       r.status,
                "latency_ms":   None,
                "error":        r.error,
                "retry_count":  0,
                "created_at":   r.received_at.isoformat() if r.received_at else None,
                "direction":    "inbound",
            })

    events.sort(key=lambda e: e["created_at"] or "", reverse=True)
    total = len(events)
    offset = (page - 1) * page_size
    page_items = events[offset: offset + page_size]
    return {
        "events":     page_items,
        "total":      total,
        "page":       page,
        "page_size":  page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
    }


# ── Integration call history (legacy alias) ───────────────────────────────────

@router.get("/calls")
async def list_calls(
    connector_id: Optional[uuid.UUID] = Query(None),
    case_id:      Optional[uuid.UUID] = Query(None),
    status:       Optional[str]       = Query(None),
    limit:        int                 = Query(50, le=200),
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(hxguard.guard("integration.connectors.read")),
):
    stmt = select(IntegrationCallModel).order_by(desc(IntegrationCallModel.created_at)).limit(limit)
    if connector_id: stmt = stmt.where(IntegrationCallModel.connector_id == connector_id)
    if case_id:      stmt = stmt.where(IntegrationCallModel.case_id == case_id)
    if status:       stmt = stmt.where(IntegrationCallModel.status == status)
    # Every tenant's rows until 2026-09-29 (8d).
    from case_service.tenancy.identity import scope_to_caller
    stmt = await scope_to_caller(stmt, IntegrationCallModel, session, _,
                                 via=(IntegrationCallModel.connector_id, ConnectorRegistryModel))
    rows = (await session.execute(stmt)).scalars().all()
    return {"calls": [_call_out(r) for r in rows], "total": len(rows)}


# ── Large responses (2026-11-08) ──────────────────────────────────────────────
# A response above `connector_overflow_threshold_bytes` is kept on disk and the
# call records `{"_overflow": true, "ref_url": "/api/v1/hxbridge/overflow/<id>"}`.
# That URL had no route. It is read like the call history: connector read
# permission, and only a call of a connector the caller's tenant may see.

@router.get("/overflow/{call_id}")
async def get_overflow(
    call_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(hxguard.guard("integration.connectors.read")),
):
    import json as _json
    import os
    from velaris_shared.config import get_settings
    from case_service.tenancy.identity import scope_to_caller
    stmt = select(IntegrationCallModel).where(IntegrationCallModel.id == call_id)
    stmt = await scope_to_caller(stmt, IntegrationCallModel, session, _,
                                 via=(IntegrationCallModel.connector_id, ConnectorRegistryModel))
    call = (await session.execute(stmt)).scalars().first()
    if call is None or not (isinstance(call.response, dict) and call.response.get("_overflow")):
        raise HTTPException(status_code=404, detail="Not found")
    path = os.path.join(get_settings().storage_local_path, "connector-overflow", f"{call.id}.json")
    try:
        with open(path) as f:
            body = _json.load(f)
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="The stored response is no longer available")
    return {"call_id": str(call.id), "connector_id": str(call.connector_id), "response": body}


# ── Dead letter queue ─────────────────────────────────────────────────────────

@router.get("/dlq")
async def list_dlq(
    connector_id: Optional[uuid.UUID] = Query(None),
    unresolved_only: bool             = Query(True),
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(hxguard.guard("integration.connectors.read")),
):
    stmt = select(DeadLetterQueueModel).order_by(desc(DeadLetterQueueModel.created_at))
    if connector_id:    stmt = stmt.where(DeadLetterQueueModel.connector_id == connector_id)
    if unresolved_only: stmt = stmt.where(DeadLetterQueueModel.resolution.is_(None))
    # Every tenant's rows until 2026-09-29 (8d).
    from case_service.tenancy.identity import scope_to_caller
    stmt = await scope_to_caller(stmt, DeadLetterQueueModel, session, _,
                                 via=(DeadLetterQueueModel.connector_id, ConnectorRegistryModel))
    rows = (await session.execute(stmt)).scalars().all()
    return {"items": [_dlq_out(r) for r in rows], "total": len(rows)}


@router.post("/dlq/{item_id}/retry")
async def retry_dlq(
    item_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: AuthenticatedUser = Depends(hxguard.guard("integration.connectors.write")),
):
    item = await session.get(DeadLetterQueueModel, item_id)
    if not item:
        raise coded(404, CONN_NOTFOUND_404, "DLQ item not found")
    if item.resolution:
        raise HTTPException(409, f"Already resolved: {item.resolution}")
    connector = await session.get(ConnectorRegistryModel, item.connector_id)
    if not connector:
        raise coded(404, CONN_NOTFOUND_404, "Connector no longer exists")
    ok = await retry_dlq_item(session, item, connector)
    return {"ok": ok, "retry_count": item.retry_count, "resolution": item.resolution}


# ── Inbound webhook receiver ──────────────────────────────────────────────────

@webhook_router.post("/{connector_id}/receive", status_code=202)
async def receive_webhook(
    connector_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Receive an inbound webhook payload and log it as an integration_call."""
    connector = await session.get(ConnectorRegistryModel, connector_id)
    if not connector or not connector.enabled:
        raise coded(404, CONN_NOTFOUND_404, "Connector not found or disabled")

    try:
        payload = await request.json()
    except Exception:
        payload = {"raw": (await request.body()).decode(errors="replace")}

    call = IntegrationCallModel(
        connector_id=connector.id,
        status="success",
        request=payload,
        response={"received": True},
        latency_ms=0,
        completed_at=datetime.now(timezone.utc),
    )
    session.add(call)
    await session.commit()
    return {"received": True, "connector": connector.name}


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _get_or_404(session: AsyncSession, connector_id: uuid.UUID) -> ConnectorRegistryModel:
    c = await session.get(ConnectorRegistryModel, connector_id)
    if not c:
        raise coded(404, CONN_NOTFOUND_404, f"Connector {connector_id} not found")
    return c


def _connector_out(c: ConnectorRegistryModel, include_config: bool = False) -> dict:
    d = {
        "id":             str(c.id),
        "name":           c.name,
        "connector_type": c.connector_type,
        "description":    c.description,
        "enabled":        c.enabled,
        "last_tested_at": c.last_tested_at.isoformat() if c.last_tested_at else None,
        "last_test_ok":   c.last_test_ok,
        "created_by":     c.created_by,
        "created_at":     c.created_at.isoformat() if c.created_at else None,
    }
    d["credential_expires_at"]   = c.credential_expires_at.isoformat() if getattr(c, "credential_expires_at", None) else None
    d["credentials_updated_at"] = c.credentials_updated_at.isoformat() if getattr(c, "credentials_updated_at", None) else None
    # Named operations (2026-11-04) — key, label, method only: what a form
    # builder offers. Paths stay in the config.
    from velaris_shared.hxbridge.operations import OperationError, validate_operations
    try:
        d["operations"] = [{"key": o["key"], "label": o["label"], "method": o["method"]}
                           for o in validate_operations(c.connector_type, c.config)]
    except OperationError:
        d["operations"] = []
    if include_config:
        d["config"]      = c.config or {}
        d["credentials"] = mask_credentials(c.credentials or {})
    return d


def _call_out(c: IntegrationCallModel) -> dict:
    return {
        "id":           str(c.id),
        "connector_id": str(c.connector_id) if c.connector_id else None,
        "case_id":      str(c.case_id) if c.case_id else None,
        "step_id":      c.step_id,
        "status":       c.status,
        "latency_ms":   c.latency_ms,
        "error":        c.error,
        "retry_count":  c.retry_count,
        "created_at":   c.created_at.isoformat() if c.created_at else None,
    }


def _dlq_out(d: DeadLetterQueueModel) -> dict:
    return {
        "id":             str(d.id),
        "connector_id":   str(d.connector_id) if d.connector_id else None,
        "case_id":        str(d.case_id) if d.case_id else None,
        "error":          d.error,
        "retry_count":    d.retry_count,
        "max_retries":    d.max_retries,
        "next_retry_at":  d.next_retry_at.isoformat() if d.next_retry_at else None,
        "resolution":     d.resolution,
        "created_at":     d.created_at.isoformat() if d.created_at else None,
    }
