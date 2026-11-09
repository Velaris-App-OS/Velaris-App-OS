"""Moving a case between tenants — the first cut of CASE-TENANT-TRANSFER-DESIGN.md.

Built 2026-09-17. Migration 205. Documented in
`docs/components/case-actions.md` §6d.

**The shape.** A source admin REQUESTS, a DIFFERENT source admin APPROVES, the
destination tenant's admin ACCEPTS or REFUSES. Nothing is readable at the
destination before acceptance except the fact of the request. The move happens
at acceptance, in one transaction — or not at all.

**Every table that hangs off a case is classified, by name, below.** A case is
not one row: on the day this was written 66 tables carried a case id. Each one
either MOVES with the case, is CLOSED, is SEVERED, BLOCKS the transfer, or STAYS
with the source as the source's own operational record. A table nobody
classified would be decided by default, which is the objection the design doc
raised against building this as a third `if` in `transfer`. So the test suite
reads the ORM metadata and fails when a new case-linked table appears here
unclassified — the next feature has to take a position.

**What blocks, and why.** Money (ledger, invoices, payments, checkout), signed
and verified evidence (e-signatures, identity checks), recorded sessions, work
delegated outside the platform (handoff orders, field nodes), tenant-specific
document storage, an open HxSentinel hold, and an open data-subject request.
Each is either bound to the source as controller or cannot follow the case
honestly; refusing is better than a case that arrives with half its meaning.

**What the destination sees of the history.** The audit trail moves, with actor
identities replaced by stable pseudonyms and person-shaped detail keys removed,
plus one entry recording the transfer. An unbroken chain with pseudonymous
actors is more useful than a case that appears to begin the day it arrived, and
it does not hand one company a staff directory of another.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import hashlib
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import (
    Base,
    CaseAuditLogModel,
    CaseInstanceModel,
    CaseTenantTransferModel,
    CaseTypeModel,
    TenantModel,
)

log = logging.getLogger(__name__)

PRIVILEGE = "case.actions.transfer_tenant"

# ── the classification ───────────────────────────────────────────────────────

#: Content that follows the case, re-tagged to the destination tenant.
MOVE_RETAG: frozenset[str] = frozenset({
    "documents", "hxnexus_document_chunks", "doc_extraction_jobs",
    "document_index_jobs", "case_outcomes",
    # Outbound-only send log (nothing threads back through it). Its connector is
    # the source's, so `connector_id` is cleared on the way (Oct item 8a).
    "sms_messages",
})

#: Content that follows the case with no tenant column to change.
MOVE_WITH_CASE: frozenset[str] = frozenset({
    "case_messages", "case_instance_variables", "case_step_completions",
    "case_events", "case_event_log", "document_verifications",
    # Moves, redacted — actor ids pseudonymised in `accept`.
    "case_audit_log",
})

#: Closed rather than moved: they point at people or clocks of the source.
CLOSE: frozenset[str] = frozenset({"case_assignments", "case_sla_instances"})

#: Links that cannot cross the boundary. Removed and recorded in the manifest.
SEVER: frozenset[str] = frozenset({
    "case_relationships", "portal_customer_cases", "portal_submission_refs",
    "hxwork_sprint_cards",
    # Oct 18a: which arm of the SOURCE's content rollout the case landed in.
    # The destination has no such rollout; the source keeps its count honest.
    "content_rollout_cases", "content_rollout_shadow",
    # Form Builder §5 step 6/7: a pin to the SOURCE's form snapshot, and a
    # source person's unfinished answers. The destination starts afresh.
    "case_form_versions", "form_drafts",
    # `case_instances.parent_case_id`, both directions.
    "case_instances",
})

#: Customer threads that run on the SOURCE's own channels — its WhatsApp and SMS
#: endpoints, its mailboxes (Oct item 8a; blocked the move until 2026-09-28).
#:
#: They are **not moved**. Moved, the destination could reply through the
#: source's number or mailbox, and the customer's next message — matched by an
#: open conversation on the source's endpoint, or by message id — would land in
#: another company's case. So the source keeps them, **detached** from the case
#: (`case_id` → NULL) and, for conversations, **closed**: the customer's next
#: message opens a fresh thread at the source, which is who they wrote to. The
#: destination gets the history as a **read-only transcript** note on the case,
#: source staff pseudonymised, and talks to the customer on its own channels.
REHOME: frozenset[str] = frozenset({
    "channel_conversations", "channel_messages", "email_messages",
})

#: The transcript is a note, not an archive. Past this it says it was cut.
TRANSCRIPT_MAX_CHARS = 200_000

#: Any row here refuses the transfer, with the table named.
BLOCK: dict[str, str] = {
    "ledger_transactions": "it has accounting entries",
    "ledger_entries": "it has accounting entries",
    "invoice_records": "it has invoices",
    "payment_requests": "it has payment requests",
    "payment_disbursements": "it has disbursements",
    "checkout_orders": "it has orders",
    # R5e step 7: the case was opened for a commerce order (or its return /
    # complaint); the order sweep finds it by this row in the source tenant.
    "commerce_case_requests": "it was opened for a commerce order",
    "esign_requests": "it has e-signature requests, which are evidence bound to the source",
    "identity_verifications": "it has identity verifications performed for the source",
    "case_sessions": "it has recorded sessions, whose consent was given to the source",
    "handoff_work_orders": "work on it has been handed to an outside party",
    "field_sync_events": "a field node holds it",
    "doc_storage_routes": "its documents are routed to tenant-specific storage",
}

#: The source's own operational record about the case. Stays with the source,
#: invisible to the destination, which never had it.
STAY: frozenset[str] = frozenset({
    "calendar_events", "user_absences", "case_time_entries",
    "case_risk_signals", "case_risk_findings", "case_risk_feedback", "case_risk_holds",
    "consent_records", "data_lineage_events", "hxdbmigrate_row_links",
    "hxnexus_conversations", "mcp_action_proposals", "ai_action_proposals",
    "replay_runs", "replay_results", "ai_decision_records", "decision_costs",
    "bot_jobs", "dead_letter_queue", "field_population_audit", "integration_calls",
    "process_instances", "process_case_bindings", "capture_runs", "capture_proposals",
    "intent_plans", "trace_events", "slack_notifications", "crm_sync_records",
    "hxcanvas_boards", "portal_csat",
    # This module's own table: the tombstone the source keeps.
    "case_tenant_transfers",
    # Oct item 13. An ACTIVE hold refuses the move outright (see `blockers`),
    # so only released holds are ever here at move time: the source's record
    # that it once froze the case, for a matter the destination has no part in.
    "legal_holds",
})

#: Every column in a moved table that names a person of the SOURCE tenant.
#: Rewritten to a pseudonym on move. `test_every_person_column_that_moves_is_
#: pseudonymised` fails when a moved table gains one that is not listed here.
PERSON_COLUMNS: dict[str, tuple[str, ...]] = {
    "case_audit_log": ("actor_id",),
    "case_events": ("actor_id",),
    "case_event_log": ("actor_id",),
    "case_messages": ("author", "author_name"),
    "case_instance_variables": ("written_by",),
    "case_step_completions": ("completed_by",),
    "case_outcomes": ("labelled_by",),
    "document_index_jobs": ("requested_by",),
    "document_verifications": ("verified_by",),
    "documents": ("uploaded_by", "deleted_by"),
}

#: JSON columns on moved tables whose top-level keys may name a person.
PERSON_JSON_COLUMNS: dict[str, tuple[str, ...]] = {
    "case_audit_log": ("details",),
    "case_events": ("data",),
    "case_event_log": ("metadata",),
}

#: The pattern a column name is checked against for the completeness test.
PERSON_COLUMN_PATTERN = re.compile(
    r"(_by$|^author|^actor(_id)?$|user_id$|^assigned_to$|^owner|^reviewer|^approver|^agent_user)")

#: The columns that make a table "case-linked" for the completeness check.
CASE_COLUMNS = ("case_id", "case_instance_id", "parent_case_id", "source_case_id",
                "target_case_id")


def classified() -> set[str]:
    return (set(MOVE_RETAG) | set(MOVE_WITH_CASE) | set(CLOSE) | set(SEVER)
            | set(REHOME) | set(BLOCK) | set(STAY))


def unclassified() -> set[str]:
    """Case-linked tables this module takes no position on. Must be empty."""
    linked = {t.name for t in Base.metadata.sorted_tables
              if any(c.name in CASE_COLUMNS for c in t.columns)}
    return linked - classified()


# ── errors ───────────────────────────────────────────────────────────────────

class TransferError(Exception):
    def __init__(self, message: str, *, code: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def pseudonym(source_tenant_id: uuid.UUID, actor: Any) -> str | None:
    """A stable stand-in for a source-tenant actor. Stable, so "the same person
    did steps 3 and 7" survives; keyed by the source tenant, so the same person
    in two transfers from two tenants does not correlate."""
    if actor in (None, ""):
        return None
    h = hashlib.sha256(f"{source_tenant_id}:{actor}".encode()).hexdigest()[:12]
    return f"actor:{h}"


_PERSONISH = re.compile(r"(^|_)(by|user|users|actor|email|name|assignee|owner|claimed|"
                        r"reviewer|approver|agent|operator|username|submitter)($|_|s$)")


def _scrub_details(details: Any) -> Any:
    if not isinstance(details, dict):
        return details
    return {k: ("[redacted]" if _PERSONISH.search(str(k).lower()) else v)
            for k, v in details.items()}


# ── checks ───────────────────────────────────────────────────────────────────

async def _tenant(session: AsyncSession, tenant_id: uuid.UUID) -> TenantModel:
    t = await session.get(TenantModel, tenant_id)
    if t is None:
        raise TransferError("No such tenant.", code="VEL-CASE-TENANT-TRANSFER-404",
                            status_code=404)
    return t


async def _count(session: AsyncSession, table_name: str, case_id: uuid.UUID) -> int:
    table = Base.metadata.tables[table_name]
    col = table.c.get("case_id")
    if col is None:
        return 0
    return int((await session.execute(
        select(func.count()).select_from(table).where(col == case_id))).scalar() or 0)


async def blockers(session: AsyncSession, case: CaseInstanceModel,
                   destination: TenantModel) -> list[str]:
    """Every reason this case may not move. Empty means it may."""
    from case_service.case_actions import seal as _seal
    from velaris_shared.db.models import CaseRiskHoldModel, GDPRRequestModel

    out: list[str] = []
    if case.tenant_id is None:
        out.append("the case has no tenant, so there is no source to move it from")
    elif case.tenant_id == destination.id:
        out.append("the case is already in that tenant")
    sealed = _seal.sealed_reason(case)
    if sealed:
        out.append(sealed)

    ct = await session.get(CaseTypeModel, case.case_type_id)
    if ct is None or getattr(ct, "deleted_at", None) is not None:
        out.append("its case type no longer exists")
    elif ct.tenant_id is not None and ct.tenant_id != destination.id:
        out.append(f"its case type '{ct.name}' belongs to the source tenant; the "
                   "destination has no such case type to run it on")

    for table, why in sorted(BLOCK.items()):
        if table in Base.metadata.tables and await _count(session, table, case.id):
            out.append(why)

    holds = int((await session.execute(
        select(func.count()).select_from(CaseRiskHoldModel).where(
            CaseRiskHoldModel.case_id == case.id,
            CaseRiskHoldModel.status == "open"))).scalar() or 0)
    if holds:
        out.append("an HxSentinel risk hold is open on it")

    # A legal hold (Oct item 13). Until it existed, the risk hold above was the
    # stand-in for "this case must stay where it is".
    from case_service.legal_hold import service as _legal_hold
    if await _legal_hold.is_held(session, case.id):
        out.append("it is under a legal hold")

    # The data subject, under every identifier a request may have been filed
    # with: the portal submitter's email, and any portal customer linked to the
    # case. `gdpr_requests.subject_id` is free text, so both spellings are tried.
    subjects = set()
    if (case.portal_submitter_email or "").strip():
        subjects.add(case.portal_submitter_email.strip().lower())
    pcc = Base.metadata.tables["portal_customer_cases"]
    subjects.update(str(v).lower() for v in (await session.execute(
        select(pcc.c.customer_id).where(pcc.c.case_id == case.id))).scalars().all() if v)
    if subjects:
        dsar = int((await session.execute(
            select(func.count()).select_from(GDPRRequestModel).where(
                func.lower(GDPRRequestModel.subject_id).in_(sorted(subjects)),
                GDPRRequestModel.status.notin_(("completed", "rejected", "cancelled"))
            ))).scalar() or 0)
        if dsar:
            out.append("its data subject has an open request with the source; the "
                       "subject asked this controller")
    return out


async def _policy_allows(session: AsyncSession, *, case: CaseInstanceModel,
                         destination: TenantModel, actor: str, stage: str) -> bool:
    """HxArbiter `case.tenant.transfer`. Asked at request and again before the
    move. Fails open exactly as the stage and status PEPs do — the `case`
    namespace defaults to allow and ships clamped to shadow."""
    try:
        from case_service.hxarbiter import pep as _pep

        return await _pep.check(
            session, tenant_id=str(case.tenant_id), action=_pep.ACTION_TENANT_TRANSFER,
            subject_id=str(actor), subject_kind=_pep.SubjectKind.USER,
            resource_kind="case", resource_id=str(case.id),
            resource_attrs={"case_type": str(case.case_type_id)},
            context={"destination_tenant": str(destination.id), "stage": stage,
                     "purpose": "tenant_transfer"},
            legacy_allowed=True, claimed_by=str(actor)[:255])
    except Exception as exc:                                    # noqa: BLE001
        log.warning("tenant transfer: HxArbiter PEP unavailable: %s", exc)
        return True


# ── the lifecycle ────────────────────────────────────────────────────────────

async def request(session: AsyncSession, *, case: CaseInstanceModel,
                  destination_tenant_id: uuid.UUID, reason: str,
                  actor: str) -> CaseTenantTransferModel:
    if len((reason or "").strip()) < 10:
        raise TransferError("A transfer needs a reason somebody at both tenants can read.",
                            code="VEL-CASE-TENANT-TRANSFER-400", status_code=400)
    destination = await _tenant(session, destination_tenant_id)
    why = await blockers(session, case, destination)
    if why:
        raise TransferError("This case cannot move: " + "; ".join(why) + ".",
                            code="VEL-CASE-TENANT-TRANSFER-BLOCKED-409")
    if not await _policy_allows(session, case=case, destination=destination,
                                actor=actor, stage="request"):
        raise TransferError("Policy refuses moving this case to that tenant.",
                            code="VEL-CASE-TENANT-TRANSFER-POLICY-403", status_code=403)
    open_one = (await session.execute(select(CaseTenantTransferModel).where(
        CaseTenantTransferModel.active_key == str(case.id)))).scalar_one_or_none()
    if open_one is not None:
        raise TransferError("A transfer of this case is already open.",
                            code="VEL-CASE-TENANT-TRANSFER-STATE-409")
    ct = await session.get(CaseTypeModel, case.case_type_id)
    row = CaseTenantTransferModel(
        id=uuid.uuid4(), case_id=case.id, case_number=case.case_number,
        case_type_name=ct.name if ct else None, source_tenant_id=case.tenant_id,
        destination_tenant_id=destination.id, state="requested",
        active_key=str(case.id), reason=reason.strip(), requested_by=str(actor),
        requested_at=_utcnow())
    session.add(row)
    await session.flush()
    return row


def _expect(row: CaseTenantTransferModel, *states: str) -> None:
    if row.state not in states:
        raise TransferError(f"This transfer is {row.state}.",
                            code="VEL-CASE-TENANT-TRANSFER-STATE-409")


async def approve(session: AsyncSession, *, row: CaseTenantTransferModel,
                  actor: str) -> CaseTenantTransferModel:
    """The source's second person. The requester may not approve their own
    request — a move that is irreversible in practice gets two people."""
    _expect(row, "requested")
    if str(actor) == str(row.requested_by):
        raise TransferError("The person who requested a transfer may not approve it.",
                            code="VEL-CASE-TENANT-TRANSFER-TWO-PERSON-403", status_code=403)
    row.state = "pending"
    row.approved_by = str(actor)
    row.approved_at = _utcnow()
    await session.flush()
    return row


async def cancel(session: AsyncSession, *, row: CaseTenantTransferModel,
                 actor: str) -> CaseTenantTransferModel:
    _expect(row, "requested", "pending")
    row.state = "cancelled"
    row.active_key = None
    row.cancelled_by = str(actor)
    row.decided_at = _utcnow()
    await session.flush()
    return row


async def refuse(session: AsyncSession, *, row: CaseTenantTransferModel, actor: str,
                 note: str | None) -> CaseTenantTransferModel:
    _expect(row, "pending")
    row.state = "refused"
    row.active_key = None
    row.decided_by = str(actor)
    row.decided_at = _utcnow()
    row.decision_note = note
    await session.flush()
    return row


def _retag(value: Any, source: TenantModel, destination: TenantModel) -> Any:
    """Keep the column's spelling: slug stays slug, GUID string stays GUID."""
    if value is None:
        return None
    text = str(value)
    if text == source.slug:
        return destination.slug
    if text.lower() == str(source.id).lower():
        return str(destination.id)
    return value


def _when(value) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else "—"


async def _rehome_communications(session: AsyncSession, case: CaseInstanceModel,
                                 source: TenantModel, row: CaseTenantTransferModel,
                                 manifest: dict[str, Any]) -> None:
    """Detach the source's customer threads from the case; hand the destination
    a read-only transcript of them. See `REHOME`."""
    from velaris_shared.db.models import (
        CaseMessageModel, ChannelConversationModel, ChannelMessageModel, EmailMessageModel,
    )

    entries: list[tuple[Any, str]] = []
    for m in (await session.execute(select(ChannelMessageModel).where(
            ChannelMessageModel.case_id == case.id))).scalars().all():
        who = (f"customer ({m.peer_address})" if m.direction == "inbound"
               else f"agent {pseudonym(source.id, m.author) or '—'}")
        entries.append((m.received_at or m.sent_at or m.created_at,
                        f"[{_when(m.received_at or m.sent_at or m.created_at)}] "
                        f"{(m.channel or 'channel').upper()} · {who}\n{m.body or ''}"))
    for e in (await session.execute(select(EmailMessageModel).where(
            EmailMessageModel.case_id == case.id))).scalars().all():
        at = e.received_at or e.sent_at or e.created_at
        head = (f"from {e.from_address}" if e.direction == "inbound"
                else f"to {', '.join(e.to_addresses or [])}")
        entries.append((at, f"[{_when(at)}] EMAIL · {e.direction} · {head}\n"
                            f"Subject: {e.subject or ''}\n{e.body_text or ''}"))

    tables = Base.metadata.tables
    conv = tables["channel_conversations"]
    res = await session.execute(update(conv).where(conv.c.case_id == case.id).values(
        case_id=None, status="closed", assigned_to=None))
    if res.rowcount:
        manifest["rehomed"]["channel_conversations"] = res.rowcount
    for name in ("channel_messages", "email_messages"):
        t = tables[name]
        res = await session.execute(update(t).where(t.c.case_id == case.id).values(case_id=None))
        if res.rowcount:
            manifest["rehomed"][name] = res.rowcount

    if not entries:
        return
    def _at(value) -> datetime:
        # Columns here mix naive and aware datetimes; compare them as UTC.
        if value is None:
            return datetime.max.replace(tzinfo=timezone.utc)
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    entries.sort(key=lambda x: _at(x[0]))
    body = "\n\n".join(text for _, text in entries)
    cut = len(body) > TRANSCRIPT_MAX_CHARS
    if cut:
        body = body[:TRANSCRIPT_MAX_CHARS]
    header = (f"Customer communication history carried from {source.name} when this "
              f"case moved here (transfer {row.id}). Read-only: the live threads "
              f"stay on {source.name}'s own channels. Reply from this tenant's channels.")
    if cut:
        header += f" Truncated at {TRANSCRIPT_MAX_CHARS} characters."
    session.add(CaseMessageModel(
        id=uuid.uuid4(), case_id=case.id, author="velaris:tenant-transfer",
        author_name="Tenant transfer", body=header + "\n\n" + body,
        portal_visible=False))
    manifest["rehomed"]["transcript_entries"] = len(entries)


async def accept(session: AsyncSession, *, row: CaseTenantTransferModel, actor: str,
                 note: str | None, restart_sla=None) -> CaseTenantTransferModel:
    """The move. Every check again, then every write, then one commit by the caller.

    Raises before writing anything if the case can no longer move. A write that
    fails part way raises out of here, and the caller's rollback takes every
    earlier write with it — "never partially move" is the transaction, not a
    promise.
    """
    from velaris_shared.db.models import HxGuardTupleModel, CustomerLinkModel

    _expect(row, "pending")
    case = await session.get(CaseInstanceModel, row.case_id)
    if case is None or case.tenant_id != row.source_tenant_id:
        raise TransferError("The case is no longer where the request found it.",
                            code="VEL-CASE-TENANT-TRANSFER-BLOCKED-409")
    source = await _tenant(session, row.source_tenant_id)
    destination = await _tenant(session, row.destination_tenant_id)
    why = await blockers(session, case, destination)
    if why:
        raise TransferError("This case can no longer move: " + "; ".join(why) + ".",
                            code="VEL-CASE-TENANT-TRANSFER-BLOCKED-409")
    if not await _policy_allows(session, case=case, destination=destination,
                                actor=actor, stage="accept"):
        raise TransferError("Policy refuses moving this case to that tenant.",
                            code="VEL-CASE-TENANT-TRANSFER-POLICY-403", status_code=403)

    manifest: dict[str, Any] = {"moved": {}, "closed": {}, "severed": {}, "rehomed": {},
                                "stayed": sorted(STAY)}
    tables = Base.metadata.tables

    # Refuse, before any write, a row tagged with a THIRD tenant. It would be
    # re-tagged to nobody's expectation, and it means the data is not what the
    # classification assumed.
    for name in sorted(MOVE_RETAG):
        t = tables[name]
        vals = (await session.execute(
            select(t.c.tenant_id).where(t.c.case_id == case.id).distinct())).scalars().all()
        odd = [v for v in vals if v is not None and _retag(v, source, destination) == v]
        if odd:
            raise TransferError(
                f"{name} holds rows for this case tagged with another tenant; refusing "
                "rather than guessing whose they are.",
                code="VEL-CASE-TENANT-TRANSFER-BLOCKED-409")

    # ── MOVE ────────────────────────────────────────────────────────────────
    for name in sorted(MOVE_RETAG):
        t = tables[name]
        moved = 0
        for value in (await session.execute(
                select(t.c.tenant_id).where(t.c.case_id == case.id).distinct())).scalars().all():
            clause = (t.c.tenant_id.is_(None) if value is None else t.c.tenant_id == value)
            res = await session.execute(
                update(t).where(t.c.case_id == case.id, clause)
                .values(tenant_id=_retag(value, source, destination)))
            moved += res.rowcount or 0
        if moved:
            manifest["moved"][name] = moved
    for name in sorted(MOVE_WITH_CASE - {"case_audit_log"}):
        n = await _count(session, name, case.id)
        if n:
            manifest["moved"][name] = n

    # Every source-staff identity that travels is replaced, column by column.
    # Pseudonyms are stable per (source tenant, actor), so "the same person did
    # both" survives and a staff directory does not.
    for name, cols in sorted(PERSON_COLUMNS.items()):
        t = tables[name]
        json_cols = PERSON_JSON_COLUMNS.get(name, ())
        rows = (await session.execute(
            select(t.c.id, *[t.c[c] for c in cols], *[t.c[c] for c in json_cols])
            .where(t.c.case_id == case.id))).all()
        for r in rows:
            values = {c: pseudonym(source.id, getattr(r, c)) for c in cols}
            values.update({c: _scrub_details(getattr(r, c)) for c in json_cols})
            await session.execute(update(t).where(t.c.id == r.id).values(**values))
        if rows and name not in manifest["moved"]:
            manifest["moved"][name] = len(rows)

    # ── CLOSE ───────────────────────────────────────────────────────────────
    t = tables["case_assignments"]
    res = await session.execute(update(t).where(
        t.c.case_id == case.id, t.c.status == "active").values(status="cancelled"))
    if res.rowcount:
        manifest["closed"]["case_assignments"] = res.rowcount
    t = tables["case_sla_instances"]
    res = await session.execute(update(t).where(
        t.c.case_id == case.id,
        t.c.status.in_(("on_track", "at_risk", "paused", "breached"))).values(status="cancelled"))
    if res.rowcount:
        manifest["closed"]["case_sla_instances"] = res.rowcount

    # ── SEVER ───────────────────────────────────────────────────────────────
    t = tables["case_relationships"]
    res = await session.execute(delete(t).where(
        or_(t.c.source_case_id == case.id, t.c.target_case_id == case.id)))
    if res.rowcount:
        manifest["severed"]["case_relationships"] = res.rowcount
    for name in ("portal_customer_cases", "portal_submission_refs", "hxwork_sprint_cards",
                 "content_rollout_cases", "content_rollout_shadow",
                 "case_form_versions", "form_drafts"):
        t = tables[name]
        res = await session.execute(delete(t).where(t.c.case_id == case.id))
        if res.rowcount:
            manifest["severed"][name] = res.rowcount
    children = await session.execute(update(CaseInstanceModel).where(
        CaseInstanceModel.parent_case_id == case.id).values(parent_case_id=None)
        .execution_options(synchronize_session=False))
    if children.rowcount:
        manifest["severed"]["child_cases"] = children.rowcount
    if case.parent_case_id is not None:
        manifest["severed"]["parent_case"] = 1
        case.parent_case_id = None
    if case.portal_tracking_token is not None:
        manifest["severed"]["portal_tracking_token"] = 1
        case.portal_tracking_token = None
    res = await session.execute(delete(CustomerLinkModel).where(
        CustomerLinkModel.object_kind == "case", CustomerLinkModel.object_id == case.id))
    if res.rowcount:
        manifest["severed"]["customer_links"] = res.rowcount
    # Every share and assignee tuple names a source-tenant person.
    res = await session.execute(delete(HxGuardTupleModel).where(
        HxGuardTupleModel.object_type == "case", HxGuardTupleModel.object_id == case.id))
    if res.rowcount:
        manifest["severed"]["hxguard_tuples"] = res.rowcount

    # ── REHOME ──────────────────────────────────────────────────────────────
    # After the pseudonym pass on purpose: the transcript note is written by
    # the platform, and its author must not be run through it.
    await _rehome_communications(session, case, source, row, manifest)
    t = tables["sms_messages"]
    await session.execute(update(t).where(t.c.case_id == case.id).values(connector_id=None))

    # ── the case itself ─────────────────────────────────────────────────────
    previous_creator = case.created_by
    case.tenant_id = destination.id
    case.created_by = pseudonym(source.id, previous_creator)
    session.add(CaseAuditLogModel(
        id=uuid.uuid4(), case_id=case.id, action="tenant_transferred",
        actor_id=pseudonym(source.id, row.approved_by), actor_type="user",
        details={"transfer_id": str(row.id), "reason": row.reason,
                 "from_tenant": source.name, "accepted_by": str(actor),
                 "manifest": {k: v for k, v in manifest.items() if k != "stayed"}},
        timestamp=_utcnow()))
    await session.flush()

    if restart_sla is not None:
        # The destination's clock, from now — not the source's, carried on.
        try:
            manifest["sla_restarted"] = int(await restart_sla(case) or 0)
        except Exception as exc:                                # noqa: BLE001
            log.warning("tenant transfer: SLA restart failed for %s: %s", case.id, exc)
            manifest["sla_restarted"] = 0

    row.state = "completed"
    row.active_key = None
    row.decided_by = str(actor)
    row.decided_at = _utcnow()
    row.decision_note = note
    row.manifest = manifest
    row.completed_at = _utcnow()
    await session.flush()
    return row


def as_dict(row: CaseTenantTransferModel, *, side: str) -> dict[str, Any]:
    """What each side may see. The destination never sees source staff ids."""
    def iso(d):
        return d.isoformat() if d else None
    out = {
        "id": str(row.id), "case_id": str(row.case_id), "case_number": row.case_number,
        "case_type_name": row.case_type_name, "state": row.state, "reason": row.reason,
        "source_tenant_id": str(row.source_tenant_id),
        "destination_tenant_id": str(row.destination_tenant_id),
        "requested_at": iso(row.requested_at), "approved_at": iso(row.approved_at),
        "decided_at": iso(row.decided_at), "completed_at": iso(row.completed_at),
        "decision_note": row.decision_note, "manifest": row.manifest,
    }
    if side == "source":
        out.update(requested_by=row.requested_by, approved_by=row.approved_by,
                   decided_by=row.decided_by, cancelled_by=row.cancelled_by)
    return out
