"""Error-catalog phase 7 — the remaining `VEL-MKT-*` raise sites.

Unlike the previous four passes, this one **extended a family that already
existed**. Six marketplace codes had been in the registry since phase 1, so the
work was mostly deciding what did *not* need a new code: 24 of the 39 newly
coded sites reuse those six. The three role gates in particular went to
`MKT_ACCESS_403` rather than getting a parallel `ROLE` code, because that
entry's own definition has always read "does not own the workspace **or lacks
the marketplace role**" — a second code would make the catalog answer one
question twice.

What did earn new codes are conditions the six could not say: policy blocks
(distinct from identity), the conformance gate, HxSandbox, quota, terminal
grants, pack ordering, and install failure.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import re
from pathlib import Path

from velaris_shared.core import error_codes as ec

# In commerce-service since R5e step 7.
_ROUTER = (Path(__file__).resolve().parents[3] / "commerce-service"
           / "commerce_service" / "api" / "routers" / "marketplace.py")


def _router_src() -> str:
    return _ROUTER.read_text()


# ── What was reused rather than reinvented ────────────────────────

class TestFamilyExtension:
    def test_role_gates_reuse_the_existing_access_code(self):
        """The three access gates must not have grown a parallel code —
        `MKT_ACCESS_403` is defined to cover role as well as ownership.

        Asserted against each helper's AST rather than against its message text.
        The original matched three literal strings on a `raise coded` line, which
        broke the moment Phase 4 renamed the refusals from roles to privileges
        (and wrapped one across two lines) — while the thing the test is *for*,
        the code they raise, had not changed at all."""
        import ast

        tree = ast.parse(_router_src())
        fns = {n.name: n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for name in ("_require_developer_or_admin", "_require_admin", "_can_view"):
            assert name in fns, f"{name} disappeared — was the gate removed?"
            codes = {a.id for call in ast.walk(fns[name])
                     if isinstance(call, ast.Call)
                     and getattr(call.func, "id", "") == "coded"
                     for a in call.args if isinstance(a, ast.Name)}
            assert "MKT_ACCESS_403" in codes, (
                f"{name} should reuse MKT_ACCESS_403, raises {codes or 'nothing'}")

    def test_ownership_refusals_use_the_same_code_as_role_refusals(self):
        """Both are "you may not", and the catalog entry says so."""
        src = _router_src()
        for msg in ("Cannot delete another user's workspace",
                    "Cannot install into another user's workspace",
                    "Cannot revoke install from another tenant"):
            line = next(l for l in src.splitlines() if msg in l)
            assert "MKT_ACCESS_403" in line

    def test_no_duplicate_code_was_minted_for_an_existing_condition(self):
        """A `ROLE` or `OWNER` code would mean the family said one thing twice."""
        mkt = {v for k, v in vars(ec).items()
               if k.startswith("MKT_") and isinstance(v, str)}
        assert not [c for c in mkt if "ROLE" in c or "OWNER" in c]


# ── Policy is not identity ────────────────────────────────────────

class TestPolicyVsIdentity:
    def test_blacklist_and_access_rules_are_a_different_code(self):
        """`MKT_ACCESS_403` means *who you are*; `MKT_BLOCKED_403` means *what
        the policy says*. Same status, and the fixes are unrelated: grant a
        role vs change a blacklist entry."""
        src = _router_src()
        blacklist = next(l for l in src.splitlines() if "Package is blacklisted" in l)
        assert "MKT_BLOCKED_403" in blacklist
        assert ec.MKT_BLOCKED_403 != ec.MKT_ACCESS_403


# ── The conformance gate answers the same way on both paths ───────

class TestConformanceGate:
    def test_install_and_update_paths_share_the_code(self):
        """The gate refuses on install and on update approval. One coded and
        the other left bare would be the drift this catalog exists to stop —
        it was caught mid-pass and fixed."""
        # Both are multi-line raises, so the constant sits on the line after
        # `raise coded(` — count the constant itself, less its import.
        assert _router_src().count("MKT_CONFORMANCE_422") - 1 == 2

    def test_it_is_422_not_400(self):
        """The request was well-formed; the *package* failed its checks."""
        assert ec.MKT_CONFORMANCE_422.endswith("-422")


# ── A revoked grant is terminal ───────────────────────────────────

class TestGrantIsTerminal:
    def test_both_revoked_states_share_one_code(self):
        src = _router_src()
        hits = [l for l in src.splitlines() if "MKT_GRANT_409" in l and "raise coded" in l]
        assert len(hits) == 2

    def test_grant_code_is_not_the_installed_code(self):
        """`MKT_INSTALLED_409` is "already installed" — a different 409 with a
        different meaning. Reusing it here would have been the lazy read."""
        assert ec.MKT_GRANT_409 != ec.MKT_INSTALLED_409


# ── Registry hygiene ──────────────────────────────────────────────

class TestRegistry:
    def test_every_marketplace_code_is_known_and_well_formed(self):
        mkt = {v for k, v in vars(ec).items()
               if k.startswith("MKT_") and isinstance(v, str)}
        assert len(mkt) == 14                      # 6 original + 8 added
        assert mkt <= ec.KNOWN_CODES
        assert [c for c in mkt if not re.fullmatch(r"VEL-MKT-[A-Z0-9-]+-\d{3}", c)] == []

    def test_bare_sites_were_left_bare(self):
        """15 uncoded raises — enum validation ("tier must be…", "type must
        be…"), required audit reasons, and service-layer ValueError passthroughs
        where the message is the whole resolution."""
        assert _router_src().count("raise HTTPException") == 15

    def test_the_router_codes_more_than_it_leaves_bare(self):
        """Sanity check on the pass as a whole rather than on any one site."""
        src = _router_src()
        # 67 since 8e(c), 2026-10-01: marketplace sources became operator-only.
        assert src.count("raise coded(") == 67
