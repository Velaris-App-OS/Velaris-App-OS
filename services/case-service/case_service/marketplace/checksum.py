"""SHA256 checksum verification for .hxapp bundles.

Called twice:
  1. At download time — before the bundle enters the sandbox container.
  2. At promotion time — before writing to marketplace_installs (tamper check).
"""
from __future__ import annotations

import hashlib
import json
import zipfile
import io
import re
import semver
from typing import Any


class ChecksumError(Exception):
    pass


class ManifestError(Exception):
    pass


REQUIRED_FIELDS = {"id", "name", "type", "publisher", "publisher_tier", "outbound_domains"}
VALID_TYPES = {"connector", "case_template", "module", "nlp_pack", "portal_theme", "bundle"}
VALID_TIERS = {"official", "community"}
# Roadmap #15: runtime co-existence split. "python" is the default and the
# compatibility fallback; "wasm" is declarable NOW (the schema is locked
# before HxSandbox ships) but rejected at install time until HxSandbox (#17)
# can actually execute it.
VALID_RUNTIMES = {"python", "wasm"}
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+")


def verify_checksum(content: bytes, expected_sha256: str) -> None:
    """Raise ChecksumError if the content doesn't match the declared checksum."""
    actual = hashlib.sha256(content).hexdigest()
    if actual != expected_sha256.lower():
        raise ChecksumError(
            f"Checksum mismatch — declared {expected_sha256}, actual {actual}. "
            "The package may have been tampered with."
        )


def parse_and_validate_manifest(hxapp_bytes: bytes) -> dict[str, Any]:
    """Extract and validate manifest.json from a .hxapp zip.

    Returns the parsed manifest dict on success.
    Raises ManifestError with a descriptive message on any failure.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(hxapp_bytes)) as zf:
            if "manifest.json" not in zf.namelist():
                raise ManifestError("manifest.json not found in .hxapp bundle")
            raw = zf.read("manifest.json")
    except zipfile.BadZipFile:
        raise ManifestError(".hxapp is not a valid zip file")

    try:
        manifest: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ManifestError(f"manifest.json is not valid JSON: {e}")

    # Required fields
    missing = REQUIRED_FIELDS - set(manifest.keys())
    if missing:
        raise ManifestError(f"manifest.json missing required fields: {', '.join(sorted(missing))}")

    # Type validation
    if manifest.get("type") not in VALID_TYPES:
        raise ManifestError(f"Invalid package type '{manifest.get('type')}'. Must be one of: {', '.join(VALID_TYPES)}")

    # Publisher tier
    if manifest.get("publisher_tier") not in VALID_TIERS:
        raise ManifestError(f"Invalid publisher_tier '{manifest.get('publisher_tier')}'")

    # outbound_domains must be a list
    domains = manifest.get("outbound_domains", [])
    if not isinstance(domains, list):
        raise ManifestError("outbound_domains must be a JSON array")

    # Block internal IP ranges from outbound_domains declaration
    BLOCKED_PREFIXES = ("10.", "172.16.", "192.168.", "127.", "169.254.", "::1")
    for domain in domains:
        if any(domain.startswith(p) for p in BLOCKED_PREFIXES):
            raise ManifestError(
                f"outbound_domains contains a blocked internal address: {domain}. "
                "Packages cannot declare internal IP ranges as allowed destinations."
            )

    # Version semver check
    version = manifest.get("version", "")
    if not SEMVER_RE.match(str(version)):
        raise ManifestError(f"version '{version}' is not valid semver (must be X.Y.Z)")

    # Manifest schema version (roadmap #14): v1 = locked RBAC format.
    # Missing field = v1 (grandfathered); unsupported versions fail loudly.
    from case_service.marketplace.manifest_version import (
        ManifestVersionError, check_manifest_version,
    )
    try:
        check_manifest_version(manifest)
    except ManifestVersionError as e:
        raise ManifestError(str(e))

    # Runtime split (roadmap #15): missing = "python" (grandfathered).
    runtime = manifest.get("runtime", "python")
    if runtime not in VALID_RUNTIMES:
        raise ManifestError(
            f"Invalid runtime '{runtime}'. Must be one of: {', '.join(sorted(VALID_RUNTIMES))}"
        )

    # ai_dependency declaration (roadmap #11/#22, §4.4): optional, validated
    # when present — none | optional | required.
    ai_dep = manifest.get("ai_dependency")
    if ai_dep is not None and ai_dep not in ("none", "optional", "required"):
        raise ManifestError(
            f"Invalid ai_dependency '{ai_dep}'. Must be one of: none, optional, required"
        )

    return manifest
