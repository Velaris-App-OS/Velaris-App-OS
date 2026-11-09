"""HxLedger — the payment producer (§8.4).

`ledger.py` shipped with no caller, so the books were empty and
`GET /ledger/reconcile` reported every captured payment as a discrepancy. That
is not a ledger with a bug, it is a ledger nobody keeps. This module is the
missing producer: it turns Stripe payment events into balanced transactions.

**Policy lives here, mechanics live in `ledger.py`.** Which accounts a payment
touches is an accounting decision that will differ per deployment; that debits
must equal credits is not. Keeping them apart means the account mapping can be
changed without anyone touching the code that guarantees balance.

**A refund is not a reversal.** `ledger.reverse()` exists for a posting that was
*wrong* — it un-says something. A refund is a real business event that happened
after a real capture, and both belong in the history. So a refund is its own
transaction with its own purpose, which also keeps the idempotency key distinct
(a refund must not collide with the capture it follows).

**Posting is non-fatal.** A payment webhook must be acknowledged or Stripe
retries it, and losing a customer's payment to fix a bookkeeping row would be a
poor trade. A failed post therefore logs and returns — and then surfaces in
`reconcile_payments()`, which is the entire reason that function exists. The
ledger fails loudly in the place designed to be read, not silently at the till.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.compliance import ledger

logger = logging.getLogger(__name__)

SOURCE_PAYMENT = "payment_request"

SOURCE_DISBURSEMENT = "payment_disbursement"

CAPTURE = "capture"
REFUND = "refund"
DISBURSE = "disburse"

#: Default chart of accounts for payments. Codes follow the conventional
#: ranges (1xxx asset, 4xxx income) so an accountant reading a trial balance
#: recognises them without a key.
CASH_CODE = "1000"
CASH_NAME = "Cash / Payments Received"
INCOME_CODE = "4000"
INCOME_NAME = "Case Payment Income"
DISBURSEMENT_CODE = "5000"
DISBURSEMENT_NAME = "Case Disbursements"

#: Invoices deliberately have NO account here. Xero is the system of record for
#: invoicing, and posting accounts receivable in Velaris would be a second set
#: of books that can disagree with it. `invoice_records` carries the Xero id,
#: number and URL back to the case; that round-trip is the integration, not a
#: ledger entry. See docs/components/hxledger.md.


async def _accounts(
    session: AsyncSession, *, currency: str, tenant_id: str | None,
) -> tuple[Any, Any]:
    """Fetch or create the two accounts a payment touches. Idempotent."""
    cash = await ledger.ensure_account(
        session, code=CASH_CODE, name=CASH_NAME, account_type=ledger.ASSET,
        currency=currency, tenant_id=tenant_id,
    )
    income = await ledger.ensure_account(
        session, code=INCOME_CODE, name=INCOME_NAME, account_type=ledger.INCOME,
        currency=currency, tenant_id=tenant_id,
    )
    return cash, income


async def post_capture(session: AsyncSession, pr) -> Any | None:
    """Money arrived: debit cash, credit income.

    Idempotent through `ledger.post` on `(source, id, purpose)` — Stripe
    retries webhooks, and a retry must not double the books.

    Returns the transaction, or None if posting was skipped or failed. Never
    raises: see the module docstring.
    """
    from velaris_shared.config import get_settings

    if not get_settings().ledger_posting_enabled:
        return None

    amount = int(pr.amount_cents or 0)
    if amount <= 0:
        # Not an error worth failing on — a zero-amount request is a real
        # thing (a fully-discounted step), and posting a zero transaction
        # would fail the ledger's positive-amount rule for no benefit.
        logger.debug("ledger: payment %s is %d cents — nothing to post", pr.id, amount)
        return None

    currency = (pr.currency or "usd").lower()
    tenant_id = str(pr.tenant_id) if getattr(pr, "tenant_id", None) else None

    try:
        cash, income = await _accounts(session, currency=currency, tenant_id=tenant_id)
        return await ledger.post(
            session,
            lines=[
                {"account_id": cash.id, "direction": ledger.DEBIT,
                 "amount_cents": amount, "description": "payment captured"},
                {"account_id": income.id, "direction": ledger.CREDIT,
                 "amount_cents": amount, "description": "payment captured"},
            ],
            description=f"Payment captured for case {pr.case_id}",
            currency=currency,
            source_type=SOURCE_PAYMENT, source_id=pr.id, purpose=CAPTURE,
            case_id=pr.case_id, tenant_id=tenant_id, posted_by="stripe_webhook",
        )
    except Exception as exc:                       # noqa: BLE001 — see docstring
        logger.warning(
            "ledger: could not post capture for payment %s (%s) — reconciliation "
            "will report this", pr.id, exc,
        )
        return None


async def post_refund(session: AsyncSession, pr) -> Any | None:
    """Money went back: debit income, credit cash.

    The mirror of the capture, but recorded as its own event rather than as a
    reversal of it — both really happened, and the history should say so.
    """
    from velaris_shared.config import get_settings

    if not get_settings().ledger_posting_enabled:
        return None

    amount = int(pr.amount_cents or 0)
    if amount <= 0:
        return None

    currency = (pr.currency or "usd").lower()
    tenant_id = str(pr.tenant_id) if getattr(pr, "tenant_id", None) else None

    try:
        cash, income = await _accounts(session, currency=currency, tenant_id=tenant_id)
        return await ledger.post(
            session,
            lines=[
                {"account_id": income.id, "direction": ledger.DEBIT,
                 "amount_cents": amount, "description": "payment refunded"},
                {"account_id": cash.id, "direction": ledger.CREDIT,
                 "amount_cents": amount, "description": "payment refunded"},
            ],
            description=f"Payment refunded for case {pr.case_id}",
            currency=currency,
            source_type=SOURCE_PAYMENT, source_id=pr.id, purpose=REFUND,
            case_id=pr.case_id, tenant_id=tenant_id, posted_by="stripe_webhook",
        )
    except Exception as exc:                       # noqa: BLE001 — see docstring
        logger.warning(
            "ledger: could not post refund for payment %s (%s) — reconciliation "
            "will report this", pr.id, exc,
        )
        return None


async def post_disbursement(session: AsyncSession, disb) -> Any | None:
    """Money paid out on a case: debit the expense, credit cash.

    Hooked at `mark_disbursement_sent` rather than at creation. A disbursement
    is created as an *intent* and stays `pending` until staff confirm the bank
    transfer actually happened — posting at creation would put money on the
    books that had not moved, which is the mirror of the failure reconciliation
    exists to catch.

    Keyed on `(payment_disbursement, id, disburse)`, so a staff member clicking
    confirm twice does not pay it out twice in the books.

    Note the source type differs from a payment's, which keeps disbursements
    out of `reconcile_payments()` — that compares captured payments against
    cash received, and money going the other way is not a discrepancy in it.
    """
    from velaris_shared.config import get_settings

    if not get_settings().ledger_posting_enabled:
        return None

    amount = int(disb.amount_cents or 0)
    if amount <= 0:
        return None

    currency = (disb.currency or "usd").lower()
    tenant_id = str(disb.tenant_id) if getattr(disb, "tenant_id", None) else None

    try:
        cash = await ledger.ensure_account(
            session, code=CASH_CODE, name=CASH_NAME, account_type=ledger.ASSET,
            currency=currency, tenant_id=tenant_id,
        )
        expense = await ledger.ensure_account(
            session, code=DISBURSEMENT_CODE, name=DISBURSEMENT_NAME,
            account_type=ledger.EXPENSE, currency=currency, tenant_id=tenant_id,
        )
        return await ledger.post(
            session,
            lines=[
                {"account_id": expense.id, "direction": ledger.DEBIT,
                 "amount_cents": amount, "description": "disbursement executed"},
                {"account_id": cash.id, "direction": ledger.CREDIT,
                 "amount_cents": amount, "description": "disbursement executed"},
            ],
            description=f"Disbursement for case {disb.case_id}",
            currency=currency,
            source_type=SOURCE_DISBURSEMENT, source_id=disb.id, purpose=DISBURSE,
            case_id=disb.case_id, tenant_id=tenant_id, posted_by="disbursement_confirm",
        )
    except Exception as exc:                       # noqa: BLE001 — see module docstring
        logger.warning(
            "ledger: could not post disbursement %s (%s) — the trial balance "
            "will be short by this amount", disb.id, exc,
        )
        return None
