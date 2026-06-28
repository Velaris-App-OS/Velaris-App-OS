"""P55 HxDeploy — Intelligent Deployment Governance service."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import uuid
from datetime import datetime, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import (
    DeploymentHealthCheckModel,
    DeploymentRunModel,
    DeploymentWindowModel,
    EnvironmentRegistryModel,
)

logger = logging.getLogger(__name__)

RISK_ORDER = ["low", "medium", "high", "critical"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Risk classification ───────────────────────────────────────────────────────

async def classify_risk(package_manifest: dict, to_env: str) -> dict:
    """Use HxNexus to classify deployment risk; fall back to heuristics."""
    changes = package_manifest.get("case_types", [])
    forms   = package_manifest.get("forms", [])
    notes   = package_manifest.get("notes", "")

    prompt = (
        f"You are a deployment risk classifier for Velaris BPM.\n"
        f"A package is being promoted to environment: {to_env}\n"
        f"Package contents:\n"
        f"- Case types changed/added: {len(changes)}\n"
        f"- Forms changed/added: {len(forms)}\n"
        f"- Notes: {notes[:500]}\n\n"
        f"Classify the deployment risk. Return JSON:\n"
        f'{{"risk_level": "low|medium|high|critical", "reason": "...", '
        f'"affected_items": [], "recommendation": "..."}}\n\n'
        f"Rules:\n"
        f"- low: UI labels, colour, optional field additions only\n"
        f"- medium: new stages/steps, required field additions, new connectors\n"
        f"- high: SLA changes, permission/role changes, stage deletions\n"
        f"- critical: breaking schema changes, table drops, prod with >50 case types changed"
    )

    try:
        from case_service.hxnexus.factory import generate_json
        result = await generate_json(prompt)
        if result and "risk_level" in result:
            return result
    except Exception as exc:
        logger.warning("HxNexus risk classification failed: %s", exc)

    return _heuristic_risk(package_manifest, to_env)


def _heuristic_risk(manifest: dict, to_env: str) -> dict:
    ct_count = len(manifest.get("case_types", []))
    has_sla  = bool(manifest.get("sla_sql"))

    if to_env == "prod" and ct_count > 5:
        level = "high"
    elif to_env == "prod" and has_sla:
        level = "high"
    elif ct_count == 0 and not has_sla:
        level = "low"
    elif ct_count <= 2:
        level = "medium"
    else:
        level = "medium"

    return {
        "risk_level":    level,
        "reason":        f"{ct_count} case type(s) changed; target: {to_env}",
        "affected_items": manifest.get("case_types", [])[:10],
        "recommendation": "Review changes before deploying to production." if to_env == "prod" else "Standard review.",
    }


def _within_window(window: DeploymentWindowModel) -> bool:
    now = _utcnow()
    return (
        now.weekday() in (window.days_of_week or list(range(7)))
        and window.start_hour_utc <= now.hour < window.end_hour_utc
    )


# ── Environments ──────────────────────────────────────────────────────────────

async def list_environments(session: AsyncSession, tenant_id: str) -> list[EnvironmentRegistryModel]:
    rows = (await session.execute(
        select(EnvironmentRegistryModel)
        .where(EnvironmentRegistryModel.tenant_id == tenant_id)
        .order_by(EnvironmentRegistryModel.order_index)
    )).scalars().all()
    return list(rows)


async def register_environment(
    session: AsyncSession,
    tenant_id: str,
    name: str,
    label: str,
    url: str | None,
    order_index: int,
    delivery_method: str = "manual",
    webhook_url: str | None = None,
    webhook_secret: str | None = None,
    import_api_key: str | None = None,
) -> EnvironmentRegistryModel:
    existing = (await session.execute(
        select(EnvironmentRegistryModel).where(
            EnvironmentRegistryModel.tenant_id == tenant_id,
            EnvironmentRegistryModel.name == name,
        )
    )).scalar_one_or_none()

    if existing:
        existing.label           = label
        existing.url             = url or existing.url
        existing.order_index     = order_index
        existing.delivery_method = delivery_method
        if webhook_url is not None:
            existing.webhook_url = webhook_url
        if webhook_secret is not None:
            existing.webhook_secret = webhook_secret
        if import_api_key is not None:
            existing.import_api_key = import_api_key
        return existing

    env = EnvironmentRegistryModel(
        tenant_id=tenant_id, name=name, label=label,
        url=url, order_index=order_index,
        delivery_method=delivery_method,
        webhook_url=webhook_url,
        webhook_secret=webhook_secret,
        import_api_key=import_api_key,
    )
    session.add(env)
    await session.flush()
    return env


async def get_environment_status(session: AsyncSession, env_id: uuid.UUID) -> dict:
    env = await session.get(EnvironmentRegistryModel, env_id)
    if not env:
        return {"error": "Environment not found"}

    health = {"status": env.status, "current_version": env.current_version}

    if env.url:
        try:
            start = time.monotonic()
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(env.url + "/api/v1/health")
            ms = int((time.monotonic() - start) * 1000)
            health["http_status"] = resp.status_code
            health["response_ms"] = ms
            health["reachable"]   = resp.status_code < 500
        except Exception as exc:
            health["reachable"] = False
            health["error"]     = str(exc)[:200]

    return health


# ── Deployments ───────────────────────────────────────────────────────────────

async def promote(
    session: AsyncSession,
    tenant_id: str,
    to_env_id: uuid.UUID,
    package_id: uuid.UUID | None,
    package_manifest: dict,
    initiated_by: str,
    deploy_notes: str = "",
    from_env_id: uuid.UUID | None = None,
    assign_to_user_id: str | None = None,
    assign_to_name: str | None = None,
) -> DeploymentRunModel:
    to_env = await session.get(EnvironmentRegistryModel, to_env_id)
    if not to_env:
        raise ValueError("Target environment not found")

    windows = (await session.execute(
        select(DeploymentWindowModel).where(
            DeploymentWindowModel.env_id == to_env_id,
            DeploymentWindowModel.enabled == True,  # noqa: E712
        )
    )).scalars().all()

    window_blocked = bool(windows) and not any(_within_window(w) for w in windows)

    risk = await classify_risk(package_manifest, to_env.name)
    risk_level = risk.get("risk_level", "medium")

    # All deployments require explicit human approval regardless of risk level.
    initial_status = "awaiting_approval"
    if window_blocked:
        risk["window_blocked"] = True
    if assign_to_user_id:
        risk["assigned_to_user_id"] = assign_to_user_id
        risk["assigned_to_name"]    = assign_to_name or assign_to_user_id

    run = DeploymentRunModel(
        tenant_id=tenant_id,
        package_id=package_id,
        from_env_id=from_env_id,
        to_env_id=to_env_id,
        risk_level=risk_level,
        risk_summary=risk,
        status=initial_status,
        initiated_by=initiated_by,
        deploy_notes=deploy_notes,
    )
    session.add(run)
    await session.flush()

    await _emit(run.id, "deploy.promoted", {"risk": risk_level, "status": run.status})
    return run


async def _trigger_webhook(
    run: DeploymentRunModel,
    env: EnvironmentRegistryModel,
    manifest: dict,
) -> None:
    """POST HMAC-signed deployment payload to env.webhook_url (Option A)."""
    if not env.webhook_url:
        raise ValueError("webhook_url is not set on this environment")

    payload = {
        "run_id":       str(run.id),
        "tenant_id":    run.tenant_id,
        "to_env":       env.name,
        "to_env_label": env.label,
        "risk_level":   run.risk_level,
        "initiated_by": run.initiated_by,
        "deploy_notes": run.deploy_notes or "",
        "manifest_summary": {
            "case_types": len(manifest.get("case_types", [])),
            "version":    manifest.get("version", ""),
        },
        "timestamp":    _utcnow().isoformat(),
    }
    body_bytes = json.dumps(payload).encode()

    headers = {"Content-Type": "application/json"}
    if env.webhook_secret:
        sig = hmac.new(
            env.webhook_secret.encode(), body_bytes, hashlib.sha256
        ).hexdigest()
        headers["X-Deploy-Signature"] = f"sha256={sig}"

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(env.webhook_url, content=body_bytes, headers=headers)
        resp.raise_for_status()

    logger.info("webhook triggered for run %s → %s (status %s)", run.id, env.webhook_url, resp.status_code)


async def _push_bundle(
    session: AsyncSession,
    run: DeploymentRunModel,
    env: EnvironmentRegistryModel,
) -> None:
    """Serialise bundle and POST to target Velaris instance (Option B)."""
    if not env.url:
        raise ValueError("env.url is not set — cannot push bundle")
    if not env.import_api_key:
        raise ValueError("import_api_key is not set on this environment")

    from case_service.hxdeploy.packager import build_bundle, build_delta_bundle

    # Use delta bundle if the environment has been deployed to before
    if getattr(env, "last_deployed_at", None) and run.to_env_id:
        bundle = await build_delta_bundle(session, run.tenant_id, run.to_env_id)
    else:
        bundle = await build_bundle(session, run.tenant_id)
    bundle["deployment_run_id"] = str(run.id)
    bundle["source_env"] = getattr(env, "name", "unknown")

    target_url = env.url.rstrip("/") + "/api/v1/deploy/import"
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            target_url,
            json=bundle,
            headers={
                "Authorization": f"Bearer {env.import_api_key}",
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()

    result = resp.json()
    logger.info(
        "bundle pushed to %s: imported=%s skipped=%s errors=%s needs_config=%s",
        target_url, result.get("imported"), result.get("skipped"),
        len(result.get("errors", [])), len(result.get("needs_configuration", [])),
    )
    env.current_version = bundle.get("created_at", "")[:10]

    # Persist push result in risk_summary so the Studio can surface it
    existing = dict(run.risk_summary or {})
    existing["push_result"] = {
        "imported":          result.get("imported", 0),
        "skipped":           result.get("skipped", 0),
        "errors":            result.get("errors", [])[:5],
        "needs_configuration": result.get("needs_configuration", []),
    }
    run.risk_summary = existing


async def _execute_deployment(
    session: AsyncSession,
    run: DeploymentRunModel,
    env: EnvironmentRegistryModel,
    manifest: dict,
) -> None:
    run.status      = "deploying"
    run.deployed_at = _utcnow()
    await session.flush()

    delivery = getattr(env, "delivery_method", "manual")

    try:
        if delivery == "webhook":
            await _trigger_webhook(run, env, manifest)
            # webhook run stays "triggered" until callback
            run.status = "triggered"
            run.completed_at = None
        elif delivery == "push":
            await _push_bundle(session, run, env)
            env.current_package_id = run.package_id
            env.last_deployed_at   = _utcnow()
            run.status       = "deployed"
            run.completed_at = _utcnow()
        else:
            # manual: governance-only, mark deployed immediately
            env.current_package_id = run.package_id
            env.current_version    = manifest.get("version", "unknown")
            env.last_deployed_at   = _utcnow()
            run.status       = "deployed"
            run.completed_at = _utcnow()
    except Exception as exc:
        logger.error("deployment execution failed (method=%s): %s", delivery, exc)
        run.status       = "failed"
        run.completed_at = _utcnow()
        run.deploy_notes = (run.deploy_notes or "") + f"\n[ERROR] {exc}"

    await _emit(run.id, "deploy.executed", {"env": env.name, "delivery": delivery})


async def approve_run(
    session: AsyncSession,
    run_id: uuid.UUID,
    approved_by: str,
    package_manifest: dict,
) -> DeploymentRunModel:
    run = await session.get(DeploymentRunModel, run_id)
    if not run:
        raise ValueError("Deployment run not found")
    if run.status != "awaiting_approval":
        raise ValueError(f"Run is not awaiting approval (status: {run.status})")

    run.approved_by = approved_by
    to_env = await session.get(EnvironmentRegistryModel, run.to_env_id) if run.to_env_id else None
    if to_env:
        await _execute_deployment(session, run, to_env, package_manifest)
    else:
        run.status = "deployed"; run.completed_at = _utcnow()

    await _emit(run.id, "deploy.approved", {"by": approved_by})
    return run


async def reject_run(
    session: AsyncSession,
    run_id: uuid.UUID,
    rejected_by: str,
    reason: str,
) -> DeploymentRunModel:
    run = await session.get(DeploymentRunModel, run_id)
    if not run:
        raise ValueError("Deployment run not found")
    if run.status != "awaiting_approval":
        raise ValueError(f"Run is not awaiting approval (status: {run.status})")

    run.rejected_by      = rejected_by
    run.rejection_reason = reason
    run.status           = "rejected"
    run.completed_at     = _utcnow()

    await _emit(run.id, "deploy.rejected", {"by": rejected_by, "reason": reason})
    return run


async def run_health_check(
    session: AsyncSession,
    run_id: uuid.UUID,
    check_url: str | None = None,
) -> DeploymentHealthCheckModel:
    run = await session.get(DeploymentRunModel, run_id)
    if not run:
        raise ValueError("Run not found")

    if not check_url and run.to_env_id:
        env = await session.get(EnvironmentRegistryModel, run.to_env_id)
        if env and env.url:
            check_url = env.url + "/api/v1/health"

    check = DeploymentHealthCheckModel(run_id=run_id, check_url=check_url)
    session.add(check)

    if check_url:
        try:
            start = time.monotonic()
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(check_url)
            ms = int((time.monotonic() - start) * 1000)
            check.status_code = resp.status_code
            check.response_ms = ms
            check.healthy     = resp.status_code < 500
        except Exception as exc:
            check.healthy = False
            check.error   = str(exc)[:300]
    else:
        check.healthy = None
        check.error   = "No URL to probe"

    if run.to_env_id:
        env = await session.get(EnvironmentRegistryModel, run.to_env_id)
        if env:
            env.status = "healthy" if check.healthy else "degraded"

    await session.flush()
    return check


async def list_runs(
    session: AsyncSession,
    tenant_id: str,
    status: str | None = None,
    limit: int = 50,
) -> list[DeploymentRunModel]:
    q = (select(DeploymentRunModel)
         .where(DeploymentRunModel.tenant_id == tenant_id)
         .order_by(DeploymentRunModel.created_at.desc())
         .limit(limit))
    if status:
        q = q.where(DeploymentRunModel.status == status)
    return list((await session.execute(q)).scalars().all())


async def get_run(session: AsyncSession, run_id: uuid.UUID) -> DeploymentRunModel | None:
    return await session.get(DeploymentRunModel, run_id)


async def _emit(run_id: uuid.UUID, event: str, data: dict) -> None:
    try:
        from case_service.hxstream.emitter import emit_event
        await emit_event(str(run_id), event, data)
    except Exception:
        pass
