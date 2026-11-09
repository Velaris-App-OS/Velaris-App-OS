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

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import (
    GUID,
    AccessGroupModel,
    BotDefinitionModel,
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
    ScheduleModel,
    WebhookReceiverRuleModel,
    WebhookSubscriptionModel,
)

logger = logging.getLogger(__name__)

BUNDLE_SCHEMA_VERSION = "3"   # v3 adds bot definitions and schedules
SUPPORTED_BUNDLE_SCHEMA_VERSIONS = {"1", "2", "3"}   # what apply_bundle can interpret (#14)

#: The fields that make a bot what it is — identical to the editor's
#: `EDITABLE_FIELDS`, deliberately: an import that changes any of them is an
#: edit, and an edit returns a bot to draft.
BOT_FIELDS = (
    "name", "description", "image", "digest", "declared_command", "declared_env",
    "credential_refs", "input_schema", "output_schema", "granted_domains",
    "timeout_seconds", "max_concurrency", "resource_profile",
    "fanout_input_key", "fanout_max",
)

#: What a schedule is, minus everything that describes a *run*.
SCHEDULE_FIELDS = ("name", "description", "cron", "timezone", "action_kind", "action_config")

_NEEDS_CREDS = "__NEEDS_CONFIGURATION__"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _s(v: Any) -> str | None:
    return str(v) if v is not None else None


# ── Selection ─────────────────────────────────────────────────────────────────

#: Every section a selection may name. A key outside this set is a typo, and a
#: typo that silently selected nothing would be indistinguishable from an empty
#: environment — so it is refused.
SELECTABLE_SECTIONS = (
    "data_models", "business_calendars", "access_roles", "portals",
    "email_templates", "email_accounts", "connectors", "process_definitions",
    "form_definitions", "rule_definitions", "case_types", "escalation_trees",
    "webhook_subscriptions", "outbound_connector_rules", "webhook_receiver_rules",
    "access_groups", "bot_definitions", "schedules",
)


def normalise_selection(selection: dict | None) -> dict:
    """Validate and canonicalise a selection.

    Shape: ``{"sections": {"case_types": ["<id>", ...] | None, ...}}``

    Per section: **absent or null = everything**, a list = exactly those ids, and
    ``[]`` = none of them. "Absent means everything" is deliberate — it is what
    the Promote screen has always promised ("leave blank to include everything"),
    and it keeps a package built before a new section existed meaning the same
    thing after it does.
    """
    selection = dict(selection or {})
    sections = selection.get("sections") or {}
    if not isinstance(sections, dict):
        raise ValueError("selection.sections must be an object")

    unknown = sorted(set(sections) - set(SELECTABLE_SECTIONS))
    if unknown:
        raise ValueError(
            f"Unknown selection section(s): {', '.join(unknown)}. "
            f"Valid sections: {', '.join(SELECTABLE_SECTIONS)}"
        )

    clean: dict[str, list[str] | None] = {}
    for name, ids in sections.items():
        if ids is None:
            clean[name] = None
            continue
        if not isinstance(ids, (list, tuple)):
            raise ValueError(f"selection.sections.{name} must be a list of ids or null")
        clean[name] = [str(i) for i in ids]

    out = {"sections": clean}
    if selection.get("delta_since"):
        out["delta_since"] = selection["delta_since"]
    # Recorded for provenance only: SLA policies live inside a case type's
    # `definition_json.sla_policies`, so they travel with the case type and there
    # is no section to include or exclude.
    for passthrough in ("include_sla", "notes", "source_manifest"):
        if passthrough in selection:
            out[passthrough] = selection[passthrough]
    return out


def _as_id(value: str) -> Any:
    """Ids arrive as strings; the columns are GUIDs. Anything unparseable is
    passed through so the IN clause simply matches nothing rather than raising."""
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return value


# ── Bundle builder ────────────────────────────────────────────────────────────

async def build_bundle(
    session: AsyncSession,
    tenant_id: str,
    case_type_ids: list[uuid.UUID] | None = None,
    since: datetime | None = None,
    selection: dict | None = None,
) -> dict:
    """Serialise the design-time environment for tenant_id.

    Args:
        case_type_ids: legacy shorthand for
                       ``selection["sections"]["case_types"]``. Ignored when the
                       selection already names that section.
        since: delta mode — only artifacts updated at or after this datetime.
        selection: see `normalise_selection`. None = the whole environment.

    Two properties this function must keep:

    * **It is tenant-scoped.** It used to take `tenant_id` and use it for the log
      line only — every one of the sixteen selects ran unfiltered, so a bundle
      built for one tenant carried another tenant's connectors, portals and case
      types, and `apply_bundle` then wrote them into the target tenant. Eleven of
      these models carry `tenant_id` and all eleven are now filtered. The rest
      have no tenant column at all (data models, forms, rules, calendars,
      webhook subscriptions, stages, steps) — those are platform-wide by schema,
      and stages/steps are reached only through tenant-filtered case types.
    * **The selection is honoured.** It used to be advisory at best: only
      `case_type_ids` was ever read, and the promotion path did not pass even
      that.

    Returns:
        bundle dict — ready to POST to /api/v1/deploy/import
    """
    sel = normalise_selection(selection)
    sections = dict(sel["sections"])
    if case_type_ids and "case_types" not in sections:
        sections["case_types"] = [str(c) for c in case_type_ids]

    is_delta = since is not None
    needs_configuration: list[dict] = []
    auto_included: dict[str, int] = {}

    # Tenant columns are not one shape: `case_types.tenant_id` is a GUID while
    # every other tenant-scoped model here stores a String, and a caller arrives
    # holding a slug. Resolving once gives both forms; comparing the raw token
    # would either match nothing or raise on read.
    from case_service.tenancy.identity import resolve_tenant
    resolved = await resolve_tenant(session, tenant_id)
    tenant_uuid = resolved[0] if resolved else None
    tenant_names = {str(tenant_id)}
    if resolved:
        tenant_names |= {resolved[1], str(resolved[0])}

    def _delta(q, model):
        """Apply updated_at filter when in delta mode and model has that column."""
        if is_delta and hasattr(model, "updated_at"):
            q = q.where(model.updated_at >= since)
        return q

    def _scope(q, model):
        """Tenant filter, for the models that have one.

        A NULL tenant column stays visible, exactly as `tenancy.same_tenant`
        treats it: these rows predate multi-tenancy and most of a real
        deployment's design-time artifacts still carry NULL. Excluding them here
        would empty every bundle, which is a worse regression than the leak this
        filter closes.
        """
        col = getattr(model, "tenant_id", None)
        if col is None:
            return q
        column_type = model.__table__.c.tenant_id.type
        if isinstance(column_type, GUID):
            candidates = [tenant_uuid] if tenant_uuid is not None else []
        else:
            candidates = sorted(tenant_names)
        if not candidates:
            return q.where(col.is_(None))
        return q.where(or_(col.is_(None), col.in_(candidates)))

    async def _collect(section: str, model, q=None, delta: bool = True) -> list:
        """One section, tenant-scoped, selection-honouring."""
        ids = sections.get(section, None)
        if ids is not None and not ids:
            return []                      # explicitly none
        q = select(model) if q is None else q
        q = _scope(q, model)
        if ids:
            q = q.where(model.id.in_([_as_id(i) for i in ids]))
        if delta:
            q = _delta(q, model)
        return list((await session.execute(q)).scalars().all())

    async def _pull_parents(section: str, model, wanted: set, have: list) -> list:
        """Add parents a selected child needs but the selection left out.

        A dependency of something you chose is part of what you chose. The
        alternative — dropping the child — turns a partial selection into an
        import that half-applies, and `apply_bundle` builds its FK maps from
        exactly these rows.
        """
        missing = {str(w) for w in wanted if w} - {str(h.id) for h in have}
        if not missing:
            return have
        q = _scope(select(model).where(model.id.in_([_as_id(m) for m in missing])), model)
        extra = list((await session.execute(q)).scalars().all())
        if extra:
            auto_included[section] = auto_included.get(section, 0) + len(extra)
        return have + extra

    # ── 1-16. Collect ─────────────────────────────────────────────────────
    data_models   = await _collect("data_models", DataModelModel, delta=False)
    calendars     = await _collect("business_calendars", BusinessCalendarModel, delta=False)
    roles         = await _collect("access_roles", AccessRoleModel, delta=False)
    portals       = await _collect("portals", PortalModel, delta=False)
    email_templates = await _collect("email_templates", EmailTemplateModel)
    email_accounts_raw = await _collect("email_accounts", EmailAccountModel)
    connectors_raw  = await _collect("connectors", ConnectorRegistryModel, delta=False)
    process_defs  = await _collect("process_definitions", ProcessDefinitionModel)
    forms         = await _collect("form_definitions", FormDefinitionModel)
    rules         = await _collect("rule_definitions", RuleDefinitionModel)

    case_types = await _collect(
        "case_types", CaseTypeModel,
        q=select(CaseTypeModel).where(CaseTypeModel.is_deleted.is_(False)).order_by(CaseTypeModel.name),
    )
    ct_ids = [ct.id for ct in case_types]

    # Stages and steps are not selectable: they belong to their case type and
    # travel with it. They carry no tenant column and are reached only through
    # the tenant-filtered case types above.
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

    escalations   = await _collect("escalation_trees", EscalationTreeModel)
    webhooks_raw  = await _collect("webhook_subscriptions", WebhookSubscriptionModel)
    ocr           = await _collect("outbound_connector_rules", OutboundConnectorRuleModel)
    receiver_rules = await _collect("webhook_receiver_rules", WebhookReceiverRuleModel, delta=False)
    access_groups = await _collect("access_groups", AccessGroupModel)

    # Automation. Both travel as *design*: what the bot is and when the schedule
    # fires — never the approval that made either runnable, and never a
    # credential value.
    bots      = await _collect("bot_definitions", BotDefinitionModel)
    schedules = await _collect("schedules", ScheduleModel)

    # ── Dependency closure ────────────────────────────────────────────────
    # FK parents only, and only the ones `apply_bundle` maps: a child whose
    # parent is missing imports with a dangling reference.
    data_models = await _pull_parents(
        "data_models", DataModelModel, {f.data_model_id for f in forms}, data_models)
    portals = await _pull_parents(
        "portals", PortalModel, {g.portal_id for g in access_groups}, portals)
    roles = await _pull_parents(
        "access_roles", AccessRoleModel,
        {r for g in access_groups for r in (g.role_ids or [])}, roles)
    connectors_raw = await _pull_parents(
        "connectors", ConnectorRegistryModel,
        {r.connector_id for r in ocr} | {r.connector_id for r in receiver_rules},
        connectors_raw)
    bots = await _pull_parents(
        "bot_definitions", BotDefinitionModel,
        {(sc.action_config or {}).get("bot_id") for sc in schedules
         if sc.action_kind == "bot"},
        bots)

    # ── Serialise the sections that strip secrets ─────────────────────────
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

    bot_entries = []
    for b in bots:
        entry = {f: getattr(b, f) for f in BOT_FIELDS}
        entry["id"] = _s(b.id)
        bot_entries.append(entry)
        if b.credential_refs:
            # The names travel so the target admin knows what to create; the
            # values never do. They live in `bot_credentials`, encrypted under
            # the source tenant's DEK, and are meaningless anywhere else.
            needs_configuration.append({
                "type": "bot_credential", "id": _s(b.id), "name": b.name,
                "missing": [str(r) for r in b.credential_refs],
            })

    schedule_entries = []
    for sc in schedules:
        entry = {f: getattr(sc, f) for f in SCHEDULE_FIELDS}
        entry["id"] = _s(sc.id)
        schedule_entries.append(entry)

    # ── Assemble ──────────────────────────────────────────────────────────
    # ── Assemble ──────────────────────────────────────────────────────────
    bundle: dict = {
        "bundle_schema_version": BUNDLE_SCHEMA_VERSION,
        "created_at": _utcnow_iso(),
        "source_tenant_id": tenant_id,
        "is_delta": is_delta,
        "delta_since": since.isoformat() if since else None,
        "selection": sel,
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
            "bot_definitions":    len(bot_entries),
            "schedules":          len(schedule_entries),
            "needs_configuration": len(needs_configuration),
            "auto_included":      auto_included,
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
        "bot_definitions": bot_entries,
        "schedules": schedule_entries,
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
        "%d templates, %d needs-configuration items (tenant %s, scoped)",
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
    selection: dict | None = None,
) -> dict:
    """Build a delta bundle containing only artifacts changed since env's last deploy.

    Falls back to a full bundle if the environment has never been deployed to.

    Delta is a **build-time** choice, not a push-time one: a sealed package is
    already fixed bytes, so the promotion path resolves the delta once, when the
    package is built, and never recomputes it against the target afterwards.
    """
    from velaris_shared.db.models import EnvironmentRegistryModel
    env = await session.get(EnvironmentRegistryModel, env_id)
    since: datetime | None = getattr(env, "last_deployed_at", None) if env else None
    return await build_bundle(session, tenant_id, case_type_ids, since=since,
                              selection=selection)


# ── Bundle applier ────────────────────────────────────────────────────────────

async def _current_case_type(session: AsyncSession, same_name: list,
                             tenant: str) -> Any:
    """The row an immediate import replaces, when a name has several versions.

    Before item 18a this was `scalar_one_or_none()`, which raised as soon as a
    second version of a name existed — the case type was then reported as an
    error and never updated. Now: the newest row, followed through any closed
    rollout to the version new cases actually get.
    """
    if not same_name:
        return None
    if len(same_name) == 1:
        return same_name[0]
    from case_service.rollouts import service as _rollouts
    res = await _rollouts.resolve(session, case_type_id=same_name[0].id, tenant=tenant,
                                  subject=None)
    return next((r for r in same_name if r.id == res.case_type_id), same_name[0])


async def _rollout_open(session: AsyncSession, name: str, tenant: str) -> bool:
    from case_service.rollouts import service as _rollouts
    if not await _rollouts._table_ready(session):
        return False
    tenant_uuid = await _rollouts._tenant_uuid(session, tenant)
    if tenant_uuid is None:
        return False
    return await _rollouts.open_for(session, tenant_uuid, "case_type", name) is not None


async def apply_bundle(
    session: AsyncSession,
    bundle: dict,
    target_tenant_id: str,
    imported_by: str = "hxdeploy-import",
    alongside: frozenset[str] | set[str] = frozenset(),
) -> dict:
    """Apply a received bundle to this Velaris instance.

    `alongside` (Oct item 18a) names case types to install as a NEW version
    beside the one already here, rather than over it — the candidate of a staged
    rollout. Empty (the default) is the behaviour every earlier release had.

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
    # Schema-skew guard (#14): a bundle built by a newer platform (e.g. a
    # future ReBAC-era format) must fail loudly at the receiving end, not
    # import permission objects this version cannot correctly interpret.
    # Missing field = legacy v1 bundle (grandfathered).
    declared = str(bundle.get("bundle_schema_version", "1"))
    if declared not in SUPPORTED_BUNDLE_SCHEMA_VERSIONS:
        raise ValueError(
            f"This environment supports bundle_schema_version "
            f"{sorted(SUPPORTED_BUNDLE_SCHEMA_VERSIONS)}; the bundle declares "
            f"{declared!r}. Upgrade this environment before promoting to it."
        )

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

    # 8i / 8e: whose rows this import may replace (case types, and since 8e
    # data models, calendars, forms and rules). Before, `same_name` matched
    # every tenant's rows, so one tenant's import overwrote another tenant's
    # case type — or a default, for everybody. The default tenant (and a
    # tenant-less target) keeps exactly the old reach over the defaults; any
    # other tenant replaces only its own rows, and a default it imports over
    # becomes its fork.
    from case_service.case_types import ownership as _ownership
    from velaris_shared.tenancy.identity import (
        resolve_tenant as _resolve_owner, same_tenant_as_default as _is_default_tenant)
    _owner_row = await _resolve_owner(session, target_tenant_id) if target_tenant_id else None
    _owner_id = _owner_row[0] if _owner_row else None
    _owner_is_platform = _owner_id is None or await _is_default_tenant(session, target_tenant_id)

    async def _owned_match(model, **ident):
        """The row this import replaces, for `model` (8e). The platform owner
        reaches defaults and its own rows, as before; any other tenant only
        its own — and a default it imports over becomes its fork."""
        stmt = select(model)
        for col, val in ident.items():
            stmt = stmt.where(getattr(model, col) == val)
        if _owner_is_platform:
            rows = (await session.execute(stmt.where(or_(
                model.tenant_id.is_(None), model.tenant_id == _owner_id)))).scalars().all()
            return next((r for r in rows if r.tenant_id is not None), None) or \
                next(iter(rows), None)
        own = (await session.execute(stmt.where(model.tenant_id == _owner_id))).scalars().first()
        if own is not None:
            return own
        default = (await session.execute(stmt.where(model.tenant_id.is_(None)))).scalars().first()
        if default is None:
            return None
        from velaris_shared.tenancy import inherit as _inherit
        return await _inherit.fork(session, model, default, _owner_id)

    _new_owner = None if _owner_is_platform else _owner_id

    # ── helpers ──────────────────────────────────────────────────────────
    def _uuid(s: str | None) -> uuid.UUID | None:
        try:
            return uuid.UUID(s) if s else None
        except Exception:
            return None

    async def _claim_id(model, source_id: str | None) -> uuid.UUID:
        """Keep the source id when it is FREE here; otherwise allocate a new one.

        Reusing the source id is what lets a promoted artifact keep its identity
        across environments, and it is right whenever the id is unused in the
        target. It is WRONG when the target already holds that id under a
        different natural key — which is not exotic: every Velaris install runs
        the same migrations, so the platform-seeded rows (`admin`, `staff`,
        `viewer` access roles among them) carry the SAME fixed ids everywhere,
        with a NULL tenant that no tenant-scoped lookup will ever match.

        Found by the F-04 twin, which is a freshly migrated environment and so
        hits this on the first import. It was never twin-specific: promoting
        into any freshly seeded environment raised a primary-key violation, and
        because the failure poisoned the session, the very next `select` in the
        loop re-raised it during autoflush — so **one colliding row aborted the
        whole bundle**, and the per-item `except` that looks like isolation gave
        none.
        """
        wanted = _uuid(source_id)
        if wanted is None:
            return uuid.uuid4()
        if await session.get(model, wanted) is not None:
            return uuid.uuid4()
        return wanted

    # ── 1. Data models ───────────────────────────────────────────────────
    for m in bundle.get("data_models", []):
        try:
            ex = await _owned_match(DataModelModel, name=m["name"], version=m["version"])
            if ex:
                ex.definition_json = m["definition_json"]
                dm_id_map[m["id"]] = ex.id
                skipped += 1
            else:
                local_id = await _claim_id(DataModelModel, m.get("id"))
                session.add(DataModelModel(id=local_id, tenant_id=_new_owner, name=m["name"], version=m["version"], definition_json=m["definition_json"]))
                dm_id_map[m["id"]] = local_id
                imported += 1
        except Exception as exc:
            errors.append(f"data_model {m.get('name')}: {exc}")

    # ── 2. Business calendars ─────────────────────────────────────────────
    for c in bundle.get("business_calendars", []):
        try:
            ex = await _owned_match(BusinessCalendarModel, name=c["name"])
            if ex:
                ex.timezone = c["timezone"]; ex.work_days = c["work_days"]
                ex.work_start_hour = c["work_start_hour"]; ex.work_end_hour = c["work_end_hour"]
                ex.holidays = c["holidays"]; ex.description = c.get("description", "")
                skipped += 1
            else:
                session.add(BusinessCalendarModel(
                    id=await _claim_id(BusinessCalendarModel, c.get("id")), tenant_id=_new_owner,
                    name=c["name"], timezone=c["timezone"],
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
                    id=await _claim_id(AccessRoleModel, r.get("id")), name=r["name"],
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
                local_id = await _claim_id(PortalModel, p.get("id"))
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
                    id=await _claim_id(EmailTemplateModel, t.get("id")), name=t["name"],
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
                local_id = await _claim_id(EmailAccountModel, ea.get("id"))
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
            # The same checks as the connector API (2026-11-04): a package can
            # no more plant an internal target or a reserved key than a person.
            from velaris_shared.hxbridge.security import check_connector_config
            await check_connector_config(session, c.get("connector_type", ""), target_tenant_id,
                                         c.get("config", {}))
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
                local_id = await _claim_id(ConnectorRegistryModel, c.get("id"))
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
        # Oct 8b: an unsafe scriptTask is refused here as at upload.
        from case_service.hxfusion.script_guard import check_bpmn
        refused = check_bpmn(p.get("bpmn_xml") or "")
        if refused:
            errors.append(f"process_def {p.get('name')}: unsafe script refused: "
                          + "; ".join(f"{r['node_id']}: {r['reason']}" for r in refused))
            continue
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
                    id=await _claim_id(ProcessDefinitionModel, p.get("id")), name=p["name"], version=p["version"],
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
            # Embeds from approved sites only, as an ordinary save (2026-11-04).
            from velaris_shared.forms.embed import form_embed_problem
            problem = await form_embed_problem(session, _new_owner, f.get("definition_json") or {})
            if problem:
                raise ValueError(problem)
            ex = await _owned_match(FormDefinitionModel, name=f["name"], version=f["version"])
            local_dm_id = dm_id_map.get(f.get("data_model_id", "") or "")
            if ex:
                ex.definition_json = f["definition_json"]
                if local_dm_id:
                    ex.data_model_id = local_dm_id
                skipped += 1
            else:
                session.add(FormDefinitionModel(
                    id=await _claim_id(FormDefinitionModel, f.get("id")), tenant_id=_new_owner,
                    name=f["name"], version=f["version"],
                    data_model_id=local_dm_id, definition_json=f["definition_json"],
                ))
                imported += 1
        except Exception as exc:
            errors.append(f"form {f.get('name')}: {exc}")

    # ── 10. Rule definitions ───────────────────────────────────────────────
    for r in bundle.get("rule_definitions", []):
        try:
            ex = await _owned_match(RuleDefinitionModel, name=r["name"], version=r["version"])
            if ex:
                ex.definition_json = r["definition_json"]; ex.enabled = r.get("enabled", True)
                ex.priority = r.get("priority", 0)
                skipped += 1
            else:
                session.add(RuleDefinitionModel(
                    id=await _claim_id(RuleDefinitionModel, r.get("id")), tenant_id=_new_owner,
                    name=r["name"], version=r["version"],
                    rule_type=r["rule_type"], scope=r.get("scope", "global"),
                    scope_target_id=r.get("scope_target_id"), definition_json=r["definition_json"],
                    enabled=r.get("enabled", True), priority=r.get("priority", 0),
                ))
                imported += 1
        except Exception as exc:
            errors.append(f"rule {r.get('name')}: {exc}")

    await session.flush()

    # ── 11. Case types ─────────────────────────────────────────────────────
    installed_alongside: dict[str, str] = {}
    for ct in bundle.get("case_types", []):
        try:
            same_name = list((await session.execute(
                select(CaseTypeModel).where(
                    CaseTypeModel.name == ct["name"], CaseTypeModel.is_deleted.is_(False),
                    or_(CaseTypeModel.tenant_id.is_(None), CaseTypeModel.tenant_id == _owner_id),
                ).order_by(CaseTypeModel.created_at.desc())
            )).scalars().all())
            if not _owner_is_platform and ct["name"] not in alongside:
                # A staged candidate may sit beside a default baseline (18a),
                # so only a REPLACING import is narrowed to the tenant's rows.
                own = [r for r in same_name if r.tenant_id is not None]
                if not own and same_name:
                    base = next((r for r in same_name if r.version == ct["version"]), None) \
                        or await _current_case_type(session, same_name, target_tenant_id)
                    own = [await _ownership.fork(session, base, _owner_id)]
                same_name = own
            if ct["name"] in alongside and same_name:
                # A staged candidate: a NEW row beside the baseline, never over it.
                if any(r.version == ct["version"] for r in same_name):
                    errors.append(
                        f"case_type {ct['name']}: version {ct['version']} is already "
                        f"installed here; a staged rollout needs a new version number")
                    continue
                local_id = await _claim_id(CaseTypeModel, ct.get("id"))
                # Limitation 13: the candidate belongs to the tenant that staged
                # it. A NULL tenant would put a not-yet-approved version in every
                # tenant's case type list.
                from case_service.tenancy.identity import resolve_tenant as _rt
                _owner = await _rt(session, target_tenant_id)
                session.add(CaseTypeModel(
                    id=local_id, name=ct["name"], version=ct["version"],
                    tenant_id=_owner[0] if _owner else None,
                    description=ct.get("description", ""), icon=ct.get("icon"),
                    color=ct.get("color"), tags=ct.get("tags", []),
                    default_priority=ct.get("default_priority", "medium"),
                    portal_enabled=ct.get("portal_enabled", False),
                    definition_json=ct["definition_json"],
                ))
                ct_id_map[ct["id"]] = local_id
                installed_alongside[ct["name"]] = str(local_id)
                imported += 1
                continue
            # The same version, when it is here, is the one being re-imported;
            # otherwise the current one. (Writing the bundle's version onto a
            # DIFFERENT row would collide with the row that already has it.)
            ex = next((r for r in same_name if r.version == ct["version"]), None) \
                or await _current_case_type(session, same_name, target_tenant_id)
            if ex is not None and await _rollout_open(session, ct["name"], target_tenant_id):
                # Overwriting the baseline in place under a running rollout
                # would change what the "baseline" arm means half way through.
                errors.append(
                    f"case_type {ct['name']}: a staged rollout is open for it here; "
                    f"promote or roll it back before replacing it")
                ct_id_map[ct["id"]] = ex.id
                continue
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
                    tenant_id=None if _owner_is_platform else _owner_id,
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
                    id=await _claim_id(EscalationTreeModel, e.get("id")), name=e["name"],
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
                    id=await _claim_id(WebhookSubscriptionModel, w.get("id")), name=w["name"], url=w["url"],
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
                    id=await _claim_id(OutboundConnectorRuleModel, r.get("id")), tenant_id=target_tenant_id,
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
                    id=await _claim_id(WebhookReceiverRuleModel, r.get("id")), tenant_id=target_tenant_id,
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
                    id=await _claim_id(AccessGroupModel, g.get("id")), name=g["name"],
                    description=g.get("description", ""), tenant_id=target_tenant_id,
                    portal_id=local_portal, role_ids=g.get("role_ids", []),
                    allowed_case_type_ids=g.get("allowed_case_type_ids", ["*"]),
                    allowed_queue_ids=g.get("allowed_queue_ids", ["*"]),
                    is_default=g.get("is_default", False), is_active=g.get("is_active", True),
                ))
                imported += 1
        except Exception as exc:
            errors.append(f"access_group {g.get('name')}: {exc}")

    # ── 19. Bot definitions ────────────────────────────────────────────────
    #
    # An imported bot is **never runnable**. It lands as `draft` with no
    # approval, and somebody in THIS environment has to approve it — the import
    # key is a machine credential, and a machine credential must not be able to
    # start a container here just because a container was approved somewhere
    # else. There is no override flag, because the override is the bypass.
    bot_id_map: dict[str, uuid.UUID] = {}
    for b in bundle.get("bot_definitions", []):
        name = b.get("name")
        try:
            # The TARGET's policy, not the source's: dev may allow a registry
            # production does not, and the refusal belongs at import rather than
            # at the first run, when nobody is watching.
            from velaris_shared.apps import runtime as _mkt_runtime
            try:
                _mkt_runtime.check_registry_allowed(str(b.get("image") or ""))
            except Exception as exc:
                errors.append(f"bot {name}: {exc}")
                continue

            ex = (await session.execute(
                select(BotDefinitionModel).where(
                    BotDefinitionModel.name == name,
                    BotDefinitionModel.tenant_id == target_tenant_id,
                )
            )).scalar_one_or_none()

            # The definition's own rules, checked where the input arrives rather
            # than only at approval: an unpinned digest, or the
            # credentials-plus-wildcard-egress combination, must be refused by
            # the environment receiving it, not discovered by whoever tries to
            # approve it later.
            from case_service.bots import service as _bot_service
            probe = BotDefinitionModel(
                tenant_id=target_tenant_id, created_by=imported_by, status="draft",
                **{f: b[f] for f in BOT_FIELDS if f in b},
            )
            try:
                _bot_service.validate_definition(probe)
            except Exception as exc:
                errors.append(f"bot {name}: {exc}")
                continue

            if ex:
                changed = [f for f in BOT_FIELDS
                           if f in b and getattr(ex, f) != b[f]]
                if not changed:
                    # A no-op import must not de-approve a live bot, exactly as a
                    # no-op save does not. Re-promoting an unchanged package is
                    # normal and must be harmless.
                    bot_id_map[str(b.get("id"))] = ex.id
                    skipped += 1
                    continue
                was_active = ex.status == "active"
                for f in changed:
                    setattr(ex, f, b[f])
                # Same rule as `PUT /bots/{id}`: an edit is an edit. The image,
                # the egress allowlist or the credential set may have moved, and
                # that is precisely the review the approval gate exists to force.
                ex.status = "draft"
                ex.approved_by = None
                ex.approved_at = None
                ex.last_edited_by = imported_by
                bot_id_map[str(b.get("id"))] = ex.id
                imported += 1
                if was_active:
                    needs_configuration.append({
                        "type": "bot_definition", "id": str(ex.id), "name": name,
                        "missing": ["approval"],
                        "detail": f"changed by import ({', '.join(changed)}) — returned to draft",
                    })
            else:
                local_id = await _claim_id(BotDefinitionModel, b.get("id"))
                row = BotDefinitionModel(
                    id=local_id, tenant_id=target_tenant_id,
                    created_by=imported_by, last_edited_by=imported_by,
                    status="draft", approved_by=None, approved_at=None,
                    **{f: b[f] for f in BOT_FIELDS if f in b},
                )
                session.add(row)
                bot_id_map[str(b.get("id"))] = local_id
                imported += 1
                needs_configuration.append({
                    "type": "bot_definition", "id": str(local_id), "name": name,
                    "missing": ["approval"],
                    "detail": "imported as draft — approve it here before it can run",
                })
        except Exception as exc:
            errors.append(f"bot {name}: {exc}")

    await session.flush()

    # ── 20. Schedules ──────────────────────────────────────────────────────
    #
    # Draft, and with no `next_run_at`: nothing imported fires until this
    # environment approves it. References inside `action_config` are remapped
    # through the maps built above, and the whole action is then validated
    # against THIS environment — a schedule that cannot work here is refused
    # with a reason rather than stored to fail at 2am.
    for sc in bundle.get("schedules", []):
        name = sc.get("name")
        try:
            cfg = dict(sc.get("action_config") or {})
            for key, id_map in (("bot_id", bot_id_map),
                                ("connector_id", connector_id_map),
                                ("case_type_id", ct_id_map)):
                if cfg.get(key) and str(cfg[key]) in id_map:
                    cfg[key] = str(id_map[str(cfg[key])])

            incoming = {f: sc[f] for f in SCHEDULE_FIELDS if f in sc}
            incoming["action_config"] = cfg

            # Validated BEFORE anything is written, against THIS environment: a
            # schedule whose bot, connector or case type does not exist here
            # cannot work, and storing it would leave a row nobody can approve
            # and nothing can run. The probe row is never added to the session.
            from case_service.scheduler import service as _sched
            probe = ScheduleModel(tenant_id=target_tenant_id, created_by=imported_by,
                                  status="draft", **incoming)
            try:
                await _sched.validate_schedule(session, probe)
            except Exception as exc:
                errors.append(f"schedule {name}: {exc}")
                continue

            ex = (await session.execute(
                select(ScheduleModel).where(
                    ScheduleModel.name == name,
                    ScheduleModel.tenant_id == target_tenant_id,
                )
            )).scalar_one_or_none()

            if ex:
                changed = [f for f, v in incoming.items() if getattr(ex, f) != v]
                if not changed:
                    skipped += 1
                    continue
                for f in changed:
                    setattr(ex, f, incoming[f])
                ex.status = "draft"
                ex.approved_by = None
                ex.approved_at = None
                ex.last_edited_by = imported_by
                ex.next_run_at = None
                local_id = ex.id
            else:
                row = ScheduleModel(
                    id=await _claim_id(ScheduleModel, sc.get("id")), tenant_id=target_tenant_id,
                    created_by=imported_by, last_edited_by=imported_by,
                    status="draft", approved_by=None, approved_at=None,
                    next_run_at=None, **incoming,
                )
                session.add(row)
                local_id = row.id
            imported += 1

            needs_configuration.append({
                "type": "schedule", "id": str(local_id), "name": name,
                "missing": ["approval"],
                "detail": "imported as draft — it fires nothing until approved here",
            })
        except Exception as exc:
            errors.append(f"schedule {name}: {exc}")

    await session.flush()

    result = {
        "imported": imported,
        "skipped": skipped,
        "errors": errors[:20],
        "needs_configuration": needs_configuration,
    }
    if installed_alongside:
        result["installed_alongside"] = installed_alongside
    logger.info("bundle applied: %s", {k: v for k, v in result.items() if k != "needs_configuration"})
    return result
