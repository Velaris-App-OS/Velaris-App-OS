"""P55 HxDeploy — Intelligent Deployment Governance router."""
from __future__ import annotations

import hashlib
import hmac
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser
from case_service.db.session import get_session
from case_service.hxdeploy import service

router = APIRouter(prefix="/deploy", tags=["deploy"])


def _tenant(user: AuthenticatedUser) -> str:
    return getattr(user, "tenant_id", None) or "default"


def _actor(user: AuthenticatedUser) -> str:
    """Return the most human-readable identifier for the user."""
    return (getattr(user, "username", None)
            or getattr(user, "email", None)
            or getattr(user, "user_id", None)
            or "system")


# ── Schemas ───────────────────────────────────────────────────────────────────

class EnvIn(BaseModel):
    name:            str
    label:           str
    url:             str | None = None
    order_index:     int = 0
    delivery_method: str = "manual"   # manual | webhook | push
    webhook_url:     str | None = None
    webhook_secret:  str | None = None
    import_api_key:  str | None = None


class EnvOut(BaseModel):
    id:               uuid.UUID
    name:             str
    label:            str
    url:              str | None
    order_index:      int
    current_version:  str | None
    status:           str
    last_deployed_at: str | None
    delivery_method:  str
    webhook_url:      str | None
    # secrets are never returned in GET — only their presence is indicated
    has_webhook_secret: bool
    has_import_api_key: bool

    model_config = {"from_attributes": True}

    @classmethod
    def from_model(cls, e: Any) -> "EnvOut":
        return cls(
            id=e.id, name=e.name, label=e.label, url=e.url,
            order_index=e.order_index, current_version=e.current_version,
            status=e.status,
            last_deployed_at=e.last_deployed_at.isoformat() if e.last_deployed_at else None,
            delivery_method=getattr(e, "delivery_method", "manual"),
            webhook_url=getattr(e, "webhook_url", None),
            has_webhook_secret=bool(getattr(e, "webhook_secret", None)),
            has_import_api_key=bool(getattr(e, "import_api_key", None)),
        )


class PromoteIn(BaseModel):
    to_env_id:          uuid.UUID
    package_id:         uuid.UUID | None = None
    package_manifest:   dict = {}
    from_env_id:        uuid.UUID | None = None
    deploy_notes:       str = ""
    assign_to_user_id:  str | None = None
    assign_to_name:     str | None = None


class ApproveIn(BaseModel):
    package_manifest: dict = {}


class RejectIn(BaseModel):
    reason: str


class RunOut(BaseModel):
    id:              uuid.UUID
    risk_level:      str
    risk_summary:    dict
    status:          str
    initiated_by:    str
    approved_by:     str | None
    rejected_by:     str | None
    rejection_reason: str | None
    deploy_notes:    str | None
    to_env_id:       uuid.UUID | None
    from_env_id:     uuid.UUID | None
    package_id:      uuid.UUID | None
    created_at:      str
    deployed_at:     str | None
    completed_at:    str | None

    @classmethod
    def from_model(cls, r: Any) -> "RunOut":
        return cls(
            id=r.id, risk_level=r.risk_level, risk_summary=r.risk_summary,
            status=r.status, initiated_by=r.initiated_by,
            approved_by=r.approved_by, rejected_by=r.rejected_by,
            rejection_reason=r.rejection_reason, deploy_notes=r.deploy_notes,
            to_env_id=r.to_env_id, from_env_id=r.from_env_id, package_id=r.package_id,
            created_at=r.created_at.isoformat(),
            deployed_at=r.deployed_at.isoformat() if r.deployed_at else None,
            completed_at=r.completed_at.isoformat() if r.completed_at else None,
        )


class RunPatchIn(BaseModel):
    deploy_notes:     str | None = None
    to_env_id:        uuid.UUID | None = None
    package_manifest: dict | None = None


class WindowIn(BaseModel):
    env_id:         uuid.UUID
    name:           str
    days_of_week:   list[int] = list(range(7))
    start_hour_utc: int = 0
    end_hour_utc:   int = 23
    enabled:        bool = True


class HealthCheckOut(BaseModel):
    id:          uuid.UUID
    run_id:      uuid.UUID
    check_url:   str | None
    status_code: int | None
    response_ms: int | None
    healthy:     bool | None
    error:       str | None
    checked_at:  str

    @classmethod
    def from_model(cls, h: Any) -> "HealthCheckOut":
        return cls(
            id=h.id, run_id=h.run_id, check_url=h.check_url,
            status_code=h.status_code, response_ms=h.response_ms,
            healthy=h.healthy, error=h.error,
            checked_at=h.checked_at.isoformat(),
        )


# ── Environments ──────────────────────────────────────────────────────────────

@router.get("/environments", response_model=list[EnvOut])
async def list_environments(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    envs = await service.list_environments(session, _tenant(user))
    return [EnvOut.from_model(e) for e in envs]


@router.post("/environments", response_model=EnvOut, status_code=201)
async def register_environment(
    body: EnvIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    env = await service.register_environment(
        session, _tenant(user), body.name, body.label, body.url, body.order_index,
        delivery_method=body.delivery_method,
        webhook_url=body.webhook_url,
        webhook_secret=body.webhook_secret,
        import_api_key=body.import_api_key,
    )
    await session.commit()
    return EnvOut.from_model(env)


@router.patch("/environments/{env_id}", response_model=EnvOut)
async def update_environment(
    env_id: uuid.UUID,
    body: EnvIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    from case_service.db.models import EnvironmentRegistryModel
    env = await session.get(EnvironmentRegistryModel, env_id)
    if not env:
        raise HTTPException(404, "Environment not found")
    env.label            = body.label
    env.url              = body.url if body.url is not None else env.url
    env.order_index      = body.order_index
    env.delivery_method  = body.delivery_method
    if body.webhook_url is not None:
        env.webhook_url = body.webhook_url
    if body.webhook_secret is not None:
        env.webhook_secret = body.webhook_secret
    if body.import_api_key is not None:
        env.import_api_key = body.import_api_key
    await session.commit()
    return EnvOut.from_model(env)


@router.delete("/environments/{env_id}", status_code=204)
async def delete_environment(
    env_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    from case_service.db.models import EnvironmentRegistryModel
    env = await session.get(EnvironmentRegistryModel, env_id)
    if not env:
        raise HTTPException(404, "Environment not found")
    await session.delete(env)
    await session.commit()


@router.get("/environments/{env_id}/status")
async def get_environment_status(
    env_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    return await service.get_environment_status(session, env_id)


# ── Deployment Runs ───────────────────────────────────────────────────────────

@router.post("/promote", response_model=RunOut, status_code=201)
async def promote(
    body: PromoteIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    try:
        run = await service.promote(
            session, _tenant(user),
            to_env_id=body.to_env_id,
            package_id=body.package_id,
            package_manifest=body.package_manifest,
            initiated_by=_actor(user),
            deploy_notes=body.deploy_notes,
            from_env_id=body.from_env_id,
            assign_to_user_id=body.assign_to_user_id,
            assign_to_name=body.assign_to_name,
        )
        await session.commit()
        return RunOut.from_model(run)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get("/runs", response_model=list[RunOut])
async def list_runs(
    status: str | None = None,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    rows = await service.list_runs(session, _tenant(user), status=status)
    return [RunOut.from_model(r) for r in rows]


@router.get("/runs/{run_id}", response_model=RunOut)
async def get_run(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    run = await service.get_run(session, run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    return RunOut.from_model(run)


@router.post("/runs/{run_id}/approve", response_model=RunOut)
async def approve_run(
    run_id: uuid.UUID,
    body: ApproveIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    try:
        run = await service.approve_run(session, run_id, _actor(user), body.package_manifest)
        await session.commit()
        return RunOut.from_model(run)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/runs/{run_id}/reject", response_model=RunOut)
async def reject_run(
    run_id: uuid.UUID,
    body: RejectIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    try:
        run = await service.reject_run(session, run_id, _actor(user), body.reason)
        await session.commit()
        return RunOut.from_model(run)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/runs/{run_id}/health-check", response_model=HealthCheckOut)
async def run_health_check(
    run_id: uuid.UUID,
    check_url: str | None = None,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    try:
        hc = await service.run_health_check(session, run_id, check_url)
        await session.commit()
        return HealthCheckOut.from_model(hc)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.patch("/runs/{run_id}", response_model=RunOut)
async def update_run(
    run_id: uuid.UUID,
    body: RunPatchIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    from case_service.db.models import DeploymentRunModel, EnvironmentRegistryModel
    run = await session.get(DeploymentRunModel, run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    if run.status not in ("pending", "awaiting_approval"):
        raise HTTPException(400, f"Cannot edit run in status '{run.status}'")

    if body.deploy_notes is not None:
        run.deploy_notes = body.deploy_notes

    if body.to_env_id is not None and body.to_env_id != run.to_env_id:
        env = await session.get(EnvironmentRegistryModel, body.to_env_id)
        if not env:
            raise HTTPException(404, "Target environment not found")
        run.to_env_id = body.to_env_id
        manifest = body.package_manifest or {"case_types": (run.risk_summary or {}).get("affected_items", [])}
        risk = await service.classify_risk(manifest, env.name)
        run.risk_level  = risk.get("risk_level", run.risk_level)
        run.risk_summary = risk
    elif body.package_manifest is not None:
        to_env = await session.get(EnvironmentRegistryModel, run.to_env_id) if run.to_env_id else None
        risk = await service.classify_risk(body.package_manifest, to_env.name if to_env else "unknown")
        run.risk_level  = risk.get("risk_level", run.risk_level)
        run.risk_summary = risk

    await session.commit()
    return RunOut.from_model(run)


@router.delete("/runs/{run_id}", status_code=204)
async def delete_run(
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    from case_service.db.models import DeploymentRunModel
    run = await session.get(DeploymentRunModel, run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    await session.delete(run)
    await session.commit()


# ── Change Windows ────────────────────────────────────────────────────────────

@router.post("/windows", status_code=201)
async def create_window(
    body: WindowIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    from case_service.db.models import DeploymentWindowModel
    w = DeploymentWindowModel(
        tenant_id=_tenant(user), env_id=body.env_id,
        name=body.name, days_of_week=body.days_of_week,
        start_hour_utc=body.start_hour_utc, end_hour_utc=body.end_hour_utc,
        enabled=body.enabled,
    )
    session.add(w)
    await session.commit()
    return {"id": str(w.id), "name": w.name, "env_id": str(w.env_id),
            "days": w.days_of_week, "start": w.start_hour_utc, "end": w.end_hour_utc,
            "enabled": w.enabled}


@router.get("/windows")
async def list_windows(
    env_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    from sqlalchemy import select
    from case_service.db.models import DeploymentWindowModel
    q = select(DeploymentWindowModel).where(DeploymentWindowModel.tenant_id == _tenant(user))
    if env_id:
        q = q.where(DeploymentWindowModel.env_id == env_id)
    rows = (await session.execute(q)).scalars().all()
    return [{"id": str(w.id), "name": w.name, "env_id": str(w.env_id),
             "days": w.days_of_week, "start": w.start_hour_utc, "end": w.end_hour_utc,
             "enabled": w.enabled} for w in rows]


@router.patch("/windows/{window_id}", status_code=200)
async def update_window(
    window_id: uuid.UUID,
    body: WindowIn,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    from case_service.db.models import DeploymentWindowModel
    w = await session.get(DeploymentWindowModel, window_id)
    if not w:
        raise HTTPException(404, "Window not found")
    w.name           = body.name
    w.days_of_week   = body.days_of_week
    w.start_hour_utc = body.start_hour_utc
    w.end_hour_utc   = body.end_hour_utc
    w.enabled        = body.enabled
    await session.commit()
    return {"id": str(w.id), "name": w.name, "env_id": str(w.env_id),
            "days": w.days_of_week, "start": w.start_hour_utc, "end": w.end_hour_utc,
            "enabled": w.enabled}


@router.delete("/windows/{window_id}", status_code=204)
async def delete_window(
    window_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    from case_service.db.models import DeploymentWindowModel
    w = await session.get(DeploymentWindowModel, window_id)
    if not w:
        raise HTTPException(404, "Window not found")
    await session.delete(w)
    await session.commit()


# ── My approvals ─────────────────────────────────────────────────────────────

@router.get("/my-approvals", response_model=list[RunOut])
async def my_approvals(
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Return deployment runs awaiting approval assigned to the current user.

    Matches against both username and user_id UUID since the user-directory
    uses the username string as its user_id field.
    """
    # user.user_id is the UUID from helix_users.id (JWT sub claim)
    # user.username is the login name — user-directory stores this as user_id
    identifiers = {
        str(getattr(user, "user_id", "") or ""),
        str(getattr(user, "username", "") or ""),
        str(getattr(user, "email", "") or ""),
    } - {""}
    rows = await service.list_runs(session, _tenant(user), status="awaiting_approval", limit=200)
    assigned = [
        r for r in rows
        if (r.risk_summary or {}).get("assigned_to_user_id") in identifiers
    ]
    return [RunOut.from_model(r) for r in assigned]


# ── Risk analysis endpoint (standalone) ──────────────────────────────────────

@router.post("/analyse-risk")
async def analyse_risk(
    body: PromoteIn,
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Classify risk without starting a deployment — useful for pre-flight checks."""
    result = await service.classify_risk(body.package_manifest, str(body.to_env_id))
    return result


# ── Bundle / Packager (Option B) ─────────────────────────────────────────────

class PackageIn(BaseModel):
    case_type_ids: list[uuid.UUID] | None = None   # None = all case types


@router.post("/package")
async def build_package(
    body: PackageIn = PackageIn(),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Serialise all (or selected) case types into a deployment bundle.

    Returns the full bundle JSON — ready to preview or POST to /deploy/import.
    """
    from case_service.hxdeploy.packager import build_bundle
    bundle = await build_bundle(session, _tenant(user), body.case_type_ids)
    return bundle


@router.post("/package/delta")
async def build_delta_package(
    env_id: uuid.UUID,
    body: PackageIn = PackageIn(),
    session: AsyncSession = Depends(get_session),
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Build a delta bundle: only artifacts changed since env's last deployment.

    Returns full bundle if env has never been deployed to (same as /package).
    """
    from case_service.hxdeploy.packager import build_delta_bundle
    bundle = await build_delta_bundle(session, _tenant(user), env_id, body.case_type_ids)
    return bundle


@router.post("/import")
async def import_bundle(
    request: Request,
    session: AsyncSession = Depends(get_session),
    authorization: str | None = Header(default=None),
    x_deploy_key: str | None = Header(default=None, alias="X-Deploy-Key"),
):
    """Receive a deployment bundle and upsert design-time artifacts.

    This is a machine-to-machine endpoint (CI/CD or push delivery).
    Auth: Bearer <import_api_key> in Authorization header, OR X-Deploy-Key header.
    """
    from sqlalchemy import select as _select
    from case_service.db.models import EnvironmentRegistryModel
    from case_service.hxdeploy.packager import apply_bundle

    # Resolve the provided key
    provided_key: str | None = None
    if authorization and authorization.lower().startswith("bearer "):
        provided_key = authorization[7:].strip()
    elif x_deploy_key:
        provided_key = x_deploy_key.strip()

    if not provided_key:
        raise HTTPException(401, "Missing import key — provide Authorization: Bearer <key>")

    # Find the environment whose import_api_key matches
    all_envs = (await session.execute(
        _select(EnvironmentRegistryModel)
        .where(EnvironmentRegistryModel.import_api_key == provided_key)
    )).scalars().all()

    if not all_envs:
        raise HTTPException(401, "Invalid import key")

    target_env  = all_envs[0]
    target_tenant = target_env.tenant_id

    bundle = await request.json()
    if not isinstance(bundle, dict):
        raise HTTPException(400, "Body must be a JSON object (bundle)")

    try:
        result = await apply_bundle(session, bundle, target_tenant)
    except ValueError as ve:
        raise HTTPException(400, str(ve))
    await session.commit()
    return {**result, "target_env": target_env.name, "tenant_id": target_tenant}


# ── CI/CD Callback (Option A) ─────────────────────────────────────────────────

class DeployCallbackIn(BaseModel):
    status:           str            # "deployed" | "failed"
    deployed_version: str | None = None
    error_message:    str | None = None


@router.post("/runs/{run_id}/callback")
async def deploy_callback(
    run_id: uuid.UUID,
    body: DeployCallbackIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    x_deploy_signature: str | None = Header(default=None, alias="X-Deploy-Signature"),
):
    """CI/CD pipeline calls this after completing a webhook-triggered deployment.

    Verifies HMAC signature against the environment's webhook_secret,
    then updates the run status accordingly.
    """
    from case_service.db.models import DeploymentRunModel, EnvironmentRegistryModel

    run = await session.get(DeploymentRunModel, run_id)
    if not run:
        raise HTTPException(404, "Run not found")
    if run.status not in ("triggered", "deploying"):
        raise HTTPException(400, f"Run is in unexpected status: {run.status}")

    # Verify HMAC if the environment has a webhook_secret
    if run.to_env_id:
        env = await session.get(EnvironmentRegistryModel, run.to_env_id)
        if env and env.webhook_secret and x_deploy_signature:
            raw_body = await request.body()
            expected = "sha256=" + hmac.new(
                env.webhook_secret.encode(), raw_body, hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(expected, x_deploy_signature):
                raise HTTPException(401, "Invalid webhook signature")
            if env and body.status == "deployed":
                env.current_version   = body.deployed_version or run.deployed_at and str(run.deployed_at)[:10] or "unknown"
                env.last_deployed_at  = service._utcnow()

    if body.status == "deployed":
        run.status       = "deployed"
        run.completed_at = service._utcnow()
    else:
        run.status       = "failed"
        run.completed_at = service._utcnow()
        run.deploy_notes = (run.deploy_notes or "") + f"\n[CI/CD ERROR] {body.error_message or 'unknown'}"

    await session.commit()
    return {"run_id": str(run.id), "status": run.status}
