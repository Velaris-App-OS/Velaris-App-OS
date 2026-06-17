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
SUPPORTED_MANIFEST_VERSIONS = {1}     # what this platform can INTERPRET


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
