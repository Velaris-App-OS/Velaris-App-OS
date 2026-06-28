"""HxCheckout (#commerce) — order intake + management API.

HxCheckout is a marketplace `module` package (`velaris/hxcheckout`). Like HxTest,
enablement is the STANDARD marketplace-install gate (not a bespoke flag): the API
is available for a tenant iff its package is installed (a non-revoked
marketplace_installs row). Until the marketplace is activated and the package
installed, every endpoint 404s. The Python ships in-image; install only flips
these routes + the Studio module on per tenant.

Endpoints (prefix /api/v1/checkout):
  Service-token auth (external sites):
    POST   /orders                  create order → order_id + tracking_token + payment_url
    GET    /orders/{id}             order status (service token or staff JWT)
    POST   /orders/{id}/cancel      cancel (before dispatch)
  Webhook (HMAC):
    POST   /webhook/{integration_id}  inbound platform order event
  Admin (staff JWT):
    GET/POST/DELETE /tokens         service-token management
    GET/POST/PUT/DELETE /integrations  webhook integrations
    GET    /analytics/summary       order KPIs

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid

import json
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.auth.dependencies import get_current_user, require_admin
from case_service.auth.models import AuthenticatedUser
from case_service.checkout import service as order_svc
from case_service.checkout import tokens as token_svc
from case_service.checkout import webhooks as wh
from case_service.checkout.tokens import require_service_token
from case_service.db.models import (
    CheckoutOrderModel, CheckoutServiceTokenModel, CheckoutWebhookEventModel,
    CheckoutWebhookIntegrationModel, MarketplaceInstallModel, _utcnow,
)
from case_service.db.session import get_session
from case_service.middleware.endpoint_rate_limit import SlidingWindowLimiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/checkout", tags=["checkout"])

# The marketplace package whose install enables HxCheckout. Must match the `id` in
# the published velaris.json/manifest.json and the official_registry.json allowlist —
# a mismatch means HxCheckout never enables on install. (4-place id contract.)
HXCHECKOUT_PACKAGE_ID = "velaris/hxcheckout"


async def require_installed(session: AsyncSession, tenant: str) -> None:
    """Marketplace-install gate: HxCheckout is available iff its package is installed
    (non-revoked) for `tenant`. No bespoke flag — the universal "feature flag enabled
    on install" mechanism. The marketplace_installs table only exists once the
    marketplace itself is activated; until then (and whenever not installed) → 404.
    Stays DB-agnostic (no Postgres-only to_regclass). Unlike HxTest's gate this does
    NOT wrap the check in a SAVEPOINT: a savepoint here was observed to make the inner
    SELECT miss already-committed install rows when the request had already issued a
    read (e.g. the webhook path loads its integration first). A savepoint isn't needed
    — on the rare truly-absent-table case the SELECT raises, we catch it and 404
    immediately with no further DB work in the request, so the aborted statement can't
    poison anything (get_session rolls back on the raised HTTPException)."""
    try:
        row = (await session.execute(
            select(MarketplaceInstallModel.id).where(
                MarketplaceInstallModel.tenant_id == tenant,
                MarketplaceInstallModel.package_id == HXCHECKOUT_PACKAGE_ID,
                MarketplaceInstallModel.revoked_at.is_(None),
            ).limit(1))).first()
    except (ProgrammingError, OperationalError):
        row = None                       # marketplace not yet activated → table absent
    if row is None:
        raise HTTPException(404, "HxCheckout is not installed on this instance")


async def require_enabled_user(session: AsyncSession, user: AuthenticatedUser) -> None:
    """Gate for staff-JWT (Studio) endpoints — tenant derived from the user."""
    await require_installed(session, user.tenant_id or "default")


# ══════════════════════════════════════════════════════════════════════════════
#  SERVICE-TOKEN MANAGEMENT  (Studio admin — HxCheckout > API Keys)
# ══════════════════════════════════════════════════════════════════════════════

class TokenCreate(BaseModel):
    label: str = ""
    mode: str = "live"           # live | test
    scope: str = "orders:create"


@router.get("/tokens")
async def list_tokens(
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """List service tokens for this tenant. Secrets are never returned — only the
    public prefix (vsk_<mode>_<keyid>), label, and usage metadata."""
    await require_enabled_user(session, user)
    tenant = user.tenant_id or "default"
    rows = (await session.execute(
        select(CheckoutServiceTokenModel)
        .where(CheckoutServiceTokenModel.tenant_id == tenant)
        .order_by(CheckoutServiceTokenModel.created_at.desc())
    )).scalars().all()
    return {"tokens": [
        {
            "id":           str(t.id),
            "label":        t.label,
            "token_prefix": t.token_prefix,
            "scope":        t.scope,
            "mode":         "test" if token_svc.token_is_test(t) else "live",
            "last_used_at": t.last_used_at.isoformat() if t.last_used_at else None,
            "revoked":      t.revoked_at is not None,
            "suspended":    t.suspended,
            "created_at":   t.created_at.isoformat(),
        }
        for t in rows
    ]}


@router.post("/tokens", status_code=201)
async def create_token(
    body: TokenCreate,
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Mint a new service token. The plaintext is returned ONCE here and never again
    (only the bcrypt hash is stored)."""
    await require_enabled_user(session, user)
    tenant = user.tenant_id or "default"
    plaintext, row = token_svc.generate_token(
        session, tenant_id=tenant, label=body.label, scope=body.scope,
        mode=body.mode, created_by=user.user_id,
    )
    await session.commit()
    return {
        "id":           str(row.id),
        "label":        row.label,
        "token_prefix": row.token_prefix,
        "scope":        row.scope,
        "token":        plaintext,   # shown once — store it securely now
        "_warning":     "This token is shown only once. Copy it now; it cannot be retrieved later.",
    }


@router.delete("/tokens/{token_id}")
async def revoke_token(
    token_id: str,
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Revoke a service token immediately — all subsequent calls with it → 401."""
    await require_enabled_user(session, user)
    row = await session.get(CheckoutServiceTokenModel, uuid.UUID(token_id))
    if row is None or row.tenant_id != (user.tenant_id or "default"):
        raise HTTPException(404, "Token not found")
    if row.revoked_at is None:
        row.revoked_at = _utcnow()
    await session.commit()
    return {"revoked": token_id}


# ══════════════════════════════════════════════════════════════════════════════
#  ORDER INTAKE  (service-token auth — external sites)
# ══════════════════════════════════════════════════════════════════════════════

class OrderCreate(BaseModel):
    basket: list[dict]                 # [{sku, name, quantity, unit_price, image_url?, metadata?}]
    customer: dict = {}                # {name, email, phone}
    shipping: dict = {}                # {address_line1, .., country, method}
    metadata: dict = {}                # free-form; stored on the order + case
    currency: str | None = None        # defaults to the store/platform currency


@router.post("/orders", status_code=201)
async def create_order(
    body: OrderCreate,
    token: CheckoutServiceTokenModel = Depends(require_service_token),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    session: AsyncSession = Depends(get_session),
):
    """Create an order from an external basket. Returns order_id + tracking_token +
    payment_url. Auth: service token (X-Velaris-Token). Idempotent via Idempotency-Key."""
    await require_installed(session, token.tenant_id)
    token_svc.check_rate_limit(session, token)
    return await order_svc.create_order(
        session,
        tenant=token.tenant_id,
        basket=body.basket,
        customer=body.customer,
        shipping=body.shipping,
        metadata=body.metadata,
        currency=body.currency,
        idempotency_key=idempotency_key,
        source="api",
        is_test=token_svc.token_is_test(token),
        created_by=f"hxcheckout:{token.token_prefix}",
    )


async def _get_owned_order(session: AsyncSession, order_id: str, tenant: str) -> CheckoutOrderModel:
    try:
        oid = uuid.UUID(order_id)
    except ValueError:
        raise HTTPException(404, "Order not found")
    order = await session.get(CheckoutOrderModel, oid)
    if order is None or order.tenant_id != tenant:
        raise HTTPException(404, "Order not found")
    return order


@router.get("/orders/{order_id}")
async def get_order(
    order_id: str,
    token: CheckoutServiceTokenModel = Depends(require_service_token),
    session: AsyncSession = Depends(get_session),
):
    """Order status + details. Auth: service token (scoped to its own tenant's orders)."""
    await require_installed(session, token.tenant_id)
    order = await _get_owned_order(session, order_id, token.tenant_id)
    return {
        "order_id":       str(order.id),
        "tracking_token": order.tracking_token,
        "status":         order.status,
        "currency":       order.currency,
        "total_cents":    order.total_cents,
        "customer":       order.customer,
        "shipping":       order.shipping,
        "is_test":        order.is_test,
        "case_id":        str(order.case_id) if order.case_id else None,
        "created_at":     order.created_at.isoformat(),
    }


@router.post("/orders/{order_id}/cancel")
async def cancel_order(
    order_id: str,
    token: CheckoutServiceTokenModel = Depends(require_service_token),
    session: AsyncSession = Depends(get_session),
):
    """Cancel an order — only before dispatch (i.e. while still pending payment or
    awaiting fulfilment). After dispatch, staff handle it as a return in Studio."""
    await require_installed(session, token.tenant_id)
    order = await _get_owned_order(session, order_id, token.tenant_id)
    if order.status not in (order_svc.STATUS_PENDING_PAYMENT, order_svc.STATUS_AWAITING_FULFILMENT):
        raise HTTPException(409, f"Order cannot be cancelled in status '{order.status}'")
    order.status = order_svc.STATUS_CANCELLED
    await session.commit()
    return {"cancelled": order_id, "status": order.status}


# ══════════════════════════════════════════════════════════════════════════════
#  CUSTOMER POST-DELIVERY  (returns / complaints — tracking-token proof)
# ══════════════════════════════════════════════════════════════════════════════

# Anti-guessing: the random 128-bit tracking token is the customer's proof of
# ownership; cap lookups per IP (doc security model: 10/min). Skipped under pytest.
_track_limiter = SlidingWindowLimiter(max_calls=10, window_seconds=60.0, name="checkout_track")


class CustomerCaseReq(BaseModel):
    tracking_token: str                 # proof of ownership (returned at checkout)
    reason: str = ""
    items: list = []


async def _order_by_token(session: AsyncSession, request: Request, order_id: str, tracking_token: str) -> CheckoutOrderModel:
    """Resolve an order by id, verifying the customer's tracking token. Rate-limited
    per IP; a wrong/absent token returns 404 (no existence oracle)."""
    import os
    if "PYTEST_CURRENT_TEST" not in os.environ:
        ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or (request.client.host if request.client else "?")
        allowed, retry_after = _track_limiter.allow(ip)
        if not allowed:
            raise HTTPException(429, "Too many lookups; please wait.", headers={"Retry-After": str(retry_after)})
    try:
        order = await session.get(CheckoutOrderModel, uuid.UUID(order_id))
    except ValueError:
        order = None
    if order is None or not tracking_token or order.tracking_token != tracking_token:
        raise HTTPException(404, "Order not found")
    return order


@router.post("/orders/{order_id}/returns", status_code=201)
async def request_return(
    order_id: str, body: CustomerCaseReq, request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Customer initiates a return → a Return sub-case linked to the Order case."""
    order = await _order_by_token(session, request, order_id, body.tracking_token)
    await require_installed(session, order.tenant_id)
    result = await order_svc.create_subcase(session, order=order, kind="return",
                                            reason=body.reason, items=body.items)
    await session.commit()
    return result


@router.post("/orders/{order_id}/complaints", status_code=201)
async def raise_complaint(
    order_id: str, body: CustomerCaseReq, request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Customer raises a complaint → a Complaint sub-case (uses the escalation engine)."""
    order = await _order_by_token(session, request, order_id, body.tracking_token)
    await require_installed(session, order.tenant_id)
    result = await order_svc.create_subcase(session, order=order, kind="complaint",
                                            reason=body.reason, items=body.items)
    await session.commit()
    return result


# ══════════════════════════════════════════════════════════════════════════════
#  ADMIN ORDER VIEWS  (Studio — staff JWT)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/orders")
async def list_orders(
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """List this tenant's orders for the Studio order board. Staff JWT (admin)."""
    await require_enabled_user(session, user)
    tenant = user.tenant_id or "default"
    stmt = select(CheckoutOrderModel).where(CheckoutOrderModel.tenant_id == tenant)
    if status:
        stmt = stmt.where(CheckoutOrderModel.status == status)
    stmt = stmt.order_by(CheckoutOrderModel.created_at.desc()).limit(min(limit, 200)).offset(offset)
    rows = (await session.execute(stmt)).scalars().all()
    return {"orders": [
        {
            "order_id": str(o.id), "tracking_token": o.tracking_token, "status": o.status,
            "currency": o.currency, "total_cents": o.total_cents,
            "customer_email": (o.customer or {}).get("email"), "source": o.source,
            "is_test": o.is_test, "case_id": str(o.case_id) if o.case_id else None,
            "created_at": o.created_at.isoformat(),
        } for o in rows
    ]}


@router.get("/analytics/summary")
async def analytics_summary(
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Order KPIs for the Studio dashboard: counts by status + revenue. Staff JWT."""
    from sqlalchemy import func
    await require_enabled_user(session, user)
    tenant = user.tenant_id or "default"
    rows = (await session.execute(
        select(CheckoutOrderModel.status, func.count(), func.coalesce(func.sum(CheckoutOrderModel.total_cents), 0))
        .where(CheckoutOrderModel.tenant_id == tenant, CheckoutOrderModel.is_test.is_(False))
        .group_by(CheckoutOrderModel.status)
    )).all()
    by_status = {s: {"count": c, "revenue_cents": int(rev)} for s, c, rev in rows}
    total_orders = sum(v["count"] for v in by_status.values())
    total_revenue = sum(v["revenue_cents"] for v in by_status.values())
    return {"total_orders": total_orders, "total_revenue_cents": total_revenue, "by_status": by_status}


# ══════════════════════════════════════════════════════════════════════════════
#  WEBHOOK MODE  (HMAC-authenticated — Shopify/WooCommerce/Magento/BigCommerce)
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/webhook/{integration_id}")
async def inbound_webhook(
    integration_id: str,
    request: Request,
    x_velaris_hmac_sha256: str | None = Header(default=None, alias="X-Velaris-HMAC-SHA256"),
    session: AsyncSession = Depends(get_session),
):
    """Inbound order event from an external platform. The HMAC signature is verified
    on the raw body BEFORE deserialization (invariant 4). Every event is logged in
    full regardless of outcome (invariant 6). Returns 401 on any auth failure with
    no detail (no integration-existence oracle)."""
    raw_body = await request.body()

    integ = None
    try:
        integ = await session.get(CheckoutWebhookIntegrationModel, uuid.UUID(integration_id))
    except ValueError:
        integ = None

    secret = wh.decrypt_secret(integ.hmac_secret_enc) if integ else None
    if integ is None or not integ.enabled or not wh.verify_hmac(raw_body, x_velaris_hmac_sha256, secret):
        # Log the rejection (best-effort; integration_id only if it resolved to a row).
        try:
            session.add(CheckoutWebhookEventModel(
                integration_id=(integ.id if integ else None),
                raw={"_unverified": True}, status="rejected",
                error="HMAC verification failed or integration not found/disabled"))
            await session.commit()
        except Exception as e:
            logger.warning("failed to log rejected webhook: %s", e)
        raise HTTPException(401, "Invalid webhook signature")

    # Verified — now safe to parse + map.
    try:
        raw = json.loads(raw_body.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        session.add(CheckoutWebhookEventModel(
            integration_id=integ.id, raw={"_undecodable": True}, status="error",
            error="payload is not valid JSON"))
        await session.commit()
        raise HTTPException(400, "Payload is not valid JSON")

    # Map + create the order, THEN write the event log as a single INSERT. The
    # event row is intentionally not added until after create_order's nested
    # savepoints have resolved (holding it across them corrupts the unit of work).
    await require_installed(session, integ.tenant_id)
    mapped: dict = {}
    try:
        mapped = wh.map_payload(integ.platform, raw, integ.field_map or {})
        # Replay defense: a captured valid-signature body can be resent forever
        # (HMAC alone has no freshness). Dedupe on the platform's own order id so a
        # resend returns the original order instead of creating a duplicate.
        platform_order_id = (mapped.get("metadata") or {}).get("platform_order_id")
        idem = f"wh:{integ.id}:{platform_order_id}" if platform_order_id else None
        result = await order_svc.create_order(
            session,
            tenant=integ.tenant_id,
            basket=mapped.get("basket") or [],
            customer=mapped.get("customer") or {},
            shipping=mapped.get("shipping") or {},
            metadata=mapped.get("metadata") or {},
            source="webhook",
            created_by=f"hxcheckout:webhook:{integ.id}",
            integration_id=integ.id,
            idempotency_key=idem,
        )
    except Exception as e:
        logger.warning("webhook %s mapping/order failed: %s", integ.id, e)
        raise HTTPException(422, "Could not process webhook payload")

    session.add(CheckoutWebhookEventModel(
        integration_id=integ.id, raw=raw, mapped=mapped,
        status="order_created", order_id=uuid.UUID(result["order_id"])))
    await session.commit()
    return {"order_id": result["order_id"], "tracking_token": result["tracking_token"]}


# ══════════════════════════════════════════════════════════════════════════════
#  WEBHOOK INTEGRATION MANAGEMENT  (Studio admin)
# ══════════════════════════════════════════════════════════════════════════════

class IntegrationCreate(BaseModel):
    platform: str = "custom"          # shopify|woocommerce|magento|bigcommerce|custom
    label: str = ""
    hmac_secret: str = ""             # shared secret; stored encrypted, never returned
    field_map: dict = {}              # {} = use the built-in platform map
    enabled: bool = True


class IntegrationUpdate(BaseModel):
    label: str | None = None
    hmac_secret: str | None = None
    field_map: dict | None = None
    enabled: bool | None = None


def _integration_dict(i: CheckoutWebhookIntegrationModel) -> dict:
    return {
        "id": str(i.id), "platform": i.platform, "label": i.label,
        "has_secret": bool(i.hmac_secret_enc), "field_map": i.field_map,
        "enabled": i.enabled, "created_at": i.created_at.isoformat(),
        "webhook_url": f"/api/v1/checkout/webhook/{i.id}",
    }


@router.get("/integrations")
async def list_integrations(
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await require_enabled_user(session, user)
    tenant = user.tenant_id or "default"
    rows = (await session.execute(
        select(CheckoutWebhookIntegrationModel)
        .where(CheckoutWebhookIntegrationModel.tenant_id == tenant)
        .order_by(CheckoutWebhookIntegrationModel.created_at.desc())
    )).scalars().all()
    return {"integrations": [_integration_dict(i) for i in rows]}


@router.post("/integrations", status_code=201)
async def create_integration(
    body: IntegrationCreate,
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await require_enabled_user(session, user)
    if body.platform not in wh.BUILTIN_PLATFORMS and not body.field_map:
        raise HTTPException(422, f"platform '{body.platform}' needs a field_map (no built-in mapper)")
    integ = CheckoutWebhookIntegrationModel(
        tenant_id=user.tenant_id or "default",
        platform=body.platform, label=body.label,
        hmac_secret_enc=wh.encrypt_secret(body.hmac_secret) if body.hmac_secret else None,
        field_map=body.field_map or {}, enabled=body.enabled, created_by=user.user_id,
    )
    session.add(integ)
    await session.commit()
    return _integration_dict(integ)


@router.put("/integrations/{integration_id}")
async def update_integration(
    integration_id: str,
    body: IntegrationUpdate,
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await require_enabled_user(session, user)
    integ = await session.get(CheckoutWebhookIntegrationModel, uuid.UUID(integration_id))
    if integ is None or integ.tenant_id != (user.tenant_id or "default"):
        raise HTTPException(404, "Integration not found")
    if body.label is not None:
        integ.label = body.label
    if body.field_map is not None:
        integ.field_map = body.field_map
    if body.enabled is not None:
        integ.enabled = body.enabled
    if body.hmac_secret is not None:
        integ.hmac_secret_enc = wh.encrypt_secret(body.hmac_secret) if body.hmac_secret else None
    await session.commit()
    return _integration_dict(integ)


@router.delete("/integrations/{integration_id}")
async def delete_integration(
    integration_id: str,
    _admin=Depends(require_admin()),
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    await require_enabled_user(session, user)
    integ = await session.get(CheckoutWebhookIntegrationModel, uuid.UUID(integration_id))
    if integ is None or integ.tenant_id != (user.tenant_id or "default"):
        raise HTTPException(404, "Integration not found")
    await session.delete(integ)
    await session.commit()
    return {"deleted": integration_id}
