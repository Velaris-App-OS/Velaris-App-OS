"""HxDeploy Packager — full and delta environment bundle serialiser.

Captures ALL design-time artifacts for a Velaris environment:
  - Data models, form definitions, rule definitions
  - Case types, stages, steps
  - Business calendars (SLA depends on these)
  - Email templates
  - Email accounts          ← host/port/config only; passwords STRIPPED
  - Connector registry      ← config + schema only; credentials STRIPPED
  - Process definitions (BPMN XML)
  - Escalation trees
  - Webhook subscriptions   ← url/events only; secret STRIPPED
  - Outbound connector rules
  - Webhook receiver rules  (Dev Connectors inbound routing)
  - Portals
  - Access roles
  - Access groups

Stripped fields are tracked in `needs_configuration` so the target
admin knows exactly what credentials to fill in after import.

Delta mode (since != None): only artifacts whose updated_at >= since are
included, except FK-root objects (DataModels, Portals, Connectors, etc.)
which are always included to keep ID maps intact on the target.

Runtime data (case instances, emails, audit logs, docs) is never included.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import (
    AccessGroupModel,
    AccessRoleModel,
    BusinessCalendarModel,
    CaseTypeModel,
    CaseTypeStageModel,
    CaseTypeStepModel,
    ConnectorRegistryModel,
    DataModelModel,
    EmailAccountModel,
    EmailTemplateModel,
    EscalationTreeModel,
    FormDefinitionModel,
    OutboundConnectorRuleModel,
    PortalModel,
    ProcessDefinitionModel,
    RuleDefinitionModel,
    WebhookReceiverRuleModel,
    WebhookSubscriptionModel,
)

logger = logging.getLogger(__name__)

BUNDLE_SCHEMA_VERSION = "2"   # bumped from v1 to reflect full scope

_NEEDS_CREDS = "__NEEDS_CONFIGURATION__"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _s(v: Any) -> str | None:
    return str(v) if v is not None else None


# ── Bundle builder ────────────────────────────────────────────────────────────

async def build_bundle(
    session: AsyncSession,
    tenant_id: str,
    case_type_ids: list[uuid.UUID] | None = None,
    since: datetime | None = None,
) -> dict:
    """Serialise the design-time environment for tenant_id.

    Args:
        case_type_ids: restrict to specific case types (+ their dependents).
                       None = all case types.
        since: if set, only include artifacts updated/created at or after this
               datetime (delta mode). FK-root objects are always included so
               that apply_bundle() can build its ID maps correctly.
    Returns:
        bundle dict — ready to POST to /api/v1/deploy/import
    """
    is_delta = since is not None
    needs_configuration: list[dict] = []

    def _delta(q, model):
        """Apply updated_at filter when in delta mode and model has that column."""
        if is_delta and hasattr(model, "updated_at"):
            q = q.where(model.updated_at >= since)
        return q

    # ── 1. Data models (always included — FK root for FormDefinitions) ────
    data_models = list((await session.execute(select(DataModelModel))).scalars().all())

    # ── 2. Business calendars (always included — referenced by SLA policies)
    calendars = list((await session.execute(select(BusinessCalendarModel))).scalars().all())

    # ── 3. Access roles (always included — FK root for AccessGroups) ──────
    roles = list((await session.execute(select(AccessRoleModel))).scalars().all())

    # ── 4. Portals (always included — FK root for AccessGroups) ──────────
    portals = list((await session.execute(select(PortalModel))).scalars().all())

    # ── 5. Email templates ────────────────────────────────────────────────
    email_templates = list((await session.execute(
        _delta(select(EmailTemplateModel), EmailTemplateModel)
    )).scalars().all())

    # ── 6. Email accounts (strip passwords) ───────────────────────────────
    email_accounts_raw = list((await session.execute(
        _delta(select(EmailAccountModel), EmailAccountModel)
    )).scalars().all())
    email_accounts = []
    for ea in email_accounts_raw:
        entry = {
            "id": _s(ea.id), "name": ea.name, "address": ea.address,
            "smtp_host": ea.smtp_host, "smtp_port": ea.smtp_port,
            "smtp_username": ea.smtp_username, "smtp_use_tls": ea.smtp_use_tls,
            "imap_host": ea.imap_host, "imap_port": ea.imap_port,
            "imap_username": ea.imap_username, "imap_use_ssl": ea.imap_use_ssl,
            "imap_folder": ea.imap_folder,
            "poll_interval_seconds": ea.poll_interval_seconds,
            "is_active": ea.is_active, "is_default_outbound": ea.is_default_outbound,
            # passwords deliberately absent — will be null on target
            "smtp_password": None, "imap_password": None,
        }
        email_accounts.append(entry)
        missing = []
        if ea.smtp_password:
            missing.append("smtp_password")
        if ea.imap_password:
            missing.append("imap_password")
        if missing:
            needs_configuration.append({
                "type": "email_account", "id": _s(ea.id),
                "name": ea.name, "missing": missing,
            })

    # ── 7. Connectors (always included — FK root for outbound/receiver rules)
    connectors_raw = list((await session.execute(select(ConnectorRegistryModel))).scalars().all())
    connectors = []
    for c in connectors_raw:
        entry = {
            "id": _s(c.id), "name": c.name, "connector_type": c.connector_type,
            "description": c.description, "config_schema": c.config_schema,
            "config": c.config, "credentials": {},   # always empty in bundle
            "enabled": c.enabled,
        }
        connectors.append(entry)
        if c.credentials:
            needs_configuration.append({
                "type": "connector", "id": _s(c.id),
                "name": c.name, "missing": list(c.credentials.keys()),
            })

    # ── 8. Process definitions (BPMN) ─────────────────────────────────────
    process_defs = list((await session.execute(
        _delta(select(ProcessDefinitionModel), ProcessDefinitionModel)
    )).scalars().all())

    # ── 9. Form definitions ───────────────────────────────────────────────
    forms = list((await session.execute(
        _delta(select(FormDefinitionModel), FormDefinitionModel)
    )).scalars().all())

    # ── 10. Rule definitions ──────────────────────────────────────────────
    rules = list((await session.execute(
        _delta(select(RuleDefinitionModel), RuleDefinitionModel)
    )).scalars().all())

    # ── 11. Case types ────────────────────────────────────────────────────
    ct_q = select(CaseTypeModel).where(CaseTypeModel.is_deleted.is_(False)).order_by(CaseTypeModel.name)
    if case_type_ids:
        ct_q = ct_q.where(CaseTypeModel.id.in_(case_type_ids))
    ct_q = _delta(ct_q, CaseTypeModel)
    case_types = list((await session.execute(ct_q)).scalars().all())
    ct_ids = [ct.id for ct in case_types]

    # ── 12. Stages + steps ────────────────────────────────────────────────
    stages: list[CaseTypeStageModel] = []
    steps: list[CaseTypeStepModel] = []
    if ct_ids:
        stages = list((await session.execute(
            select(CaseTypeStageModel)
            .where(CaseTypeStageModel.case_type_id.in_(ct_ids))
            .order_by(CaseTypeStageModel.case_type_id, CaseTypeStageModel.order)
        )).scalars().all())
        steps = list((await session.execute(
            select(CaseTypeStepModel).where(CaseTypeStepModel.case_type_id.in_(ct_ids))
        )).scalars().all())

    # ── 13. Escalation trees ──────────────────────────────────────────────
    escalations = list((await session.execute(
        _delta(select(EscalationTreeModel), EscalationTreeModel)
    )).scalars().all())

    # ── 14. Webhook subscriptions (strip secret) ──────────────────────────
    webhooks_raw = list((await session.execute(
        _delta(select(WebhookSubscriptionModel), WebhookSubscriptionModel)
    )).scalars().all())
    webhooks = []
    for w in webhooks_raw:
        entry = {
            "id": _s(w.id), "name": w.name, "url": w.url, "secret": None,
            "events": w.events, "case_type_id": _s(w.case_type_id),
            "is_active": w.is_active, "headers": w.headers,
            "retry_count": w.retry_count, "timeout_seconds": w.timeout_seconds,
        }
        webhooks.append(entry)
        if w.secret:
            needs_configuration.append({
                "type": "webhook_subscription", "id": _s(w.id),
                "name": w.name, "missing": ["secret"],
            })

    # ── 15. Outbound connector rules ──────────────────────────────────────
    ocr = list((await session.execute(
        _delta(select(OutboundConnectorRuleModel), OutboundConnectorRuleModel)
    )).scalars().all())

    # ── 16. Webhook receiver rules (no updated_at — always included) ──────
    receiver_rules = list((await session.execute(select(WebhookReceiverRuleModel))).scalars().all())

    # ── 17. Access groups ─────────────────────────────────────────────────
    access_groups = list((await session.execute(
        _delta(select(AccessGroupModel), AccessGroupModel)
    )).scalars().all())

    # ── Assemble ──────────────────────────────────────────────────────────
    bundle: dict = {
        "bundle_schema_version": BUNDLE_SCHEMA_VERSION,
        "created_at": _utcnow_iso(),
        "source_tenant_id": tenant_id,
        "is_delta": is_delta,
        "delta_since": since.isoformat() if since else None,
        "needs_configuration": needs_configuration,
        "summary": {
            "data_models":        len(data_models),
            "business_calendars": len(calendars),
            "access_roles":       len(roles),
            "portals":            len(portals),
            "email_templates":    len(email_templates),
            "email_accounts":     len(email_accounts),
            "connectors":         len(connectors),
            "process_definitions": len(process_defs),
            "form_definitions":   len(forms),
            "rule_definitions":   len(rules),
            "case_types":         len(case_types),
            "stages":             len(stages),
            "steps":              len(steps),
            "escalation_trees":   len(escalations),
            "webhook_subscriptions": len(webhooks),
            "outbound_connector_rules": len(ocr),
            "webhook_receiver_rules": len(receiver_rules),
            "access_groups":      len(access_groups),
            "needs_configuration": len(needs_configuration),
        },
        "data_models": [
            {"id": _s(m.id), "name": m.name, "version": m.version, "definition_json": m.definition_json}
            for m in data_models
        ],
        "business_calendars": [
            {
                "id": _s(c.id), "name": c.name, "timezone": c.timezone,
                "work_days": c.work_days, "work_start_hour": c.work_start_hour,
                "work_end_hour": c.work_end_hour, "holidays": c.holidays,
                "description": c.description,
            }
            for c in calendars
        ],
        "access_roles": [
            {
                "id": _s(r.id), "name": r.name, "description": r.description,
                "privileges": r.privileges,
            }
            for r in roles
        ],
        "portals": [
            {
                "id": _s(p.id), "name": p.name, "portal_type": p.portal_type,
                "modules": p.modules, "homepage": p.homepage, "theme": p.theme,
                "is_active": p.is_active,
            }
            for p in portals
        ],
        "email_templates": [
            {
                "id": _s(t.id), "name": t.name, "description": t.description,
                "subject": t.subject, "body_text": t.body_text, "body_html": t.body_html,
                "engine": t.engine, "scope": t.scope, "case_type_id": _s(t.case_type_id),
                "is_active": t.is_active,
            }
            for t in email_templates
        ],
        "email_accounts": email_accounts,
        "connectors": connectors,
        "process_definitions": [
            {
                "id": _s(p.id), "name": p.name, "version": p.version,
                "description": p.description, "bpmn_xml": p.bpmn_xml,
                "case_type_id": p.case_type_id, "status": p.status,
            }
            for p in process_defs
        ],
        "form_definitions": [
            {
                "id": _s(f.id), "name": f.name, "version": f.version,
                "data_model_id": _s(f.data_model_id), "definition_json": f.definition_json,
            }
            for f in forms
        ],
        "rule_definitions": [
            {
                "id": _s(r.id), "name": r.name, "version": r.version,
                "rule_type": r.rule_type, "scope": r.scope,
                "scope_target_id": r.scope_target_id, "definition_json": r.definition_json,
                "enabled": r.enabled, "priority": r.priority,
            }
            for r in rules
        ],
        "case_types": [
            {
                "id": _s(ct.id), "name": ct.name, "version": ct.version,
                "description": ct.description, "icon": ct.icon, "color": ct.color,
                "tags": ct.tags or [], "default_priority": ct.default_priority,
                "portal_enabled": ct.portal_enabled, "definition_json": ct.definition_json,
            }
            for ct in case_types
        ],
        "stages": [
            {
                "id": _s(s.id), "case_type_id": _s(s.case_type_id),
                "stage_id": s.stage_id, "name": s.name, "stage_type": s.stage_type,
                "order": s.order, "sla_policy_id": s.sla_policy_id,
                "definition_json": s.definition_json,
            }
            for s in stages
        ],
        "steps": [
            {
                "id": _s(s.id), "case_type_id": _s(s.case_type_id),
                "stage_id": _s(s.stage_id), "step_id": s.step_id,
                "name": s.name, "step_type": s.step_type,
                "bpmn_element_id": s.bpmn_element_id, "definition_json": s.definition_json,
            }
            for s in steps
        ],
        "escalation_trees": [
            {
                "id": _s(e.id), "name": e.name, "description": e.description,
                "scope": e.scope, "case_type_id": _s(e.case_type_id),
                "tree_json": e.tree_json, "is_active": e.is_active,
            }
            for e in escalations
        ],
        "webhook_subscriptions": webhooks,
        "outbound_connector_rules": [
            {
                "id": _s(r.id), "name": r.name, "trigger_event": r.trigger_event,
                "case_type_id": _s(r.case_type_id), "condition_expr": r.condition_expr,
                "connector_id": _s(r.connector_id), "input_mapping": r.input_mapping,
                "enabled": r.enabled,
            }
            for r in ocr
        ],
        "webhook_receiver_rules": [
            {
                "id": _s(r.id), "name": r.name, "connector_id": _s(r.connector_id),
                "case_id_field": r.case_id_field, "match_case_field": r.match_case_field,
                "match_payload_field": r.match_payload_field,
                "field_updates": r.field_updates, "advance_stage": r.advance_stage,
                "enabled": r.enabled,
            }
            for r in receiver_rules
        ],
        "access_groups": [
            {
                "id": _s(g.id), "name": g.name, "description": g.description,
                "portal_id": _s(g.portal_id), "role_ids": g.role_ids,
                "allowed_case_type_ids": g.allowed_case_type_ids,
                "allowed_queue_ids": g.allowed_queue_ids,
                "is_default": g.is_default, "is_active": g.is_active,
            }
            for g in access_groups
        ],
    }

    logger.info(
        "bundle v2 built%s: %d case types, %d connectors, %d email accounts, "
        "%d templates, %d needs-configuration items (tenant %s)",
        f" [delta since {since.date()}]" if since else "",
        len(case_types), len(connectors), len(email_accounts),
        len(email_templates), len(needs_configuration), tenant_id,
    )
    return bundle


# ── Delta bundle convenience wrapper ─────────────────────────────────────────

async def build_delta_bundle(
    session: AsyncSession,
    tenant_id: str,
    env_id: uuid.UUID,
    case_type_ids: list[uuid.UUID] | None = None,
) -> dict:
    """Build a delta bundle containing only artifacts changed since env's last deploy.

    Falls back to a full bundle if the environment has never been deployed to.
    """
    from case_service.db.models import EnvironmentRegistryModel
    env = await session.get(EnvironmentRegistryModel, env_id)
    since: datetime | None = getattr(env, "last_deployed_at", None) if env else None
    return await build_bundle(session, tenant_id, case_type_ids, since=since)


# ── Bundle applier ────────────────────────────────────────────────────────────

async def apply_bundle(
    session: AsyncSession,
    bundle: dict,
    target_tenant_id: str,
) -> dict:
    """Apply a received bundle to this Velaris instance.

    Import order respects FK dependencies:
      DataModels → Calendars → AccessRoles → Portals → EmailTemplates
      → EmailAccounts → Connectors → ProcessDefs → Forms → Rules
      → CaseTypes → Stages → Steps → EscalationTrees
      → WebhookSubscriptions → OutboundConnectorRules
      → WebhookReceiverRules → AccessGroups

    Credential preservation rule:
      If a sensitive field (password, credentials, secret) already exists
      on the target record, it is NEVER overwritten — even if the bundle
      sends null. The admin updates credentials manually on the target.

    Returns: {"imported": N, "skipped": M, "errors": [...], "needs_configuration": [...]}
    """
    imported = 0
    skipped  = 0
    errors:  list[str] = []
    needs_configuration: list[dict] = list(bundle.get("needs_configuration", []))

    # ID maps: source_id_str → local_uuid (needed for FK remapping)
    dm_id_map:        dict[str, uuid.UUID] = {}
    portal_id_map:    dict[str, uuid.UUID] = {}
    connector_id_map: dict[str, uuid.UUID] = {}
    ct_id_map:        dict[str, uuid.UUID] = {}
    stage_id_map:     dict[str, uuid.UUID] = {}

    # ── helpers ──────────────────────────────────────────────────────────
    def _uuid(s: str | None) -> uuid.UUID | None:
        try:
            return uuid.UUID(s) if s else None
        except Exception:
            return None

    def _map_or_new(source_id: str | None) -> uuid.UUID:
        return _uuid(source_id) or uuid.uuid4()

    # ── 1. Data models ───────────────────────────────────────────────────
    for m in bundle.get("data_models", []):
        try:
            ex = (await session.execute(
                select(DataModelModel).where(
                    DataModelModel.name == m["name"],
                    DataModelModel.version == m["version"],
                )
            )).scalar_one_or_none()
            if ex:
                ex.definition_json = m["definition_json"]
                dm_id_map[m["id"]] = ex.id
                skipped += 1
            else:
                local_id = _map_or_new(m.get("id"))
                session.add(DataModelModel(id=local_id, name=m["name"], version=m["version"], definition_json=m["definition_json"]))
                dm_id_map[m["id"]] = local_id
                imported += 1
        except Exception as exc:
            errors.append(f"data_model {m.get('name')}: {exc}")

    # ── 2. Business calendars ─────────────────────────────────────────────
    for c in bundle.get("business_calendars", []):
        try:
            ex = (await session.execute(
                select(BusinessCalendarModel).where(BusinessCalendarModel.name == c["name"])
            )).scalar_one_or_none()
            if ex:
                ex.timezone = c["timezone"]; ex.work_days = c["work_days"]
                ex.work_start_hour = c["work_start_hour"]; ex.work_end_hour = c["work_end_hour"]
                ex.holidays = c["holidays"]; ex.description = c.get("description", "")
                skipped += 1
            else:
                session.add(BusinessCalendarModel(
                    id=_map_or_new(c.get("id")), name=c["name"], timezone=c["timezone"],
                    work_days=c["work_days"], work_start_hour=c["work_start_hour"],
                    work_end_hour=c["work_end_hour"], holidays=c["holidays"],
                    description=c.get("description", ""),
                ))
                imported += 1
        except Exception as exc:
            errors.append(f"business_calendar {c.get('name')}: {exc}")

    # ── 3. Access roles ────────────────────────────────────────────────────
    for r in bundle.get("access_roles", []):
        try:
            ex = (await session.execute(
                select(AccessRoleModel).where(
                    AccessRoleModel.name == r["name"],
                    AccessRoleModel.tenant_id == target_tenant_id,
                )
            )).scalar_one_or_none()
            if ex:
                ex.description = r.get("description", ""); ex.privileges = r["privileges"]
                skipped += 1
            else:
                session.add(AccessRoleModel(
                    id=_map_or_new(r.get("id")), name=r["name"],
                    description=r.get("description", ""), privileges=r["privileges"],
                    tenant_id=target_tenant_id,
                ))
                imported += 1
        except Exception as exc:
            errors.append(f"access_role {r.get('name')}: {exc}")

    # ── 4. Portals ─────────────────────────────────────────────────────────
    for p in bundle.get("portals", []):
        try:
            ex = (await session.execute(
                select(PortalModel).where(
                    PortalModel.name == p["name"],
                    PortalModel.tenant_id == target_tenant_id,
                )
            )).scalar_one_or_none()
            if ex:
                ex.portal_type = p["portal_type"]; ex.modules = p["modules"]
                ex.homepage = p["homepage"]; ex.theme = p["theme"]
                portal_id_map[p["id"]] = ex.id
                skipped += 1
            else:
                local_id = _map_or_new(p.get("id"))
                session.add(PortalModel(
                    id=local_id, name=p["name"], portal_type=p["portal_type"],
                    modules=p["modules"], homepage=p["homepage"], theme=p["theme"],
                    tenant_id=target_tenant_id, is_active=p.get("is_active", True),
                ))
                portal_id_map[p["id"]] = local_id
                imported += 1
        except Exception as exc:
            errors.append(f"portal {p.get('name')}: {exc}")

    await session.flush()

    # ── 5. Email templates ─────────────────────────────────────────────────
    for t in bundle.get("email_templates", []):
        try:
            ex = (await session.execute(
                select(EmailTemplateModel).where(EmailTemplateModel.name == t["name"])
            )).scalar_one_or_none()
            if ex:
                ex.subject = t["subject"]; ex.body_text = t["body_text"]
                ex.body_html = t.get("body_html"); ex.description = t.get("description", "")
                ex.engine = t.get("engine", "jinja2"); ex.scope = t.get("scope", "global")
                skipped += 1
            else:
                session.add(EmailTemplateModel(
                    id=_map_or_new(t.get("id")), name=t["name"],
                    description=t.get("description", ""), subject=t["subject"],
                    body_text=t["body_text"], body_html=t.get("body_html"),
                    engine=t.get("engine", "jinja2"), scope=t.get("scope", "global"),
                    tenant_id=target_tenant_id, is_active=t.get("is_active", True),
                ))
                imported += 1
        except Exception as exc:
            errors.append(f"email_template {t.get('name')}: {exc}")

    # ── 6. Email accounts (preserve existing credentials) ─────────────────
    for ea in bundle.get("email_accounts", []):
        try:
            ex = (await session.execute(
                select(EmailAccountModel).where(EmailAccountModel.address == ea["address"])
            )).scalar_one_or_none()
            if ex:
                # Update config — NEVER wipe an existing password
                ex.name = ea["name"]; ex.smtp_host = ea["smtp_host"]
                ex.smtp_port = ea["smtp_port"]; ex.smtp_username = ea.get("smtp_username")
                ex.smtp_use_tls = ea["smtp_use_tls"]; ex.imap_host = ea.get("imap_host")
                ex.imap_port = ea.get("imap_port", 993); ex.imap_username = ea.get("imap_username")
                ex.imap_use_ssl = ea.get("imap_use_ssl", True); ex.imap_folder = ea.get("imap_folder", "INBOX")
                ex.poll_interval_seconds = ea.get("poll_interval_seconds", 15)
                # preserve existing passwords — only set if not already stored
                if ea.get("smtp_password") and not ex.smtp_password:
                    ex.smtp_password = ea["smtp_password"]
                if ea.get("imap_password") and not ex.imap_password:
                    ex.imap_password = ea["imap_password"]
                skipped += 1
            else:
                local_id = _map_or_new(ea.get("id"))
                session.add(EmailAccountModel(
                    id=local_id, name=ea["name"], address=ea["address"],
                    smtp_host=ea["smtp_host"], smtp_port=ea.get("smtp_port", 587),
                    smtp_username=ea.get("smtp_username"), smtp_password=None,
                    smtp_use_tls=ea.get("smtp_use_tls", True),
                    imap_host=ea.get("imap_host"), imap_port=ea.get("imap_port", 993),
                    imap_username=ea.get("imap_username"), imap_password=None,
                    imap_use_ssl=ea.get("imap_use_ssl", True),
                    imap_folder=ea.get("imap_folder", "INBOX"),
                    poll_interval_seconds=ea.get("poll_interval_seconds", 15),
                    is_active=ea.get("is_active", True),
                    is_default_outbound=ea.get("is_default_outbound", False),
                    tenant_id=target_tenant_id,
                ))
                imported += 1
        except Exception as exc:
            errors.append(f"email_account {ea.get('address')}: {exc}")

    # ── 7. Connectors (preserve existing credentials) ──────────────────────
    for c in bundle.get("connectors", []):
        try:
            ex = (await session.execute(
                select(ConnectorRegistryModel).where(
                    ConnectorRegistryModel.name == c["name"],
                    ConnectorRegistryModel.tenant_id == target_tenant_id,
                )
            )).scalar_one_or_none()
            if ex:
                ex.connector_type = c["connector_type"]; ex.description = c.get("description")
                ex.config_schema = c.get("config_schema", {}); ex.config = c.get("config", {})
                ex.enabled = c.get("enabled", True)
                # NEVER overwrite existing credentials with empty dict
                if c.get("credentials") and not ex.credentials:
                    ex.credentials = c["credentials"]
                connector_id_map[c["id"]] = ex.id
                skipped += 1
            else:
                local_id = _map_or_new(c.get("id"))
                session.add(ConnectorRegistryModel(
                    id=local_id, name=c["name"], connector_type=c["connector_type"],
                    description=c.get("description"), config_schema=c.get("config_schema", {}),
                    config=c.get("config", {}), credentials={},  # always empty on import
                    tenant_id=target_tenant_id, enabled=c.get("enabled", True),
                ))
                connector_id_map[c["id"]] = local_id
                imported += 1
        except Exception as exc:
            errors.append(f"connector {c.get('name')}: {exc}")

    # ── 8. Process definitions ─────────────────────────────────────────────
    for p in bundle.get("process_definitions", []):
        try:
            ex = (await session.execute(
                select(ProcessDefinitionModel).where(
                    ProcessDefinitionModel.name == p["name"],
                    ProcessDefinitionModel.version == p["version"],
                )
            )).scalar_one_or_none()
            if ex:
                ex.bpmn_xml = p["bpmn_xml"]; ex.description = p.get("description")
                ex.status = p.get("status", "active")
                skipped += 1
            else:
                session.add(ProcessDefinitionModel(
                    id=_map_or_new(p.get("id")), name=p["name"], version=p["version"],
                    description=p.get("description"), bpmn_xml=p["bpmn_xml"],
                    case_type_id=p.get("case_type_id"), status=p.get("status", "active"),
                    tenant_id=target_tenant_id,
                ))
                imported += 1
        except Exception as exc:
            errors.append(f"process_def {p.get('name')}: {exc}")

    # ── 9. Form definitions ────────────────────────────────────────────────
    for f in bundle.get("form_definitions", []):
        try:
            ex = (await session.execute(
                select(FormDefinitionModel).where(
                    FormDefinitionModel.name == f["name"],
                    FormDefinitionModel.version == f["version"],
                )
            )).scalar_one_or_none()
            local_dm_id = dm_id_map.get(f.get("data_model_id", "") or "")
            if ex:
                ex.definition_json = f["definition_json"]
                if local_dm_id:
                    ex.data_model_id = local_dm_id
                skipped += 1
            else:
                session.add(FormDefinitionModel(
                    id=_map_or_new(f.get("id")), name=f["name"], version=f["version"],
                    data_model_id=local_dm_id, definition_json=f["definition_json"],
                ))
                imported += 1
        except Exception as exc:
            errors.append(f"form {f.get('name')}: {exc}")

    # ── 10. Rule definitions ───────────────────────────────────────────────
    for r in bundle.get("rule_definitions", []):
        try:
            ex = (await session.execute(
                select(RuleDefinitionModel).where(
                    RuleDefinitionModel.name == r["name"],
                    RuleDefinitionModel.version == r["version"],
                )
            )).scalar_one_or_none()
            if ex:
                ex.definition_json = r["definition_json"]; ex.enabled = r.get("enabled", True)
                ex.priority = r.get("priority", 0)
                skipped += 1
            else:
                session.add(RuleDefinitionModel(
                    id=_map_or_new(r.get("id")), name=r["name"], version=r["version"],
                    rule_type=r["rule_type"], scope=r.get("scope", "global"),
                    scope_target_id=r.get("scope_target_id"), definition_json=r["definition_json"],
                    enabled=r.get("enabled", True), priority=r.get("priority", 0),
                ))
                imported += 1
        except Exception as exc:
            errors.append(f"rule {r.get('name')}: {exc}")

    await session.flush()

    # ── 11. Case types ─────────────────────────────────────────────────────
    for ct in bundle.get("case_types", []):
        try:
            ex = (await session.execute(
                select(CaseTypeModel).where(
                    CaseTypeModel.name == ct["name"], CaseTypeModel.is_deleted.is_(False),
                )
            )).scalar_one_or_none()
            if ex:
                ex.version = ct["version"]; ex.description = ct.get("description", "")
                ex.icon = ct.get("icon"); ex.color = ct.get("color")
                ex.tags = ct.get("tags", []); ex.default_priority = ct.get("default_priority", "medium")
                ex.portal_enabled = ct.get("portal_enabled", False)
                ex.definition_json = ct["definition_json"]
                ct_id_map[ct["id"]] = ex.id
                skipped += 1
            else:
                local_id = uuid.uuid4()
                session.add(CaseTypeModel(
                    id=local_id, name=ct["name"], version=ct["version"],
                    description=ct.get("description", ""), icon=ct.get("icon"),
                    color=ct.get("color"), tags=ct.get("tags", []),
                    default_priority=ct.get("default_priority", "medium"),
                    portal_enabled=ct.get("portal_enabled", False),
                    definition_json=ct["definition_json"],
                ))
                ct_id_map[ct["id"]] = local_id
                imported += 1
        except Exception as exc:
            errors.append(f"case_type {ct.get('name')}: {exc}")

    await session.flush()

    # ── 12. Stages ─────────────────────────────────────────────────────────
    for s in bundle.get("stages", []):
        try:
            local_ct = ct_id_map.get(s["case_type_id"])
            if not local_ct:
                skipped += 1; continue
            ex = (await session.execute(
                select(CaseTypeStageModel).where(
                    CaseTypeStageModel.case_type_id == local_ct,
                    CaseTypeStageModel.stage_id == s["stage_id"],
                )
            )).scalar_one_or_none()
            if ex:
                ex.name = s["name"]; ex.stage_type = s["stage_type"]
                ex.order = s.get("order", 0); ex.sla_policy_id = s.get("sla_policy_id")
                ex.definition_json = s["definition_json"]
                stage_id_map[s["id"]] = ex.id
                skipped += 1
            else:
                local_id = uuid.uuid4()
                session.add(CaseTypeStageModel(
                    id=local_id, case_type_id=local_ct, stage_id=s["stage_id"],
                    name=s["name"], stage_type=s["stage_type"], order=s.get("order", 0),
                    sla_policy_id=s.get("sla_policy_id"), definition_json=s["definition_json"],
                ))
                stage_id_map[s["id"]] = local_id
                imported += 1
        except Exception as exc:
            errors.append(f"stage {s.get('name')}: {exc}")

    await session.flush()

    # ── 13. Steps ──────────────────────────────────────────────────────────
    for s in bundle.get("steps", []):
        try:
            local_ct = ct_id_map.get(s["case_type_id"])
            local_stage = stage_id_map.get(s["stage_id"])
            if not local_ct or not local_stage:
                skipped += 1; continue
            ex = (await session.execute(
                select(CaseTypeStepModel).where(
                    CaseTypeStepModel.case_type_id == local_ct,
                    CaseTypeStepModel.step_id == s["step_id"],
                )
            )).scalar_one_or_none()
            if ex:
                ex.name = s["name"]; ex.step_type = s["step_type"]
                ex.bpmn_element_id = s["bpmn_element_id"]; ex.definition_json = s["definition_json"]
                skipped += 1
            else:
                session.add(CaseTypeStepModel(
                    id=uuid.uuid4(), case_type_id=local_ct, stage_id=local_stage,
                    step_id=s["step_id"], name=s["name"], step_type=s["step_type"],
                    bpmn_element_id=s["bpmn_element_id"], definition_json=s["definition_json"],
                ))
                imported += 1
        except Exception as exc:
            errors.append(f"step {s.get('name')}: {exc}")

    await session.flush()

    # ── 14. Escalation trees ───────────────────────────────────────────────
    for e in bundle.get("escalation_trees", []):
        try:
            local_ct = ct_id_map.get(e.get("case_type_id") or "")
            ex = (await session.execute(
                select(EscalationTreeModel).where(EscalationTreeModel.name == e["name"])
            )).scalar_one_or_none()
            if ex:
                ex.description = e.get("description", ""); ex.scope = e.get("scope", "global")
                ex.case_type_id = local_ct; ex.tree_json = e["tree_json"]
                ex.is_active = e.get("is_active", True)
                skipped += 1
            else:
                session.add(EscalationTreeModel(
                    id=_map_or_new(e.get("id")), name=e["name"],
                    description=e.get("description", ""), scope=e.get("scope", "global"),
                    case_type_id=local_ct, tenant_id=target_tenant_id,
                    tree_json=e["tree_json"], is_active=e.get("is_active", True),
                ))
                imported += 1
        except Exception as exc:
            errors.append(f"escalation_tree {e.get('name')}: {exc}")

    # ── 15. Webhook subscriptions (preserve existing secret) ───────────────
    for w in bundle.get("webhook_subscriptions", []):
        try:
            local_ct = ct_id_map.get(w.get("case_type_id") or "")
            ex = (await session.execute(
                select(WebhookSubscriptionModel).where(
                    WebhookSubscriptionModel.name == w["name"],
                    WebhookSubscriptionModel.url == w["url"],
                )
            )).scalar_one_or_none()
            if ex:
                ex.events = w["events"]; ex.case_type_id = local_ct
                ex.is_active = w.get("is_active", True); ex.headers = w.get("headers", {})
                ex.retry_count = w.get("retry_count", 3); ex.timeout_seconds = w.get("timeout_seconds", 10)
                # preserve existing secret
                if w.get("secret") and not ex.secret:
                    ex.secret = w["secret"]
                skipped += 1
            else:
                session.add(WebhookSubscriptionModel(
                    id=_map_or_new(w.get("id")), name=w["name"], url=w["url"],
                    secret=None,  # always null on import
                    events=w["events"], case_type_id=local_ct,
                    is_active=w.get("is_active", True), headers=w.get("headers", {}),
                    retry_count=w.get("retry_count", 3), timeout_seconds=w.get("timeout_seconds", 10),
                ))
                imported += 1
        except Exception as exc:
            errors.append(f"webhook {w.get('name')}: {exc}")

    # ── 16. Outbound connector rules ───────────────────────────────────────
    for r in bundle.get("outbound_connector_rules", []):
        try:
            local_ct = ct_id_map.get(r.get("case_type_id") or "")
            local_conn = connector_id_map.get(r.get("connector_id") or "")
            ex = (await session.execute(
                select(OutboundConnectorRuleModel).where(
                    OutboundConnectorRuleModel.name == r["name"],
                    OutboundConnectorRuleModel.tenant_id == target_tenant_id,
                )
            )).scalar_one_or_none()
            if ex:
                ex.trigger_event = r["trigger_event"]; ex.case_type_id = local_ct
                ex.condition_expr = r.get("condition_expr"); ex.connector_id = local_conn or ex.connector_id
                ex.input_mapping = r.get("input_mapping", {}); ex.enabled = r.get("enabled", True)
                skipped += 1
            else:
                session.add(OutboundConnectorRuleModel(
                    id=_map_or_new(r.get("id")), tenant_id=target_tenant_id,
                    name=r["name"], trigger_event=r["trigger_event"],
                    case_type_id=local_ct, condition_expr=r.get("condition_expr"),
                    connector_id=local_conn, input_mapping=r.get("input_mapping", {}),
                    enabled=r.get("enabled", True),
                ))
                imported += 1
        except Exception as exc:
            errors.append(f"outbound_connector_rule {r.get('name')}: {exc}")

    # ── 17. Webhook receiver rules ─────────────────────────────────────────
    for r in bundle.get("webhook_receiver_rules", []):
        try:
            local_conn = connector_id_map.get(r.get("connector_id") or "")
            ex = (await session.execute(
                select(WebhookReceiverRuleModel).where(
                    WebhookReceiverRuleModel.name == r["name"],
                    WebhookReceiverRuleModel.tenant_id == target_tenant_id,
                )
            )).scalar_one_or_none()
            if ex:
                ex.connector_id = local_conn or ex.connector_id
                ex.case_id_field = r.get("case_id_field"); ex.match_case_field = r.get("match_case_field")
                ex.match_payload_field = r.get("match_payload_field")
                ex.field_updates = r.get("field_updates", {}); ex.advance_stage = r.get("advance_stage", False)
                ex.enabled = r.get("enabled", True)
                skipped += 1
            else:
                session.add(WebhookReceiverRuleModel(
                    id=_map_or_new(r.get("id")), tenant_id=target_tenant_id,
                    connector_id=local_conn, name=r["name"],
                    case_id_field=r.get("case_id_field"), match_case_field=r.get("match_case_field"),
                    match_payload_field=r.get("match_payload_field"),
                    field_updates=r.get("field_updates", {}), advance_stage=r.get("advance_stage", False),
                    enabled=r.get("enabled", True),
                ))
                imported += 1
        except Exception as exc:
            errors.append(f"webhook_receiver_rule {r.get('name')}: {exc}")

    # ── 18. Access groups ──────────────────────────────────────────────────
    for g in bundle.get("access_groups", []):
        try:
            local_portal = portal_id_map.get(g.get("portal_id") or "")
            if not local_portal:
                errors.append(f"access_group {g.get('name')}: portal_id not found in bundle/target")
                continue
            ex = (await session.execute(
                select(AccessGroupModel).where(
                    AccessGroupModel.name == g["name"],
                    AccessGroupModel.tenant_id == target_tenant_id,
                )
            )).scalar_one_or_none()
            if ex:
                ex.description = g.get("description", ""); ex.portal_id = local_portal
                ex.role_ids = g.get("role_ids", [])
                ex.allowed_case_type_ids = g.get("allowed_case_type_ids", ["*"])
                ex.allowed_queue_ids = g.get("allowed_queue_ids", ["*"])
                ex.is_default = g.get("is_default", False); ex.is_active = g.get("is_active", True)
                skipped += 1
            else:
                session.add(AccessGroupModel(
                    id=_map_or_new(g.get("id")), name=g["name"],
                    description=g.get("description", ""), tenant_id=target_tenant_id,
                    portal_id=local_portal, role_ids=g.get("role_ids", []),
                    allowed_case_type_ids=g.get("allowed_case_type_ids", ["*"]),
                    allowed_queue_ids=g.get("allowed_queue_ids", ["*"]),
                    is_default=g.get("is_default", False), is_active=g.get("is_active", True),
                ))
                imported += 1
        except Exception as exc:
            errors.append(f"access_group {g.get('name')}: {exc}")

    await session.flush()

    result = {
        "imported": imported,
        "skipped": skipped,
        "errors": errors[:20],
        "needs_configuration": needs_configuration,
    }
    logger.info("bundle v2 applied: %s", {k: v for k, v in result.items() if k != "needs_configuration"})
    return result
