"""HxBridge — connector executor.

Runs a connector call, records it in integration_calls, fires HxStream events,
and pushes failures to the dead_letter_queue with exponential-backoff retry scheduling.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from typing import Any
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import (
    ConnectorRegistryModel, IntegrationCallModel, DeadLetterQueueModel,
    FieldPopulationAuditModel,
)
from case_service.hxbridge.encryption import decrypt_credentials, encrypt_credentials
from case_service.hxbridge.protocol import get_connector

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
#: Where a dead-letter item keeps the operation it was called with (2026-11-08).
DLQ_OPERATION_KEY = "_velaris_operation"
_RETRY_DELAYS = [60, 300, 1800]  # seconds: 1m, 5m, 30m


def normalize_response(response: dict, connector_type: str = "") -> dict:
    """Normalise connector response to safe JSON-serialisable form.

    - mongo_safe: ObjectId / Decimal128 / datetime → str
    - postgres_safe: numeric (Decimal) → str to avoid precision loss
    - default: pass-through with str() fallback on non-serialisable values
    """
    def _coerce(obj):
        if isinstance(obj, dict):
            return {k: _coerce(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_coerce(v) for v in obj]
        try:
            json.dumps(obj)
            return obj
        except (TypeError, ValueError):
            return str(obj)

    return _coerce(response)


def _check_response_size(response: dict) -> tuple[bool, int]:
    """Return (is_overflow, size_bytes). Raises RuntimeError if hard limit exceeded."""
    from velaris_shared.config import get_settings
    cfg = get_settings()
    try:
        size = len(json.dumps(response).encode())
    except Exception:
        size = 0
    if size > cfg.connector_hard_reject_bytes:
        raise RuntimeError(
            f"Connector response too large: {size} bytes > {cfg.connector_hard_reject_bytes} limit"
        )
    return size > cfg.connector_overflow_threshold_bytes, size


async def execute_connector(
    session: AsyncSession,
    connector: ConnectorRegistryModel,
    input_data: dict,
    *,
    case_id: uuid.UUID | None = None,
    step_id: str | None = None,
    operation: str | None = None,
) -> dict:
    """Execute a connector, record the call, emit HxStream event.

    `operation` (2026-11-04): one of the connector's named operations — chosen
    by a saved form field or step, never by whoever fills the form. None = the
    connector's own request, as before.

    Returns the connector response dict on success.
    Raises RuntimeError on unrecoverable failure (already pushed to DLQ).
    """
    call = IntegrationCallModel(
        connector_id=connector.id,
        case_id=case_id,
        step_id=step_id,
        status="running",
        request=input_data,
    )
    session.add(call)
    await session.flush()

    start = time.monotonic()
    try:
        creds = decrypt_credentials(connector.credentials or {})
        from velaris_shared.hxbridge.security import check_connector_targets
        await check_connector_targets(session, connector.tenant_id, connector.config)
        from velaris_shared.hxbridge.security import platform_owned, runtime_config
        from velaris_shared.hxbridge.operations import OperationError, apply_operation
        try:
            op_config = apply_operation(connector.connector_type, connector.config, operation)
        except OperationError as exc:
            raise RuntimeError(str(exc)) from exc
        impl = get_connector(connector.connector_type, runtime_config(op_config, await platform_owned(session, connector.tenant_id)), creds)
        raw_response = await impl.execute(input_data)
        latency_ms = int((time.monotonic() - start) * 1000)

        response = normalize_response(raw_response, connector.connector_type)

        # Tiered large-response handling
        is_overflow, size = _check_response_size(response)
        if is_overflow:
            ref_url = await _store_overflow(response, call.id)
            response = {"_overflow": True, "ref_url": ref_url, "size_bytes": size}

        call.status       = "success"
        call.response     = response
        call.latency_ms   = latency_ms
        call.completed_at = datetime.now(timezone.utc)

        # SD-7: persist rotated refresh token (Xero rotates on every use)
        new_rt = getattr(impl, "_new_refresh_token", None)
        token_refreshed = getattr(impl, "_token_refreshed", False)
        if new_rt or token_refreshed:
            now = datetime.now(timezone.utc)
            if new_rt:
                stored = connector.credentials or {}
                updated_creds = dict(decrypt_credentials(stored))
                updated_creds["refresh_token"] = new_rt
                # Re-encrypt in the format it was ALREADY in. Without this a
                # connector stored under HxVault (hxv2, per-tenant DEK) was
                # silently rewritten under the platform key the first time the
                # provider rotated its refresh token — Xero rotates on every
                # use — and from then on crypto-shredding that tenant no longer
                # covered these credentials. A token refresh must never change
                # who can read a secret.
                was_vaulted = str(stored.get("_enc", "")).startswith("hxv2:")
                connector.credentials = encrypt_credentials(
                    updated_creds,
                    tenant_id=connector.tenant_id,
                    vault=was_vaulted,
                )
            connector.credentials_updated_at = now
        await session.commit()

        await _emit_hxstream(connector.name, "success", latency_ms, case_id)
        return response

    except Exception as exc:
        latency_ms = int((time.monotonic() - start) * 1000)
        call.status       = "failed"
        call.error        = str(exc)
        call.latency_ms   = latency_ms
        call.completed_at = datetime.now(timezone.utc)
        await session.commit()

        # The operation travels with the item, so a retry runs the same request
        # (until 2026-11-08 a retry dropped it and ran the connector's default).
        # A caller's own key of that name is dropped: only the call's operation counts.
        dlq_payload = {k: v for k, v in (input_data or {}).items() if k != DLQ_OPERATION_KEY}
        if operation:
            dlq_payload[DLQ_OPERATION_KEY] = operation
        await _push_dlq(session, connector, dlq_payload, case_id, step_id, str(exc))
        await _emit_hxstream(connector.name, "failed", latency_ms, case_id, error=str(exc))
        raise RuntimeError(f"Connector '{connector.name}' failed: {exc}") from exc


async def retry_dlq_item(
    session: AsyncSession,
    dlq_item: DeadLetterQueueModel,
    connector: ConnectorRegistryModel,
) -> bool:
    """Attempt one retry of a dead-letter item. Returns True on success."""
    dlq_item.retry_count += 1
    payload = dict(dlq_item.payload or {})
    op = payload.pop(DLQ_OPERATION_KEY, None)
    try:
        result = await execute_connector(
            session, connector, payload,
            case_id=dlq_item.case_id,
            step_id=dlq_item.step_id,
            operation=op if isinstance(op, str) else None,
        )
        dlq_item.resolution  = "retried"
        dlq_item.resolved_at = datetime.now(timezone.utc)
        await session.commit()
        return True
    except Exception as exc:
        if dlq_item.retry_count >= dlq_item.max_retries:
            dlq_item.resolution  = "abandoned"
            dlq_item.resolved_at = datetime.now(timezone.utc)
        else:
            delay = _RETRY_DELAYS[min(dlq_item.retry_count - 1, len(_RETRY_DELAYS) - 1)]
            dlq_item.next_retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
        await session.commit()
        return False


async def _push_dlq(
    session: AsyncSession,
    connector: ConnectorRegistryModel,
    payload: dict,
    case_id: uuid.UUID | None,
    step_id: str | None,
    error: str,
) -> None:
    delay = _RETRY_DELAYS[0]
    item = DeadLetterQueueModel(
        connector_id=connector.id,
        case_id=case_id,
        step_id=step_id,
        payload=payload,
        error=error,
        retry_count=0,
        max_retries=_MAX_RETRIES,
        next_retry_at=datetime.now(timezone.utc) + timedelta(seconds=delay),
    )
    session.add(item)
    await session.flush()
    logger.warning("Pushed to DLQ: connector=%s error=%s", connector.name, error)


async def _store_overflow(response: dict, call_id: uuid.UUID) -> str:
    """Persist an oversized response payload to local storage; return a ref URL."""
    import os
    from velaris_shared.config import get_settings
    cfg = get_settings()
    try:
        path = os.path.join(cfg.storage_local_path, "connector-overflow")
        os.makedirs(path, exist_ok=True)
        file_path = os.path.join(path, f"{call_id}.json")
        with open(file_path, "w") as f:
            json.dump(response, f)
        return f"/api/v1/hxbridge/overflow/{call_id}"
    except Exception as exc:
        logger.warning("overflow store failed: %s", exc)
        return ""


async def form_lookup_connector(
    session: AsyncSession,
    connector: ConnectorRegistryModel,
    input_data: dict,
    *,
    tenant_id: str,
    form_id: str | None = None,
    field_key: str | None = None,
    user_id: str | None = None,
    case_id: uuid.UUID | None = None,
    operation: str | None = None,
) -> dict:
    """Execute a connector for a form lookup and log the field population audit trail."""
    response = await execute_connector(
        session, connector, input_data, case_id=case_id, step_id="form_lookup",
        operation=operation,
    )
    if field_key:
        response_hash = hashlib.sha256(
            json.dumps(response, sort_keys=True, default=str).encode()
        ).hexdigest()
        audit = FieldPopulationAuditModel(
            tenant_id=tenant_id,
            case_id=case_id,
            form_id=form_id,
            field_key=field_key,
            connector_id=connector.id,
            user_id=user_id,
            response_hash=response_hash,
        )
        session.add(audit)
        await session.flush()
    return response


async def _emit_hxstream(
    connector_name: str,
    status: str,
    latency_ms: int,
    case_id: uuid.UUID | None,
    error: str | None = None,
) -> None:
    """Fire an integration_call event into HxStream."""
    try:
        from velaris_shared.hxstream.emitter import emit_event
        payload = {
            "connector":  connector_name,
            "status":     status,
            "latency_ms": latency_ms,
        }
        if error:
            payload["error"] = error[:200]
        await emit_event(
            event_type="integration_call",
            actor="system",
            case_id=str(case_id) if case_id else None,
            payload=payload,
        )
    except Exception:
        pass  # HxStream emission is best-effort



async def run_connector_by_id(
    session: AsyncSession,
    connector_id: Any,
    payload: dict,
    *,
    owner_tenant: Any,
    case_id: uuid.UUID | None = None,
    step_id: str | None = None,
    operation: str | None = None,
) -> dict:
    """Run a connector named by id from a saved workflow step or process node
    (2026-11-04). Both callers had called `get_connector(connector_id,
    session)` — the TYPE factory, which takes (type, config, credentials) — so
    every service task that named a connector failed; and they bypassed this
    module's recording, target checks and operations.

    The connector must be enabled and be the owning tenant's own (the platform
    operator's only for the default tenant); anything else is answered as not
    found.
    """
    from velaris_shared.hxbridge.security import usable_by
    try:
        cid = connector_id if isinstance(connector_id, uuid.UUID) else uuid.UUID(str(connector_id))
    except ValueError:
        raise RuntimeError(f"Connector '{connector_id}' not found")
    connector = await session.get(ConnectorRegistryModel, cid)
    # Its own tenant's, or — for the default tenant alone — the operator's.
    if connector is None or not await usable_by(session, connector, owner_tenant):
        raise RuntimeError(f"Connector '{connector_id}' not found")
    if not connector.enabled:
        raise RuntimeError(f"Connector '{connector.name}' is disabled")
    return await execute_connector(session, connector, payload, case_id=case_id,
                                   step_id=step_id, operation=operation)
