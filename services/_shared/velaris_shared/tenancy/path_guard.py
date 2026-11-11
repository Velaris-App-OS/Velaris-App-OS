"""One tenant check for every route keyed by a tenant-owned id — 8d, 2026-09-29.

A probe that seeded one row owned by tenant `acme` in every table and called
every id-keyed route as tenant `evil`'s admin found ~110 routes that answered:
evil could read acme's hxwork boards and queues, run acme's connectors and sync
destinations, delete acme's SSO providers and audit-stream targets, anonymize
acme's users under "GDPR", and PATCH or permanently DELETE the tenant `acme`
itself. Each route had a privilege gate; none asked whose row it was handed.

Fixing ~110 handlers one by one would leave the next route to be written the
same way. So this is ONE app-wide dependency and ONE table: for each path
parameter it knows, it loads the row the id names, follows the row to its
owning tenant (directly, or through its parent — a story through its board),
and answers **404** when the caller's tenant is not that one. 404 because a
row in another tenant must be indistinguishable from a missing one, as it is
everywhere else on the platform.

What it deliberately leaves alone:

* **A missing row** — the handler's own 404 (or create path) is more specific.
* **A NULL tenant** — a platform-shared row, visible to all, exactly as
  `same_tenant` has always treated it.
* **A request with no staff identity** — the public receivers and the customer
  portal authenticate differently; their own dependencies decide.
* **superadmin** — break-glass, the one role that crosses tenants
  (`is_superadmin`).

`tests/phase_authz/test_tenant_path_guard.py` re-runs the probe, so a new
id-keyed route on a tenant-owned table that this table does not cover fails
the suite rather than shipping.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import Depends, HTTPException
from starlette.requests import HTTPConnection
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db import models as M
from velaris_shared.db.session import get_session

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Owned:
    """A row whose owner is its own `tenant_id`, or its parent's."""
    model: Any
    #: Follow this foreign key to the parent and ask the parent instead.
    via: tuple[str, "Owned"] | None = None
    #: Look the id up by this column instead of the primary key.
    key: str | None = None
    #: A platform-shared row (no tenant) of this kind may be READ by every
    #: tenant but CHANGED only by the platform operator. Without it, any tenant's
    #: admin rewrote the operator's platform-wide SSO provider — its endpoints
    #: included, i.e. the sign-in of everyone using it — and revoked its SCIM
    #: token (found on a clean box, R5e step 10, 2026-10-22). Per rule, not
    #: guard-wide: a tenant user legitimately writes against shared rows
    #: elsewhere (a case opened from a shared case type).
    shared_write_operator_only: bool = False
    #: A row with no tenant is the DEFAULT tenant's, not shared (2026-11-04).
    #: For connectors: the operator's carry the operator's credentials, and every
    #: tenant — a website demo tenant included — could run them.
    null_is_default: bool = False


CASE_TYPE = Owned(M.CaseTypeModel)
BOARD = Owned(M.HxWorkBoardModel, null_is_default=True)
DEST = Owned(M.SyncDestinationModel, null_is_default=True)
ACCESS_GROUP = Owned(M.AccessGroupModel)
CASE = Owned(M.CaseInstanceModel)
TENANT = object()     # the id IS a tenant (uuid or slug)
USER = object()       # the id is a user; their tenant is their directory row's

#: (path regex, parameter) → how to find the owner. Checked in order; the first
#: regex that matches the route's PATH TEMPLATE decides for that parameter.
RULES: list[tuple[str, str, Any]] = [
    # Tenants themselves. A tenant's admin runs their own tenant; only the
    # platform operator reaches another.
    (r"^/api/v1/tenants/", "tenant_id", TENANT),
    (r"^/api/v1/tenants/", "slug", TENANT),
    (r"^/api/v1/portal-admin/tenants/", "slug", TENANT),
    # Configuration keyed by its own id.
    (r"^/api/v1/access-groups/", "group_id", ACCESS_GROUP),
    (r"^/api/v1/marketplace/access-rules/", "access_group_id", ACCESS_GROUP),
    # A built-in role (no tenant) is in every tenant's list and every tenant's
    # groups may hold it: a tenant admin rewrote or deleted it for all tenants
    # until 2026-10-30. Read by all, changed by the platform operator only.
    (r"^/api/v1/access-roles/", "role_id", Owned(M.AccessRoleModel, shared_write_operator_only=True)),
    (r"^/api/v1/(admin/)?queues/", "queue_id", Owned(M.WorkQueueModel, shared_write_operator_only=True)),
    (r"^/api/v1/variables/case-types/", "variable_id",
     Owned(M.CaseTypeVariableModel, via=("case_type_id", CASE_TYPE), shared_write_operator_only=True)),
    (r"^/api/v1/audit-stream/", "target_id", Owned(M.AuditStreamTargetModel, null_is_default=True)),
    (r"^/api/v1/auth/", "provider_id", Owned(M.SsoProviderModel, shared_write_operator_only=True)),
    # R5e step 10 (2026-10-21): two identity routes no rule covered. Another
    # tenant's admin revoked acme's SCIM token, and made or replaced acme's SAML
    # SP key (`/{tenant}/sp-key`; `/{tenant}/test` ran acme's provisioning
    # decision). The SAML routes an IdP or a signing-in browser call carry no
    # staff identity, which this guard leaves alone.
    (r"^/api/v1/scim/tokens/", "token_id", Owned(M.ScimTokenModel, shared_write_operator_only=True)),
    (r"^/api/v1/auth/saml/", "tenant", TENANT),
    (r"^/api/v1/channels/endpoints/", "endpoint_id", Owned(M.ChannelEndpointModel, null_is_default=True)),
    (r"^/api/v1/compliance/reports/", "report_id", Owned(M.ComplianceReportModel, null_is_default=True)),
    (r"^/api/v1/devconn/connectors/inbound/", "rule_id", Owned(M.WebhookReceiverRuleModel, null_is_default=True)),
    (r"^/api/v1/devconn/connectors/outbound/", "rule_id", Owned(M.OutboundConnectorRuleModel, null_is_default=True)),
    (r"^/api/v1/escalation-trees/", "tree_id", Owned(M.EscalationTreeModel, shared_write_operator_only=True)),
    (r"^/api/v1/fusion/definitions/", "defn_id", Owned(M.ProcessDefinitionModel, null_is_default=True)),
    (r"^/api/v1/fusion/instances/", "instance_id", Owned(M.ProcessInstanceModel, null_is_default=True)),
    (r"^/api/v1/global/sovereignty-rules/", "rule_id", Owned(M.SovereigntyRuleModel, shared_write_operator_only=True)),
    (r"^/api/v1/global/tenant-assignments/", "assignment_id", Owned(M.TenantRegionAssignmentModel, null_is_default=True)),
    (r"^/api/v1/hxbridge/", "connector_id", Owned(M.ConnectorRegistryModel, null_is_default=True)),
    (r"^/api/v1/hxwork/", "board_id", BOARD),
    (r"^/api/v1/hxwork/", "story_id", Owned(M.HxWorkStoryModel, via=("board_id", BOARD))),
    (r"^/api/v1/hxwork/", "sprint_id", Owned(M.HxWorkSprintModel, null_is_default=True)),
    (r"^/api/v1/hxwork/story-relations/", "relation_id",
     Owned(M.HxWorkStoryRelationModel, via=("board_id", BOARD))),
    (r"^/api/v1/hxwork/relations/", "relation_id",
     Owned(M.HxWorkCardRelationModel, via=("board_id", BOARD))),
    (r"^/api/v1/identity/provisioning/group-mappings/", "mapping_id",
     Owned(M.IdentityGroupMappingModel, shared_write_operator_only=True)),
    (r"^/api/v1/intake/events/", "event_id", Owned(M.IntakeEventModel, via=("case_type_id", CASE_TYPE), null_is_default=True)),
    (r"^/api/v1/marketplace/workspaces/", "workspace_id", Owned(M.MarketplaceWorkspaceModel, null_is_default=True)),
    (r"^/api/v1/marketplace/updates/", "update_id", Owned(M.MarketplaceUpdateModel, null_is_default=True)),
    # A system-wide portal (no tenant) is every tenant's: the same rule (2026-10-30).
    (r"^/api/v1/portals/", "portal_id", Owned(M.PortalModel, shared_write_operator_only=True)),
    (r"^/api/v1/shield/incidents/", "incident_id", Owned(M.SecurityIncidentModel, null_is_default=True)),
    (r"^/api/v1/shield/rules/", "rule_id", Owned(M.SecurityRuleModel, shared_write_operator_only=True)),
    (r"^/api/v1/sync/", "dest_id", DEST),
    (r"^/api/v1/sync/runs/", "run_id", Owned(M.SyncRunModel, via=("destination_id", DEST))),
    (r"^/api/v1/sync/field-mappings/", "mapping_id",
     Owned(M.SyncFieldMappingModel, via=("destination_id", DEST))),
    (r"^/api/v1/sync/redaction-rules/", "rule_id",
     Owned(M.SyncRedactionRuleModel, via=("destination_id", DEST))),
    (r"^/api/v1/testsuite/runs/", "run_id", Owned(M.TestRunModel, null_is_default=True)),
    (r"^/api/v1/engage/", "interaction_id", Owned(M.CustomerInteractionModel, null_is_default=True)),
    (r"^/api/v1/engage/", "customer_id", Owned(M.CustomerProfileModel, null_is_default=True)),
    (r"^/api/v1/engage/join-links/", "join_id", Owned(M.CustomerInteractionJoinModel, null_is_default=True)),
    (r"^/api/v1/engage/backfill/", "proposal_id", Owned(M.CustomerBackfillProposalModel, null_is_default=True)),
    (r"^/api/v1/rollouts/", "rollout_id", Owned(M.ContentRolloutModel, null_is_default=True)),
    (r"^/api/v1/legal-holds/", "hold_id", Owned(M.LegalHoldModel, null_is_default=True)),
    # 8e (2026-10-01): tenant records that had no owner until migration 213.
    (r"^/api/v1/branches/", "branch_id", Owned(M.ArtifactBranchModel, null_is_default=True)),
    (r"^/api/v1/commits/", "commit_id", Owned(M.ComponentCommitModel, null_is_default=True)),
    (r"^/api/v1/apps/packages/", "package_id", Owned(M.AppPackageModel, null_is_default=True)),
    (r"^/api/v1/apps/packages/", "other_id", Owned(M.AppPackageModel, null_is_default=True)),
    (r"^/api/v1/enterprise/gdpr/requests/", "request_id", Owned(M.GDPRRequestModel, null_is_default=True)),
    # 8e part a: design content — a default (NULL) passes, a tenant's is its own.
    (r"^/api/v1/forms/", "form_id", Owned(M.FormDefinitionModel)),
    # Form Builder v2 step 3: a platform table is read by all, changed by the operator only.
    (r"^/api/v1/reference-tables/", "table_id", Owned(M.ReferenceTableModel, shared_write_operator_only=True)),
    (r"^/api/v1/form-sections/", "section_id", Owned(M.FormSectionModel, shared_write_operator_only=True)),
    (r"^/api/v1/rules/", "rule_id", Owned(M.RuleDefinitionModel)),
    (r"^/api/v1/data-models/", "model_id", Owned(M.DataModelModel)),
    (r"^/api/v1/admin/calendars/", "calendar_id", Owned(M.BusinessCalendarModel)),
    (r"^/api/v1/webhooks/(?!\{connector_id\}/receive)", "webhook_id",
     Owned(M.WebhookSubscriptionModel, via=("case_type_id", CASE_TYPE), null_is_default=True)),
    # Generic names LAST: the first matching rule decides a parameter, so a
    # route-specific meaning of `assignment_id` (a region assignment) must be
    # seen before the general one (a case assignment).
    (r".", "user_id", USER),
    (r".", "case_type_id", CASE_TYPE),
    (r".", "assignment_id", Owned(M.CaseAssignmentModel, via=("case_id", CASE))),
]

_COMPILED = [(re.compile(rx), param, spec) for rx, param, spec in RULES]

#: Ids named in the JSON BODY or the QUERY STRING (2026-09-29, the second pass
#: of 8d). The path rules above cannot see `{"case_type_id": ...}` on a create,
#: so a caller could create work inside another tenant's case type, attach
#: another tenant's connector, or simply write `"tenant_id": "<theirs>"`. Top
#: level keys only; a list of ids is checked id by id. `destination_tenant`
#: (a tenant transfer names the other tenant by design) is deliberately absent.
FIELD_RULES: dict[str, Any] = {
    "tenant_id": TENANT, "tenant": TENANT,
    "case_type_id": CASE_TYPE, "case_type_ids": CASE_TYPE,
    "allowed_case_type_ids": CASE_TYPE,
    "case_id": CASE, "case_ids": CASE, "parent_case_id": CASE,
    "connector_id": Owned(M.ConnectorRegistryModel, null_is_default=True),
    "access_group_id": ACCESS_GROUP, "access_group_ids": ACCESS_GROUP,
    "portal_id": Owned(M.PortalModel),
    # 2026-10-30: an access group's roles and members. A tenant admin could put
    # another tenant's role in their group, or another tenant's user in it.
    "role_ids": Owned(M.AccessRoleModel), "operator_id": USER,
    "allowed_queue_ids": Owned(M.WorkQueueModel), "queue_id": Owned(M.WorkQueueModel),
    "user_id": USER, "manager_user_id": USER, "delegate_user_id": USER,
    "assign_to_user_id": USER, "attendee_user_ids": USER,
    "board_id": BOARD, "sprint_id": Owned(M.HxWorkSprintModel, null_is_default=True),
    "conversation_id": Owned(M.ChannelConversationModel),
    "interaction_id": Owned(M.CustomerInteractionModel, null_is_default=True),
    "env_id": Owned(M.EnvironmentRegistryModel),
    "to_env_id": Owned(M.EnvironmentRegistryModel),
    "from_env_id": Owned(M.EnvironmentRegistryModel),
    "destination_id": DEST, "definition_id": Owned(M.ProcessDefinitionModel, null_is_default=True),
    "session_id": Owned(M.CaseSessionModel),
    "target_case_id": CASE,
    # Oct item 4 pass: engage bodies that name a second PERSON.
    "to_customer_id": Owned(M.CustomerProfileModel, null_is_default=True),
    "winner_id": Owned(M.CustomerProfileModel, null_is_default=True),
    "loser_id": Owned(M.CustomerProfileModel, null_is_default=True),
}

#: Routes whose own, TESTED contract is stronger than a flat refusal, so the
#: guard defers to them for that key:
#: * email mailboxes/templates COERCE a body `tenant_id` to the caller's
#:   ("created in the caller's tenant whatever the body says") — a refusal would
#:   break existing clients that echo the field back;
#: * bulk case actions check EACH case and report a foreign one as that item's
#:   404 — refusing the whole batch would hide which cases were applied.
FIELD_EXEMPT: set[tuple[str, str]] = {
    ("/api/v1/email/accounts", "tenant_id"),
    ("/api/v1/email/accounts/{account_id}", "tenant_id"),
    ("/api/v1/email/templates", "tenant_id"),
    ("/api/v1/email/templates/{tpl_id}", "tenant_id"),
    ("/api/v1/cases/bulk", "case_ids"),
    ("/api/v1/cases/bulk/stage", "case_ids"),
    ("/api/v1/cases/bulk/stage/preview", "case_ids"),
}


def rules_for(path_template: str) -> dict[str, Any]:
    """Which parameters of this route the guard checks, and how."""
    out: dict[str, Any] = {}
    for rx, param, spec in _COMPILED:
        if param not in out and "{" + param in path_template and rx.search(path_template):
            out[param] = spec
    return out


def _coerce(model, column: str, raw: str):
    col = model.__table__.columns[column]
    if type(col.type).__name__ == "GUID":
        try:
            return uuid.UUID(str(raw))
        except ValueError:
            return None
    return raw


async def _owner(session: AsyncSession, spec: Owned, raw: Any, depth: int = 0):
    """(found, tenant) — tenant None means platform-shared."""
    if raw is None or depth > 3:
        return False, None
    key = spec.key or next(iter(spec.model.__table__.primary_key.columns)).name
    value = _coerce(spec.model, key, raw)
    if value is None:
        return False, None
    row = (await session.execute(
        select(spec.model).where(getattr(spec.model, key) == value).limit(1)
    )).scalars().first()
    if row is None:
        return False, None
    if spec.via is not None:
        fk, parent = spec.via
        parent_id = getattr(row, fk, None)
        if parent_id is None:
            return True, None
        found, tenant = await _owner(session, parent, parent_id, depth + 1)
        return True, tenant if found else None
    return True, getattr(row, "tenant_id", None)


async def _user_tenants(session: AsyncSession, user_id: str) -> list:
    """Every tenant this user belongs to: their directory row's, and each
    membership's. A user in two tenants is reachable from either."""
    from velaris_shared.tenancy import home
    from velaris_shared.tenancy.identity import DEFAULT_TENANT

    out = []
    # Every name the person goes by (account id, username, email): a row made
    # by a tenant admin's register is keyed by the account id, and a lookup by
    # username alone found nothing and let anyone through (2026-11-09).
    idents = await home.account_identifiers(session, str(user_id))
    rows = await home.directory_rows(session, idents)
    # A NULL tenant on a user is the platform's default tenant — the rule
    # everywhere else ("no tenant claim = default"). Treating it as "no
    # owner" let another tenant's admin edit, or adopt, platform users.
    out += [r.tenant_id or DEFAULT_TENANT for r in rows]
    if not rows and len(idents) > 1:
        # An ACCOUNT with no directory row is the default tenant's too. Before
        # 2026-11-09 it was nobody's, so any tenant's admin could create its
        # row in their own tenant — adopting, say, an operator account made
        # through the API — and then manage it.
        out.append(DEFAULT_TENANT)
    out += list((await session.execute(
        select(M.TenantMembershipModel.tenant_id).where(
            M.TenantMembershipModel.user_id.in_(idents)))).scalars().all())
    return out


async def _json_body(request: HTTPConnection):
    """The JSON body, or None. Starlette caches it, so the route still reads it."""
    from starlette.requests import Request

    if not isinstance(request, Request) or request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return None
    if "json" not in (request.headers.get("content-type") or ""):
        return None
    try:
        return await request.json()
    except Exception:                                           # noqa: BLE001
        return None       # malformed: the route's own validation answers


async def _caller(request: HTTPConnection):
    """The staff caller, or None when this request carries no staff identity."""
    from fastapi.security import HTTPAuthorizationCredentials
    from velaris_shared.auth.dependencies import get_current_user, is_auth_check_busy

    header = request.headers.get("authorization") or ""
    if not header.lower().startswith("bearer "):
        return None
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=header[7:].strip())
    try:
        return await get_current_user(request, creds)
    except HTTPException as exc:
        # A busy database must not skip this guard: the route's own sign-in
        # check could pass a moment later, with nothing having checked the ids.
        if is_auth_check_busy(exc):
            raise
        return None       # the route's own authentication answers


async def tenant_path_guard(request: HTTPConnection,
                            session: AsyncSession = Depends(get_session)) -> None:
    route = request.scope.get("route")
    template = getattr(route, "path", None)
    if not template:
        return
    rules = rules_for(template)
    checks: list[tuple[str, Any, Any]] = []
    for param, spec in rules.items():
        checks.append((param, spec, request.path_params.get(param)))
    for key, value in request.query_params.multi_items():
        if key in FIELD_RULES and (template, key) not in FIELD_EXEMPT:
            checks.append((key, FIELD_RULES[key], value))
    body = await _json_body(request)
    if isinstance(body, dict):
        for key, spec in FIELD_RULES.items():
            if key in body and (template, key) not in FIELD_EXEMPT:
                values = body[key] if isinstance(body[key], list) else [body[key]]
                for v in values[:500]:
                    checks.append((key, spec, v))
    checks = [c for c in checks if not (
        c[2] is None or (isinstance(c[2], str) and not c[2].strip())
        or isinstance(c[2], (dict, list, bool)))]
    if not checks:
        return        # nothing named — no identity lookup on the hot path
    user = await _caller(request)
    if user is None:
        return

    await refuse_foreign_ids(session, user, checks, where=f"{request.method} {template}",
                             write=request.method not in ("GET", "HEAD", "OPTIONS"))


def _null_is_default(spec) -> bool:
    """A child row owned through its parent (`via`) follows the parent's rule."""
    if getattr(spec, "null_is_default", False):
        return True
    via = getattr(spec, "via", None)
    return bool(via) and _null_is_default(via[1])


async def refuse_foreign_ids(session: AsyncSession, user, checks, *, where: str = "",
                             write: bool = False) -> None:
    """Raise 404 if any (name, rule, id) names a row of another tenant.

    Shared by the HTTP guard and the MCP server, whose tools call handler
    functions directly and so never pass through an app dependency. `write`:
    the request changes something (not GET / HEAD / OPTIONS) — it decides the
    `shared_write_operator_only` rules.
    """
    from velaris_shared.tenancy.identity import is_superadmin, same_tenant

    if is_superadmin(user):
        return
    token = getattr(user, "tenant_id", None)

    for param, spec, raw in checks:
        raw = str(raw)
        if spec is USER:
            owners = await _user_tenants(session, raw)
            if owners and not any([await same_tenant(session, o, token) for o in owners]):
                log.info("tenant_path_guard: %s refused (user of another tenant)", where)
                raise HTTPException(404, "Not found")
            continue
        if spec is TENANT:
            # The tenant registry is the install's: its operator (superadmin,
            # or the platform's own default tenant) manages every tenant, as
            # on the day before this guard existed. Another tenant's admin
            # reaches only their own.
            from velaris_shared.tenancy.identity import is_platform_operator

            if await is_platform_operator(session, user):
                continue
            found, owner = True, raw
        else:
            found, owner = await _owner(session, spec, raw)
        if found and owner is None and write and getattr(spec, "shared_write_operator_only", False):
            from velaris_shared.tenancy.identity import is_platform_operator

            if not await is_platform_operator(session, user):
                log.info("tenant_path_guard: %s refused (platform-shared %s)", where, param)
                raise HTTPException(404, "Not found")
            continue
        if found and owner is None and _null_is_default(spec):
            from velaris_shared.tenancy.identity import DEFAULT_TENANT, same_tenant_as_default
            if not await same_tenant_as_default(session, token or DEFAULT_TENANT):
                log.info("tenant_path_guard: %s refused (%s is the default tenant's)", where, param)
                raise HTTPException(404, "Not found")
            continue
        if not found or owner is None:
            continue
        if not await same_tenant(session, owner, token):
            log.info("tenant_path_guard: %s refused (%s owned elsewhere)", where, param)
            raise HTTPException(404, "Not found")


async def platform_operator_only(
    request: HTTPConnection,
    session: AsyncSession = Depends(get_session),
) -> None:
    """For data with no tenant column that is still one tenant's business — the
    security-event log, push delivery logs, GDPR requests. Until the table
    carries a tenant it can only be scoped to the install's operator
    (superadmin, or the platform's own default tenant). A single-tenant install
    is unaffected: its admins are in the default tenant.
    """
    from velaris_shared.tenancy.identity import is_platform_operator

    user = await _caller(request)
    if user is None:
        return            # the route's own authentication answers
    if not await is_platform_operator(session, user):
        raise HTTPException(403, "This log spans every tenant; platform operator only.")
