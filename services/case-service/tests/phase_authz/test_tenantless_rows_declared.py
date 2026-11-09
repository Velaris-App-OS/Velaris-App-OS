"""Every path rule says what a row with NO tenant means (2026-11-04).

The demo-tenant sweep found tenant-less rows — the operator's connectors, old
records, platform security rows — readable or writable by every tenant, because
the path guard let a row without an owner through. Each rule now declares one of:
the default tenant's (`null_is_default`), shared configuration only the operator
changes (`shared_write_operator_only`), or one of the named exceptions below. A
new rule that declares nothing fails here instead of shipping open.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from velaris_shared.tenancy import path_guard as pg

#: Deliberately neither, each with its reason.
EXCEPTIONS = {
    # Copy-on-write defaults (8i/8e): a tenant's edit of a default forks it in
    # the route, and every tenant reads the default.
    "CaseTypeModel", "FormDefinitionModel", "RuleDefinitionModel", "DataModelModel",
    "BusinessCalendarModel", "FormSectionModel", "ReferenceTableModel",
    # Cases: a tenant-less case is governed by VELARIS_CASE_TENANT_STRICT_READS
    # (`case_visible_to`), not by this guard.
    "CaseInstanceModel",
    # A tenant column that cannot be NULL.
    "AccessGroupModel", "CaseAssignmentModel", "CaseSessionModel",
    "ChannelConversationModel", "EnvironmentRegistryModel",
}


def _declared(spec) -> bool:
    if getattr(spec, "null_is_default", False) or getattr(spec, "shared_write_operator_only", False):
        return True
    via = getattr(spec, "via", None)
    return bool(via) and _declared(via[1])


def test_every_route_rule_declares_tenantless_rows():
    undeclared = sorted({
        spec.model.__name__ for _rx, _param, spec in pg.RULES
        if isinstance(spec, pg.Owned) and not _declared(spec)
        and spec.model.__name__ not in EXCEPTIONS})
    assert not undeclared, (
        "these path rules let another tenant reach a row with no tenant; mark "
        f"null_is_default / shared_write_operator_only, or justify in EXCEPTIONS: {undeclared}")
