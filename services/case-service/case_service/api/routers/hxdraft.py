"""HxDraft API — conversational component authoring inside HxNexus (P1–P3).

Natural language → validated draft card → human Apply. The draft endpoint only
GENERATES (nothing is created); Apply re-normalizes and re-validates the
submitted draft server-side (the client copy is never trusted) and then calls
the same creation paths a human would use — under the caller's own authority,
never a service token. Rule drafts are simulated via the existing HxReplay
ad-hoc candidate endpoint (no backend here). Modify drafts carry a base
checksum and 409 when the target changed (never a blind overwrite); Stage
creates rules DISABLED / escalation trees INACTIVE behind a reviewer-gated
HxBranch whose merge turns them on.

Design: docs/Future/hxdraft.md (P1 signed off 2026-07-04, P2 §10 + P3 §11
2026-07-05).

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser
from case_service.db import repository as repo
from case_service.db.models import CopilotConversationModel, CopilotMessageModel
from case_service.db.session import get_session
from case_service.hxnexus.guard import scan_input, validate_message_length
from case_service.nlp import (case_type_builder, escalation_builder, form_builder,
                              modify_builder, routing_builder, rule_builder,
                              rule_lint, sla_builder, user_builder)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/hxnexus/draft", tags=["hxdraft"])

_KINDS = ("rule", "case_type", "form", "sla", "user", "escalation")
#: connector drafts are MODIFY-ONLY (creating a connector needs credentials —
#: credentials are never draftable in any direction); routing is MODIFY-ONLY by
#: nature (it surgically re-points one step of an EXISTING case type)
_MODIFY_KINDS = ("rule", "case_type", "form", "connector", "escalation", "routing")


def _checksum(d: dict | None) -> str:
    """Same technique as HxBranch conflict detection — Apply 409s when the target
    changed after the draft was generated (never a blind overwrite)."""
    import hashlib
    import json
    return hashlib.sha256(
        json.dumps(d or {}, sort_keys=True, default=str).encode()).hexdigest()


def _can_apply_rule(user: AuthenticatedUser) -> bool:
    # Signed-off decision: rule Apply = admin + designer. Gated HERE — the /rules
    # endpoint's authenticated-only posture is neither relied on nor widened.
    roles = user.roles or []
    return (user.has_privilege("*", "*") or "admin" in roles
            or "superadmin" in roles or "designer" in roles)


# ── draft generation ────────────────────────────────────────────────────────────

class DraftRequest(BaseModel):
    kind: str                                   # _KINDS, or _MODIFY_KINDS with target_id
    description: str = Field(min_length=1, max_length=4000)
    case_type_id: Optional[str] = None          # rule scope / sla target / escalation scope
    conversation_id: Optional[str] = None       # append the exchange to a chat
    target_id: Optional[str] = None             # modify-existing: the artifact to change


@router.post("")
async def create_draft(
    body: DraftRequest,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    from case_service.api.routers.hxnexus import chat_rate_limiter
    allowed, retry = chat_rate_limiter.is_allowed(str(user.user_id))
    if not allowed:
        raise HTTPException(429, f"Rate limit reached. Try again in {retry}s.",
                            headers={"Retry-After": str(retry)})
    if body.target_id:
        if body.kind not in _MODIFY_KINDS:
            raise HTTPException(400, f"Modify drafts support {list(_MODIFY_KINDS)}")
    elif body.kind not in _KINDS:
        raise HTTPException(400, f"kind must be one of {list(_KINDS)}")
    try:
        validate_message_length(body.description)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    scan = scan_input(body.description)
    if scan.flagged:
        log.warning("hxdraft: flagged draft prompt user=%s signals=%s",
                    user.user_id, scan.signals)

    try:
        if body.target_id:
            result = await _generate_modification(body, session)
        elif body.kind == "rule":
            out = await rule_builder.generate_rule_draft(
                body.description, scope_target_id=body.case_type_id)
            result = {"kind": "rule", "source": "llm", **out}
            await _attach_rule_lint(session, result, body.case_type_id)
        elif body.kind == "form":
            result = {"kind": "form",
                      **await form_builder.generate_form_draft(body.description)}
        elif body.kind == "user":
            result = {"kind": "user",
                      **await user_builder.generate_user_draft(body.description)}
        elif body.kind == "escalation":
            ct_scope = None
            if body.case_type_id:
                ct_scope = str((await _get_case_type_or_404(
                    session, body.case_type_id)).id)
            result = {"kind": "escalation",
                      **await escalation_builder.generate_escalation_draft(
                          body.description, case_type_id=ct_scope)}
        elif body.kind == "sla":
            # SLA policies live inside the case-type definition — the draft is a
            # scoped diff on that case type, carrying a base checksum for Apply.
            if not body.case_type_id:
                raise HTTPException(400, "SLA drafts need a case_type_id")
            ct = await _get_case_type_or_404(session, body.case_type_id)
            out = await sla_builder.generate_sla_draft(
                body.description, ct.definition_json or {})
            out["draft"]["case_type_id"] = str(ct.id)
            out["draft"]["case_type_name"] = ct.name
            out["draft"]["base_checksum"] = _checksum(ct.definition_json)
            existing_policies = [p for p in (ct.definition_json or {})
                                 .get("sla_policies", []) if isinstance(p, dict)]
            out["draft"]["current_policies"] = [
                {"id": p.get("id"), "name": p.get("name")} for p in existing_policies]
            # deterministic modify: an id collision becomes a REPLACE draft — the
            # card shows before/after of that policy instead of erroring.
            pid = (out["draft"].get("policy") or {}).get("id")
            before = next((p for p in existing_policies if p.get("id") == pid), None)
            if before is not None:
                out["draft"]["replaces_policy_id"] = pid
                out["draft"]["before_policy"] = before
                out["errors"] = [e for e in out["errors"]
                                 if "already exists" not in e]
            result = {"kind": "sla", **out}
        else:
            built = await case_type_builder.build_from_description(body.description)
            if built.get("error"):
                raise HTTPException(503, built["error"])
            result = {"kind": "case_type", "source": built.get("source", "llm"),
                      "errors": [],
                      "draft": {"name": built.get("name", "Drafted case type"),
                                "version": "1.0.0",
                                "definition_json": built,
                                "description": built.get("description", "")}}
    except (rule_builder.RuleDraftError, form_builder.FormDraftError,
            sla_builder.SLADraftError, modify_builder.ModifyDraftError,
            user_builder.UserDraftError, escalation_builder.EscalationDraftError,
            routing_builder.RoutingDraftError) as exc:
        raise HTTPException(503 if "unavailable" in str(exc) else 400, str(exc))

    await _append_to_conversation(session, user, body.conversation_id,
                                  body.kind, body.description, result)
    return result


async def _append_to_conversation(session: AsyncSession, user: AuthenticatedUser,
                                  conversation_id: str | None, kind: str,
                                  prompt: str, result: dict[str, Any]) -> None:
    """Drafts ride the existing chat transcript (no new tables). Only the
    conversation's OWNER may append to it."""
    if not conversation_id:
        return
    try:
        cid = uuid.UUID(conversation_id)
    except ValueError:
        return
    conv = (await session.execute(
        select(CopilotConversationModel).where(
            CopilotConversationModel.id == cid,
            CopilotConversationModel.user_id == user.user_id)
    )).scalar_one_or_none()
    if conv is None:
        return
    draft = result.get("draft") or {}
    errors = result.get("errors") or []
    if "policy" in draft:                      # sla drafts nest under "policy"
        draft = {**draft, "name": (draft.get("policy") or {}).get("name")}
    if "username" in draft and "name" not in draft:      # user drafts
        draft = {**draft, "name": draft.get("username")}
    summary = (f"[HxDraft] {kind}: \"{draft.get('name', '?')}\" — "
               + ("VALID draft, awaiting review" if not errors
                  else f"rejected by validation ({len(errors)} issue(s))"))
    session.add(CopilotMessageModel(conversation_id=conv.id, role="user",
                                    content=f"/draft {kind}: {prompt}"))
    session.add(CopilotMessageModel(conversation_id=conv.id, role="assistant",
                                    content=summary))
    await session.commit()


# ── apply (re-validated server-side; user's own authority) ──────────────────────

class ApplyRequest(BaseModel):
    kind: str
    draft: dict = Field(default_factory=dict)


@router.post("/apply", status_code=201)
async def apply_draft(
    body: ApplyRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    if (body.draft or {}).get("mode") == "modify":
        if body.kind == "rule":
            return await _apply_rule_modify(body.draft, background_tasks, session, user)
        if body.kind == "case_type":
            return await _apply_case_type_modify(body.draft, background_tasks,
                                                 session, user)
        if body.kind == "form":
            return await _apply_form_modify(body.draft, session)
        if body.kind == "connector":
            return await _apply_connector_modify(body.draft, background_tasks,
                                                 session, user)
        if body.kind == "escalation":
            return await _apply_escalation_modify(body.draft, session)
        if body.kind == "routing":
            return await _apply_routing(body.draft, background_tasks, session, user)
        raise HTTPException(400, f"Modify apply supports {list(_MODIFY_KINDS)}")
    if body.kind == "rule":
        return await _apply_rule(body.draft, background_tasks, session, user)
    if body.kind == "case_type":
        return await _apply_case_type(body.draft, session, user)
    if body.kind == "form":
        return await _apply_form(body.draft, session)
    if body.kind == "sla":
        return await _apply_sla(body.draft, background_tasks, session, user)
    if body.kind == "user":
        return await _apply_user(body.draft, session, user)
    if body.kind == "escalation":
        return await _apply_escalation(body.draft, session, user)
    raise HTTPException(400, f"kind must be one of {list(_KINDS)}")


async def _apply_escalation(draft: dict, session: AsyncSession,
                            user: AuthenticatedUser):
    """Create the tree via the same logic as POST /escalation-trees, under the
    caller's token. Signed-off P3 decision: endpoint parity — any authenticated
    user (the manual endpoint has no role gate; nothing is relied on or widened)."""
    clean = escalation_builder.normalize_escalation_draft(
        {"name": draft.get("name"), "description": draft.get("description"),
         "levels": (draft.get("tree_json") or {}).get("levels")},
        case_type_id=draft.get("case_type_id"))
    errors = escalation_builder.validate_escalation_draft(clean)
    if errors:
        raise HTTPException(400, "Draft failed validation: " + "; ".join(errors))

    ct_id = None
    if clean["scope"] == "case_type":
        ct_id = (await _get_case_type_or_404(session, clean["case_type_id"])).id

    from case_service.db.models import EscalationTreeModel
    tree = EscalationTreeModel(
        name=clean["name"][:255], description=clean["description"],
        scope=clean["scope"], case_type_id=ct_id,
        tenant_id=user.tenant_id, tree_json=clean["tree_json"],
        is_active=True, created_by=user.user_id,
    )
    session.add(tree)
    await session.commit()
    await session.refresh(tree)
    return {"kind": "escalation", "id": str(tree.id), "name": tree.name}


async def _apply_escalation_modify(draft: dict, session: AsyncSession):
    """Endpoint parity (any authenticated user); tree_json only — name, scope and
    activation are never changed by a modify draft."""
    tree = await _get_tree_or_404(session, draft.get("target_id"))
    if draft.get("base_checksum") != _checksum(tree.tree_json):
        raise HTTPException(409, "Escalation tree changed since this draft was "
                                 "generated — re-draft against the current tree")

    clean = escalation_builder.normalize_escalation_draft(
        {"name": tree.name, "levels": (draft.get("tree_json") or {}).get("levels")})
    errors = escalation_builder.validate_escalation_draft(clean)
    if errors:
        raise HTTPException(400, "Draft failed validation: " + "; ".join(errors))

    tree.tree_json = clean["tree_json"]
    await session.commit()
    return {"kind": "escalation", "mode": "modify", "id": str(tree.id),
            "name": tree.name}


async def _apply_routing(draft: dict, background_tasks: BackgroundTasks,
                         session: AsyncSession, user: AuthenticatedUser):
    """Surgical server-side patch of ONE step's assignment — canonical case-type
    write gate + checksum, then the standard case-type PATCH hooks."""
    ct = await _get_case_type_or_404(session, draft.get("target_id") or "")
    from case_service.api.routers.case_types import _assert_can_write_case_type
    _assert_can_write_case_type(user, ct.tenant_id, action="update")
    if draft.get("base_checksum") != _checksum(ct.definition_json):
        raise HTTPException(409, "Case type changed since this draft was generated "
                                 "— re-draft against the current definition")

    clean = routing_builder.normalize_routing_draft(draft)
    errors = routing_builder.validate_routing_draft(clean, ct.definition_json or {})
    if errors:
        raise HTTPException(400, "Draft failed validation: " + "; ".join(errors))

    ct.definition_json = routing_builder.patch_step_assignment(
        ct.definition_json or {}, clean)
    await session.commit()
    from case_service.api.routers.case_types import _sync_lifecycle_docs
    from case_service.testsuite import regen
    background_tasks.add_task(_sync_lifecycle_docs, ct.id)
    background_tasks.add_task(regen.bg_case_type_changed, ct.id)
    return {"kind": "routing", "mode": "modify", "case_type_id": str(ct.id),
            "stage_id": clean["stage_id"], "step_id": clean["step_id"]}


async def _apply_user(draft: dict, session: AsyncSession, user: AuthenticatedUser):
    """Create a login user — the same posture as the admin-only /auth/register
    path, under the caller's own token. The temp password is generated HERE
    (never by the LLM), returned exactly once, and must be changed on first login."""
    roles = user.roles or []
    if not (user.is_admin or "admin" in roles or "superadmin" in roles
            or user.has_privilege("*", "*")):
        raise HTTPException(403, "Applying a drafted user requires admin")

    clean = user_builder.normalize_user_draft(draft)
    errors = user_builder.validate_user_draft(clean)
    if errors:
        raise HTTPException(400, "Draft failed validation: " + "; ".join(errors))

    from case_service.db.models import HelixUserModel
    existing = (await session.execute(
        select(HelixUserModel).where(
            (HelixUserModel.username == clean["username"])
            | (HelixUserModel.email == clean["email"]))
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(409, "Username or email already in use.")

    import secrets
    from case_service.api.routers.auth_real import _hash_password
    temp_password = secrets.token_urlsafe(12)
    new_user = HelixUserModel(
        username=clean["username"],
        email=clean["email"],
        display_name=clean["display_name"],
        password_hash=_hash_password(temp_password),
        roles=clean["roles"],
        password_change_required=True,
    )
    session.add(new_user)
    await session.commit()
    await session.refresh(new_user)
    log.info("hxdraft: user %r created via draft by %s", clean["username"],
             user.user_id)
    return {"kind": "user", "id": str(new_user.id), "username": new_user.username,
            "roles": new_user.roles, "temp_password": temp_password,
            "password_change_required": True}


async def _attach_rule_lint(session: AsyncSession, result: dict,
                            case_type_id: str | None) -> None:
    """P3: deterministic ADVISORY lint of a valid rule draft against its scoped
    case type — a separate `lint` list, never part of the blocking `errors`."""
    if not case_type_id or result.get("errors"):
        return
    try:
        ct = await repo.get_case_type(session, uuid.UUID(str(case_type_id)))
    except ValueError:
        return
    if ct is None:
        return
    result["lint"] = rule_lint.lint_rule_draft(result.get("draft") or {},
                                               ct.definition_json or {})


async def _get_tree_or_404(session: AsyncSession, tree_id: str | None):
    from case_service.db.models import EscalationTreeModel
    try:
        tid = uuid.UUID(str(tree_id))
    except ValueError:
        raise HTTPException(400, f"Invalid escalation tree id {tree_id!r}")
    tree = await session.get(EscalationTreeModel, tid)
    if tree is None:
        raise HTTPException(404, "Escalation tree not found")
    return tree


# ── modify-existing generation ──────────────────────────────────────────────────

async def _generate_modification(body: DraftRequest, session: AsyncSession) -> dict:
    """Fetch the current artifact, draft a FULL replacement, return a diff card.

    Every result carries mode="modify", the target id and a base checksum; Apply
    re-fetches and 409s if the target changed — never a blind overwrite."""
    from case_service.api.routers.branches import _compute_diff

    if body.kind == "rule":
        rule = await _get_rule_or_404(session, body.target_id)
        if rule.rule_type != "when":
            raise HTTPException(400, "Only WHEN rules can be modified by draft")
        out = await modify_builder.generate_rule_modification(
            body.description, current_name=rule.name,
            current_definition=rule.definition_json or {},
            scope_target_id=rule.scope_target_id)
        base = rule.definition_json or {}
        diff = _compute_diff(base, out["draft"]["definition_json"])
        current_view = {"name": rule.name, "definition_json": base,
                        "enabled": rule.enabled}
        # carry the REAL id so Simulate replays this as a MODIFICATION of the
        # existing rule (HxReplay keys candidates by id), not an addition
        out["draft"]["id"] = str(rule.id)
        await _attach_rule_lint(session, out, rule.scope_target_id)
    elif body.kind == "case_type":
        ct = await _get_case_type_or_404(session, body.target_id)
        out = await modify_builder.generate_case_type_modification(
            body.description, current_definition=ct.definition_json or {})
        base = ct.definition_json or {}
        diff = _compute_diff(base, out["draft"]["definition_json"])
        current_view = {"name": ct.name, "version": ct.version}
        out["draft"]["name"] = ct.name
    elif body.kind == "form":
        form = await _get_form_or_404(session, body.target_id)
        out = await modify_builder.generate_form_modification(
            body.description, current_name=form.name,
            current_definition=form.definition_json or {})
        base = form.definition_json or {}
        diff = _compute_diff({"fields": base.get("fields", [])},
                             {"fields": out["draft"]["definition_json"]["fields"]})
        current_view = {"name": form.name, "version": form.version}
    elif body.kind == "escalation":
        tree = await _get_tree_or_404(session, body.target_id)
        out = await modify_builder.generate_escalation_modification(
            body.description, current_name=tree.name,
            current_tree=tree.tree_json or {})
        base = tree.tree_json or {}
        diff = _compute_diff(base, out["draft"]["tree_json"])
        current_view = {"name": tree.name, "scope": tree.scope,
                        "is_active": tree.is_active}
    elif body.kind == "routing":
        # routing modifies a CASE TYPE: target_id is the case type; the LLM only
        # picks a step + assignment, the server does the surgical patch on Apply
        ct = await _get_case_type_or_404(session, body.target_id)
        out = await routing_builder.generate_routing_draft(
            body.description, ct.definition_json or {})
        base = ct.definition_json or {}
        before = _current_assignment(base, out["draft"])
        diff = _compute_diff({"assignment": before},
                             {"assignment": out["draft"].get("assignment")})
        current_view = {"name": ct.name, "stage_id": out["draft"].get("stage_id"),
                        "step_id": out["draft"].get("step_id")}
    else:  # connector — config only, credentials never enter the prompt
        connector = await _get_connector_or_404(session, body.target_id)
        out = await modify_builder.generate_connector_modification(
            body.description, current_name=connector.name,
            connector_type=connector.connector_type,
            current_config=connector.config or {})
        base = connector.config or {}
        diff = _compute_diff(base, out["draft"]["config"])
        current_view = {"name": connector.name,
                        "connector_type": connector.connector_type}

    out["draft"].update({"mode": "modify", "target_id": str(body.target_id),
                         "base_checksum": _checksum(base)})
    return {"kind": body.kind, "mode": "modify", "source": "llm",
            "current": current_view, "diff": diff, **out}


def _current_assignment(definition: dict, draft: dict) -> Any:
    for stage in (definition or {}).get("stages", []):
        if isinstance(stage, dict) and stage.get("id") == draft.get("stage_id"):
            for step in stage.get("steps", []):
                if isinstance(step, dict) and step.get("id") == draft.get("step_id"):
                    return step.get("assignment")
    return None


async def _get_rule_or_404(session: AsyncSession, rule_id: str | None):
    try:
        rid = uuid.UUID(str(rule_id))
    except ValueError:
        raise HTTPException(400, f"Invalid rule id {rule_id!r}")
    rule = await repo.get_rule(session, rid)
    if rule is None:
        raise HTTPException(404, "Rule not found")
    return rule


async def _get_form_or_404(session: AsyncSession, form_id: str | None):
    from case_service.db.models import FormDefinitionModel
    try:
        fid = uuid.UUID(str(form_id))
    except ValueError:
        raise HTTPException(400, f"Invalid form id {form_id!r}")
    form = await session.get(FormDefinitionModel, fid)
    if form is None:
        raise HTTPException(404, "Form not found")
    return form


async def _get_connector_or_404(session: AsyncSession, connector_id: str | None):
    from case_service.db.models import ConnectorRegistryModel
    try:
        cid = uuid.UUID(str(connector_id))
    except ValueError:
        raise HTTPException(400, f"Invalid connector id {connector_id!r}")
    connector = await session.get(ConnectorRegistryModel, cid)
    if connector is None:
        raise HTTPException(404, "Connector not found")
    return connector


async def _get_case_type_or_404(session: AsyncSession, case_type_id: str):
    try:
        ct_id = uuid.UUID(str(case_type_id))
    except ValueError:
        raise HTTPException(400, f"Invalid case_type_id {case_type_id!r}")
    ct = await repo.get_case_type(session, ct_id)
    if ct is None:
        raise HTTPException(404, "Case type not found")
    return ct


async def _apply_sla(draft: dict, background_tasks: BackgroundTasks,
                     session: AsyncSession, user: AuthenticatedUser):
    """Append the drafted policy to the case type's sla_policies — a scoped
    modify-existing: canonical write gate, base-checksum conflict check, then the
    same background hooks the manual case-type PATCH fires."""
    ct = await _get_case_type_or_404(session, draft.get("case_type_id") or "")
    from case_service.api.routers.case_types import _assert_can_write_case_type
    _assert_can_write_case_type(user, ct.tenant_id, action="update")

    if draft.get("base_checksum") != _checksum(ct.definition_json):
        raise HTTPException(409, "Case type changed since this draft was generated "
                                 "— re-draft against the current definition")

    # Re-normalize + re-validate against the CURRENT definition (client untrusted).
    # A replace draft validates against the definition minus the policy it replaces.
    clean = sla_builder.normalize_sla_draft(draft.get("policy") or {})
    definition = dict(ct.definition_json or {})
    policies = [p for p in definition.get("sla_policies", []) if isinstance(p, dict)]
    replaces = draft.get("replaces_policy_id")
    if replaces:
        if not any(p.get("id") == replaces for p in policies):
            raise HTTPException(409, f"Policy {replaces!r} no longer exists on this "
                                     f"case type — re-draft")
        if clean["id"] != replaces:
            raise HTTPException(400, "A replace draft must keep the policy id")
        policies = [p for p in policies if p.get("id") != replaces]
    errors = sla_builder.validate_sla_draft(
        {"policy": clean}, {**definition, "sla_policies": policies})
    if errors:
        raise HTTPException(400, "Draft failed validation: " + "; ".join(errors))

    definition["sla_policies"] = [*policies, clean]
    ct.definition_json = definition
    await session.commit()

    # exact hook parity with PATCH /case-types
    from case_service.api.routers.case_types import _sync_lifecycle_docs
    from case_service.testsuite import regen
    background_tasks.add_task(_sync_lifecycle_docs, ct.id)
    background_tasks.add_task(regen.bg_case_type_changed, ct.id)
    return {"kind": "sla", "case_type_id": str(ct.id), "policy_id": clean["id"],
            "name": clean["name"]}


async def _assert_rule_name_free(session: AsyncSession, name: str, version: str) -> None:
    """Rules are unique on (name, version) — fail with a clean 409, not a DB 500."""
    from case_service.db.models import RuleDefinitionModel
    existing = (await session.execute(
        select(RuleDefinitionModel).where(RuleDefinitionModel.name == name,
                                          RuleDefinitionModel.version == version)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(409, f"A rule named '{name}' v{version} already exists — "
                                 f"re-draft with a different name or modify the "
                                 f"existing rule instead")


async def _apply_rule(draft: dict, background_tasks: BackgroundTasks,
                      session: AsyncSession, user: AuthenticatedUser):
    if not _can_apply_rule(user):
        raise HTTPException(403, "Applying a drafted rule requires admin or designer")

    # Re-normalize from the submitted draft (client copy untrusted) then re-gate.
    d = draft.get("definition_json") or {}
    clean = rule_builder.normalize_rule_draft(
        {"name": draft.get("name"), "description": draft.get("description"),
         "conditions": d.get("conditions"), "actions": d.get("actions")},
        scope_target_id=draft.get("scope_target_id"))
    errors = rule_builder.validate_rule_draft(clean)
    if errors:
        raise HTTPException(400, "Draft failed validation: " + "; ".join(errors))
    await _assert_rule_name_free(session, clean["name"], clean["version"])

    rule = await repo.create_rule(session, data={
        "name": clean["name"], "version": clean["version"], "rule_type": "when",
        "scope": clean["scope"], "scope_target_id": clean["scope_target_id"],
        # provenance lives inside definition_json (the rule row has no description col)
        "definition_json": {**clean["definition_json"],
                            "description": clean["description"]},
        "enabled": True, "priority": 0,
    })
    await session.commit()
    # mirror the rules router: a rule change can stale AI test scenarios
    from case_service.testsuite import regen
    background_tasks.add_task(regen.bg_scenario_source_changed, clean["scope_target_id"])
    return {"kind": "rule", "id": str(rule.id), "name": rule.name}


async def _apply_case_type(draft: dict, session: AsyncSession, user: AuthenticatedUser):
    # exactly the canonical case-type create authorization (global type)
    from case_service.api.routers.case_types import _assert_can_write_case_type
    _assert_can_write_case_type(user, None, action="create")

    name = str(draft.get("name") or "").strip()[:255]
    version = str(draft.get("version") or "1.0.0").strip()[:50]
    definition = draft.get("definition_json")
    if not name or not isinstance(definition, dict) or not definition.get("stages"):
        raise HTTPException(400, "Draft needs a name and a definition with stages")
    if await repo.get_case_type_by_name(session, name, version):
        raise HTTPException(409, f"Case type '{name}' v{version} already exists")

    ct = await repo.create_case_type(session, data={
        "name": name, "version": version, "tenant_id": None,
        "default_priority": str(definition.get("default_priority") or "medium"),
        "definition_json": definition,
        "description": str(draft.get("description") or "Drafted by HxNexus (HxDraft)")[:500],
        "tags": ["hxnexus-draft"],
    })
    await session.commit()
    return {"kind": "case_type", "id": str(ct.id), "name": ct.name, "version": ct.version}


# ── modify-existing apply (re-fetched, checksum-guarded, re-gated) ──────────────

async def _apply_rule_modify(draft: dict, background_tasks: BackgroundTasks,
                             session: AsyncSession, user: AuthenticatedUser):
    if not _can_apply_rule(user):
        raise HTTPException(403, "Applying a rule modification requires admin or designer")
    rule = await _get_rule_or_404(session, draft.get("target_id"))
    if rule.rule_type != "when":
        raise HTTPException(400, "Only WHEN rules can be modified by draft")
    if draft.get("base_checksum") != _checksum(rule.definition_json):
        raise HTTPException(409, "Rule changed since this draft was generated — "
                                 "re-draft against the current definition")

    d = draft.get("definition_json") or {}
    # name/scope immutable in modify mode — parity with the manual rules PATCH
    clean = rule_builder.normalize_rule_draft(
        {"name": rule.name, "description": d.get("description"),
         "conditions": d.get("conditions"), "actions": d.get("actions")},
        scope_target_id=rule.scope_target_id)
    errors = rule_builder.validate_rule_draft(clean)
    if errors:
        raise HTTPException(400, "Draft failed validation: " + "; ".join(errors))

    await repo.update_rule(session, rule.id, values={
        "definition_json": {**clean["definition_json"],
                            "description": clean["description"]}})
    await session.commit()
    from case_service.testsuite import regen
    background_tasks.add_task(regen.bg_scenario_source_changed, rule.scope_target_id)
    return {"kind": "rule", "mode": "modify", "id": str(rule.id), "name": rule.name}


async def _apply_case_type_modify(draft: dict, background_tasks: BackgroundTasks,
                                  session: AsyncSession, user: AuthenticatedUser):
    ct = await _get_case_type_or_404(session, draft.get("target_id") or "")
    from case_service.api.routers.case_types import _assert_can_write_case_type
    _assert_can_write_case_type(user, ct.tenant_id, action="update")
    if draft.get("base_checksum") != _checksum(ct.definition_json):
        raise HTTPException(409, "Case type changed since this draft was generated "
                                 "— re-draft against the current definition")

    definition = draft.get("definition_json")
    errors = modify_builder.validate_case_type_definition(definition)
    if errors:
        raise HTTPException(400, "Draft failed validation: " + "; ".join(errors))

    ct.definition_json = definition
    await session.commit()
    # exact hook parity with PATCH /case-types
    from case_service.api.routers.case_types import _sync_lifecycle_docs
    from case_service.testsuite import regen
    background_tasks.add_task(_sync_lifecycle_docs, ct.id)
    background_tasks.add_task(regen.bg_case_type_changed, ct.id)
    return {"kind": "case_type", "mode": "modify", "id": str(ct.id), "name": ct.name}


async def _apply_form_modify(draft: dict, session: AsyncSession):
    # parity with PATCH /forms (authenticated)
    form = await _get_form_or_404(session, draft.get("target_id"))
    if draft.get("base_checksum") != _checksum(form.definition_json):
        raise HTTPException(409, "Form changed since this draft was generated — "
                                 "re-draft against the current definition")

    clean = form_builder.normalize_form_draft(
        {"name": form.name,
         "fields": (draft.get("definition_json") or {}).get("fields")})
    errors = form_builder.validate_form_draft(clean)
    if errors:
        raise HTTPException(400, "Draft failed validation: " + "; ".join(errors))

    # replace the fields, preserve every other definition key (layout etc.)
    from datetime import datetime, timezone
    definition = dict(form.definition_json or {})
    definition["fields"] = clean["definition_json"]["fields"]
    form.definition_json = definition
    form.updated_at = datetime.now(timezone.utc)
    await session.commit()
    return {"kind": "form", "mode": "modify", "id": str(form.id), "name": form.name}


async def _apply_connector_modify(draft: dict, background_tasks: BackgroundTasks,
                                  session: AsyncSession, user: AuthenticatedUser):
    # gated in HxDraft (matching the rotate-credentials posture) — the PUT
    # endpoint's authenticated-only posture is neither relied on nor widened
    if not (user.is_admin or "integration" in (user.roles or [])
            or "admin" in (user.roles or []) or "superadmin" in (user.roles or [])
            or user.has_privilege("*", "*")):
        raise HTTPException(403, "Applying a connector config change requires "
                                 "admin or the integration role")
    connector = await _get_connector_or_404(session, draft.get("target_id"))
    if draft.get("base_checksum") != _checksum(connector.config):
        raise HTTPException(409, "Connector changed since this draft was generated "
                                 "— re-draft against the current config")

    config = draft.get("config")
    errors = modify_builder.validate_connector_config(config)
    if errors:
        raise HTTPException(400, "Draft failed validation: " + "; ".join(errors))

    # config ONLY — name/enabled/credentials are never touched by a draft
    from datetime import datetime, timezone
    connector.config = config
    connector.updated_at = datetime.now(timezone.utc)
    await session.commit()
    from case_service.testsuite import regen
    background_tasks.add_task(regen.bg_scenario_source_changed, None)
    return {"kind": "connector", "mode": "modify", "id": str(connector.id),
            "name": connector.name}


# ── stage in HxBranch (rules): disabled-create + branch + merge-enables ─────────

class StageRequest(BaseModel):
    kind: str
    draft: dict = Field(default_factory=dict)


@router.post("/stage", status_code=201)
async def stage_draft(
    body: StageRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Stage a drafted rule (P2) or escalation tree (P3) — created DISABLED /
    INACTIVE plus an HxBranch whose only diff is turning it on. Signed-off
    decisions: ANY authenticated user may stage — nothing can go live without a
    reviewer's approval (SOD: reviewer ≠ owner), so this is the propose→review
    path for non-admins."""
    # any-auth endpoint that writes two rows — shares the chat rate limit
    from case_service.api.routers.hxnexus import chat_rate_limiter
    allowed, retry = chat_rate_limiter.is_allowed(str(user.user_id))
    if not allowed:
        raise HTTPException(429, f"Rate limit reached. Try again in {retry}s.",
                            headers={"Retry-After": str(retry)})
    if body.kind == "escalation":
        return await _stage_escalation(body.draft, session, user)
    if body.kind != "rule":
        raise HTTPException(400, "Only rule and escalation drafts can be staged "
                                 "in HxBranch")

    # Same tamper posture as Apply: the client copy is never trusted.
    d = body.draft.get("definition_json") or {}
    clean = rule_builder.normalize_rule_draft(
        {"name": body.draft.get("name"), "description": body.draft.get("description"),
         "conditions": d.get("conditions"), "actions": d.get("actions")},
        scope_target_id=body.draft.get("scope_target_id"))
    errors = rule_builder.validate_rule_draft(clean)
    if errors:
        raise HTTPException(400, "Draft failed validation: " + "; ".join(errors))
    await _assert_rule_name_free(session, clean["name"], clean["version"])

    rule = await repo.create_rule(session, data={
        "name": clean["name"], "version": clean["version"], "rule_type": "when",
        "scope": clean["scope"], "scope_target_id": clean["scope_target_id"],
        "definition_json": {**clean["definition_json"],
                            "description": clean["description"]},
        "enabled": False, "priority": 0,   # disabled until the branch merges
    })
    await session.flush()

    # The branch diff IS the enabling (base: disabled → content: enabled),
    # snapshots in the exact shape branches._get_local_artifact renders rules.
    snapshot = {"id": str(rule.id), "name": rule.name, "version": rule.version,
                "rule_type": rule.rule_type, "definition_json": rule.definition_json,
                "enabled": False}
    from case_service.api.routers.branches import _log_event
    from case_service.db.models import ArtifactBranchModel
    ag = user.active_access_group
    branch = ArtifactBranchModel(
        name=f"hxdraft/{clean['name']}"[:200],
        description=clean["description"],
        branch_type="artifact", artifact_type="rule", artifact_id=str(rule.id),
        source_env_name="dev (local)", status="open",
        base_snapshot=snapshot,
        content_snapshot={**snapshot, "enabled": True},
        created_by=user.username, owner_id=user.user_id,
        access_group_id=uuid.UUID(ag.id) if ag and ag.id else None,
    )
    session.add(branch)
    await session.flush()
    await _log_event(session, branch.id, "branch_created", user, {
        "branch_name": branch.name, "artifact_type": "rule",
        "artifact_id": str(rule.id), "via": "hxdraft",
    })
    await session.commit()
    # parity with the manual rules endpoints: a new rule row stales AI scenarios
    from case_service.testsuite import regen
    background_tasks.add_task(regen.bg_scenario_source_changed, clean["scope_target_id"])
    return {"kind": "rule", "rule_id": str(rule.id), "rule_name": rule.name,
            "branch_id": str(branch.id), "branch_name": branch.name,
            "enabled": False}


async def _stage_escalation(draft: dict, session: AsyncSession,
                            user: AuthenticatedUser):
    """P3: create the tree INACTIVE + an HxBranch whose only diff is activation —
    HxBranch's existing escalation merge (tree_json + is_active) does the rest."""
    clean = escalation_builder.normalize_escalation_draft(
        {"name": draft.get("name"), "description": draft.get("description"),
         "levels": (draft.get("tree_json") or {}).get("levels")},
        case_type_id=draft.get("case_type_id"))
    errors = escalation_builder.validate_escalation_draft(clean)
    if errors:
        raise HTTPException(400, "Draft failed validation: " + "; ".join(errors))

    ct_id = None
    if clean["scope"] == "case_type":
        ct_id = (await _get_case_type_or_404(session, clean["case_type_id"])).id

    from case_service.db.models import ArtifactBranchModel, EscalationTreeModel
    tree = EscalationTreeModel(
        name=clean["name"][:255], description=clean["description"],
        scope=clean["scope"], case_type_id=ct_id,
        tenant_id=user.tenant_id, tree_json=clean["tree_json"],
        is_active=False, created_by=user.user_id,   # inactive until the merge
    )
    session.add(tree)
    await session.flush()

    # snapshots in the exact shape branches._get_local_artifact renders escalations
    snapshot = {"id": str(tree.id), "name": tree.name,
                "description": tree.description, "scope": tree.scope,
                "tree_json": tree.tree_json, "is_active": False}
    from case_service.api.routers.branches import _log_event
    ag = user.active_access_group
    branch = ArtifactBranchModel(
        name=f"hxdraft/{clean['name']}"[:200],
        description=clean["description"],
        branch_type="artifact", artifact_type="escalation",
        artifact_id=str(tree.id),
        source_env_name="dev (local)", status="open",
        base_snapshot=snapshot,
        content_snapshot={**snapshot, "is_active": True},
        created_by=user.username, owner_id=user.user_id,
        access_group_id=uuid.UUID(ag.id) if ag and ag.id else None,
    )
    session.add(branch)
    await session.flush()
    await _log_event(session, branch.id, "branch_created", user, {
        "branch_name": branch.name, "artifact_type": "escalation",
        "artifact_id": str(tree.id), "via": "hxdraft",
    })
    await session.commit()
    return {"kind": "escalation", "tree_id": str(tree.id), "tree_name": tree.name,
            "branch_id": str(branch.id), "branch_name": branch.name,
            "is_active": False}


async def _apply_form(draft: dict, session: AsyncSession):
    clean = form_builder.normalize_form_draft(
        {"name": draft.get("name"),
         "fields": (draft.get("definition_json") or {}).get("fields")})
    # keep the generation-time description (it already carries provenance)
    desc = str((draft.get("definition_json") or {}).get("description") or "")[:800]
    if desc:
        clean["definition_json"]["description"] = desc
    errors = form_builder.validate_form_draft(clean)
    if errors:
        raise HTTPException(400, "Draft failed validation: " + "; ".join(errors))
    form = await repo.create_form(session, data={
        "name": clean["name"], "version": clean["version"],
        "definition_json": clean["definition_json"],
    })
    await session.commit()
    return {"kind": "form", "id": str(form.id), "name": form.name}
