"""Privilege authorization Phase 1 — the parity proof.

The exit criterion for Phase 1 is a single claim: **for every action in the
registry and every subject shape, `PrivilegeBackend` decides exactly what
`require_role` decides today.** Everything else in this phase is machinery; this
is the artefact that makes the Phase-3 cutover defensible, because after it a
disagreement is a bug rather than an opinion.

It also pins the two bypasses that are easy to lose in a rewrite — superadmin,
and `is_admin` passing every gate — and the wildcard matching that is the
classic way a privilege system quietly becomes permissive.

Nothing here is enforced in the app yet. These tests would pass identically with
the routers untouched, which is the point of Phase 1.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import itertools

import pytest

from velaris_shared.hxguard.actions import ACTIONS, GATE_ROLES, resource_of, verb_of
from velaris_shared.hxguard.privileges import (
    KNOWN_ROLES,
    SEEDED_ROLE_PRIVILEGES,
    PrivilegeBackend,
    effective_privileges,
    evaluate_privileges,
    legacy_role_gate_allows,
    resource_matches,
)


class _Subject:
    kind = "user"

    def __init__(self, roles, privileges=None):
        self.roles = list(roles)
        self.privileges = privileges
        self.is_admin = "admin" in self.roles


#: Every shape a real subject takes at these gates: no roles, one role, and the
#: role pairs that actually occur (an operator is rarely single-role).
SUBJECT_ROLE_SETS = (
    (),
    *[(r,) for r in KNOWN_ROLES],
    ("viewer", "designer"),
    ("case_worker", "security"),
    ("designer", "devops"),
    ("manager", "viewer", "integration"),
    ("admin", "viewer"),
    ("superadmin", "viewer"),
)


# ── the parity table ──────────────────────────────────────────────

class TestParity:
    @pytest.mark.parametrize("action_name", sorted(ACTIONS))
    def test_action_matches_the_legacy_gate_for_every_subject(self, action_name):
        action = ACTIONS[action_name]
        backend = PrivilegeBackend()
        for roles in SUBJECT_ROLE_SETS:
            expected = legacy_role_gate_allows(roles, action.legacy_roles)
            got = backend.evaluate(_Subject(roles), action_name).allow
            assert got == expected, (
                f"{action_name} with roles={roles!r}: "
                f"require_role({action.legacy_roles!r}) says {expected}, "
                f"PrivilegeBackend says {got}"
            )

    def test_the_table_is_not_vacuous(self):
        """A parity table that only ever compares False to False proves nothing.
        Both answers must actually occur across the matrix."""
        backend = PrivilegeBackend()
        results = {
            backend.evaluate(_Subject(roles), name).allow
            for name in ACTIONS for roles in SUBJECT_ROLE_SETS
        }
        assert results == {True, False}

    def test_every_gate_role_grants_something(self):
        """If a role in the registry granted nothing, its seed is broken."""
        backend = PrivilegeBackend()
        for role in sorted(GATE_ROLES):
            if role == "admin":
                continue
            granted = [n for n in ACTIONS
                       if backend.evaluate(_Subject([role]), n).allow]
            assert granted, f"role {role!r} appears in a gate but grants nothing"


# ── the bypasses, pinned ──────────────────────────────────────────

class TestBypasses:
    def test_superadmin_passes_everything(self):
        backend = PrivilegeBackend()
        for name in ACTIONS:
            d = backend.evaluate(_Subject(["superadmin"]), name)
            assert d.allow and "break-glass" in d.reason

    def test_admin_passes_everything(self):
        backend = PrivilegeBackend()
        assert all(backend.evaluate(_Subject(["admin"]), n).allow for n in ACTIONS)

    def test_designer_gates_have_always_meant_designer_or_admin(self):
        """`require_role("designer")` on the case-variable routes lists only
        designer, but admin passes unconditionally. Seeding it designer-only
        would be a silent tightening at cutover."""
        backend = PrivilegeBackend()
        designer_only = [a for a in ACTIONS.values() if a.legacy_roles == ("designer",)]
        assert designer_only, "expected the case_variables gates"
        for a in designer_only:
            assert backend.evaluate(_Subject(["admin"]), a.name).allow
            assert backend.evaluate(_Subject(["designer"]), a.name).allow

    def test_roles_absent_from_every_gate_grant_nothing(self):
        """viewer/case_worker/staff seed to an empty set. That is not an
        oversight — it is what they can do at these gates.

        `developer` and `finance` LEFT this list in Phase 4 step 2, which is the
        point of the phase rather than a regression: both were named only by
        imperative checks in handler bodies (`marketplace._require_developer_or_
        admin`, `payments._has_finance_access`), so a scan of *dependency* gates
        saw them nowhere and seeded them to nothing. `manager` left it too, via
        `marketplace._can_view`."""
        backend = PrivilegeBackend()
        # `case_worker` left this list in Phase 5: `risk.assessment.read` named it in
        # `ACTION_ROLES`, the case layer's own role table, which no earlier
        # phase's detector could see because it was not a `require_role` gate.
        for role in ("viewer", "staff"):
            assert SEEDED_ROLE_PRIVILEGES[role] == []
            assert not any(backend.evaluate(_Subject([role]), n).allow for n in ACTIONS)

    def test_the_roles_that_phase_4_gave_privileges_to(self):
        """The other half of the assertion above: these three now grant exactly
        the actions their imperative checks named, and nothing else."""
        backend = PrivilegeBackend()
        for role, expected in (
            # 2026-08-16: the omnichannel and RPA actions are the first NEW
            # gates written natively rather than migrated, so these sets grow
            # for the first time by intent rather than by discovery. A bot is
            # developer work; a channel conversation is casework.
            ("developer", {# 2026-09-30, login-only sweep: the branch
                           # screen, and READING connectors a form or rule
                           # binds to. Writing a connector is not theirs.
                           "design.branches.read", "design.branches.write",
                           "integration.connectors.read", "devconn.rules.read",
                           # 2026-10-29: reading reference tables a form binds to.
                           "design.reference.read",
                           "marketplace.packages.publish", "marketplace.catalogue.read",
                           # 2026-09-04: F-11. Authoring a process fragment is
                           # developer work — it is a building block every
                           # future plan may be composed from. Reading plans
                           # comes with it; CONFIRMING one does not, because the
                           # author of a fragment may not be the person who
                           # consents to a process built from it.
                           "intent.fragments.read", "intent.fragments.write",
                           "intent.plans.read",
                           # 2026-09-29: Oct 18a. A developer ships a new case
                           # type version and REQUESTS its rollout; approving,
                           # changing and closing one are not theirs.
                           "content.rollouts.read", "content.rollouts.request",
                           # 2026-09-29, 8g: outbound webhooks were ungated;
                           # the Studio screen that manages them is developer work.
                           "webhooks.subscriptions.read", "webhooks.subscriptions.write",
                           "platform.dbmanager.read", "platform.dbmanager.reauth",
                           "devconn.namespace.register",
                           "bots.definitions.read", "bots.definitions.write",
                           "bots.jobs.read", "bots.jobs.run",
                           # 2026-08-20: the scheduler. Writing a timed action is
                           # developer work and firing one by hand is how you test
                           # it — but `scheduler.schedules.approve` is NOT here,
                           # and must not be: the author of a schedule may never
                           # be the person who puts it into service.
                           "scheduler.schedules.read", "scheduler.schedules.write",
                           "scheduler.schedules.run",
                           # 2026-08-21: HxDeploy + the app registry. Building
                           # and previewing a promotion package is developer
                           # work; `deploy.packages.seal`, `deploy.runs.promote`
                           # and `deploy.runs.approve` are NOT here — sealing
                           # declares what ships, and approving releases it into
                           # another environment.
                           "deploy.environments.read", "deploy.packages.read",
                           "deploy.packages.write", "deploy.runs.read",
                           "deploy.windows.read",
                           "apps.packages.read", "apps.packages.write",
                           # 2026-08-28: HxArbiter. Reading policy and running a
                           # what-if change nothing — a developer needs both to
                           # know why a call was refused. `policies.write`,
                           # `policies.activate` and `namespaces.promote` are
                           # NOT here: authoring a rule the whole platform obeys,
                           # putting it into effect, and switching a namespace
                           # from shadow to enforce are not developer work.
                           "arbiter.policies.read", "arbiter.simulate.run",
                           # 2026-09-01: F-00 agent identity. A developer wiring
                           # a worker needs to see which agents exist and what
                           # each may hold — the slug is what their code names.
                           # Creating one, changing its status and minting it a
                           # token are NOT here: an agent principal, and the
                           # authority handed to it, are an operator's to grant.
                           "agents.registry.read",
                           # 2026-09-01: F-04. A developer rehearses a change
                           # — that is ordinary devops work. Setting the
                           # thresholds and overriding a failed gate are not.
                           "deploy.simulation.run",
                           # 2026-08-21: Calendar. A developer can see who is
                           # away — a build waits on people too — but records
                           # nobody's leave and approves none.
                           "calendar.absences.read",
                           "calendar.events.read", "calendar.events.write",
                           # A developer can see WHICH calendars are subscribed
                           # to — a feed that has been failing is an operational
                           # fact — but subscribes to none, and the URL is a
                           # capability they are not shown.
                           "calendar.subscriptions.read"}),
            ("finance", {"finance.details.read",
                         # 2026-09-01: F-02. Finance reads what a case cost;
                         # it does not set the rates, and it does not gain the
                         # governance ledger.
                         "costing.decisions.read",
                         # 2026-09-29: payments had no gate. Finance holds all of
                         # it but testing connector credentials, which is admin.
                         "payments.requests.read", "payments.requests.write",
                         "payments.refunds.write", "payments.disbursements.write",
                         "payments.disbursements.execute", "payments.connectors.read"}),
            ("manager", {# 2026-09-30, login-only sweep: a supervisor
                         # sets up their floor's work queues.
                         "casework.queues.write", "comms.email.send",
                         "marketplace.catalogue.read", "analytics.reports.read",
                         # 2026-09-29, Oct 18a: a manager sees which version
                         # their customers are getting; changing it is not theirs.
                         "content.rollouts.read",
                         "design.escalation.read", "risk.assessment.read", "risk.outcomes.write",
                         "risk.rules.configure", "risk.scores.review", "casework.records.any",
                         "channels.conversations.read", "channels.conversations.write",
                         "channels.cti.pop", "bots.jobs.read", "bots.jobs.run",
                         # Read-only on the timetable: a manager needs to see what
                         # runs overnight without being able to change it.
                         "scheduler.schedules.read",
                         # Same shape for deployments: a manager can see what was
                         # promoted and what is waiting, and can move nothing.
                         "deploy.runs.read",
                         # Calendar: a manager records and approves their
                         # reports' leave. `calendar.patterns.write` is NOT here
                         # — contracted hours are an admin change.
                         "calendar.absences.read", "calendar.absences.write",
                         "calendar.absences.approve",
                         "calendar.events.read", "calendar.events.write",
                         # 2026-08-27: importing an .ics. Its own action rather
                         # than part of `events.write` — an upload writes many
                         # rows from a file this platform did not produce, and
                         # it can request leave. Subscribing is separated again,
                         # because it makes this SERVER fetch a URL a user chose.
                         "calendar.imports.write",
                         "calendar.subscriptions.read",
                         "calendar.subscriptions.write",
                         # 2026-09-01: F-01. A manager reviews AI proposals for
                         # the same reason a case worker does; the governance
                         # read is still not theirs.
                         "ai_ledger.verdicts.write",
                         # 2026-09-01: F-02. What a case cost is a management
                         # number. Setting the RATES is not — that stays admin.
                         "costing.decisions.read",
                         # 2026-09-02: F-05. A manager supervises the mixed
                         # roster and is the human in the routing-weight loop.
                         # ENROLLING an agent is a staffing decision and stays
                         # admin.
                         "agents.workforce.read", "agents.workforce.approve",
                         # 2026-09-04: F-07. A manager may see what models left
                         # this install and what was taken in. Publishing and
                         # installing both stay admin — one sends this tenant's
                         # learned aggregates outside, the other accepts a
                         # stranger's.
                         "marketplace.models.read",
                         # 2026-09-04: F-08. A manager may read what the
                         # platform proposed to do to itself. Approving and
                         # rolling back stay with admin and security.
                         "healing.episodes.read",
                         # 2026-09-04: F-09. A manager dispatches a case to an
                         # enrolled counterparty and reads the trail. ENROLLING
                         # one — pinning a key, setting the ceiling they may
                         # ever be granted — stays with admin and security.
                         "handoff.counterparties.read", "handoff.orders.issue",
                         "handoff.orders.read",
                         # 2026-09-04: F-10. A manager dispatches to a field
                         # node and reads the fleet; REGISTERING one — pinning
                         # its key, fixing its charter, ordering a shred — stays
                         # with admin and security.
                         "field.nodes.read", "field.nodes.dispatch",
                         "field.conflicts.resolve",
                         # 2026-09-04: F-11. A manager reads the fragment
                         # library, plans, and CONFIRMS — the consent that binds
                         # the hash. Authoring a fragment stays with developers.
                         "intent.fragments.read", "intent.plans.read",
                         "intent.plans.create", "intent.plans.confirm",
                         # 2026-09-04: F-12. A manager negotiates and may close
                         # the seam; APPROVING a contract — agreeing to be bound
                         # by it — stays with admin.
                         "negotiate.contracts.read", "negotiate.contracts.propose",
                         "negotiate.contracts.suspend",
                         # 2026-09-04: item 12. A manager does everything a rep
                         # does, and may MERGE two profiles.
                         "engage.profile.read", "engage.profile.write",
                         "engage.profile.reveal", "engage.profile.merge",
                         "engage.interactions.read", "engage.interactions.write",
                         "engage.actions.on_behalf", "engage.actions.consent",
                         # Oct item 3: a manager decides backfill proposals;
                         # starting a scan stays with admin.
                         "engage.backfill.decide",
                         # 2026-09-29: payments had no gate at all. A manager
                         # does all of it except testing a connector's
                         # credentials against the provider.
                         "payments.requests.read", "payments.requests.write",
                         "payments.refunds.write", "payments.disbursements.write",
                         "payments.disbursements.execute", "payments.connectors.read",
                         # 2026-09-07: migration 198. A manager acts on cases,
                         # so they hold the five action privileges the six
                         # removed routes required — this is the legacy role set
                         # of `case.update`, unwidened. `case.actions.configure`
                         # is NOT here: authoring a tenant's status set decides
                         # what every case worker may do next, which is an
                         # administrator's job rather than case work.
                         "case.actions.update", "case.actions.close",
                         "case.actions.reopen", "case.actions.assign",
                         "case.actions.note",
                         # 2026-09-08, §7 answered. A manager may seal a
                         # finished case, raise one up its escalation tree,
                         # split work out of it and hand it to another queue or
                         # group. `delete` is NOT here — hiding a case from
                         # everyone is an administrator's act.
                         "case.actions.archive", "case.actions.escalate",
                         "case.actions.split", "case.actions.transfer",
                         # 2026-09-17. A manager moves a selection of cases
                         # through a stage. A case worker does not by default:
                         # it fires SLAs and workflow steps per case.
                         "case.actions.bulk_stage",
                         # 2026-09-17: item 12 P5. A supervisor reads the
                         # floor's numbers; naming the wrap-up codes is admin.
                         "engage.analytics.read"}),
            ("case_worker", {"risk.assessment.read",
                             # 2026-09-30, login-only sweep: writing to a
                             # customer is case work; a viewer does not.
                             "comms.email.send",
                             "channels.conversations.read", "channels.conversations.write",
                             "channels.cti.pop", "bots.jobs.read", "bots.jobs.run",
                             # Their own leave, and the team's availability. They
                             # approve nothing.
                             "calendar.absences.read", "calendar.absences.write",
                             "calendar.events.read", "calendar.events.write",
                             # Their own calendar, imported from their own
                             # Outlook export — bounded by `_may_write_for`.
                             "calendar.imports.write",
                             "calendar.subscriptions.read",
                             "calendar.subscriptions.write",
                             # 2026-09-01: F-01. The person who accepts or
                             # rejects an AI proposal is the case worker looking
                             # at it. Recording that is case work — it does NOT
                             # come with `ai_ledger.records.read`, which is the
                             # auditor's governance surface.
                             "ai_ledger.verdicts.write",
                             # 2026-09-04: F-10. Choosing between a field value
                             # and the hub's is case work — the person who
                             # understands the case decides it, and needs no
                             # authority over the node fleet to do so.
                             "field.conflicts.resolve",
                             # 2026-09-04: F-11. Stating an intent composes and
                             # shows a plan; it changes nothing until somebody
                             # with `plans.confirm` agrees to it.
                             "intent.plans.create",
                             # 2026-09-04: item 12. The rep desk is case work —
                             # the person on the call reads the 360, verifies,
                             # reveals a field with a reason, and raises work in
                             # the customer's name. MERGE is not here: moving one
                             # person's history onto another's screen is a
                             # supervisor's decision.
                             "engage.profile.read", "engage.profile.write",
                             "engage.profile.reveal",
                             "engage.interactions.read",
                             "engage.interactions.write",
                             "engage.actions.on_behalf", "engage.actions.consent",
                             # 2026-09-29: asking for a payment and recording a
                             # disbursement are case work; refunding and
                             # confirming money LEFT are not.
                             "payments.requests.read", "payments.requests.write",
                             "payments.disbursements.write",
                             # 2026-09-07: migration 198. Each of the five
                             # carries the legacy role set of `case.update` —
                             # what the six removed routes actually required —
                             # so a case worker holds exactly what they held
                             # before. Widening here would have granted, on
                             # upgrade, something no route granted before.
                             # `case.actions.configure` is deliberately absent:
                             # authoring a tenant's status set decides what
                             # every case worker may do next, which is an
                             # administrator's job rather than case work.
                             "case.actions.update", "case.actions.close",
                             "case.actions.reopen", "case.actions.assign",
                             "case.actions.note",
                             # 2026-09-08. A case worker escalates, splits and
                             # transfers — all ordinary case work. `archive`
                             # (sealing) and `delete` (hiding) are not: both
                             # take a case away from everybody else.
                             "case.actions.escalate", "case.actions.split",
                             "case.actions.transfer"}),
        ):
            granted = {n for n in ACTIONS if backend.evaluate(_Subject([role]), n).allow}
            assert granted == expected, f"{role}: {granted ^ expected}"


# ── fail-closed ───────────────────────────────────────────────────

class TestFailClosed:
    def test_unknown_action_is_denied(self):
        d = PrivilegeBackend().evaluate(_Subject(["admin"]), "does.not.exist")
        assert not d.allow and "unknown action" in d.reason

    def test_unknown_action_is_denied_even_for_superadmin(self):
        """Fail-closed outranks break-glass: an action that does not exist
        cannot be granted, or a typo becomes a capability."""
        d = PrivilegeBackend().evaluate(_Subject(["superadmin"]), "does.not.exist")
        assert not d.allow

    def test_a_non_user_subject_is_denied(self):
        class Machine(_Subject):
            kind = "connector"
        assert not PrivilegeBackend().evaluate(Machine(["admin"]), next(iter(ACTIONS))).allow

    def test_the_backend_never_raises(self):
        backend = PrivilegeBackend()
        for subject in (_Subject([]), _Subject(None or []), _Subject(["admin"], privileges=None)):
            for name in ("", "x", "a.b.c", next(iter(ACTIONS))):
                assert isinstance(backend.evaluate(subject, name).allow, bool)


# ── wildcard matching ─────────────────────────────────────────────

class TestWildcards:
    def test_a_wildcard_matches_whole_segments_only(self):
        assert resource_matches("compliance.*", "compliance.ledger")
        assert resource_matches("compliance.*", "compliance.dr")
        assert not resource_matches("compliance.*", "compliance_admin.ledger")
        assert not resource_matches("compliance.*", "compliancex.ledger")

    def test_bare_wildcard_matches_everything(self):
        assert all(resource_matches("*", resource_of(n)) for n in ACTIONS)

    def test_a_prefix_wildcard_never_matches_a_shorter_resource(self):
        assert not resource_matches("compliance.ledger.*", "compliance")

    @pytest.mark.parametrize("prefix", ["identity", "platform", "compliance", "portal"])
    def test_a_domain_wildcard_grants_only_that_domain(self, prefix):
        privs = [{"resource": f"{prefix}.*", "actions": ["*"]}]
        for name in ACTIONS:
            expected = resource_of(name).split(".")[0] == prefix
            assert evaluate_privileges(privs, name) is expected, name


# ── explicit deny ─────────────────────────────────────────────────

class TestDeny:
    def test_deny_beats_allow_regardless_of_order(self):
        """'administers everything except identity' is unexpressible without
        this, and some gates cannot migrate cleanly until it exists."""
        target = "identity.users.write"
        deny = {"resource": "identity.*", "actions": ["*"], "effect": "deny"}
        allow = {"resource": "*", "actions": ["*"]}
        assert not evaluate_privileges([allow, deny], target)
        assert not evaluate_privileges([deny, allow], target)

    def test_deny_is_scoped_to_what_it_names(self):
        deny = {"resource": "identity.*", "actions": ["*"], "effect": "deny"}
        allow = {"resource": "*", "actions": ["*"]}
        assert evaluate_privileges([allow, deny], "compliance.ledger.read")

    def test_no_seeded_role_uses_deny(self):
        """The capability exists for Phase 3; nothing depends on it in Phase 1,
        so it cannot change any current answer."""
        for role, privs in SEEDED_ROLE_PRIVILEGES.items():
            assert all("effect" not in p for p in privs), role


# ── registry hygiene ──────────────────────────────────────────────

class TestRegistry:
    def test_every_action_is_well_formed(self):
        for name, a in ACTIONS.items():
            assert a.name == name
            assert len(name.split(".")) >= 2, name
            assert name.islower() and " " not in name, name
            assert a.summary.strip(), name
            assert a.kind in ("endpoint", "module"), name

    def test_the_registry_covers_the_phase_0_inventory(self):
        """49 endpoint actions from Phase 0, 19 from Phase 3.5's
        `require_admin()` gates, 16 from Phase 4 step 2's imperative checks, and
        6 from the 2026-08-15 sweep for routes that had **no gate at all** —
        the population no previous phase could see, because a detector looking
        for gates cannot find their absence — and 15 from Phase 5, which
        migrated `RbacBackend.ACTION_ROLES`, the case layer's own role table,
        plus the ReBAC admin/manager bypass.

        The module count moves 12 → 17: Phase 4 added five whole-module gates
        that were written as statements rather than as router dependencies
        (hxdbmigrate, hxevolve, hxreplay, hxstream, hxdbmanager, marketplace
        admin). A change here is the most consequential kind — a module action
        governs every route in its file — so it is pinned exactly and moves only
        with a phase."""
        endpoint = [a for a in ACTIONS.values() if a.kind == "endpoint"]
        module = [a for a in ACTIONS.values() if a.kind == "module"]
        assert len(endpoint) == 49 + 19 + 16 + 6 + 15 + 4 + 5 + 6 + 1 + 4 + 14 + 6 + 1 + 2 + 7 + 4 + 1 + 5 + 4 + 2 + 2 + 3 + 3 + 3 + 3 + 4 + 4 + 5 + 4 + 7 + 6 + 5 + 2 + 1 + 1 + 1 + 2 + 1 + 7 + 1 + 2 + 1 + 5 + 2 + 1 + 23 + 3 + 1 + 2 + 1, (
            f"{len(endpoint)} endpoint actions — expected 49 (Phase 0) + 19 "
            f"(Phase 3.5) + 16 (Phase 4 step 2) + 6 (ungated sweep) + 15 "
            f"(Phase 5: the case layer's ACTION_ROLES table + the ReBAC bypass) "
            f"+ 4 (enterprise identity) + 5 (omnichannel channels.*) "
            f"+ 6 (RPA bots.*) + 1 (portal.customers.write — staff-created "
            f"customer accounts, the only path to one once self-registration "
            f"is switched off) + 4 (scheduler.schedules.* — read, write, approve "
            f"and a manual run; approve is separate from write for the same "
            f"reason it is on bots) + 14 (deploy.* and apps.* — the promotion "
            f"path had no privilege gate of any kind: 21 and 13 endpoints behind "
            f"a bare get_current_user, so any authenticated user could register a "
            f"target environment, promote to production and approve their own run) "
            f"+ 4 (calendar.* — Calendar: absence is personal data before it is "
            f"a scheduling input, so read/write/approve are split and the kind "
            f"and note are filtered per caller) + 2 (calendar.events.* — team "
            f"meetings, which HxMeet could not hold because its sessions are "
            f"case-scoped) + 7 (arbiter.* — HxArbiter ARB-A: reading policy is "
            f"split from writing it, writing from ACTIVATING it (a draft is not "
            f"a decision, and four-eyes has nothing to attach to if the two are "
            f"one privilege), and promoting a namespace ceiling — the control "
            f"that turns shadow into enforcement across a whole class of actions "
            f"at once — is admin-only and separate from all three) + 4 "
            f"(arbiter.* — ARB-B..F: reading PARITY is separated from reading "
            f"policy because a reviewer signs off on the evidence, which is a "
            f"different job from authoring the rule it is about; regenerating a "
            f"legacy MIRROR is separate again because it rewrites the oracle "
            f"the evidence is measured against; and drafting a policy in words "
            f"is separated from APPROVING one, or four-eyes has nothing to "
            f"attach to) + 1 (arbiter.upkeep.read — ARB-I: maintenance "
            f"findings over the live policy set. Read-only in the strongest "
            f"sense, and not folded into `policies.read` because it also "
            f"counts decisions, nor into `decisions.read` because somebody "
            f"holding neither still needs telling that their policies stopped "
            f"compiling) + 5 (agents.* — F-00 agent identity: reading the "
            f"registry is split from writing it; SUSPENDING an agent is split "
            f"from both, so an incident responder can stop one at 3am without "
            f"also being able to create one or widen a ceiling; and minting a "
            f"token — authority handed to something that acts unattended — is "
            f"admin-only and separate from revoking one) + 4 (ai_ledger.* — F-01 "
            f"AI decision ledger: reading the ledger is separated from every "
            f"case action, so an auditor establishes HOW decisions were made "
            f"without thereby reading what they were made about; recording a "
            f"VERDICT is case work and is not implied by that read, or an "
            f"auditor could manufacture the oversight they are auditing; and "
            f"exporting the dossier and verifying the seal are their own "
            f"actions) + 2 (costing.* — F-02 cost per decision: setting a "
            f"MODEL's token price is separate from the LABOUR rate, because "
            f"the platform-wide default row is inherited by every tenant that "
            f"has not priced that model itself; and reading a cost breakdown "
            f"is case-adjacent — it shows how a case was worked — so it sits "
            f"with the people who already see case analytics) + 2 "
            f"(compliance.proofs.* — "
            f"F-03 counterfactual compliance proofs: GENERATING one replays a "
            f"candidate policy across the whole recorded caseload, which is "
            f"the most expensive read this platform offers and whose cohort "
            f"definition is itself a statement about what exists; reading one "
            f"back is an auditor's job and producing one is an operator's) + 3 "
            f"(deploy.simulation.* — F-04's gate: RUNNING a rehearsal is "
            f"ordinary devops work; CONFIGURING the thresholds decides what "
            f"every future promotion must clear; and OVERRIDING a failed gate "
            f"is the one control that lets a change through after its "
            f"rehearsal said not to, so bypassing the gate and approving the "
            f"deployment are two grants rather than one) + 3 "
            f"(agents.workforce.* — F-05: reading the mixed roster is "
            f"supervision; ENROLLING an agent decides that a non-human may "
            f"hold case work at all, which is a staffing decision; and "
            f"approving a routing-weight change is separated from both, "
            f"because a system adjusting its own routing from its own "
            f"performance data is a feedback loop nobody is steering) + 3 "
            f"(marketplace.models.* — F-07 the sovereign model marketplace: "
            f"PUBLISHING sends this tenant's learned aggregates — the "
            f"per-feature means and standard deviations over real cases — "
            f"outside the install, while INSTALLING accepts a stranger's, and "
            f"an operator may very reasonably want one and not the other; "
            f"reading what left here and what was taken in is an auditor's "
            f"question and implies neither power) + 3 "
            f"(healing.episodes.* — F-08 self-healing: READING the episode log "
            f"is governance, and establishes what the platform proposed to do "
            f"to itself without thereby being able to do any of it; APPROVING "
            f"is the one human decision in the loop AND applies the change, "
            f"because a second 'now really do it' step would be either a "
            f"second approval or a rubber stamp; and ROLLING BACK is separate "
            f"because the person undoing a healing change at 3 a.m. is usually "
            f"not the person who approved it at 2 p.m.) + 4 "
            f"(handoff.* — F-09 agent-to-agent handoff, on OUR side only: the "
            f"counterparty's side is authenticated by the work order token "
            f"alone, because an external party never holds a role in this "
            f"platform's vocabulary. ENROLLING is separated from ISSUING "
            f"because they are decisions of different size — enrolling pins an "
            f"organisation's key and sets the ceiling it may EVER be granted, "
            f"while issuing hands one case to an already-trusted party for a "
            f"few hours, and a dispatcher who may do the second should not "
            f"thereby be able to add a new organisation to the list of who "
            f"exists) + 4 "
            f"(field.* — F-10 offline field operations: registering a node pins "
            f"a key and fixes the CHARTER — what that device may ever hold — "
            f"while dispatching hands it cases that charter already admits, so "
            f"the two follow the blast radius rather than the workflow; and "
            f"RESOLVING a field/hub conflict is case work rather than fleet "
            f"work, so the person who understands the case needs no authority "
            f"over nodes to decide it) + 5 "
            f"(intent.* — F-11 intent-based execution: WRITING a fragment is "
            f"separated from confirming a plan because a fragment is a building "
            f"block every future plan may be composed from — an authoring "
            f"decision with a long tail — while confirming is a decision about "
            f"one process now; and CREATING a plan is separated from confirming "
            f"it because planning composes and shows and changes nothing, while "
            f"confirming is the consent that binds the hash of what will run) "
            f"+ 4 (negotiate.* — F-12 process-as-negotiation: SUSPENDING is its "
            f"own action, and that is the important split — the person closing "
            f"a seam during an incident is not necessarily the person who "
            f"approved the contract, and a kill switch that requires the "
            f"approver's authority is a kill switch nobody can reach at 3 a.m.) "
            f"+ 5 (case.actions.* — §7 answered 2026-09-08: archive is "
            f"separated from close because it is the action after which nothing "
            f"else may happen; delete is the only destructive one and is "
            f"soft; escalate reaches OUTSIDE the case and notifies people not "
            f"working it; split CREATES a case, which is not implied by being "
            f"allowed to change one; transfer moves who holds it) "
            f"+ 6 (case.actions.* — migration 198: one privilege per action "
            f"GROUP rather than one per action, because `close`, `resolve` and "
            f"`cancel` all end a case and no coherent grant permits one and "
            f"refuses the others; and `configure` is separate from all five "
            f"because authoring a tenant's status set decides what every case "
            f"worker may do next, which is not itself case work) "
            f"+ 7 (engage.* — item 12, the rep desk: READING a profile is not "
            f"reading a FIELD, so `reveal` is its own action — 'may see that "
            f"this person exists' and 'may read their national id' are held by "
            f"different people on most floors; MERGE is separate because it is "
            f"the one operation that moves one person's history onto another's "
            f"screen; and raising work in a customer's NAME is an act of "
            f"attribution a 360 reader does not automatically hold) "
            f"+ 2 (audit.stream.* — market gap A3, streaming security events "
            f"to a SIEM: READ is separated from WRITE because seeing where "
            f"the audit goes, and whether it is arriving, is a security "
            f"reviewer's job, while POINTING it somewhere else decides who "
            f"sees the platform's security events at all — the one change "
            f"that can make an install go quiet without anything appearing "
            f"to break) "
            f"+ 1 (security.shield.respond — roadmap #25: HxShield computed an "
            f"action for every incident since it shipped and nothing ever "
            f"performed one. Approving a response suspends an account or kills "
            f"every session, which is not the same job as reading the incident "
            f"queue that `security.shield.access` already covers) "
            f"+ 1 (case.actions.bulk_stage — 2026-09-17: moving many cases "
            f"into a stage fires SLAs, on_enter_status and workflow signals "
            f"per case, so it is not implied by `case.update` on one case) "
            f"+ 1 (ai.index_queue.read — 2026-09-17: the tenant-wide document "
            f"index queue names documents across every case, so it is not the "
            f"per-case listing's case access) "
            f"+ 2 (engage.analytics.read, engage.config.write — item 12 P5: "
            f"floor analytics is a supervisor's view; naming wrap-up codes "
            f"changes what every rep may record) "
            f"+ 1 (case.actions.transfer_tenant — 2026-09-17: moving a case into "
            f"another tenant's data is not `transfer`, which every case worker "
            f"holds; admin only, and still needs a second admin and the "
            f"destination's acceptance) "
            f"+ 7 (payments.* — 2026-09-29: every /payments route had "
            f"authentication and nothing else, so any signed-in user in any "
            f"tenant could disburse on, mark sent or refund another tenant's "
            f"case; asking for and recording money is case work, giving it "
            f"back and confirming it left are not) "
            f"+ 1 (engage.actions.consent — Oct item 2: recording a person's "
            f"consent on their word over the phone is a legal record about them, "
            f"not the same act as raising work in their name) "
            f"+ 2 (engage.backfill.* — Oct item 3: RUNNING a scan reads every "
            f"portal account and case submitter in the tenant, an administrator's "
            f"act; DECIDING a proposal creates or enriches one person, which a "
            f"supervisor may do) "
            f"+ 1 (engage.profile.erase — 2026-09-29: erasing a customer from the "
            f"rep desk is irreversible and legal in weight; admin only) "
            f"+ 5 (content.rollouts.* — 2026-09-29, Oct 18a staged rollout: "
            f"requesting and approving are separate actions and separate people; "
            f"changing a percentage is operation, closing one is the decision) "
            f"+ 2 (webhooks.subscriptions.* — 2026-09-29, 8g: every /webhooks route "
            f"needed a login and nothing else; a viewer could subscribe to every "
            f"tenant's case events) "
            f"+ 1 (privacy.customers.export — 2026-09-29, 8f: a customer's GDPR "
            f"Art. 15 export gathers everything the tenant holds about a person "
            f"into one file that leaves the platform; admin only) "
            f"+ 23 (the login-only sweep — 2026-09-30: 270 routes needed a login "
            f"and nothing else; a viewer created platform regions and tenantless "
            f"work queues and read connectors, sync destinations and platform "
            f"logs. Each list follows the Studio screen that manages the thing) "
            f"+ 3 (legal.holds.* — 2026-10-11, Oct item 13: PLACING a hold protects "
            f"a record and RELEASING one is what lets it be destroyed again, so "
            f"release is the narrower; READING a hold shows the dispute it names) "
            f"+ 1 (legal.holds.configure — 2026-10-14, mig 218: naming the access "
            f"group whose member must approve a release decides who can free a "
            f"record at all; changing a group already named also needs membership "
            f"of it, so the person the rule binds cannot switch it off) "
            f"+ 2 (design.reference.* — 2026-10-29, Form Builder v2 step 3: READING "
            f"a reference table is design work a developer also needs; WRITING "
            f"one changes the options and filled values of every form using it) "
            f"+ 1 (design.embeds.manage — 2026-10-29, the embed field: APPROVING a site "
            f"decides whose page runs inside every form of the tenant, and whether it "
            f"reaches anonymous portal visitors; admin only, not the designer who "
            f"places an embed)")
        assert len(module) == 18, (
            f"{len(module)} module actions — 12 from Phase 0, 6 from Phase 4")

    def test_every_action_names_at_least_one_role(self):
        """An action with no legacy roles has no parity oracle, so it would be
        migrated on nothing but hope."""
        for name, a in ACTIONS.items():
            assert a.legacy_roles, name

    def test_module_actions_use_the_access_verb(self):
        """A router gate fires before the handler is known, so it cannot be read
        or write. Naming one `.read` would promise a split that does not exist."""
        for a in ACTIONS.values():
            if a.kind == "module":
                assert verb_of(a.name) == "access", a.name

    def test_read_actions_outnumber_nothing_by_accident(self):
        """33 of the 49 endpoint actions' gates are read-only in the inventory;
        the registry must keep enough read verbs to make the read/write split
        the real deliverable it is."""
        reads = [a for a in ACTIONS.values()
                 if a.kind == "endpoint" and verb_of(a.name) == "read"]
        assert len(reads) >= 12, f"only {len(reads)} read actions — split lost?"


# ── no behaviour change ───────────────────────────────────────────

class TestCutoverLedger:
    """Phase 3 is under way, so "nothing changed" is no longer the assertion.
    What replaces it is a ledger: a router is either fully migrated or fully
    not, and moving one is an explicit edit to `MIGRATED_ROUTERS`.

    The guard this replaces caught its own author twice — once when Phase 2
    added an endpoint, once when Phase 3 began. Keeping that property matters
    more than keeping the exact number."""

    def test_migrated_routers_have_no_role_gates_left(self):
        # `find_router` looks across every service, not just this one: seven of
        # these routers moved to analytics-, hxgraph- and hxmigrate-service at
        # 13c-e, and a path that assumed one service would have turned this
        # guard into a FileNotFoundError rather than a check.
        from velaris_shared.hxguard.actions import MIGRATED_ROUTERS

        from case_service.hxguard_ledger.legacy_sites import find_router
        for name in sorted(MIGRATED_ROUTERS):
            src = find_router(name).read_text()
            assert "require_role(" not in src, (
                f"{name} is listed as migrated but still has role gates")
            assert "hxguard.guard(" in src, f"{name} has no guard() calls"

    def test_every_router_that_had_a_gate_is_in_the_ledger(self):
        """A router losing its gates without joining the ledger is how a module
        silently loses authorization — the failure this whole phase is built to
        make impossible."""
        from velaris_shared.hxguard.actions import (
            ACTION_BY_GATE, MIGRATED_ROUTERS, MODULE_ACTION_BY_FILE)
        expected = {g.split("::")[0] for g in ACTION_BY_GATE} | set(MODULE_ACTION_BY_FILE)
        assert expected == set(MIGRATED_ROUTERS), (
            f"ledger drift: {expected ^ set(MIGRATED_ROUTERS)}")

    def test_no_role_gate_remains_anywhere_in_the_api(self):
        """Phase 3 completed 2026-08-13: all 110 gates migrated. Phase 3.5 added
        the 56 `require_admin()` gates the original grep could not see.

        **The gate spellings are listed, not pattern-matched, deliberately.**
        Every under-count in this migration came from a detector that knew one
        spelling and not the next: `require_role(` hid the router-level
        `Depends(` gates, then it hid `require_admin()`. A new alias must be
        added here by whoever introduces it."""
        import re
        from pathlib import Path
        # The lookbehind matters: a router-local `_require_admin(user)` is an
        # imperative check tracked by the legacy-site ledger below, not a
        # dependency gate, and matching it here would make this test unfixable.
        gate = re.compile(r"(?<![A-Za-z0-9_])(require_role|require_admin)\(")
        from case_service.hxguard_ledger.legacy_sites import ROUTER_DIRS
        offenders = {f.name: len(gate.findall(f.read_text()))
                     for d in ROUTER_DIRS if d.is_dir()
                     for f in d.glob("*.py") if gate.search(f.read_text())}
        assert not offenders, f"role gates reappeared: {offenders}"

    def test_every_gate_became_a_guard(self):
        from case_service.hxguard_ledger.legacy_sites import ROUTER_DIRS
        guards = sum(f.read_text().count("hxguard.guard(")
                     for d in ROUTER_DIRS if d.is_dir() for f in d.glob("*.py"))
        assert guards >= 166, f"only {guards} guard() calls for 166 migrated gates"


class TestLegacySiteLedger:
    """Phase 3 migrated the *declarative* gates. The *imperative* ones — a
    helper called inside a handler body that reads `user.is_admin` and raises
    403 itself — were never counted, and there were more of them than there were
    gates: 205 sites, 164 of them routed endpoints.

    **Phase 4 step 2 migrated them and the ledger dropped to 75.** It is not
    empty and should not become empty: what remains is superadmin identity
    tests, display serialization, one deliberate literal role match, and
    case-level `has_privilege` calls — see the header of `legacy_sites.txt`.
    The ledger's job now is to keep that residue reviewed, so a genuinely new
    role-name decision cannot slip in among things that merely look like one."""

    def test_the_scan_matches_the_recorded_population(self):
        from case_service.hxguard_ledger.legacy_sites import EXPECTED_SITES, scan
        found = {f"{s.file}::{s.function}" for s in scan()}
        new = found - EXPECTED_SITES
        gone = EXPECTED_SITES - found
        assert not new, (
            f"{len(new)} new role-name decision site(s) — add an hxguard action "
            f"instead, or record them in legacy_sites.txt: {sorted(new)[:8]}")
        assert not gone, (
            f"{len(gone)} recorded site(s) no longer decide on role names — "
            f"delete their lines from legacy_sites.txt: {sorted(gone)[:8]}")

    def test_the_ledger_is_not_empty_or_vacuous(self):
        """A scan that silently matched nothing would pass the test above while
        proving nothing — the same vacuity trap as the Phase-1 parity table."""
        from case_service.hxguard_ledger.legacy_sites import scan, summary
        s = summary()
        assert s["sites"] > 40, s
        assert s["endpoints"] > 0 and s["helpers"] > 0, s
        assert any(x.direct for x in scan()), "no site reads a signal directly"

    def test_migrated_routers_can_still_appear(self):
        """Being in MIGRATED_ROUTERS is about gates, not bodies. `auth_real.py`
        is migrated *and* still decides on `superadmin` inside handlers — both
        are true, and conflating them is how the follow-up work got lost the
        first time."""
        from velaris_shared.hxguard.actions import MIGRATED_ROUTERS
        from case_service.hxguard_ledger.legacy_sites import scan
        files = {s.file for s in scan()}
        assert files & set(MIGRATED_ROUTERS), (
            "expected at least one migrated router to still have imperative checks")

    def test_the_four_phase35_routers_left_the_ledger_clean(self):
        """storefront/checkout/testsuite/hxtest had 56 gates and no imperative
        checks; after the cutover they should contribute no *unguarded*
        endpoint sites."""
        from case_service.hxguard_ledger.legacy_sites import scan
        for name in ("storefront.py", "checkout.py", "testsuite.py", "hxtest.py"):
            unguarded = [s for s in scan()
                         if s.file == name and s.route and not s.also_guarded]
            assert not unguarded, (
                f"{name}: {len(unguarded)} endpoint(s) still decide on role "
                f"names with no guard: {[s.function for s in unguarded][:5]}")
