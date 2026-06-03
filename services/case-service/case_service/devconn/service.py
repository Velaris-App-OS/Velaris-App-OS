"""P53 Developer & Custom Connectors service."""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import (
    CaseInstanceModel,
    ConnectorRegistryModel,
    WebhookReceiverEventModel,
    WebhookReceiverRuleModel,
)
from case_service.hxbridge.encryption import encrypt_credentials, decrypt_credentials

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _get_nested(obj: object, path: str) -> object:
    """Traverse dot-path into nested dict/list."""
    for key in path.split("."):
        if isinstance(obj, dict):
            obj = obj.get(key)
        elif isinstance(obj, list):
            try:
                obj = obj[int(key)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return obj


# ── Webhook Receiver ──────────────────────────────────────────────────────────

async def receive_webhook(
    session: AsyncSession,
    connector_id: uuid.UUID,
    payload: dict,
    tenant_id: str,
) -> WebhookReceiverEventModel:
    event = WebhookReceiverEventModel(
        tenant_id=tenant_id,
        connector_id=connector_id,
        payload=payload,
        status="received",
    )
    session.add(event)
    await session.flush()

    rules = (await session.execute(
        select(WebhookReceiverRuleModel).where(
            WebhookReceiverRuleModel.connector_id == connector_id,
            WebhookReceiverRuleModel.enabled == True,  # noqa: E712
        )
    )).scalars().all()

    matched_case: CaseInstanceModel | None = None
    matched_rule: WebhookReceiverRuleModel | None = None

    for rule in rules:
        case = await _find_case(session, rule, payload)
        if case:
            matched_case = case
            matched_rule = rule
            break

    if matched_case and matched_rule:
        event.matched_case_id = matched_case.id
        event.rule_id         = matched_rule.id
        event.status          = "matched"

        # Apply field updates to case data
        if matched_rule.field_updates:
            data = dict(matched_case.data or {})
            for case_field, payload_path in matched_rule.field_updates.items():
                val = _get_nested(payload, payload_path)
                if val is not None:
                    data[case_field] = val
            matched_case.data = data

        # Advance stage if configured
        if matched_rule.advance_stage:
            try:
                async with session.begin_nested():
                    from case_service.db.models import CaseTypeModel
                    ct = (await session.execute(
                        select(CaseTypeModel).where(CaseTypeModel.id == matched_case.case_type_id)
                    )).scalar_one_or_none()
                    if ct:
                        from case_service.api.routers.cases import _auto_advance_if_complete
                        await _auto_advance_if_complete(session, matched_case, ct.definition_json or {})
            except Exception as exc:
                logger.warning("Auto-advance failed: %s", exc)
    else:
        event.status = "no_match"

    event.processed_at = _utcnow()
    await session.flush()
    return event


async def _find_case(
    session: AsyncSession,
    rule: WebhookReceiverRuleModel,
    payload: dict,
) -> CaseInstanceModel | None:
    # Strategy 1: payload contains case UUID directly
    if rule.case_id_field:
        raw = _get_nested(payload, rule.case_id_field)
        if raw:
            try:
                cid = uuid.UUID(str(raw))
                return (await session.execute(
                    select(CaseInstanceModel).where(CaseInstanceModel.id == cid)
                )).scalar_one_or_none()
            except ValueError:
                pass

    # Strategy 2: match a case data field against a payload value
    if rule.match_case_field and rule.match_payload_field:
        match_val = _get_nested(payload, rule.match_payload_field)
        if match_val is not None:
            from sqlalchemy import cast, String
            from sqlalchemy.dialects.postgresql import JSONB
            rows = (await session.execute(select(CaseInstanceModel))).scalars().all()
            for case in rows:
                data = case.data or {}
                if str(data.get(rule.match_case_field)) == str(match_val):
                    return case

    return None


async def list_rules(session: AsyncSession, tenant_id: str) -> list[WebhookReceiverRuleModel]:
    rows = (await session.execute(
        select(WebhookReceiverRuleModel)
        .where(WebhookReceiverRuleModel.tenant_id == tenant_id)
        .order_by(WebhookReceiverRuleModel.created_at.desc())
    )).scalars().all()
    return list(rows)


async def create_rule(session: AsyncSession, rule: WebhookReceiverRuleModel) -> WebhookReceiverRuleModel:
    session.add(rule)
    await session.flush()
    return rule


async def list_events(
    session: AsyncSession,
    status: str | None = None,
    connector_id: uuid.UUID | None = None,
    limit: int = 100,
) -> list[WebhookReceiverEventModel]:
    q = select(WebhookReceiverEventModel).order_by(WebhookReceiverEventModel.received_at.desc()).limit(limit)
    if status:
        q = q.where(WebhookReceiverEventModel.status == status)
    if connector_id:
        q = q.where(WebhookReceiverEventModel.connector_id == connector_id)
    rows = (await session.execute(q)).scalars().all()
    return list(rows)


# ── Custom HTTP Connector Builder ─────────────────────────────────────────────

async def build_http_connector(
    session: AsyncSession,
    tenant_id: str,
    name: str,
    method: str,
    url: str,
    headers: dict,
    auth_type: str,
    body_template: str,
    response_mapping: dict,
    credentials: dict,
) -> ConnectorRegistryModel:
    row = ConnectorRegistryModel(
        name=name,
        connector_type="http_custom",
        config={
            "method":           method.upper(),
            "url":              url,
            "headers":          headers,
            "auth_type":        auth_type,
            "body_template":    body_template,
            "response_mapping": response_mapping,
        },
        credentials=encrypt_credentials(credentials),
        tenant_id=tenant_id,
        enabled=True,
    )
    session.add(row)
    await session.flush()
    return row


# ── OpenAPI Auto-Connector ────────────────────────────────────────────────────

async def generate_from_openapi(spec_text: str, connector_name: str) -> dict:
    """Call HxNexus to generate an http_custom connector config from an OpenAPI spec."""
    prompt = f"""You are a Helix connector generator. Given this OpenAPI/Swagger spec, generate a connector configuration.

OpenAPI spec:
{spec_text[:8000]}

Return a JSON object with these exact keys:
{{
  "name": "{connector_name}",
  "suggested_operations": [
    {{
      "operation_id": "string",
      "summary": "string",
      "method": "GET|POST|PUT|PATCH|DELETE",
      "url": "base_url + path (use {{var}} for path params)",
      "headers": {{}},
      "auth_type": "none|bearer|basic",
      "body_template": "JSON string template with {{var}} placeholders or empty",
      "response_mapping": {{"case_field": "response.json.dotpath"}},
      "step_type_suggestion": "suggested Helix step type name"
    }}
  ],
  "auth_notes": "brief description of authentication required",
  "base_url": "the server base URL from the spec"
}}"""

    try:
        from case_service.hxnexus.factory import generate_json
        result = await generate_json(prompt, system="You are a Helix BPM connector configuration generator.")
        if result:
            return result
    except Exception as exc:
        logger.warning("HxNexus generate_json failed: %s", exc)

    # Fallback: basic heuristic parse
    return _heuristic_parse(spec_text, connector_name)


def _heuristic_parse(spec_text: str, name: str) -> dict:
    """Best-effort parse without LLM — extracts paths and methods."""
    try:
        spec = json.loads(spec_text)
    except Exception:
        try:
            import yaml  # type: ignore
            spec = yaml.safe_load(spec_text)
        except Exception:
            return {"name": name, "suggested_operations": [], "auth_notes": "Could not parse spec", "base_url": ""}

    servers = spec.get("servers", [{}])
    base_url = servers[0].get("url", "") if servers else ""
    paths = spec.get("paths", {})

    ops = []
    for path, methods in list(paths.items())[:10]:
        for method, op in methods.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            ops.append({
                "operation_id":       op.get("operationId", f"{method}_{path.replace('/', '_')}"),
                "summary":            op.get("summary", ""),
                "method":             method.upper(),
                "url":                base_url + re.sub(r"\{(\w+)\}", r"{\1}", path),
                "headers":            {},
                "auth_type":          "bearer" if spec.get("components", {}).get("securitySchemes") else "none",
                "body_template":      "",
                "response_mapping":   {},
                "step_type_suggestion": f"http_{method}",
            })

    return {
        "name":                name,
        "suggested_operations": ops,
        "auth_notes":          "Check the spec security schemes for authentication requirements.",
        "base_url":            base_url,
    }
