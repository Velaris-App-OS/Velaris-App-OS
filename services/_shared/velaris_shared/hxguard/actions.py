"""HxGuard action registry — Phase 1 of privilege-based authorization.

Every `require_role(...)` gate in the routers is named here as an **action**.
Actions are declared in code, never in data: data decides who *holds* an action,
code decides which actions *exist*. That is what keeps the model fail-closed —
a typo'd privilege in the database grants nothing rather than inventing a
capability.

`legacy_roles` records the roles the gate required before this migration. It is
not decoration: `privileges.SEEDED_ROLE_PRIVILEGES` is derived from it, and
`tests/phase_authz` asserts that the derived answer matches `require_role` for
every action and every subject shape. Delete a `legacy_roles` entry and the
parity proof for that action goes with it.

**Nothing here is enforced yet.** Phase 1 adds the registry and the evaluator
alongside the existing gates; Phase 2 shadows them; Phase 3 cuts over.

Source: docs/Future/Done/authz-gate-inventory.md (Phase 0, 2026-08-13).

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ActionDef:
    """One authorization decision the platform can be asked to make."""

    #: `<domain>.<resource>.<verb>` — the verb is the last dot-segment.
    name: str
    #: "endpoint" gates one route; "module" gates a whole router. A module
    #: action cannot be split into read/write because a router-level dependency
    #: fires before the handler is known — that split is Phase 3 work.
    kind: Literal["endpoint", "module"]
    #: Roles the pre-migration `require_role(...)` gate required. The parity
    #: oracle, not documentation.
    legacy_roles: tuple[str, ...]
    #: What the action actually protects, in one sentence.
    summary: str


def _a(name, kind, roles, summary):
    return ActionDef(name=name, kind=kind, legacy_roles=tuple(roles), summary=summary)


#: Every action the platform evaluates. Unknown action ⇒ deny, always.
ACTIONS: dict[str, ActionDef] = {}


def _reg(a: ActionDef) -> None:
    if a.name in ACTIONS:
        raise ValueError(f"duplicate action {a.name!r}")
    ACTIONS[a.name] = a


# ── endpoint actions ──────────────────────────────────────────────
_reg(_a("ai.metrics.read", "endpoint", ['admin'],
        "Per-call AI cost and volume. Commercially sensitive."))
# 2026-09-17: the tenant-wide window on the document index queue (mig 203). The
# per-case listing stays on case access; this one spans every case in the
# tenant and names their documents, so it is an administrator's view.
_reg(_a("ai.index_queue.read", "endpoint", ['admin'],
        "See the whole document index queue for the tenant: pending, failed, and why."))

_reg(_a("compliance.audit.seal", "endpoint", ['admin'],
        "Anchors the chain externally."))
_reg(_a("compliance.consent.read", "endpoint", ['admin', 'security'],
        "Is this processing lawful right now."))
_reg(_a("compliance.consent.write", "endpoint", ['admin', 'security'],
        "Appends to the lawful-basis ledger. Append-only by design."))
_reg(_a("compliance.dr.read", "endpoint", ['admin'],
        "RPO/RTO posture per region."))
_reg(_a("compliance.dr.write", "endpoint", ['admin'],
        "An untested backup is a hope — this is the evidence."))
_reg(_a("compliance.ledger.read", "endpoint", ['admin'],
        "Payments vs ledger. Reports, never corrects."))
_reg(_a("compliance.ledger.write", "endpoint", ['admin'],
        "The only correction mechanism — a mirror posting."))
_reg(_a("compliance.privacy.read", "endpoint", ['admin', 'security'],
        "PII findings and their review state."))
_reg(_a("compliance.privacy.review", "endpoint", ['admin', 'security'],
        "A human verdict a rescan must never overwrite."))
_reg(_a("compliance.privacy.scan", "endpoint", ['admin', 'security'],
        "Scans every form and data model for personal data."))
_reg(_a("compliance.reports.generate", "endpoint", ['admin'],
        "Produces a compliance report."))
_reg(_a("bots.definitions.read", "endpoint", ['admin', 'developer'],
        "The bot registry."))
_reg(_a("bots.definitions.write", "endpoint", ['admin', 'developer'],
        "Registers a container image that will run on this host."))
_reg(_a("bots.definitions.approve", "endpoint", ['admin'],
        "Puts a bot into production. The review gate — never the author."))
_reg(_a("bots.credentials.write", "endpoint", ['admin'],
        "Stores a third party's password for a bot to sign in with."))
_reg(_a("bots.jobs.read", "endpoint", ['admin', 'developer', 'manager', 'case_worker'],
        "Job status and the attended proposal diff."))
_reg(_a("bots.jobs.run", "endpoint", ['admin', 'developer', 'manager', 'case_worker'],
        "Runs a bot, cancels one, or accepts its proposed changes."))
# Calendar. Absence is personal data before it is a scheduling input: the
# team view says *busy*, and only the person, their manager and an admin see the
# kind or the note. Approving leave reroutes other people's work, so it is a
# separate action from writing it — the same split bots and schedules carry.
_reg(_a("calendar.absences.read", "endpoint", ['admin', 'manager', 'case_worker', 'developer'],
        "Who is away and when. The kind and the note are filtered per caller."))
_reg(_a("calendar.absences.write", "endpoint", ['admin', 'manager', 'case_worker'],
        "Records an absence. Your own, or somebody's if you manage them."))
_reg(_a("calendar.absences.approve", "endpoint", ['admin', 'manager'],
        "Puts leave into effect — never for the person taking it."))
_reg(_a("calendar.patterns.write", "endpoint", ['admin'],
        "Changes the hours a person is expected to work."))
_reg(_a("calendar.events.read", "endpoint", ['admin', 'manager', 'case_worker', 'developer'],
        "Team events, and when everybody named is free."))
_reg(_a("calendar.events.write", "endpoint", ['admin', 'manager', 'case_worker', 'developer'],
        "Schedules or cancels a team event."))
# Importing is its own action, not `events.write`. An upload writes many rows at
# once from a file this platform did not produce, and it can request leave —
# a tenant may reasonably allow typing a meeting and refuse bulk import.
# Three segments deliberately: a two-segment name cannot be granted from data
# (`_crosses_into_platform_namespace`), which is roadmap 3c's whole point.
_reg(_a("calendar.imports.write", "endpoint", ['admin', 'manager', 'case_worker'],
        "Imports an .ics export into a person's calendar. Never approves leave."))
# Subscribing makes this SERVER fetch a URL a user chose, so it is separated
# from reading the list: the allowlist bounds where, and this bounds who.
_reg(_a("calendar.subscriptions.read", "endpoint",
        ['admin', 'manager', 'case_worker', 'developer'],
        "Which external calendars are subscribed to. The URL is a capability "
        "and is shown only to whoever may see the detail."))
_reg(_a("calendar.subscriptions.write", "endpoint",
        ['admin', 'manager', 'case_worker'],
        "Subscribes to a calendar published elsewhere, and refreshes it. "
        "Read-only against the feed; never writes back."))

# HxDeploy + App Registry. Neither router had a privilege gate of any kind — 21
# and 13 endpoints behind a bare `get_current_user`, so any authenticated user
# could register a target environment (which holds its import key), promote to
# production, and approve their own run. Promotion moves design-time artifacts
# between environments and, from phase 3, container images: it is an
# administrative act, not a read.
_reg(_a("deploy.environments.read", "endpoint", ['admin', 'devops', 'developer'],
        "The pipeline's environments and their health."))
_reg(_a("deploy.environments.write", "endpoint", ['admin', 'devops'],
        "Registers a target environment, including the key that lets it be written to."))
_reg(_a("deploy.packages.read", "endpoint", ['admin', 'devops', 'developer'],
        "Promotion packages and the bundles they froze."))
_reg(_a("deploy.packages.write", "endpoint", ['admin', 'devops', 'developer'],
        "Builds or discards a draft package."))
_reg(_a("deploy.packages.seal", "endpoint", ['admin', 'devops'],
        "Freezes a package. After this it is what ships, and it cannot be edited."))
_reg(_a("deploy.runs.read", "endpoint", ['admin', 'devops', 'developer', 'manager'],
        "Deployment runs, their risk verdicts and health checks."))
_reg(_a("deploy.runs.promote", "endpoint", ['admin', 'devops'],
        "Queues a package for an environment."))
_reg(_a("deploy.runs.approve", "endpoint", ['admin'],
        "Releases a run into the target. The review gate — never the initiator."))
# F-04 — the simulation gate. Three actions, and the split is the point.
#
# RUNNING a rehearsal is ordinary devops work. CONFIGURING the gate decides what
# every future promotion must clear, so it is admin-only. And OVERRIDING a
# failed gate is the one control that lets a change through after its rehearsal
# said not to — it is separated from `deploy.runs.approve` deliberately, so that
# bypassing the gate and approving the deployment are two grants, not one.
_reg(_a("deploy.simulation.run", "endpoint", ['admin', 'devops', 'developer'],
        "Rehearses a promotion by replaying recorded cases through the candidate rules."))
_reg(_a("deploy.simulation.configure", "endpoint", ['admin'],
        "Sets the thresholds every promotion must clear. Off until somebody turns it on."))
_reg(_a("deploy.simulation.override", "endpoint", ['admin'],
        "Lets a promotion past a FAILED rehearsal. Signed, reasoned and permanent."))
_reg(_a("deploy.windows.read", "endpoint", ['admin', 'devops', 'developer'],
        "When deployments are allowed to run."))
_reg(_a("deploy.windows.write", "endpoint", ['admin', 'devops'],
        "Changes when deployments are allowed to run."))
_reg(_a("apps.packages.read", "endpoint", ['admin', 'devops', 'developer'],
        "App-registry package snapshots and their download."))
_reg(_a("apps.packages.write", "endpoint", ['admin', 'devops', 'developer'],
        "Snapshots platform state as a package, or moves its status."))
_reg(_a("apps.packages.import", "endpoint", ['admin', 'devops'],
        "Writes a case type and its forms into this environment from a bundle."))
_reg(_a("apps.deployments.write", "endpoint", ['admin', 'devops'],
        "Records that a package was deployed to an environment."))
_reg(_a("scheduler.schedules.read", "endpoint", ['admin', 'developer', 'manager'],
        "Timed actions and their run history."))
_reg(_a("scheduler.schedules.write", "endpoint", ['admin', 'developer'],
        "Writes a timed action. It stays draft until somebody else approves it."))
_reg(_a("scheduler.schedules.approve", "endpoint", ['admin'],
        "Puts a timed action into service. The review gate — never the author."))
_reg(_a("scheduler.schedules.run", "endpoint", ['admin', 'developer'],
        "Fires an approved schedule by hand, off its timetable."))
_reg(_a("channels.endpoints.read", "endpoint", ['admin'],
        "Channel endpoints — the numbers customers text."))
_reg(_a("channels.endpoints.write", "endpoint", ['admin'],
        "Creates an inbound webhook and its signing secret. A wrong secret "
        "here means an unauthenticated case-creation endpoint."))
_reg(_a("channels.conversations.read", "endpoint", ['admin', 'manager', 'case_worker'],
        "The channel inbox and one thread's messages."))
_reg(_a("channels.conversations.write", "endpoint", ['admin', 'manager', 'case_worker'],
        "Replying, assigning and closing. Replies cost money and reach a "
        "customer's phone."))
_reg(_a("channels.cti.pop", "endpoint", ['admin', 'manager', 'case_worker'],
        "Screen-pop from a contact-centre platform."))
_reg(_a("compliance.retention.run", "endpoint", ['admin'],
        "Deletes data. The most destructive action in this table."))
_reg(_a("docs.guides.regenerate", "endpoint", ['designer', 'admin'],
        "Regenerates the generated guides."))
_reg(_a("identity.directory.read", "endpoint", ['admin'],
        "The people directory."))
_reg(_a("identity.directory.sync", "endpoint", ['admin'],
        "The SSO/LDAP upsert path. SCIM will land here (sequence item 4)."))
_reg(_a("identity.directory.write", "endpoint", ['admin'],
        "Deactivation, not deletion."))
_reg(_a("identity.groups.read", "endpoint", ['admin'],
        "Membership."))
_reg(_a("identity.groups.write", "endpoint", ['admin'],
        "Lockout risk — can remove the last member who can grant access."))
_reg(_a("identity.permissions.write", "endpoint", ['admin'],
        "The Studio route-visibility map. UI only — never a backend gate."))
_reg(_a("identity.portals.read", "endpoint", ['admin'],
        "One module set."))
_reg(_a("identity.portals.write", "endpoint", ['admin'],
        "Removes a module set out from under its groups."))
_reg(_a("identity.provisioning.read", "endpoint", ['admin'],
        "IdP group mappings and the provisioning log — why a user is in a group."))
_reg(_a("identity.provisioning.write", "endpoint", ['admin'],
        "Maps an IdP group onto an access group. This is how a directory "
        "confers authorization here, so it is the escalation path to watch."))
_reg(_a("identity.scim.read", "endpoint", ['admin'],
        "SCIM token listing. Metadata only — the token itself is unrecoverable."))
_reg(_a("identity.scim.write", "endpoint", ['admin'],
        "Mints and revokes SCIM tokens. A minted token provisions and "
        "deprovisions accounts for its whole tenant."))
_reg(_a("identity.roles.read", "endpoint", ['admin'],
        "One role's privileges."))
_reg(_a("identity.roles.write", "endpoint", ['admin'],
        "Revokes a bundle from everyone holding it."))
_reg(_a("identity.sso.configure", "endpoint", ['admin'],
        "Removes a sign-in path for everyone using it."))
_reg(_a("identity.sso.read", "endpoint", ['admin'],
        "Provider config incl. allowed domains; never the secret."))
_reg(_a("identity.tokens.configure", "endpoint", ['admin'],
        "Lengthens or shortens every session on the platform."))
_reg(_a("identity.tokens.read", "endpoint", ['admin'],
        "Session lifetime setting."))
_reg(_a("identity.users.read", "endpoint", ['admin'],
        "One account. Was ungated until 2026-08-13."))
_reg(_a("identity.users.write", "endpoint", ['admin'],
        "Removes a login."))
_reg(_a("platform.calendars.write", "endpoint", ['admin'],
        "Same, for an existing calendar."))
_reg(_a("platform.email.configure", "endpoint", ['admin'],
        "Removes the account."))
_reg(_a("platform.email.operate", "endpoint", ['admin'],
        "Live SMTP/IMAP probe using stored credentials."))
_reg(_a("platform.push.configure", "endpoint", ['admin'],
        "Changes who gets notified about what."))
_reg(_a("platform.push.operate", "endpoint", ['admin'],
        "Sends a real notification to a real device."))
_reg(_a("platform.push.read", "endpoint", ['admin'],
        "Per-case-type notification overrides."))
_reg(_a("platform.status.read", "endpoint", ['admin'],
        "Diagnostic detail — deliberately not the public status."))
_reg(_a("platform.updates.approve", "endpoint", ['admin'],
        "Per-ring approval in per-env mode."))
_reg(_a("platform.updates.approve_prod", "endpoint", ['admin'],
        "Second deliberate gate — the prod ring. Keep it separable from `approve`."))
_reg(_a("platform.updates.configure", "endpoint", ['admin'],
        "Changes how updates are approved at all."))
_reg(_a("platform.updates.read", "endpoint", ['admin'],
        "Every registered environment's version."))
_reg(_a("platform.updates.request", "endpoint", ['admin'],
        "Withdraws that approval."))
_reg(_a("platform.updates.rollback", "endpoint", ['admin'],
        "Reverts this environment."))
_reg(_a("portal.config.read", "endpoint", ['admin'],
        "Public presentation of a case type."))
_reg(_a("portal.config.write", "endpoint", ['admin'],
        "Customer-visible branding asset."))
_reg(_a("portal.customers.erase", "endpoint", ['admin'],
        "GDPR Art. 17 erasure — irreversible. Deserves its own action."))
_reg(_a("portal.customers.read", "endpoint", ['admin'],
        "One customer's record."))
_reg(_a("portal.customers.write", "endpoint", ['admin'],
        "Create a customer account on someone's behalf — the only path to an "
        "account once self-registration is switched off."))
_reg(_a("portal.submissions.read", "endpoint", ['admin'],
        "What customers submitted."))
_reg(_a("variables.definitions.write", "endpoint", ['designer'],
        "Removes a definition."))


# ── Phase 3.5: the gates Phase 0 could not see ────────────────────
# `require_admin()` is a one-line alias for `require_role("admin")`, so the
# Phase-0 grep for `require_role(` walked straight past 56 gates in four
# routers. Same defect class as the router-level `Depends(` gates that grep
# also hid — an alias is a gate, and a gate you cannot count is a gate you
# cannot migrate.
#
# All 56 were bare `admin`, so every action here seeds to admin-only and the
# cutover is behaviour-preserving. The read/write split is the same win Phase 1
# delivered elsewhere: 24 of the 56 are read-only and demanded the same `admin`
# as the writes beside them, so nobody could look at a storefront's catalogue
# without also being able to rewrite it.
_reg(_a("commerce.analytics.read", "endpoint", ['admin'],
        "Checkout revenue and conversion totals. Commercially sensitive."))
_reg(_a("commerce.content.read", "endpoint", ['admin'],
        "Storefront theme, pages, navigation, SEO and media."))
_reg(_a("commerce.content.write", "endpoint", ['admin'],
        "Publishes customer-facing storefront content."))
_reg(_a("commerce.integrations.read", "endpoint", ['admin'],
        "Which payment/fulfilment integrations are configured."))
_reg(_a("commerce.integrations.write", "endpoint", ['admin'],
        "Configures an integration — holds third-party credentials."))
_reg(_a("commerce.inventory.read", "endpoint", ['admin'],
        "Stock levels and their adjustment history."))
_reg(_a("commerce.inventory.write", "endpoint", ['admin'],
        "Adjusts stock. Moves real, sellable quantity."))
_reg(_a("commerce.orders.read", "endpoint", ['admin'],
        "Customer orders, including buyer detail."))
_reg(_a("commerce.products.read", "endpoint", ['admin'],
        "Catalogue: products, variants, options, categories."))
_reg(_a("commerce.products.write", "endpoint", ['admin'],
        "Changes what is for sale and how it is grouped."))
_reg(_a("commerce.promotions.read", "endpoint", ['admin'],
        "Promotions, and validating a code against a cart."))
_reg(_a("commerce.promotions.write", "endpoint", ['admin'],
        "Creates or withdraws a discount — directly affects revenue."))
_reg(_a("commerce.stores.read", "endpoint", ['admin'],
        "Store list and configuration."))
_reg(_a("commerce.stores.write", "endpoint", ['admin'],
        "Creates, reconfigures, clones or archives a storefront."))
_reg(_a("commerce.tokens.read", "endpoint", ['admin'],
        "Which checkout service tokens exist."))
_reg(_a("commerce.tokens.write", "endpoint", ['admin'],
        "Mints or revokes a checkout service token — a credential."))
_reg(_a("platform.hxtest.write", "endpoint", ['admin'],
        "Generates and registers HxTest suites."))
_reg(_a("platform.testsuite.read", "endpoint", ['admin'],
        "Past test runs and their streamed output."))
_reg(_a("platform.testsuite.run", "endpoint", ['admin'],
        "Executes a suite against the running platform."))


# ── module actions (router-level gates) ───────────────────────────
# Each of these governs an entire router. They are the most consequential gates
# in the codebase: a router dependency applies to every route added to that file
# afterwards, so migrating a module endpoint-by-endpoint and missing the router
# line would remove authorization from all of it.
_reg(_a("analytics.mining.access", "module", ['designer', 'admin'],
        "Whole-router gate on process_mining.py (~8 routes)."))
_reg(_a("compliance.enterprise.access", "module", ['admin'],
        "Whole-router gate on enterprise.py (~15 routes)."))
_reg(_a("design.codegen.access", "module", ['designer', 'admin'],
        "Whole-router gate on codegen.py (~3 routes)."))
_reg(_a("design.data_models.access", "module", ['designer', 'admin'],
        "Whole-router gate on data_models.py (~5 routes)."))
_reg(_a("design.nlp.access", "module", ['designer', 'admin'],
        "Whole-router gate on nlp.py (~4 routes)."))
_reg(_a("integration.fusion.access", "module", ['integration', 'admin', 'designer'],
        "Whole-router gate on hxfusion.py (~15 routes)."))
_reg(_a("platform.admin.access", "module", ['admin'],
        "Whole-router gate on admin.py (~9 routes)."))
_reg(_a("platform.orchestrator.access", "module", ['devops', 'admin'],
        "Whole-router gate on orchestrator.py (~12 routes)."))
_reg(_a("platform.scout.access", "module", ['devops', 'admin'],
        "Whole-router gate on scout.py (~7 routes)."))
_reg(_a("platform.scout_ai.access", "module", ['devops', 'admin'],
        "Whole-router gate on scout_ai.py (~6 routes)."))
_reg(_a("platform.tenants.access", "module", ['admin'],
        "Whole-router gate on tenants.py (~12 routes)."))
_reg(_a("security.shield.access", "module", ['security', 'admin'],
        "Whole-router gate on hxshield.py (~12 routes)."))


# ── Phase 4 step 2: the imperative checks ─────────────────────────
# Not gates at all, in the FastAPI sense — role checks written as statements in
# handler bodies, or in sync helpers those handlers call. 205 sites across 24
# routers, of which 45 are roots (the rest inherit by calling a root).
#
# They differ from every earlier wave in one way that shapes the naming below:
# a dependency either passes or raises, but a statement in a body can also
# *shape a response* — scope a query, redact a column, hide a field. Those need
# an action just as much, and they are the ones a blanket "make it admin-only"
# rewrite would have broken, because their answer is a `bool`, not a refusal.
#
# `legacy_roles` continues to mean "what the pre-migration check required", so
# the seeding stays derived. Where a check tested `has_privilege("*", "*")` as
# well, that is NOT reproduced as a role: the legacy access-group wildcard is
# deliberately outside the platform namespace (see privileges.py), and Phase 4
# step 1 is what made that boundary load-bearing.

# Module-level access, previously `_require_admin`-style statements.
_reg(_a("platform.dbmigrate.access", "module", ['admin', 'superadmin'],
        "HxDBMigrate — moves data between live databases (~18 routes)."))
_reg(_a("design.evolve.access", "module", ['admin', 'superadmin'],
        "HxEvolve insight review and proposal decisions (~9 routes)."))
_reg(_a("platform.replay.access", "module", ['admin', 'superadmin'],
        "HxReplay — replay runs across other operators' cases (~6 routes)."))
_reg(_a("platform.stream.access", "module", ['admin'],
        "HxStream event ingest and query; staff-level portal or above."))
_reg(_a("platform.stream.read_all", "endpoint", ['admin'],
        "Read every actor's stream events, not just your own."))
_reg(_a("platform.dbmanager.access", "module", ['admin'],
        "HxDBManager — direct database browsing (~17 routes)."))
_reg(_a("platform.dbmanager.read", "endpoint", ['admin', 'designer', 'developer'],
        "Schema browser and table viewer; the developer tier sees only _DEV_TABLES."))
_reg(_a("platform.dbmanager.reauth", "endpoint", ['admin', 'designer', 'developer'],
        "Step-up re-authentication for the DB viewer session."))

# Marketplace — package distribution, distinct from the `commerce.*` storefront.
# `marketplace.admin.access` covers the 33 routes behind `_require_admin`
# (sources, capability grants, the review queue, installs, blacklist, access
# rules, release requests). They were one bare `admin` check, so they stay one
# action: inventing a finer split here would be a guess at a boundary the code
# has never drawn, and a guess is what the parity discipline exists to avoid.
_reg(_a("marketplace.admin.access", "module", ['admin'],
        "Marketplace administration: sources, grants, review queue, installs, blacklist."))
_reg(_a("marketplace.packages.publish", "endpoint", ['admin', 'developer'],
        "Build and submit a package others can install."))
_reg(_a("marketplace.catalogue.read", "endpoint", ['admin', 'developer', 'manager'],
        "Reach the marketplace at all."))
_reg(_a("marketplace.workspaces.override", "endpoint", ['admin'],
        "Act on a workspace you did not create, and bypass the per-user limit."))

# Design-time authoring, previously ownership-plus-admin-override statements.
_reg(_a("design.branches.override", "endpoint", ['admin'],
        "Act on a branch you do not own — review, merge, recall, revert."))
_reg(_a("design.case_types.write", "endpoint", ['admin', 'designer'],
        "Create or modify a case type."))
_reg(_a("design.draft.apply", "endpoint", ['admin', 'designer'],
        "Apply an HxDraft proposal to live configuration."))
_reg(_a("design.rules.read", "endpoint", ['admin', 'designer'],
        "Rule-migration reporting across case types."))

# Response-shaping actions: these answer `bool`, and the answer redacts or
# scopes rather than refuses.
_reg(_a("finance.details.read", "endpoint", ['admin', 'finance'],
        "Unredacted invoice and payment financials; otherwise totals-only."))
_reg(_a("casework.sensitive.read", "endpoint", ['admin'],
        "Cases and fields marked sensitive; otherwise filtered from the list."))
_reg(_a("docs.files.delete", "endpoint", ['admin'],
        "Delete a document or one of its versions."))
_reg(_a("integration.connectors.rotate", "endpoint", ['admin', 'integration'],
        "Rotate stored connector credentials, and apply a drafted connector change."))
_reg(_a("variables.operator.elevated", "endpoint", ['admin'],
        "Operator context that bypasses case-variable visibility scoping."))
# 2026-09-17: no longer consulted by the routes. `admin` seeds to a wildcard, so
# this privilege could never keep a TENANT admin out; cross-tenant PII findings
# and backup records are now superadmin-only in `compliance.py`. Kept registered
# so existing grants and the parity oracle do not break.
_reg(_a("compliance.crosstenant.read", "endpoint", ['admin'],
        "Unused since 2026-09-17 — cross-tenant PII findings and backups are superadmin-only."))

# ── 2026-08-15 verification: routes that had NO gate at all ───────
# Every phase so far migrated the gates that EXISTED. Nothing ever inventoried
# the routes that had none — a `require_role` grep cannot see an absent gate, and
# neither could any of the three detectors that followed it. A post-Phase-4 sweep
# of the BUILT app (not the source) found 58 such routes, 29 of them writes.
# These actions close the ones with no legitimate anonymous caller.
_reg(_a("platform.email.read", "endpoint", ['admin'],
        "Mailbox contents, templates and account config. Message bodies are correspondence."))
_reg(_a("analytics.reports.read", "endpoint", ['admin', 'manager'],
        "Case volumes, SLA compliance and stage bottlenecks across the deployment."))
_reg(_a("platform.intake.read", "endpoint", ['admin'],
        "Raw inbound intake events, before they become cases."))
_reg(_a("design.escalation.read", "endpoint", ['admin', 'manager'],
        "Escalation trees and a case's escalation schedule."))
_reg(_a("design.escalation.write", "endpoint", ['admin'],
        "Create, edit or delete an escalation tree; fire an escalation."))
_reg(_a("compliance.frameworks.read", "endpoint", ['admin', 'security'],
        "Which compliance frameworks this deployment claims."))


# -- Phase 5 (2026-08-15): the case layer's own role registry ------
# `RbacBackend.ACTION_ROLES` was the LAST role-name decision table in the
# platform - Phase A built it to mirror `require_role`, and phases 1-4 migrated
# everything around it without touching it. Same shape as Phase 0's inventory
# (action -> role set, with an `is_admin` bypass), so the same treatment applies
# and `legacy_roles` is again transcribed straight from the table it replaces.
#
# `case.rule.apply` is deliberately NOT here. Its role set is EMPTY, meaning
# "any authenticated user" - a privilege model cannot express that (a subject
# with no roles holds no privileges), so registering it would silently tighten
# an open surface. It stays in `ACTION_ROLES`, which is now what that dict is
# for: decisions that are about authentication, not about privilege.
_reg(_a("connector.namespace.register", "endpoint", ['admin', 'integration'],
        "Bind a connector to a variable namespace."))
_reg(_a("costing.rates.access", "endpoint", ['admin'],
        "Rate cards. Commercially sensitive."))
_reg(_a("devconn.namespace.register", "endpoint", ['admin', 'developer', 'integration'],
        "Bind a DevConn build to a variable namespace."))
_reg(_a("mcp.tokens.mint", "endpoint", ['admin', 'designer'],
        "Mint a scoped external-agent (MCP) token."))
_reg(_a("replay.cohorts.run", "endpoint", ['admin'],
        "HxReplay cohort replay: bulk read across other operators' cases."))
_reg(_a("risk.outcomes.write", "endpoint", ['admin', 'manager'],
        "Write ground truth. A wrong label poisons everything learned later."))
_reg(_a("risk.assessment.read", "endpoint", ['admin', 'case_worker', 'manager'],
        "HxSentinel findings and outcome labels."))
_reg(_a("risk.rules.configure", "endpoint", ['admin', 'manager'],
        "Tune the HxSentinel rule engine - tenant-wide."))
_reg(_a("risk.scores.review", "endpoint", ['admin', 'manager'],
        "Supervise reviewers' scores; dashboard and dispute."))
_reg(_a("risk.taxonomy.write", "endpoint", ['admin'],
        "Outcome vocabulary - tenant-wide."))
_reg(_a("rules.definitions.write", "endpoint", ['admin', 'designer'],
        "Rule definition CRUD - gates automation behaviour."))
# Audit streaming (market gap A3). Three segments so a privilege can be granted
# from data. Read is separate from write because the endpoint and its state are
# something a security reviewer should be able to see without being able to
# point the platform's audit somewhere else.
_reg(_a("audit.stream.read", "endpoint", ['admin'],
        "See where security events are streamed, and whether it is working."))
_reg(_a("audit.stream.write", "endpoint", ['admin'],
        "Set the SIEM this platform ships its security events to."))
# HxShield's response layer (roadmap #25). Separate from `security.shield.access`
# because seeing that the platform wants to suspend somebody and being able to
# suspend them are different jobs on most floors.
_reg(_a("security.shield.respond", "endpoint", ['admin'],
        "Approve or decline a HxShield response - suspends accounts, kills sessions."))
_reg(_a("variables.namespace.admin", "endpoint", ['admin'],
        "Create or administer a case-variable namespace."))
_reg(_a("variables.blobkeys.promote", "endpoint", ['admin'],
        "Promote a blob key to a typed variable, platform-wide for that type."))
_reg(_a("variables.blobkeys.scan", "endpoint", ['admin', 'designer'],
        "Scan a case type's blob keys for promotable variables."))

# The ReBAC bypass. `RebacBackend` answered `superadmin or is_admin or manager`
# before it looked at a single relationship, so "manager sees every case" was
# unrevocable and invisible - it appeared in no gate, no registry and no UI.
# Naming it makes it a privilege like any other; seeding it to admin+manager
# keeps today's answer exactly.
_reg(_a("casework.records.any", "endpoint", ['admin', 'manager'],
        "Act on any case without a relationship to it. The ReBAC bypass."))


#: Every role name that appears in any gate. A role outside this set grants no
#: platform action today, which is why `viewer`/`manager`/`case_worker` seed to
#: an empty privilege set rather than to something plausible-looking.
GATE_ROLES: frozenset[str] = frozenset(
    r for a in ACTIONS.values() for r in a.legacy_roles
)




#: Which router handler each endpoint action came from, as `file.py::function`.
#: Phase 2 resolves a live route back to its action through this index; Phase 3
#: deletes the gate it names. A key here with no matching handler means a route
#: was renamed and its shadow comparison silently stopped running.
ACTION_GATES: dict[str, tuple[str, ...]] = {
    # Phase 3.5 — the `require_admin()` gates.
    "commerce.analytics.read": ('checkout.py::analytics_summary',),
    "commerce.content.read": ('storefront.py::get_navigation', 'storefront.py::get_page', 'storefront.py::get_seo', 'storefront.py::get_theme', 'storefront.py::list_media', 'storefront.py::list_pages'),
    "commerce.content.write": ('storefront.py::delete_media', 'storefront.py::preview_theme', 'storefront.py::save_navigation', 'storefront.py::save_page', 'storefront.py::save_seo', 'storefront.py::save_theme', 'storefront.py::upload_media'),
    "commerce.integrations.read": ('checkout.py::list_integrations',),
    "commerce.integrations.write": ('checkout.py::create_integration', 'checkout.py::delete_integration', 'checkout.py::update_integration'),
    "commerce.inventory.read": ('storefront.py::inventory_history', 'storefront.py::list_inventory'),
    "commerce.inventory.write": ('storefront.py::adjust_inventory',),
    "commerce.orders.read": ('checkout.py::list_orders',),
    "commerce.products.read": ('storefront.py::get_product', 'storefront.py::list_categories', 'storefront.py::list_products'),
    "commerce.products.write": ('storefront.py::add_option', 'storefront.py::add_variant', 'storefront.py::archive_product', 'storefront.py::create_category', 'storefront.py::create_product', 'storefront.py::delete_category', 'storefront.py::delete_variant', 'storefront.py::set_product_categories', 'storefront.py::update_product'),
    "commerce.promotions.read": ('storefront.py::list_promotions', 'storefront.py::validate_promotion_admin'),
    "commerce.promotions.write": ('storefront.py::create_promotion', 'storefront.py::deactivate_promotion', 'storefront.py::update_promotion'),
    "commerce.stores.read": ('storefront.py::get_store', 'storefront.py::list_stores'),
    "commerce.stores.write": ('storefront.py::archive_store', 'storefront.py::clone_store', 'storefront.py::create_store', 'storefront.py::update_store'),
    "commerce.tokens.read": ('checkout.py::list_tokens',),
    "commerce.tokens.write": ('checkout.py::create_token', 'checkout.py::revoke_token'),
    "platform.hxtest.write": ('hxtest.py::generate', 'hxtest.py::register_bundled'),
    "platform.testsuite.read": ('testsuite.py::list_runs', 'testsuite.py::run_detail', 'testsuite.py::run_stream'),
    "platform.testsuite.run": ('testsuite.py::generate_suite', 'testsuite.py::run', 'testsuite.py::run_conformance'),

    "ai.metrics.read": ('hxnexus.py::llmops_metrics',),
    "compliance.audit.seal": ('compliance.py::seal_chain', 'compliance.py::trigger_audit_anchor'),
    "compliance.consent.read": ('compliance.py::check_consent', 'compliance.py::get_consent_state'),
    "compliance.consent.write": ('compliance.py::record_consent',),
    "compliance.dr.read": ('compliance.py::get_dr_posture', 'compliance.py::list_backups'),
    "compliance.dr.write": ('compliance.py::record_backup', 'compliance.py::record_restore_test'),
    "compliance.ledger.read": ('compliance.py::get_trial_balance', 'compliance.py::reconcile_ledger'),
    "compliance.ledger.write": ('compliance.py::create_ledger_account', 'compliance.py::post_ledger_transaction', 'compliance.py::reverse_ledger_transaction'),
    "compliance.privacy.read": ('compliance.py::list_pii_findings',),
    "compliance.privacy.review": ('compliance.py::review_pii_finding',),
    "compliance.privacy.scan": ('compliance.py::run_pii_scan',),
    "compliance.reports.generate": ('compliance.py::generate_report',),
    "compliance.retention.run": ('enterprise.py::run_retention',),
    "docs.guides.regenerate": ('hxnexus.py::regenerate_docs',),
    "identity.directory.read": ('user_directory.py::list_entries',),
    "identity.directory.sync": ('user_directory.py::bulk_sync', 'user_directory.py::sync_from_db'),
    "identity.directory.write": ('user_directory.py::create_entry', 'user_directory.py::deactivate_entry', 'user_directory.py::update_entry'),
    "identity.groups.read": ('access_groups.py::get_access_group', 'access_groups.py::list_access_groups', 'access_groups.py::list_members'),
    "identity.groups.write": ('access_groups.py::add_member', 'access_groups.py::create_access_group', 'access_groups.py::delete_access_group', 'access_groups.py::remove_member', 'access_groups.py::update_access_group'),
    "identity.permissions.write": ('permissions.py::update_permissions',),
    "identity.portals.read": ('access_groups.py::get_portal', 'access_groups.py::list_portals'),
    "identity.portals.write": ('access_groups.py::create_portal', 'access_groups.py::delete_portal', 'access_groups.py::update_portal'),
    "identity.roles.read": ('access_groups.py::get_access_role', 'access_groups.py::get_privilege_catalog', 'access_groups.py::list_access_roles'),
    "identity.roles.write": ('access_groups.py::create_access_role', 'access_groups.py::delete_access_role', 'access_groups.py::update_access_role'),
    "identity.sso.configure": ('auth_real.py::create_sso_provider', 'auth_real.py::delete_sso_provider', 'auth_real.py::update_sso_provider'),
    "identity.sso.read": ('auth_real.py::manage_sso_providers',),
    "identity.tokens.configure": ('auth_real.py::set_token_expiry',),
    "identity.tokens.read": ('auth_real.py::get_token_expiry',),
    "identity.users.read": ('auth_real.py::get_user', 'auth_real.py::list_users'),
    "identity.users.write": ('auth_real.py::delete_user', 'auth_real.py::register_user', 'auth_real.py::update_user'),
    "platform.calendars.write": ('admin.py::create_calendar', 'admin.py::update_calendar'),
    "platform.email.configure": ('email.py::create_account', 'email.py::deactivate_account', 'email.py::delete_account', 'email.py::update_account'),
    "platform.email.operate": ('email.py::poll_now', 'email.py::test_connection'),
    "platform.push.configure": ('push.py::upsert_ct_override',),
    "platform.push.operate": ('push.py::test_send',),
    "platform.push.read": ('push.py::admin_delivery_logs', 'push.py::admin_list_devices', 'push.py::get_ct_overrides'),
    "platform.status.read": ('status.py::authz_parity', 'status.py::internal_status', 'status.py::readiness'),
    "platform.updates.approve": ('platform_updates.py::approve_plan', 'platform_updates.py::approve_run', 'platform_updates.py::halt_plan'),
    "platform.updates.approve_prod": ('platform_updates.py::approve_prod',),
    "platform.updates.configure": ('platform_updates.py::put_update_settings',),
    "platform.updates.read": ('platform_updates.py::fleet_versions', 'platform_updates.py::get_update_settings', 'platform_updates.py::list_plans', 'platform_updates.py::update_status'),
    "platform.updates.request": ('platform_updates.py::cancel_update_request', 'platform_updates.py::request_update'),
    "platform.updates.rollback": ('platform_updates.py::request_rollback',),
    "portal.config.read": ('portal.py::get_case_type_portal_display', 'portal.py::list_portal_tenants'),
    "portal.config.write": ('portal.py::toggle_case_type_portal', 'portal.py::update_case_type_portal_display', 'portal.py::update_portal_settings', 'portal.py::upload_portal_logo'),
    "portal.customers.erase": ('portal_customers.py::admin_delete_customer',),
    "portal.customers.read": ('portal_customers.py::admin_get_customer', 'portal_customers.py::admin_list_customers'),
    "portal.customers.write": ('portal_customers.py::admin_create_customer',),
    "portal.submissions.read": ('portal.py::list_portal_submissions',),
    "variables.definitions.write": ('case_variables.py::define_variable', 'case_variables.py::delete_variable', 'case_variables.py::patch_variable'),
}

#: Router file → module action, for the 12 router-level gates.
MODULE_ACTION_BY_FILE: dict[str, str] = {
    "process_mining.py": "analytics.mining.access",
    "enterprise.py": "compliance.enterprise.access",
    "codegen.py": "design.codegen.access",
    "data_models.py": "design.data_models.access",
    "nlp.py": "design.nlp.access",
    "hxfusion.py": "integration.fusion.access",
    "admin.py": "platform.admin.access",
    "orchestrator.py": "platform.orchestrator.access",
    "scout.py": "platform.scout.access",
    "scout_ai.py": "platform.scout_ai.access",
    "tenants.py": "platform.tenants.access",
    "hxshield.py": "security.shield.access",
}

#: Routers cut over to `hxguard.guard(...)`. **Phase 3 completed 2026-08-13 —
#: this is now every router that ever had a role gate, and `require_role` has no
#: remaining call sites in the API layer.** The ledger stays because it is what
#: `tests/phase_authz` checks against: a router here must have zero
#: `require_role` calls, and a gate reappearing anywhere is a regression rather
#: than a preference.
MIGRATED_ROUTERS: frozenset[str] = frozenset({
    "access_groups.py",
    "admin.py",
    "auth_real.py",
    "case_variables.py",
    "checkout.py",             # Phase 3.5
    "codegen.py",
    "compliance.py",
    "data_models.py",
    "email.py",
    "enterprise.py",
    "hxfusion.py",
    "hxnexus.py",
    "hxshield.py",
    "hxtest.py",               # Phase 3.5
    "nlp.py",
    "orchestrator.py",
    "permissions.py",
    "platform_updates.py",
    "portal.py",
    "portal_customers.py",
    "process_mining.py",
    "push.py",
    "scout.py",
    "scout_ai.py",
    "status.py",
    "storefront.py",           # Phase 3.5
    "tenants.py",
    "testsuite.py",            # Phase 3.5
    "user_directory.py",
})


#: Reverse index: `file.py::function` → endpoint action.
ACTION_BY_GATE: dict[str, str] = {
    gate: action for action, gates in ACTION_GATES.items() for gate in gates
}

# HxArbiter — the sovereignty & security policy engine.
#
# `decide` is separated from everything else because it is called by SERVICES,
# not by people: the engine, workers and marketplace connectors ask it on the
# hot path, while a human never does. Giving it the same action as reading
# policy would mean every service that enforces could also rewrite the rules.
#
# Three segments throughout, deliberately — a two-segment name cannot be granted
# from data (`_crosses_into_platform_namespace`), and the point of this engine is
# that its own governance is expressible.
_reg(_a("arbiter.policies.read", "endpoint", ['admin', 'security', 'developer'],
        "Reads policies, versions and the attribute catalogue. Changes nothing."))
_reg(_a("arbiter.policies.write", "endpoint", ['admin'],
        "Creates a policy or appends a version. Never activates one."))
# Activation is its own action, not `policies.write`. Writing a version is a
# draft; activating it is the moment the platform starts answering differently,
# and the four-eyes rule has nothing to attach to if the two are one privilege.
_reg(_a("arbiter.policies.activate", "endpoint", ['admin'],
        "Puts a version into effect, or retires a policy. Never for a version you wrote alone."))
# Raising a namespace ceiling is what turns shadow into enforcement across a
# whole class of actions at once. It is the single most consequential control
# here and is admin-only for that reason.
_reg(_a("arbiter.namespaces.promote", "endpoint", ['admin'],
        "Raises or lowers a namespace's rollout ceiling. Shadow to enforce lives here."))
_reg(_a("arbiter.decisions.read", "endpoint", ['admin', 'security'],
        "Reads the decision log and verifies its seal. The audit surface."))
_reg(_a("arbiter.decisions.evaluate", "endpoint", ['admin', 'security'],
        "Asks the engine for a decision. Called by services on the hot path, never by a person."))
_reg(_a("arbiter.simulate.run", "endpoint", ['admin', 'security', 'developer'],
        "What-if against supplied attributes. Writes a simulated decision, enforces nothing."))
# Parity is what earns a promotion (ARB-B, §8), so reading it is separated from
# reading policy: a security reviewer signs off on the evidence, and that is a
# different job from authoring the rule the evidence is about.
_reg(_a("arbiter.parity.read", "endpoint", ['admin', 'security'],
        "Agreement between the engine and the control it shadows. The promotion evidence."))
# Regenerating a mirror rewrites the ORACLE the parity report is measured
# against, which is why it is not `policies.write`.
_reg(_a("arbiter.bridge.sync", "endpoint", ['admin'],
        "Regenerates the read-only policies that mirror the legacy controls."))
# Authoring a draft is separated from approving one, and not for tidiness: the
# four-eyes rule has nothing to attach to if one privilege covers both.
_reg(_a("arbiter.drafts.write", "endpoint", ['admin', 'security'],
        "Describes a policy in words and has one drafted. Enacts nothing."))
_reg(_a("arbiter.drafts.approve", "endpoint", ['admin'],
        "Signs off a draft's readback. Never for a draft you wrote, without a cooling-off wait."))
# Upkeep reads the policy set and the decision log and writes NOTHING — it is
# the one arbiter action that cannot change any answer. It is not folded into
# `policies.read` because it also reads decision counts, and it is not folded
# into `decisions.read` because a developer holding neither still needs to be
# told their own policies stopped compiling.
_reg(_a("arbiter.upkeep.read", "endpoint", ['admin', 'security'],
        "Maintenance findings over the live policy set, and HxNexus' reading of them. Changes nothing."))

# F-00 — managing agent principals.
#
# `agents.*` (plural) governs who may MANAGE an agent. It is deliberately not
# `agent.*`, which is the HxArbiter namespace for what an agent may DO. One
# letter apart and on opposite sides of the trust boundary: an agent holds
# `agent.read_case`; the human who created it holds `agents.registry.write`.
# Spelling them the same would make an agent's own capability grantable through
# the platform action catalogue, which is exactly backwards.
_reg(_a("agents.registry.read", "endpoint", ['admin', 'security', 'developer'],
        "Lists agent principals, their kind, model binding and capability ceiling."))
_reg(_a("agents.registry.write", "endpoint", ['admin'],
        "Creates an agent principal and sets its capability ceiling."))
# Suspension is the kill switch: it invalidates every outstanding token for that
# agent on the next call. It is separated from `registry.write` so an incident
# responder can stop an agent without also being able to create one or widen a
# ceiling — the two jobs are minutes apart and should not need the same power.
_reg(_a("agents.status.change", "endpoint", ['admin', 'security'],
        "Suspends, resumes or retires an agent. Suspension kills its live tokens at once."))
# Minting is the privileged half. A token is authority handed to a non-human
# that will act unattended, so it is admin-only and never inherited from
# `registry.read`.
_reg(_a("agents.tokens.issue", "endpoint", ['admin'],
        "Mints a short-lived, capability-scoped token for an agent."))
_reg(_a("agents.tokens.revoke", "endpoint", ['admin', 'security'],
        "Revokes an outstanding agent token. Takes effect on the agent's next call."))

# F-05 — agents as workers. Three actions, split the way the work splits.
#
# Reading the roster is supervision. ENROLLING an agent decides that a
# non-human may hold case work at all, which is a staffing decision and not a
# configuration one. And approving a routing-weight change is separated from
# both, because a system that adjusted its own routing from its own performance
# data would be a feedback loop nobody is steering — the approval is the person
# in that loop.
_reg(_a("agents.workforce.read", "endpoint", ['admin', 'manager', 'security'],
        "The mixed roster: which agents hold work, their load, and their scorecards."))
_reg(_a("agents.workforce.write", "endpoint", ['admin'],
        "Puts an agent on the roster, sets its caps, benches it. A staffing decision."))
_reg(_a("agents.workforce.approve", "endpoint", ['admin', 'manager'],
        "Approves a routing-weight change. The human in the loop, never the scorecard itself."))

# F-01 — the AI decision ledger.
#
# Reading the ledger is separated from reading cases, and that is the whole
# access design: an auditor establishes *how* decisions were made without
# thereby being able to read what they were made about. The ledger stores
# commitments (hashes) rather than content precisely so this separation is real
# and not a promise.
#
# There is no new `ai_auditor` ROLE. Inventing one would mean seeding privileges
# for it across the whole catalogue, and Phase 6 already put role → privilege
# mapping in data: an operator who wants a read-only auditor grants exactly
# `ai_ledger.records.read` to a role of their own naming, with no code change.
_reg(_a("ai_ledger.records.read", "endpoint", ['admin', 'security'],
        "Reads AI decision records and override rates. Governance data, not case content."))
# Recording a verdict is CASE work, not governance work: the person who accepts
# or rejects an AI proposal is the case worker looking at it. Holding
# `records.read` must not imply it — an auditor who could also record verdicts
# could manufacture the oversight they are auditing.
_reg(_a("ai_ledger.verdicts.write", "endpoint", ['admin', 'manager', 'case_worker'],
        "Records what a human decided about one AI proposal. Append-only."))
# Exporting is its own action AND passes through the HxArbiter `export.evidence`
# enforcement point: an auditor download is itself a logged, policy-checked
# decision, which is the property the pack claims about everything else.
_reg(_a("ai_ledger.evidence.export", "endpoint", ['admin', 'security'],
        "Generates an AI governance evidence pack. The Article 22 dossier."))
_reg(_a("ai_ledger.chain.verify", "endpoint", ['admin', 'security'],
        "Recomputes the ledger's seal. Answers whether the record has been edited."))

# F-02 — explainable cost per decision.
#
# Model rates are commercially sensitive in the same way `costing.rates.access`
# already is, and are a separate action from it for one reason: an operator who
# may set the LABOUR rate is not automatically the one who may set what an AI
# provider is charged at, and on a shared install the platform-wide default row
# reaches every tenant that has not priced a model for itself.
_reg(_a("costing.model_rates.access", "endpoint", ['admin'],
        "Reads and sets per-model token prices. Commercially sensitive; the platform row reaches every tenant."))
# Reading a cost breakdown is case-adjacent — it reveals how a case was worked,
# which models touched it and how much human time it took — so it sits with the
# people who already see case analytics rather than with the ledger auditor.
_reg(_a("costing.decisions.read", "endpoint", ['admin', 'manager', 'finance'],
        "Per-decision cost breakdown for a case, and the tenant rollup."))

# F-03 — counterfactual compliance proofs.
#
# THREE segments, and in the `compliance` family: a two-segment name cannot be
# granted from data at all (`_crosses_into_platform_namespace`), which is what
# sent four `risk.*` actions back to be renamed. `proofs.generate` was written
# that way first and the guard caught it.
#
# Generating is separated from reading, and not for tidiness: a proof replays a
# candidate policy across the whole recorded caseload, which is the most
# expensive read this platform offers, and its cohort definition is itself a
# statement about what exists. Reading one back is an auditor's job; producing
# one is an operator's.
_reg(_a("compliance.proofs.generate", "endpoint", ['admin', 'security'],
        "Replays a policy version across the whole recorded cohort and stores the proof."))
_reg(_a("compliance.proofs.read", "endpoint", ['admin', 'security'],
        "Reads a proof and re-derives it. The divergent list is filtered per viewer."))


# F-07 — the sovereign model marketplace.
#
# In the `marketplace` family beside `marketplace.packages.publish`, because it
# is the same trust question about a different payload — and a two-segment name
# could not be granted from data at all.
#
# Publishing and installing are SEPARATE powers, and the separation is the
# whole access story here. Publishing sends this tenant's learned aggregates
# (per-feature means and standard deviations over real cases) outside the
# install. Installing accepts a stranger's. An operator may very reasonably
# want one and not the other, and a single `models.manage` would have made that
# inexpressible.
_reg(_a("marketplace.models.read", "endpoint", ['admin', 'security', 'manager'],
        "Reads model packages, their canary results and their evaluations. What left here, and what we took in."))
_reg(_a("marketplace.models.publish", "endpoint", ['admin'],
        "Prepares, publishes and downloads a model package. Sends this tenant's learned aggregates outside."))
_reg(_a("marketplace.models.install", "endpoint", ['admin'],
        "Imports, evaluates, installs and uninstalls a third-party model. Never activates one."))


# F-08 — self-healing processes.
#
# Three actions, and the split is the design. READING the episode log is
# governance: an auditor establishes what the platform proposed to do to itself
# and what it was allowed to do, without thereby being able to do any of it.
#
# APPROVING is the one human decision in the loop, and it APPLIES — the roadmap
# asks for a one-approval deploy card, and a second "now really do it" step
# would be either a second approval or a rubber stamp.
#
# ROLLING BACK is separate from approving because the person undoing a healing
# change at 3 a.m. is usually not the person who approved it at 2 p.m. They need
# no new authority over config: the bytes written back were pre-signed at
# approval by a named human, which is the whole point of pre-signing them.
_reg(_a("healing.episodes.read", "endpoint", ['admin', 'security', 'manager'],
        "Reads healing episodes, their validation ladders and their outcomes. What the platform proposed to do to itself."))
_reg(_a("healing.episodes.approve", "endpoint", ['admin'],
        "Opens, rejects and APPROVES a healing episode. Approving applies the change to live config."))
_reg(_a("healing.episodes.rollback", "endpoint", ['admin', 'security'],
        "Undoes an applied healing change by replaying its pre-signed snapshot."))


# F-09 — the agent-to-agent case handoff protocol.
#
# Four actions on OUR side. The counterparty's side has none of these: it is
# authenticated by the work order token alone, which is the whole point — an
# external party never holds a role in this platform's vocabulary.
#
# ENROLLING is separated from ISSUING because they are decisions of different
# size. Enrolling pins an organisation's key and sets the ceiling it may EVER be
# granted; issuing hands one case to an already-trusted party for a few hours. A
# dispatcher who may do the second should not thereby be able to add a new
# organisation to the list of who exists.
_reg(_a("handoff.counterparties.read", "endpoint", ['admin', 'security', 'manager'],
        "Reads the counterparty registry and the pinned key fingerprints."))
_reg(_a("handoff.counterparties.write", "endpoint", ['admin', 'security'],
        "Enrols or suspends a counterparty. Pins the key a handoff is authenticated against."))
_reg(_a("handoff.orders.issue", "endpoint", ['admin', 'manager'],
        "Hands one case to an enrolled counterparty for one window, and revokes it."))
_reg(_a("handoff.orders.read", "endpoint", ['admin', 'security', 'manager'],
        "Reads work orders and every action an external party took or was refused."))


# F-10 — offline-sovereign field operations.
#
# DISPATCHING is separated from WRITING the node record, and the split follows
# the blast radius. Registering a node pins a key and fixes the charter — what
# that device may EVER hold; dispatching hands it the cases that charter already
# admits. Ordering a shred sits with the write action, beside the decision that
# created the node in the first place.
#
# RESOLVING a conflict is its own action because it is case work, not fleet
# work: the person choosing between a field value and the hub's is the person
# who understands the case, and they need no authority over nodes to do it.
_reg(_a("field.nodes.read", "endpoint", ['admin', 'security', 'manager'],
        "Reads the field-node registry, charters and staleness."))
_reg(_a("field.nodes.write", "endpoint", ['admin', 'security'],
        "Registers a node, pins its key and charter, and orders a remote shred."))
_reg(_a("field.nodes.dispatch", "endpoint", ['admin', 'manager'],
        "Hands a node its bundle and takes its stream back."))
_reg(_a("field.conflicts.resolve", "endpoint", ['admin', 'manager', 'case_worker'],
        "Decides one field/hub conflict. Case work: nothing sensitive merges itself."))


# 8g (2026-09-29) — outbound case-event webhooks. Every route needed a login
# and nothing else, so a viewer could subscribe an address of their choice to
# every tenant's case events. Admin and developer, as the Studio screen that
# manages them always assumed.
_reg(_a("webhooks.subscriptions.read", "endpoint", ['admin', 'developer'],
        "Lists outbound case-event webhook subscriptions and their deliveries."))
_reg(_a("webhooks.subscriptions.write", "endpoint", ['admin', 'developer'],
        "Creates, changes, tests and deletes outbound case-event webhook subscriptions."))


# Oct item 8f — a CUSTOMER's subject-access export (GDPR Art. 15). Separate
# from reading any one case: it gathers everything the tenant holds about a
# person, across cases, calls, messages, orders and consent, into one file
# that leaves the platform. Administrators, as for the staff export's erasure.
_reg(_a("privacy.customers.export", "endpoint", ['admin'],
        "Exports everything the tenant holds about one customer (GDPR Art. 15)."))


# Oct item 18a — staged rollout of content versions. Requesting and approving
# are separate actions AND separate people (the service refuses the requester
# as approver), the same shape HxDeploy promotion has. Changing the percentage
# is day-to-day operation; closing (promote, roll back, supersede) is the
# decision that sticks, so it sits with the people who approve.
_reg(_a("content.rollouts.read", "endpoint", ['admin', 'devops', 'developer', 'manager'],
        "Reads staged rollouts, their arm counts and their history."))
_reg(_a("content.rollouts.request", "endpoint", ['admin', 'devops', 'developer'],
        "Requests a staged rollout of a new case type version. No effect until approved."))
_reg(_a("content.rollouts.approve", "endpoint", ['admin'],
        "Approves a requested rollout, and may override its strategy and percent."))
_reg(_a("content.rollouts.change", "endpoint", ['admin', 'devops'],
        "Changes a rollout's percentage, or switches a flag or blue-green rollout."))
_reg(_a("content.rollouts.close", "endpoint", ['admin', 'devops'],
        "Promotes, rolls back or supersedes a rollout."))


# F-11 — intent-based process execution.
#
# WRITING a fragment is separated from CONFIRMING a plan, and the gap is the
# supply chain: a fragment is a building block every future plan may be composed
# from, so publishing and verifying one is an authoring decision with a long
# tail. Confirming a plan is a decision about one process, now.
#
# CREATING a plan is separated from confirming it because planning is cheap and
# reversible — it composes, checks and shows — while confirming is the consent
# that binds the hash of what will run.
_reg(_a("intent.fragments.read", "endpoint", ['admin', 'developer', 'manager'],
        "Reads the fragment library and the closed vocabulary a goal spec may name."))
_reg(_a("intent.fragments.write", "endpoint", ['admin', 'developer'],
        "Publishes and verifies a process fragment. Every future plan may be composed from it."))
_reg(_a("intent.plans.read", "endpoint", ['admin', 'developer', 'manager'],
        "Reads composed plans, their ladders and their refusals."))
_reg(_a("intent.plans.create", "endpoint", ['admin', 'manager', 'case_worker'],
        "States an intent and composes a plan. Composes and shows; changes nothing."))
_reg(_a("intent.plans.confirm", "endpoint", ['admin', 'manager'],
        "Confirms a plan and runs it. The confirmation binds the hash of the exact process."))


# F-12 — process-as-negotiation.
#
# SUSPENDING is its own action, and that is the important split. The person
# closing a seam during an incident is not necessarily the person who approved
# the contract, and they must not need to be: a kill switch that requires the
# approver's authority is a kill switch nobody can reach at 3 a.m.
#
# PROPOSING is separated from APPROVING for the ordinary reason — running the
# matcher and exchanging signatures is negotiation, and agreeing to be bound by
# the result is a decision.
_reg(_a("negotiate.contracts.read", "endpoint", ['admin', 'security', 'manager'],
        "Reads joint contracts, their resolved clauses and the anchor exchange."))
_reg(_a("negotiate.contracts.propose", "endpoint", ['admin', 'manager'],
        "Runs the matcher, exchanges signatures and records anchors."))
_reg(_a("negotiate.contracts.approve", "endpoint", ['admin'],
        "Approves a joint contract and moves it through shadow to enforced."))
_reg(_a("negotiate.contracts.suspend", "endpoint", ['admin', 'security', 'manager'],
        "Closes the seam unilaterally. Needs nothing from the counterparty."))


# Item 12 — Customer Engagement (the rep desk).
#
# READING a profile is not READING A FIELD. The 360 shows a masked view; a
# `reveal` unmasks one field with a reason and leaves a record, and it is its own
# action because "may see that this person exists" and "may read their national
# id" are different powers held by different people on most floors.
#
# MERGE is separated from WRITE because it is the one operation that changes what
# a future call shows about somebody else: merging the wrong two people moves one
# person's cases onto another's screen.
#
# `engage.actions.on_behalf` is separate again — typing a case on somebody's
# behalf is an act of attribution, and a rep who may read a 360 is not
# automatically a rep who may raise work in a customer's name.
_reg(_a("engage.profile.read", "endpoint", ['admin', 'manager', 'case_worker'],
        "Reads a customer 360, masked, at the verification tier the call reached."))
_reg(_a("engage.profile.write", "endpoint", ['admin', 'manager', 'case_worker'],
        "Creates profiles, identifiers and party relations."))
_reg(_a("engage.profile.reveal", "endpoint", ['admin', 'manager', 'case_worker'],
        "Unmasks ONE field with a reason. Every reveal is a record; there is no bulk unmask."))
_reg(_a("engage.profile.merge", "endpoint", ['admin', 'manager'],
        "Merges two profiles, reversibly. Moves one person's history onto another's screen."))
_reg(_a("engage.interactions.read", "endpoint", ['admin', 'manager', 'case_worker'],
        "Reads the interaction record — what a call was, and what was promised."))
_reg(_a("engage.interactions.write", "endpoint", ['admin', 'manager', 'case_worker'],
        "Opens a screen-pop, records verification, transfers and wraps up a contact."))
_reg(_a("engage.actions.on_behalf", "endpoint", ['admin', 'manager', 'case_worker'],
        "Raises work in a customer's name, attributed to the rep and stamped with the tier."))
# 2026-09-29, Oct item 2. Recording a person's consent is a legal record about
# them, made on their word over the phone — separate from raising work for them,
# so a floor can let reps do one and not the other.
# 2026-09-29, Oct item 3. Running a backfill reads every portal account and
# case submitter in the tenant — an administrator's act. Deciding a proposal
# creates or enriches a person on the record, which a supervisor may do; a
# MERGE proposal additionally needs `engage.profile.merge`.
# 2026-10-11, Oct item 13 — legal holds. PLACING freezes a case against
# deletion, anonymization, erasure and transfer; RELEASING is what lets it be
# destroyed again, so it is the narrower one: an operator who may protect a
# record has not thereby been given the power to unprotect it. READING the
# matter and the reason is a third thing — a hold names a dispute, so a manager
# does not hold it by default; whoever can read a case is still told THAT it is
# held (`GET /legal-holds/cases/{id}`), which is all their work needs.
_reg(_a("legal.holds.read", "endpoint", ['admin', 'security'],
        "Reads legal holds: which cases are frozen, for which matter, why, and by whom."))
_reg(_a("legal.holds.place", "endpoint", ['admin', 'security'],
        "Places a legal hold on a case: it cannot be deleted, anonymized, erased or moved until released."))
_reg(_a("legal.holds.release", "endpoint", ['admin'],
        "Releases a legal hold, after which retention and erasure apply to the case again."))
# 2026-10-14 — which access group must approve a release (mig 218). Setting it
# when none is set is this privilege; CHANGING or clearing one already set also
# needs membership of that group (or the superadmin), so the person the rule
# constrains cannot simply switch it off. Approving is membership, not a privilege.
_reg(_a("legal.holds.configure", "endpoint", ['admin'],
        "Names the access group whose member must approve a legal hold release."))
# Erasing a customer from the rep desk is irreversible and legal in weight.
_reg(_a("engage.profile.erase", "endpoint", ['admin'],
        "Erases a customer from the rep desk: profile anonymised, identifiers and call numbers removed."))
_reg(_a("engage.backfill.run", "endpoint", ['admin'],
        "Starts a profile backfill scan. It writes proposals only — it applies nothing."))
_reg(_a("engage.backfill.decide", "endpoint", ['admin', 'manager'],
        "Accepts or rejects a backfill proposal; accepting creates or enriches a profile."))
_reg(_a("engage.actions.consent", "endpoint", ['admin', 'manager', 'case_worker'],
        "Records the customer's consent answer given on a verified call, bound to the interaction."))
# 2026-09-17: item 12 P5. Analytics reads every rep's handling time and wrap-up
# mix, which is a supervisor's view of the floor rather than case work; naming
# the tenant's wrap-up codes changes what every rep may record, so it is admin.
_reg(_a("engage.analytics.read", "endpoint", ['admin', 'manager'],
        "Contact-centre numbers for the tenant: handling time, wrap-up mix, repeat contact, FCR."))
_reg(_a("engage.config.write", "endpoint", ['admin'],
        "Names the tenant's wrap-up codes; unset keeps free text."))


# ── 198: case actions ────────────────────────────────────────────────────────
#
# One privilege per action GROUP, not one per action: `close`, `resolve` and
# `cancel` all end a case and there is no coherent grant that permits one and
# refuses the others. Three segments so each is grantable from data rather than
# tied to a role name.
#
# `legacy_roles` is the parity oracle, and it is the roles the routes these
# replace actually required — every one of them gated on `case.update`, whose
# legacy set is what these carry. Widening it here would grant, on upgrade,
# something no route granted before.
_reg(_a("case.actions.update", "endpoint", ['admin', 'manager', 'case_worker'],
        "Move a case's status or priority. The everyday action."))
_reg(_a("case.actions.close", "endpoint", ['admin', 'manager', 'case_worker'],
        "End a case — close, resolve or cancel. Reversible by reopen."))
_reg(_a("case.actions.reopen", "endpoint", ['admin', 'manager', 'case_worker'],
        "Reopen a terminal case and restart its lifecycle workflow."))
_reg(_a("case.actions.assign", "endpoint", ['admin', 'manager', 'case_worker'],
        "Assign or reassign the work on a case."))
_reg(_a("case.actions.note", "endpoint", ['admin', 'manager', 'case_worker'],
        "Append an internal note to the case thread."))
_reg(_a("case.actions.configure", "endpoint", ['admin'],
        "Author the status set and the action configuration a case type runs on."))

# ── §7 answered 2026-09-08: the rest of the vocabulary ───────────────────────
#
# Four more groups, and the splits follow blast radius rather than workflow.
#
# `archive` is separated from `close` because it is the one action after which
# nothing else may happen to the case — an operator who may end a case has not
# thereby been given the power to seal it. `delete` is separated from both and
# is the only DESTRUCTIVE action in the set; it is soft, and it still hides the
# case from everyone. `escalate` is separated because it reaches OUTSIDE the
# case — it fires a tree and notifies people who are not working it, which is
# an act with an audience. `split` CREATES a case, and creating work is not
# implied by being allowed to change some. `transfer` moves who holds the case,
# which is the assign question, so it carries the assign roles.
_reg(_a("case.actions.archive", "endpoint", ['admin', 'manager'],
        "Seal a finished case. After this, nothing further may be done to it."))
_reg(_a("case.actions.delete", "endpoint", ['admin'],
        "Soft-delete a case: hidden from every list and read, history retained."))
_reg(_a("case.actions.escalate", "endpoint", ['admin', 'manager', 'case_worker'],
        "Raise a case up its escalation tree and notify the people it names."))
_reg(_a("case.actions.split", "endpoint", ['admin', 'manager', 'case_worker'],
        "Split part of a case into a new child case."))
_reg(_a("case.actions.transfer", "endpoint", ['admin', 'manager', 'case_worker'],
        "Hand a case to another queue or access group."))

# ── 2026-09-17: bulk STAGE transition ────────────────────────────────────────
#
# Its own privilege, and deliberately NOT `case.actions.update`. Moving one case
# into a stage fires that stage's SLA, its `on_enter_status` and the workflow
# signals for one case; moving five hundred does it five hundred times, and none
# of it is undone by moving them back. The single-case route keeps its gate
# (`case.update` on the case) and is untouched. Admin and manager only: a new
# capability, so no install gains it on upgrade except through these two roles,
# and a case worker who needs it is granted it from data.
_reg(_a("case.actions.bulk_stage", "endpoint", ['admin', 'manager'],
        "Move many cases into a stage at once, after a server-checked preview."))

# ── 2026-09-17: moving a case between tenants ────────────────────────────────
#
# Not `case.actions.transfer`, which every case worker holds and which moves who
# WORKS a case. This moves the case into another company's data. Admin only, and
# holding it is still not enough: a request needs a second holder in the source
# tenant to approve it, and a holder in the destination to accept it.
_reg(_a("case.actions.transfer_tenant", "endpoint", ['admin'],
        "Request, approve, accept or refuse moving a case into another tenant."))

# ── 2026-09-29: payments ─────────────────────────────────────────────────────
#
# Until today every `/payments` route depended on `get_current_user` and nothing
# else — no privilege and no tenant check — so any signed-in user of any role in
# any tenant could record a disbursement on another tenant's case, mark it sent
# (a ledger posting) or refund its payment. Proven in-process before the fix.
# There was no gate to keep parity with, so these follow blast radius: asking
# for money and recording that it is owed are case work; giving money BACK and
# confirming it LEFT are not, and a case worker who does them is granted it
# from data. Connectors and webhook events describe the tenant's payment
# plumbing, not a case.
_reg(_a("payments.requests.read", "endpoint", ['admin', 'manager', 'case_worker', 'finance'],
        "Reads payment requests and disbursements on cases the caller can see."))
_reg(_a("payments.requests.write", "endpoint", ['admin', 'manager', 'case_worker', 'finance'],
        "Asks a customer for a payment on a case step (a checkout link)."))
_reg(_a("payments.refunds.write", "endpoint", ['admin', 'manager', 'finance'],
        "Refunds a succeeded payment, fully or in part."))
_reg(_a("payments.disbursements.write", "endpoint", ['admin', 'manager', 'case_worker', 'finance'],
        "Records a pay-to-customer disbursement on a case step and completes the step."))
_reg(_a("payments.disbursements.execute", "endpoint", ['admin', 'manager', 'finance'],
        "Confirms a disbursement left the bank, which posts it to the ledger."))
_reg(_a("payments.connectors.read", "endpoint", ['admin', 'manager', 'finance'],
        "Lists the tenant's payment connectors (credentials masked) and webhook events."))
_reg(_a("payments.connectors.test", "endpoint", ['admin'],
        "Tests a payment connector's credentials against the provider."))


# ── 2026-09-30: the login-only sweep ─────────────────────────────────────────
#
# A scan of the built route table (every route whose only gate is a login, with
# helper calls followed three deep) found 270 routes. Proven in-process before
# the fix, as a VIEWER: created a platform-wide region, created a work queue
# (stored with NO tenant, so every tenant's Work Center saw it), and listed
# connectors, sync destinations, platform logs, branches and MCP tokens. The
# case-type DELETE took no identity parameter at all: any login hard-deleted a
# case type. There was no gate to keep parity with, so each list follows the
# Studio screen that manages the thing (nav-data.ts) — nobody who reaches the
# screen today loses it — and a write is never wider than its read.
_reg(_a("platform.regions.read", "endpoint", ['admin'],
        "Reads the region registry, region health and the tenant placement map."))
_reg(_a("platform.regions.write", "endpoint", ['admin'],
        "Adds, changes or removes a region, places a tenant, or moves one. Platform operator only."))
_reg(_a("platform.sovereignty.write", "endpoint", ['admin'],
        "Writes a tenant's data-sovereignty rules: where its data may live."))
_reg(_a("integration.sync.read", "endpoint", ['admin', 'integration', 'devops'],
        "Reads outbound data-sync destinations, their mappings, redaction rules and runs."))
_reg(_a("integration.sync.write", "endpoint", ['admin', 'integration'],
        "Creates or changes a destination that copies case data out of the platform."))
_reg(_a("integration.sync.run", "endpoint", ['admin', 'integration'],
        "Runs or tests a sync — sends case data to the destination now."))
_reg(_a("integration.connectors.read", "endpoint", ['admin', 'integration', 'designer', 'developer', 'devops'],
        "Reads connector definitions (credentials masked), call history, events and the DLQ."))
_reg(_a("integration.connectors.write", "endpoint", ['admin', 'integration'],
        "Creates, changes or deletes a connector, or retries a dead-lettered call."))
_reg(_a("integration.connectors.execute", "endpoint", ['admin', 'integration'],
        "Calls a connector's endpoint by hand (test or sandbox execute)."))
_reg(_a("devconn.rules.read", "endpoint", ['admin', 'integration', 'designer', 'developer'],
        "Reads inbound/outbound developer-connector rules, custom connectors and their events."))
_reg(_a("devconn.rules.write", "endpoint", ['admin', 'integration', 'designer'],
        "Creates, changes or deletes a developer-connector rule."))
_reg(_a("design.branches.read", "endpoint", ['admin', 'designer', 'developer', 'devops'],
        "Reads artifact branches, their diffs, reviews, audit and commits."))
_reg(_a("design.branches.write", "endpoint", ['admin', 'designer', 'developer'],
        "Creates, edits, deletes or pulls an artifact branch, or records a commit."))
_reg(_a("design.forms.write", "endpoint", ['admin', 'designer'],
        "Creates, changes or deletes a form definition."))
_reg(_a("design.reference.read", "endpoint", ['admin', 'designer', 'developer'],
        "Reads reference tables (branches, products, price bands) and their rows."))
_reg(_a("design.reference.write", "endpoint", ['admin', 'designer'],
        "Creates, changes or deletes a reference table, or replaces its rows."))
_reg(_a("design.embeds.manage", "endpoint", ['admin'],
        "Approves the sites a form may embed (and whether embeds show on the portal)."))
_reg(_a("casework.queues.write", "endpoint", ['admin', 'manager'],
        "Creates a work queue in the caller's tenant."))
_reg(_a("platform.logs.read", "endpoint", ['admin', 'security', 'designer'],
        "Reads the platform's own service logs and has HxNexus analyse them."))
_reg(_a("compliance.audit.read", "endpoint", ['admin', 'security'],
        "Reads the audit chain's status, anchors and verification."))
_reg(_a("compliance.reports.read", "endpoint", ['admin', 'security'],
        "Lists and downloads generated compliance reports."))
_reg(_a("platform.migration.read", "endpoint", ['admin', 'devops', 'designer'],
        "Reads BPM import and HxMigrate jobs, runs and their results."))
_reg(_a("platform.realtime.read", "endpoint", ['admin', 'designer'],
        "Live websocket connection statistics (the Live Activity screen). Operator only."))
_reg(_a("comms.email.send", "endpoint", ['admin', 'manager', 'case_worker'],
        "Sends an email to any address from the tenant's outbound mailbox."))
_reg(_a("design.graph.sync", "endpoint", ['admin', 'designer'],
        "Rebuilds HxGraph from the database and re-embeds every node with the AI model."))
_reg(_a("platform.migration.run", "endpoint", ['admin', 'devops', 'designer'],
        "Uploads a BPM export or runs a migration that writes case types into the platform."))


def resource_of(action: str) -> str:
    """`compliance.ledger.read` → `compliance.ledger`."""
    return action.rsplit(".", 1)[0]


def verb_of(action: str) -> str:
    """`compliance.ledger.read` → `read`."""
    return action.rsplit(".", 1)[-1]
