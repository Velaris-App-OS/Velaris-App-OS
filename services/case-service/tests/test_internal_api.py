"""case-service's internal API (R5e step 7 — the commerce split).

The guard (secret, proxy headers, unconfigured), both client paths (local mode
inside case-service, HTTP for a separate process) answering the same, the
idempotent order-case, the other endpoint groups, the orphan sweep, and the
gateway refusing `/internal`.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
import pathlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from case_service.main import app
from velaris_shared.config import get_settings
from velaris_shared.db.models import CaseAuditLogModel, CaseInstanceModel, CheckoutOrderModel
from velaris_shared.internal import client as internal
from velaris_shared.internal.auth import HEADER

from tests.conftest import TestSessionFactory

SECRET = "t" * 40


@pytest.fixture
def secret():
    get_settings().internal_service_secret = SECRET
    yield SECRET


@pytest.fixture
async def raw():
    """A client with NO default headers — what reaches case-service directly."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
def http_path():
    """Route internal.call over HTTP into the app, as commerce-service will."""
    internal.set_transport(ASGITransport(app=app))
    internal.force_http = True
    yield
    internal.set_transport(None)
    internal.force_http = False


def _order_payload(key: str) -> dict:
    return {"kind": "order", "tenant": None, "request_key": key,
            "data": {"tracking_token": key, "source": "api", "total_cents": 100},
            "created_by": "hxcheckout:test", "rollout_subject": [None, "hxcheckout:test", key],
            "via": "checkout"}


# ── The guard ────────────────────────────────────────────────────────────────

async def test_unconfigured_install_refuses_every_internal_route(raw):
    get_settings().internal_service_secret = ""
    r = await raw.post("/internal/v1/order-cases", json=_order_payload("k0"),
                       headers={HEADER: "anything"})
    assert r.status_code == 503


async def test_missing_or_wrong_secret_is_refused(raw, secret):
    assert (await raw.post("/internal/v1/order-cases", json=_order_payload("k1"))).status_code == 403
    r = await raw.post("/internal/v1/order-cases", json=_order_payload("k1"),
                       headers={HEADER: "wrong-" + secret})
    assert r.status_code == 403


async def test_a_proxied_call_is_refused_even_with_the_secret(raw, secret):
    # The gateway / an ingress adds X-Forwarded-For. Internal calls never pass one.
    r = await raw.post("/internal/v1/order-cases", json=_order_payload("k2"),
                       headers={HEADER: secret, "X-Forwarded-For": "203.0.113.9"})
    assert r.status_code == 404


async def test_the_right_secret_is_admitted(raw, secret):
    r = await raw.post("/internal/v1/order-cases", json=_order_payload("k3"),
                       headers={HEADER: secret})
    assert r.status_code == 200, r.text
    assert r.json()["created"] is True


def test_the_gateway_never_forwards_internal():
    conf = (pathlib.Path(__file__).resolve().parents[3]
            / "deploy/docker-compose/gateway/nginx.conf.template").read_text()
    block = conf.index("location ^~ /internal { return 404; }")
    assert block < conf.index("        location / {"), "the block must come before the catch-all"


def test_internal_routes_are_not_in_the_public_schema():
    assert not any(p.startswith("/internal") for p in app.openapi().get("paths", {}))


# ── Order cases: idempotent, both paths ──────────────────────────────────────

async def test_order_case_is_idempotent_on_its_request_key(secret, http_path):
    first = await internal.call("order-cases", _order_payload("idem-1"))
    again = await internal.call("order-cases", _order_payload("idem-1"))
    assert first["created"] is True and again["created"] is False
    assert first["case_id"] == again["case_id"]


async def test_local_mode_uses_the_callers_session(secret):
    async with TestSessionFactory() as session:
        answer = await internal.call("order-cases", _order_payload("local-1"), session=session)
        # Same transaction as the caller: visible here, before any commit.
        assert await session.get(CaseInstanceModel, uuid.UUID(answer["case_id"])) is not None
        await session.rollback()
    async with TestSessionFactory() as session:
        assert await session.get(CaseInstanceModel, uuid.UUID(answer["case_id"])) is None


async def test_local_mode_validates_like_the_route(secret):
    async with TestSessionFactory() as session:
        with pytest.raises(internal.InternalCallError) as info:
            await internal.call("order-cases", {**_order_payload("bad"), "kind": "refund"},
                                session=session)
    assert info.value.status == 422


async def test_a_return_links_to_its_order_case(secret, http_path):
    order = await internal.call("order-cases", _order_payload("parent-1"))
    sub = await internal.call("order-cases", {
        "kind": "return", "tenant": None, "request_key": "return-1",
        "data": {"kind": "return", "reason": "broken"}, "created_by": "hxcheckout:customer",
        "rollout_subject": [None, "parent-1"], "via": "checkout_subcase",
        "parent_case_id": order["case_id"]})
    async with TestSessionFactory() as session:
        row = await session.get(CaseInstanceModel, uuid.UUID(sub["case_id"]))
        assert str(row.parent_case_id) == order["case_id"]


# ── The other groups ─────────────────────────────────────────────────────────

async def test_auto_advance_of_an_unknown_case_is_404(secret, http_path):
    with pytest.raises(internal.InternalCallError) as info:
        await internal.call("cases/auto-advance", {"case_id": str(uuid.uuid4())})
    assert info.value.status == 404


async def test_bad_rails_are_refused_with_the_rail_rules_message(secret, http_path):
    with pytest.raises(internal.InternalCallError) as info:
        await internal.call("engage/rails/validate", {"rails": {"not": "a list"}})
    assert info.value.status == 422 and "array" in info.value.detail


async def test_egress_policy_answers_with_the_declared_hosts_at_shadow(secret, http_path):
    answer = await internal.call("marketplace/egress-policy", {
        "tenant_id": "default", "package_id": "pkg.test", "fallback_subject": "app-1",
        "domains": ["api.example.com"]})
    # Every namespace ships at shadow: the legacy answer (the manifest's hosts) stands.
    assert answer["allowed_domains"] == ["api.example.com"]
    assert answer["agent_id"]


async def test_labelled_features_for_a_tenant_with_no_history(secret, http_path):
    answer = await internal.call("sentinel/labelled-features", {"tenant": "default"})
    assert answer == {"X": [], "y": []}


async def test_client_without_a_secret_does_not_call(http_path):
    get_settings().internal_service_secret = ""
    with pytest.raises(internal.InternalCallError) as info:
        await internal.call("order-cases", _order_payload("x"))
    assert info.value.status is None


# ── The orphan sweep ──────────────────────────────────────────────────────────

async def _age(case_id: str, minutes: int) -> None:
    from velaris_shared.db.models import CommerceCaseRequestModel

    then = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    async with TestSessionFactory() as session:
        (await session.get(CaseInstanceModel, uuid.UUID(case_id))).created_at = then
        for req in (await session.execute(select(CommerceCaseRequestModel).where(
                CommerceCaseRequestModel.case_id == uuid.UUID(case_id)))).scalars():
            req.created_at = then
        await session.commit()


async def test_the_sweep_cancels_an_order_case_with_no_order(secret, http_path):
    from case_service.internal_api import order_cases

    orphan = await internal.call("order-cases", _order_payload("orphan-1"))
    kept = await internal.call("order-cases", _order_payload("kept-1"))
    young = await internal.call("order-cases", _order_payload("young-1"))
    await _age(orphan["case_id"], 60)
    await _age(kept["case_id"], 60)
    async with TestSessionFactory() as session:
        session.add(CheckoutOrderModel(tenant_id="default", case_id=uuid.UUID(kept["case_id"]),
                                       tracking_token="kept-1", currency="EUR", total_cents=100))
        await session.commit()
    async with TestSessionFactory() as session:
        assert await order_cases.sweep_orphans(session) == 1
        await session.commit()
    async with TestSessionFactory() as session:
        status = {k: (await session.get(CaseInstanceModel, uuid.UUID(v["case_id"]))).status
                  for k, v in {"orphan": orphan, "kept": kept, "young": young}.items()}
        audit = (await session.execute(select(CaseAuditLogModel).where(
            CaseAuditLogModel.case_id == uuid.UUID(orphan["case_id"])))).scalars().all()
    assert status == {"orphan": "cancelled", "kept": "new", "young": "new"}
    assert any(a.actor_id == order_cases.SWEEP_ACTOR for a in audit), "the cancel is audited"


# ── Second wiring check (2026-10-19): limits and the client's TLS ────────────

def _starlette_app():
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def ok(request):
        await request.body()
        return JSONResponse({"ok": True})
    return Starlette(routes=[Route("/internal/v1/x", ok, methods=["POST"]),
                             Route("/api/v1/x", ok, methods=["POST"])])


def test_is_internal_caller_needs_path_secret_and_no_proxy(secret):
    from starlette.requests import HTTPConnection
    from velaris_shared.internal.auth import is_internal_caller

    def conn(path, headers):
        return HTTPConnection({"type": "http", "path": path, "query_string": b"",
                               "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]})
    assert is_internal_caller(conn("/internal/v1/x", {HEADER: secret}))
    assert not is_internal_caller(conn("/api/v1/x", {HEADER: secret}))
    assert not is_internal_caller(conn("/internal/v1/x", {HEADER: "wrong"}))
    assert not is_internal_caller(conn("/internal/v1/x", {HEADER: secret, "X-Forwarded-For": "1.2.3.4"}))


async def test_a_proven_internal_caller_is_not_rate_limited_and_nobody_else_escapes(secret):
    from velaris_shared.middleware.rate_limit import RateLimitMiddleware

    app_ = RateLimitMiddleware(_starlette_app(), requests_per_minute=3, burst=0)
    # A pod address, not loopback: the in-memory limiter already lets loopback
    # through, so on Kubernetes (and with the Valkey limiter) this is what counts.
    transport = ASGITransport(app=app_, client=("10.42.0.7", 40000))
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        proven = [(await c.post("/internal/v1/x", headers={HEADER: secret})).status_code for _ in range(10)]
        unproven = [(await c.post("/internal/v1/x")).status_code for _ in range(10)]
    assert proven == [200] * 10
    assert 429 in unproven, "an /internal flood without the secret is still limited"


async def test_only_a_proven_internal_caller_gets_the_larger_body_ceiling(secret):
    from velaris_shared.middleware.body_limit import BodyLimitMiddleware

    app_ = BodyLimitMiddleware(_starlette_app(), max_body_bytes=1000,
                               max_upload_bytes=1000, max_migrate_upload_bytes=100_000)
    body = b"x" * 5000
    async with AsyncClient(transport=ASGITransport(app=app_), base_url="http://t") as c:
        assert (await c.post("/internal/v1/x", content=body, headers={HEADER: secret})).status_code == 200
        assert (await c.post("/internal/v1/x", content=body)).status_code == 413
        assert (await c.post("/api/v1/x", content=body, headers={HEADER: secret})).status_code == 413


def test_the_client_tls_context_carries_the_client_certificate(tmp_path):
    """httpx 0.28 silently sent NO client certificate for verify=<path> + cert=(…)."""
    import ssl
    from datetime import datetime, timedelta, timezone as tz

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "commerce-service")])
    now = datetime.now(tz.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(1)
            .not_valid_before(now).not_valid_after(now + timedelta(days=1))
            .sign(key, hashes.SHA256()))
    (tmp_path / "c.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "c.key").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))
    ctx = internal._ssl_context((str(tmp_path / "c.crt"), str(tmp_path / "c.key")),
                                str(tmp_path / "c.crt"))
    assert isinstance(ctx, ssl.SSLContext)
    assert internal._ssl_context(None, True) is True        # TLS off: unchanged
