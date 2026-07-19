"""Marketplace Layer-1 — capability grants for remote apps (mig 122).

Freedom to publish is not freedom to execute. Installing a Layer-1 package
auto-maps its descriptor into an INERT connector (enabled=False, no
credentials, zero egress) plus a `pending` grant carrying exactly what the
manifest requested. Admin approval writes the ticked SUBSET into `granted`
and activates the connector with that set — never "now it's trusted".
Revocation flips the connector inert instantly.

Enforcement lives in the connector itself (`http_custom_connector`): a
marketplace-installed connector refuses any URL whose host is outside its
granted domains, runs the SSRF guard on every call, and logs every attempt —
allowed or blocked — to marketplace_network_log.

Copyright (c) 2024-2026 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import (
    ConnectorRegistryModel,
    MarketplaceCapabilityGrantModel,
)

logger = logging.getLogger(__name__)

GRANTABLE_KEYS = ("outbound_domains", "scopes")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def active_grant(
    session: AsyncSession, tenant_id: str, package_id: str,
) -> MarketplaceCapabilityGrantModel | None:
    """The one non-revoked grant for (tenant, package), if any."""
    return (await session.execute(
        select(MarketplaceCapabilityGrantModel).where(
            MarketplaceCapabilityGrantModel.tenant_id == tenant_id,
            MarketplaceCapabilityGrantModel.package_id == package_id,
            MarketplaceCapabilityGrantModel.status != "revoked",
        )
    )).scalars().first()


async def _map_descriptor(manifest: dict) -> dict:
    """Descriptor > connector config, via the existing devconn generator.

    - `openapi`: run through devconn (LLM if available, heuristic fallback);
      the FIRST suggested operation becomes the connector's active operation,
      the full list is kept in config["operations"] for review/UI.
    - `connector_template`: the descriptor IS the connector config (the
      publisher hand-shaped it) — minimally validated.
    """
    fmt = (manifest.get("execution") or {}).get("descriptor_format")
    text = manifest.get("_descriptor_text") or ""

    if fmt == "connector_template":
        try:
            template = json.loads(text)
        except json.JSONDecodeError as e:
            raise ValueError(f"connector_template descriptor is not valid JSON: {e}")
        if not isinstance(template, dict) or not template.get("url"):
            raise ValueError("connector_template descriptor must be an object with at least a 'url'")
        return {
            "method":           str(template.get("method", "POST")).upper(),
            "url":              template["url"],
            "headers":          template.get("headers", {}),
            "auth_type":        template.get("auth_type", "none"),
            "body_template":    template.get("body_template", ""),
            "response_mapping": template.get("response_mapping", {}),
            "operations":       template.get("operations", []),
        }

    # openapi — the DETERMINISTIC devconn parser, never the LLM path:
    # drift classification compares successive mappings, so the same spec
    # must always map to the same config (an LLM re-map would manufacture
    # phantom "breaking" drift out of paraphrasing).
    from case_service.devconn.service import _heuristic_parse
    generated = _heuristic_parse(text, manifest.get("name", "marketplace connector"))
    ops = generated.get("suggested_operations") or []
    if not ops:
        raise ValueError("descriptor mapping produced no operations — the OpenAPI spec has no usable paths")
    first = ops[0]
    return {
        "method":           str(first.get("method", "POST")).upper(),
        "url":              first.get("url", ""),
        "headers":          first.get("headers", {}),
        "auth_type":        first.get("auth_type", "none"),
        "body_template":    first.get("body_template", ""),
        "response_mapping": first.get("response_mapping", {}),
        "operations":       ops,
    }


async def create_pending_grant(
    session: AsyncSession,
    *,
    tenant_id: str,
    package_id: str,
    workspace_id: uuid.UUID | None,
    manifest: dict,
    requested_by: str,
    publisher_tier: str = "community",
) -> MarketplaceCapabilityGrantModel:
    """Install-side provisioning: inert connector + pending grant.

    Idempotent per (tenant, package): a second install with the SAME
    descriptor returns the existing grant untouched; a CHANGED descriptor
    runs drift classification instead (P2) — never a second connector.
    `publisher_tier` must come from the effective-tier resolution (source
    URL + official registry), never from the manifest.
    """
    existing = await active_grant(session, tenant_id, package_id)
    if existing is not None:
        await apply_descriptor_drift(
            session, grant=existing, manifest=manifest, publisher_tier=publisher_tier)
        return existing

    execution = manifest.get("execution") or {}
    if execution.get("layer") == 2:
        return await _create_pending_container_grant(
            session, tenant_id=tenant_id, package_id=package_id,
            workspace_id=workspace_id, manifest=manifest, requested_by=requested_by)

    config = await _map_descriptor(manifest)

    connector = ConnectorRegistryModel(
        name=f"mkt:{package_id}",
        connector_type="http_custom",
        description=f"Marketplace Layer-1 connector for {manifest.get('name', package_id)} — "
                    "inert until an admin activates its capability grant.",
        config=config,
        credentials={},          # never shipped by the package; admin adds later
        tenant_id=tenant_id,
        enabled=False,           # INERT — default-deny
    )
    session.add(connector)
    await session.flush()

    grant = MarketplaceCapabilityGrantModel(
        tenant_id=tenant_id,
        package_id=package_id,
        workspace_id=workspace_id,
        connector_id=connector.id,
        requested={
            "outbound_domains": list(manifest.get("outbound_domains", [])),
            "scopes":           list(manifest.get("scopes", [])),
        },
        granted={},
        status="pending",
        descriptor_sha256=manifest.get("_descriptor_sha256"),
        descriptor_format=execution.get("descriptor_format"),
        requested_by=requested_by,
    )
    session.add(grant)
    await session.flush()

    # The connector carries its grant anchor so the runtime can enforce and
    # log without a registry lookup. granted_domains stays ABSENT until
    # approval — the connector blocks everything meanwhile.
    connector.config = {**config, "_marketplace": {
        "package_id": package_id,
        "grant_id":   str(grant.id),
    }}
    logger.info("Layer-1 install: inert connector %s + pending grant %s for %s/%s",
                connector.id, grant.id, tenant_id, package_id)
    return grant


CONTAINER_DESCRIPTOR_FORMAT = "container_image"


async def _create_pending_container_grant(
    session: AsyncSession,
    *,
    tenant_id: str,
    package_id: str,
    workspace_id: uuid.UUID | None,
    manifest: dict,
    requested_by: str,
) -> MarketplaceCapabilityGrantModel:
    """Layer-2 install: declared container + pending grant — nothing runs.

    The registry allowlist is enforced HERE (fail-closed at install, from
    platform config, never the manifest). The image digest doubles as the
    drift anchor: any new digest is new code and is always held for human
    re-approval, both tiers.
    """
    from case_service.db.models import MarketplaceContainerModel
    from case_service.marketplace import runtime

    execution = manifest.get("execution") or {}
    image = execution["image"]
    digest = execution["image_digest"]
    registry = runtime.check_registry_allowed(image)   # raises Layer2Error

    grant = MarketplaceCapabilityGrantModel(
        tenant_id=tenant_id,
        package_id=package_id,
        workspace_id=workspace_id,
        connector_id=None,                             # containers, not connectors
        requested={
            "outbound_domains": list(manifest.get("outbound_domains", [])),
            "scopes":           list(manifest.get("scopes", [])),
            # Part of what the package asks to run — admin sees it in review.
            "execution": {
                "command": execution.get("command"),
                "env":     execution.get("env", {}),
                "port":    execution.get("port"),
            },
        },
        granted={},
        status="pending",
        descriptor_sha256=digest,
        descriptor_format=CONTAINER_DESCRIPTOR_FORMAT,
        requested_by=requested_by,
    )
    session.add(grant)
    await session.flush()

    session.add(MarketplaceContainerModel(
        tenant_id=tenant_id,
        package_id=package_id,
        grant_id=grant.id,
        image=image,
        image_digest=digest,
        registry=registry,
        status="declared",
        port=execution.get("port"),
    ))
    logger.info("Layer-2 install: declared container (%s@%s) + pending grant %s for %s/%s",
                image, digest[:19], grant.id, tenant_id, package_id)
    return grant


async def _container_row(session: AsyncSession, grant: MarketplaceCapabilityGrantModel):
    from case_service.db.models import MarketplaceContainerModel
    return (await session.execute(
        select(MarketplaceContainerModel).where(
            MarketplaceContainerModel.grant_id == grant.id)
    )).scalars().first()


def is_container_grant(grant: MarketplaceCapabilityGrantModel) -> bool:
    return grant.descriptor_format == CONTAINER_DESCRIPTOR_FORMAT


def _subset_or_raise(ticked: list, requested: list, what: str) -> list:
    extra = [d for d in ticked if d not in requested]
    if extra:
        raise ValueError(f"{what} not requested by the package: {', '.join(map(str, extra))}")
    return list(ticked)


async def approve_grant(
    session: AsyncSession,
    *,
    grant: MarketplaceCapabilityGrantModel,
    outbound_domains: list[str],
    scopes: list[str],
    admin_id: str,
    note: str | None = None,
) -> MarketplaceCapabilityGrantModel:
    """Activate exactly the ticked subset — approval is a grant, not trust."""
    requested = grant.requested or {}
    granted_domains = _subset_or_raise(
        outbound_domains, requested.get("outbound_domains", []), "outbound_domains")
    granted_scopes = _subset_or_raise(
        scopes, requested.get("scopes", []), "scopes")
    # Layer 2 runs egress-DROP by default — an empty domain grant is its most
    # locked-down (and normal) form. A Layer-1 connector with no reachable
    # domain is pointless — refuse that instead.
    if not granted_domains and not is_container_grant(grant):
        raise ValueError("an empty outbound_domains grant would activate a connector that can reach nothing — revoke instead")

    grant.granted = {"outbound_domains": granted_domains, "scopes": granted_scopes}
    if is_container_grant(grant):
        # Fail-closed ordering: the container must be running hardened before
        # the STATUS flips to granted (the broker refuses non-granted grants,
        # so the freshly-minted token in `granted` is inert until then).
        # Any Layer2Error aborts the approval.
        await _start_container_for_grant(session, grant, granted_domains)
    grant.status = "granted"
    grant.granted_by = admin_id
    grant.granted_at = _utcnow()
    grant.note = note

    connector = await session.get(ConnectorRegistryModel, grant.connector_id) if grant.connector_id else None
    if connector is not None:
        mkt = dict((connector.config or {}).get("_marketplace") or {})
        mkt["granted_domains"] = granted_domains
        connector.config = {**(connector.config or {}), "_marketplace": mkt}
        connector.enabled = True
    logger.info("Layer-%s grant %s APPROVED by %s: domains=%s scopes=%s",
                2 if is_container_grant(grant) else 1,
                grant.id, admin_id, granted_domains, granted_scopes)
    return grant


async def _start_container_for_grant(
    session: AsyncSession,
    grant: MarketplaceCapabilityGrantModel,
    granted_domains: list[str],
) -> None:
    """Pull (digest-pinned) > verify signature per policy > run hardened.
    Provenance and failure are both recorded on the container row."""
    import asyncio

    from case_service.marketplace import runtime

    row = await _container_row(session, grant)
    if row is None:
        raise ValueError("no container is declared for this grant")
    if row.container_id and runtime.container_running(row.container_id):
        return                                          # already up (idempotent)

    runtime.check_registry_allowed(row.image)           # config may have tightened
    try:
        verified = await asyncio.to_thread(
            runtime.verify_image_signature, row.image, row.image_digest)
        await asyncio.to_thread(runtime.pull_pinned_image, row.image, row.image_digest)
        row.pulled_at = _utcnow()
        row.signature_verified = verified

        broker_url, broker_token = await _broker_credentials(session, grant)
        declared = (grant.requested or {}).get("execution") or {}
        container_id = await asyncio.to_thread(
            lambda: runtime.provision_app_container(
                row_id=str(row.id), tenant_id=grant.tenant_id,
                package_id=grant.package_id, image=row.image,
                digest=row.image_digest, granted_domains=granted_domains,
                broker_url=broker_url, broker_token=broker_token,
                declared_env=declared.get("env") or {},
                declared_command=declared.get("command"),
                port=declared.get("port")))
        row.container_id = container_id
        row.status = "running"
        row.started_at = _utcnow()
        row.error = None
    except runtime.Layer2Error as exc:
        row.status = "failed"
        row.error = str(exc)[:500]
        raise
    except Exception as exc:
        row.status = "failed"
        row.error = str(exc)[:500]
        raise runtime.Layer2Error(f"Container start failed: {exc}") from exc


async def _broker_credentials(
    session: AsyncSession, grant: MarketplaceCapabilityGrantModel,
) -> tuple[str, str]:
    """Mint the container's broker token: opaque CSPRNG secret, hash-only
    storage on the grant (same posture as invite tokens), raw value injected
    once into the container env. Every container (re)start rotates it. The
    broker checks grant status per call — revocation is instant."""
    import hashlib
    import secrets

    from case_service.marketplace import runtime

    raw = secrets.token_urlsafe(32)
    grant.granted = {**(grant.granted or {}),
                     "broker_token_hash": hashlib.sha256(raw.encode()).hexdigest()}
    return runtime.BROKER_URL_IN_NETWORK, raw


async def revoke_grant(
    session: AsyncSession,
    *,
    grant: MarketplaceCapabilityGrantModel,
    admin_id: str,
    note: str | None = None,
) -> MarketplaceCapabilityGrantModel:
    """Instant inert: disable the connector / stop the container, strip the set."""
    grant.status = "revoked"
    grant.revoked_by = admin_id
    grant.revoked_at = _utcnow()
    if note:
        grant.note = note

    connector = await session.get(ConnectorRegistryModel, grant.connector_id) if grant.connector_id else None
    if connector is not None:
        mkt = dict((connector.config or {}).get("_marketplace") or {})
        mkt.pop("granted_domains", None)
        connector.config = {**(connector.config or {}), "_marketplace": mkt}
        connector.enabled = False

    if is_container_grant(grant):
        import asyncio

        from case_service.marketplace import runtime
        row = await _container_row(session, grant)
        if row is not None:
            if row.container_id:
                await asyncio.to_thread(runtime.stop_app_container, row.container_id)
            row.status = "stopped"
            row.stopped_at = _utcnow()
    logger.info("Grant %s REVOKED by %s", grant.id, admin_id)
    return grant


# ── P2: templated-connector drift (mig 123) ──
#
# A publisher-controlled schema change must never silently reshape the
# integration. New descriptor > regenerate mapping > classify > either
# auto-apply (additive AND official tier) or hold for admin re-approval
# while the old mapping keeps running.

def _op_index(config: dict) -> dict:
    return {str(op.get("operation_id")): op
            for op in (config.get("operations") or []) if op.get("operation_id")}


def classify_drift(
    old_config: dict, old_requested: dict,
    new_config: dict, new_requested: dict,
) -> tuple[str, list[str]]:
    """additive | widening | breaking, with human-readable reasons.

    Widening = the package asks to reach MORE (domains/scopes) than before.
    Breaking = the active operation changed shape or operations disappeared.
    Anything else (new operations, added response fields) = additive.
    """
    reasons: list[str] = []

    old_domains = set(old_requested.get("outbound_domains", []))
    new_domains = set(new_requested.get("outbound_domains", []))
    added_domains = sorted(new_domains - old_domains)
    if added_domains:
        reasons.append(f"new outbound domains requested: {', '.join(added_domains)}")

    old_scopes = set(old_requested.get("scopes", []))
    new_scopes = set(new_requested.get("scopes", []))
    added_scopes = sorted(new_scopes - old_scopes)
    if added_scopes:
        reasons.append(f"new scopes requested: {', '.join(added_scopes)}")

    if added_domains or added_scopes:
        return "widening", reasons

    breaking: list[str] = []
    if old_config.get("url") != new_config.get("url"):
        breaking.append(f"active operation URL changed: {old_config.get('url')} > {new_config.get('url')}")
    if old_config.get("method") != new_config.get("method"):
        breaking.append(f"active operation method changed: {old_config.get('method')} > {new_config.get('method')}")
    removed_ops = sorted(set(_op_index(old_config)) - set(_op_index(new_config)))
    if removed_ops:
        breaking.append(f"operations removed: {', '.join(removed_ops)}")
    removed_fields = sorted(set(old_config.get("response_mapping") or {})
                            - set(new_config.get("response_mapping") or {}))
    if removed_fields:
        breaking.append(f"response fields removed: {', '.join(removed_fields)}")
    if breaking:
        return "breaking", breaking

    return "additive", reasons or ["descriptor changed within the granted envelope"]


async def apply_descriptor_drift(
    session: AsyncSession,
    *,
    grant: MarketplaceCapabilityGrantModel,
    manifest: dict,
    publisher_tier: str,
) -> dict:
    """Handle a changed descriptor for an active grant.

    Returns {"action": "unchanged"|"auto_applied"|"pending_reapproval",
             "classification": ..., "reasons": [...]}.
    """
    if is_container_grant(grant):
        return _hold_image_drift(grant, manifest)

    new_sha = manifest.get("_descriptor_sha256")
    if not new_sha or new_sha == grant.descriptor_sha256:
        return {"action": "unchanged", "classification": None, "reasons": []}

    new_config = await _map_descriptor(manifest)
    new_requested = {
        "outbound_domains": list(manifest.get("outbound_domains", [])),
        "scopes":           list(manifest.get("scopes", [])),
    }
    connector = await session.get(ConnectorRegistryModel, grant.connector_id) if grant.connector_id else None
    old_config = {k: v for k, v in ((connector.config if connector else {}) or {}).items()
                  if k != "_marketplace"}

    classification, reasons = classify_drift(
        old_config, grant.requested or {}, new_config, new_requested)

    # Community packages NEVER auto-apply — every change is re-reviewed.
    if classification == "additive" and publisher_tier == "official":
        if connector is not None:
            mkt = (connector.config or {}).get("_marketplace") or {}
            connector.config = {**new_config, "_marketplace": mkt}
        grant.requested = new_requested
        grant.descriptor_sha256 = new_sha
        grant.descriptor_format = (manifest.get("execution") or {}).get("descriptor_format")
        grant.mapping_version = (grant.mapping_version or 1) + 1
        logger.info("Layer-1 drift AUTO-APPLIED (additive, official) for grant %s: %s",
                    grant.id, "; ".join(reasons))
        return {"action": "auto_applied", "classification": classification, "reasons": reasons}

    grant.proposed = {
        "config":            new_config,
        "requested":         new_requested,
        "descriptor_sha256": new_sha,
        "descriptor_format": (manifest.get("execution") or {}).get("descriptor_format"),
        "classification":    classification,
        "reasons":           reasons,
        "from_version":      manifest.get("version"),
    }
    grant.status = "pending_reapproval"
    logger.info("Layer-1 drift HELD for re-approval (%s) for grant %s: %s",
                classification, grant.id, "; ".join(reasons))
    return {"action": "pending_reapproval", "classification": classification, "reasons": reasons}


def _hold_image_drift(grant: MarketplaceCapabilityGrantModel, manifest: dict) -> dict:
    """Layer-2 drift: a new image digest is NEW CODE. It is never additive —
    human review is mandatory for code-bearing packages, both tiers."""
    execution = manifest.get("execution") or {}
    new_digest = execution.get("image_digest")
    if not new_digest or new_digest == grant.descriptor_sha256:
        return {"action": "unchanged", "classification": None, "reasons": []}
    reasons = [f"container image changed: {grant.descriptor_sha256} > {new_digest}"]
    grant.proposed = {
        "image":             execution.get("image"),
        "image_digest":      new_digest,
        "requested": {
            "outbound_domains": list(manifest.get("outbound_domains", [])),
            "scopes":           list(manifest.get("scopes", [])),
            "execution": {
                "command": execution.get("command"),
                "env":     execution.get("env", {}),
                "port":    execution.get("port"),
            },
        },
        "classification":    "image_changed",
        "reasons":           reasons,
        "from_version":      manifest.get("version"),
    }
    grant.status = "pending_reapproval"
    logger.info("Layer-2 drift HELD (new code) for grant %s: %s", grant.id, reasons[0])
    return {"action": "pending_reapproval", "classification": "image_changed",
            "reasons": reasons}


async def approve_drift(
    session: AsyncSession,
    *,
    grant: MarketplaceCapabilityGrantModel,
    outbound_domains: list[str],
    scopes: list[str],
    admin_id: str,
    note: str | None = None,
) -> MarketplaceCapabilityGrantModel:
    """Apply the held mapping with the admin-ticked subset of the NEW request."""
    proposed = grant.proposed or {}
    if grant.status != "pending_reapproval" or not proposed:
        raise ValueError("no drift is pending re-approval on this grant")
    new_requested = proposed.get("requested") or {}
    granted_domains = _subset_or_raise(
        outbound_domains, new_requested.get("outbound_domains", []), "outbound_domains")
    granted_scopes = _subset_or_raise(scopes, new_requested.get("scopes", []), "scopes")
    if not granted_domains and not is_container_grant(grant):
        raise ValueError("an empty outbound_domains grant would activate a connector that can reach nothing — revoke instead")

    grant.requested = new_requested
    grant.granted = {"outbound_domains": granted_domains, "scopes": granted_scopes}

    if is_container_grant(grant):
        # New code: stop the old container, restart on the approved digest.
        import asyncio

        from case_service.marketplace import runtime
        row = await _container_row(session, grant)
        if row is None:
            raise ValueError("no container is declared for this grant")
        if row.container_id:
            await asyncio.to_thread(runtime.stop_app_container, row.container_id)
            row.container_id = None
        if proposed.get("image"):
            row.image = proposed["image"]
        row.image_digest = proposed.get("image_digest") or row.image_digest
        row.registry = runtime.registry_of(row.image)
        row.status = "declared"
        grant.descriptor_sha256 = row.image_digest
        await _start_container_for_grant(session, grant, granted_domains)
    else:
        connector = await session.get(ConnectorRegistryModel, grant.connector_id) if grant.connector_id else None
        if connector is not None:
            mkt = dict(((connector.config or {}).get("_marketplace")) or {})
            mkt["granted_domains"] = granted_domains
            connector.config = {**(proposed.get("config") or {}), "_marketplace": mkt}
            connector.enabled = True
        grant.descriptor_sha256 = proposed.get("descriptor_sha256")
        grant.descriptor_format = proposed.get("descriptor_format")

    grant.mapping_version = (grant.mapping_version or 1) + 1
    grant.proposed = None
    grant.status = "granted"
    grant.granted_by = admin_id
    grant.granted_at = _utcnow()
    if note:
        grant.note = note
    logger.info("Layer-1 drift APPROVED by %s for grant %s: domains=%s",
                admin_id, grant.id, granted_domains)
    return grant


async def reject_drift(
    session: AsyncSession,
    *,
    grant: MarketplaceCapabilityGrantModel,
    admin_id: str,
    note: str | None = None,
) -> MarketplaceCapabilityGrantModel:
    """Discard the held mapping — the live one was never touched."""
    if grant.status != "pending_reapproval":
        raise ValueError("no drift is pending re-approval on this grant")
    grant.proposed = None
    grant.status = "granted" if grant.granted_at else "pending"
    if note:
        grant.note = note
    logger.info("Layer-1 drift REJECTED by %s for grant %s", admin_id, grant.id)
    return grant


def grant_view(g: MarketplaceCapabilityGrantModel) -> dict:
    return {
        "id":                str(g.id),
        "tenant_id":         g.tenant_id,
        "package_id":        g.package_id,
        "install_id":        str(g.install_id) if g.install_id else None,
        "connector_id":      str(g.connector_id) if g.connector_id else None,
        "requested":         g.requested or {},
        "granted":           g.granted or {},
        "proposed":          g.proposed,
        "status":            g.status,
        "descriptor_sha256": g.descriptor_sha256,
        "descriptor_format": g.descriptor_format,
        "mapping_version":   g.mapping_version,
        "requested_by":      g.requested_by,
        "requested_at":      g.requested_at.isoformat() if g.requested_at else None,
        "granted_by":        g.granted_by,
        "granted_at":        g.granted_at.isoformat() if g.granted_at else None,
        "revoked_by":        g.revoked_by,
        "revoked_at":        g.revoked_at.isoformat() if g.revoked_at else None,
        "note":              g.note,
    }


# ── runtime enforcement helpers (called from http_custom_connector) ──

def host_allowed(host: str, granted_domains: list[str]) -> bool:
    """Exact host or subdomain of a granted domain — never substring tricks."""
    host = (host or "").lower().rstrip(".")
    for d in granted_domains:
        d = str(d).lower().rstrip(".")
        if host == d or host.endswith("." + d):
            return True
    return False


async def log_marketplace_call(
    *,
    grant_id: str | None,
    package_id: str,
    url: str,
    method: str,
    status: str,               # allowed | blocked
    http_status_code: int | None = None,
    is_declared: bool = True,
) -> None:
    """Audit every capability use — own session, never blocks the call path.
    One retry: a transient lock (shared dev SQLite) must not lose audit rows."""
    import asyncio

    from case_service.db.models import MarketplaceNetworkLogModel
    from case_service.db.session import get_session_factory
    for attempt in (1, 2):
        try:
            factory = get_session_factory()
            async with factory() as session:
                session.add(MarketplaceNetworkLogModel(
                    workspace_id=None,
                    grant_id=uuid.UUID(grant_id) if grant_id else None,
                    package_id=package_id,
                    destination_url=url[:1024],
                    http_method=method[:16],
                    status=status,
                    http_status_code=http_status_code,
                    is_declared=is_declared,
                ))
                await session.commit()
            return
        except Exception as exc:
            if attempt == 2:
                logger.warning("marketplace network log write failed: %s", exc)
            else:
                await asyncio.sleep(0.05)
