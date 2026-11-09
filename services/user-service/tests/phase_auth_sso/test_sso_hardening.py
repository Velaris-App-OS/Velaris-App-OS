"""SSO callback hardening — the four holes that were open in shipped code.

1. `state` was minted and returned, then never verified: any site could post a
   provider code to /sso/callback and land a victim's browser in the attacker's
   account (login CSRF).
2. Auto-provisioning was unconditional — anyone on the internet who completed
   the Google flow got a `viewer` account.
3. The email that decides *which* account an SSO identity binds to was taken on
   faith, including GitHub's unverified free-text profile email.
4. `is_active` was never checked, so disabling a user left their SSO button
   working.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import time as _time
import types
import uuid

import pytest
from sqlalchemy import select

from user_service.api.routers import auth_real
from velaris_shared.db.models import HelixUserModel, SsoProviderModel


# ── Provider doubles ──────────────────────────────────────────────

class _Resp:
    def __init__(self, payload, ok: bool = True):
        self._payload, self._ok = payload, ok

    @property
    def is_success(self) -> bool:
        return self._ok

    def json(self):
        return self._payload


class _FakeHttp:
    """Stands in for httpx.AsyncClient inside sso_callback.

    Records every outbound call so a test can assert that a forged callback
    never reaches the provider at all.
    """

    calls: list[str] = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kw):
        _FakeHttp.calls.append(url)
        return _Resp({"access_token": "provider-token"})

    async def get(self, url, **kw):
        _FakeHttp.calls.append(url)
        if url.endswith("/user/emails"):
            return _Resp(self.emails)
        return _Resp(self.userinfo)


@pytest.fixture
def no_dns(monkeypatch):
    """Neutralise only the DNS leg of the SSRF guard.

    `example.okta.com` happens to resolve on a connected box, so tests that are
    about the *endpoint contract* would silently depend on the network and fail
    in an offline CI. The tests that are genuinely about SSRF use literal
    addresses and `localhost`, which resolve without a network, and deliberately
    do NOT take this fixture.
    """
    from velaris_shared.hxbridge import security

    async def _allow(url: str) -> None:
        return None

    monkeypatch.setattr(security, "validate_outbound_url", _allow)


@pytest.fixture
def fake_provider(monkeypatch):
    """Patch httpx for the duration of one test and shape its answers."""
    import httpx

    _FakeHttp.calls = []

    def _configure(userinfo: dict, emails: list | None = None):
        _FakeHttp.userinfo = userinfo
        _FakeHttp.emails = emails or []
        monkeypatch.setattr(httpx, "AsyncClient", _FakeHttp)
        return _FakeHttp

    return _configure


async def _provider_row(session, provider="google", config=None) -> SsoProviderModel:
    row = SsoProviderModel(
        provider=provider, client_id=f"{provider}-client-id",
        enabled=True, config=config or {},
    )
    session.add(row)
    await session.commit()
    return row


_REDIRECT = "http://test/auth/sso/callback"


def _state(row, redirect=_REDIRECT):
    """State is bound to the provider *row*, not the provider name."""
    return auth_real._mint_sso_state(row.id, redirect)


def _callback_body(row, **over):
    body = {
        "provider": row.provider,
        "code": "provider-code",
        "redirect_uri": _REDIRECT,
        "state": _state(row),
    }
    body.update(over)
    return body


# ── 1. state ──────────────────────────────────────────────────────

class TestState:
    _CB = "http://x/cb"

    def test_round_trips_and_returns_the_provider_row(self):
        pid = str(uuid.uuid4())
        s = auth_real._mint_sso_state(pid, self._CB)
        assert auth_real._verify_sso_state(s, self._CB) == pid

    def test_rejects_a_tampered_signature(self):
        s = auth_real._mint_sso_state(str(uuid.uuid4()), self._CB)
        flipped = s[:-1] + ("0" if s[-1] != "0" else "1")
        assert auth_real._verify_sso_state(flipped, self._CB) is None

    def test_rejects_an_unsigned_value(self):
        """The old implementation returned exactly this shape."""
        import secrets
        assert auth_real._verify_sso_state(secrets.token_urlsafe(16), self._CB) is None

    def test_is_bound_to_the_redirect_uri(self):
        s = auth_real._mint_sso_state(str(uuid.uuid4()), self._CB)
        assert auth_real._verify_sso_state(s, "http://evil/cb") is None

    def test_expires(self):
        body = ".".join(("nonce", str(uuid.uuid4()),
                         auth_real.hashlib.sha256(self._CB.encode()).hexdigest()[:16],
                         str(int(_time.time()) - 1)))
        expired = f"{body}.{auth_real._sso_state_sign(body)}"
        assert auth_real._verify_sso_state(expired, self._CB) is None

    def test_refuses_to_sign_without_an_auth_secret(self, monkeypatch):
        """An empty HMAC key signs deterministically, so anyone could forge a
        state. Failing closed is the only safe reading."""
        from fastapi import HTTPException
        from velaris_shared import config as cfg

        real = cfg.get_settings()

        class _NoSecret:
            def __getattr__(self, name):
                return getattr(real, name)
            auth_secret = ""

        monkeypatch.setattr(auth_real, "get_settings", lambda: _NoSecret())
        with pytest.raises(HTTPException) as exc:
            auth_real._mint_sso_state(str(uuid.uuid4()), self._CB)
        assert exc.value.status_code == 503

    @pytest.mark.asyncio
    async def test_a_state_for_one_provider_row_cannot_complete_another(
        self, anon_client, session, fake_provider,
    ):
        """The state names a row, so a github state cannot finish as google."""
        google = await _provider_row(session, provider="google")
        github = await _provider_row(session, provider="github")
        fake_provider({"email": "x@company.com", "email_verified": True, "sub": "s-x"})

        r = await anon_client.post("/api/v1/auth/real/sso/callback", json={
            "provider": "google", "code": "c", "redirect_uri": _REDIRECT,
            "state": _state(github),
        })
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "VEL-AUTH-SSO-UNCONFIGURED-404"
        assert _FakeHttp.calls == []
        assert google is not None

    @pytest.mark.asyncio
    async def test_a_state_stops_working_once_the_provider_is_disabled(
        self, anon_client, session, fake_provider,
    ):
        row = await _provider_row(session, config={"allowed_domains": ["company.com"]})
        state = _state(row)
        row.enabled = False
        await session.commit()
        fake_provider({"email": "x@company.com", "email_verified": True, "sub": "s-y"})

        r = await anon_client.post("/api/v1/auth/real/sso/callback",
                                   json=_callback_body(row, state=state))
        assert r.status_code == 404
        assert _FakeHttp.calls == []

    @pytest.mark.asyncio
    async def test_auth_url_issues_a_verifiable_state(self, anon_client, session):
        row = await _provider_row(session)
        r = await anon_client.get(
            "/api/v1/auth/real/sso/google/auth-url",
            params={"redirect_uri": "http://test/auth/sso/callback"},
        )
        assert r.status_code == 200
        state = r.json()["state"]
        assert auth_real._verify_sso_state(state, "http://test/auth/sso/callback") == str(row.id)

    @pytest.mark.asyncio
    async def test_auth_url_encodes_its_parameters(self, anon_client, session):
        """A raw `&` in redirect_uri used to inject extra authorize params."""
        row = await _provider_row(session)
        r = await anon_client.get(
            "/api/v1/auth/real/sso/google/auth-url",
            params={"redirect_uri": "http://test/cb?a=1&scope=evil"},
        )
        url = r.json()["auth_url"]
        assert "&scope=evil" not in url
        assert "scope=openid" in url

    @pytest.mark.asyncio
    async def test_callback_requires_state(self, anon_client, session):
        row = await _provider_row(session)
        body = _callback_body(row)
        body.pop("state")
        r = await anon_client.post("/api/v1/auth/real/sso/callback", json=body)
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_forged_state_is_refused_before_calling_the_provider(
        self, anon_client, session, fake_provider,
    ):
        row = await _provider_row(session)
        fake_provider({"email": "x@y.com", "sub": "1"})
        r = await anon_client.post(
            "/api/v1/auth/real/sso/callback",
            json=_callback_body(row, state="attacker-chosen-value"),
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "VEL-AUTH-SSO-STATE-400"
        assert _FakeHttp.calls == [], "a forged callback must not reach the provider"


# ── 2. auto-provisioning ──────────────────────────────────────────

class TestProvisioning:
    @pytest.mark.asyncio
    async def test_refused_when_no_allowlist_is_configured(
        self, anon_client, session, fake_provider,
    ):
        row = await _provider_row(session, config={})
        fake_provider({"email": "stranger@internet.com", "email_verified": True, "sub": "s-1"})

        r = await anon_client.post("/api/v1/auth/real/sso/callback", json=_callback_body(row))
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "VEL-AUTH-SSO-PROVISION-403"

        created = (await session.execute(
            select(HelixUserModel).where(HelixUserModel.email == "stranger@internet.com")
        )).scalar_one_or_none()
        assert created is None

    @pytest.mark.asyncio
    async def test_refused_for_a_domain_outside_the_allowlist(
        self, anon_client, session, fake_provider,
    ):
        row = await _provider_row(session, config={"allowed_domains": ["company.com"]})
        fake_provider({"email": "stranger@gmail.com", "email_verified": True, "sub": "s-2"})
        r = await anon_client.post("/api/v1/auth/real/sso/callback", json=_callback_body(row))
        assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_allowlisted_domain_is_provisioned(
        self, anon_client, session, fake_provider,
    ):
        row = await _provider_row(session, config={"allowed_domains": ["Company.com"]})
        fake_provider({"email": "New.Person@company.com", "email_verified": True,
                       "sub": "s-3", "name": "New Person"})

        r = await anon_client.post("/api/v1/auth/real/sso/callback", json=_callback_body(row))
        assert r.status_code == 200, r.text
        assert r.json()["access_token"]

        u = (await session.execute(
            select(HelixUserModel).where(HelixUserModel.sso_subject == "s-3")
        )).scalar_one()
        assert u.email == "new.person@company.com"   # normalised
        assert u.roles == ["viewer"]

    @pytest.mark.asyncio
    async def test_default_roles_come_from_the_provider_config(
        self, anon_client, session, fake_provider,
    ):
        row = await _provider_row(session, config={"allowed_domains": ["company.com"],
                                             "default_roles": ["case_worker"]})
        fake_provider({"email": "worker@company.com", "email_verified": True, "sub": "s-4"})
        r = await anon_client.post("/api/v1/auth/real/sso/callback", json=_callback_body(row))
        assert r.status_code == 200
        u = (await session.execute(
            select(HelixUserModel).where(HelixUserModel.sso_subject == "s-4")
        )).scalar_one()
        assert u.roles == ["case_worker"]

    @pytest.mark.asyncio
    async def test_username_collision_does_not_500(
        self, anon_client, session, fake_provider,
    ):
        """`admin@company.com` derives `admin`, which is already taken."""
        session.add(HelixUserModel(username="admin", email="admin@velaris.local",
                                   roles=["admin"], is_active=True))
        await session.commit()
        row = await _provider_row(session, config={"allowed_domains": ["company.com"]})
        fake_provider({"email": "admin@company.com", "email_verified": True, "sub": "s-5"})

        r = await anon_client.post("/api/v1/auth/real/sso/callback", json=_callback_body(row))
        assert r.status_code == 200, r.text
        u = (await session.execute(
            select(HelixUserModel).where(HelixUserModel.sso_subject == "s-5")
        )).scalar_one()
        assert u.username == "admin-2"


# ── 3. email verification ─────────────────────────────────────────

class TestEmailTrust:
    @pytest.mark.asyncio
    async def test_unverified_google_email_is_refused(
        self, anon_client, session, fake_provider,
    ):
        row = await _provider_row(session, config={"allowed_domains": ["company.com"]})
        fake_provider({"email": "spoof@company.com", "email_verified": False, "sub": "s-6"})
        r = await anon_client.post("/api/v1/auth/real/sso/callback", json=_callback_body(row))
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "VEL-AUTH-SSO-UNVERIFIED-403"

    @pytest.mark.asyncio
    async def test_github_profile_email_alone_is_not_trusted(
        self, anon_client, session, fake_provider,
    ):
        """/user.email is free text; with no verified address it must refuse."""
        row = await _provider_row(session, provider="github",
                            config={"allowed_domains": ["company.com"]})
        fake_provider({"email": "ceo@company.com", "id": 42, "login": "attacker"}, emails=[])
        r = await anon_client.post(
            "/api/v1/auth/real/sso/callback",
            json=_callback_body(row),
        )
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "VEL-AUTH-SSO-UNVERIFIED-403"

    @pytest.mark.asyncio
    async def test_github_uses_the_verified_primary_address(
        self, anon_client, session, fake_provider,
    ):
        row = await _provider_row(session, provider="github",
                            config={"allowed_domains": ["company.com"]})
        fake_provider(
            {"email": "vanity@company.com", "id": 43, "login": "dev"},
            emails=[{"email": "other@company.com", "primary": False, "verified": True},
                    {"email": "real@company.com", "primary": True, "verified": True}],
        )
        r = await anon_client.post(
            "/api/v1/auth/real/sso/callback",
            json=_callback_body(row),
        )
        assert r.status_code == 200, r.text
        u = (await session.execute(
            select(HelixUserModel).where(HelixUserModel.sso_subject == "43")
        )).scalar_one()
        assert u.email == "real@company.com"

    @pytest.mark.asyncio
    async def test_enrolment_no_longer_adopts_an_existing_account_by_email(
        self, anon_client, session, fake_provider,
    ):
        """`email_verified` is not proof that the *account* is theirs.

        It says the provider believes the person controls that mailbox. Whoever
        administers the directory chooses those addresses, so adopting on a
        match hands them whichever local account already holds it, with its
        roles. An administrator links the account deliberately instead.
        """
        session.add(HelixUserModel(username="jo", email="Jo@Company.com",
                                   roles=["designer"], is_active=True))
        await session.commit()
        row = await _provider_row(session, config={})
        fake_provider({"email": "jo@company.com", "email_verified": True, "sub": "s-7"})

        r = await anon_client.post("/api/v1/auth/real/sso/callback", json=_callback_body(row))
        assert r.status_code == 403
        u = (await session.execute(
            select(HelixUserModel).where(HelixUserModel.username == "jo")
        )).scalar_one()
        assert u.sso_subject is None, "the account must not have been claimed"

    @pytest.mark.asyncio
    async def test_the_opt_in_knob_links_a_passwordless_account(
        self, anon_client, session, fake_provider,
    ):
        """The old convenience is still available per provider, explicitly.

        Case-insensitively, and without downgrading the account it links."""
        session.add(HelixUserModel(username="jo", email="Jo@Company.com",
                                   roles=["designer"], is_active=True))
        await session.commit()
        row = await _provider_row(session, config={"link_existing_by_email": True})
        fake_provider({"email": "jo@company.com", "email_verified": True, "sub": "s-7"})

        r = await anon_client.post("/api/v1/auth/real/sso/callback", json=_callback_body(row))
        assert r.status_code == 200, r.text
        u = (await session.execute(
            select(HelixUserModel).where(HelixUserModel.username == "jo")
        )).scalar_one()
        assert u.sso_subject == "s-7"
        assert u.roles == ["designer"], "enrolment must not downgrade an existing account"

    @pytest.mark.asyncio
    async def test_the_opt_in_knob_still_refuses_a_password_account(
        self, anon_client, session, fake_provider,
    ):
        """A row with a password is somebody who signs in here directly."""
        session.add(HelixUserModel(username="admin2", email="admin2@company.com",
                                   roles=["admin"], is_active=True,
                                   password_hash="$2b$12$local"))
        await session.commit()
        row = await _provider_row(session, config={"link_existing_by_email": True})
        fake_provider({"email": "admin2@company.com", "email_verified": True,
                       "sub": "s-9"})

        r = await anon_client.post("/api/v1/auth/real/sso/callback", json=_callback_body(row))
        assert r.status_code == 403
        u = (await session.execute(
            select(HelixUserModel).where(HelixUserModel.username == "admin2")
        )).scalar_one()
        assert u.sso_subject is None

    @pytest.mark.asyncio
    async def test_an_administrator_can_link_the_account_and_then_it_signs_in(
        self, anon_client, client, session, fake_provider,
    ):
        """The refusal has a way out: OIDC keys on `(provider, subject)`, so the
        link endpoint has to bind that pair — an `external_id` alone would leave
        this login refused forever."""
        session.add(HelixUserModel(username="jo", email="jo@company.com",
                                   roles=["designer"], is_active=True))
        await session.commit()
        row = await _provider_row(session, config={})
        fake_provider({"email": "jo@company.com", "email_verified": True, "sub": "s-11"})

        refused = await anon_client.post(
            "/api/v1/auth/real/sso/callback", json=_callback_body(row))
        assert refused.status_code == 403

        linked = await client.post("/api/v1/identity/provisioning/links", json={
            "identifier": "jo@company.com",
            "sso_provider": row.provider, "sso_subject": "s-11"})
        assert linked.status_code == 200, linked.text

        accepted = await anon_client.post(
            "/api/v1/auth/real/sso/callback", json=_callback_body(row))
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["user"]["username"] == "jo"


# ── 4. disabled accounts ──────────────────────────────────────────

class TestDisabledAccount:
    @pytest.mark.asyncio
    async def test_disabled_user_cannot_sign_in_with_sso(
        self, anon_client, session, fake_provider,
    ):
        session.add(HelixUserModel(username="gone", email="gone@company.com",
                                   roles=["viewer"], is_active=False,
                                   sso_provider="google", sso_subject="s-8"))
        await session.commit()
        row = await _provider_row(session, config={"allowed_domains": ["company.com"]})
        fake_provider({"email": "gone@company.com", "email_verified": True, "sub": "s-8"})

        r = await anon_client.post("/api/v1/auth/real/sso/callback", json=_callback_body(row))
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "VEL-AUTH-DISABLED-403"


class TestResetIsClosedToFederatedAccounts:
    """Password reset is the only flow that hands out a local password without
    asking for the old one. Left open, an SSO user can mint a credential they
    never had — and it outlives their IdP account, so revoking them at the
    provider stops the SSO button and nothing else. Change-password is not the
    hole: it already requires a password a federated account does not have.

    Every refusal here is *invisible*. A distinct message, status or next screen
    would turn the flow into an oracle for which addresses exist and which are
    SSO-only.
    """

    FEDERATED = "jit@company.com"
    LOCAL = "local@company.com"

    async def _users(self, session):
        session.add(HelixUserModel(
            username="jit", email=self.FEDERATED, roles=["viewer"],
            is_active=True, password_hash=None,
            external_id="kf2cBOSnXvm2DsXTsufE"))
        session.add(HelixUserModel(
            username="local", email=self.LOCAL, roles=["viewer"],
            is_active=True, password_hash=auth_real._hash_password("original-pw")))
        await session.commit()

    async def _forgot(self, anon_client, email):
        return await anon_client.post(
            "/api/v1/auth/real/forgot-password", json={"email": email})

    @pytest.mark.asyncio
    async def test_no_code_is_minted_for_an_account_with_no_local_password(
        self, anon_client, session,
    ):
        from velaris_shared.db.models import AuthOtpModel

        await self._users(session)
        assert (await self._forgot(anon_client, self.FEDERATED)).status_code == 200

        otps = (await session.execute(
            select(AuthOtpModel).where(AuthOtpModel.purpose == "password_reset")
        )).scalars().all()
        assert otps == [], "a federated account must not be issued a reset code"

    @pytest.mark.asyncio
    async def test_a_local_account_still_gets_one(self, anon_client, session):
        from velaris_shared.db.models import AuthOtpModel

        await self._users(session)
        assert (await self._forgot(anon_client, self.LOCAL)).status_code == 200
        otps = (await session.execute(
            select(AuthOtpModel).where(AuthOtpModel.purpose == "password_reset")
        )).scalars().all()
        assert len(otps) == 1

    @pytest.mark.asyncio
    async def test_all_three_outcomes_are_indistinguishable(self, anon_client, session):
        """Unknown address, federated account, real account — one answer."""
        await self._users(session)
        answers = [
            (await self._forgot(anon_client, e)).json()
            for e in ("nobody@company.com", self.FEDERATED, self.LOCAL)
        ]
        assert answers[0] == answers[1] == answers[2]
        assert "single sign-on" in answers[0]["message"], (
            "the shared wording has to tell an SSO user why no code arrives, "
            "without telling an attacker which address is which"
        )

    @pytest.mark.asyncio
    async def test_a_code_minted_before_the_password_went_away_cannot_restore_one(
        self, anon_client, session,
    ):
        """The window `/forgot-password` alone does not close.

        A code issued while the account still had a password stays valid for its
        lifetime. If the account is federated in between, that code must not put
        a password back.
        """
        await self._users(session)
        assert (await self._forgot(anon_client, self.LOCAL)).status_code == 200

        from velaris_shared.db.models import AuthOtpModel
        otp_row = (await session.execute(
            select(AuthOtpModel).where(AuthOtpModel.purpose == "password_reset")
        )).scalar_one()
        # The code itself is only in the email; re-point the stored hash at a
        # known value so the test can present the right one.
        otp_row.otp_hash = auth_real._hash_password("123456")
        user = (await session.execute(
            select(HelixUserModel).where(HelixUserModel.email == self.LOCAL)
        )).scalar_one()
        user.password_hash = None            # became federated after the code
        await session.commit()

        r = await anon_client.post("/api/v1/auth/real/reset-password", json={
            "email": self.LOCAL, "otp": "123456", "new_password": "attacker-set-pw",
        })
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "VEL-AUTH-OTP-INVALID-400"

        await session.refresh(user)
        assert user.password_hash is None, "the account must still be federated"


# ── 5. provider discovery ─────────────────────────────────────────

class TestAdminManagement:
    """The Admin Console tab's endpoints. The anonymous discovery endpoint
    cannot back a config screen — it hides `config`, `tenant_id` and disabled
    rows on purpose — and there was no way to edit a provider at all."""

    @pytest.mark.asyncio
    async def test_manage_listing_needs_a_token(self, anon_client, session):
        row = await _provider_row(session)
        r = await anon_client.get("/api/v1/auth/real/sso/providers/manage")
        assert r.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_manage_listing_shows_config_but_never_the_secret(self, client, session):
        row = await _provider_row(session, config={"allowed_domains": ["company.com"]})
        r = await client.get("/api/v1/auth/real/sso/providers/manage")
        assert r.status_code == 200
        row = r.json()["providers"][0]
        assert row["config"]["allowed_domains"] == ["company.com"]
        assert row["has_secret"] is False
        assert "client_secret" not in row and "client_secret_enc" not in row

    @pytest.mark.asyncio
    async def test_manage_listing_includes_disabled_rows(self, client, session):
        """The login page must not see these; the admin screen must."""
        row = await _provider_row(session)
        row.enabled = False
        await session.commit()
        assert (await client.get("/api/v1/auth/real/sso/providers/manage")).json()["providers"]
        assert (await client.get("/api/v1/auth/real/sso/providers")).json()["providers"] == []

    @pytest.mark.asyncio
    async def test_create_refuses_a_provider_the_callback_cannot_service(self, client):
        r = await client.post("/api/v1/auth/real/sso/providers", json={
            "provider": "azure", "client_id": "x", "client_secret": "y",
        })
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_create_stores_the_secret_and_reports_it_as_set(self, client):
        r = await client.post("/api/v1/auth/real/sso/providers", json={
            "provider": "google", "client_id": "cid", "client_secret": "shh",
            "config": {"allowed_domains": ["company.com"]},
        })
        assert r.status_code == 201
        assert r.json()["has_secret"] is True
        assert "shh" not in r.text

    @pytest.mark.asyncio
    async def test_patch_updates_config_and_keeps_the_secret_when_blank(self, client, session):
        created = (await client.post("/api/v1/auth/real/sso/providers", json={
            "provider": "google", "client_id": "cid", "client_secret": "shh",
        })).json()
        stored = (await session.execute(
            select(SsoProviderModel).where(SsoProviderModel.id == uuid.UUID(created["id"]))
        )).scalar_one()
        before = dict(stored.client_secret_enc)

        r = await client.patch(f"/api/v1/auth/real/sso/providers/{created['id']}", json={
            "config": {"allowed_domains": ["company.com"], "default_roles": ["case_worker"]},
            "enabled": False,
        })
        assert r.status_code == 200
        assert r.json()["config"]["default_roles"] == ["case_worker"]
        assert r.json()["enabled"] is False

        await session.refresh(stored)
        assert dict(stored.client_secret_enc) == before, "a blank secret must not erase the stored one"

    @pytest.mark.asyncio
    async def test_patch_rotates_the_secret_when_one_is_supplied(self, client, session):
        created = (await client.post("/api/v1/auth/real/sso/providers", json={
            "provider": "google", "client_id": "cid", "client_secret": "old",
        })).json()
        stored = (await session.execute(
            select(SsoProviderModel).where(SsoProviderModel.id == uuid.UUID(created["id"]))
        )).scalar_one()
        before = dict(stored.client_secret_enc)

        r = await client.patch(f"/api/v1/auth/real/sso/providers/{created['id']}",
                               json={"client_secret": "new"})
        assert r.status_code == 200
        await session.refresh(stored)
        assert dict(stored.client_secret_enc) != before

    @pytest.mark.asyncio
    async def test_patch_on_a_missing_provider_is_a_coded_404(self, client):
        r = await client.patch(f"/api/v1/auth/real/sso/providers/{uuid.uuid4()}", json={"enabled": True})
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "VEL-AUTH-SSO-UNCONFIGURED-404"

    @pytest.mark.asyncio
    async def test_config_written_by_the_screen_drives_the_callback(
        self, client, anon_client, session, fake_provider,
    ):
        """End to end: what the tab saves is what the login path enforces."""
        created = (await client.post("/api/v1/auth/real/sso/providers", json={
            "provider": "google", "client_id": "cid", "client_secret": "shh",
            "config": {"allowed_domains": ["company.com"], "default_roles": ["designer"]},
        })).json()
        fake_provider({"email": "hire@company.com", "email_verified": True, "sub": "s-9"})
        row = types.SimpleNamespace(id=created["id"], provider=created["provider"])

        r = await anon_client.post("/api/v1/auth/real/sso/callback", json=_callback_body(row))
        assert r.status_code == 200, r.text
        u = (await session.execute(
            select(HelixUserModel).where(HelixUserModel.sso_subject == "s-9")
        )).scalar_one()
        assert u.roles == ["designer"]

        # Now turn provisioning off through the same screen.
        await client.patch(f"/api/v1/auth/real/sso/providers/{created['id']}",
                           json={"config": {"allowed_domains": []}})
        fake_provider({"email": "second@company.com", "email_verified": True, "sub": "s-10"})
        r2 = await anon_client.post("/api/v1/auth/real/sso/callback", json=_callback_body(row))
        assert r2.status_code == 403
        assert r2.json()["error"]["code"] == "VEL-AUTH-SSO-PROVISION-403"


class TestRoleGate:
    """`require_role` is an async dependency factory. Every one of these
    endpoints called it inline — `require_role("admin")(user)` — which builds a
    coroutine, discards it, and never raises. The gate had never run: any
    authenticated user could register an SSO provider pointing at an OAuth app
    they control with `default_roles: ["admin"]`, sign in through it, and come
    back an admin."""

    @staticmethod
    def _viewer_headers():
        from velaris_shared.auth.jwt_handler import create_dev_token
        from velaris_shared.config import get_settings
        s = get_settings()
        tok = create_dev_token(
            user_id="viewer-1", username="viewer-1", roles=["viewer"],
            secret=s.auth_secret, private_key=s.auth_rsa_private_key or "",
        )
        return {"Authorization": f"Bearer {tok}"}

    @pytest.mark.asyncio
    async def test_viewer_cannot_register_a_provider(self, client):
        r = await client.post(
            "/api/v1/auth/real/sso/providers",
            headers=self._viewer_headers(),
            json={"provider": "google", "client_id": "attacker-app",
                  "client_secret": "s",
                  "config": {"allowed_domains": ["attacker.com"],
                             "default_roles": ["admin"]}},
        )
        assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_viewer_cannot_delete_a_provider(self, client, session):
        row = await _provider_row(session)
        r = await client.delete(f"/api/v1/auth/real/sso/providers/{row.id}",
                                headers=self._viewer_headers())
        assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_viewer_cannot_read_or_edit_provider_config(self, client, session):
        row = await _provider_row(session)
        assert (await client.get("/api/v1/auth/real/sso/providers/manage",
                                 headers=self._viewer_headers())).status_code == 403
        assert (await client.patch(f"/api/v1/auth/real/sso/providers/{row.id}",
                                   headers=self._viewer_headers(),
                                   json={"enabled": False})).status_code == 403

    @pytest.mark.asyncio
    async def test_viewer_cannot_list_users(self, client):
        """Same dead gate; this one leaked every account's roles and email."""
        r = await client.get("/api/v1/auth/real/users", headers=self._viewer_headers())
        assert r.status_code == 403

    @pytest.mark.asyncio
    async def test_superadmin_cannot_be_an_auto_provisioned_role(self, client):
        r = await client.post("/api/v1/auth/real/sso/providers", json={
            "provider": "google", "client_id": "cid", "client_secret": "s",
            "config": {"allowed_domains": ["company.com"], "default_roles": ["superadmin"]},
        })
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_superadmin_is_stripped_even_if_the_row_is_edited_directly(
        self, anon_client, session, fake_provider,
    ):
        row = await _provider_row(session, config={"allowed_domains": ["company.com"],
                                             "default_roles": ["superadmin", "viewer"]})
        fake_provider({"email": "sneak@company.com", "email_verified": True, "sub": "s-11"})
        r = await anon_client.post("/api/v1/auth/real/sso/callback", json=_callback_body(row))
        assert r.status_code == 200
        u = (await session.execute(
            select(HelixUserModel).where(HelixUserModel.sso_subject == "s-11")
        )).scalar_one()
        assert u.roles == ["viewer"]


class TestSamlProviderWrites:
    """A `saml` row is administered through the same CRUD, so its own unsafe
    values have to be refused here — `SamlConfig` silently drops them on the
    read path, which is right for a config column a DBA can edit but wrong as
    the only answer an administrator ever gets."""

    _SAML = {
        "idp_entity_id": "http://idp.test/entity",
        "idp_sso_url": "https://idp.test/sso",
        "idp_x509_certs": ["MIIDqjCCApKg"],
        "sp_entity_id": "velaris-studio",
        "acs_url": "https://velaris.test/api/v1/auth/saml/acme/acs",
    }

    def _body(self, **config):
        return {"provider": "saml", "client_id": "velaris-studio", "client_secret": "",
                "config": {**self._SAML, **config}}

    @pytest.mark.asyncio
    async def test_a_complete_saml_row_is_accepted_without_a_secret(self, client):
        r = await client.post("/api/v1/auth/real/sso/providers", json=self._body())
        assert r.status_code == 201, r.text

    @pytest.mark.asyncio
    async def test_an_incomplete_saml_row_is_refused(self, client):
        body = self._body()
        del body["config"]["idp_x509_certs"]
        r = await client.post("/api/v1/auth/real/sso/providers", json=body)
        assert r.status_code == 400
        assert "idp_x509_certs" in r.text

    @pytest.mark.parametrize("prefix", ["//evil.test", "/\\evil.test", "https://evil.test", "studio"])
    @pytest.mark.asyncio
    async def test_a_landing_path_that_leaves_the_origin_is_refused(self, client, prefix):
        r = await client.post("/api/v1/auth/real/sso/providers",
                              json=self._body(allowed_relay_state_prefixes=[prefix]))
        assert r.status_code == 400
        assert "landing path" in r.text

    @pytest.mark.parametrize("skew", [601, -1, "soon"])
    @pytest.mark.asyncio
    async def test_an_unbounded_clock_skew_is_refused(self, client, skew):
        r = await client.post("/api/v1/auth/real/sso/providers",
                              json=self._body(clock_skew_seconds=skew))
        assert r.status_code == 400
        assert "clock_skew_seconds" in r.text

    @pytest.mark.asyncio
    async def test_a_patch_cannot_smuggle_one_in_either(self, client):
        created = (await client.post("/api/v1/auth/real/sso/providers",
                                     json=self._body())).json()
        r = await client.patch(f"/api/v1/auth/real/sso/providers/{created['id']}", json={
            "config": {**self._SAML, "allowed_relay_state_prefixes": ["//evil.test"]},
        })
        assert r.status_code == 400

    @pytest.mark.parametrize("icon", ["https://cdn.evil.test/i.svg", "//evil.test/i.svg",
                                      "/\\evil.test/i.svg"])
    @pytest.mark.asyncio
    async def test_a_remote_icon_is_refused_on_write_not_only_dropped_on_read(
        self, client, icon,
    ):
        r = await client.post("/api/v1/auth/real/sso/providers", json=self._body(icon_url=icon))
        assert r.status_code == 400
        assert "icon_url" in r.text

    @pytest.mark.asyncio
    async def test_the_audit_record_names_the_allowlist_this_protocol_reads(
        self, client, session,
    ):
        """"Who widened auto-provisioning, and to what" is the whole point of
        the record. It logged `allowed_domains`, which a SAML row never has, so
        every SAML edit was recorded as an empty allowlist."""
        from velaris_shared.db.models import SecurityEventModel

        created = (await client.post(
            "/api/v1/auth/real/sso/providers",
            json=self._body(email_domain_allowlist=["acme.com"]),
        )).json()
        r = await client.patch(f"/api/v1/auth/real/sso/providers/{created['id']}", json={
            "config": {**self._SAML, "email_domain_allowlist": ["acme.com", "acme.co.uk"],
                       "link_existing_by_email": True},
        })
        assert r.status_code == 200

        event = (await session.execute(
            select(SecurityEventModel)
            .where(SecurityEventModel.event_type == "sso_provider_updated")
        )).scalars().all()[-1]
        assert event.details["allowed_domains"] == ["acme.com", "acme.co.uk"]
        assert event.details["link_existing_by_email"] is True

    @pytest.mark.asyncio
    async def test_the_icon_rule_covers_oauth_rows_too(self, client):
        """`_branding` is applied to every provider, not just SAML."""
        r = await client.post("/api/v1/auth/real/sso/providers", json={
            "provider": "google", "client_id": "cid", "client_secret": "s",
            "config": {"icon_url": "https://cdn.evil.test/g.svg"},
        })
        assert r.status_code == 400


class TestCustomProvider:
    """The provider field is free text, so a deployment can point at Okta,
    Entra, Keycloak — anything OIDC-shaped. The cost of that freedom is that an
    admin now types the URLs the server will POST a client secret to, which is
    an SSRF primitive unless it is guarded."""

    _OKTA = {
        "auth_url":  "https://example.okta.com/oauth2/v1/authorize",
        "token_url": "https://example.okta.com/oauth2/v1/token",
        "userinfo":  "https://example.okta.com/oauth2/v1/userinfo",
        "scope":     "openid email profile",
    }

    @pytest.mark.asyncio
    async def test_a_custom_provider_without_endpoints_is_refused(self, client):
        r = await client.post("/api/v1/auth/real/sso/providers", json={
            "provider": "okta", "client_id": "cid", "client_secret": "s",
        })
        assert r.status_code == 400
        assert "token_url" in r.text

    @pytest.mark.asyncio
    async def test_a_custom_provider_with_endpoints_is_accepted(self, client, no_dns):
        r = await client.post("/api/v1/auth/real/sso/providers", json={
            "provider": "okta", "client_id": "cid", "client_secret": "s",
            "config": {**self._OKTA, "allowed_domains": ["company.com"]},
        })
        assert r.status_code == 201, r.text

    @pytest.mark.asyncio
    async def test_plain_http_endpoints_are_refused(self, client):
        r = await client.post("/api/v1/auth/real/sso/providers", json={
            "provider": "okta", "client_id": "cid", "client_secret": "s",
            "config": {**self._OKTA, "token_url": "http://localhost/oauth2/v1/token"},
        })
        assert r.status_code == 400
        assert "https" in r.text

    @pytest.mark.asyncio
    async def test_internal_endpoints_are_refused(self, client):
        """The client secret is POSTed to token_url — pointing it at the
        loopback or the cloud metadata service must not be possible."""
        for bad in ("https://localhost/token",
                    "https://127.0.0.1/token",
                    "https://169.254.169.254/token"):
            r = await client.post("/api/v1/auth/real/sso/providers", json={
                "provider": "okta", "client_id": "cid", "client_secret": "s",
                "config": {**self._OKTA, "token_url": bad},
            })
            assert r.status_code == 400, f"{bad} was accepted"

    @pytest.mark.asyncio
    async def test_a_preset_cannot_be_repointed_by_config(self, client, session):
        """`google` must always mean Google, whatever the row's config says."""
        row = await _provider_row(session, provider="google", config={
            "token_url": "https://evil.example.com/token",
            "userinfo":  "https://evil.example.com/me",
        })
        ep = auth_real._endpoints_for(row)
        assert ep["token_url"] == "https://oauth2.googleapis.com/token"

    @pytest.mark.asyncio
    async def test_an_edit_cannot_strip_a_custom_provider_of_its_endpoints(self, client, no_dns):
        created = (await client.post("/api/v1/auth/real/sso/providers", json={
            "provider": "okta", "client_id": "cid", "client_secret": "s",
            "config": {**self._OKTA},
        })).json()
        r = await client.patch(f"/api/v1/auth/real/sso/providers/{created['id']}",
                               json={"config": {"allowed_domains": ["company.com"]}})
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_a_custom_provider_signs_a_user_in(
        self, anon_client, session, fake_provider, no_dns,
    ):
        row = await _provider_row(session, provider="okta", config={
            **self._OKTA, "allowed_domains": ["company.com"],
        })
        fake_provider({"email": "person@company.com", "email_verified": True, "sub": "okta-1"})
        r = await anon_client.post("/api/v1/auth/real/sso/callback", json=_callback_body(row))
        assert r.status_code == 200, r.text
        assert "https://example.okta.com/oauth2/v1/token" in _FakeHttp.calls

    @pytest.mark.asyncio
    async def test_a_custom_provider_still_needs_a_verified_email(
        self, anon_client, session, fake_provider, no_dns,
    ):
        row = await _provider_row(session, provider="okta", config={
            **self._OKTA, "allowed_domains": ["company.com"],
        })
        fake_provider({"email": "person@company.com", "sub": "okta-2"})   # no claim
        r = await anon_client.post("/api/v1/auth/real/sso/callback", json=_callback_body(row))
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "VEL-AUTH-SSO-UNVERIFIED-403"


class TestDiscovery:
    @pytest.mark.asyncio
    async def test_is_reachable_without_a_token(self, anon_client, session):
        """A login page has no bearer; requiring one hid every SSO button."""
        row = await _provider_row(session)
        r = await anon_client.get("/api/v1/auth/real/sso/providers")
        assert r.status_code == 200
        assert [p["provider"] for p in r.json()["providers"]] == ["google"]

    @pytest.mark.asyncio
    async def test_does_not_leak_tenant_id(self, anon_client, session):
        row = await _provider_row(session)
        r = await anon_client.get("/api/v1/auth/real/sso/providers")
        assert "tenant_id" not in r.json()["providers"][0]


class TestButtonBranding:
    """`display_name` and `icon_url` are published to an anonymous login page.

    The icon is fetched by the *visitor's* browser, so one served by a third
    party reports every person who reaches the sign-in page to that host —
    before anybody has authenticated. Only a path this deployment serves, or a
    `data:` URI, may be published.
    """

    async def _icon(self, anon_client, session, value):
        await _provider_row(session, config={"icon_url": value, "display_name": "Okta"})
        r = await anon_client.get("/api/v1/auth/real/sso/providers")
        return r.json()["providers"][0]

    @pytest.mark.asyncio
    async def test_a_same_origin_path_is_published(self, anon_client, session):
        out = await self._icon(anon_client, session, "/idp/okta-lockup.svg")
        assert out["icon_url"] == "/idp/okta-lockup.svg"
        assert out["display_name"] == "Okta"

    @pytest.mark.asyncio
    async def test_a_data_uri_is_published(self, anon_client, session):
        out = await self._icon(anon_client, session, "data:image/svg+xml;base64,PHN2Zy8+")
        assert out["icon_url"].startswith("data:image/")

    @pytest.mark.parametrize("value", [
        "https://cdn.evil.test/okta.svg",
        "//evil.test/okta.svg",
        # A leading slash was the whole test, and these all pass it while a
        # browser resolves them against evil.test: `\` is normalised to `/`
        # inside an http(s) URL, and tab/newline are stripped before parsing.
        "/\\evil.test/okta.svg",
        "/\tevil.test/okta.svg",
        "/\nevil.test/okta.svg",
    ])
    @pytest.mark.asyncio
    async def test_an_icon_the_browser_would_fetch_elsewhere_is_dropped(
        self, anon_client, session, value,
    ):
        out = await self._icon(anon_client, session, value)
        assert "icon_url" not in out
        # The button still renders, just without the icon.
        assert out["display_name"] == "Okta"
