"""Routes with NO authorization at all — the population no phase could see.

Phases 0–4 each migrated the gates that **existed**. A `require_role(` grep, an
AST pass for `user.is_admin`, the `require_admin()` alias sweep — every one of
them searched for a gate. **None of them could find a route that had no gate**,
because absence has no spelling.

A sweep of the BUILT app on 2026-08-15 (route table, not source, so router-level
dependencies count) found **58 such routes, 29 of them writes**. Proven against
the dev database, anonymously and in-process:

  * `GET /api/v1/email/messages` returned **255KB of email bodies** —
    `body_text`, `body_html`, `from_address`, `cc_addresses`.
  * `GET /api/v1/email/accounts` disclosed SMTP/IMAP hosts and usernames.
  * `POST/PATCH/DELETE /api/v1/email/templates` — anonymous **writes**.
  * `GET /api/v1/user-directory/{user_id}` returned a named user's email, roles
    and access groups, while the sibling LIST endpoint was gated.
  * `GET /api/v1/analytics/dashboard` returned case volumes and SLA figures.
  * `GET /api/v1/documents/{id}` and `/versions` returned real rows.

**All 58 are now closed** (documents last, by Option A on 2026-08-15).

This test is the guard. It asserts the *known* set never grows: a new route
either carries an auth dependency, or is added to `PUBLIC_BY_DESIGN` /
`ACCEPTED` / `IN_BODY_AUTHZ` below by someone who has thought about it — and
`IN_BODY_AUTHZ` additionally demands a test that sends the anonymous request,
because in-body authorization is invisible to every static check.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from fastapi.routing import APIRoute

from case_service.main import app


def _platform_routes() -> list[APIRoute]:
    """Every APIRoute the PLATFORM serves, not just this service's.

    13c-e moved 80 routes into analytics-service, hxgraph-service and
    hxmigrate-service; R5e step 7 moved 146 into commerce-service, step 8 52 into portal-service, step 10 75 into user-service. A sweep that kept reading `case_service.main` alone
    would have gone on passing while the routes it was built to watch left its
    field of view — the same shape as the documents hole this file exists for.
    The gateway serves all seven apps behind one prefix, so the sweep covers all
    seven. Importing them is deliberately not wrapped in a try: a service that
    cannot be imported must break this test, not shrink it.
    """
    from analytics_service.main import app as analytics_app
    from commerce_service.main import app as commerce_app
    from hxgraph_service.main import app as hxgraph_app
    from hxmigrate_service.main import app as hxmigrate_app
    from portal_service.main import app as portal_app      # R5e step 8
    from user_service.main import app as user_app          # R5e step 10

    routes: list[APIRoute] = []
    for a in (app, analytics_app, hxgraph_app, hxmigrate_app, commerce_app, portal_app,
              user_app):
        routes.extend(r for r in a.routes if isinstance(r, APIRoute))
    return routes

#: Prefixes that serve anonymous callers by design.
PUBLIC_BY_DESIGN = (
    "/api/v1/portal/",              # customer portal — its own session/token model
    "/api/v1/storefront/public/",   # public storefront
    "/health", "/ready", "/metrics", "/ping",
    "/api/v1/auth/login", "/api/v1/auth/real/",   # sign-in itself
    "/api/v1/status/public",
    "/api/v1/releases/flags",
    "/api/v1/mcp",
    # 2026-10-29: Studio's Content-Security-Policy reports — browsers send them
    # with no credentials; capped body and rate, logs no query strings.
    "/api/v1/csp-reports",
)

#: Dependency names that establish *some* identity.
AUTH_DEPS = {
    "get_current_user", "_dep", "guard", "get_broker_grant",
    "require_portal_session", "get_current_user_optional",
    "_portal_session", "get_customer_session", "_require_superadmin",
    # SCIM callers are machines holding a `scim_tokens` bearer credential, not
    # people with a JWT. The dependency resolves the token, refuses uniformly on
    # every failure, and carries the tenant — so a SCIM route is gated, just not
    # by `get_current_user`.
    "scim_principal",
    # R5e step 7: case-service's /internal/v1 routes — sibling services only.
    # Refuses without the install's internal service secret, refuses anything
    # that came through a proxy, and the gateway never forwards /internal
    # (tests/test_internal_api.py proves all three).
    "require_internal_caller",
    # 2026-11-04: user-service's /internal/v1/demo routes — the website's
    # registration service only. Refuses without VELARIS_DEMO_PROVISION_SECRET
    # (valid on these routes and nothing else), refuses anything that came
    # through a proxy; the gateway never forwards /internal
    # (user-service/tests/test_demo_tenants.py proves all three).
    "require_demo_caller",
}

#: Ungated on purpose, each with the reason it is not a hole. Shrinking this set
#: is progress; growing it needs a justification in this comment block.
#:
#: **These were VERIFIED by reading each handler on 2026-08-15, not assumed** —
#: the first version of this list took "it's a webhook, it must check a
#: signature" on trust, and that assumption was wrong in four places (below).
#:
#: * **Signature-verified webhooks** — stripe, onfido, docusign, livekit,
#:   connector receive, devconn receive, checkout, deploy callback. All reject a
#:   MISSING signature, not just a bad one. Four of them did not until
#:   2026-08-15: stripe/onfido/docusign read `elif not sig_header: verified =
#:   True`, and the deploy callback only verified `if ... and x_deploy_signature`.
#:   The Stripe one was proven exploitable — an anonymous unsigned POST moved a
#:   real payment request to `succeeded` and posted to the ledger.
#: * **Residual, by documented design:** a connector or environment with NO
#:   webhook_secret configured is unverified; the only gate is the unguessable
#:   UUID in the path. `devconn.py` says so in its docstring and tells the
#:   operator to configure a secret. Changing that would break existing installs
#:   and is a product decision, not a bug fix.
#: * **`deploy/import`** requires `Authorization: Bearer <import_api_key>` or
#:   `X-Deploy-Key`, matched against `environment_registry` — 401 without.
#: * **checkout order flows** are reached by a customer holding an order id from
#:   the public storefront; gating them would break anonymous checkout.
#: * **meet guest** endpoints exist for callers with no account: single-use
#:   invite token, IP rate-limited.
#: * **platform update peer endpoints** carry a shared-secret header verified in
#:   the handler (they answer 401 anonymously — proven).
#: * **documents** is NOT here — it is authorized in the handler body; see
#:   `IN_BODY_AUTHZ` below.
ACCEPTED = {
    "/api/v1/payments/webhooks/stripe",
    "/api/v1/esign/webhooks/docusign",
    "/api/v1/identity/webhooks/onfido",
    "/api/v1/meet/webhook/livekit",
    "/api/v1/webhooks/{connector_id}/receive",
    "/api/v1/devconn/webhooks/receive/{connector_id}",
    "/api/v1/intake/webhook/{case_type_id}",
    "/api/v1/checkout/webhook/{integration_id}",
    "/api/v1/deploy/runs/{run_id}/callback",
    "/api/v1/deploy/import",
    "/api/v1/checkout/orders",
    "/api/v1/checkout/orders/{order_id}",
    "/api/v1/checkout/orders/{order_id}/cancel",
    "/api/v1/checkout/orders/{order_id}/complaints",
    "/api/v1/checkout/orders/{order_id}/returns",
    "/api/v1/meet/guest/preview",
    "/api/v1/meet/guest/token",
    "/api/v1/platform/update/peer-status",
    "/api/v1/platform/update/trigger",
    # SAML SP. Anonymous by construction, and each for a different reason:
    # `metadata` publishes only what we would otherwise ask an IdP admin to
    # type, and they have no Velaris account to authenticate with; `login` only
    # builds a redirect; `acs` IS the login, and its credential is the IdP's
    # signature over the assertion — a token dependency there would 401 the IdP,
    # which is the caller the route exists for; `exchange` presents a single-use
    # handoff code minted seconds earlier by `acs`. All four are rate-limited,
    # and all four are pinned by anonymous-request tests in
    # `tests/phase_identity/test_saml.py`.
    "/api/v1/auth/saml/{tenant}/metadata",
    "/api/v1/auth/saml/{tenant}/login",
    "/api/v1/auth/saml/{tenant}/acs",
    "/api/v1/auth/saml/exchange",
    # Single logout, same shape as `acs` and the same reasoning: the IdP sends
    # it and has no Velaris token to present, so its **signature over the raw
    # query string is the credential**. Nothing is revoked until that verifies
    # against a configured certificate — without the check this route would be a
    # denial of service anyone could aim at any user by name, once a second.
    # A logout for an unknown subject answers exactly like one for a known
    # subject, so it is not an oracle for which identities exist here. Pinned by
    # anonymous-request tests in `tests/phase_identity/test_saml_sp_key.py`.
    "/api/v1/auth/saml/{tenant}/slo",
    # The vendored D3 the HxGraph visualizer loads (2026-09-10). It is an
    # unmodified copy of a public library, byte-for-byte what a CDN would have
    # served until today, and it carries no data of any kind — the graph itself
    # is behind `/graph/visualize`, which is gated.
    #
    # It also CANNOT be gated: the reader is an iframe's `<script src>`, which
    # sends no Authorization header, and Studio authenticates with a bearer
    # token rather than a cookie. Serving it ourselves is the point — the
    # screen was dead on any install without internet, and a third party's
    # server was a runtime dependency of a sovereignty-first product.
    "/api/v1/graph/vendor/d3.js",
    # F-09 agent-to-agent handoff, the COUNTERPARTY's four routes. Not
    # unauthenticated — this sweep looks for `get_current_user`, and these wear
    # `get_agent_caller`, which is a real credential dependency: no bearer token
    # is a 401, and the token is resolved against the agent registry and, for a
    # work order, against its own case. The asymmetry is deliberate and
    # documented on that dependency — a surface built for an unattended actor
    # must not quietly double as a human one, or its rate limits, its logging
    # and its policy namespace all describe traffic they were not written for.
    # `get_current_user` would refuse the only caller these routes exist for: an
    # external party holds no role in this platform's vocabulary. All four are
    # additionally rate-limited (`_agent_rl`).
    "/api/v1/handoff/protocol/{order_id}/accept",
    "/api/v1/handoff/protocol/{order_id}/case",
    "/api/v1/handoff/protocol/{order_id}/propose",
    "/api/v1/handoff/protocol/{order_id}/return",
    # Omnichannel inbound. The caller is Twilio or Meta and there is no token to
    # give them: the provider signature over the payload IS the credential, and
    # a missing header is refused exactly like a wrong one. Every failure —
    # unknown provider, unparseable id, unknown endpoint, inactive endpoint, bad
    # signature — answers the same 404, so the endpoint id cannot be probed.
    # Pinned by `tests/phase_channels/test_inbound.py`.
    "/api/v1/channels/{provider}/inbound/{endpoint_id}",
    # RPA bot result/progress. The caller is a container on the internal apps
    # network holding a PER-JOB, single-use token over its own row and nothing
    # else — it has no session and no user, and `get_current_user` would refuse
    # the only caller these routes exist for. Giving the container broker write
    # scopes instead would have put mutation capability in every container on
    # that network, permanently. Pinned by `tests/phase_bots/test_result_path.py`.
    "/api/v1/bots/jobs/{job_id}/result",
    "/api/v1/bots/jobs/{job_id}/progress",
}

#: Authorized INSIDE the handler, not by a dependency — so this scan cannot see
#: it, and neither could any of the detectors the authz phases relied on. That is
#: precisely why each one is pinned by an explicit anonymous-request test in
#: `test_document_grants.py::TestRoutesRefuseAnonymousCallers`.
#:
#: They cannot use a dependency: the customer portal hands ANONYMOUS customers
#: download links pointing here, so `Depends(get_current_user)` would 401 the
#: very caller the route exists for. Instead each admits exactly two callers —
#: an authenticated one who passes the case-level ReBAC check, or a signed,
#: expiring, single-document grant minted by the portal (Option A, 2026-08-15).
#:
#: **Adding a route here requires adding its anonymous-refusal test.** A path in
#: this set with no such test is indistinguishable from the hole it replaced.
IN_BODY_AUTHZ = {
    "/api/v1/documents/{document_id}",
    "/api/v1/documents/{document_id}/download",
    "/api/v1/documents/{document_id}/preview",
    "/api/v1/documents/{document_id}/versions",
}


def _dependency_names(route: APIRoute) -> set[str]:
    names: set[str] = set()

    def walk(dep, depth: int = 0) -> None:
        if depth > 5:
            return
        for sub in dep.dependencies:
            if sub.call is not None:
                names.add(getattr(sub.call, "__name__", ""))
            walk(sub, depth + 1)

    walk(route.dependant)
    return names


def _ungated() -> set[str]:
    out: set[str] = set()
    for route in _platform_routes():
        if any(route.path.startswith(p) for p in PUBLIC_BY_DESIGN):
            continue
        if _dependency_names(route) & AUTH_DEPS:
            continue
        out.add(route.path)
    return out


def test_no_new_route_is_ungated():
    known = ACCEPTED | IN_BODY_AUTHZ
    surprises = sorted(_ungated() - known)
    assert not surprises, (
        f"{len(surprises)} route(s) have no authentication dependency and are not "
        f"recorded as deliberate. Add an hxguard action, or justify them in this "
        f"file's ACCEPTED block: {surprises}")


def test_the_scan_sees_the_route_table():
    """Non-vacuity: a scanner that found no routes would pass everything."""
    total = len(_platform_routes())
    assert total > 500, f"only {total} API routes seen — the scan is not working"


def test_accepted_entries_still_exist():
    """An ACCEPTED path that no longer exists is stale permission — it should be
    deleted, so the list keeps meaning what it says."""
    paths = {r.path for r in _platform_routes()}
    assert not (ACCEPTED - paths), f"stale ACCEPTED entries: {sorted(ACCEPTED - paths)}"


def test_in_body_authz_routes_each_have_an_anonymous_refusal_test():
    """The set above is trusted only because something proves it.

    A route authorized in its body looks identical, to every static check, to a
    route authorized nowhere — that similarity is what let the documents hole
    survive four migration phases. So the trust is transferred to a test that
    actually sends the anonymous request, and this asserts that test exists.
    """
    from pathlib import Path

    proof = (Path(__file__).parent / "test_document_grants.py").read_text()
    assert "TestRoutesRefuseAnonymousCallers" in proof
    for path in IN_BODY_AUTHZ:
        leaf = path.rsplit("/", 1)[-1]
        token = "documents/{doc_id}" if leaf == "{document_id}" else leaf
        assert token in proof, (
            f"{path} claims in-body authorization with nothing proving it — "
            f"add an anonymous-request test to test_document_grants.py")
