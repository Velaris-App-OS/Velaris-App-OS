"""The package format, and the scanner that decides what may enter it.

Design: docs/Future/Done/AI-ERA-ROADMAP.md §F-07.

**The security property is the format, not the manifest.** A package that
declares "weights only" and is then trusted is a promise; a format that can only
express numbers and a closed set of known names cannot carry anything else
whatever the publisher intended. Everything below exists to make the second
sentence true.

Two artefact kinds ship, and both are already numbers-and-text on this platform:

  · `risk_model`  — the HxSentinel local logistic regression. Five coefficients,
    an intercept, five means, five standard deviations, a threshold. The
    scanner requires the feature names to be exactly HxSentinel's own closed
    vocabulary, so the ONLY strings the payload may contain are five names this
    codebase already knows. There is no field a base64 blob could hide in.

  · `signal_pack` — declarative rules whose conditions are safe-expression
    strings. Strings are unavoidable here, so the boundary is the one HxSentinel
    P4 already draws: every expression must classify as CONFORMING under the
    strict grammar, with no hardened fallback. A pack that would not install
    locally cannot be published or imported either.

**What the signature does and does not prove.** The detached Ed25519 signature
binds the bytes to a key. It does NOT bind the key to a publisher — the public
half travels inside the file, so anybody can sign anything. Trusting a publisher
means pinning their `key_id` out of band, exactly as a SAML SP pins an IdP
certificate imported from metadata. The importer surfaces the key id for that
reason and never treats "signed" as "trustworthy".

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
from typing import Any

from velaris_shared.sentinel import schema as sentinel_packs   # validate_manifest, ManifestError
from velaris_shared.sentinel.schema import MODEL_FEATURES

#: The envelope version. A file whose `format` is not this is refused outright
#: rather than parsed hopefully — a future format may mean the opposite thing by
#: the same key.
FORMAT = "velaris.model/1"

KIND_RISK_MODEL = "risk_model"
KIND_SIGNAL_PACK = "signal_pack"
KINDS = frozenset({KIND_RISK_MODEL, KIND_SIGNAL_PACK})

#: Exactly the keys a risk-model payload may carry. Not "at least" — a payload
#: with an extra key is refused, because an extra key is precisely how a
#: smuggled field arrives.
_RISK_KEYS = frozenset({
    "features", "coefficients", "intercept",
    "feature_means", "feature_stds", "threshold",
})

#: Bounds. A coefficient outside this is not a model, it is a payload trying to
#: be something else — and an unbounded float still serialises to a very long
#: string on the other side.
_MAX_ABS = 1e6
#: A zero standard deviation divides by zero in `score_case`, and every case
#: then scores inf or NaN. HxSentinel's trainer already substitutes 1.0 for a
#: constant feature, so a zero here means the file was not written by it.
_MIN_STD = 1e-9

_MAX_BYTES = 512 * 1024


class PackageError(ValueError):
    """The package is malformed, or carries something the format forbids."""


# ── canonical form ───────────────────────────────────────────────────────────


def canonical(payload: Any) -> str:
    """Sorted keys, no whitespace — the same convention the proof chain uses."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def body(*, kind: str, package_id: str, version: int,
         manifest: dict, payload: dict) -> dict:
    """The part of the file the hash and the signature cover.

    `content_sha256` and `signature` are deliberately outside it: a hash cannot
    cover itself, and a file re-signed with a second key must still be the same
    package rather than a new one.
    """
    return {
        "format": FORMAT,
        "kind": kind,
        "package_id": package_id,
        "version": int(version),
        "manifest": manifest,
        "payload": payload,
    }


def content_hash(body_: dict) -> str:
    return hashlib.sha256(canonical(body_).encode("utf-8")).hexdigest()


def eval_fingerprint(kind: str, payload: dict) -> str:
    """Commits to WHAT was measured, not to the measurement.

    Two models are only comparable when they consume the same features in the
    same preprocessing. Without this, a published AUC and a re-measured one
    could disagree for a reason that has nothing to do with either model.
    """
    if kind == KIND_RISK_MODEL:
        return hashlib.sha256(
            canonical({"features": sorted(payload.get("features") or []),
                       "preprocessing": "zscore"}).encode()).hexdigest()
    keys = sorted(str(r.get("key")) for r in (payload.get("rules") or []))
    return hashlib.sha256(canonical({"rules": keys}).encode()).hexdigest()


# ── the scanner ──────────────────────────────────────────────────────────────


def _finite(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PackageError(f"{where} must be a number")
    f = float(value)
    if not math.isfinite(f):
        raise PackageError(f"{where} is not finite")
    if abs(f) > _MAX_ABS:
        raise PackageError(f"{where} is out of range (|x| > {_MAX_ABS:g})")
    return f


def _numeric_map(value: Any, features: tuple[str, ...], where: str) -> dict[str, float]:
    if not isinstance(value, dict):
        raise PackageError(f"{where} must be an object")
    if set(value) != set(features):
        raise PackageError(
            f"{where} must name exactly the declared features "
            f"(missing {sorted(set(features) - set(value))}, "
            f"unexpected {sorted(set(value) - set(features))})")
    return {k: _finite(v, f"{where}.{k}") for k, v in value.items()}


def scan_risk_model(payload: Any) -> dict:
    """Refuse anything that is not five numbers, five names, and two scalars.

    The feature names must be HxSentinel's own, which is what closes the format:
    after this function returns, every string in the payload is one of five
    literals defined in this codebase.
    """
    if not isinstance(payload, dict):
        raise PackageError("payload must be an object")
    extra = set(payload) - _RISK_KEYS
    if extra:
        raise PackageError(f"payload carries fields the format forbids: {sorted(extra)}")
    missing = _RISK_KEYS - set(payload)
    if missing:
        raise PackageError(f"payload is missing {sorted(missing)}")

    features = payload["features"]
    if not isinstance(features, list) or not all(isinstance(f, str) for f in features):
        raise PackageError("payload.features must be a list of names")
    if set(features) != set(MODEL_FEATURES):
        raise PackageError(
            "payload.features is not this platform's risk feature set — the "
            "model was trained under a different one and its scores would not "
            "mean what the card says")

    threshold = _finite(payload["threshold"], "payload.threshold")
    if not 0.0 <= threshold <= 1.0:
        raise PackageError("payload.threshold must be a probability in [0, 1]")

    stds = _numeric_map(payload["feature_stds"], MODEL_FEATURES, "payload.feature_stds")
    for name, value in stds.items():
        if value < _MIN_STD:
            # Not pedantry: score_case divides by this, so a zero here makes
            # every case score inf or NaN on the importer's box.
            raise PackageError(
                f"payload.feature_stds.{name} is zero — scoring would divide by it")

    return {
        "features": list(MODEL_FEATURES),
        "coefficients": _numeric_map(payload["coefficients"], MODEL_FEATURES,
                                     "payload.coefficients"),
        "intercept": _finite(payload["intercept"], "payload.intercept"),
        "feature_means": _numeric_map(payload["feature_means"], MODEL_FEATURES,
                                      "payload.feature_means"),
        "feature_stds": stds,
        "threshold": threshold,
    }


def scan_signal_pack(payload: Any) -> dict:
    """Delegate to HxSentinel's own manifest gate — the boundary already drawn.

    A pack that could not be installed locally must not be publishable either;
    re-implementing the check here would give the marketplace a second, weaker
    opinion about what a safe expression is.
    """
    if not isinstance(payload, dict):
        raise PackageError("payload must be an object")
    try:
        sentinel_packs.validate_manifest(payload)
    except sentinel_packs.ManifestError as exc:
        raise PackageError(str(exc)) from exc
    return payload


def scan(kind: str, payload: Any) -> dict:
    if kind == KIND_RISK_MODEL:
        return scan_risk_model(payload)
    if kind == KIND_SIGNAL_PACK:
        return scan_signal_pack(payload)
    raise PackageError(f"unknown package kind {kind!r}")


# ── signing ──────────────────────────────────────────────────────────────────
#
# The private half is DERIVED from the tenant's HxVault DEK rather than stored.
# Nothing new to protect, nothing new to leak, and no key material in a settings
# blob that admin screens return wholesale. The cost is honest and recorded: a
# DEK rotation produces a different publisher key, which shows up as a new
# `key_id` rather than as a silent change of identity.

_KEY_INFO = b"velaris-model-publisher-key/1"


def derive_keypair(dek: bytes):
    """`(private, public)` Ed25519 objects for this tenant's publisher identity."""
    from cryptography.hazmat.primitives.asymmetric import ed25519

    seed = hashlib.blake2b(dek, digest_size=32, person=_KEY_INFO[:16]).digest()
    private = ed25519.Ed25519PrivateKey.from_private_bytes(seed)
    return private, private.public_key()


def _public_bytes(public) -> bytes:
    from cryptography.hazmat.primitives import serialization

    return public.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw)


def key_id(public) -> str:
    """A short, stable name for a publisher key. Pin THIS, not the file."""
    return hashlib.sha256(_public_bytes(public)).hexdigest()[:32]


def sign(dek: bytes, content_sha256: str) -> dict:
    private, public = derive_keypair(dek)
    return {
        "alg": "ed25519",
        "key_id": key_id(public),
        "public_key": base64.b64encode(_public_bytes(public)).decode(),
        "sig": base64.b64encode(private.sign(content_sha256.encode())).decode(),
    }


def verify_signature(signature: Any, content_sha256: str) -> tuple[bool, str]:
    """`(ok, key_id_or_reason)`.

    An UNSIGNED package is not an error here — it is a fact the caller decides
    about. Refusing every unsigned file would make the feature unusable on the
    day it ships, since no install has ever published one.
    """
    from cryptography.hazmat.primitives.asymmetric import ed25519

    if not signature:
        return False, "unsigned"
    if not isinstance(signature, dict) or signature.get("alg") != "ed25519":
        return False, "unsupported signature algorithm"
    try:
        raw = base64.b64decode(signature["public_key"], validate=True)
        sig = base64.b64decode(signature["sig"], validate=True)
        public = ed25519.Ed25519PublicKey.from_public_bytes(raw)
        public.verify(sig, content_sha256.encode())
    except Exception:                                            # noqa: BLE001
        return False, "signature does not verify"
    claimed = str(signature.get("key_id") or "")
    actual = key_id(public)
    if claimed and claimed != actual:
        # The file names one key and carries another. Nothing breaks, but an
        # operator pinning `key_id` would have pinned a lie.
        return False, "key_id does not match the key in the file"
    return True, actual


# ── the file ─────────────────────────────────────────────────────────────────


def build_file(*, kind: str, package_id: str, version: int, manifest: dict,
               payload: dict, dek: bytes | None) -> dict:
    scanned = scan(kind, payload)
    body_ = body(kind=kind, package_id=package_id, version=version,
                 manifest=manifest, payload=scanned)
    digest = content_hash(body_)
    return {**body_, "content_sha256": digest,
            "signature": sign(dek, digest) if dek else None}


def read_file(raw: bytes | str | dict) -> dict:
    """Parse, re-hash, re-scan. Nothing downstream re-checks any of this.

    Order matters: the hash is recomputed BEFORE the payload is scanned, so a
    file whose contents were edited after signing is refused without the scanner
    ever being asked to interpret them.
    """
    if isinstance(raw, (bytes, str)):
        blob = raw.encode() if isinstance(raw, str) else raw
        if len(blob) > _MAX_BYTES:
            raise PackageError(f"package is larger than {_MAX_BYTES // 1024} KiB")
        try:
            doc = json.loads(blob)
        except Exception as exc:                                 # noqa: BLE001
            raise PackageError("package is not valid JSON") from exc
    else:
        doc = raw
    if not isinstance(doc, dict):
        raise PackageError("package must be an object")
    if doc.get("format") != FORMAT:
        raise PackageError(f"unknown package format {doc.get('format')!r}")
    kind = doc.get("kind")
    if kind not in KINDS:
        raise PackageError(f"unknown package kind {kind!r}")
    package_id = doc.get("package_id")
    if not isinstance(package_id, str) or not (1 <= len(package_id) <= 255):
        raise PackageError("package_id is required (1..255 chars)")
    try:
        version = int(doc.get("version", 1))
    except (TypeError, ValueError) as exc:
        raise PackageError("version must be an integer") from exc
    manifest = doc.get("manifest")
    if not isinstance(manifest, dict):
        raise PackageError("manifest must be an object")

    body_ = body(kind=kind, package_id=package_id, version=version,
                 manifest=manifest, payload=doc.get("payload"))
    digest = content_hash(body_)
    if doc.get("content_sha256") != digest:
        raise PackageError(
            "content hash does not match the package body — it was altered "
            "after it was written")

    signed, detail = verify_signature(doc.get("signature"), digest)
    return {
        "kind": kind,
        "package_id": package_id,
        "version": version,
        "manifest": manifest,
        "payload": scan(kind, doc.get("payload")),
        "content_sha256": digest,
        "signed": signed,
        "signing_key_id": detail if signed else None,
        "signature_reason": None if signed else detail,
        "signature": doc.get("signature"),
    }
