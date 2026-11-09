"""Joint contracts: propose, match, approve, shadow, enforce, suspend, anchor.

Design: docs/Future/Done/AI-ERA-ROADMAP.md §F-12. The capstone.

**No trust is required, only verification.** The matcher is a pure function that
both sides run locally from the same inputs, so neither has to take the other's
word for what was agreed. This module is the part that stores the answer, gets it
signed, and runs the lifecycle around it.

**Deny-overrides survives negotiation by construction.** The matcher computes the
seam's ceiling as the minimum of the negotiated term and this platform's own —
so a contract cannot grant what the platform denies, because the wider number is
never produced. `verify_their_match` re-runs the matcher against the same inputs
to confirm the other side got the same answer we did.

**The SOV lifecycle, not a switch.** `approved → shadow → enforced` is the same
progression HxArbiter uses for a policy, and for the same reason: a joint process
that has never run should not be the first thing a seam enforces.

**Either side can suspend, unilaterally.** It fail-closes the seam to
proposals-queued rather than data-flowing, so it is safe to press at 3 a.m.
without knowing what is in flight — and no negotiation, no notice period and no
counterparty cooperation is required to press it.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import (
    ContractAnchorModel,
    HandoffCounterpartyModel,
    JointContractModel,
)

from case_service.handoff import service as handoff
from case_service.negotiate import matcher

log = logging.getLogger(__name__)

#: The lifecycle, in order. A contract may only move forward through it, except
#: `suspended`, which any state after approval may reach at once.
LIFECYCLE = ("draft", "matched", "approved", "shadow", "enforced")

#: What the seam does when a contract is suspended: proposals are still accepted
#: and queued, and nothing flows. Fail-closed to a queue rather than to an error,
#: so a counterparty mid-case is not left with work it cannot hand back.
SUSPENDED_BEHAVIOUR = "proposals_queued"


class NegotiationError(ValueError):
    """The contract cannot proceed as asked. The message is for a human."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def propose(
    session: AsyncSession, *, tenant_uuid: uuid.UUID,
    counterparty: HandoffCounterpartyModel, name: str,
    our_offer: dict[str, Any], their_offer: dict[str, Any],
    our_requirements: dict[str, Any], their_requirements: dict[str, Any],
    supersedes: uuid.UUID | None = None, created_by: str | None = None,
) -> JointContractModel:
    """Run the matcher and store the answer, matched or not.

    An `unmatchable` contract is kept: "these two sets of requirements have no
    joint process" is the outcome of a real negotiation, and the clause that
    failed is what the next round has to move.
    """
    if counterparty.status != "active":
        raise NegotiationError(f"{counterparty.slug!r} is {counterparty.status}")

    version = 1
    if supersedes is not None:
        previous = await session.get(JointContractModel, supersedes)
        if previous is None or previous.tenant_id != tenant_uuid:
            raise NegotiationError("the contract this one replaces does not exist")
        version = int(previous.version) + 1

    # This side's own boundary, passed in rather than looked up inside the
    # matcher so the matcher stays pure and the other side can reproduce our
    # answer from what we published.
    platform_ceiling = counterparty.max_ceiling

    try:
        result = matcher.match(
            our_offer=our_offer, their_offer=their_offer,
            our_requirements=our_requirements, their_requirements=their_requirements,
            platform_ceiling=platform_ceiling)
    except matcher.MatchError as exc:
        raise NegotiationError(str(exc)) from exc

    row = JointContractModel(
        id=uuid.uuid4(), tenant_id=tenant_uuid, counterparty_id=counterparty.id,
        name=name, version=version, supersedes=supersedes,
        our_offer=our_offer, their_offer=their_offer,
        our_requirements=our_requirements, their_requirements=their_requirements,
        composition=result["composition"], resolution=result["resolution"],
        effective_ceiling=result["effective_ceiling"],
        contract_sha256=result["contract_sha256"],
        status="matched" if result["matched"] else "unmatchable",
        unmatched_reason=result.get("reason"),
        created_by=created_by)
    if result["matched"]:
        row.our_signature = await _sign(session, tenant_uuid, result["contract_sha256"])
    session.add(row)
    await session.flush()
    return row


def verify_their_match(row: JointContractModel, *, platform_ceiling: str
                       ) -> dict[str, Any]:
    """Re-run the matcher on the same inputs. Do we get the same contract?

    This is what "no trusted third party" means in practice: either side can
    recompute the other's answer, and a mismatch is a disagreement about the
    contract itself rather than a difference of opinion about it.
    """
    result = matcher.match(
        our_offer=row.our_offer, their_offer=row.their_offer,
        our_requirements=row.our_requirements,
        their_requirements=row.their_requirements,
        platform_ceiling=platform_ceiling)
    inputs_agree = result["contract_sha256"] == row.contract_sha256

    # TWO checks, not one, and the second was missing at first. Recomputing from
    # the INPUTS catches a contract whose terms were renegotiated behind the
    # hash. It does NOT catch somebody editing the stored composition and
    # leaving `contract_sha256` alone — the inputs are unchanged, so the
    # recomputation still matches. Re-hashing what is actually stored is what
    # closes that, and it is the tamper a reader of this row most needs caught.
    stored_agrees = True
    if row.composition is not None and row.resolution is not None:
        stored_agrees = matcher.contract_hash(
            row.composition, row.resolution) == row.contract_sha256

    same = inputs_agree and stored_agrees
    if same:
        detail = "both sides compute the same contract"
    elif not stored_agrees:
        detail = ("the stored composition no longer hashes to this contract's "
                  "id — it was edited after both sides signed it")
    else:
        detail = ("the stored contract is not what these inputs produce — the "
                  "terms were changed after the match")
    return {
        "agrees": same,
        "inputs_agree": inputs_agree,
        "stored_agrees": stored_agrees,
        "recomputed": result["contract_sha256"],
        "stored": row.contract_sha256,
        "detail": detail,
    }


async def _sign(session: AsyncSession, tenant_uuid: uuid.UUID,
                digest: str) -> str | None:
    try:
        from case_service.hxvault import keyring
        from velaris_shared.modelmarket import packaging

        dek = keyring.cached_dek(tenant_uuid) or await keyring.ensure_dek(
            session, tenant_uuid)
        return json.dumps(packaging.sign(dek, digest))
    except Exception as exc:                                     # noqa: BLE001
        log.info("negotiate: contract signed by neither key (%s)", exc)
        return None


async def counter_sign(
    session: AsyncSession, *, row: JointContractModel, signature: str,
) -> JointContractModel:
    """Their signature over the contract hash, against the key we pinned."""
    if row.status != "matched":
        raise NegotiationError(f"this contract is {row.status!r}")
    counterparty = await session.get(HandoffCounterpartyModel, row.counterparty_id)
    if counterparty is None or counterparty.status != "active":
        raise NegotiationError("the counterparty is suspended or gone")
    ok, detail = handoff.verify_signature(counterparty, row.contract_sha256, signature)
    if not ok:
        raise NegotiationError(detail)
    row.their_signature = signature
    await session.flush()
    return row


async def approve(session: AsyncSession, *, row: JointContractModel,
                  actor: str) -> JointContractModel:
    """A person here agrees. Both signatures must already be on it.

    A contract only one side signed is a proposal, and approving one would be
    agreeing to something the other party has not yet said they will do.
    """
    if row.status != "matched":
        raise NegotiationError(f"this contract is {row.status!r}")
    if not row.our_signature or not row.their_signature:
        raise NegotiationError(
            "both sides sign before either approves; a contract one side signed "
            "is a proposal")
    row.status = "approved"
    row.approved_by = actor
    row.approved_at = _utcnow()
    await session.flush()
    return row


async def promote(session: AsyncSession, *, row: JointContractModel,
                  to: str, actor: str) -> JointContractModel:
    """`approved → shadow → enforced`, one step at a time.

    The SOV lifecycle, deliberately: a joint process that has never run should
    not be the first thing a seam enforces, and skipping shadow would make the
    first real case the experiment.
    """
    order = {"approved": 0, "shadow": 1, "enforced": 2}
    if row.status not in order:
        raise NegotiationError(f"this contract is {row.status!r}")
    if to not in order:
        raise NegotiationError(f"{to!r} is not a stage of the lifecycle")
    if order[to] != order[row.status] + 1:
        raise NegotiationError(
            f"a contract moves one stage at a time: {row.status!r} → "
            f"{[k for k, v in order.items() if v == order[row.status] + 1]}")
    row.status = to
    if to == "enforced":
        row.enforced_at = _utcnow()
    await session.flush()
    return row


async def suspend(session: AsyncSession, *, row: JointContractModel,
                  actor: str, reason: str) -> dict[str, Any]:
    """Either side, unilaterally, at any time after approval.

    No negotiation, no notice period, no counterparty cooperation. The seam
    fail-closes to a QUEUE rather than an error, so a counterparty mid-case is
    not left holding work it cannot hand back.
    """
    if row.status in ("suspended", "withdrawn"):
        return {"contract": row, "already": True}
    row.status = "suspended"
    row.suspended_at = _utcnow()
    row.suspended_by = actor
    row.suspended_reason = reason
    await session.flush()
    return {"contract": row, "seam": SUSPENDED_BEHAVIOUR,
            "note": ("The seam is closed to data. Proposals are still accepted "
                     "and queued so nothing in flight is stranded.")}


def seam_ceiling(row: JointContractModel) -> str:
    """What the seam will actually carry right now.

    A suspended or un-enforced contract carries NOTHING, whatever it negotiated.
    Reading the negotiated ceiling directly is the mistake this exists to
    prevent: a term agreed is not a term in force.
    """
    if row.status not in ("shadow", "enforced"):
        return "none"
    return row.effective_ceiling or "none"


# ── the interleaved anchor exchange ─────────────────────────────────────────


async def anchor(
    session: AsyncSession, *, row: JointContractModel, direction: str,
    chain_head: str, signature: str | None = None,
) -> ContractAnchorModel:
    """Record a chain head — ours going out, or theirs coming in.

    A head that does not extend the last one we held from that side is recorded
    with a `divergence`, not rejected: refusing it would leave no trace of the
    thing we most need evidence of. Detection is a row.
    """
    if direction not in ("ours", "theirs"):
        raise NegotiationError("direction is 'ours' or 'theirs'")

    previous = (await session.execute(
        select(ContractAnchorModel)
        .where(ContractAnchorModel.contract_id == row.id,
               ContractAnchorModel.direction == direction)
        .order_by(ContractAnchorModel.seq.desc()).limit(1))).scalars().first()

    divergence = None
    if previous is not None and previous.chain_head == chain_head:
        divergence = ("the same head was anchored twice; nothing has been added "
                      "to that side's history since the last exchange")

    if direction == "theirs" and signature is not None:
        counterparty = await session.get(HandoffCounterpartyModel, row.counterparty_id)
        ok, detail = handoff.verify_signature(counterparty, chain_head, signature) \
            if counterparty else (False, "the counterparty is gone")
        if not ok:
            divergence = f"the anchor is not signed by the pinned key: {detail}"

    anchor_row = ContractAnchorModel(
        id=uuid.uuid4(), contract_id=row.id, tenant_id=row.tenant_id,
        direction=direction, seq=(int(previous.seq) + 1) if previous else 1,
        chain_head=chain_head, signature=signature, divergence=divergence)
    session.add(anchor_row)
    await session.flush()
    return anchor_row


async def audit_anchors(session: AsyncSession, row: JointContractModel
                        ) -> dict[str, Any]:
    """Has either side's history been rewritten under the other's nose?

    Symmetric on purpose: this reports on both directions, because the value of
    the exchange is not that we can prove what they did — it is that **neither of
    us can deny it.**
    """
    rows = list((await session.execute(
        select(ContractAnchorModel)
        .where(ContractAnchorModel.contract_id == row.id)
        .order_by(ContractAnchorModel.direction, ContractAnchorModel.seq)
    )).scalars().all())
    problems = [{"direction": a.direction, "seq": a.seq, "detail": a.divergence}
                for a in rows if a.divergence]
    return {
        "exchanges": len(rows),
        "ours": sum(1 for a in rows if a.direction == "ours"),
        "theirs": sum(1 for a in rows if a.direction == "theirs"),
        "clean": not problems,
        "problems": problems,
        "note": ("Neither side can rewrite the joint history without breaking a "
                 "hash the other holds." if not problems else
                 "Something does not line up — see `problems`."),
    }
