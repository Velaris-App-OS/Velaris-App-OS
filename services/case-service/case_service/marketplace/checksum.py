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

# Marketplace execution & trust model (Layer-1): a package that declares an
# `execution` block carries code that runs SOMEWHERE — the block pins where.
# Layer 1 (remote, publisher infra) is the only layer installable today;
# Layer 2 (local container) is declarable-later; Layer 3 (in-process) is
# forbidden by decision, permanently.
VALID_DESCRIPTOR_FORMATS = {"openapi", "connector_template"}


def _read_bounded(zf: zipfile.ZipFile, name: str, limit: int, what: str) -> bytes:
    """Stream a zip entry with a hard size cap — a zip-bomb entry must fail
    the check BEFORE it is decompressed into memory, not after."""
    with zf.open(name) as fh:
        data = fh.read(limit + 1)
    if len(data) > limit:
        raise ManifestError(f"{what} exceeds the {limit // 1_000_000} MB limit")
    return data


def _validate_execution(manifest: dict[str, Any], zf: zipfile.ZipFile,
                        names: set[str]) -> None:
    """Validate the Layer-1 `execution` block and load its descriptor.

    Declarative packages (no `execution` block) are untouched. A valid block
    attaches `_descriptor_text` / `_descriptor_sha256` to the manifest
    (underscore = derived, never trusted from the manifest itself).
    """
    execution = manifest.get("execution")
    if execution is None:
        return
    if not isinstance(execution, dict):
        raise ManifestError("execution must be a JSON object")

    layer = execution.get("layer")
    if layer == 3:
        raise ManifestError(
            "execution.layer 3 (in-process) is forbidden: marketplace code "
            "never runs inside the platform process.")
    if layer == 2:
        _validate_layer2(manifest, execution)
        return
    if layer != 1:
        raise ManifestError("execution.layer must be 1 (remote) or 2 (local container)")

    fmt = execution.get("descriptor_format")
    if fmt not in VALID_DESCRIPTOR_FORMATS:
        raise ManifestError(
            f"Invalid execution.descriptor_format '{fmt}'. Must be one of: "
            f"{', '.join(sorted(VALID_DESCRIPTOR_FORMATS))}")

    descriptor = execution.get("descriptor")
    if not descriptor or not isinstance(descriptor, str):
        raise ManifestError("execution.descriptor (path inside the bundle) is required for layer 1")
    if descriptor.startswith("/") or ".." in descriptor:
        raise ManifestError("execution.descriptor must be a plain relative path inside the bundle")
    if descriptor not in names:
        raise ManifestError(f"execution.descriptor '{descriptor}' not found in the bundle")

    # A remote app exists to call out — an empty declaration would make the
    # capability review meaningless.
    if not manifest.get("outbound_domains"):
        raise ManifestError("layer 1 packages must declare at least one outbound_domain")

    scopes = manifest.get("scopes", [])
    if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
        raise ManifestError("scopes must be a JSON array of strings")

    descriptor_bytes = _read_bounded(zf, descriptor, 2_000_000, "execution.descriptor")
    manifest["_descriptor_text"] = descriptor_bytes.decode("utf-8", errors="replace")
    manifest["_descriptor_sha256"] = hashlib.sha256(descriptor_bytes).hexdigest()


IMAGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _validate_layer2(manifest: dict[str, Any], execution: dict[str, Any]) -> None:
    """Layer-2 (local container): the image is the code — its identity is the
    DIGEST, never the tag. Format checks live here (pure); the registry
    allowlist and signature policy are enforced at provisioning time against
    platform config (fail-closed)."""
    image = execution.get("image")
    if not image or not isinstance(image, str):
        raise ManifestError("execution.image (container image reference) is required for layer 2")
    if "@sha256:" in image:
        raise ManifestError(
            "execution.image must not embed a digest — declare it in execution.image_digest")

    digest = execution.get("image_digest")
    if not digest or not IMAGE_DIGEST_RE.match(str(digest)):
        raise ManifestError(
            "execution.image_digest is required for layer 2 and must be 'sha256:<64 hex>' — "
            "a tag is not an identity")

    port = execution.get("port")
    if port is not None and (not isinstance(port, int) or not (1 <= port <= 65535)):
        raise ManifestError("execution.port must be an integer between 1 and 65535")

    env = execution.get("env", {})
    if not isinstance(env, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        raise ManifestError("execution.env must be a JSON object of string values")

    command = execution.get("command")
    if command is not None and (
            not isinstance(command, list) or not all(isinstance(c, str) for c in command)):
        raise ManifestError("execution.command must be a JSON array of strings")

    # Layer 2 defaults to egress-DROP — an empty outbound_domains list is the
    # normal, most-locked-down declaration (unlike layer 1).
    domains = manifest.get("outbound_domains", [])
    if not isinstance(domains, list):
        raise ManifestError("outbound_domains must be a JSON array")

    scopes = manifest.get("scopes", [])
    if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
        raise ManifestError("scopes must be a JSON array of strings")


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
            names = set(zf.namelist())

            try:
                manifest: dict[str, Any] = json.loads(raw)
            except json.JSONDecodeError as e:
                raise ManifestError(f"manifest.json is not valid JSON: {e}")

            _validate_execution(manifest, zf, names)
    except zipfile.BadZipFile:
        raise ManifestError(".hxapp is not a valid zip file")

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
