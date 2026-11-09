"""SAML 2.0 Service Provider endpoints.

The protocol lives in :mod:`user_service.identity.saml`; this module is the HTTP
and database half — provider lookup, replay storage, provisioning, and token
issuance. After the assertion has been verified the flow is deliberately the
same as `sso_callback`'s: device binding and `_make_tokens`, reused rather than
reimplemented, so a SAML session is a Velaris session in every respect.

**Why the ACS does not hand back tokens directly.** The IdP POSTs to us and we
have to get the browser to a page. Redirecting with tokens in the URL — query or
fragment — writes a refresh token into browser history, into the `Referer` of
whatever the landing page loads next, and into any proxy log on the way. Instead
the ACS mints a **single-use, 60-second handoff code**, redirects with that, and
the SPA exchanges it for the real tokens over a POST it makes itself. The code is
stored hashed in `auth_otp` (`purpose='saml_handoff'`), which already has the
single-use and expiry semantics this needs, so no new table.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import RedirectResponse

from velaris_shared import hxguard
from velaris_shared.auth.dependencies import get_current_user
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.core.error_codes import (
    AUTH_DISABLED_403,
    AUTH_SAML_ASSERTION_400,
    AUTH_SAML_PROVISION_403,
    AUTH_SSO_UNCONFIGURED_404,
    coded,
)
from velaris_shared.db.models import (
    AuthOtpModel,
    HelixUserModel,
    SamlAssertionReplayModel,
    SamlAuthnRequestModel,
    SsoProviderModel,
)
from velaris_shared.db.session import get_session
from user_service.identity import provisioning, saml
from velaris_shared.middleware.endpoint_rate_limit import rate_limit
from velaris_shared.tenancy.identity import resolve_tenant

log = logging.getLogger(__name__)

router = APIRouter(prefix="/auth/saml", tags=["auth-saml"])

#: The ACS is unauthenticated by construction — the signature is the credential —
#: and it does XML canonicalisation and an RSA verification per request against
#: every configured certificate. Without a bound, posting junk at it is a cheap
#: way to buy CPU.
#:
#: The number is per **client IP**, and with HTTP-POST binding the ACS request
#: comes from the user's own browser rather than from the IdP — so a large
#: customer behind one corporate NAT shares a bucket. 30/min would have throttled
#: legitimate morning sign-ins there; 240 is still far below what a flood needs
#: to be worth sending, and rejects on signature long before any provisioning.
_saml_rl = rate_limit(max_calls=240, window_seconds=60, name="saml")
_exchange_rl = rate_limit(max_calls=240, window_seconds=60, name="saml-exchange")

#: Long enough for the browser to follow one redirect and make one POST.
_HANDOFF_TTL_SECONDS = 60
_HANDOFF_PURPOSE = "saml_handoff"

#: Metadata import fetches a URL an administrator supplies. The SSRF guard is
#: what makes that safe; this is what stops it being used as a scanner.
_metadata_rl = rate_limit(max_calls=20, window_seconds=60, name="saml-metadata")


# ── metadata import ───────────────────────────────────────────────────────

class SamlMetadataRequest(BaseModel):
    """Either half is enough; the XML wins, because it needs no outbound call."""

    url: Optional[str] = Field(None, max_length=2048)
    xml: Optional[str] = Field(None, max_length=2 * 1024 * 1024)


@router.post("/metadata/parse", dependencies=[Depends(_metadata_rl)])
async def saml_metadata_parse(
    body: SamlMetadataRequest,
    _user: AuthenticatedUser = Depends(hxguard.guard("identity.sso.read")),
):
    """Read an IdP's federation metadata and return the fields it contains.

    **Fills the form; saves nothing.** The administrator still reviews and
    presses save, so importing metadata can never quietly repoint a live
    provider — and the endpoint needs only `identity.sso.read`, which is why a
    developer can use it without also holding the privilege to rewrite SSO.

    Every product names these fields differently (Okta's "Identity Provider
    Issuer" is Entra's "Microsoft Entra Identifier" is our `idp_entity_id`),
    which is what sends people to a search engine halfway through a setup. The
    metadata document is the one place all of them agree.
    """
    from user_service.identity import saml_metadata

    try:
        parsed = await saml_metadata.load_metadata(url=body.url, xml=body.xml)
    except saml_metadata.MetadataError as exc:
        # Safe to state plainly: the caller is configuring this and already
        # holds the privilege to read providers, and the document is public.
        raise HTTPException(400, str(exc)) from exc

    return {
        **parsed,
        "certs": saml_metadata.cert_health(parsed["idp_x509_certs"]),
        "source": "xml" if (body.xml and body.xml.strip()) else "url",
    }


# ── provider lookup ───────────────────────────────────────────────────────

async def _provider_for(
    session: AsyncSession, tenant: str,
) -> tuple[SsoProviderModel, saml.SamlConfig, str]:
    """The enabled SAML provider for a tenant, its parsed config, and the token.

    Every miss answers the same 404 — unknown tenant, no SAML row, disabled row,
    half-configured row. A caller probing this endpoint learns only that SAML is
    not usable here, not which of those is true, and in particular not which
    tenant slugs exist.
    """
    resolved = await resolve_tenant(session, tenant)
    tokens = {tenant}
    if resolved is not None:
        tokens = {str(resolved[0]), resolved[1], tenant}

    rows = (await session.execute(
        select(SsoProviderModel).where(SsoProviderModel.provider == "saml")
    )).scalars().all()

    row = next((r for r in rows if r.enabled and (r.tenant_id or "") in tokens), None)
    if row is None:
        # A single-tenant deployment configures SAML with no tenant at all; that
        # row serves whatever tenant token the URL carries.
        row = next((r for r in rows if r.enabled and not r.tenant_id), None)
    if row is None:
        raise coded(404, AUTH_SSO_UNCONFIGURED_404, "SAML is not configured here.")

    cfg = saml.SamlConfig.from_row(row.config)
    if cfg.missing():
        log.warning("saml: provider %s is missing %s", row.id, cfg.missing())
        raise coded(404, AUTH_SSO_UNCONFIGURED_404, "SAML is not configured here.")

    tenant_token = row.tenant_id or (resolved[1] if resolved else None) or tenant
    return row, cfg, tenant_token


def _acs_matches_route(cfg: saml.SamlConfig, tenant: str) -> bool:
    """Does the provider's configured ACS URL name the tenant in this URL?

    **This is a tenant-escape check, not tidiness.** `Destination` and
    `Recipient` are compared against `cfg.acs_url`, which is a fixed string — so
    they pass no matter which `{tenant}` segment the request actually arrived on.
    With a tenantless provider row (the single-tenant convenience), a federated
    user could take their own valid assertion out of the browser and POST it to
    `/auth/saml/<some-other-tenant>/acs`, and be provisioned into that tenant.
    The IdP posts to one fixed URL; nothing stops a person posting elsewhere.

    Tying the route to the configured ACS closes it without trusting the `Host`
    header, which an attacker upstream of us may control.
    """
    path = urlsplit(cfg.acs_url).path.rstrip("/")
    return path.endswith(f"/auth/saml/{tenant}/acs")


# ── SP key material ───────────────────────────────────────────────────────

class SpKeyRequest(BaseModel):
    #: Replacing a live pair breaks anything the IdP is already encrypting to
    #: the old key, so it cannot happen by re-pressing a button.
    regenerate: bool = False


@router.post("/{tenant}/sp-key")
async def saml_sp_key(
    tenant: str,
    body: SpKeyRequest,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("identity.sso.configure")),
):
    """Create (or replace) this provider's SP key pair. Returns the certificate.

    The private half is sealed with the tenant's HxVault DEK and never leaves
    the server; the certificate is public and is what the IdP administrator
    imports. Generating is `identity.sso.configure` rather than `.read` because
    it writes key material and, with `regenerate`, invalidates the old one.
    """
    from velaris_shared.db.security_events import log_security_event
    from user_service.identity import saml_keys

    row, _cfg, _t = await _provider_for(session, tenant)
    try:
        cert = await saml_keys.ensure_keypair(session, row, regenerate=body.regenerate)
    except saml_keys.SpKeyError as exc:
        raise HTTPException(400, str(exc)) from exc

    await log_security_event(
        session, event_type="saml_sp_key_generated",
        severity="high" if body.regenerate else "medium",
        user_id=user.user_id, resource_type="sso_provider", resource_id=str(row.id),
        action="generate", outcome="success",
        details={"regenerated": body.regenerate, "tenant": tenant},
    )
    await session.commit()
    await session.refresh(row)
    return {
        "certificate": cert,
        "summary": saml_keys.key_summary(row),
        "next_step": "Re-import this SP's metadata at your identity provider so "
                     "it trusts the new key.",
    }


# ── metadata ──────────────────────────────────────────────────────────────

@router.get("/{tenant}/metadata", dependencies=[Depends(_saml_rl)])
async def saml_metadata(tenant: str, session: AsyncSession = Depends(get_session)):
    """SP metadata XML, for the IdP administrator to upload.

    Public on purpose: it contains only what we would otherwise ask them to type
    (our entity ID and ACS URL), and an IdP admin configuring us has no Velaris
    account to authenticate with.
    """
    row, cfg, _tenant = await _provider_for(session, tenant)
    xml = saml.build_metadata(
        sp_entity_id=cfg.sp_entity_id,
        acs_url=cfg.acs_url,
        name_id_format=cfg.name_id_format,
        # Published only when a pair exists. Advertising a key we cannot use
        # invites the IdP to start encrypting to it, and every login then fails
        # in a way that reads like a signature fault.
        sp_cert=row.sp_cert,
        sign_authn_requests=cfg.sign_authn_requests,
        slo_url=_slo_url(cfg) if cfg.slo_enabled else None,
    )
    return Response(content=xml, media_type="application/samlmetadata+xml")


def _slo_url(cfg: saml.SamlConfig) -> str | None:
    """Our own SLO endpoint, derived from the configured ACS.

    Derived rather than separately configured for the same reason the login URL
    is: the ACS already carries the tenant segment and is already the one URL an
    administrator has to get exactly right.
    """
    acs = (cfg.acs_url or "").rstrip("/")
    return acs[: -len("/acs")] + "/slo" if acs.endswith("/acs") else None


# ── SP-initiated login ────────────────────────────────────────────────────

@router.get("/{tenant}/login", dependencies=[Depends(_saml_rl)])
async def saml_login(
    tenant: str,
    target: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    """Redirect the browser to the IdP with an AuthnRequest.

    `target` is checked against the provider's prefix allowlist **here**, before
    it is signed, so a value that would not be honoured on the way back is never
    minted in the first place.

    **The default comes from the provider, not from a constant.** It used to be
    a hardcoded `/studio`, which is not a route in the Studio SPA: the sign-in
    completed, the ACS redirected to `/studio?saml_code=…`, and the browser
    landed on a path the app does not render — so nothing redeemed the code and
    a working login looked like a broken one. The first allowed prefix is the
    landing page the administrator configured, and it is the same value the ACS
    falls back to when the IdP sends no RelayState at all.
    """
    row, cfg, _tenant = await _provider_for(session, tenant)
    target = target or saml.default_landing(cfg)
    if not saml.relay_state_allowed(target, cfg):
        raise coded(400, AUTH_SAML_ASSERTION_400, "That sign-in destination is not allowed.")

    private_key = None
    if cfg.sign_authn_requests:
        # A provider configured to sign must not quietly send an unsigned
        # request: the IdP would refuse it with an error the user cannot read,
        # and the administrator would be debugging the wrong end.
        from user_service.identity import saml_keys
        try:
            private_key = await saml_keys.load_private_key(session, row)
        except saml_keys.SpKeyError as exc:
            log.error("saml: provider %s is set to sign AuthnRequests but its key "
                      "is unusable: %s", row.id, exc)
            raise coded(404, AUTH_SSO_UNCONFIGURED_404, "SAML is not configured here.") from exc

    url, request_id = saml.build_authn_request(
        cfg=cfg, acs_url=cfg.acs_url, relay_state=saml.mint_relay_state(target),
        private_key_pem=private_key,
    )
    # Recorded so the ACS can *check* `InResponseTo` rather than merely read it.
    # Same lifetime as RelayState: a login round trip is a human walking through
    # an IdP, not a session.
    session.add(SamlAuthnRequestModel(
        request_id=request_id, provider_id=row.id,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=saml.RELAY_TTL_SECONDS),
    ))
    await session.commit()
    return RedirectResponse(url, status_code=302)


# ── assertion consumer service ────────────────────────────────────────────

@router.post("/{tenant}/acs", dependencies=[Depends(_saml_rl)])
async def saml_acs(
    tenant: str,
    request: Request,
    SAMLResponse: str = Form(...),
    RelayState: Optional[str] = Form(None),
    session: AsyncSession = Depends(get_session),
):
    """Consume a signed assertion and start a session.

    The order is the same as the design's, and each step is a refusal rather
    than a downgrade:

      1. resolve tenant + enabled provider          → uniform 404
      2. verify signature, audience, destination,
         recipient and timing (`identity.saml`)     → uniform 400
      3. claim the assertion ID                     → replay is a 400
      4. email domain allowlist / auto-provision    → 403
      5. provision through the P0 spine
      6. mint a single-use handoff code, redirect
    """
    from velaris_shared.db.security_events import log_security_event

    row, cfg, tenant_token = await _provider_for(session, tenant)
    if not _acs_matches_route(cfg, tenant):
        # Uniform with every other "SAML is not usable here" answer: a caller
        # probing tenant names learns nothing from the difference. The reason
        # goes to the log instead — without it a mistyped `acs_url` is an
        # unexplained 404 on the one endpoint an admin cannot test from a
        # browser, which is indistinguishable from the attack this refuses.
        log.warning(
            "saml: refusing ACS for tenant %r — provider %s has acs_url %r, "
            "which does not name that tenant",
            tenant, row.id, cfg.acs_url,
        )
        raise coded(404, AUTH_SSO_UNCONFIGURED_404, "SAML is not configured here.")
    client_ip = request.client.host if request.client else None

    async def _refuse(reason: str, detail: str = "") -> None:
        """One shape for every assertion-level refusal.

        The reason is recorded; it is not returned. Telling a forger whether the
        signature or the audience failed lets them fix one thing at a time.
        """
        await log_security_event(
            session, event_type="saml_assertion_refused", severity="high",
            action="saml_login", outcome="denied", ip_address=client_ip,
            resource_type="sso_provider", resource_id=str(row.id),
            details={"reason": reason, "detail": detail[:200], "tenant": tenant},
        )
        await session.commit()

    # Loaded unconditionally when the provider has one: whether the IdP encrypts
    # is the IdP's choice, made in their console, and can change without anyone
    # telling us. `verify_response` ignores it unless the document is encrypted.
    private_key = None
    if row.sp_key_enc:
        from user_service.identity import saml_keys
        try:
            private_key = await saml_keys.load_private_key(session, row)
        except saml_keys.SpKeyError as exc:
            log.warning("saml: provider %s has key material that will not "
                        "decrypt: %s", row.id, exc)

    try:
        assertion = saml.verify_response(
            SAMLResponse, cfg=cfg, acs_url=cfg.acs_url, private_key_pem=private_key)
    except saml.SamlError as exc:
        await _refuse(exc.reason, exc.detail)
        raise coded(400, AUTH_SAML_ASSERTION_400, "Sign-in could not be verified.") from exc

    # `InResponseTo`, when the assertion carries one. Absent means an
    # IdP-initiated sign-in, which answers no request and is legitimate — both
    # Okta and Entra offer a tile that starts there, and requiring the field
    # would break exactly the flow the landing-path setting exists to serve.
    # Present means it must name a request we actually sent, and consuming the
    # row is what stops the same assertion being posted into a second attempt.
    if assertion.in_response_to is not None:
        consumed = (await session.execute(
            delete(SamlAuthnRequestModel)
            .where(SamlAuthnRequestModel.request_id == assertion.in_response_to)
            .where(SamlAuthnRequestModel.provider_id == row.id)
            .where(SamlAuthnRequestModel.expires_at > datetime.now(timezone.utc))
        )).rowcount or 0
        if not consumed:
            await _refuse("unknown_in_response_to", assertion.in_response_to)
            raise coded(400, AUTH_SAML_ASSERTION_400, "Sign-in could not be verified.")

    # Claim the assertion id before anything is written on its behalf. The unique
    # insert is the check — a SELECT first would leave the window it exists to
    # close.
    claimed = await _claim_assertion(
        session, assertion_id=assertion.assertion_id, provider_id=row.id,
        not_on_or_after=assertion.not_on_or_after,
    )
    if not claimed:
        await _refuse("replayed", assertion.assertion_id)
        raise coded(400, AUTH_SAML_ASSERTION_400, "Sign-in could not be verified.")

    if not assertion.email:
        await _refuse("no_email", assertion.name_id)
        raise coded(400, AUTH_SAML_ASSERTION_400, "Sign-in could not be verified.")

    domain = assertion.email.rsplit("@", 1)[-1]
    existing = (await session.execute(
        select(HelixUserModel).where(HelixUserModel.external_id == assertion.name_id)
    )).scalars().first()

    # The allowlist gates *provisioning*, not login: an account an administrator
    # created here is not made unreachable by a later tightening of the list.
    if existing is None:
        if not cfg.auto_provision or domain not in cfg.email_domain_allowlist:
            await log_security_event(
                session, event_type="saml_provision_refused", severity="medium",
                action="saml_login", outcome="denied", ip_address=client_ip,
                details={"domain": domain, "auto_provision": cfg.auto_provision,
                         "allowlist_configured": bool(cfg.email_domain_allowlist)},
            )
            await session.commit()
            raise coded(
                403, AUTH_SAML_PROVISION_403,
                "This account is not provisioned in Velaris. Ask an administrator "
                "to create it, or to allow your email domain.",
            )

    result = await provisioning.upsert_principal(
        session, source="saml_jit",
        external_id=assertion.name_id,
        email=assertion.email,
        display_name=assertion.display_name,
        tenant_token=tenant_token,
        attributes={"manager": assertion.manager},
        group_names=assertion.groups,
        roles=cfg.default_roles,
        # NOT `active=True`. A successful authentication says the person exists
        # at the IdP; it does not say an administrator's decision to disable them
        # here should be undone. Passing True would make every SAML login a
        # silent reactivation, and the `is_active` check below would then never
        # fire because this call had just cleared it.
        active=None,
        actor=f"saml:{row.id}",
        # An assertion may not adopt an account it matches only by email. The
        # IdP's directory administrator chooses that address; without this, any
        # of them signs in as whichever local account already holds it and
        # inherits its roles — proven live against `localadmin`/`admin`. The
        # account is found by `external_id` or created; a taken address is a
        # refusal for an administrator to resolve deliberately.
        link_by_email=cfg.link_existing_by_email,
    )
    if not result.ok:
        await log_security_event(
            session, event_type="saml_provision_refused", severity="high",
            action="saml_login", outcome="denied", ip_address=client_ip,
            resource_type="sso_provider", resource_id=str(row.id),
            details={"reason": result.reason, "email": assertion.email,
                     "name_id": assertion.name_id, "tenant": tenant},
        )
        await session.commit()          # keep the provisioning-log row
        if result.reason in {"email_taken", "local_account_protected"}:
            # Reachable only with a valid assertion for this address, so naming
            # the cause is not enumeration — and without it the administrator's
            # only clue is a server log.
            raise coded(
                403, AUTH_SAML_PROVISION_403,
                "An account already uses this email address and is not linked to "
                "your identity provider. Ask an administrator to link it.",
            )
        raise coded(403, AUTH_SAML_PROVISION_403,
                    "This account could not be provisioned. Contact an administrator.")

    user = (await session.execute(
        select(HelixUserModel).where(HelixUserModel.username == result.username)
    )).scalar_one()
    if not user.is_active or user.deleted_at is not None:
        await session.commit()
        raise coded(403, AUTH_DISABLED_403, "Account is disabled. Contact an administrator.")

    user.last_login_at = datetime.now(timezone.utc)
    code = await _mint_handoff(session, user)
    await session.commit()

    target = saml.verify_relay_state(RelayState or "", cfg) if RelayState else None
    if target is None:
        # An absent or unacceptable RelayState is not a reason to fail a valid
        # login — it is a reason not to honour the destination. IdP-initiated
        # sign-in arrives with no RelayState at all and lands here legitimately.
        # This value is not checked by `relay_state_allowed` (nothing arrived
        # from outside), and it lands in a `Location` carrying a live handoff
        # code, so it comes from the helper that guarantees a same-origin path.
        target = saml.default_landing(cfg)

    sep = "&" if "?" in target else "?"
    return RedirectResponse(f"{target}{sep}{urlencode({'saml_code': code})}", status_code=303)


async def _claim_assertion(
    session: AsyncSession, *, assertion_id: str, provider_id: uuid.UUID,
    not_on_or_after: datetime | None,
) -> bool:
    """Insert the assertion id, returning False if it was already consumed.

    The insert runs in its own SAVEPOINT so a duplicate-key error does not
    poison the outer transaction — the caller still has security events to
    write on exactly that path.
    """
    from sqlalchemy.exc import IntegrityError

    expiry = not_on_or_after or (datetime.now(timezone.utc) + timedelta(minutes=10))
    try:
        async with session.begin_nested():
            session.add(SamlAssertionReplayModel(
                assertion_id=assertion_id,
                provider_id=provider_id,
                not_on_or_after=expiry,
            ))
    except IntegrityError:
        return False
    return True


# ── single logout ─────────────────────────────────────────────────────────

async def _revoke_sessions(session: AsyncSession, user: HelixUserModel, *, by: str) -> dict:
    """End every session this account holds. Does **not** disable the account.

    Logout and deprovisioning revoke the same things, but they are different
    events: a logout that also set `is_active = False` would let an IdP lock a
    user out of Velaris by ending their browser session.
    """
    from sqlalchemy import update

    from velaris_shared.db.models import AuthDeviceModel, RefreshTokenModel

    now = datetime.now(timezone.utc)
    uid = str(user.id)
    tokens = (await session.execute(
        update(RefreshTokenModel)
        .where(RefreshTokenModel.user_id == uid, RefreshTokenModel.revoked_at.is_(None))
        .values(revoked_at=now, revoked_by=by)
    )).rowcount or 0
    devices = (await session.execute(
        update(AuthDeviceModel)
        .where(AuthDeviceModel.user_id == uid, AuthDeviceModel.revoked_at.is_(None))
        .values(revoked_at=now, revoked_by=by)
    )).rowcount or 0
    return {"refresh_tokens": tokens, "devices": devices}


@router.get("/{tenant}/slo", dependencies=[Depends(_saml_rl)])
async def saml_slo(
    tenant: str,
    request: Request,
    SAMLRequest: Optional[str] = None,
    RelayState: Optional[str] = None,
    session: AsyncSession = Depends(get_session),
):
    """IdP-initiated single logout.

    **The signature is the whole gate.** This endpoint is public and the request
    names a user, so an unverified `LogoutRequest` would be a denial of service
    anyone could aim at anyone: send one per second and that person can never
    stay signed in. Nothing is revoked until the IdP's signature over the raw
    query string verifies against a configured certificate.

    Answers a redirect to the IdP either way. A refusal that looked different
    from a success would tell an unauthenticated caller whether a given NameID
    exists here.
    """
    from velaris_shared.db.security_events import log_security_event
    from user_service.identity import saml_keys

    row, cfg, _tenant = await _provider_for(session, tenant)
    if not cfg.slo_enabled or not SAMLRequest:
        raise coded(404, AUTH_SSO_UNCONFIGURED_404, "SAML is not configured here.")

    verified = saml.verify_redirect_signature(str(request.url.query or ""), "SAMLRequest", cfg)
    try:
        parsed = saml.parse_logout_request(SAMLRequest)
    except saml.SamlError as exc:
        await log_security_event(
            session, event_type="saml_logout_refused", severity="medium",
            action="saml_logout", outcome="denied",
            details={"reason": exc.reason, "tenant": tenant})
        await session.commit()
        raise coded(400, AUTH_SAML_ASSERTION_400, "Sign-out could not be verified.") from exc

    if not verified or parsed.issuer != cfg.idp_entity_id:
        await log_security_event(
            session, event_type="saml_logout_refused", severity="high",
            action="saml_logout", outcome="denied",
            ip_address=request.client.host if request.client else None,
            details={"reason": "signature_invalid" if not verified else "issuer_mismatch",
                     "tenant": tenant})
        await session.commit()
        raise coded(400, AUTH_SAML_ASSERTION_400, "Sign-out could not be verified.")

    user = (await session.execute(
        select(HelixUserModel).where(HelixUserModel.external_id == parsed.name_id)
    )).scalars().first() if parsed.name_id else None

    revoked = {}
    if user is not None:
        revoked = await _revoke_sessions(session, user, by=f"saml_slo:{row.id}")
    await log_security_event(
        session, event_type="saml_logout", severity="low",
        user_id=str(user.id) if user else None,
        resource_type="sso_provider", resource_id=str(row.id),
        action="saml_logout", outcome="success",
        details={"matched_account": user is not None, "tenant": tenant, **revoked})
    await session.commit()

    private_key = None
    if row.sp_key_enc:
        try:
            private_key = await saml_keys.load_private_key(session, row)
        except saml_keys.SpKeyError:
            private_key = None
    try:
        url = saml.build_logout_response(
            cfg=cfg, in_response_to=parsed.request_id,
            private_key_pem=private_key, relay_state=RelayState)
    except saml.SamlError:
        # No IdP SLO URL to answer on. The sessions are already gone, which is
        # the part that matters; there is simply nowhere to send the receipt.
        return Response(status_code=204)
    return RedirectResponse(url, status_code=302)


@router.post("/{tenant}/logout", dependencies=[Depends(_saml_rl)])
async def saml_logout_start(
    tenant: str,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """SP-initiated logout: revoke here, then hand the browser to the IdP.

    Local sessions are ended **before** the redirect, not after the IdP answers:
    if the user closes the tab mid-flight, or the IdP is down, the outcome must
    still be that they are signed out of Velaris.
    """
    from user_service.identity import saml_keys

    row, cfg, _tenant = await _provider_for(session, tenant)
    account = (await session.execute(
        select(HelixUserModel).where(HelixUserModel.id == uuid.UUID(str(user.user_id)))
    )).scalar_one_or_none()
    if account is None:
        raise coded(404, AUTH_SSO_UNCONFIGURED_404, "SAML is not configured here.")

    revoked = await _revoke_sessions(session, account, by=f"saml_logout:{row.id}")
    await session.commit()

    if not (cfg.slo_enabled and cfg.idp_slo_url and account.external_id):
        # Signed out here; the IdP session simply outlives it.
        return {"ok": True, "idp_logout": False, **revoked}

    private_key = None
    if row.sp_key_enc:
        try:
            private_key = await saml_keys.load_private_key(session, row)
        except saml_keys.SpKeyError:
            private_key = None
    try:
        url, _rid = saml.build_logout_request(
            cfg=cfg, name_id=account.external_id, private_key_pem=private_key)
    except saml.SamlError:
        return {"ok": True, "idp_logout": False, **revoked}
    return {"ok": True, "idp_logout": True, "redirect_url": url, **revoked}


# ── handoff ───────────────────────────────────────────────────────────────

async def _mint_handoff(session: AsyncSession, user: HelixUserModel) -> str:
    """A single-use code the SPA trades for tokens.

    Stored hashed for the same reason a refresh token is: a database read
    should not yield a working credential.
    """
    code = secrets.token_urlsafe(32)
    session.add(AuthOtpModel(
        user_id=user.id,
        otp_hash=hashlib.sha256(code.encode()).hexdigest(),
        purpose=_HANDOFF_PURPOSE,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=_HANDOFF_TTL_SECONDS),
    ))
    return code


class SamlExchangeRequest(BaseModel):
    code: str = Field(..., max_length=128)
    device_id: Optional[str] = None


@router.post("/exchange", dependencies=[Depends(_exchange_rl)])
async def saml_exchange(
    request: Request,
    body: SamlExchangeRequest,
    session: AsyncSession = Depends(get_session),
):
    """Trade a handoff code for a session. One use, then it is dead.

    The device is bound from *this* request, not from the IdP's POST: this is
    the browser that will hold the session, and the ACS request came from the
    IdP's redirect rather than from the user's own client.
    """
    from user_service.api.routers.auth_real import _make_tokens, _user_dict
    from velaris_shared.auth.devices import get_or_create_device

    digest = hashlib.sha256(body.code.encode()).hexdigest()
    now = datetime.now(timezone.utc)

    otp = (await session.execute(
        select(AuthOtpModel).where(
            AuthOtpModel.otp_hash == digest,
            AuthOtpModel.purpose == _HANDOFF_PURPOSE,
        )
    )).scalar_one_or_none()

    # SQLite hands back a naive datetime for a `DateTime(timezone=True)` column
    # where PostgreSQL returns an aware one, and comparing the two raises.
    expires_at = otp.expires_at if otp is not None else None
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    # One refusal for expired, already-used and never-existed alike: they are
    # the same event to anyone who should be told about it.
    if otp is None or otp.used_at is not None or expires_at <= now:
        raise coded(400, AUTH_SAML_ASSERTION_400, "Sign-in could not be completed.")

    otp.used_at = now
    user = await session.get(HelixUserModel, otp.user_id)
    if user is None or not user.is_active or user.deleted_at is not None:
        await session.commit()
        raise coded(403, AUTH_DISABLED_403, "Account is disabled. Contact an administrator.")

    await session.commit()

    device = await get_or_create_device(
        session, str(user.id),
        request.headers.get("user-agent", ""),
        request.client.host if request.client else None,
        claimed_device_id=body.device_id,
    )
    access_token, refresh_token, expires_in = await _make_tokens(
        user, session, device_id=device.id,
    )
    return {"access_token": access_token, "refresh_token": refresh_token,
            "expires_in": expires_in, "token_type": "bearer",
            "user": _user_dict(user), "device_id": str(device.id)}


# ── admin: dry-run an assertion ───────────────────────────────────────────

class SamlTestRequest(BaseModel):
    saml_response: str = Field(..., description="Base64 SAMLResponse, as the IdP posts it")


async def _may_administer_tenant(session: AsyncSession, user: AuthenticatedUser, tenant: str) -> bool:
    """The platform operator, or an admin of exactly the tenant in the URL.

    Not `tenant_for_write`: that reads the literal "default" as "no opinion,
    mine" — right for a create body, wrong for a URL naming whose config to
    read. The first fix used it and a `dev` admin still tested `default`'s IdP
    (found live, 2026-09-30)."""
    from velaris_shared.tenancy.identity import (
        DEFAULT_TENANT, is_platform_operator, resolve_tenant)
    if await is_platform_operator(session, user):
        return True
    mine = await resolve_tenant(session, getattr(user, "tenant_id", None) or DEFAULT_TENANT)
    theirs = await resolve_tenant(session, tenant)
    return mine is not None and theirs is not None and mine[0] == theirs[0]


@router.post("/{tenant}/test")
async def saml_test(
    tenant: str,
    body: SamlTestRequest,
    session: AsyncSession = Depends(get_session),
    _user: AuthenticatedUser = Depends(hxguard.guard("identity.sso.configure")),
):
    """Run a pasted assertion through the real verifier and show the mapping.

    Issues no token, claims no assertion id and provisions nobody — an admin can
    paste the same capture repeatedly while fixing an attribute map, and doing so
    must not burn the assertion for the live path or create accounts.

    This is the one place the refusal reason is disclosed, because the caller
    already holds `identity.sso.configure`; without it, diagnosing a mapping
    means reading server logs.
    """
    # The tenant came from the URL and was trusted until 2026-09-30: any
    # tenant's admin could run assertions against another tenant's IdP config
    # and learn which of its accounts exist. Another tenant answers as absent.
    if not await _may_administer_tenant(session, _user, tenant):
        raise coded(404, AUTH_SSO_UNCONFIGURED_404, "SAML is not configured here.")
    _row, cfg, tenant_token = await _provider_for(session, tenant)
    try:
        assertion = saml.verify_response(body.saml_response, cfg=cfg, acs_url=cfg.acs_url)
    except saml.SamlError as exc:
        return {"ok": False, "reason": exc.reason, "detail": exc.detail}

    mapped, unmapped = [], list(assertion.groups)
    if assertion.groups:
        resolved = await resolve_tenant(session, tenant_token)
        if resolved is not None:
            found, unmapped = await provisioning._mapped_groups(
                session, tenant_id=resolved[0], source="saml_jit",
                group_names=assertion.groups,
            )
            mapped = [{"group": k, "access_group_id": str(v)} for k, v in found.items()]

    # What the live path would actually decide, in the same order it decides it.
    # A tester that answers "would provision" where the ACS answers 403 sends the
    # administrator looking at their attribute map for a fault that is not there.
    linked = await provisioning.find_principal(
        session, external_id=assertion.name_id,
    ) if assertion.name_id else None
    held_by_other = None
    if linked is None and assertion.email:
        held_by_other = await provisioning.find_principal(session, email=assertion.email)

    if linked is not None:
        outcome, would_provision = "would_sign_in_as_existing", True
    elif not assertion.email:
        outcome, would_provision = "no_email", False
    elif held_by_other is not None and not cfg.link_existing_by_email:
        # The address is on an account this identity is not linked to. The ACS
        # refuses rather than adopting it — an administrator links it on purpose.
        outcome, would_provision = "email_taken", False
    elif held_by_other is not None and held_by_other.password_hash:
        outcome, would_provision = "local_account_protected", False
    elif not cfg.auto_provision:
        outcome, would_provision = "auto_provision_off", False
    elif assertion.email.rsplit("@", 1)[-1] not in cfg.email_domain_allowlist:
        outcome, would_provision = "domain_not_allowed", False
    else:
        outcome, would_provision = ("would_link_existing" if held_by_other is not None
                                    else "would_create"), True

    return {
        "ok": True,
        "name_id": assertion.name_id,
        "email": assertion.email,
        "display_name": assertion.display_name,
        "manager": assertion.manager,
        "groups": assertion.groups,
        "mapped_groups": mapped,
        "unmapped_groups": unmapped,
        "attributes": assertion.attributes,
        "outcome": outcome,
        "linked_username": linked.username if linked is not None else None,
        "email_held_by": held_by_other.username if held_by_other is not None else None,
        "would_provision": would_provision,
    }
