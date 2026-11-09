"""Credential encryption — hxv3 is what gets written, hxv1 is only ever read.

**What was actually wrong.** hxv1 is not broken: it appends an HMAC-SHA256 tag
and verifies it with `compare_digest` before decrypting, so stored secrets were
authenticated. What it *was*: hand-rolled cipher code — a SHA-256 counter-mode
keystream — in a codebase that already depends on `cryptography` and already
uses AES-256-GCM in `hxvault/`. Its own docstring said to swap it. It also used
a single key for both the keystream and the MAC.

hxv3 replaces the construction with the vetted primitive under a **separately
derived** key. hxv2 (HxVault) remains the better answer where a caller has a
tenant and an awaited session, because its key is per-tenant and can be
crypto-shredded; hxv3's key is platform-wide and shares its fate with
`auth_secret`.

**And one live defect this found:** the connector token-refresh path
re-encrypted with no tenant and no vault flag, so a connector stored under hxv2
was silently rewritten under the platform key the first time its provider
rotated a refresh token — Xero rotates on every use. Crypto-shredding that
tenant stopped covering those credentials, and nothing said so.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import base64
import json
import pathlib

import pytest

from case_service.hxbridge import encryption as enc


class TestWhatGetsWritten:

    def test_new_secrets_are_hxv3(self):
        out = enc.encrypt_credentials({"secret": "hunter2"})
        assert out["_enc"].startswith("hxv3:")

    def test_hxv1_is_never_written_again(self):
        """The prefix must survive for reading and appear in no write path."""
        for _ in range(5):
            assert not enc.encrypt_credentials({"k": "v"})["_enc"].startswith("hxv1:")

    def test_round_trip(self):
        creds = {"secret": "hunter2", "nested": {"a": 1}, "unicode": "héllo"}
        assert enc.decrypt_credentials(enc.encrypt_credentials(creds)) == creds

    def test_two_encryptions_differ(self):
        """A fresh nonce every time, or equal secrets are visibly equal."""
        a = enc.encrypt_credentials({"secret": "same"})["_enc"]
        b = enc.encrypt_credentials({"secret": "same"})["_enc"]
        assert a != b


class TestItIsRealAEAD:

    def test_a_flipped_bit_is_refused(self):
        """GCM authenticates. Tampering must fail closed, not decrypt garbage."""
        out = enc.encrypt_credentials({"secret": "hunter2"})
        raw = bytearray(base64.urlsafe_b64decode(out["_enc"][len("hxv3:"):]))
        raw[20] ^= 0x01
        tampered = {"_enc": "hxv3:" + base64.urlsafe_b64encode(bytes(raw)).decode()}
        with pytest.raises(Exception):
            enc.decrypt_credentials(tampered)

    def test_a_truncated_ciphertext_is_refused(self):
        out = enc.encrypt_credentials({"secret": "hunter2"})
        raw = base64.urlsafe_b64decode(out["_enc"][len("hxv3:"):])[:10]
        with pytest.raises(Exception):
            enc.decrypt_credentials(
                {"_enc": "hxv3:" + base64.urlsafe_b64encode(raw).decode()})

    def test_the_hxv3_key_is_not_the_hxv1_key(self):
        """A format change that reuses the key carries the old format's
        properties into the new one."""
        assert enc._derive_key("s") != enc._derive_key_v3("s")

    def test_aad_binds_the_scheme(self):
        """A blob cannot be lifted into another scheme that shares the key."""
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        key = enc._get_key_v3()
        blob = enc._aesgcm_encrypt(key, b"x")
        with pytest.raises(Exception):
            AESGCM(key).decrypt(blob[:12], blob[12:], b"some-other-context")


class TestLegacyStaysReadable:
    """An upgrade must not make existing secrets unreadable."""

    def _legacy_blob(self, creds: dict) -> dict:
        import os

        key = enc._get_key()
        nonce = os.urandom(16)
        payload = nonce + enc._xor_encrypt(
            key, json.dumps(creds, sort_keys=True).encode(), nonce)
        return {"_enc": "hxv1:" + base64.urlsafe_b64encode(payload).decode()}

    def test_an_hxv1_value_still_decrypts(self):
        creds = {"secret": "stored-before-the-upgrade"}
        assert enc.decrypt_credentials(self._legacy_blob(creds)) == creds

    def test_a_tampered_hxv1_value_is_still_refused(self):
        """hxv1 was authenticated too — that must not regress on the way out."""
        blob = self._legacy_blob({"secret": "x"})
        raw = bytearray(base64.urlsafe_b64decode(blob["_enc"][len("hxv1:"):]))
        raw[-1] ^= 0x01
        with pytest.raises(ValueError):
            enc.decrypt_credentials(
                {"_enc": "hxv1:" + base64.urlsafe_b64encode(bytes(raw)).decode()})

    def test_plain_values_pass_through(self):
        assert enc.decrypt_credentials({"user": "x"}) == {"user": "x"}
        assert enc.decrypt_credentials({}) == {}


#: Source files are read from the repository, from whichever service now holds
#: them (the payments router moved to commerce-service, R5e step 7; the portal
#: routers to portal-service, step 8; the sign-in routers to user-service, step 10).
_SERVICES = pathlib.Path(__file__).resolve().parents[3]


def _source(rel: str) -> str:
    for root in (_SERVICES / "case-service" / "case_service",
                 _SERVICES / "commerce-service" / "commerce_service",
                 _SERVICES / "portal-service" / "portal_service",
                 _SERVICES / "user-service" / "user_service"):
        if (root / rel).exists():
            return (root / rel).read_text()
    raise FileNotFoundError(rel)


class TestTheRefreshPathDoesNotDowngrade:

    def test_the_executor_preserves_the_stored_format(self):
        """The defect, pinned: re-encrypting must ask what it was, not assume."""
        src = _source("hxbridge/executor.py")
        block = src[src.index("_new_refresh_token"):]
        block = block[:block.index("await session.commit()")]
        assert 'startswith("hxv2:")' in block, (
            "the token-refresh path must re-encrypt in the format the "
            "credentials were already in, or an hxv2 connector is silently "
            "moved to the platform key the first time its token rotates.")
        assert "vault=was_vaulted" in block
        assert "tenant_id=connector.tenant_id" in block, (
            "vault=True without a tenant cannot reach the right DEK.")


class TestUnreadableDeksAreReportedOnce:
    """A permanent condition stated every 30 seconds is noise, not an alert.

    The dev box logged a full stack trace per bad row per resync tick — 822 of
    them — and none of them said what it meant or what to do.
    """

    def test_the_report_is_deduplicated_by_row(self):
        from case_service.hxvault import keyring

        class _Row:
            id = "row-1"
            tenant_id = None

        keyring._UNREADABLE.discard("row-1")
        calls = []
        original = keyring.logger.error
        keyring.logger.error = lambda *a, **k: calls.append(a)
        try:
            keyring._report_unreadable(_Row(), "startup")
            keyring._report_unreadable(_Row(), "resync")
            keyring._report_unreadable(_Row(), "resync")
        finally:
            keyring.logger.error = original
            keyring._UNREADABLE.discard("row-1")
        assert len(calls) == 1, "an unreadable DEK must be reported once, not per tick"

    def test_the_message_names_the_likely_cause(self):
        """An operator reading this at 3am needs the next step, not a traceback."""
        import inspect

        from case_service.hxvault import keyring

        src = inspect.getsource(keyring._report_unreadable)
        assert "VELARIS_CASE_KEK" in src and "auth_secret" in src

    def test_neither_loop_logs_an_exception_per_row(self):
        import inspect

        from case_service.hxvault import keyring

        for fn in (keyring.warm_cache, keyring.resync_cache):
            src = inspect.getsource(fn)
            assert "logger.exception" not in src, (
                f"{fn.__name__} still logs a stack trace per unreadable row")


class TestNoConfigurationStoresPlaintext:
    """`_encrypt_token` used to fall back to base64 when no dedicated key was
    configured — a personal access token stored in a form anyone can read,
    labelled `b64:` as though that were a cipher, with nothing saying so."""

    def _clear_keys(self, monkeypatch):
        from velaris_shared.config import get_settings

        s = get_settings()
        monkeypatch.setattr(s, "marketplace_token_key", None, raising=False)
        monkeypatch.setattr(s, "storage_master_key", None, raising=False)

    def test_an_unconfigured_install_still_encrypts(self, monkeypatch):
        from commerce_service.api.routers.marketplace import (
            _encrypt_token,
            _get_encryption_key,
        )

        self._clear_keys(monkeypatch)
        assert _get_encryption_key() is None, "precondition: no dedicated key"
        out = _encrypt_token("ghp_a_real_looking_token")
        assert not out.startswith("b64:"), "a token was stored as plain base64"
        assert "ghp_a_real_looking_token" not in out

    def test_it_round_trips_without_a_dedicated_key(self, monkeypatch):
        from commerce_service.api.routers.marketplace import _decrypt_token, _encrypt_token

        self._clear_keys(monkeypatch)
        assert _decrypt_token(_encrypt_token("tok")) == "tok"

    def test_legacy_b64_values_are_still_readable(self):
        """Refusing them would lock an install out of its own sources."""
        import base64

        from commerce_service.api.routers.marketplace import _decrypt_token

        assert _decrypt_token("b64:" + base64.b64encode(b"old").decode()) == "old"


class TestPerTenantDekWherePossible:
    """hxv3's key is platform-wide; hxv2's is per tenant and can be shredded.

    Most call sites were on the platform key only because hxv2 needs an awaited
    `ensure_dek` — which every one of them was in a position to do.
    """

    CALL_SITES = [
        ("auth_real.py", "mfa_secret_enc"),
        ("auth_real.py", "client_secret_enc"),
        ("channels.py", "signing_secret_enc"),
        ("branches.py", "api_token_enc"),
    ]

    @pytest.mark.parametrize("router,column", CALL_SITES)
    def test_the_site_asks_for_a_tenant_dek(self, router, column):
        """Every WRITE of the column, not the first mention of it — the reads
        come earlier in the file and would make this pass vacuously."""
        import re

        src = _source(f"api/routers/{router}")
        writes = [m for m in re.finditer(
            rf"{column}\s*=\s*(await\s+)?encrypt_credentials", src)]
        assert writes, f"no encrypted write of {column} found in {router}"
        for m in writes:
            assert src[m.start():m.end() + 30].find("encrypt_credentials_vaulted") >= 0, (
                f"{router}:{column} still stores under the platform key even "
                "though it has a session and a tenant.")

    async def test_it_falls_back_rather_than_failing(self, session):
        """A tenant with no resolvable DEK must still get an encrypted secret —
        never plaintext, and never a 500 on a credential write."""
        from case_service.hxbridge.encryption import (
            decrypt_credentials,
            encrypt_credentials_vaulted,
        )

        out = await encrypt_credentials_vaulted(
            session, {"secret": "x"}, tenant_id="no-such-tenant-slug")
        assert out["_enc"].startswith(("hxv2:", "hxv3:"))
        assert decrypt_credentials(out) == {"secret": "x"}

    async def test_no_tenant_is_the_platform_key(self, session):
        from case_service.hxbridge.encryption import encrypt_credentials_vaulted

        out = await encrypt_credentials_vaulted(session, {"s": "x"}, tenant_id=None)
        assert out["_enc"].startswith("hxv3:")


class TestBankReferencesAreShreddable:
    """A bank reference is a data subject's banking detail.

    It was encrypted from the start, but under the platform-wide key — sound
    confidentiality, and nothing about it that could be destroyed for one
    person. Erasing a tenant has to make this unreadable, which is the entire
    reason per-tenant DEKs exist.
    """

    async def test_it_is_stored_under_the_tenant_key(self, session):
        from commerce_service.api.routers.payments import (
            _decrypt_bank_ref,
            _encrypt_bank_ref,
        )
        from case_service.hxvault.keyring import ensure_dek
        from case_service.tenancy.identity import resolve_tenant

        from velaris_shared.db.models import TenantModel

        # Create the tenant the test needs rather than skipping when the fixture
        # database has none — a skip here would quietly stop checking the one
        # property the change exists for.
        slug = "shredtest"
        session.add(TenantModel(slug=slug, name="Shred Test"))
        await session.flush()

        resolved = await resolve_tenant(session, slug)
        assert resolved is not None
        await ensure_dek(session, resolved[0])

        stored = await _encrypt_bank_ref(session, "ACME|12345678|00-11-22", slug)
        assert '"hxv2:' in stored or "hxv2:" in stored, (
            "a bank reference must go under the tenant DEK when one resolves — "
            "on the platform key it cannot be crypto-shredded for one person.")
        assert _decrypt_bank_ref(stored) == "ACME|12345678|00-11-22"

    async def test_it_never_falls_back_to_plaintext(self, session):
        """A disbursement must not fail over key material, and must not store
        the account number in the clear when a tenant cannot be resolved."""
        from commerce_service.api.routers.payments import (
            _decrypt_bank_ref,
            _encrypt_bank_ref,
        )

        stored = await _encrypt_bank_ref(session, "ACME|12345678", "no-such-tenant")
        assert "12345678" not in stored
        assert _decrypt_bank_ref(stored) == "ACME|12345678"

    async def test_legacy_rows_still_read(self, session):
        """Existing rows are hxv1 or hxv3 and must keep opening."""
        import json

        from commerce_service.api.routers.payments import _decrypt_bank_ref
        from case_service.hxbridge.encryption import encrypt_credentials

        legacy = json.dumps(encrypt_credentials({"v": "OLD|999"}))
        assert _decrypt_bank_ref(legacy) == "OLD|999"

    def test_both_call_sites_pass_a_tenant(self):
        """The portal one is the customer submitting their OWN details."""
        import re

        for router in ("payments.py", "portal.py"):
            src = _source(f"api/routers/{router}")
            for m in re.finditer(r"await _encrypt_bank_ref\(", src):
                window = src[m.start():m.start() + 260]
                assert "tenant" in window, (
                    f"{router}: a bank reference was encrypted with no tenant, so "
                    "it lands on the platform key and cannot be shredded.")
