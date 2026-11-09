"""HxLedger — double-entry ledger & reconciliation (§8.4).

The platform recorded money movement (`payment_requests`,
`payment_disbursements`, `invoice_records`) but never balanced it. That is an
audit gap, not a reporting one: without a reconciling ledger there is no
mechanical way to show that what was collected equals what was recorded.

Four rules, each enforced rather than documented:

**A transaction balances or it does not exist.** Debits must equal credits at
write time; an unbalanced posting raises and stores nothing. Checking later
would mean discovering broken books after they had been relied on.

**Append-only.** A mistake is corrected with a reversing transaction, never an
edit or a delete. An edited ledger cannot answer "what did the books say on the
3rd", which is exactly what an auditor asks.

**Integer cents.** Money is never a float, matching the existing payment
models. Floats accumulate rounding error that reconciliation then reports as a
real discrepancy.

**One currency per transaction.** Mixing currencies inside one transaction
without an explicit FX rate produces a total that means nothing, so it is
refused.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import (
    LedgerAccountModel,
    LedgerEntryModel,
    LedgerTransactionModel,
)

logger = logging.getLogger(__name__)

DEBIT = "debit"
CREDIT = "credit"

# ── Account types ─────────────────────────────────────────────────
ASSET = "asset"
LIABILITY = "liability"
INCOME = "income"
EXPENSE = "expense"
EQUITY = "equity"

ACCOUNT_TYPES = frozenset({ASSET, LIABILITY, INCOME, EXPENSE, EQUITY})

#: Accounts where a debit *increases* the balance. For the rest a credit does.
#: Getting this backwards silently inverts every report, so it is stated once
#: here rather than re-derived at each call site.
DEBIT_POSITIVE = frozenset({ASSET, EXPENSE})


class LedgerError(ValueError):
    """The posting would produce books that do not balance — refused."""


async def ensure_account(
    session: AsyncSession, *, code: str, name: str, account_type: str,
    currency: str = "usd", tenant_id: str | None = None,
) -> LedgerAccountModel:
    """Fetch or create an account. Idempotent on (tenant, code)."""
    if account_type not in ACCOUNT_TYPES:
        raise LedgerError(
            f"{account_type!r} is not an account type. Known: "
            f"{', '.join(sorted(ACCOUNT_TYPES))}"
        )
    existing = (await session.execute(
        select(LedgerAccountModel).where(
            LedgerAccountModel.code == code,
            LedgerAccountModel.tenant_id == tenant_id,
        )
    )).scalar_one_or_none()
    if existing is not None:
        return existing

    account = LedgerAccountModel(
        code=code, name=name, account_type=account_type,
        currency=currency, tenant_id=tenant_id,
    )
    session.add(account)
    await session.flush()
    return account


async def post(
    session: AsyncSession,
    *,
    lines: list[dict[str, Any]],
    description: str | None = None,
    currency: str = "usd",
    source_type: str | None = None,
    source_id: uuid.UUID | None = None,
    purpose: str | None = None,
    case_id: uuid.UUID | None = None,
    tenant_id: str | None = None,
    posted_by: str | None = None,
    reverses_id: uuid.UUID | None = None,
) -> LedgerTransactionModel:
    """Post one balanced transaction.

    `lines` are ``{"account_id", "direction", "amount_cents", "description"?}``.

    Returns the existing transaction unchanged when this (source, purpose) has
    already been posted — a payment webhook firing twice must not double-post,
    and raising would make the caller's retry look like a failure.
    """
    if source_type and source_id and purpose:
        already = (await session.execute(
            select(LedgerTransactionModel).where(
                LedgerTransactionModel.source_type == source_type,
                LedgerTransactionModel.source_id == source_id,
                LedgerTransactionModel.purpose == purpose,
            )
        )).scalar_one_or_none()
        if already is not None:
            logger.info("ledger: %s/%s already posted — returning existing",
                        source_type, purpose)
            return already

    if len(lines) < 2:
        raise LedgerError("a double-entry transaction needs at least two lines")

    debits = credits = 0
    for i, line in enumerate(lines):
        direction = str(line.get("direction") or "")
        if direction not in (DEBIT, CREDIT):
            raise LedgerError(f"lines[{i}].direction must be {DEBIT} or {CREDIT}")
        amount = line.get("amount_cents")
        if not isinstance(amount, int) or isinstance(amount, bool):
            raise LedgerError(
                f"lines[{i}].amount_cents must be an integer number of cents — "
                f"floats accumulate rounding error that reconciliation then "
                f"reports as a real discrepancy"
            )
        if amount <= 0:
            raise LedgerError(
                f"lines[{i}].amount_cents must be positive; express the "
                f"direction with debit/credit, not a negative amount"
            )
        if direction == DEBIT:
            debits += amount
        else:
            credits += amount

    if debits != credits:
        raise LedgerError(
            f"transaction does not balance: debits {debits} != credits {credits}. "
            f"Refusing to store it — an unbalanced ledger is worse than none."
        )

    txn = LedgerTransactionModel(
        description=description, currency=currency,
        source_type=source_type, source_id=source_id, purpose=purpose,
        case_id=case_id, tenant_id=tenant_id, posted_by=posted_by,
        reverses_id=reverses_id,
    )
    session.add(txn)
    await session.flush()

    for line in lines:
        session.add(LedgerEntryModel(
            transaction_id=txn.id,
            account_id=line["account_id"],
            direction=line["direction"],
            amount_cents=line["amount_cents"],
            currency=currency,
            description=line.get("description"),
            source_type=source_type, source_id=source_id, purpose=purpose,
            case_id=case_id, tenant_id=tenant_id, posted_by=posted_by,
        ))
    await session.flush()
    return txn


async def reverse(
    session: AsyncSession, transaction_id: uuid.UUID, *, posted_by: str,
    reason: str | None = None,
) -> LedgerTransactionModel:
    """Post the mirror image of a transaction.

    This is how a mistake is corrected. The original stays exactly as written,
    so the books can still be reproduced as at any past date.
    """
    original = await session.get(LedgerTransactionModel, transaction_id)
    if original is None:
        raise LedgerError(f"Transaction {transaction_id} not found")

    existing_reversal = (await session.execute(
        select(LedgerTransactionModel).where(
            LedgerTransactionModel.reverses_id == transaction_id
        )
    )).scalar_one_or_none()
    if existing_reversal is not None:
        raise LedgerError("that transaction has already been reversed")

    lines = (await session.execute(
        select(LedgerEntryModel).where(
            LedgerEntryModel.transaction_id == transaction_id
        )
    )).scalars().all()

    flipped = [{
        "account_id": line.account_id,
        "direction": CREDIT if line.direction == DEBIT else DEBIT,
        "amount_cents": line.amount_cents,
        "description": f"reversal of {line.description or ''}".strip(),
    } for line in lines]

    return await post(
        session, lines=flipped, currency=original.currency,
        description=reason or f"Reversal of {original.description or transaction_id}",
        case_id=original.case_id, tenant_id=original.tenant_id,
        posted_by=posted_by, reverses_id=transaction_id,
        # Deliberately no source/purpose: a reversal is its own event, and
        # reusing the original's key would collide with the idempotency guard.
    )


async def balance(
    session: AsyncSession, account_id: uuid.UUID,
) -> dict[str, Any]:
    """Signed balance of one account, in cents.

    Sign follows the account type: for assets and expenses a debit increases
    the balance; for liabilities, income and equity a credit does.
    """
    account = await session.get(LedgerAccountModel, account_id)
    if account is None:
        raise LedgerError(f"Account {account_id} not found")

    rows = (await session.execute(
        select(LedgerEntryModel.direction, func.sum(LedgerEntryModel.amount_cents))
        .where(LedgerEntryModel.account_id == account_id)
        .group_by(LedgerEntryModel.direction)
    )).all()

    totals = {d: int(v or 0) for d, v in rows}
    debits, credits = totals.get(DEBIT, 0), totals.get(CREDIT, 0)
    signed = (
        debits - credits if account.account_type in DEBIT_POSITIVE
        else credits - debits
    )
    return {
        "account_id": str(account_id),
        "code": account.code,
        "account_type": account.account_type,
        "currency": account.currency,
        "debits_cents": debits,
        "credits_cents": credits,
        "balance_cents": signed,
    }


async def trial_balance(
    session: AsyncSession, *, tenant_id: str | None = None,
) -> dict[str, Any]:
    """Every account's totals, plus the whole-ledger balance check.

    In a correct double-entry ledger total debits equal total credits across
    all accounts. If they do not, something wrote entries outside `post()` —
    the one thing that guarantees balance — and that is worth surfacing loudly
    rather than leaving to be noticed at audit.
    """
    q = select(
        LedgerEntryModel.direction, func.sum(LedgerEntryModel.amount_cents)
    ).group_by(LedgerEntryModel.direction)
    if tenant_id:
        q = q.where(LedgerEntryModel.tenant_id == tenant_id)

    totals = {d: int(v or 0) for d, v in (await session.execute(q)).all()}
    debits, credits = totals.get(DEBIT, 0), totals.get(CREDIT, 0)

    aq = select(LedgerAccountModel)
    if tenant_id:
        aq = aq.where(LedgerAccountModel.tenant_id == tenant_id)
    accounts = (await session.execute(aq)).scalars().all()

    return {
        "total_debits_cents": debits,
        "total_credits_cents": credits,
        "balanced": debits == credits,
        "accounts": [await balance(session, a.id) for a in accounts],
    }


async def reconcile_payments(
    session: AsyncSession, *, tenant_id: str | None = None,
) -> dict[str, Any]:
    """Compare captured payments against what the ledger recorded (§8.4).

    A discrepancy means money moved without a matching book entry — the exact
    failure a ledger exists to catch. Reports rather than corrects: an
    automatic fix would paper over the cause.

    **Both sides are net, and both are scoped to cash.** Two traps sit here:

    * A refunded payment leaves the captured set, so the ledger side must lose
      it too. Comparing gross debits against current captures would report
      every refund as a permanent discrepancy — a reconciliation that is always
      broken is one nobody reads.
    * Both entries of a transaction carry the same `source_type`, so summing by
      direction alone nets a capture against its own income credit and always
      returns zero. The ledger side is therefore restricted to the **cash
      account**, where the movement actually is.
    """
    from velaris_shared.compliance.ledger_posting import CASH_CODE
    from velaris_shared.db.models import PaymentRequestModel

    pq = select(
        func.coalesce(func.sum(PaymentRequestModel.amount_cents), 0)
    ).where(PaymentRequestModel.status.in_(("captured", "succeeded", "paid")))
    if tenant_id:
        pq = pq.where(PaymentRequestModel.tenant_id == tenant_id)
    payments_total = int((await session.execute(pq)).scalar_one() or 0)

    lq = (
        select(LedgerEntryModel.direction,
               func.coalesce(func.sum(LedgerEntryModel.amount_cents), 0))
        .join(LedgerAccountModel,
              LedgerAccountModel.id == LedgerEntryModel.account_id)
        .where(
            LedgerEntryModel.source_type == "payment_request",
            LedgerAccountModel.code == CASH_CODE,
        )
        .group_by(LedgerEntryModel.direction)
    )
    if tenant_id:
        lq = lq.where(LedgerEntryModel.tenant_id == tenant_id)

    totals = {d: int(v or 0) for d, v in (await session.execute(lq)).all()}
    debits, credits = totals.get(DEBIT, 0), totals.get(CREDIT, 0)
    ledger_total = debits - credits

    return {
        "payments_captured_cents": payments_total,
        "ledger_debits_cents": debits,
        "ledger_refunds_cents": credits,
        "ledger_net_cents": ledger_total,
        "difference_cents": payments_total - ledger_total,
        "reconciled": payments_total == ledger_total,
    }
