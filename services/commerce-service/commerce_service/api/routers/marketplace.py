"""P-Marketplace — Velaris App Store API.

Architecture:
  - Publishers host their own repo with a velaris.json manifest + .hxapp releases.
  - Admins register publisher source URLs in Studio (no Velaris repo involvement).
  - Velaris polls each source on its own schedule and caches packages locally.
  - All hardcoded URLs/intervals are read from config.py / .env — nothing is hardcoded here.

Endpoints:
  Sources
    GET    /marketplace/sources               List registered sources
    POST   /marketplace/sources               Add a new source (admin)
    DELETE /marketplace/sources/{id}          Remove a source (admin)
    POST   /marketplace/sources/{id}/sync     Force-poll one source (admin)
    POST   /marketplace/sources/sync-all      Force-poll all sources (admin)

  Packages
    GET    /marketplace/packages              List all cached packages
    GET    /marketplace/packages/{id}         Package detail + version history
    POST   /marketplace/packages/refresh      Force full re-fetch (admin)

  Workspaces (sandbox)
    GET    /marketplace/workspaces            List caller's workspaces
    POST   /marketplace/workspaces            Create workspace
    DELETE /marketplace/workspaces/{id}       Destroy workspace
    POST   /marketplace/workspaces/{id}/install      Install package into workspace
    POST   /marketplace/workspaces/{id}/submit       Mark ready for admin review
    GET    /marketplace/workspaces/{id}/network-log  Outbound call log

  Whitelist
    POST   /marketplace/workspaces/{id}/whitelist   Request domain whitelist
    PATCH  /marketplace/whitelist/{id}               Admin approve/deny

  Review Queue
    GET    /marketplace/review-queue                 Admin: pending workspaces
    POST   /marketplace/review-queue/{id}/approve    Approve workspace/items
    POST   /marketplace/review-queue/{id}/reject     Reject with reason

  Production installs
    GET    /marketplace/installs             Installed packages for this tenant
    DELETE /marketplace/installs/{id}        Revoke + decommission installed package (admin)

  Official package release requests
    POST   /marketplace/packages/{id}/request-release  Flag official package for next HxDeploy cycle
    GET    /marketplace/release-requests               List pending official package release requests (admin)

  Source decommissioning
    GET    /marketplace/sources/{id}/decommission-preview  Preview impact before removing source
    POST   /marketplace/sources/{id}/decommission          Execute decommissioning + remove source

  Updates
    GET    /marketplace/updates              Pending update notifications (admin)
    POST   /marketplace/updates/{id}/approve Fast-track approve (no sandbox)
    POST   /marketplace/updates/{id}/dismiss Dismiss update notification

  Sandbox datasets
    GET    /marketplace/sandbox-datasets     List admin-defined datasets

  Blacklist (tenant-managed, per-instance)
    GET    /marketplace/blacklist            List blacklisted entries (admin)
    POST   /marketplace/blacklist            Add blacklist entry (admin)
    DELETE /marketplace/blacklist/{id}       Remove blacklist entry (admin)

  Access rules (per-access-group install restrictions)
    GET    /marketplace/access-rules         List all access group rules (admin)
    PUT    /marketplace/access-rules/{access_group_id}  Set rule for a group (admin)
    DELETE /marketplace/access-rules/{access_group_id}  Reset to allow_all (admin)
"""
from __future__ import annotations

import re
import json
import uuid
import logging
from datetime import datetime, timezone, timedelta
from typing import Any

import httpx
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared import hxguard
from velaris_shared.auth.dependencies import get_current_user
from velaris_shared.auth.models import AuthenticatedUser
from velaris_shared.config import get_settings
from velaris_shared.db.session import get_session
from velaris_shared.db.models import (
    MarketplaceSourceModel,
    MarketplacePackageCacheModel,
    MarketplaceWorkspaceModel,
    MarketplaceWorkspaceItemModel,
    MarketplaceInstallModel,
    MarketplaceNetworkLogModel,
    MarketplaceWhitelistModel,
    MarketplaceUpdateModel,
    MarketplaceSandboxDatasetModel,
    MarketplaceBlacklistModel,
    MarketplaceAccessRuleModel,
)
from velaris_shared.core.error_codes import (
    MKT_ACCESS_403,
    MKT_BLOCKED_403,
    MKT_CONFORMANCE_422,
    MKT_DISABLED_404,
    MKT_GRANT_409,
    MKT_INSTALL_400,
    MKT_INSTALLED_409,
    MKT_NOTFOUND_404,
    MKT_OFFICIAL_400,
    MKT_PACK_409,
    MKT_QUOTA_429,
    MKT_SANDBOX_400,
    MKT_STATE_400,
    MKT_VERIFY_502,
    coded,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/marketplace", tags=["marketplace"])

# ── Auth helpers ──────────────────────────────────────────────────────────────

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _require_developer_or_admin(user: AuthenticatedUser) -> None:
    """Phase 4 step 2 — was `is_admin or "developer" in roles`."""
    if not hxguard.user_allows(user, "marketplace.packages.publish"):
        raise coded(403, MKT_ACCESS_403,
                    "Requires the marketplace.packages.publish privilege")


def _require_admin(user: AuthenticatedUser) -> None:
    """Marketplace administration — 33 routes, one bare `admin` check until now."""
    if not hxguard.user_allows(user, "marketplace.admin.access"):
        raise coded(403, MKT_ACCESS_403,
                    "Requires the marketplace.admin.access privilege")


async def _require_operator(session: AsyncSession, user: AuthenticatedUser) -> None:
    """Marketplace SOURCES are platform-wide (8e decision c, 2026-10-01): every
    tenant's catalogue is fed from them. A tenant admin with the marketplace
    privilege could add, remove, sync or decommission them for everybody. They
    are the platform operator's — on a single-tenant install, every admin."""
    from velaris_shared.tenancy.identity import is_platform_operator
    if not await is_platform_operator(session, user):
        raise coded(403, MKT_ACCESS_403,
                    "Marketplace sources are shared by every tenant; only the "
                    "platform operator may change them")


def _can_view(user: AuthenticatedUser) -> None:
    from velaris_shared.core.feature_flags import is_feature_enabled
    if get_settings().marketplace_kill_switch or not is_feature_enabled("marketplace"):
        raise coded(404, MKT_DISABLED_404, "Marketplace is not available on this instance")
    if not hxguard.user_allows(user, "marketplace.catalogue.read"):
        raise coded(403, MKT_ACCESS_403, "Marketplace is not accessible for your role")


# ── AES-256-GCM token encryption ─────────────────────────────────────────────

def _get_encryption_key() -> bytes | None:
    """Return 32-byte key from config, or None if not configured (dev mode)."""
    settings = get_settings()
    key_hex = settings.marketplace_token_key or settings.storage_master_key
    if not key_hex or len(key_hex) < 64:
        return None
    return bytes.fromhex(key_hex[:64])


def _encrypt_token(token: str) -> str:
    """Encrypt a source PAT or licence key with AES-256-GCM.

    When no dedicated key is configured this used to store the token as plain
    **base64** — a personal access token sitting in the database in a form
    anyone can read, labelled `b64:` as if that were a cipher. An install that
    never set `VELARIS_CASE_MARKETPLACE_TOKEN_KEY` or
    `VELARIS_CASE_STORAGE_MASTER_KEY` had no encryption here at all and nothing
    said so.

    The fallback is now hxv3 (AES-256-GCM under a key derived from
    `auth_secret`, which every install has), so there is no configuration under
    which this writes plaintext. Existing `b64:` values are still read — see
    `_decrypt_token` — because refusing them would lock an install out of its
    own sources.
    """
    key = _get_encryption_key()
    if not key:
        import json as _json

        from velaris_shared.hxbridge.encryption import encrypt_credentials
        return "hxv3json:" + _json.dumps(encrypt_credentials({"t": token}))

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    import os, base64
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, token.encode(), None)
    return "aes:" + base64.b64encode(nonce + ct).decode()


def _decrypt_token(enc: str) -> str:
    if not enc:
        return enc
    if enc.startswith("hxv3json:"):
        import json as _json

        from velaris_shared.hxbridge.encryption import decrypt_credentials
        try:
            return decrypt_credentials(_json.loads(enc[len("hxv3json:"):]))["t"]
        except Exception:
            return enc
    if enc.startswith("b64:"):   # legacy plaintext — read, never written again
        import base64
        try:
            return base64.b64decode(enc[4:].encode()).decode()
        except Exception:
            return enc
    if enc.startswith("aes:"):
        key = _get_encryption_key()
        if not key:
            return enc   # can't decrypt without key
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        import base64
        try:
            raw = base64.b64decode(enc[4:].encode())
            nonce, ct = raw[:12], raw[12:]
            return AESGCM(key).decrypt(nonce, ct, None).decode()
        except Exception:
            return enc
    # Legacy base64 (no prefix)
    import base64
    try:
        return base64.b64decode(enc.encode()).decode()
    except Exception:
        return enc


# ── Source fetching ────────────────────────────────────────────────────────────

async def _fetch_source(url: str, token: str | None = None) -> dict[str, Any]:
    """Fetch a velaris.json manifest from a publisher's URL.

    Supports:
      - Public URLs (no auth)
      - Private GitHub repos via Personal Access Token
    """
    headers: dict[str, str] = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    settings = get_settings()
    timeout = 15.0

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        r = await client.get(url, headers=headers)
        r.raise_for_status()
        return r.json()


async def _fetch_official_index() -> list[dict[str, Any]]:
    """Fetch the official Velaris marketplace index (sources.json).
    URL comes from config — never hardcoded here.
    """
    from velaris_shared.core.feature_flags import is_feature_enabled
    settings = get_settings()
    if settings.marketplace_kill_switch or not is_feature_enabled("marketplace"):
        return []
    try:
        data = await _fetch_source(settings.marketplace_index_url)
        return data.get("sources", [])
    except Exception as exc:
        logger.warning("Failed to fetch official marketplace index from %s: %s",
                       settings.marketplace_index_url, exc)
        return []


def _effective_tier(source_url: str, package_id: str = "") -> str:
    """Determine package tier — never from the manifest. Official requires ALL of:

      1. the source URL's GitHub org is in the configured official orgs,
      2. the source URL is under the write-protected `official/` folder, AND
      3. the package id is in the baked-in official registry (`official_registry.json`).

    All three matter because Official and Community now live in the SAME repo/org
    (`Velaris-App-OS/Marketplace`, folders `official/` + `community/`) — the org
    check alone would bless the whole repo, and even the registry id alone could be
    claimed by a community entry (the community index shares the org). The
    write-protected `official/` folder is the real boundary (only the Velaris org
    can publish there); the registry is the per-id allowlist on top. A manifest
    cannot self-tag, and a community contributor cannot append to the registry (a
    platform-release change), so Official is unspoofable. Works for github.com and
    raw.githubusercontent.com URLs.

    A missing/empty `package_id` (callers that haven't resolved the id yet) can
    never be Official — fail-closed.
    """
    from commerce_service.marketplace.official_registry import is_registered_official

    settings = get_settings()
    official_orgs = {o.strip().lower() for o in settings.marketplace_official_orgs.split(",") if o.strip()}

    # Extract org + path segments from GitHub-style URLs: domain/{org}/{repo}/...
    try:
        from urllib.parse import urlparse
        path_parts = [p.lower() for p in urlparse(source_url).path.strip("/").split("/")]
        org = path_parts[0] if path_parts else ""
        in_official_folder = "official" in path_parts
        if org in official_orgs and in_official_folder and package_id and is_registered_official(package_id):
            return "official"
    except Exception:
        pass
    return "community"


async def _is_blacklisted(
    tenant_id: str,
    package_id: str,
    source_url: str,
    session: AsyncSession,
) -> tuple[bool, str]:
    """Check both DB blacklist (tenant-managed) and global blacklist.
    Returns (is_blacklisted, reason).
    """
    from urllib.parse import urlparse
    path_parts = urlparse(source_url).path.strip("/").split("/")
    org = path_parts[0].lower() if path_parts else ""

    result = await session.execute(
        select(MarketplaceBlacklistModel).where(
            MarketplaceBlacklistModel.tenant_id == tenant_id,
            # Match org, source URL, or package ID
            (MarketplaceBlacklistModel.type == "org") & (MarketplaceBlacklistModel.value == org) |
            (MarketplaceBlacklistModel.type == "source") & (MarketplaceBlacklistModel.value == source_url) |
            (MarketplaceBlacklistModel.type == "package") & (MarketplaceBlacklistModel.value == package_id),
        )
    )
    entry = result.scalars().first()
    if entry:
        return True, entry.reason
    return False, ""


async def _check_access_rule(
    tenant_id: str,
    access_group_id: str | None,
    package_id: str,
    publisher_tier: str,
    session: AsyncSession,
) -> tuple[bool, str]:
    """Check if this developer's access group is allowed to install this package.
    Returns (allowed, reason).
    """
    if not access_group_id:
        return True, ""   # no access group — no restriction

    result = await session.execute(
        select(MarketplaceAccessRuleModel).where(
            MarketplaceAccessRuleModel.tenant_id == tenant_id,
            MarketplaceAccessRuleModel.access_group_id == access_group_id,
        )
    )
    rule = result.scalar_one_or_none()
    if not rule or rule.rule_type == "allow_all":
        return True, ""

    if rule.rule_type == "official_only":
        if publisher_tier != "official":
            return False, "Your access group can only install Official packages. Ask your admin to change your access rule."
        return True, ""

    if rule.rule_type == "allowlist":
        allowed = json.loads(rule.allowed_package_ids or "[]")
        if package_id not in allowed:
            return False, f"Package '{package_id}' is not on the allowlist for your access group. Ask your admin."
        return True, ""

    if rule.rule_type == "blocklist":
        blocked = json.loads(rule.blocked_package_ids or "[]")
        if package_id in blocked:
            return False, f"Package '{package_id}' is blocked for your access group."
        return True, ""

    return True, ""


async def _report_to_velaris(event: str, package_id: str, source_url: str, detail: str) -> None:
    """Optionally report a security event to the Velaris team webhook.
    Opt-in via VELARIS_CASE_MARKETPLACE_REPORT_WEBHOOK env var.
    Only package metadata sent — no customer data, tenant ID is hashed.
    """
    settings = get_settings()
    webhook_url = getattr(settings, "marketplace_report_webhook", "")
    secret      = getattr(settings, "marketplace_report_webhook_secret", "")
    if not webhook_url:
        return
    import hashlib, hmac, time
    payload = {
        "event":       event,
        "package_id":  package_id,
        "source_url":  source_url,
        "detail":      detail,
        "reported_at": _utcnow().isoformat(),
    }
    body = json.dumps(payload).encode()
    sig  = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest() if secret else ""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                webhook_url,
                content=body,
                headers={
                    "Content-Type":      "application/json",
                    "X-Velaris-Event":   event,
                    "X-Velaris-Sig":     sig,
                },
            )
    except Exception as exc:
        logger.debug("Velaris report webhook failed (non-critical): %s", exc)


async def _suspend_workspaces_for_blacklisted_package(
    package_id: str, tenant_id: str, session: AsyncSession
) -> int:
    """Suspend all active sandboxes using a newly blacklisted package."""
    items_result = await session.execute(
        select(MarketplaceWorkspaceItemModel).where(
            MarketplaceWorkspaceItemModel.package_id == package_id
        )
    )
    suspended = 0
    for item in items_result.scalars().all():
        ws = await session.get(MarketplaceWorkspaceModel, item.workspace_id)
        if ws and ws.tenant_id == tenant_id and ws.status in ("active", "submitted"):
            ws.status = "destroyed"
            suspended += 1
            logger.warning("Blacklist: suspended sandbox workspace %s (package %s)", ws.id, package_id)
    return suspended


async def _poll_source(
    source: MarketplaceSourceModel,
    session: AsyncSession,
) -> int:
    """Fetch a source's velaris.json and upsert packages into the cache.
    Returns the number of packages upserted.
    """
    token = _decrypt_token(source.token_enc) if source.token_enc else None
    try:
        manifest = await _fetch_source(source.url, token)
    except Exception as exc:
        source.last_error = str(exc)
        source.last_polled_at = _utcnow()
        await session.commit()
        logger.warning("Failed to poll source %s (%s): %s", source.name, source.url, exc)
        return 0

    # velaris.json can describe a single package or a list
    packages_raw: list[dict] = []
    if isinstance(manifest, list):
        packages_raw = manifest
    elif "packages" in manifest:
        packages_raw = manifest["packages"]
    elif "id" in manifest:
        # Single-package manifest (most common)
        packages_raw = [manifest]

    upserted = 0
    for raw in packages_raw:
        pkg_id = raw.get("id", "")
        if not pkg_id:
            continue

        # Determine latest version from versions array or top-level version field
        versions: list[dict] = raw.get("versions", [])
        latest_ver = raw.get("latest_version") or raw.get("version", "")
        latest_entry = next(
            (v for v in versions if v.get("version") == latest_ver),
            versions[0] if versions else {}
        )

        existing = await session.get(MarketplacePackageCacheModel, pkg_id)
        if existing:
            # Check if version bumped
            old_version = existing.version
            new_version = latest_ver or existing.version
            existing.version          = new_version
            existing.description      = raw.get("description", existing.description)
            existing.rating           = float(raw.get("rating", existing.rating))
            existing.installs         = int(raw.get("installs", existing.installs))
            existing.download_url     = latest_entry.get("download_url", existing.download_url)
            existing.checksum_sha256  = latest_entry.get("checksum_sha256", existing.checksum_sha256)
            existing.outbound_domains = json.dumps(latest_entry.get("outbound_domains", json.loads(existing.outbound_domains)))
            existing.release_notes    = latest_entry.get("release_notes")
            existing.all_versions     = json.dumps(versions)
            existing.updated_at       = latest_entry.get("released_at", existing.updated_at)
            # Tier derived from source URL org + baked official registry — never the manifest
            existing.publisher_tier   = _effective_tier(source.url, pkg_id)
            existing.source_id        = source.id
            existing.fetched_at       = _utcnow()

            # If version bumped, record update availability for all tenants with this installed
            if old_version and old_version != new_version:
                await _record_update_available(
                    pkg_id=pkg_id,
                    installed_version=old_version,
                    available_version=new_version,
                    release_notes=latest_entry.get("release_notes"),
                    new_outbound_domains=latest_entry.get("outbound_domains", []),
                    session=session,
                )
        else:
            row = MarketplacePackageCacheModel(
                id=pkg_id,
                name=raw.get("name", pkg_id),
                description=raw.get("description", ""),
                package_type=raw.get("type", "connector"),
                category=raw.get("category", ""),
                publisher=raw.get("publisher", source.name),
                # Tier derived from source URL org + baked official registry — manifest ignored
                publisher_tier=_effective_tier(source.url, pkg_id),
                version=latest_ver,
                price=raw.get("price", "free"),
                price_label=raw.get("price_label"),
                contact_url=raw.get("contact_url"),
                rating=float(raw.get("rating", 0.0)),
                installs=int(raw.get("installs", 0)),
                download_url=latest_entry.get("download_url", ""),
                checksum_sha256=latest_entry.get("checksum_sha256", ""),
                outbound_domains=json.dumps(latest_entry.get("outbound_domains", [])),
                tags=json.dumps(raw.get("tags", [])),
                icon_color=raw.get("icon_color"),
                icon_letter=raw.get("icon_letter"),
                min_platform_version=raw.get("min_platform_version", "1.0.0"),
                updated_at=latest_entry.get("released_at"),
                release_notes=latest_entry.get("release_notes"),
                all_versions=json.dumps(versions),
                source_id=source.id,
            )
            session.add(row)
        upserted += 1

    source.last_polled_at = _utcnow()
    source.last_error = None
    source.package_count = upserted
    await session.commit()
    return upserted


async def _record_update_available(
    pkg_id: str,
    installed_version: str,
    available_version: str,
    release_notes: str | None,
    new_outbound_domains: list[str],
    session: AsyncSession,
) -> None:
    """Record that a new version is available for all tenants that have this installed."""
    installs_result = await session.execute(
        select(MarketplaceInstallModel).where(
            MarketplaceInstallModel.package_id == pkg_id,
            MarketplaceInstallModel.revoked_at.is_(None),
        )
    )
    installs = installs_result.scalars().all()

    for install in installs:
        # Get the currently-installed outbound domains from the package cache
        pkg = await session.get(MarketplacePackageCacheModel, pkg_id)
        old_domains = set(json.loads(pkg.outbound_domains) if pkg else [])
        added_domains = [d for d in new_outbound_domains if d not in old_domains]
        fast_track = len(added_domains) == 0

        # Upsert update record for this tenant
        existing_update = await session.execute(
            select(MarketplaceUpdateModel).where(
                MarketplaceUpdateModel.tenant_id == install.tenant_id,
                MarketplaceUpdateModel.package_id == pkg_id,
                MarketplaceUpdateModel.status == "pending",
            )
        )
        upd = existing_update.scalar_one_or_none()
        if upd:
            upd.available_version    = available_version
            upd.release_notes        = release_notes
            upd.new_outbound_domains = json.dumps(added_domains)
            upd.fast_track           = fast_track
            upd.detected_at          = _utcnow()
        else:
            session.add(MarketplaceUpdateModel(
                tenant_id=install.tenant_id,
                package_id=pkg_id,
                installed_version=installed_version,
                available_version=available_version,
                release_notes=release_notes,
                new_outbound_domains=json.dumps(added_domains),
                fast_track=fast_track,
            ))


async def _ensure_sources_seeded(session: AsyncSession) -> None:
    """Seed the Velaris official (and community) indexes as sources, once.

    Both live in the Velaris-App-OS/Marketplace repo (folders official/ +
    community/). The seeded `tier` is a display hint only — actual tier is always
    re-derived per package by `_effective_tier` (org + registry)."""
    result = await session.execute(select(MarketplaceSourceModel))
    if result.scalars().first():
        return   # already seeded

    settings = get_settings()
    session.add(MarketplaceSourceModel(
        name="Velaris Official",
        url=settings.marketplace_index_url,
        tier="official",
        poll_interval_hours=settings.marketplace_official_poll_interval_hours,
        added_by="system",
    ))
    if settings.marketplace_community_index_url:
        session.add(MarketplaceSourceModel(
            name="Velaris Community",
            url=settings.marketplace_community_index_url,
            tier="community",
            poll_interval_hours=settings.marketplace_poll_interval_hours,
            added_by="system",
        ))
    await session.commit()


async def _get_packages(session: AsyncSession) -> list[MarketplacePackageCacheModel]:
    """Return cached packages, polling stale sources first."""
    settings = get_settings()
    await _ensure_sources_seeded(session)

    sources_result = await session.execute(
        select(MarketplaceSourceModel).where(MarketplaceSourceModel.enabled.is_(True))
    )
    sources = sources_result.scalars().all()

    for source in sources:
        cutoff = _utcnow() - timedelta(hours=source.poll_interval_hours)
        if not source.last_polled_at or source.last_polled_at < cutoff:
            await _poll_source(source, session)

    result = await session.execute(select(MarketplacePackageCacheModel))
    return result.scalars().all()


def _pkg_to_dict(p: MarketplacePackageCacheModel, include_versions: bool = False) -> dict:
    d = {
        "id":                   p.id,
        "name":                 p.name,
        "description":          p.description,
        "type":                 p.package_type,
        "category":             p.category,
        "publisher":            p.publisher,
        "publisher_tier":       p.publisher_tier,
        "version":              p.version,
        "price":                p.price,
        "price_label":          p.price_label,
        "contact_url":          p.contact_url,
        "rating":               p.rating,
        "installs":             p.installs,
        "download_url":         p.download_url,
        "checksum_sha256":      p.checksum_sha256,
        "outbound_domains":     json.loads(p.outbound_domains or "[]"),
        "tags":                 json.loads(p.tags or "[]"),
        "icon_color":           p.icon_color,
        "icon_letter":          p.icon_letter,
        "min_platform_version": p.min_platform_version,
        "updated_at":           p.updated_at,
        "release_notes":        p.release_notes,
        "source_id":            str(p.source_id) if p.source_id else None,
    }
    if include_versions:
        d["versions"] = json.loads(p.all_versions or "[]")
    return d


# ── Request models ────────────────────────────────────────────────────────────

class AddSourceReq(BaseModel):
    name: str
    url: str
    tier: str = "community"        # community | private
    token: str | None = None       # PAT for private repos — stored encrypted
    poll_interval_hours: int = 6

class CreateWorkspaceReq(BaseModel):
    name: str
    dataset_id: str | None = None

class InstallPackageReq(BaseModel):
    package_id: str
    licence_key: str | None = None

class SubmitWorkspaceReq(BaseModel):
    notes: str | None = None

class WhitelistReq(BaseModel):
    domain: str
    package_id: str
    justification: str | None = None

class ApproveReq(BaseModel):
    package_ids: list[str] | None = None   # None = approve all

class RejectReq(BaseModel):
    reason: str

class WhitelistDecisionReq(BaseModel):
    decision: str   # approved | denied


# ══════════════════════════════════════════════════════════════════════════════
#  SOURCE ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

_TAGS_FILE  = Path(__file__).parent.parent.parent / "marketplace" / "tags.json"
_TYPES_FILE = Path(__file__).parent.parent.parent / "marketplace" / "types.json"
_tags_data:  dict | None = None
_types_data: dict | None = None   # loaded once at first request, stays in memory


@router.get("/tags")
async def get_tags(
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Return the predefined tag taxonomy.

    Loaded from commerce_service/marketplace/tags.json — part of the Velaris platform,
    versioned and shipped with each release. No network call, works air-gapped.
    """
    global _tags_data
    _can_view(user)

    if _tags_data is None:
        try:
            with open(_TAGS_FILE) as f:
                _tags_data = json.load(f)
        except Exception as exc:
            logger.error("Could not load marketplace tags.json: %s", exc)
            return {"categories": {}, "all_tags": []}

    return _tags_data


@router.get("/types")
async def get_types(
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Return the predefined package type registry.

    Loaded from commerce_service/marketplace/types.json — part of the Velaris platform,
    versioned and shipped with each release. No network call, works air-gapped.
    """
    global _types_data
    _can_view(user)

    if _types_data is None:
        try:
            with open(_TYPES_FILE) as f:
                _types_data = json.load(f)
        except Exception as exc:
            logger.error("Could not load marketplace types.json: %s", exc)
            return {"categories": []}

    return _types_data


@router.get("/config")
async def get_marketplace_config(
    user: AuthenticatedUser = Depends(get_current_user),
):
    """Return marketplace runtime config values the frontend needs (limit, dev-only flag)."""
    _can_view(user)
    settings = get_settings()
    return {
        "dev_only":                 settings.marketplace_dev_only,
        "max_workspaces_per_user":  settings.marketplace_max_workspaces_per_user,
        "workspace_expiry_days":    settings.marketplace_workspace_expiry_days,
        "source_stale_days":        settings.marketplace_source_stale_days,
        "enabled":                  settings.marketplace_kill_switch is False,
    }


@router.get("/sources")
async def list_sources(
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _require_admin(user)
    await _ensure_sources_seeded(session)
    result = await session.execute(select(MarketplaceSourceModel).order_by(MarketplaceSourceModel.created_at))
    sources = result.scalars().all()
    settings = get_settings()
    stale_threshold = timedelta(days=settings.marketplace_source_stale_days)
    now = _utcnow()

    return {"sources": [
        {
            "id":                  str(s.id),
            "name":                s.name,
            "url":                 s.url,
            "tier":                s.tier,
            "has_token":           bool(s.token_enc),
            "poll_interval_hours": s.poll_interval_hours,
            "enabled":             s.enabled,
            "last_polled_at":      s.last_polled_at.isoformat() if s.last_polled_at else None,
            "last_error":          s.last_error,
            "package_count":       s.package_count,
            "added_by":            s.added_by,
            "created_at":          s.created_at.isoformat(),
            # Stale = last successful poll was more than N days ago (or never polled)
            "is_stale":            (
                s.last_polled_at is None or
                (s.last_error is not None and (now - s.last_polled_at) > stale_threshold)
            ),
            "stale_since_days":    (
                int((now - s.last_polled_at).days)
                if s.last_polled_at and s.last_error
                else None
            ),
        }
        for s in sources
    ]}


@router.post("/sources")
async def add_source(
    body: AddSourceReq,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _require_admin(user)
    await _require_operator(session, user)   # 8e (c): sources are the install's
    if body.tier not in ("community", "private"):
        raise HTTPException(400, "tier must be 'community' or 'private'")
    if body.poll_interval_hours < 1:
        raise HTTPException(400, "poll_interval_hours must be >= 1")

    source = MarketplaceSourceModel(
        name=body.name.strip(),
        url=body.url.strip(),
        tier=body.tier,
        token_enc=_encrypt_token(body.token) if body.token else None,
        poll_interval_hours=body.poll_interval_hours,
        added_by=user.user_id,
    )
    session.add(source)
    await session.commit()
    await session.refresh(source)

    # Immediately poll the new source so packages appear right away
    await _poll_source(source, session)

    return {
        "id":           str(source.id),
        "name":         source.name,
        "url":          source.url,
        "tier":         source.tier,
        "package_count": source.package_count,
    }


@router.delete("/sources/{source_id}")
async def remove_source(
    source_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _require_admin(user)
    await _require_operator(session, user)   # 8e (c): sources are the install's
    source = await session.get(MarketplaceSourceModel, uuid.UUID(source_id))
    if not source:
        raise coded(404, MKT_NOTFOUND_404, "Source not found")
    if source.tier == "official":
        raise coded(400, MKT_OFFICIAL_400, "Cannot remove official Velaris source")
    # Remove packages from this source (they'll disappear from the catalogue)
    await session.execute(
        delete(MarketplacePackageCacheModel).where(
            MarketplacePackageCacheModel.source_id == source.id
        )
    )
    await session.delete(source)
    await session.commit()
    return {"removed": source_id}


@router.post("/sources/{source_id}/sync")
async def sync_source(
    source_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _require_admin(user)
    await _require_operator(session, user)   # 8e (c): sources are the install's
    source = await session.get(MarketplaceSourceModel, uuid.UUID(source_id))
    if not source:
        raise coded(404, MKT_NOTFOUND_404, "Source not found")
    count = await _poll_source(source, session)
    return {"source_id": source_id, "packages_synced": count, "polled_at": _utcnow().isoformat()}


@router.post("/sources/sync-all")
async def sync_all_sources(
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _require_admin(user)
    await _require_operator(session, user)   # 8e (c): sources are the install's
    result = await session.execute(
        select(MarketplaceSourceModel).where(MarketplaceSourceModel.enabled.is_(True))
    )
    sources = result.scalars().all()
    totals = {}
    for s in sources:
        count = await _poll_source(s, session)
        totals[s.name] = count
    return {"synced": totals, "total_packages": sum(totals.values())}


# ══════════════════════════════════════════════════════════════════════════════
#  PACKAGE ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/packages")
async def list_packages(
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _can_view(user)
    packages = await _get_packages(session)
    return {"packages": [_pkg_to_dict(p) for p in packages]}


@router.get("/packages/{package_id:path}")
async def get_package(
    package_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _can_view(user)
    await _get_packages(session)   # ensure cache is fresh
    pkg = await session.get(MarketplacePackageCacheModel, package_id)
    if not pkg:
        raise coded(404, MKT_NOTFOUND_404, f"Package '{package_id}' not found")
    return _pkg_to_dict(pkg, include_versions=True)


@router.post("/packages/refresh")
async def refresh_all_packages(
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _require_admin(user)
    # Reset poll timestamps so _get_packages forces a full refresh
    result = await session.execute(select(MarketplaceSourceModel))
    for s in result.scalars().all():
        s.last_polled_at = None
    await session.commit()
    packages = await _get_packages(session)
    return {"packages_refreshed": len(packages)}


# ══════════════════════════════════════════════════════════════════════════════
#  WORKSPACE ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/workspaces")
async def list_workspaces(
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _require_developer_or_admin(user)
    query = select(MarketplaceWorkspaceModel)
    if not hxguard.user_allows(user, "marketplace.workspaces.override"):
        query = query.where(MarketplaceWorkspaceModel.created_by == user.user_id)
    # The override privilege widened to every TENANT's workspaces until
    # 2026-09-29 (8d); it now means every workspace in the caller's tenant.
    from velaris_shared.tenancy.identity import scope_to_caller
    query = await scope_to_caller(query, MarketplaceWorkspaceModel, session, user)
    result = await session.execute(query.order_by(MarketplaceWorkspaceModel.created_at.desc()))
    workspaces = result.scalars().all()

    out = []
    for ws in workspaces:
        items_r = await session.execute(
            select(MarketplaceWorkspaceItemModel).where(
                MarketplaceWorkspaceItemModel.workspace_id == ws.id
            )
        )
        out.append({
            "id":           str(ws.id),
            "name":         ws.name,
            "status":       ws.status,
            "created_by":   ws.created_by,
            "created_at":   ws.created_at.isoformat(),
            "expires_at":   ws.expires_at.isoformat(),
            "submitted_at": ws.submitted_at.isoformat() if ws.submitted_at else None,
            "reviewed_at":  ws.reviewed_at.isoformat() if ws.reviewed_at else None,
            "review_note":  ws.review_note,
            "items": [
                {
                    "package_id":      i.package_id,
                    "package_version": i.package_version,
                    "status":          i.status,
                    "installed_at":    i.installed_at.isoformat(),
                }
                for i in items_r.scalars().all()
            ],
        })
    return {"workspaces": out}


@router.post("/workspaces")
async def create_workspace(
    body: CreateWorkspaceReq,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _require_developer_or_admin(user)
    settings = get_settings()

    # Enforce per-user workspace limit (admins are exempt)
    if not hxguard.user_allows(user, "marketplace.workspaces.override"):
        active_count_result = await session.execute(
            select(MarketplaceWorkspaceModel).where(
                MarketplaceWorkspaceModel.created_by == user.user_id,
                MarketplaceWorkspaceModel.status.in_(["active", "submitted"]),
            )
        )
        active_count = len(active_count_result.scalars().all())
        limit = settings.marketplace_max_workspaces_per_user
        if active_count >= limit:
            raise coded(
                429, MKT_QUOTA_429,
                f"Workspace limit reached ({limit} active workspaces). "
                f"Delete or wait for approval of an existing workspace before creating a new one. "
                f"Admins can raise this limit via VELARIS_CASE_MARKETPLACE_MAX_WORKSPACES_PER_USER.",
            )

    ws = MarketplaceWorkspaceModel(
        tenant_id=user.tenant_id or "default",
        name=body.name.strip(),
        status="active",
        dataset_id=uuid.UUID(body.dataset_id) if body.dataset_id else None,
        created_by=user.user_id,
        expires_at=_utcnow() + timedelta(days=settings.marketplace_workspace_expiry_days),
    )
    session.add(ws)
    await session.commit()
    await session.refresh(ws)

    # Provision sandbox container
    try:
        from velaris_shared.apps.sandbox import provision_sandbox
        container_id = provision_sandbox(str(ws.id), [])
        ws.container_id = container_id
        await session.commit()
        logger.info("Sandbox container %s provisioned for workspace %s", container_id, ws.id)
    except Exception as exc:
        logger.warning("Sandbox container provisioning failed (non-fatal in dev): %s", exc)
    return {
        "id":         str(ws.id),
        "name":       ws.name,
        "status":     ws.status,
        "expires_at": ws.expires_at.isoformat(),
    }


@router.delete("/workspaces/{workspace_id}")
async def delete_workspace(
    workspace_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _require_developer_or_admin(user)
    ws = await session.get(MarketplaceWorkspaceModel, uuid.UUID(workspace_id))
    if not ws:
        raise coded(404, MKT_NOTFOUND_404, "Workspace not found")
    if not hxguard.user_allows(user, "marketplace.workspaces.override") and ws.created_by != user.user_id:
        raise coded(403, MKT_ACCESS_403, "Cannot delete another user's workspace")
    # Destroy sandbox container
    if ws.container_id:
        try:
            from velaris_shared.apps.sandbox import destroy_sandbox
            destroy_sandbox(ws.container_id)
        except Exception as exc:
            logger.warning("Container destroy failed for workspace %s: %s", workspace_id, exc)

    await session.delete(ws)
    await session.commit()
    return {"deleted": workspace_id}


@router.post("/workspaces/{workspace_id}/install")
async def install_package(
    workspace_id: str,
    body: InstallPackageReq,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _require_developer_or_admin(user)
    ws = await session.get(MarketplaceWorkspaceModel, uuid.UUID(workspace_id))
    if not ws:
        raise coded(404, MKT_NOTFOUND_404, "Workspace not found")
    if ws.status != "active":
        raise coded(400, MKT_STATE_400, f"Cannot install into workspace with status '{ws.status}'")
    if not hxguard.user_allows(user, "marketplace.workspaces.override") and ws.created_by != user.user_id:
        raise coded(403, MKT_ACCESS_403, "Cannot install into another user's workspace")

    pkg = await session.get(MarketplacePackageCacheModel, body.package_id)
    if not pkg:
        raise coded(404, MKT_NOTFOUND_404, f"Package '{body.package_id}' not found")

    existing_r = await session.execute(
        select(MarketplaceWorkspaceItemModel).where(
            MarketplaceWorkspaceItemModel.workspace_id == ws.id,
            MarketplaceWorkspaceItemModel.package_id == body.package_id,
        )
    )
    if existing_r.scalar_one_or_none():
        raise coded(409, MKT_INSTALLED_409, "Package already installed in this workspace")

    # Check tenant blacklist — block immediately if package/source/org is blacklisted
    src_result = await session.execute(
        select(MarketplaceSourceModel).where(MarketplaceSourceModel.id == pkg.source_id)
    ) if pkg.source_id else None
    src = src_result.scalar_one_or_none() if src_result else None
    source_url = src.url if src else ""

    blacklisted, bl_reason = await _is_blacklisted(
        ws.tenant_id, body.package_id, source_url, session
    )
    if blacklisted:
        await _report_to_velaris("install_blocked_blacklist", body.package_id, source_url, bl_reason)
        raise coded(403, MKT_BLOCKED_403, f"Package is blacklisted: {bl_reason}")

    # Check access group rule — developer may be restricted from this package
    ag_id = str(user.active_access_group.id) if getattr(user, "active_access_group", None) else None
    allowed, ar_reason = await _check_access_rule(
        ws.tenant_id, ag_id, body.package_id, pkg.publisher_tier, session
    )
    if not allowed:
        raise coded(403, MKT_BLOCKED_403, ar_reason)

    # Download + verify checksum + validate manifest before touching the container
    # #29: what the package DECLARES it needs, resolved from the verified
    # manifest and carried to the install row so the broker can hold it to it.
    # `None` stays None when there is nothing to verify (no download URL, no
    # checksum) — an absent declaration is not an empty one, and only the
    # second means "may touch nothing".
    declared_permissions: list[dict[str, str]] | None = None

    if pkg.download_url and pkg.checksum_sha256:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.get(pkg.download_url)
                r.raise_for_status()
                hxapp_bytes = r.content
            from velaris_shared.apps.checksum import verify_checksum, parse_and_validate_manifest
            verify_checksum(hxapp_bytes, pkg.checksum_sha256)
            manifest = parse_and_validate_manifest(hxapp_bytes)
            # Attached by parse_and_validate_manifest, never read from the
            # manifest itself — the underscore marks it as derived.
            declared_permissions = manifest.get("_permissions")
            # Roadmap #15: wasm is a declarable runtime (schema locked ahead
            # of HxSandbox) but cannot EXECUTE until HxSandbox (#17) ships —
            # reject at install, not at first run.
            if manifest.get("runtime", "python") == "wasm":
                if not get_settings().hxsandbox_enabled:
                    raise coded(400, MKT_SANDBOX_400,
                        "This package declares runtime 'wasm' — HxSandbox is not "
                        "enabled on this platform; only runtime 'python' "
                        "packages can be installed.")
                # HxSandbox is on: the guest is extracted, compiled, ABI- and
                # capability-checked HERE. #15's mandate is install-time
                # failure, never first-run, so a module that cannot run must
                # not be allowed to reach a sync pipeline.
                from velaris_shared.hxsandbox.registry import (
                    ModuleValidationError, extract_module,
                )
                try:
                    wasm_bytes = extract_module(hxapp_bytes, manifest)
                except ModuleValidationError as exc:
                    raise coded(400, MKT_SANDBOX_400, f"WASM module rejected: {exc}")
                # Store the validated bytes per tenant. A transform running per
                # sync row must not re-fetch from the publisher, and re-install
                # of the same package replaces the module rather than
                # accumulating versions.
                import hashlib as _hashlib

                from velaris_shared.db.models import HxSandboxModuleModel
                existing_mod = (await session.execute(
                    select(HxSandboxModuleModel).where(
                        HxSandboxModuleModel.tenant_id == ws.tenant_id,
                        HxSandboxModuleModel.package_id == body.package_id,
                    )
                )).scalar_one_or_none()
                digest = _hashlib.sha256(wasm_bytes).hexdigest()
                if existing_mod is None:
                    session.add(HxSandboxModuleModel(
                        tenant_id=ws.tenant_id, package_id=body.package_id,
                        version=pkg.version, wasm_bytes=wasm_bytes, sha256=digest,
                    ))
                else:
                    existing_mod.version = pkg.version
                    existing_mod.wasm_bytes = wasm_bytes
                    existing_mod.sha256 = digest
            # Roadmap #27 — conformance gate (report §6.5). Runs BEFORE the
            # capability grant below, deliberately: the result is committed so a
            # refusal stays visible to the publisher, and committing after the
            # grant was staged would persist a pending grant for a package the
            # gate is about to reject. Deterministic static analysis of the
            # declarative artifacts — no LLM, no execution — so the gate can
            # never be flaky, and it is refused BEFORE landing, never at first run.
            from commerce_service.marketplace import conformance_gate as conf
            conf_result = await conf.run_and_record(
                session, tenant_id=ws.tenant_id, package_id=body.package_id,
                package_version=pkg.version, manifest=manifest,
                hxapp_bytes=hxapp_bytes,
            )
            await session.commit()          # keep the record even if we refuse below
            decision = await conf.evaluate(
                session, tenant_id=ws.tenant_id, package_id=body.package_id,
                package_version=pkg.version,
                publisher_tier=_effective_tier(source_url, body.package_id),
                is_existing_install=False,
            )
            if decision.blocked:
                failed = [c for c in conf_result.get("checks", []) if not c.get("ok")]
                detail = "; ".join(f"{c['check']}: {c['detail']}" for c in failed[:5]) or decision.reason
                raise coded(
                    422, MKT_CONFORMANCE_422,
                    f"Package failed marketplace conformance — {decision.reason}. {detail}",
                )
            if decision.badge == conf.BADGE_FAILED:
                # warn mode: allowed through, but the operator is told plainly
                # rather than discovering it from a badge later.
                logger.warning("Package %s installed despite failing conformance (%s)",
                               body.package_id, decision.reason)

            # Layer-1 (execution & trust model): provision the remote app as
            # an INERT connector + pending capability grant. Nothing can run
            # or reach anywhere until an admin activates the grant.
            if manifest.get("execution"):
                from commerce_service.marketplace import grants as mkt_grants
                try:
                    await mkt_grants.create_pending_grant(
                        session, tenant_id=ws.tenant_id, package_id=body.package_id,
                        workspace_id=ws.id, manifest=manifest,
                        requested_by=user.user_id,
                        publisher_tier=_effective_tier(source_url, body.package_id))
                except ValueError as e:
                    raise coded(400, MKT_INSTALL_400, f"Execution provisioning failed: {e}")
            logger.info("Package %s checksum + manifest + conformance verified", body.package_id)
        except HTTPException:
            raise
        except Exception as exc:
            raise coded(400, MKT_INSTALL_400, f"Package verification failed: {exc}")

    item = MarketplaceWorkspaceItemModel(
        workspace_id=ws.id,
        package_id=body.package_id,
        package_version=pkg.version,
        status="installed",
        licence_key_enc=_encrypt_token(body.licence_key) if body.licence_key else None,
        declared_permissions=declared_permissions,
    )
    session.add(item)
    await session.commit()
    return {"package_id": body.package_id, "status": "installed", "workspace_id": workspace_id}


@router.post("/workspaces/{workspace_id}/submit")
async def submit_workspace(
    workspace_id: str,
    body: SubmitWorkspaceReq,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _require_developer_or_admin(user)
    ws = await session.get(MarketplaceWorkspaceModel, uuid.UUID(workspace_id))
    if not ws:
        raise coded(404, MKT_NOTFOUND_404, "Workspace not found")
    if ws.status != "active":
        raise coded(400, MKT_STATE_400, f"Workspace is already '{ws.status}'")
    if not hxguard.user_allows(user, "marketplace.workspaces.override") and ws.created_by != user.user_id:
        raise coded(403, MKT_ACCESS_403, "Cannot submit another user's workspace")
    # Conformance gate (#27 Phase C, decision D3): a NEW submission must pass 100%
    # structural conformance first. (Pre-gate packages are grandfathered separately
    # via conformance_status='legacy_unverified' and are never re-submitted here.)
    if ws.conformance_status not in ("structural_passed", "full_passed"):
        raise coded(
            400, MKT_STATE_400,
            "Workspace must pass the structural Conformance Suite before submission. "
            "Run POST /api/v1/testsuite/conformance first.",
        )
    ws.status = "submitted"
    ws.submitted_at = _utcnow()
    if body.notes:
        ws.review_note = body.notes
    await session.commit()
    return {"status": "submitted", "workspace_id": workspace_id}


@router.get("/workspaces/{workspace_id}/network-log")
async def get_network_log(
    workspace_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _require_developer_or_admin(user)
    ws = await session.get(MarketplaceWorkspaceModel, uuid.UUID(workspace_id))
    if not ws:
        raise coded(404, MKT_NOTFOUND_404, "Workspace not found")
    if not hxguard.user_allows(user, "marketplace.workspaces.override") and ws.created_by != user.user_id:
        raise coded(403, MKT_ACCESS_403, "Access denied")
    result = await session.execute(
        select(MarketplaceNetworkLogModel)
        .where(MarketplaceNetworkLogModel.workspace_id == uuid.UUID(workspace_id))
        .order_by(MarketplaceNetworkLogModel.created_at.desc())
        .limit(500)
    )
    return {"logs": [
        {
            "id":               str(l.id),
            "package_id":       l.package_id,
            "destination_url":  l.destination_url,
            "destination_ip":   l.destination_ip,
            "http_method":      l.http_method,
            "bytes_sent":       l.bytes_sent,
            "bytes_received":   l.bytes_received,
            "status":           l.status,
            "http_status_code": l.http_status_code,
            "is_declared":      l.is_declared,
            "created_at":       l.created_at.isoformat(),
        }
        for l in result.scalars().all()
    ]}


# ══════════════════════════════════════════════════════════════════════════════
#  WHITELIST ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/workspaces/{workspace_id}/whitelist")
async def request_whitelist(
    workspace_id: str,
    body: WhitelistReq,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _require_developer_or_admin(user)
    ws = await session.get(MarketplaceWorkspaceModel, uuid.UUID(workspace_id))
    if not ws:
        raise coded(404, MKT_NOTFOUND_404, "Workspace not found")
    if not hxguard.user_allows(user, "marketplace.workspaces.override") and ws.created_by != user.user_id:
        raise coded(403, MKT_ACCESS_403, "Access denied")
    entry = MarketplaceWhitelistModel(
        workspace_id=ws.id,
        package_id=body.package_id,
        domain=body.domain.lower().strip(),
        justification=body.justification,
        status="pending",
        requested_by=user.user_id,
    )
    session.add(entry)
    await session.commit()
    return {"whitelist_id": str(entry.id), "domain": entry.domain, "status": "pending"}


@router.patch("/whitelist/{whitelist_id}")
async def decide_whitelist(
    whitelist_id: str,
    body: WhitelistDecisionReq,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _require_admin(user)
    if body.decision not in ("approved", "denied"):
        raise HTTPException(400, "decision must be 'approved' or 'denied'")
    entry = await session.get(MarketplaceWhitelistModel, uuid.UUID(whitelist_id))
    if not entry:
        raise coded(404, MKT_NOTFOUND_404, "Whitelist entry not found")
    entry.status = body.decision
    entry.decided_by = user.user_id
    entry.decided_at = _utcnow()
    await session.commit()
    # Add iptables ACCEPT rule in the sandbox container when approved
    if body.decision == "approved":
        ws = await session.get(MarketplaceWorkspaceModel, entry.workspace_id)
        if ws and ws.container_id:
            try:
                from velaris_shared.apps.network_enforcer import add_whitelist_rule
                add_whitelist_rule(ws.container_id, entry.domain)
                logger.info("Whitelist rule added for domain %s in container %s", entry.domain, ws.container_id)
            except Exception as exc:
                logger.warning("Could not add iptables rule for %s: %s", entry.domain, exc)

    return {"whitelist_id": whitelist_id, "status": body.decision}


# ══════════════════════════════════════════════════════════════════════════════
#  LAYER-1 CAPABILITY GRANTS (execution & trust model, mig 122)
#  Approval is never "now it's trusted" — it activates exactly the ticked,
#  default-deny capability subset. Revocation is instant inert.
# ══════════════════════════════════════════════════════════════════════════════

class GrantApproveReq(BaseModel):
    outbound_domains: list[str]
    scopes: list[str] = []
    note: str | None = None


class GrantRevokeReq(BaseModel):
    note: str | None = None


async def _visible_grant(session: AsyncSession, user: AuthenticatedUser, grant_id: str):
    """Tenant-scoped grant lookup — 404 anti-oracle."""
    from velaris_shared.db.models import MarketplaceCapabilityGrantModel
    try:
        gid = uuid.UUID(grant_id)
    except ValueError:
        raise coded(404, MKT_NOTFOUND_404, "Grant not found")
    grant = await session.get(MarketplaceCapabilityGrantModel, gid)
    if grant is None or grant.tenant_id != (user.tenant_id or "default"):
        raise coded(404, MKT_NOTFOUND_404, "Grant not found")
    return grant


@router.get("/grants")
async def list_grants(
    status: str | None = None,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _require_admin(user)
    from velaris_shared.db.models import MarketplaceCapabilityGrantModel
    from commerce_service.marketplace import grants as mkt_grants
    q = select(MarketplaceCapabilityGrantModel).where(
        MarketplaceCapabilityGrantModel.tenant_id == (user.tenant_id or "default"))
    if status:
        q = q.where(MarketplaceCapabilityGrantModel.status == status)
    rows = (await session.execute(
        q.order_by(MarketplaceCapabilityGrantModel.requested_at.desc()))).scalars().all()
    return {"grants": [mkt_grants.grant_view(g) for g in rows]}


@router.get("/grants/{grant_id}")
async def get_grant(
    grant_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _require_admin(user)
    from velaris_shared.db.models import MarketplaceNetworkLogModel
    from velaris_shared.apps import egress as mkt_egress
    from commerce_service.marketplace import grants as mkt_grants
    grant = await _visible_grant(session, user, grant_id)
    # Fold any fresh egress-gateway attempts in first so the view is current.
    if mkt_egress.egress_enabled():
        await mkt_egress.ingest_access_log(session)
    logs = (await session.execute(
        select(MarketplaceNetworkLogModel)
        .where(MarketplaceNetworkLogModel.grant_id == grant.id)
        .order_by(MarketplaceNetworkLogModel.created_at.desc()).limit(100)
    )).scalars().all()
    view = mkt_grants.grant_view(grant)
    # Layer-2: the grant's container (image identity, provenance, status).
    if mkt_grants.is_container_grant(grant):
        row = await mkt_grants._container_row(session, grant)
        if row is not None:
            view["container"] = {
                "image":              row.image,
                "image_digest":       row.image_digest,
                "registry":           row.registry,
                "status":             row.status,
                "container_id":       (row.container_id or "")[:12] or None,
                "signature_verified": row.signature_verified,
                "pulled_at":          row.pulled_at.isoformat() if row.pulled_at else None,
                "started_at":         row.started_at.isoformat() if row.started_at else None,
                "error":              row.error,
            }
    view["network_log"] = [{
        "destination_url": l.destination_url, "http_method": l.http_method,
        "status": l.status, "http_status_code": l.http_status_code,
        "is_declared": l.is_declared,
        "created_at": l.created_at.isoformat() if l.created_at else None,
    } for l in logs]
    return view


@router.post("/grants/{grant_id}/approve")
async def approve_capability_grant(
    grant_id: str,
    body: GrantApproveReq,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _require_admin(user)
    from commerce_service.marketplace import grants as mkt_grants
    grant = await _visible_grant(session, user, grant_id)
    if grant.status == "revoked":
        raise coded(409, MKT_GRANT_409, "Grant is revoked — reinstall the package to request again")
    try:
        await mkt_grants.approve_grant(
            session, grant=grant, outbound_domains=body.outbound_domains,
            scopes=body.scopes, admin_id=user.user_id, note=body.note)
    except ValueError as e:
        # Layer-2: a container start failure is recorded on the container row
        # (status/error) — persist that record even though the approval aborts.
        await session.commit()
        raise HTTPException(400, str(e))
    await session.commit()
    return mkt_grants.grant_view(grant)


@router.post("/grants/{grant_id}/drift/approve")
async def approve_grant_drift(
    grant_id: str,
    body: GrantApproveReq,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Apply a held (drifted) mapping with the admin-ticked subset of the
    NEW request. Until this call the old mapping keeps running untouched."""
    _require_admin(user)
    from commerce_service.marketplace import grants as mkt_grants
    grant = await _visible_grant(session, user, grant_id)
    try:
        await mkt_grants.approve_drift(
            session, grant=grant, outbound_domains=body.outbound_domains,
            scopes=body.scopes, admin_id=user.user_id, note=body.note)
    except ValueError as e:
        # Persist the container failure record (and the physical reality that
        # the old container was stopped) — the grant stays pending_reapproval
        # so the admin can retry or reject.
        await session.commit()
        raise HTTPException(400, str(e))
    await session.commit()
    return mkt_grants.grant_view(grant)


@router.post("/grants/{grant_id}/drift/reject")
async def reject_grant_drift(
    grant_id: str,
    body: GrantRevokeReq,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _require_admin(user)
    from commerce_service.marketplace import grants as mkt_grants
    grant = await _visible_grant(session, user, grant_id)
    try:
        await mkt_grants.reject_drift(session, grant=grant, admin_id=user.user_id, note=body.note)
    except ValueError as e:
        raise HTTPException(400, str(e))
    await session.commit()
    return mkt_grants.grant_view(grant)


@router.post("/grants/{grant_id}/revoke")
async def revoke_capability_grant(
    grant_id: str,
    body: GrantRevokeReq,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _require_admin(user)
    from commerce_service.marketplace import grants as mkt_grants
    grant = await _visible_grant(session, user, grant_id)
    if grant.status == "revoked":
        raise coded(409, MKT_GRANT_409, "Grant is already revoked")
    await mkt_grants.revoke_grant(session, grant=grant, admin_id=user.user_id, note=body.note)
    await session.commit()
    return mkt_grants.grant_view(grant)


# ══════════════════════════════════════════════════════════════════════════════
#  REVIEW QUEUE
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/review-queue")
async def get_review_queue(
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _require_admin(user)
    result = await session.execute(
        select(MarketplaceWorkspaceModel)
        .where(MarketplaceWorkspaceModel.status == "submitted")
        .order_by(MarketplaceWorkspaceModel.submitted_at.asc())
    )
    out = []
    for ws in result.scalars().all():
        items_r = await session.execute(
            select(MarketplaceWorkspaceItemModel).where(
                MarketplaceWorkspaceItemModel.workspace_id == ws.id
            )
        )
        log_r = await session.execute(
            select(MarketplaceNetworkLogModel).where(
                MarketplaceNetworkLogModel.workspace_id == ws.id
            )
        )
        logs = log_r.scalars().all()
        out.append({
            "workspace_id":      str(ws.id),
            "name":              ws.name,
            "created_by":        ws.created_by,
            "submitted_at":      ws.submitted_at.isoformat() if ws.submitted_at else None,
            "expires_at":        ws.expires_at.isoformat(),
            "review_note":       ws.review_note,
            "security_violation": any(not l.is_declared for l in logs),
            "network_log_count": len(logs),
            "blocked_calls":     sum(1 for l in logs if l.status == "blocked"),
            "items": [
                {"package_id": i.package_id, "package_version": i.package_version, "status": i.status}
                for i in items_r.scalars().all()
            ],
        })
    return {"queue": out}


@router.post("/review-queue/{workspace_id}/approve")
async def approve_workspace(
    workspace_id: str,
    body: ApproveReq,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _require_admin(user)
    ws = await session.get(MarketplaceWorkspaceModel, uuid.UUID(workspace_id))
    if not ws or ws.status != "submitted":
        raise coded(400, MKT_STATE_400, "Workspace not found or not in submitted state")

    items_r = await session.execute(
        select(MarketplaceWorkspaceItemModel).where(
            MarketplaceWorkspaceItemModel.workspace_id == ws.id
        )
    )
    approved = []
    for item in items_r.scalars().all():
        if body.package_ids and item.package_id not in body.package_ids:
            continue
        pkg = await session.get(MarketplacePackageCacheModel, item.package_id)
        existing_r = await session.execute(
            select(MarketplaceInstallModel).where(
                MarketplaceInstallModel.tenant_id == ws.tenant_id,
                MarketplaceInstallModel.package_id == item.package_id,
            )
        )
        if not existing_r.scalar_one_or_none():
            install_row = MarketplaceInstallModel(
                tenant_id=ws.tenant_id,
                package_id=item.package_id,
                package_version=item.package_version,
                package_type=pkg.package_type if pkg else "unknown",
                licence_key_enc=item.licence_key_enc,
                approved_by=user.user_id,
                workspace_id=ws.id,
                # #29: the ceiling travels with the package. Approving an
                # install is not the moment to widen what it declared.
                declared_permissions=item.declared_permissions,
            )
            session.add(install_row)
            await session.flush()
            # Layer-1: anchor the pending grant to the production install.
            # Workspace approval does NOT activate capabilities — that stays
            # an explicit, separate admin act on the grant itself.
            from commerce_service.marketplace import grants as mkt_grants
            grant = await mkt_grants.active_grant(session, ws.tenant_id, item.package_id)
            if grant is not None and grant.install_id is None:
                grant.install_id = install_row.id
        item.status = "approved"
        item.approved_at = _utcnow()
        approved.append(item.package_id)

    ws.status = "approved"
    ws.reviewed_by = user.user_id
    ws.reviewed_at = _utcnow()
    await session.commit()
    return {"approved": approved, "workspace_id": workspace_id}


@router.post("/review-queue/{workspace_id}/reject")
async def reject_workspace(
    workspace_id: str,
    body: RejectReq,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _require_admin(user)
    ws = await session.get(MarketplaceWorkspaceModel, uuid.UUID(workspace_id))
    if not ws or ws.status != "submitted":
        raise coded(400, MKT_STATE_400, "Workspace not found or not in submitted state")
    ws.status = "rejected"
    ws.reviewed_by = user.user_id
    ws.reviewed_at = _utcnow()
    ws.review_note = body.reason
    await session.commit()
    return {"rejected": workspace_id, "reason": body.reason}


# ══════════════════════════════════════════════════════════════════════════════
#  PRODUCTION INSTALLS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/installs")
async def list_installs(
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _require_admin(user)
    result = await session.execute(
        select(MarketplaceInstallModel).where(
            MarketplaceInstallModel.tenant_id == (user.tenant_id or "default"),
            MarketplaceInstallModel.revoked_at.is_(None),
        ).order_by(MarketplaceInstallModel.installed_at.desc())
    )
    return {"installs": [
        {
            "id":              str(i.id),
            "package_id":      i.package_id,
            "package_version": i.package_version,
            "package_type":    i.package_type,
            "licence_expires": i.licence_expires,
            "approved_by":     i.approved_by,
            "installed_at":    i.installed_at.isoformat(),
        }
        for i in result.scalars().all()
    ]}


@router.delete("/installs/{install_id}")
async def revoke_install(
    install_id: str,
    delete_data: bool = False,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Uninstall a package for this tenant.

    `delete_data=false` (default) — **revoke**: close the gate (the app's routes/
    UI go dark) but keep its data, so re-install is instant and lossless.
    `delete_data=true` — **revoke + delete data**: also run the app's registered
    data teardown (first-party official apps only). For HxTest this deletes its
    AI-generated suites + runs/results; the core Test Suite data is untouched.
    """
    _require_admin(user)
    install = await session.get(MarketplaceInstallModel, uuid.UUID(install_id))
    if not install:
        raise coded(404, MKT_NOTFOUND_404, "Install not found")
    if install.tenant_id != (user.tenant_id or "default"):
        raise coded(403, MKT_ACCESS_403, "Cannot revoke install from another tenant")
    install.revoked_at = _utcnow()
    teardown = {"deleted": False}
    if delete_data:
        from commerce_service.marketplace.app_lifecycle import teardown_package_data
        teardown = await teardown_package_data(session, install.package_id, install.tenant_id)
    await session.commit()
    return {"revoked": install_id, "package_id": install.package_id, "data_teardown": teardown}


# ══════════════════════════════════════════════════════════════════════════════
#  UPDATE NOTIFICATIONS
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/updates")
async def list_updates(
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _require_admin(user)
    result = await session.execute(
        select(MarketplaceUpdateModel).where(
            MarketplaceUpdateModel.tenant_id == (user.tenant_id or "default"),
            MarketplaceUpdateModel.status == "pending",
        ).order_by(MarketplaceUpdateModel.detected_at.desc())
    )
    return {"updates": [
        {
            "id":                  str(u.id),
            "package_id":          u.package_id,
            "installed_version":   u.installed_version,
            "available_version":   u.available_version,
            "release_notes":       u.release_notes,
            "new_outbound_domains": json.loads(u.new_outbound_domains or "[]"),
            "fast_track":          u.fast_track,
            "detected_at":         u.detected_at.isoformat(),
        }
        for u in result.scalars().all()
    ]}


@router.post("/updates/{update_id}/approve")
async def approve_update(
    update_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Fast-track approve: applies when no new outbound domains were added.
    Bumps the installed version in marketplace_installs directly — no sandbox needed.
    """
    _require_admin(user)
    upd = await session.get(MarketplaceUpdateModel, uuid.UUID(update_id))
    if not upd:
        raise coded(404, MKT_NOTFOUND_404, "Update not found")
    if not upd.fast_track:
        raise HTTPException(400, "This update has new outbound domains — must go through sandbox testing first")
    if upd.status != "pending":
        raise coded(400, MKT_STATE_400, f"Update is already '{upd.status}'")

    # Bump version in the install record
    install_r = await session.execute(
        select(MarketplaceInstallModel).where(
            MarketplaceInstallModel.tenant_id == upd.tenant_id,
            MarketplaceInstallModel.package_id == upd.package_id,
            MarketplaceInstallModel.revoked_at.is_(None),
        )
    )
    install = install_r.scalar_one_or_none()
    if install:
        install.package_version = upd.available_version

    # Layer-1 drift (P2): an update to a package with an active capability
    # grant gets its NEW descriptor inspected before the version lands.
    # Fail-closed: if the new bundle can't be fetched and verified, the
    # update does not apply — a code-bearing package is never bumped blind.
    from commerce_service.marketplace import grants as mkt_grants
    drift: dict | None = None
    grant = await mkt_grants.active_grant(session, upd.tenant_id, upd.package_id)
    if grant is not None:
        pkg = await session.get(MarketplacePackageCacheModel, upd.package_id)
        if not pkg or not pkg.download_url or not pkg.checksum_sha256:
            raise coded(502, MKT_VERIFY_502, "Cannot verify the updated package bundle for drift inspection")
        try:
            async with httpx.AsyncClient(timeout=30.0) as http_client:
                r = await http_client.get(pkg.download_url)
                r.raise_for_status()
            from velaris_shared.apps.checksum import parse_and_validate_manifest, verify_checksum
            verify_checksum(r.content, pkg.checksum_sha256)
            new_manifest = parse_and_validate_manifest(r.content)
        except HTTPException:
            raise
        except Exception as exc:
            raise coded(502, MKT_VERIFY_502, f"Drift inspection failed — update not applied: {exc}")
        if new_manifest.get("execution"):
            src = await session.get(MarketplaceSourceModel, pkg.source_id) if pkg.source_id else None
            drift = await mkt_grants.apply_descriptor_drift(
                session, grant=grant, manifest=new_manifest,
                publisher_tier=_effective_tier(src.url if src else "", upd.package_id))

    # Roadmap #27 — an update is NEW CODE, so it is re-checked exactly like a
    # new install. Skipping this would make the gate bypassable by shipping
    # v1.0.1: install once while the gate was off (or before it existed), then
    # push anything through the update path forever after.
    #
    # The already-installed package is NOT touched by a refusal here — §6.5
    # forbids removing it. The version simply does not advance.
    from commerce_service.marketplace import conformance_gate as conf
    if conf.gate_mode() != conf.MODE_OFF:
        upd_pkg = await session.get(MarketplacePackageCacheModel, upd.package_id)
        new_bytes: bytes | None = None
        new_man: dict | None = None
        if upd_pkg and upd_pkg.download_url and upd_pkg.checksum_sha256:
            try:
                async with httpx.AsyncClient(timeout=30.0) as http_client:
                    rr = await http_client.get(upd_pkg.download_url)
                    rr.raise_for_status()
                new_bytes = rr.content
                from velaris_shared.apps.checksum import (
                    parse_and_validate_manifest as _pm, verify_checksum as _vc,
                )
                _vc(new_bytes, upd_pkg.checksum_sha256)
                new_man = _pm(new_bytes)
            except Exception as exc:
                # Fail closed: an update whose bundle cannot be fetched and
                # verified is not conformance-checkable, so it does not land.
                raise coded(502, MKT_VERIFY_502, f"Conformance re-check failed — update not applied: {exc}")
            await conf.run_and_record(
                session, tenant_id=upd.tenant_id, package_id=upd.package_id,
                package_version=upd.available_version, manifest=new_man,
                hxapp_bytes=new_bytes,
            )
            await session.commit()
        src_u = await session.get(MarketplaceSourceModel, upd_pkg.source_id) if (upd_pkg and upd_pkg.source_id) else None
        upd_decision = await conf.evaluate(
            session, tenant_id=upd.tenant_id, package_id=upd.package_id,
            package_version=upd.available_version,
            publisher_tier=_effective_tier(src_u.url if src_u else "", upd.package_id),
            is_existing_install=False,          # the NEW version has no standing yet
        )
        if upd_decision.blocked:
            raise coded(
                422, MKT_CONFORMANCE_422,
                f"Update failed marketplace conformance — {upd_decision.reason}. "
                f"The installed version is unaffected and keeps running.",
            )

    upd.status = "approved"
    upd.approved_at = _utcnow()
    upd.approved_by = user.user_id
    await session.commit()
    out = {"approved": update_id, "new_version": upd.available_version}
    if drift is not None:
        out["drift"] = drift
    return out


@router.post("/updates/{update_id}/dismiss")
async def dismiss_update(
    update_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _require_admin(user)
    upd = await session.get(MarketplaceUpdateModel, uuid.UUID(update_id))
    if not upd:
        raise coded(404, MKT_NOTFOUND_404, "Update not found")
    upd.status = "dismissed"
    await session.commit()
    return {"dismissed": update_id}


# ══════════════════════════════════════════════════════════════════════════════
#  BLACKLIST — tenant-managed, per-instance
# ══════════════════════════════════════════════════════════════════════════════

class BlacklistAddReq(BaseModel):
    type: str           # org | source | package
    value: str
    reason: str
    notify_velaris: bool = False


@router.get("/blacklist")
async def list_blacklist(
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _require_admin(user)
    result = await session.execute(
        select(MarketplaceBlacklistModel).where(
            MarketplaceBlacklistModel.tenant_id == (user.tenant_id or "default")
        ).order_by(MarketplaceBlacklistModel.created_at.desc())
    )
    return {"blacklist": [
        {
            "id":             str(e.id),
            "type":           e.type,
            "value":          e.value,
            "reason":         e.reason,
            "blacklisted_by": e.blacklisted_by,
            "notify_velaris": e.notify_velaris,
            "created_at":     e.created_at.isoformat(),
        }
        for e in result.scalars().all()
    ]}


@router.post("/blacklist")
async def add_blacklist_entry(
    body: BlacklistAddReq,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _require_admin(user)
    if body.type not in ("org", "source", "package"):
        raise HTTPException(400, "type must be 'org', 'source', or 'package'")
    if not body.reason.strip():
        raise HTTPException(400, "reason is required for the audit trail")

    tenant_id = user.tenant_id or "default"
    entry = MarketplaceBlacklistModel(
        tenant_id=tenant_id,
        type=body.type,
        value=body.value.strip(),
        reason=body.reason.strip(),
        blacklisted_by=user.user_id,
        notify_velaris=body.notify_velaris,
    )
    session.add(entry)

    # Suspend any active sandbox workspaces using this package/source
    suspended = 0
    if body.type == "package":
        suspended = await _suspend_workspaces_for_blacklisted_package(body.value, tenant_id, session)

    await session.commit()

    # Report to Velaris team if admin opted in
    if body.notify_velaris:
        await _report_to_velaris(
            "admin_blacklist",
            body.value if body.type == "package" else f"[{body.type}] {body.value}",
            body.value if body.type == "source" else "",
            body.reason,
        )

    logger.warning(
        "BLACKLIST: %s '%s' blacklisted by admin %s — reason: %s (sandboxes suspended: %d)",
        body.type, body.value, user.user_id, body.reason, suspended,
    )

    return {
        "id":                str(entry.id),
        "type":              entry.type,
        "value":             entry.value,
        "sandboxes_suspended": suspended,
    }


@router.delete("/blacklist/{blacklist_id}")
async def remove_blacklist_entry(
    blacklist_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _require_admin(user)
    entry = await session.get(MarketplaceBlacklistModel, uuid.UUID(blacklist_id))
    if not entry:
        raise coded(404, MKT_NOTFOUND_404, "Blacklist entry not found")
    if entry.tenant_id != (user.tenant_id or "default"):
        raise coded(403, MKT_ACCESS_403, "Cannot remove another tenant's blacklist entry")
    await session.delete(entry)
    await session.commit()
    return {"removed": blacklist_id}


# ══════════════════════════════════════════════════════════════════════════════
#  ACCESS RULES — per-access-group install restrictions
# ══════════════════════════════════════════════════════════════════════════════

class AccessRuleReq(BaseModel):
    rule_type: str                        # allow_all | official_only | allowlist | blocklist
    allowed_package_ids: list[str] = []
    blocked_package_ids: list[str] = []


@router.get("/access-rules")
async def list_access_rules(
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _require_admin(user)
    result = await session.execute(
        select(MarketplaceAccessRuleModel).where(
            MarketplaceAccessRuleModel.tenant_id == (user.tenant_id or "default")
        )
    )
    return {"rules": [
        {
            "access_group_id":     r.access_group_id,
            "rule_type":           r.rule_type,
            "allowed_package_ids": json.loads(r.allowed_package_ids or "[]"),
            "blocked_package_ids": json.loads(r.blocked_package_ids or "[]"),
            "updated_by":          r.updated_by,
            "updated_at":          r.updated_at.isoformat(),
        }
        for r in result.scalars().all()
    ]}


@router.put("/access-rules/{access_group_id}")
async def set_access_rule(
    access_group_id: str,
    body: AccessRuleReq,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _require_admin(user)
    valid_types = ("allow_all", "official_only", "allowlist", "blocklist")
    if body.rule_type not in valid_types:
        raise HTTPException(400, f"rule_type must be one of: {', '.join(valid_types)}")

    tenant_id = user.tenant_id or "default"
    existing_r = await session.execute(
        select(MarketplaceAccessRuleModel).where(
            MarketplaceAccessRuleModel.tenant_id == tenant_id,
            MarketplaceAccessRuleModel.access_group_id == access_group_id,
        )
    )
    rule = existing_r.scalar_one_or_none()
    if rule:
        rule.rule_type           = body.rule_type
        rule.allowed_package_ids = json.dumps(body.allowed_package_ids)
        rule.blocked_package_ids = json.dumps(body.blocked_package_ids)
        rule.updated_by          = user.user_id
        rule.updated_at          = _utcnow()
    else:
        rule = MarketplaceAccessRuleModel(
            tenant_id=tenant_id,
            access_group_id=access_group_id,
            rule_type=body.rule_type,
            allowed_package_ids=json.dumps(body.allowed_package_ids),
            blocked_package_ids=json.dumps(body.blocked_package_ids),
            updated_by=user.user_id,
        )
        session.add(rule)

    await session.commit()
    return {"access_group_id": access_group_id, "rule_type": body.rule_type}


@router.delete("/access-rules/{access_group_id}")
async def reset_access_rule(
    access_group_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _require_admin(user)
    result = await session.execute(
        select(MarketplaceAccessRuleModel).where(
            MarketplaceAccessRuleModel.tenant_id == (user.tenant_id or "default"),
            MarketplaceAccessRuleModel.access_group_id == access_group_id,
        )
    )
    rule = result.scalar_one_or_none()
    if rule:
        await session.delete(rule)
        await session.commit()
    return {"reset": access_group_id, "rule_type": "allow_all"}


# ══════════════════════════════════════════════════════════════════════════════
#  SANDBOX DATASETS
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
#  OFFICIAL PACKAGE RELEASE REQUESTS
#  Official packages don't go through sandbox. Installing one in dev
#  automatically flags it for inclusion in the next HxDeploy release cycle.
#  It will roll through the client's deployment pipeline (dev→UAT→prod) as
#  part of the next platform upgrade — never injected directly into production.
# ══════════════════════════════════════════════════════════════════════════════

@router.post("/packages/{package_id:path}/request-release")
async def request_release(
    package_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Flag an official package for inclusion in the next HxDeploy release cycle."""
    _require_developer_or_admin(user)
    pkg = await session.get(MarketplacePackageCacheModel, package_id)
    if not pkg:
        raise coded(404, MKT_NOTFOUND_404, "Package not found")
    if pkg.publisher_tier != "official":
        raise coded(400, MKT_OFFICIAL_400, "Only official packages use the release-request flow. Community packages use the sandbox workflow.")

    # Write a release request — picked up by HxDeploy on next deployment build
    from velaris_shared.db.models import MarketplaceReleaseRequestModel
    existing = await session.execute(
        select(MarketplaceReleaseRequestModel).where(
            MarketplaceReleaseRequestModel.tenant_id == (user.tenant_id or "default"),
            MarketplaceReleaseRequestModel.package_id == package_id,
            MarketplaceReleaseRequestModel.status == "pending",
        )
    )
    if existing.scalar_one_or_none():
        return {"status": "already_requested", "package_id": package_id}

    req = MarketplaceReleaseRequestModel(
        tenant_id=user.tenant_id or "default",
        package_id=package_id,
        package_version=pkg.version,
        requested_by=user.user_id,
    )
    session.add(req)
    await session.commit()
    return {
        "status": "requested",
        "package_id": package_id,
        "message": "Flagged for next release cycle. Will be included in the next HxDeploy deployment package.",
    }


@router.get("/release-requests")
async def list_release_requests(
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """List pending official package release requests — visible to admins for inclusion in HxDeploy."""
    _require_admin(user)
    from velaris_shared.db.models import MarketplaceReleaseRequestModel
    result = await session.execute(
        select(MarketplaceReleaseRequestModel).where(
            MarketplaceReleaseRequestModel.tenant_id == (user.tenant_id or "default"),
            MarketplaceReleaseRequestModel.status == "pending",
        ).order_by(MarketplaceReleaseRequestModel.created_at.asc())
    )
    return {"requests": [
        {
            "id":              str(r.id),
            "package_id":      r.package_id,
            "package_version": r.package_version,
            "requested_by":    r.requested_by,
            "created_at":      r.created_at.isoformat(),
        }
        for r in result.scalars().all()
    ]}


@router.post("/release-requests/{request_id}/approve")
async def approve_release_request(
    request_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Approve a pending official-package release request → install it for this tenant.

    On a single environment there is no separate HxDeploy pipeline, so admin
    approval here is what activates the package: it creates (or reactivates) the
    `marketplace_installs` row that feature-gates the app. The request row is
    deleted once fulfilled — the install row (with approved_by / installed_at)
    becomes the source of truth, and keeping a terminal-status request row would
    collide with the (tenant_id, package_id, status) unique constraint on the
    next request cycle.
    """
    _require_admin(user)
    from velaris_shared.db.models import MarketplaceReleaseRequestModel
    try:
        rid = uuid.UUID(request_id)
    except ValueError:
        raise coded(404, MKT_NOTFOUND_404, "Release request not found")
    req = await session.get(MarketplaceReleaseRequestModel, rid)
    if not req or req.tenant_id != (user.tenant_id or "default") or req.status != "pending":
        raise coded(404, MKT_NOTFOUND_404, "Release request not found")

    package_id = req.package_id
    pkg = await session.get(MarketplacePackageCacheModel, package_id)
    existing_r = await session.execute(
        select(MarketplaceInstallModel).where(
            MarketplaceInstallModel.tenant_id == req.tenant_id,
            MarketplaceInstallModel.package_id == package_id,
        )
    )
    install = existing_r.scalar_one_or_none()
    if install is None:
        session.add(MarketplaceInstallModel(
            tenant_id=req.tenant_id,
            package_id=package_id,
            package_version=req.package_version,
            package_type=pkg.package_type if pkg else "module",
            approved_by=user.user_id,
            workspace_id=None,
        ))
    elif install.revoked_at is not None:
        # Re-approving a previously uninstalled package — reactivate in place.
        install.revoked_at      = None
        install.package_version = req.package_version
        install.approved_by     = user.user_id
        install.installed_at    = _utcnow()

    # Fulfilled — drop the request row (see docstring re: unique constraint).
    await session.delete(req)
    await session.commit()
    return {"status": "approved", "package_id": package_id}


@router.post("/release-requests/{request_id}/reject")
async def reject_release_request(
    request_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Reject/cancel a pending official-package release request.

    The row is deleted (not status-flipped) so the developer can re-request later
    without tripping the (tenant_id, package_id, status) unique constraint.
    """
    _require_admin(user)
    from velaris_shared.db.models import MarketplaceReleaseRequestModel
    try:
        rid = uuid.UUID(request_id)
    except ValueError:
        raise coded(404, MKT_NOTFOUND_404, "Release request not found")
    req = await session.get(MarketplaceReleaseRequestModel, rid)
    if not req or req.tenant_id != (user.tenant_id or "default") or req.status != "pending":
        raise coded(404, MKT_NOTFOUND_404, "Release request not found")
    await session.delete(req)
    await session.commit()
    return {"status": "rejected"}


# ══════════════════════════════════════════════════════════════════════════════
#  SOURCE DECOMMISSIONING
#  Removing a source is irreversible. It triggers a decommissioning flow:
#    1. Preview: show all installed packages from this source + active case counts
#    2. Dependency check: flag packages other installed packages depend on
#    3. Admin confirms uninstall of each affected production package
#    4. Audit trail written for every revocation
#    5. Source record deleted
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/sources/{source_id}/decommission-preview")
async def decommission_preview(
    source_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Preview the impact of removing a source before committing to decommission."""
    _require_admin(user)
    source = await session.get(MarketplaceSourceModel, uuid.UUID(source_id))
    if not source:
        raise coded(404, MKT_NOTFOUND_404, "Source not found")
    if source.tier == "official":
        raise coded(400, MKT_OFFICIAL_400, "Official Velaris sources cannot be removed.")

    # Packages from this source
    pkgs_result = await session.execute(
        select(MarketplacePackageCacheModel).where(
            MarketplacePackageCacheModel.source_id == source.id
        )
    )
    source_packages = pkgs_result.scalars().all()
    source_pkg_ids = {p.id for p in source_packages}

    # Production installs from this source (for this tenant)
    installed_result = await session.execute(
        select(MarketplaceInstallModel).where(
            MarketplaceInstallModel.package_id.in_(source_pkg_ids),
            MarketplaceInstallModel.tenant_id == (user.tenant_id or "default"),
            MarketplaceInstallModel.revoked_at.is_(None),
        )
    )
    prod_installs = installed_result.scalars().all()

    # Active sandbox workspaces using packages from this source
    active_ws_result = await session.execute(
        select(MarketplaceWorkspaceItemModel).where(
            MarketplaceWorkspaceItemModel.package_id.in_(source_pkg_ids),
            MarketplaceWorkspaceItemModel.status == "installed",
        )
    )
    active_sandbox_items = active_ws_result.scalars().all()

    return {
        "source_name":         source.name,
        "source_url":          source.url,
        "packages_in_source":  len(source_packages),
        "prod_installs_affected": [
            {"package_id": i.package_id, "version": i.package_version, "installed_at": i.installed_at.isoformat()}
            for i in prod_installs
        ],
        "active_sandboxes_affected": len(set(i.workspace_id for i in active_sandbox_items)),
        "warnings": [
            "All packages from this source will be removed from the catalogue.",
            "Production installs listed above must be explicitly uninstalled before or during decommissioning.",
            "Any active case workflows using connectors from this source will lose their integration.",
            "Licence keys stored for paid packages from this source will be permanently deleted.",
            "This action is irreversible. Ensure no production data depends on these connectors.",
        ] if prod_installs else [
            "No production installs found. Safe to remove — only catalogue entries will be deleted.",
        ],
        "safe_to_remove": len(prod_installs) == 0,
    }


class DecommissionReq(BaseModel):
    confirm_uninstall_package_ids: list[str] = []   # prod install IDs to revoke
    reason: str


@router.post("/sources/{source_id}/decommission")
async def decommission_source(
    source_id: str,
    body: DecommissionReq,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """
    Execute full source decommissioning:
    1. Revoke all listed production installs
    2. Destroy active sandbox workspaces using packages from this source
    3. Remove all cached packages from this source
    4. Delete the source record
    5. Write audit trail for every action
    """
    _require_admin(user)
    await _require_operator(session, user)   # 8e (c): sources are the install's
    source = await session.get(MarketplaceSourceModel, uuid.UUID(source_id))
    if not source:
        raise coded(404, MKT_NOTFOUND_404, "Source not found")
    if source.tier == "official":
        raise coded(400, MKT_OFFICIAL_400, "Official Velaris sources cannot be removed.")
    if not body.reason.strip():
        raise HTTPException(400, "Decommissioning reason is required for the audit trail.")

    pkgs_result = await session.execute(
        select(MarketplacePackageCacheModel).where(
            MarketplacePackageCacheModel.source_id == source.id
        )
    )
    source_pkg_ids = {p.id for p in pkgs_result.scalars().all()}

    revoked = []
    # Step 1: Revoke confirmed production installs
    for install_id in body.confirm_uninstall_package_ids:
        install = await session.get(MarketplaceInstallModel, uuid.UUID(install_id))
        if install and install.revoked_at is None:
            install.revoked_at = _utcnow()
            revoked.append(install.package_id)
            logger.info(
                "DECOMMISSION: revoked prod install package=%s by admin=%s reason=%s",
                install.package_id, user.user_id, body.reason,
            )

    # Check no unrevoked prod installs remain from this source
    remaining_result = await session.execute(
        select(MarketplaceInstallModel).where(
            MarketplaceInstallModel.package_id.in_(source_pkg_ids),
            MarketplaceInstallModel.tenant_id == (user.tenant_id or "default"),
            MarketplaceInstallModel.revoked_at.is_(None),
        )
    )
    remaining = remaining_result.scalars().all()
    if remaining:
        raise HTTPException(
            409,
            f"Cannot decommission: {len(remaining)} production install(s) not yet confirmed for uninstall. "
            f"Include their IDs in confirm_uninstall_package_ids or revoke them manually first.",
        )

    # Step 2: Destroy active sandboxes using packages from this source
    ws_items_result = await session.execute(
        select(MarketplaceWorkspaceItemModel).where(
            MarketplaceWorkspaceItemModel.package_id.in_(source_pkg_ids)
        )
    )
    affected_ws_ids = {item.workspace_id for item in ws_items_result.scalars().all()}
    for ws_id in affected_ws_ids:
        ws = await session.get(MarketplaceWorkspaceModel, ws_id)
        if ws and ws.status in ("active", "submitted"):
            ws.status = "destroyed"
            logger.info("DECOMMISSION: destroyed sandbox workspace=%s", ws_id)

    # Step 3: Remove cached packages from this source
    await session.execute(
        delete(MarketplacePackageCacheModel).where(
            MarketplacePackageCacheModel.source_id == source.id
        )
    )

    # Step 4: Delete the source
    await session.delete(source)
    await session.commit()

    logger.info(
        "DECOMMISSION COMPLETE: source=%s removed by admin=%s reason=%s revoked=%s",
        source_id, user.user_id, body.reason, revoked,
    )

    return {
        "status":            "decommissioned",
        "source_id":         source_id,
        "prod_revoked":      revoked,
        "sandboxes_destroyed": len(affected_ws_ids),
        "reason":            body.reason,
        "completed_at":      _utcnow().isoformat(),
    }


@router.get("/sandbox-datasets")
async def list_datasets(
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    _require_developer_or_admin(user)
    result = await session.execute(
        select(MarketplaceSandboxDatasetModel).where(
            MarketplaceSandboxDatasetModel.tenant_id == (user.tenant_id or "default")
        )
    )
    return {"datasets": [
        {"id": str(d.id), "name": d.name, "description": d.description}
        for d in result.scalars().all()
    ]}


# ══════════════════════════════════════════════════════════════════════
# Roadmap #27 — conformance gate + grandfather window (report §6.5)
# ══════════════════════════════════════════════════════════════════════

@router.get("/conformance/window")
async def conformance_window(
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Grandfather-window status + the gate mode currently in force."""
    _can_view(user)
    from commerce_service.marketplace import conformance_gate as conf
    state = await conf.window_state(session)
    state["mode"] = conf.gate_mode()
    return state


@router.post("/conformance/window/activate")
async def activate_conformance_window(
    window_days: int = 60,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Open the grandfather window (admin).

    Idempotent by design: calling it again returns the RUNNING window untouched
    rather than restarting it. A restart would silently hand every lagging
    publisher another 60 days and quietly void the deadline.
    """
    _require_admin(user)
    from commerce_service.marketplace import conformance_gate as conf
    already = await conf.get_window(session) is not None
    await conf.activate_window(session, activated_by=user.user_id, window_days=window_days)
    state = await conf.window_state(session)
    state["already_active"] = already
    state["mode"] = conf.gate_mode()
    return state


@router.get("/conformance")
async def conformance_badges(
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Badge for every installed package, in one call.

    The catalogue needs a badge per row; fetching them one at a time would be an
    N+1 against a list that is rendered on every visit to the Installed tab.

    Sort order is part of the contract, not a UI preference: §6.5 requires
    unverified packages to sort BELOW conformance-passing ones.
    """
    _can_view(user)
    from commerce_service.marketplace import conformance_gate as conf

    tenant_id = user.tenant_id or "default"
    installs = (await session.execute(
        select(MarketplaceInstallModel).where(
            MarketplaceInstallModel.tenant_id == tenant_id,
            MarketplaceInstallModel.revoked_at.is_(None),
        )
    )).scalars().all()

    # Rank ascending = display order. Passing first, then the two "no verdict"
    # states, with an outright failure last.
    rank = {conf.BADGE_PASSED: 0, conf.BADGE_LEGACY: 1,
            conf.BADGE_UNVERIFIED: 2, conf.BADGE_FAILED: 3}

    out: list[dict] = []
    for inst in installs:
        pkg = await session.get(MarketplacePackageCacheModel, inst.package_id)
        src = await session.get(MarketplaceSourceModel, pkg.source_id) if (pkg and pkg.source_id) else None
        d = await conf.evaluate(
            session, tenant_id=tenant_id, package_id=inst.package_id,
            package_version=inst.package_version,
            publisher_tier=_effective_tier(src.url if src else "", inst.package_id),
            is_existing_install=True, installed_at=inst.installed_at,
        )
        out.append({
            "package_id": inst.package_id,
            "package_version": inst.package_version,
            "badge": d.badge,
            "grandfathered": d.grandfathered,
            "reason": d.reason,
            "sort_rank": rank.get(d.badge, 9),
        })

    out.sort(key=lambda r: (r["sort_rank"], r["package_id"]))
    state = await conf.window_state(session)
    return {"mode": conf.gate_mode(), "window": state, "packages": out}


@router.get("/conformance/{package_id:path}")
async def conformance_results(
    package_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Conformance history + current badge for one package.

    `badge` is what the catalogue sorts on: passing packages first, then
    unverified ones. Nothing here uninstalls or delists anything (§6.5).
    """
    _can_view(user)
    from velaris_shared.db.models import MarketplaceConformanceModel as M
    from commerce_service.marketplace import conformance_gate as conf

    tenant_id = user.tenant_id or "default"
    rows = (await session.execute(
        select(M).where(M.tenant_id == tenant_id, M.package_id == package_id)
                 .order_by(M.created_at.desc(), M.id.desc()).limit(20)
    )).scalars().all()

    install = (await session.execute(
        select(MarketplaceInstallModel).where(
            MarketplaceInstallModel.tenant_id == tenant_id,
            MarketplaceInstallModel.package_id == package_id,
            MarketplaceInstallModel.revoked_at.is_(None),
        )
    )).scalar_one_or_none()

    pkg = await session.get(MarketplacePackageCacheModel, package_id)
    src = await session.get(MarketplaceSourceModel, pkg.source_id) if (pkg and pkg.source_id) else None
    decision = await conf.evaluate(
        session, tenant_id=tenant_id, package_id=package_id,
        package_version=(install.package_version if install else (pkg.version if pkg else "")),
        publisher_tier=_effective_tier(src.url if src else "", package_id),
        is_existing_install=install is not None,
        installed_at=install.installed_at if install else None,
    )
    return {
        "package_id": package_id,
        "badge": decision.badge,
        "allowed": decision.allowed,
        "grandfathered": decision.grandfathered,
        "reason": decision.reason,
        "mode": decision.mode,
        "results": [
            {
                "id": str(r.id), "version": r.package_version, "status": r.status,
                "total": r.total, "passed": r.passed_count, "failed": r.failed_count,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "checks": r.checks_json,
            } for r in rows
        ],
    }


# ══════════════════════════════════════════════════════════════════════
# Accelerator Packs P1 — vertical content packs (mig 134)
# ══════════════════════════════════════════════════════════════════════

async def _require_installed(session: AsyncSession, tenant_id: str, package_id: str) -> None:
    """This tenant must hold an approved install of the pack.

    Install is where the blacklist, per-access-group rules, checksum, manifest
    validation and the #27 conformance gate all run. Provisioning straight from
    the catalogue would walk past every one of them — a package could be
    blacklisted, or fail conformance, and still have its case types, forms and
    rules written into the tenant. Entitlement is checked here so no pack
    endpoint can be the way around install.
    """
    install = (await session.execute(
        select(MarketplaceInstallModel).where(
            MarketplaceInstallModel.tenant_id == tenant_id,
            MarketplaceInstallModel.package_id == package_id,
            MarketplaceInstallModel.revoked_at.is_(None),
        )
    )).scalar_one_or_none()
    if install is None:
        raise coded(
            403, MKT_ACCESS_403,
            f"'{package_id}' is not installed for this tenant. Install it from the "
            f"marketplace first — that is where the conformance gate and access "
            f"rules are applied.",
        )


async def _load_pack(session: AsyncSession, package_id: str) -> tuple[dict, Any]:
    """Fetch + verify a pack bundle and parse it. Raises HTTPException on refusal."""
    from commerce_service.marketplace import packs as mkt_packs
    from velaris_shared.apps.checksum import verify_checksum, parse_and_validate_manifest

    pkg = await session.get(MarketplacePackageCacheModel, package_id)
    if not pkg:
        raise coded(404, MKT_NOTFOUND_404, f"Package '{package_id}' not found")
    if not pkg.download_url or not pkg.checksum_sha256:
        raise HTTPException(400, "Package has no verifiable bundle to provision")
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(pkg.download_url)
            r.raise_for_status()
        verify_checksum(r.content, pkg.checksum_sha256)
        manifest = parse_and_validate_manifest(r.content)
    except Exception as exc:
        # Fail closed: an unverifiable bundle is never provisioned.
        raise coded(502, MKT_VERIFY_502, f"Could not verify pack bundle: {exc}")

    if manifest.get("type") != mkt_packs.PACK_TYPE:
        raise HTTPException(400,
            f"'{package_id}' is a '{manifest.get('type')}' package, not an accelerator pack")
    try:
        pack = await mkt_packs.validate_pack_rails(mkt_packs.read_pack(r.content), session=session)
        return pack, pkg
    except mkt_packs.PackError as exc:
        raise HTTPException(400, str(exc))


@router.get("/packs/{package_id:path}/preview")
async def preview_pack(
    package_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """What provisioning this pack would create. Changes nothing."""
    _can_view(user)
    from commerce_service.marketplace import packs as mkt_packs

    await _require_installed(session, user.tenant_id or "default", package_id)
    pack, _pkg = await _load_pack(session, package_id)
    out = mkt_packs.preview(pack)
    existing = await mkt_packs.already_provisioned(
        session, user.tenant_id or "default", package_id)
    out["already_provisioned"] = existing is not None
    out["provisioned_at"] = existing.provisioned_at.isoformat() if existing else None
    return out


@router.post("/packs/{package_id:path}/provision")
async def provision_pack(
    package_id: str,
    sample_data: bool = False,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Provision a pack into this tenant (Developer or Admin).

    `sample_data` is opt-in and defaults to false — creating demo cases in a
    live tenant unasked is not a reasonable default.

    P1 provisions once: a second call is refused rather than re-importing, since
    `apply_bundle` upserts and would overwrite whatever the tenant has edited
    since. Pack upgrade is P2.
    """
    _require_developer_or_admin(user)
    from commerce_service.marketplace import packs as mkt_packs

    await _require_installed(session, user.tenant_id or "default", package_id)
    pack, _pkg = await _load_pack(session, package_id)
    try:
        return await mkt_packs.provision(
            session, tenant_id=user.tenant_id or "default", package_id=package_id,
            pack=pack, provisioned_by=user.user_id, with_sample_data=sample_data)
    except mkt_packs.PackError as exc:
        raise coded(409, MKT_PACK_409, str(exc))


@router.get("/packs")
async def list_provisioned_packs(
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Packs provisioned into this tenant, newest first."""
    _can_view(user)
    from velaris_shared.db.models import MarketplacePackInstallModel as P

    rows = (await session.execute(
        select(P).where(P.tenant_id == (user.tenant_id or "default"))
                 .order_by(P.provisioned_at.desc())
    )).scalars().all()
    return {"packs": [
        {
            "package_id": r.package_id, "version": r.pack_version,
            "vertical": r.vertical, "status": r.status,
            "imported": r.imported_count, "skipped": r.skipped_count,
            "needs_configuration": r.needs_config, "errors": r.errors_json,
            "sample_data": r.sample_data, "sample_cases": r.sample_count,
            "provisioned_by": r.provisioned_by,
            "provisioned_at": r.provisioned_at.isoformat() if r.provisioned_at else None,
        } for r in rows
    ]}


@router.get("/packs/{package_id:path}/upgrade-preview")
async def preview_pack_upgrade(
    package_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """What a pack upgrade would apply, keep and flag (P2). Changes nothing."""
    _can_view(user)
    from commerce_service.marketplace import packs as mkt_packs

    await _require_installed(session, user.tenant_id or "default", package_id)
    pack, _pkg = await _load_pack(session, package_id)
    try:
        return await mkt_packs.upgrade_preview(
            session, tenant_id=user.tenant_id or "default",
            package_id=package_id, pack=pack)
    except mkt_packs.PackError as exc:
        raise coded(409, MKT_PACK_409, str(exc))


@router.post("/packs/{package_id:path}/upgrade")
async def upgrade_pack(
    package_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Upgrade a provisioned pack, merging around tenant edits (P2).

    Artifacts changed by BOTH the pack and this tenant are reported as conflicts
    and left alone — never auto-resolved, because silently picking a side loses
    somebody's work. Everything non-conflicted still applies.
    """
    _require_developer_or_admin(user)
    from commerce_service.marketplace import packs as mkt_packs

    await _require_installed(session, user.tenant_id or "default", package_id)
    pack, _pkg = await _load_pack(session, package_id)
    try:
        return await mkt_packs.upgrade(
            session, tenant_id=user.tenant_id or "default", package_id=package_id,
            pack=pack, upgraded_by=user.user_id)
    except mkt_packs.PackError as exc:
        raise coded(409, MKT_PACK_409, str(exc))


@router.get("/packs/{package_id:path}/upgrades")
async def pack_upgrade_history(
    package_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Upgrade history for a pack, newest first."""
    _can_view(user)
    from velaris_shared.db.models import MarketplacePackUpgradeModel as U

    rows = (await session.execute(
        select(U).where(U.tenant_id == (user.tenant_id or "default"),
                        U.package_id == package_id)
                 .order_by(U.upgraded_at.desc()).limit(50)
    )).scalars().all()
    return {"upgrades": [{
        "from_version": r.from_version, "to_version": r.to_version,
        "status": r.status, "applied": r.applied_count, "skipped": r.skipped_count,
        "conflicts": r.conflicts_json, "errors": r.errors_json,
        "upgraded_by": r.upgraded_by,
        "upgraded_at": r.upgraded_at.isoformat() if r.upgraded_at else None,
    } for r in rows]}


@router.post("/packs/{package_id:path}/fork")
async def fork_pack(
    package_id: str,
    new_id: str,
    new_name: str,
    user: AuthenticatedUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    """Fork a provisioned pack into an editable pack of your own (P3).

    Returns the pack as a downloadable `.hxapp`. Exports the tenant's CURRENT
    state, customisations included — that is the point of forking. The fork is
    community tier and carries no upgrade link to its parent.
    """
    _require_developer_or_admin(user)
    from fastapi.responses import Response
    from commerce_service.marketplace import packs as mkt_packs

    try:
        forked = await mkt_packs.fork(
            session, tenant_id=user.tenant_id or "default", package_id=package_id,
            new_id=new_id, new_name=new_name)
        blob = mkt_packs.fork_to_hxapp(forked)
    except mkt_packs.PackError as exc:
        raise coded(409, MKT_PACK_409, str(exc))

    # new_id is user-supplied and lands in a response header. A quote or a
    # newline would break out of the header value (response-header injection),
    # and "../" would make the browser's suggested path a traversal. Reduce to a
    # conservative charset rather than trying to escape it.
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", new_id.replace("/", "-"))[:64] or "pack"
    filename = safe + ".hxapp"
    return Response(
        content=blob, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
