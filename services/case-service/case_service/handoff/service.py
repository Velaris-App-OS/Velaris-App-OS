"""Work orders: enrol, issue, accept, read, propose, hand back, revoke.

Design: docs/Future/Done/AI-ERA-ROADMAP.md §F-09.

**The work order is the entire authority.** Not a session, not a login. It is
case-scoped, it expires, and it is carried by an F-00 agent token — so
revocation, the live capability ceiling and the deployment kill switch are the
ones already built and proven, rather than a second set that could disagree with
them. Revoking the grant kills the order everywhere at once without this
module being consulted.

**Four verbs, and none of them writes to a case.** `accept`, `read`, `propose`,
`return`. An external agent's output is a proposal a human confirms, and it
lands in the SAME place an email's proposal lands — F-06's `capture_proposals`,
decided on the Captured tab, applied as the confirming person through their own
grant. That is not convenience: an external agent's message is untrusted input
in exactly the way an inbound email is, and giving it a second, gentler path
into case state would undo the reason the first one exists.

**The whitelist is F-06's too.** `capture.service.allowed_targets` bounds what
may be proposed to the case type's own defined variables. An external agent
therefore cannot propose anything an injected email could not, and widening it
would take a migration rather than a persuasive sentence.

**Signatures both ways.** We sign the order with a key derived from the tenant's
HxVault DEK (the same construction the model marketplace uses); they sign the
acceptance with the key an operator pinned when enrolling them. Neither side can
later deny the exchange without breaking a hash the other holds.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import (
    CaseInstanceModel,
    HandoffActionModel,
    HandoffCounterpartyModel,
    HandoffWorkOrderModel,
)

from case_service.handoff import projection as proj

log = logging.getLogger(__name__)

#: The four verbs. Closed, and there is deliberately no verb that writes.
ACTIONS: tuple[str, ...] = ("accept", "read", "propose", "return")

#: A work order's default and maximum life. The roadmap says "expiry ≤ 24 h
#: default"; the ceiling is here so a dispatcher cannot hand out a week.
DEFAULT_TTL_MINUTES = 8 * 60
MAX_TTL_MINUTES = 24 * 60

OPEN_STATUSES = ("issued", "accepted")


class HandoffError(ValueError):
    """The handoff cannot proceed as asked. The message is for a human."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _sha(payload: Any) -> str:
    return hashlib.sha256(_canonical(payload).encode()).hexdigest()


# ── the counterparty registry ────────────────────────────────────────────────


def key_fingerprint(public_key_b64: str) -> str:
    """The name an operator checks against what they were told by phone."""
    try:
        raw = base64.b64decode(public_key_b64, validate=True)
    except Exception as exc:                                     # noqa: BLE001
        raise HandoffError("the public key is not valid base64") from exc
    if len(raw) != 32:
        raise HandoffError(
            "an Ed25519 public key is 32 bytes; this one is "
            f"{len(raw)}. A key of the wrong length is a key from the wrong "
            "algorithm, and accepting it would pin nothing.")
    return hashlib.sha256(raw).hexdigest()[:32]


async def enrol(
    session: AsyncSession, *, tenant_uuid: uuid.UUID, slug: str,
    display_name: str, public_key: str, max_ceiling: str = proj.CEILING_INTERNAL,
    notes: str | None = None, created_by: str | None = None,
) -> HandoffCounterpartyModel:
    """Pin an organisation's key. There is no self-registration path."""
    proj.rank(max_ceiling)
    fingerprint = key_fingerprint(public_key)
    existing = (await session.execute(
        select(HandoffCounterpartyModel).where(
            HandoffCounterpartyModel.tenant_id == tenant_uuid,
            HandoffCounterpartyModel.slug == slug))).scalars().first()
    if existing is not None:
        raise HandoffError(
            f"a counterparty {slug!r} is already enrolled. Rotating a key is a "
            f"deliberate act on the existing row, not a second enrolment — two "
            f"rows for one organisation is how an old key stays live.")
    row = HandoffCounterpartyModel(
        id=uuid.uuid4(), tenant_id=tenant_uuid, slug=slug,
        display_name=display_name, public_key=public_key, key_id=fingerprint,
        status="active", max_ceiling=max_ceiling, notes=notes,
        created_by=created_by)
    session.add(row)
    await session.flush()
    return row


def verify_signature(counterparty: HandoffCounterpartyModel, message: str,
                     signature_b64: str) -> tuple[bool, str]:
    """Against the PINNED key, never a key that arrived with the message.

    A key travelling with the thing it authenticates proves the sender owns a
    key. It says nothing about who they are, which is the only question the
    counterparty registry exists to answer.
    """
    from cryptography.hazmat.primitives.asymmetric import ed25519

    try:
        pub = ed25519.Ed25519PublicKey.from_public_bytes(
            base64.b64decode(counterparty.public_key, validate=True))
        pub.verify(base64.b64decode(signature_b64, validate=True), message.encode())
    except Exception:                                            # noqa: BLE001
        return False, "the signature does not verify against the pinned key"
    return True, counterparty.key_id


# ── issuing a work order ─────────────────────────────────────────────────────


def order_document(order: HandoffWorkOrderModel) -> dict[str, Any]:
    """What both signatures cover: the grant, not its bookkeeping."""
    return {
        "work_order_id": str(order.id),
        "case_id": str(order.case_id),
        "counterparty_id": str(order.counterparty_id),
        "ceiling": order.ceiling,
        "allowed_actions": sorted(order.allowed_actions or []),
        "expires_at": order.expires_at.isoformat() if order.expires_at else None,
        "obligations": order.obligations,
    }


async def _our_signature(session: AsyncSession, tenant_uuid: uuid.UUID,
                         document: dict[str, Any]) -> str | None:
    """Signed with a key derived from the tenant DEK, never a stored one.

    The same construction the model marketplace uses, and for the same reason:
    nothing new to protect and no key material in a settings blob that admin
    screens return wholesale. An install with no readable DEK issues an unsigned
    order, which is a stated fact on the row rather than a silent downgrade.
    """
    try:
        from case_service.hxvault import keyring
        from velaris_shared.modelmarket import packaging

        dek = keyring.cached_dek(tenant_uuid) or await keyring.ensure_dek(
            session, tenant_uuid)
        return json.dumps(packaging.sign(dek, _sha(document)))
    except Exception as exc:                                     # noqa: BLE001
        log.info("handoff: issuing unsigned (%s)", exc)
        return None


async def issue(
    session: AsyncSession, *, tenant_uuid: uuid.UUID, tenant_token: str,
    case_id: uuid.UUID, counterparty: HandoffCounterpartyModel,
    ceiling: str, allowed_actions: list[str], ttl_minutes: int | None = None,
    obligations: dict[str, Any] | None = None, created_by: str | None = None,
) -> tuple[HandoffWorkOrderModel, str]:
    """Mint the order and its token. Returns `(order, raw_token)`.

    The token is returned ONCE and never stored — the row keeps the grant id, so
    the order can be revoked without anybody having to hold the secret.
    """
    if counterparty.status != "active":
        raise HandoffError(f"{counterparty.slug!r} is {counterparty.status}")

    # The dispatcher can never exceed the enrolment. "What may this
    # organisation ever see" was decided once, by whoever enrolled them.
    effective = proj.at_most(ceiling, counterparty.max_ceiling)
    verbs = sorted({str(a) for a in (allowed_actions or [])})
    unknown = [v for v in verbs if v not in ACTIONS]
    if unknown:
        raise HandoffError(f"unknown handoff verb(s): {', '.join(unknown)}")
    if not verbs:
        raise HandoffError("a work order with no verbs can do nothing")

    case = await session.get(CaseInstanceModel, case_id)
    if case is None:
        raise HandoffError("no such case")

    # One open order per case, on EVERY dialect. PostgreSQL also has a partial
    # unique index; MySQL and MariaDB cannot express one, and a rule enforced
    # only where the database happens to support it is a rule two backends do
    # not have.
    open_order = (await session.execute(
        select(HandoffWorkOrderModel).where(
            HandoffWorkOrderModel.case_id == case_id,
            HandoffWorkOrderModel.status.in_(OPEN_STATUSES)))).scalars().first()
    if open_order is not None:
        raise HandoffError(
            "this case already has an open work order; two external parties "
            "working one case at once is a merge conflict nobody asked for")

    ttl = int(ttl_minutes or DEFAULT_TTL_MINUTES)
    if ttl <= 0 or ttl > MAX_TTL_MINUTES:
        raise HandoffError(f"ttl_minutes must be between 1 and {MAX_TTL_MINUTES}")

    # **A work order may not outlive the token that carries it.** The
    # deployment's own agent-token ceiling is the shorter one on most installs,
    # and an order row claiming a window its token cannot honour would show a
    # counterparty an expiry that stops working hours before it says. The
    # clamp is silent by design in only one direction: it can shorten, never
    # lengthen, and the row records what was actually granted.
    from velaris_shared.config import get_settings

    token_ceiling = int(get_settings().agent_token_max_ttl_minutes)
    ttl = min(ttl, token_ceiling)

    agent, raw_token, grant = await _external_agent_token(
        session, tenant_token=tenant_token, counterparty=counterparty,
        case_id=case_id, ttl_minutes=ttl, issued_by=created_by)

    order = HandoffWorkOrderModel(
        id=uuid.uuid4(), tenant_id=tenant_uuid, case_id=case_id,
        counterparty_id=counterparty.id, agent_id=getattr(agent, "id", None),
        ceiling=effective, allowed_actions=verbs, obligations=obligations,
        status="issued", expires_at=_utcnow() + timedelta(minutes=ttl),
        token_grant_id=getattr(grant, "id", None), created_by=created_by)
    order.order_signature = await _our_signature(
        session, tenant_uuid, order_document(order))
    session.add(order)
    await session.flush()
    return order, raw_token


async def _external_agent_token(
    session: AsyncSession, *, tenant_token: str,
    counterparty: HandoffCounterpartyModel, case_id: uuid.UUID,
    ttl_minutes: int, issued_by: str | None,
):
    """One F-00 agent per counterparty, kind `external`, case-scoped token.

    F-00 deliberately left `external` out of the kinds it issues, saying the
    code path admitting it would be "a code change with a review". This is that
    path, and it is the ONLY one: `identity.ISSUABLE_KINDS` still excludes
    `external`, so nothing else on the platform can mint one by accident.

    The agent holds `agent.read_case` and `agent.propose` — never
    `agent.act_on_case`. An external party that could act on a case would make
    the proposals-only rule a convention rather than a ceiling.
    """
    from case_service.agents import identity, tokens

    slug = f"handoff.{counterparty.slug}"
    agent = await identity.ensure_agent(
        session, tenant_id=counterparty.tenant_id, slug=slug, kind="external",
        display_name=counterparty.display_name,
        capabilities=["agent.read_case", "agent.propose"],
        # The one door in the platform through which an `external` principal
        # comes into being — see `identity.create_agent`.
        allow_external=True)
    raw, grant = await tokens.mint_agent_token(
        session, agent, capabilities=["agent.read_case", "agent.propose"],
        ttl_minutes=ttl_minutes, case_id=case_id,
        label=f"handoff:{counterparty.slug}", issued_by=issued_by)
    return agent, raw, grant


# ── the protocol verbs ───────────────────────────────────────────────────────


async def _record(session: AsyncSession, order: HandoffWorkOrderModel, *,
                  kind: str, decision: str, reason: str | None = None,
                  projection: Any = None, payload_sha256: str | None = None,
                  proposal_id: uuid.UUID | None = None) -> HandoffActionModel:
    row = HandoffActionModel(
        id=uuid.uuid4(), work_order_id=order.id, kind=kind, decision=decision,
        reason=reason, projection=projection, payload_sha256=payload_sha256,
        proposal_id=proposal_id)
    session.add(row)
    await session.flush()
    return row


async def guard(session: AsyncSession, order: HandoffWorkOrderModel,
                verb: str) -> HandoffCounterpartyModel:
    """Every reason this verb may not run now. Refusals are RECORDED.

    Checked in this order on purpose: the counterparty's own status first,
    because suspending an organisation must stop everything it has outstanding
    without anybody visiting each order.
    """
    counterparty = await session.get(HandoffCounterpartyModel, order.counterparty_id)
    if counterparty is None or counterparty.status != "active":
        await _record(session, order, kind=verb, decision="denied",
                      reason="the counterparty is suspended or gone")
        raise HandoffError("this counterparty is not active")
    if order.status not in OPEN_STATUSES:
        await _record(session, order, kind=verb, decision="denied",
                      reason=f"the work order is {order.status}")
        raise HandoffError(f"this work order is {order.status}")
    expires = order.expires_at
    if expires is not None and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires is not None and expires <= _utcnow():
        order.status = "expired"
        await _record(session, order, kind=verb, decision="denied",
                      reason="the work order has expired")
        raise HandoffError("this work order has expired")
    if verb not in (order.allowed_actions or []):
        await _record(session, order, kind=verb, decision="denied",
                      reason=f"{verb!r} is not a verb this order permits")
        raise HandoffError(f"this work order does not permit {verb!r}")
    return counterparty


async def accept(session: AsyncSession, *, order: HandoffWorkOrderModel,
                 signature: str) -> HandoffWorkOrderModel:
    """They sign the order document with the key we pinned. Nothing else counts."""
    counterparty = await guard(session, order, "accept")
    if order.status == "accepted":
        raise HandoffError("this work order has already been accepted")
    ok, detail = verify_signature(counterparty, _sha(order_document(order)), signature)
    if not ok:
        await _record(session, order, kind="accept", decision="denied", reason=detail)
        raise HandoffError(detail)
    order.status = "accepted"
    order.accepted_at = _utcnow()
    order.acceptance_signature = signature
    await _record(session, order, kind="accept", decision="allowed",
                  reason=f"signature verified against pinned key {detail}")
    return order


async def read(session: AsyncSession, *, order: HandoffWorkOrderModel,
               tenant_token: str) -> dict[str, Any]:
    """The projection, and a record of exactly what it disclosed."""
    await guard(session, order, "read")
    case = await session.get(CaseInstanceModel, order.case_id)
    if case is None:
        raise HandoffError("the case no longer exists")
    view = await proj.project(session, case=case, ceiling=order.ceiling,
                              tenant_token=tenant_token)
    await _record(session, order, kind="read", decision="allowed",
                  projection={"disclosed": view["disclosed"],
                              "withheld": view["withheld"]})
    return view


async def propose(
    session: AsyncSession, *, order: HandoffWorkOrderModel, tenant_token: str,
    target: str, value: Any, note: str | None = None,
) -> dict[str, Any]:
    """An external agent's suggestion, into the same inbox an email's goes to.

    Bounded by `capture.service.allowed_targets`, so an external agent cannot
    propose anything an injected email could not. A target outside it is
    refused and the refusal is recorded — dropping it silently would leave the
    counterparty believing they had contributed something.
    """
    await guard(session, order, "propose")
    from case_service.capture import service as capture

    case = await session.get(CaseInstanceModel, order.case_id)
    if case is None:
        raise HandoffError("the case no longer exists")
    targets = await capture.allowed_targets(session, case)
    if target not in targets:
        await _record(session, order, kind="propose", decision="denied",
                      reason=f"{target!r} is not a variable this case type defines",
                      payload_sha256=_sha({"target": target, "value": value}))
        raise HandoffError(
            f"{target!r} is not one of the variables this case type defines; "
            f"proposals are bounded by the case type, not by the sender")

    proposal = await _store_proposal(session, order=order, case=case,
                                     target=target, value=value, note=note,
                                     current=targets.get(target) or {})
    await _record(session, order, kind="propose", decision="allowed",
                  proposal_id=proposal.id,
                  payload_sha256=_sha({"target": target, "value": value}))
    return {"proposal_id": str(proposal.id), "status": "pending",
            "note": ("Recorded as a proposal. It changes nothing until a person "
                     "on our side confirms it.")}


async def _store_proposal(session: AsyncSession, *, order, case, target, value,
                          note, current):
    """Write into F-06's proposal store, not through F-06's extraction path.

    The pipeline in `capture.service.run` fetches source text and checks consent
    for a source kind it knows how to fetch. A handoff has no such source: the
    text came from an external agent over the protocol. Reusing the STORE gives
    the human the Captured tab, the ledger binding and `decide()`'s
    apply-as-the-confirming-person; reusing the PIPELINE would mean teaching it
    about a source it cannot read.
    """
    from velaris_shared.db.models import CaptureProposalModel, CaptureRunModel

    run = CaptureRunModel(
        id=uuid.uuid4(), tenant_id=order.tenant_id, case_id=order.case_id,
        source_kind="handoff", source_ref=str(order.id),
        source_sha256=_sha({"work_order": str(order.id), "target": target}),
        source_locator=f"handoff:{order.id}", status="complete",
        requested_by=f"handoff:{order.counterparty_id}",
        proposed_count=1, dropped_count=0, completed_at=_utcnow())
    session.add(run)
    await session.flush()

    proposal = CaptureProposalModel(
        id=uuid.uuid4(), run_id=run.id, tenant_id=order.tenant_id,
        case_id=order.case_id, kind="variable_update", target=target,
        current_value=capture_box(current.get("value")),
        proposed_value=capture_box(value),
        confidence=None, snippet=(note or "")[:2000] or None,
        locator=f"handoff:{order.id}", status="pending")
    session.add(proposal)
    await session.flush()
    return proposal


def capture_box(value: Any) -> dict[str, Any]:
    """F-06's boxing, imported rather than reimplemented.

    `PortableJSON` passes a bare `str` through unencoded on non-PostgreSQL
    dialects and then `json.loads` everything it reads back, so an unboxed
    string note raises on the next read of the case. That trap is F-06's to
    document and this module's to respect.
    """
    from case_service.capture.service import box

    return box(value)


async def hand_back(session: AsyncSession, *, order: HandoffWorkOrderModel,
                    summary: str) -> HandoffWorkOrderModel:
    """They give the case back. The token is revoked in the same breath."""
    await guard(session, order, "return")
    order.status = "returned"
    order.returned_at = _utcnow()
    order.return_summary = (summary or "")[:4000]
    await _revoke_token(session, order)
    await _record(session, order, kind="return", decision="allowed",
                  reason="handed back by the counterparty")
    return order


async def revoke(session: AsyncSession, *, order: HandoffWorkOrderModel,
                 reason: str) -> HandoffWorkOrderModel:
    """We take it away. Instant, because the TOKEN GRANT is what carries it.

    The order row is updated for the record; what actually stops the
    counterparty is the F-00 grant being revoked, which every request resolves
    against. A revocation that depended on this table being read would be as
    fast as the next cache expiry.
    """
    if order.status not in OPEN_STATUSES:
        raise HandoffError(f"this work order is {order.status}")
    order.status = "revoked"
    order.revoked_at = _utcnow()
    order.revoked_reason = reason
    await _revoke_token(session, order)
    await _record(session, order, kind="return", decision="allowed",
                  reason=f"revoked: {reason}")
    return order


async def _revoke_token(session: AsyncSession, order: HandoffWorkOrderModel) -> None:
    if order.token_grant_id is None:
        return
    try:
        from velaris_shared.db.models import AgentTokenGrantModel
        from case_service.agents import tokens

        grant = await session.get(AgentTokenGrantModel, order.token_grant_id)
        if grant is not None:
            await tokens.revoke_grant(session, grant)
    except Exception as exc:                                     # noqa: BLE001
        # The order is closed either way; an unrevoked grant would still be
        # refused by `guard`, but it must be visible that the fast path failed.
        log.warning("handoff: could not revoke grant for order %s: %s",
                    order.id, exc)
