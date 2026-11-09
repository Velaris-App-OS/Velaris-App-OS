"""Package manifest schema versioning (Architecture Report §6.3, roadmap #14).

manifest_version 1 is the CURRENT, LOCKED format: permissions are expressed
as RBAC role/access-group declarations. It is frozen — v2 (ReBAC relationship
tuples + the install-time translation layer) will be defined by the HxGuard
workstream, never by mutating v1.

Every manifest without the field is v1 by definition (the field did not
exist before this lock), so all existing packages are grandfathered.

This is the single chokepoint: marketplace .hxapp validation and the app
packager both go through it. HxDeploy bundles version a different schema
(bundle_schema_version, checked in hxdeploy/packager.apply_bundle).
"""
from __future__ import annotations

from typing import Any

MANIFEST_VERSION = 1                  # what this platform WRITES
SUPPORTED_MANIFEST_VERSIONS = {1, 2}  # what this platform can INTERPRET

#: v2 relations a package may declare. Deliberately the subset HxGuard's ReBAC
#: backend actually evaluates — a manifest cannot invent a relation the PDP
#: would silently never satisfy.
VALID_V2_RELATIONS = {"assignee", "editor", "viewer"}

#: v2 object types a package may declare permissions over.
VALID_V2_OBJECT_TYPES = {"case", "case_type", "form", "report"}

#: v2 subject types — who the relation is granted to.
VALID_V2_SUBJECT_TYPES = {"user", "access_group", "package"}

#: How a v1 RBAC role maps onto a v2 relation. This is the §6.3 translation
#: layer: v1 is frozen, so v1 packages are read THROUGH this rather than being
#: rewritten, and a v1 package keeps installing forever.
V1_ROLE_TO_V2_RELATION = {
    "admin":       "editor",
    "manager":     "editor",
    "case_worker": "assignee",
    "designer":    "editor",
    "viewer":      "viewer",
    "auditor":     "viewer",
}

#: Anything not in the map is read as the least privilege v2 can express.
#: Guessing upward on an unknown role would silently widen a package's reach.
V1_UNKNOWN_ROLE_RELATION = "viewer"


class ManifestVersionError(Exception):
    pass


def check_manifest_version(manifest: dict[str, Any]) -> int:
    """Return the manifest's schema version, raising on unsupported ones.

    Missing field → 1 (grandfathered). Non-integer or unsupported values are
    rejected with a message naming both sides, so a v2 package on a v1
    platform fails loudly instead of importing permissions it cannot
    correctly interpret.
    """
    raw = manifest.get("manifest_version", MANIFEST_VERSION)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ManifestVersionError(
            f"manifest_version must be an integer, got {raw!r}"
        )
    if raw not in SUPPORTED_MANIFEST_VERSIONS:
        raise ManifestVersionError(
            f"This Velaris version supports manifest_version "
            f"{sorted(SUPPORTED_MANIFEST_VERSIONS)}; the package declares {raw}. "
            "Upgrade the platform or republish the package in a supported format."
        )
    return raw


def validate_v2_permissions(manifest: dict[str, Any]) -> list[dict[str, str]]:
    """Validate a v2 manifest's declarative permission block.

    v2 expresses permissions as **relationship tuples** rather than RBAC role
    names — the format ReBAC needs and the one v1 could not express. Each entry
    is ``{relation, object_type, subject_type}``; concrete subject ids are
    bound at install, never baked into a published package.

    Rejects unknown relations, object types and subject types rather than
    ignoring them. A permission the PDP will never evaluate is worse than a
    rejected one: the package appears to have been granted something it has
    not, and nobody finds out until it silently fails.
    """
    raw = manifest.get("permissions", [])
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise ManifestVersionError("manifest_version 2 'permissions' must be a list")

    out: list[dict[str, str]] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ManifestVersionError(f"permissions[{i}] must be an object")
        relation = str(entry.get("relation") or "")
        object_type = str(entry.get("object_type") or "")
        subject_type = str(entry.get("subject_type") or "user")

        if relation not in VALID_V2_RELATIONS:
            raise ManifestVersionError(
                f"permissions[{i}].relation {relation!r} is not one of "
                f"{sorted(VALID_V2_RELATIONS)}")
        if object_type not in VALID_V2_OBJECT_TYPES:
            raise ManifestVersionError(
                f"permissions[{i}].object_type {object_type!r} is not one of "
                f"{sorted(VALID_V2_OBJECT_TYPES)}")
        if subject_type not in VALID_V2_SUBJECT_TYPES:
            raise ManifestVersionError(
                f"permissions[{i}].subject_type {subject_type!r} is not one of "
                f"{sorted(VALID_V2_SUBJECT_TYPES)}")

        out.append({
            "relation": relation,
            "object_type": object_type,
            "subject_type": subject_type,
        })
    return out


def translate_v1_permissions(manifest: dict[str, Any]) -> list[dict[str, str]]:
    """Read a v1 manifest's RBAC declarations as v2 tuples (§6.3).

    v1 is frozen, so nothing rewrites the package: this reads it through the
    translation instead, which is what keeps every existing package installable
    after v2 exists. An unknown role degrades to `viewer` — guessing upward
    would silently widen a package's reach on a name nobody recognised.
    """
    roles = manifest.get("required_roles") or manifest.get("roles") or []
    if isinstance(roles, str):
        roles = [roles]
    if not isinstance(roles, list):
        return []

    seen: set[tuple[str, str, str]] = set()
    out: list[dict[str, str]] = []
    for role in roles:
        relation = V1_ROLE_TO_V2_RELATION.get(
            str(role).strip().lower(), V1_UNKNOWN_ROLE_RELATION,
        )
        key = (relation, "case", "user")
        if key in seen:
            continue
        seen.add(key)
        out.append({"relation": relation, "object_type": "case", "subject_type": "user"})
    return out


def effective_permissions(manifest: dict[str, Any]) -> list[dict[str, str]]:
    """Permissions as v2 tuples, whatever version the manifest declares.

    The single place the rest of the platform should ask, so no caller has to
    branch on manifest version to find out what a package may do.
    """
    version = check_manifest_version(manifest)
    if version >= 2:
        return validate_v2_permissions(manifest)
    return translate_v1_permissions(manifest)


# ── Enforcement (#29) ─────────────────────────────────────────────────────────
#
# Validating a declaration and enforcing it are different jobs, and until
# 2026-08-24 only the first one existed: `effective_permissions` resolved the
# block at install and the result was attached to an in-memory dict that nobody
# persisted and nothing read. A package could declare `viewer` on `case` and
# still reach everything its installer's capability grant allowed.
#
# The declaration is a **ceiling**. The operator's grant decides what is on
# offer; the manifest decides what the package is allowed to accept. A call
# needs both, and neither can widen the other.

#: Which declared relations imply the right to READ an object. All three do —
#: an assignee and an editor can obviously see what they act on. Listed rather
#: than assumed so that adding a write-only relation later cannot silently
#: become a read.
READ_RELATIONS = {"assignee", "editor", "viewer"}

#: Relations that imply the right to CHANGE an object. The broker is read-only
#: today, so nothing consults this yet; it is here so the write surface, when it
#: arrives, cannot be built without deciding the question.
WRITE_RELATIONS = {"assignee", "editor"}


def declares_access(
    permissions: list[dict[str, str]] | None,
    object_type: str,
    *,
    write: bool = False,
) -> bool:
    """Does this package's own manifest permit touching `object_type`?

    `None` means the install predates enforcement: there is no declaration to
    check, so this is not the layer that refuses. Say yes and let the
    capability grant decide, exactly as it did before. An upgrade must not
    revoke access from packages that are working today.

    An empty list is NOT the same thing. It means the package was read and
    declared nothing, which is a real answer: it may touch nothing.
    """
    if permissions is None:
        return True
    wanted = WRITE_RELATIONS if write else READ_RELATIONS
    return any(
        entry.get("object_type") == object_type
        and entry.get("relation") in wanted
        for entry in permissions
    )
