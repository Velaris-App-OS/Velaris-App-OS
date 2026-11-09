"""The closed vocabulary of case actions.

Design: docs/Future/Done/CASE-ACTIONS-STATUS-DESIGN.md

**Why this is a closed registry and the status set is not.** A status is a
label: the customer's process gives it meaning, and there is no reason the
platform should own the word `awaiting_customer`. An action is *behaviour* —
`escalate` fires an escalation tree and notifies, and **you cannot infer that
from the word "escalate"**. A system that tried would be the HxArbiter injection
failure one layer up: there, an operator-written policy *name* steered the
authoring model, and the fix was a closed output vocabulary, not a better
prompt. Same fix here.

So: an unknown status is a configuration question. **An unknown action is a
refusal.**

What a customer may change is presentation and reach — label, visibility, order,
which privilege gates it, which statuses it is offered from. That lives in
`case_action_config`, validated against this registry at write time.

`effects` exists so that a person approving a bulk run over 400 cases can be
shown what is about to happen without reading a handler. It is the same reason
F-11's fragments carry `CONSEQUENTIAL`.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


#: Every effect an action may declare. Closed, like every other vocabulary a
#: decision in this platform depends on.
EFFECTS: frozenset[str] = frozenset({
    "status",       # changes case.status
    "priority",     # changes case.priority
    "assignment",   # creates or moves a case_assignments row
    "note",         # appends to the case thread
    "sla",          # pauses, resumes or escalates an SLA
    "notify",       # sends someone something
    "relationship", # links or unlinks cases
    "create",       # brings a new case into existence
    "delete",       # removes or hides a case
})


@dataclass(frozen=True)
class ActionDef:
    """One thing an operator can do to a case."""

    #: Stable identifier. Appears in the URL, in `case_action_config.action`,
    #: and in every audit row — so it is renamed only by a migration.
    name: str
    #: What an operator sees, before any per-tenant override.
    label: str
    #: HxGuard PLATFORM action — a role/privilege question ("may this kind of
    #: operator ever do this"). Three segments so it can be granted from data.
    #:
    #: It is NOT the whole gate. `hxguard.check()` routes an action to exactly
    #: ONE backend: names in `CASE_ACTIONS` go to ReBAC, names in
    #: `PLATFORM_ACTIONS` go to the privilege backend. `case.actions.*` are
    #: platform actions, so checking only this would answer "does this operator
    #: hold the role" and **never** "does this operator have any relationship to
    #: THIS case" — which is what the six routes this replaced actually asked.
    #: The dispatcher therefore checks `rebac_action` as well; see
    #: `dispatcher.required_privileges`.
    privilege: str
    #: The status this action moves the case to, or None if it does not move it.
    #: `"$param"` means the caller names the target — only `change_status` does.
    status_effect: str | None
    #: Everything this action does, from the closed set above. Shown at confirm
    #: time for a bulk run.
    effects: tuple[str, ...]
    #: One sentence. Rendered in the API and in the config screen.
    summary: str
    #: Whether this destroys or hides something. A destructive action is never
    #: offered in a bulk run without an explicit confirmation.
    destructive: bool = False
    #: The action that undoes this one, where one exists. Documentation with
    #: teeth: an action claiming no reverse is one an operator should hesitate
    #: over.
    reversible_by: str | None = None
    #: Parameters the handler requires. Absent ones are a 400 before any case
    #: is touched — a bulk run must not half-apply because id 200 was missing a
    #: field.
    required_params: tuple[str, ...] = ()
    #: The CASE-level (ReBAC) action, which asks whether this operator has a
    #: relationship to this particular case — assignee, editor, or a manager
    #: bypass. Every one of the six removed routes gated on `case.update`, so
    #: that is the default and the parity baseline.
    rebac_action: str = "case.update"
    #: What this writes into `case_audit_log.action`. Defaults to the action's
    #: own name, and is set explicitly wherever the pre-198 route wrote
    #: something else. **The audit vocabulary is not renamed by this change.**
    #: `/resolve` wrote `resolved` and an auditor searching for `resolved` must
    #: keep finding it — including the rows written before today, which no
    #: rename could reach. So the URL verb changes and the recorded verb does
    #: not.
    audit_action: str | None = None

    @property
    def audit_name(self) -> str:
        return self.audit_action or self.name

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name, "label": self.label, "privilege": self.privilege,
            "status_effect": self.status_effect, "effects": list(self.effects),
            "summary": self.summary, "destructive": self.destructive,
            "reversible_by": self.reversible_by,
            "required_params": list(self.required_params),
            "audit_action": self.audit_name,
        }


ACTIONS: dict[str, ActionDef] = {}


def _reg(a: ActionDef) -> None:
    if a.name in ACTIONS:
        raise ValueError(f"duplicate case action {a.name!r}")
    unknown = set(a.effects) - EFFECTS
    if unknown:
        raise ValueError(f"{a.name}: undeclared effects {sorted(unknown)}")
    ACTIONS[a.name] = a


# ── the built set ────────────────────────────────────────────────────────────
#
# Sixteen actions. Eleven shipped with 198; the other five landed on 2026-09-08
# when §7's questions were answered.
#
# **Merge and the two tag actions are not here and never will be** — they were
# dropped rather than deferred, and the design document that listed them was
# edited rather than annotated. An item that stays listed gets raised again.
#
# The rule that kept the first eleven honest still holds: an action that is
# half-specified is worse than one that is missing, because missing gives a 404
# and half-specified gives a wrong answer. `transfer` is the live example — its
# queue and access-group forms are here, and moving a case to another TENANT is
# deliberately not, because that crosses the boundary the whole of September was
# spent closing and needs its own design.

_reg(ActionDef(
    name="open", label="Open",
    privilege="case.actions.update",
    status_effect="open",
    effects=("status",),
    summary="Accepts the case for work.",
    reversible_by="change_status",
))

_reg(ActionDef(
    name="close", label="Close",
    privilege="case.actions.close",
    status_effect="closed",
    effects=("status",),
    summary="Ends the case. Stamps closed_at and stops its SLAs.",
    reversible_by="reopen",
    audit_action="closed",
))

_reg(ActionDef(
    name="resolve", label="Resolve",
    privilege="case.actions.close",
    status_effect="resolved",
    effects=("status",),
    summary="Marks the case resolved and records the resolution.",
    reversible_by="reopen",
    audit_action="resolved",
))

_reg(ActionDef(
    name="reopen", label="Reopen",
    privilege="case.actions.reopen",
    status_effect="reopened",
    effects=("status",),
    summary="Reopens a terminal case and restarts its lifecycle workflow.",
    reversible_by="close",
    audit_action="reopened",
))

_reg(ActionDef(
    name="cancel", label="Cancel",
    privilege="case.actions.close",
    status_effect="cancelled",
    effects=("status",),
    summary="Ends the case without resolving it.",
    reversible_by="reopen",
    audit_action="cancelled",
))

_reg(ActionDef(
    name="put_on_hold", label="Put on Hold",
    privilege="case.actions.update",
    status_effect="on_hold",
    effects=("status", "sla"),
    # A hold that leaves the clock running is not a hold — it is a label on a
    # case that is still going late. Pausing the SLA is the point of it.
    summary="Pauses the case and its running SLAs, with a reason.",
    reversible_by="open",
    required_params=("reason",),
))

_reg(ActionDef(
    name="change_status", label="Change Status",
    privilege="case.actions.update",
    # The only action whose target the caller names. Every value still goes
    # through the resolved status set, so "any string" is no longer accepted.
    status_effect="$param",
    effects=("status",),
    summary="Moves the case to a named status in the configured set.",
    required_params=("status",),
    audit_action="status_changed",
))

_reg(ActionDef(
    name="change_priority", label="Change Priority",
    privilege="case.actions.update",
    status_effect=None,
    effects=("priority",),
    summary="Changes the case priority.",
    required_params=("priority",),
    audit_action="priority_changed",
))

_reg(ActionDef(
    name="assign", label="Assign",
    privilege="case.actions.assign",
    status_effect=None,
    effects=("assignment",),
    # step_id is required and not guessed. An assignment row without one is an
    # assignment nobody can complete — the reason the scheduled bulk action
    # refuses assignment outright.
    summary="Creates a work assignment on a named step.",
    required_params=("step_id", "assignee_type", "assignee_id"),
))

_reg(ActionDef(
    name="reassign", label="Reassign",
    privilege="case.actions.assign",
    status_effect=None,
    effects=("assignment",),
    # Unlike the scheduled action, this one has the case in hand: it moves the
    # ACTIVE assignments, which already name their steps, so nothing is guessed.
    summary="Moves every active assignment on the case to a new assignee.",
    required_params=("assignee_type", "assignee_id"),
))

_reg(ActionDef(
    name="add_note", label="Add Note",
    privilege="case.actions.note",
    status_effect=None,
    effects=("note",),
    # Notes are case_messages. `portal_visible` defaults to false, so a note
    # added in a bulk sweep never reaches a customer by accident.
    summary="Appends an internal note to the case thread.",
    required_params=("body",),
))


# ── §7 answered 2026-09-08 ───────────────────────────────────────────────────

_reg(ActionDef(
    name="archive", label="Archive",
    privilege="case.actions.archive",
    status_effect="archived",
    effects=("status",),
    # Not `close`: closing ends the work, archiving seals the record. An
    # operator who may end a case has not thereby been given the power to make
    # it unchangeable, so the privilege is separate and narrower.
    summary="Seal a finished case. Nothing further may be done to it except reopen.",
    reversible_by="reopen",
    audit_action="archived",
))

_reg(ActionDef(
    name="delete", label="Delete",
    privilege="case.actions.delete",
    status_effect=None,
    effects=("delete",),
    # SOFT. The row and its hash-linked history stay; the case leaves every list
    # and every read. A hard delete would be a second, weaker erasure than the
    # `anonymize` this platform already implements — one that also destroyed the
    # evidence the first deliberately preserves.
    destructive=True,
    summary="Hide a case from every list and read. History is retained; erasure is a separate act.",
    reversible_by="restore",
    audit_action="deleted",
))

_reg(ActionDef(
    name="restore", label="Restore",
    privilege="case.actions.delete",
    status_effect=None,
    effects=("delete",),
    # Same privilege as `delete` on purpose: the power to hide a case and the
    # power to bring it back are the same power. Splitting them would produce
    # an operator who can delete and cannot undo, which is the worse half.
    summary="Bring back a soft-deleted case.",
    reversible_by="delete",
    audit_action="restored",
))

_reg(ActionDef(
    name="escalate", label="Escalate",
    privilege="case.actions.escalate",
    status_effect="escalated",
    effects=("status", "sla", "notify"),
    # The action the registry's own docstring uses as its example of why this
    # vocabulary is closed: you cannot infer "fires a tree and notifies people
    # who are not working this case" from the word.
    summary="Raise the case up its escalation tree, notify, and mark it escalated.",
    reversible_by="open",
    audit_action="escalated",
))

_reg(ActionDef(
    name="split", label="Split",
    privilege="case.actions.split",
    status_effect=None,
    effects=("relationship", "create"),
    # Creates a CHILD, linked, with `propagate_status` — so the parent learns
    # when the child finishes. Reuses the shape `POST /{id}/children` already
    # writes rather than inventing a second one.
    summary="Split work out of this case into a new linked child case.",
    required_params=("case_type_id",),
    audit_action="child_case_created",
))

_reg(ActionDef(
    name="transfer", label="Transfer",
    privilege="case.actions.transfer",
    status_effect=None,
    effects=("assignment",),
    # Two forms, both of which move who HOLDS the case:
    #   target_type = "queue"        → the case's active assignments move
    #   target_type = "access_group" → a `viewer`/`editor` tuple is written
    #                                  against the group, resolved through
    #                                  membership at decision time
    #
    # A third form — moving a case to another TENANT — is deliberately not here.
    # It crosses the boundary September was spent closing, it has to say
    # explicitly what moves with the case (documents, messages, audit rows,
    # variables, assignments, SLA instances, portal links) and it hands the
    # destination tenant data authored under a different lawful basis. That is a
    # design, not a third branch of this action.
    summary="Hand the case to another queue, or give an access group standing access to it.",
    required_params=("target_type", "target_id"),
    audit_action="case_transferred",
))


def get(name: str) -> ActionDef | None:
    return ACTIONS.get(name)


def names() -> tuple[str, ...]:
    return tuple(sorted(ACTIONS))


def describe_all() -> list[dict[str, Any]]:
    return [ACTIONS[n].describe() for n in names()]
