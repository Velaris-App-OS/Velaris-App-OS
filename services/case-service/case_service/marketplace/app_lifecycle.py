"""Marketplace app data lifecycle — the "revoke + delete data" teardown lane.

Uninstall offers two modes (see `revoke_install`):
  * revoke         — close the gate, keep the app's data (re-install is instant)
  * revoke+delete  — also delete the app's own data via the teardown registered here

Only first-party official apps register a teardown: their data shape is known and
the deletion SQL is Velaris-authored. HxTest has no tables of its own (it reuses the
core Test Suite's `hxtest_*` tables), so its "data" is the AI-generated suites it
produced — those are deleted; the core builtin/conformance suites are untouched.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from typing import Awaitable, Callable

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import (
    CheckoutOrderModel, CheckoutServiceTokenModel, CheckoutWebhookEventModel,
    CheckoutWebhookIntegrationModel, StorefrontStoreModel,
    TestResultModel, TestRunModel, TestSuiteModel,
)


async def _teardown_hxtest(session: AsyncSession, tenant_id: str) -> dict:
    """Delete HxTest's own data: the AI-generated suites + their runs/results.

    Core suites (`source` in builtin/developer — smoke/component/security/
    conformance) are left intact; only `ai_generated` rows go.

    NOTE: `hxtest_suites` has no tenant_id column (generated suites are global), so
    this deletes ALL ai_generated suites, not just `tenant_id`'s. Fine for single-
    tenant; a multi-tenant deployment would need a tenant_id column on the suite to
    scope this. `tenant_id` is accepted now for that future scoping."""
    suite_ids = (await session.execute(
        select(TestSuiteModel.id).where(TestSuiteModel.source == "ai_generated")
    )).scalars().all()
    n_suites = n_runs = n_results = 0
    if suite_ids:
        run_ids = (await session.execute(
            select(TestRunModel.id).where(TestRunModel.suite_id.in_(suite_ids))
        )).scalars().all()
        if run_ids:
            n_results = (await session.execute(
                delete(TestResultModel).where(TestResultModel.run_id.in_(run_ids))
            )).rowcount or 0
            n_runs = (await session.execute(
                delete(TestRunModel).where(TestRunModel.id.in_(run_ids))
            )).rowcount or 0
        n_suites = (await session.execute(
            delete(TestSuiteModel).where(TestSuiteModel.id.in_(suite_ids))
        )).rowcount or 0
    return {"deleted": True, "suites": n_suites, "runs": n_runs, "results": n_results}


async def _teardown_hxcheckout(session: AsyncSession, tenant_id: str) -> dict:
    """Delete HxCheckout's own data for a tenant: service tokens, webhook
    integrations (+ their events), and orders (+ items/notifications via FK CASCADE).

    The Order/Return/Complaint CASES are core case-service data and are NOT deleted
    here — they remain in Case Manager; only the checkout_* rows go. checkout_orders
    has ON DELETE SET NULL to case_instances, so deleting orders never touches cases."""
    n_events = (await session.execute(
        delete(CheckoutWebhookEventModel).where(
            CheckoutWebhookEventModel.integration_id.in_(
                select(CheckoutWebhookIntegrationModel.id).where(
                    CheckoutWebhookIntegrationModel.tenant_id == tenant_id)))
    )).rowcount or 0
    n_integrations = (await session.execute(
        delete(CheckoutWebhookIntegrationModel).where(
            CheckoutWebhookIntegrationModel.tenant_id == tenant_id))).rowcount or 0
    n_tokens = (await session.execute(
        delete(CheckoutServiceTokenModel).where(
            CheckoutServiceTokenModel.tenant_id == tenant_id))).rowcount or 0
    # Items + notifications cascade from the order delete (FK ON DELETE CASCADE).
    n_orders = (await session.execute(
        delete(CheckoutOrderModel).where(
            CheckoutOrderModel.tenant_id == tenant_id))).rowcount or 0
    return {"deleted": True, "orders": n_orders, "tokens": n_tokens,
            "integrations": n_integrations, "webhook_events": n_events}


async def _teardown_hxstorefront(session: AsyncSession, tenant_id: str) -> dict:
    """Delete HxStorefront's own data for a tenant: deleting the tenant's stores
    cascades (FK ON DELETE CASCADE) to products, variants, options, images,
    categories, inventory logs, themes, pages, navigation, promotions, domains,
    SEO overrides, subscribers, media, and analytics events."""
    n_stores = (await session.execute(
        delete(StorefrontStoreModel).where(
            StorefrontStoreModel.tenant_id == tenant_id))).rowcount or 0
    return {"deleted": True, "stores": n_stores}


# package_id → teardown coroutine. Unlisted packages have no data teardown.
_TEARDOWNS: dict[str, Callable[[AsyncSession, str], Awaitable[dict]]] = {
    "velaris/hxtest": _teardown_hxtest,
    "velaris/hxcheckout": _teardown_hxcheckout,
    "velaris/hxstorefront": _teardown_hxstorefront,
}


async def teardown_package_data(session: AsyncSession, package_id: str, tenant_id: str) -> dict:
    """Run the registered data teardown for a package, if any. Caller commits."""
    fn = _TEARDOWNS.get(package_id)
    if fn is None:
        return {"deleted": False, "reason": "no data teardown registered for this package"}
    return await fn(session, tenant_id)
