"""#29 — a package's declared permissions are a ceiling, not documentation.

**What was wrong.** `manifest_version.effective_permissions` resolved a v2
manifest's permission tuples at install, validated them strictly — rejecting
relations the PDP could never evaluate — and attached the result to an
in-memory dict. Nothing persisted it and nothing read it. A package could
declare `viewer` on `case` and still reach whatever the installing operator's
capability grant allowed, because only the grant was ever checked.

**The shape of the fix.** Two independent questions, and a call needs both to
say yes:

* the capability grant — what the operator agreed to expose,
* the manifest — what the package said it needed.

The manifest can only narrow. The distinction that makes it safe to ship is
`None` vs `[]`: an install from before enforcement has *no recorded*
declaration and keeps working on its grant alone, while a package that was read
and declared nothing may touch nothing.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import pytest

from velaris_shared.apps.manifest_version import (
    READ_RELATIONS,
    VALID_V2_RELATIONS,
    WRITE_RELATIONS,
    declares_access,
    effective_permissions,
)


class TestTheCeilingItself:

    def test_an_unrecorded_declaration_does_not_refuse(self):
        """NULL means "installed before this existed", not "declared nothing".

        Treating the two the same would revoke access from every package that
        is working today, on an upgrade nobody asked for.
        """
        assert declares_access(None, "case") is True

    def test_declaring_nothing_refuses_everything(self):
        """An empty list is a real answer, and it is 'no'."""
        assert declares_access([], "case") is False

    def test_a_declared_object_type_is_allowed(self):
        perms = [{"relation": "viewer", "object_type": "case", "subject_type": "package"}]
        assert declares_access(perms, "case") is True

    def test_an_undeclared_object_type_is_refused(self):
        """The defect in one line: declaring `report` never meant `case`."""
        perms = [{"relation": "editor", "object_type": "report", "subject_type": "package"}]
        assert declares_access(perms, "case") is False

    @pytest.mark.parametrize("relation", sorted(READ_RELATIONS))
    def test_every_read_relation_permits_reading(self, relation):
        perms = [{"relation": relation, "object_type": "case", "subject_type": "package"}]
        assert declares_access(perms, "case") is True

    def test_viewer_cannot_write(self):
        """The broker is read-only today; this is what stops the write surface
        being built on the assumption that any declaration is enough."""
        perms = [{"relation": "viewer", "object_type": "case", "subject_type": "package"}]
        assert declares_access(perms, "case", write=True) is False

    @pytest.mark.parametrize("relation", sorted(WRITE_RELATIONS))
    def test_write_relations_permit_writing(self, relation):
        perms = [{"relation": relation, "object_type": "case", "subject_type": "package"}]
        assert declares_access(perms, "case", write=True) is True

    def test_write_relations_are_a_subset_of_read_relations(self):
        """Anything that may change an object may obviously see it. If these
        ever diverge, a write would be permitted where a read is not."""
        assert WRITE_RELATIONS <= READ_RELATIONS

    def test_the_vocabulary_cannot_drift_past_the_pdp(self):
        """Relations the manifest may declare and relations enforcement knows
        about must be the same set — a declared relation nobody evaluates looks
        granted and silently is not."""
        assert READ_RELATIONS == VALID_V2_RELATIONS


class TestWhatGetsPersisted:

    def test_a_v2_manifest_resolves_to_its_tuples(self):
        manifest = {
            "manifest_version": 2,
            "permissions": [
                {"relation": "viewer", "object_type": "case", "subject_type": "package"},
            ],
        }
        assert declares_access(effective_permissions(manifest), "case") is True

    def test_a_v2_manifest_that_declares_only_reports_cannot_read_cases(self):
        manifest = {
            "manifest_version": 2,
            "permissions": [
                {"relation": "editor", "object_type": "report", "subject_type": "package"},
            ],
        }
        perms = effective_permissions(manifest)
        assert declares_access(perms, "report") is True
        assert declares_access(perms, "case") is False

    def test_a_v1_manifest_is_read_through_the_translation(self):
        """v1 is frozen and has no vocabulary for this, so it is translated
        rather than rewritten — a v1 package keeps installing forever."""
        manifest = {"manifest_version": 1, "required_roles": ["viewer"]}
        perms = effective_permissions(manifest)
        assert isinstance(perms, list)


class TestTheBrokerIsWiredToIt:
    """The broker is the ONLY data path a package container has; enforcing
    anywhere else would be enforcing nowhere."""

    def test_both_broker_routes_consult_the_manifest(self):
        import inspect

        from case_service.api.routers import broker

        for fn in (broker.broker_list_cases, broker.broker_get_case):
            src = inspect.getsource(fn)
            assert "_manifest_allows_cases" in src, (
                f"{fn.__name__} does not check the package's declaration, so a "
                "package can reach cases it never declared.")

    def test_the_single_case_route_checks_before_parsing_the_id(self):
        """Otherwise a package outside its declaration could tell a malformed
        id from a real one."""
        import inspect

        from case_service.api.routers import broker

        src = inspect.getsource(broker.broker_get_case)
        assert src.index("_manifest_allows_cases") < src.index("uuid.UUID(case_id)")

    def test_the_refusal_is_logged_like_every_other_block(self):
        import inspect

        from case_service.api.routers import broker

        src = inspect.getsource(broker.broker_list_cases)
        head = src[:src.index("_scoped_case_types")]
        assert '"blocked"' in head, (
            "a manifest refusal must reach marketplace_network_log like every "
            "other broker block, or the one denial nobody can audit is the new "
            "one.")


class TestItIsPersistedAtInstall:

    def test_the_install_row_can_hold_a_declaration(self):
        from velaris_shared.db.models import (
            MarketplaceInstallModel,
            MarketplaceWorkspaceItemModel,
        )

        for model in (MarketplaceInstallModel, MarketplaceWorkspaceItemModel):
            assert hasattr(model, "declared_permissions"), (
                f"{model.__name__} cannot carry the declaration, so the broker "
                "has nothing to enforce.")
            assert model.__table__.c.declared_permissions.nullable, (
                "NULL is load-bearing: it distinguishes an install from before "
                "enforcement from a package that declared nothing.")

    def test_install_captures_it_from_the_verified_manifest(self):
        """Not from the registry cache — only the verified bundle is trusted."""
        import pathlib

        # In commerce-service since R5e step 7.
        src = (pathlib.Path(__file__).resolve().parents[3] / "commerce-service"
               / "commerce_service" / "api" / "routers" / "marketplace.py").read_text()
        assert 'declared_permissions = manifest.get("_permissions")' in src
        assert "declared_permissions=declared_permissions," in src
        assert "declared_permissions=item.declared_permissions," in src, (
            "the ceiling must travel from the workspace item to the install "
            "row — approving an install is not the moment to widen it.")
