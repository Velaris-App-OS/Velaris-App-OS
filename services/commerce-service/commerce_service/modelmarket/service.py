"""Publish, import, evaluate, install, and shred a model package.

Design: docs/Future/Done/AI-ERA-ROADMAP.md §F-07.

Three rules hold everywhere in this module, and every function below is shaped
by them:

  1. **Nothing imported ever runs until a person switches it on.** A risk model
     lands as a `pending` row in `case_risk_models` and a signal pack lands
     `enabled = False`. Activation is HxSentinel's own human gate; this feature
     does not add a second one, and it certainly does not skip the first.
  2. **The importer's own measurement is what their screens show.** A published
     AUC is the publisher's arithmetic about the publisher's data. Storing it
     where a local metric belongs would print a stranger's number at somebody
     deciding whether to trust it.
  3. **A refusal is a record, not an exception nobody keeps.** Failed evals and
     blocked publishes leave rows.

**The two tenant identities.** `case_risk_models.tenant_id` is a token — a slug
or a UUID string, whatever the caller had — while these tables carry a real FK
to `tenants.id`. They are not interchangeable, and writing one where the other
belongs is how an installed model becomes invisible to the module that would
score with it. Both are resolved once, at the top of each entry point, and named
`tenant_token` and `tenant_uuid` throughout.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from commerce_service.modelmarket import canary, evaluate as ev
from velaris_shared.modelmarket import packaging
from commerce_service.modelmarket import sentinel_calls
from velaris_shared.sentinel.schema import MODEL_FEATURES
from velaris_shared.db.models import (
    CaseRiskModelModel,
    CaseRiskSignalPackModel,
    ModelPackageEvalModel,
    ModelPackageInstallModel,
    ModelPackageModel,
)

log = logging.getLogger(__name__)

DIRECTION_PUBLISHED = "published"
DIRECTION_IMPORTED = "imported"


class ModelMarketError(ValueError):
    """The operation cannot be performed as asked; the message is for a human."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def tenant_uuid(session: AsyncSession, token: Any) -> uuid.UUID:
    from velaris_shared.tenancy.identity import resolve_tenant

    resolved = await resolve_tenant(session, token)
    if resolved is None:
        raise ModelMarketError(f"no tenant for {token!r}")
    return resolved[0]


# ── publishing ───────────────────────────────────────────────────────────────


async def prepare(session: AsyncSession, *, tenant_token: str, kind: str,
                  source: str) -> dict[str, Any]:
    """Assemble the card and payload for a local artefact, and probe it.

    Separate from `publish` on purpose: an operator has to be able to see what
    the canary says, and what the card would disclose, BEFORE they put their
    name to it. A sign-off nobody could read first is a checkbox.
    """
    payload, manifest, source_ref, trained_on, metrics = await _collect(
        session, tenant_token=tenant_token, kind=kind, source=source)
    scanned = packaging.scan(kind, payload)
    probe = await canary.probe(session, tenant_token=tenant_token, kind=kind,
                               payload=scanned, trained_on=trained_on,
                               metrics=metrics)
    manifest = {
        **manifest,
        "data_classes": probe["data_classes"],
        "disclosure": _disclosure_note(kind, scanned, trained_on),
    }
    return {"kind": kind, "source_ref": source_ref, "manifest": manifest,
            "payload": scanned, "trained_on": trained_on,
            "declared_metrics": metrics, "canary": probe,
            "eval_fingerprint": packaging.eval_fingerprint(kind, scanned)}


def _disclosure_note(kind: str, payload: dict, trained_on: int) -> dict[str, Any]:
    """Say plainly what leaves with the file.

    A model card that lists metrics and intended use and stays quiet about the
    fact that it also ships the publisher's feature distribution is a card that
    reads as more private than the artefact is.
    """
    if kind != packaging.KIND_RISK_MODEL:
        return {"contains": ["declarative rules", "rule text authored by the publisher"],
                "cohort_size": None}
    return {
        "contains": [
            "learned coefficients and intercept",
            "the publisher's per-feature MEAN and STANDARD DEVIATION over the "
            "training cohort — these are aggregate statistics of real cases",
            "the decision threshold",
        ],
        "contains_no": ["case data", "identifiers", "text of any kind"],
        "cohort_size": trained_on,
        "cohort_floor": canary.MIN_COHORT,
    }


async def _collect(session: AsyncSession, *, tenant_token: str, kind: str,
                   source: str) -> tuple[dict, dict, str, int, dict]:
    if kind == packaging.KIND_RISK_MODEL:
        try:
            version = int(source)
        except (TypeError, ValueError) as exc:
            raise ModelMarketError(
                "source must be a risk-model version number") from exc
        row = (await session.execute(
            select(CaseRiskModelModel).where(
                CaseRiskModelModel.tenant_id == tenant_token,
                CaseRiskModelModel.version == version))).scalars().first()
        if row is None:
            raise ModelMarketError(f"no risk model version {version} for this tenant")
        payload = {
            "features": list(MODEL_FEATURES),
            "coefficients": dict(row.coefficients or {}),
            "intercept": float(row.intercept),
            "feature_means": dict(row.feature_means or {}),
            "feature_stds": dict(row.feature_stds or {}),
            "threshold": float(row.threshold),
        }
        manifest = {
            "name": f"risk model v{row.version}",
            "task": "case_risk_scoring",
            "base": "hxsentinel.logistic_regression/1",
            "features": list(MODEL_FEATURES),
            "preprocessing": "zscore",
            "intended_use": (
                "Recommend a risk score for a case. It recommends; it never "
                "decides, and it is not a fraud determination."),
            "limitations": (
                "Trained on one tenant's cases. The coefficients encode that "
                "tenant's mix of work; on a different mix the score may be "
                "uninformative. The importing install re-measures before it "
                "may be installed."),
            "trained_on": int(row.trained_on),
            "metrics": dict(row.metrics or {}),
        }
        return payload, manifest, f"case_risk_models:{row.version}", int(row.trained_on), dict(row.metrics or {})

    if kind == packaging.KIND_SIGNAL_PACK:
        row = (await session.execute(
            select(CaseRiskSignalPackModel).where(
                CaseRiskSignalPackModel.tenant_id == tenant_token,
                CaseRiskSignalPackModel.pack_id == source))).scalars().first()
        if row is None:
            raise ModelMarketError(f"no signal pack {source!r} for this tenant")
        payload = dict(row.manifest or {})
        manifest = {
            "name": row.name,
            "task": "case_risk_signals",
            "base": "hxsentinel.signal_pack/1",
            "vertical": row.vertical,
            "rules": len(payload.get("rules") or []),
            "intended_use": "Add declarative risk signals to HxSentinel.",
            "limitations": (
                "Rules are written against one tenant's vocabulary and "
                "thresholds; fire rates on another tenant's cases are measured "
                "before install, not assumed."),
        }
        return payload, manifest, f"case_risk_signal_packs:{row.pack_id}", 0, {}

    raise ModelMarketError(f"unknown package kind {kind!r}")


async def _dek(session: AsyncSession, tid: uuid.UUID) -> bytes | None:
    """The tenant's DEK, or None when key material is unavailable.

    None means "publish unsigned", which is a stated fact on the row and in the
    API — never a silent downgrade.
    """
    try:
        from velaris_shared.hxvault import keyring

        return keyring.cached_dek(tid) or await keyring.ensure_dek(session, tid)
    except Exception as exc:                                     # noqa: BLE001
        log.info("modelmarket: no publisher key (%s)", exc)
        return None


async def publish(session: AsyncSession, *, tenant_token: str, kind: str,
                  source: str, package_id: str, actor: str,
                  acknowledged: bool) -> ModelPackageModel:
    """Produce a package row — `published`, or `quarantined` when the canary blocks.

    `acknowledged` is the named human accepting the residual disclosure risk.
    It is a required argument rather than a default because the whole point is
    that somebody had to say it.

    Returns the row either way. A blocked canary comes back `quarantined` with
    the failing checks in `status_reason`; the caller decides what that means
    on the wire.
    """
    if not acknowledged:
        raise ModelMarketError(
            "publishing requires an explicit acknowledgement of the disclosure "
            "this package carries — see the card's `disclosure` section")
    prepared = await prepare(session, tenant_token=tenant_token, kind=kind, source=source)
    probe = prepared["canary"]

    tid = await tenant_uuid(session, tenant_token)
    version = await _next_version(session, tid, package_id)
    body = packaging.body(kind=kind, package_id=package_id, version=version,
                          manifest=prepared["manifest"], payload=prepared["payload"])
    digest = packaging.content_hash(body)

    row = ModelPackageModel(
        id=uuid.uuid4(), tenant_id=tid, direction=DIRECTION_PUBLISHED, kind=kind,
        package_id=package_id, version=version, content_sha256=digest,
        manifest=prepared["manifest"], payload=prepared["payload"],
        publisher_ref=str(tenant_token), publisher_name=str(tenant_token),
        source_ref=prepared["source_ref"], trained_on=prepared["trained_on"],
        declared_metrics=prepared["declared_metrics"],
        eval_fingerprint=prepared["eval_fingerprint"],
        canary_verdict=probe["verdict"], canary_detail=probe,
        data_classes=probe["data_classes"],
        special_category=bool(probe["special_category"]),
        created_by=actor)

    if probe["verdict"] != "pass":
        # The refusal is KEPT, and it is returned rather than raised. Raising
        # here would unwind the transaction that holds it, and "why can we not
        # share this" would have no answer the moment the screen was closed.
        # The caller reads `status` and reports; it is not an exception path.
        row.status = "quarantined"
        row.status_reason = "; ".join(
            c["detail"] for c in probe["checks"] if c["blocking"] and not c["ok"])
    else:
        row.status = "published"
        row.signed_off_by = actor
        row.signed_off_at = _utcnow()
        # Signed HERE, not at download. The row is what every screen and every
        # listing reads, and a package that says "unsigned" until somebody
        # happens to export it tells the operator something untrue about
        # itself in the meantime.
        dek = await _dek(session, tid)
        if dek is not None:
            signature = packaging.sign(dek, digest)
            row.signature = json.dumps(signature)
            row.signing_key_id = signature["key_id"]
    session.add(row)
    await session.flush()
    return row


async def _next_version(session: AsyncSession, tid: uuid.UUID, package_id: str) -> int:
    last = (await session.execute(
        select(ModelPackageModel.version).where(
            ModelPackageModel.tenant_id == tid,
            ModelPackageModel.direction == DIRECTION_PUBLISHED,
            ModelPackageModel.package_id == package_id)
        .order_by(ModelPackageModel.version.desc()).limit(1))).scalar_one_or_none()
    return int(last or 0) + 1


async def export_file(session: AsyncSession, row: ModelPackageModel) -> dict:
    """The bytes that leave.

    The signature is recomputed rather than read off the row: it is derived
    from the DEK, so the two always agree, and a file must never be able to
    ship a signature that the row it came from does not also hold.
    """
    dek = await _dek(session, row.tenant_id)
    doc = packaging.build_file(
        kind=row.kind, package_id=row.package_id, version=row.version,
        manifest=row.manifest, payload=row.payload, dek=dek)
    if doc["content_sha256"] != row.content_sha256:
        # The row and the file must be the same package. A mismatch means one
        # of them was edited, and shipping either would be shipping something
        # nobody reviewed.
        raise ModelMarketError(
            "the stored package no longer hashes to its recorded content id")
    if doc.get("signature"):
        row.signature = json.dumps(doc["signature"])
        row.signing_key_id = doc["signature"]["key_id"]
    return doc


# ── importing ────────────────────────────────────────────────────────────────


async def import_file(session: AsyncSession, *, tenant_token: str, raw: bytes | str,
                      actor: str) -> ModelPackageModel:
    """Take a package in. It is stored, not installed, and nothing runs.

    Re-importing the same bytes returns the existing row: content-addressed
    means the same file is the same package, not a second opinion about it.
    """
    parsed = packaging.read_file(raw)
    tid = await tenant_uuid(session, tenant_token)

    existing = (await session.execute(
        select(ModelPackageModel).where(
            ModelPackageModel.tenant_id == tid,
            ModelPackageModel.direction == DIRECTION_IMPORTED,
            ModelPackageModel.content_sha256 == parsed["content_sha256"]))
    ).scalars().first()
    if existing is not None:
        return existing

    manifest = parsed["manifest"]
    row = ModelPackageModel(
        id=uuid.uuid4(), tenant_id=tid, direction=DIRECTION_IMPORTED,
        kind=parsed["kind"], package_id=parsed["package_id"],
        version=parsed["version"], content_sha256=parsed["content_sha256"],
        manifest=manifest, payload=parsed["payload"],
        signature=json.dumps(parsed["signature"]) if parsed["signature"] else None,
        signing_key_id=parsed["signing_key_id"],
        publisher_ref=str(manifest.get("publisher_ref") or "") or None,
        publisher_name=str(manifest.get("publisher_name") or "") or None,
        source_ref=None,
        trained_on=int(manifest.get("trained_on") or 0) or None,
        declared_metrics=manifest.get("metrics"),
        eval_fingerprint=packaging.eval_fingerprint(parsed["kind"], parsed["payload"]),
        # The publisher's canary ran on the publisher's box, about the
        # publisher's data. It is not evidence here and is not copied in.
        canary_verdict="not_run",
        data_classes=manifest.get("data_classes"),
        status="published", created_by=actor)
    session.add(row)
    await session.flush()
    return row


async def run_eval(session: AsyncSession, *, tenant_token: str,
                   package: ModelPackageModel, actor: str,
                   tolerance: float = ev.DEFAULT_TOLERANCE) -> ModelPackageEvalModel:
    result = await ev.evaluate(
        session, tenant_token=tenant_token, tenant_uuid=package.tenant_id,
        kind=package.kind, payload=package.payload,
        declared=package.declared_metrics, tolerance=tolerance)
    row = ModelPackageEvalModel(
        id=uuid.uuid4(), package_ref=package.id, tenant_id=package.tenant_id,
        holdout_size=int(result["holdout_size"]), positives=int(result["positives"]),
        negatives=int(result["negatives"]), declared=result["declared"],
        measured=result["measured"], tolerance=result["tolerance"],
        verdict=result["verdict"], reason=result["reason"], evaluated_by=actor)
    session.add(row)
    await session.flush()
    return row


# ── installing ───────────────────────────────────────────────────────────────


async def install(session: AsyncSession, *, tenant_token: str,
                  package: ModelPackageModel, actor: str) -> ModelPackageInstallModel:
    """Place an imported package into the runtime, INACTIVE.

    The gate is the eval, and it must have passed on this tenant's own data
    within this call's knowledge — an eval that was never run, or that came
    back `fail` or `insufficient_data`, refuses.
    """
    if package.direction != DIRECTION_IMPORTED:
        raise ModelMarketError("only an imported package can be installed")
    if package.status != "published":
        raise ModelMarketError(f"package is {package.status}")

    latest = (await session.execute(
        select(ModelPackageEvalModel)
        .where(ModelPackageEvalModel.package_ref == package.id)
        .order_by(ModelPackageEvalModel.evaluated_at.desc()).limit(1))).scalars().first()
    if latest is None:
        raise ModelMarketError(
            "this package has not been evaluated on this tenant's data yet")
    if latest.verdict != ev.VERDICT_PASS:
        raise ModelMarketError(
            f"the evaluation came back {latest.verdict}: {latest.reason}")

    existing = (await session.execute(
        select(ModelPackageInstallModel).where(
            ModelPackageInstallModel.tenant_id == package.tenant_id,
            ModelPackageInstallModel.package_ref == package.id))).scalars().first()
    if existing is not None and existing.status in ("staged", "activated"):
        raise ModelMarketError("this package is already installed")

    if package.kind == packaging.KIND_RISK_MODEL:
        target_ref = await _install_risk_model(
            session, tenant_token=tenant_token, package=package, eval_row=latest)
    else:
        target_ref = await _install_signal_pack(
            session, tenant_token=tenant_token, package=package, actor=actor)

    row = existing or ModelPackageInstallModel(
        id=uuid.uuid4(), package_ref=package.id, tenant_id=package.tenant_id)
    row.model_origin = "marketplace"
    row.target_kind = package.kind
    row.target_ref = target_ref
    row.eval_ref = latest.id
    row.status = "staged"
    row.installed_by = actor
    row.installed_at = _utcnow()
    row.uninstalled_at = None
    session.add(row)
    await session.flush()
    return row


async def _install_risk_model(session: AsyncSession, *, tenant_token: str,
                              package: ModelPackageModel,
                              eval_row: ModelPackageEvalModel) -> str:
    payload = package.payload
    last = (await session.execute(
        select(CaseRiskModelModel.version)
        .where(CaseRiskModelModel.tenant_id == tenant_token)
        .order_by(CaseRiskModelModel.version.desc()).limit(1))).scalar_one_or_none()
    row = CaseRiskModelModel(
        id=uuid.uuid4(), tenant_id=tenant_token, version=int(last or 0) + 1,
        coefficients=payload["coefficients"], intercept=payload["intercept"],
        feature_means=payload["feature_means"], feature_stds=payload["feature_stds"],
        threshold=payload["threshold"],
        # The cohort belongs to the publisher, and saying otherwise would put
        # somebody else's sample size on this tenant's screen as if it were
        # theirs. Zero is the honest local figure: nothing here trained it.
        trained_on=0,
        # The MEASURED numbers first, because they are the ones about this
        # tenant. What the card claimed is kept beside them, labelled.
        metrics={
            **(eval_row.measured or {}),
            "origin": "marketplace",
            "package": package.package_id,
            "package_version": package.version,
            "content_sha256": package.content_sha256,
            "publisher_trained_on": package.trained_on,
            "declared": eval_row.declared,
        },
        # Pending, always. Activation is HxSentinel's human gate and this
        # feature does not reach past it.
        status="pending")
    session.add(row)
    await session.flush()
    return f"case_risk_models:{row.id}"


async def _install_signal_pack(session: AsyncSession, *, tenant_token: str,
                               package: ModelPackageModel, actor: str) -> str:
    # `install_pack` enables what it installs — right for a pack an operator
    # chose from the catalogue, wrong for a stranger's rules arriving in a
    # file. It is installed disabled, deliberately, and the same promise the
    # risk model keeps: nothing imported fires until somebody switches it on.
    # (case-service installs it — HxSentinel's table — over the internal API.)
    row_id = await sentinel_calls.install_signal_pack(
        session, tenant=tenant_token, manifest=package.payload, installed_by=actor,
        enabled=False)
    return f"case_risk_signal_packs:{row_id}"


async def record_activation(session: AsyncSession, *, tenant_token: str,
                            target_ref: str, actor: str) -> ModelPackageInstallModel | None:
    """Mark an install activated. Called by whoever performed the activation.

    This never activates anything itself — it records that HxSentinel's own
    gate was passed, so `model_package_installs` can answer "is a stranger's
    model scoring here" without a second source of truth about it.
    """
    row = (await session.execute(
        select(ModelPackageInstallModel).where(
            ModelPackageInstallModel.target_ref == target_ref))).scalars().first()
    if row is None:
        return None
    row.status = "activated"
    row.activated_by = actor
    row.activated_at = _utcnow()
    await session.flush()
    return row


# ── uninstalling ─────────────────────────────────────────────────────────────


async def uninstall(session: AsyncSession, *, tenant_token: str,
                    install_row: ModelPackageInstallModel, shred: bool,
                    actor: str) -> ModelPackageInstallModel:
    """Remove the artefact from the runtime; optionally destroy the weights.

    The install row always survives. That a tenant once ran a stranger's model
    is a fact an auditor may need long after somebody pressed delete, and it is
    not the sort of fact a delete button gets to remove.
    """
    kind, _, ref = (install_row.target_ref or "").partition(":")
    if kind == "case_risk_models" and ref:
        row = await session.get(CaseRiskModelModel, uuid.UUID(ref))
        if row is not None:
            # Archived rather than deleted: a score already attached to a case
            # names this version, and deleting it orphans that explanation.
            row.status = "archived"
    elif kind == "case_risk_signal_packs" and ref:
        row = await session.get(CaseRiskSignalPackModel, uuid.UUID(ref))
        if row is not None:
            row.enabled = False

    install_row.status = "uninstalled"
    install_row.uninstalled_at = _utcnow()

    if shred:
        package = await session.get(ModelPackageModel, install_row.package_ref)
        if package is not None:
            package.payload = {"shredded": True}
            package.status = "revoked"
            package.status_reason = "uninstalled with delete"
        install_row.status = "shredded"
        install_row.shredded_at = _utcnow()
        install_row.shred_reason = "uninstall_with_delete"
    await session.flush()
    return install_row
