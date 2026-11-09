"""F-07 — the sovereign model marketplace API.

The exchange has four moments, and each one is a route because each one is a
decision somebody has to be able to point at afterwards:

* *What could we share, and what would sharing it disclose?* — `POST /prepare`
  writes nothing and is the screen a publisher reads before signing.
* *Publish it.* — `POST /publish`, which requires a named acknowledgement, and
  which records a REFUSAL as a row when the canary blocks.
* *Is this stranger's model any good on our data?* — `POST /{id}/evaluate`, run
  against this tenant's own labelled cases, none of which the model has seen.
* *Install it.* — `POST /{id}/install`, gated on that evaluation having passed,
  and landing INACTIVE.

Three access decisions worth stating:

* **Publishing and installing are different powers.** Publishing sends this
  tenant's learned aggregates outside; installing accepts a stranger's. An
  operator may reasonably grant one and not the other, so they are separate
  actions rather than one `models.manage`.
* **Reading is separate from both**, because the interesting read — what left
  here, and what we took in — is exactly the question an auditor asks and has
  no business implying the power to do either.
* **Nothing here activates anything.** There is no route that makes an imported
  model score a case; that gate is HxSentinel's and stays there.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared import hxguard
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.db.models import (
    CaseRiskModelModel,
    CaseRiskSignalPackModel,
    ModelPackageEvalModel,
    ModelPackageInstallModel,
    ModelPackageModel,
)
from velaris_shared.db.session import get_session
from velaris_shared.middleware.endpoint_rate_limit import rate_limit

from velaris_shared.core.timestamps import iso_utc
from commerce_service.modelmarket import evaluate as ev
from velaris_shared.modelmarket import packaging
from commerce_service.modelmarket import service

log = logging.getLogger(__name__)

router = APIRouter(prefix="/model-market", tags=["model-market"])

#: `prepare` and `evaluate` both walk the tenant's whole labelled caseload and
#: score it. They are the expensive calls here.
_probe_rl = rate_limit(max_calls=10, window_seconds=60, name="model-market-probe")
_publish_rl = rate_limit(max_calls=10, window_seconds=60, name="model-market-publish")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PrepareIn(_Strict):
    kind: str = Field(min_length=1, max_length=20)
    #: A risk-model version number, or a signal pack's `pack_id`.
    source: str = Field(min_length=1, max_length=128)


class PublishIn(PrepareIn):
    package_id: str = Field(min_length=3, max_length=255)
    #: The named human accepting the residual disclosure. Never defaulted.
    acknowledged: bool = False


class ImportIn(_Strict):
    #: The package file's contents. Bounded here as well as in the scanner:
    #: a body this endpoint will not read is cheaper to refuse than to parse.
    package: str = Field(min_length=2, max_length=512 * 1024)


class EvaluateIn(_Strict):
    tolerance: float = Field(default=ev.DEFAULT_TOLERANCE, ge=0.0, le=1.0)


class UninstallIn(_Strict):
    #: Uninstall-with-DELETE. Destroys the stored weights; the install record
    #: survives, because that a stranger's model once ran here is not a fact a
    #: delete button gets to remove.
    shred: bool = False


def _token(user: AuthenticatedUser) -> str:
    return user.tenant_id or "default"


async def _tenant(session: AsyncSession, user: AuthenticatedUser) -> uuid.UUID:
    try:
        return await service.tenant_uuid(session, _token(user))
    except service.ModelMarketError as exc:
        raise HTTPException(403, "VEL-MODELMARKET-TENANT-UNRESOLVED-403") from exc


async def _load(session: AsyncSession, package_id: uuid.UUID,
                tenant: uuid.UUID) -> ModelPackageModel:
    row = await session.get(ModelPackageModel, package_id)
    # 404 rather than 403 for another tenant's package: a 403 confirms the id.
    if row is None or row.tenant_id != tenant:
        raise HTTPException(404, "VEL-MODELMARKET-404: no such package")
    return row


def _package_json(r: ModelPackageModel) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "direction": r.direction,
        "kind": r.kind,
        "package_id": r.package_id,
        "version": r.version,
        "content_sha256": r.content_sha256,
        "status": r.status,
        "status_reason": r.status_reason,
        "manifest": r.manifest,
        "trained_on": r.trained_on,
        "declared_metrics": r.declared_metrics,
        "eval_fingerprint": r.eval_fingerprint,
        "canary": {"verdict": r.canary_verdict, "detail": r.canary_detail},
        "data_classes": r.data_classes,
        "special_category": bool(r.special_category),
        # Signed says the bytes match a key. It does NOT say the key belongs to
        # anybody in particular — the public half travels in the file — so the
        # key id is reported for an operator to pin out of band, and the word
        # "trusted" is deliberately not used anywhere on this object.
        "signed": bool(r.signing_key_id),
        "signing_key_id": r.signing_key_id,
        "publisher_ref": r.publisher_ref,
        "signed_off_by": r.signed_off_by,
        "signed_off_at": iso_utc(r.signed_off_at),
        "created_by": r.created_by,
        "created_at": iso_utc(r.created_at),
    }


def _eval_json(e: ModelPackageEvalModel) -> dict[str, Any]:
    return {
        "id": str(e.id),
        "verdict": e.verdict,
        "reason": e.reason,
        "holdout_size": e.holdout_size,
        "positives": e.positives,
        "negatives": e.negatives,
        "declared": e.declared,
        "measured": e.measured,
        "tolerance": float(e.tolerance) if e.tolerance is not None else None,
        "evaluated_by": e.evaluated_by,
        "evaluated_at": iso_utc(e.evaluated_at),
    }


def _install_json(i: ModelPackageInstallModel) -> dict[str, Any]:
    return {
        "id": str(i.id),
        "package_ref": str(i.package_ref),
        "model_origin": i.model_origin,
        "target_kind": i.target_kind,
        "target_ref": i.target_ref,
        "status": i.status,
        "installed_by": i.installed_by,
        "installed_at": iso_utc(i.installed_at),
        "activated_by": i.activated_by,
        "activated_at": iso_utc(i.activated_at),
        "uninstalled_at": iso_utc(i.uninstalled_at),
        "shredded_at": iso_utc(i.shredded_at),
    }


# ── what could be published from here ────────────────────────────────────────


@router.get("/sources")
async def list_sources(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("marketplace.models.read")),
) -> dict[str, Any]:
    """The local artefacts a publisher could choose from.

    Every risk model version, not only the active one: a tenant may well want to
    share the version they trust rather than the one they happen to be running.
    """
    token = _token(user)
    models = list((await session.execute(
        select(CaseRiskModelModel).where(CaseRiskModelModel.tenant_id == token)
        .order_by(CaseRiskModelModel.version.desc()))).scalars().all())
    packs = list((await session.execute(
        select(CaseRiskSignalPackModel).where(
            CaseRiskSignalPackModel.tenant_id == token))).scalars().all())
    return {
        "risk_models": [
            {"source": str(m.version), "version": m.version, "status": m.status,
             "trained_on": m.trained_on, "metrics": m.metrics,
             "publishable": int(m.trained_on or 0) > 0}
            for m in models
        ],
        "signal_packs": [
            {"source": p.pack_id, "name": p.name, "version": p.version,
             "enabled": p.enabled, "rules": len((p.manifest or {}).get("rules") or [])}
            for p in packs
        ],
    }


@router.post("/prepare", dependencies=[Depends(_probe_rl)])
async def prepare(
    body: PrepareIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("marketplace.models.publish")),
) -> dict[str, Any]:
    """The card, the payload, and what the canary says. Writes nothing."""
    if body.kind not in packaging.KINDS:
        raise HTTPException(400, f"VEL-MODELMARKET-KIND-400: unknown kind {body.kind!r}")
    try:
        return await service.prepare(
            session, tenant_token=_token(user), kind=body.kind, source=body.source)
    except (service.ModelMarketError, packaging.PackageError) as exc:
        raise HTTPException(400, f"VEL-MODELMARKET-PREPARE-400: {exc}") from exc


@router.post("/publish", status_code=201, dependencies=[Depends(_publish_rl)])
async def publish(
    body: PublishIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("marketplace.models.publish")),
) -> dict[str, Any]:
    """Publish, or record why not.

    A blocked canary is a 201 carrying a `quarantined` package, not a 4xx that
    unwinds the row. The refusal is the record.
    """
    if body.kind not in packaging.KINDS:
        raise HTTPException(400, f"VEL-MODELMARKET-KIND-400: unknown kind {body.kind!r}")
    try:
        row = await service.publish(
            session, tenant_token=_token(user), kind=body.kind, source=body.source,
            package_id=body.package_id, actor=user.user_id,
            acknowledged=body.acknowledged)
    except (service.ModelMarketError, packaging.PackageError) as exc:
        raise HTTPException(400, f"VEL-MODELMARKET-PUBLISH-400: {exc}") from exc
    await session.commit()
    return {"package": _package_json(row),
            "published": row.status == "published"}


@router.get("/packages")
async def list_packages(
    direction: str | None = Query(None, pattern="^(published|imported)$"),
    limit: int = Query(100, ge=1, le=500),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("marketplace.models.read")),
) -> dict[str, Any]:
    tenant = await _tenant(session, user)
    q = select(ModelPackageModel).where(ModelPackageModel.tenant_id == tenant)
    if direction:
        q = q.where(ModelPackageModel.direction == direction)
    rows = list((await session.execute(
        q.order_by(ModelPackageModel.created_at.desc()).limit(limit))).scalars().all())
    return {"packages": [_package_json(r) for r in rows], "count": len(rows)}


@router.get("/packages/{package_id}")
async def get_package(
    package_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("marketplace.models.read")),
) -> dict[str, Any]:
    tenant = await _tenant(session, user)
    row = await _load(session, package_id, tenant)
    evals = list((await session.execute(
        select(ModelPackageEvalModel)
        .where(ModelPackageEvalModel.package_ref == row.id)
        .order_by(ModelPackageEvalModel.evaluated_at.desc()))).scalars().all())
    install = (await session.execute(
        select(ModelPackageInstallModel)
        .where(ModelPackageInstallModel.package_ref == row.id))).scalars().first()
    return {"package": _package_json(row),
            "evaluations": [_eval_json(e) for e in evals],
            "install": _install_json(install) if install else None}


@router.get("/packages/{package_id}/file")
async def download_package(
    package_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("marketplace.models.publish")),
) -> dict[str, Any]:
    """The bytes that leave this install.

    Behind `publish`, not `read`: producing the file is the act of sending the
    tenant's learned aggregates somewhere, whoever downloads it.
    """
    tenant = await _tenant(session, user)
    row = await _load(session, package_id, tenant)
    if row.status != "published":
        raise HTTPException(409, f"VEL-MODELMARKET-STATUS-409: package is {row.status}")
    try:
        doc = await service.export_file(session, row)
    except service.ModelMarketError as exc:
        raise HTTPException(409, f"VEL-MODELMARKET-EXPORT-409: {exc}") from exc
    await session.commit()
    return {"filename": f"{row.package_id.replace('/', '_')}-v{row.version}.hxmodel",
            "content": doc, "signed": bool(doc.get("signature"))}


# ── taking one in ────────────────────────────────────────────────────────────


@router.post("/import", status_code=201)
async def import_package(
    body: ImportIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("marketplace.models.install")),
) -> dict[str, Any]:
    """Store a package. Nothing is installed and nothing runs."""
    try:
        row = await service.import_file(
            session, tenant_token=_token(user), raw=body.package, actor=user.user_id)
    except (packaging.PackageError, service.ModelMarketError) as exc:
        raise HTTPException(400, f"VEL-MODELMARKET-IMPORT-400: {exc}") from exc
    await session.commit()
    return {"package": _package_json(row)}


@router.post("/packages/{package_id}/evaluate", status_code=201,
             dependencies=[Depends(_probe_rl)])
async def evaluate_package(
    package_id: uuid.UUID,
    body: EvaluateIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("marketplace.models.install")),
) -> dict[str, Any]:
    """Score it on this tenant's own cases. The gate, and a kept record."""
    tenant = await _tenant(session, user)
    row = await _load(session, package_id, tenant)
    try:
        result = await service.run_eval(
            session, tenant_token=_token(user), package=row, actor=user.user_id,
            tolerance=body.tolerance)
    except service.ModelMarketError as exc:
        raise HTTPException(400, f"VEL-MODELMARKET-EVAL-400: {exc}") from exc
    await session.commit()
    return {"evaluation": _eval_json(result)}


@router.post("/packages/{package_id}/install", status_code=201)
async def install_package(
    package_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("marketplace.models.install")),
) -> dict[str, Any]:
    """Place it in the runtime, INACTIVE. Activation is HxSentinel's own gate."""
    tenant = await _tenant(session, user)
    row = await _load(session, package_id, tenant)
    try:
        install = await service.install(
            session, tenant_token=_token(user), package=row, actor=user.user_id)
    except service.ModelMarketError as exc:
        raise HTTPException(409, f"VEL-MODELMARKET-INSTALL-409: {exc}") from exc
    await session.commit()
    return {"install": _install_json(install),
            "note": ("Installed but not active. A risk model lands pending and a "
                     "signal pack lands disabled; switch it on in HxSentinel.")}


@router.post("/installs/{install_id}/uninstall")
async def uninstall_package(
    install_id: uuid.UUID,
    body: UninstallIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(hxguard.guard("marketplace.models.install")),
) -> dict[str, Any]:
    tenant = await _tenant(session, user)
    row = await session.get(ModelPackageInstallModel, install_id)
    if row is None or row.tenant_id != tenant:
        raise HTTPException(404, "VEL-MODELMARKET-404: no such install")
    result = await service.uninstall(
        session, tenant_token=_token(user), install_row=row, shred=body.shred,
        actor=user.user_id)
    await session.commit()
    return {"install": _install_json(result)}
