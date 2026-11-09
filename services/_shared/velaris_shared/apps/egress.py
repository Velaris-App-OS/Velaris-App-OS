"""Marketplace Layer-2 — egress host-filter (the "granted outbound domains
for containers" follow-up).

The internal apps network stays `internal: true` — untouched. The egress
gateway (deploy/docker-compose/egress-gw) is a CONNECT-only proxy dual-homed
on the apps network and the NATted default network; it is the ONLY way out,
and only for containers holding a per-container credential minted at grant
approval. This module owns the platform side:

  * credential mint (raw injected once into the container env, hash-only
    at rest on the grant — same posture as broker tokens)
  * the allowlist file the proxy re-reads per request (atomic writes:
    upsert on approval, removal on revoke/teardown = instant egress cut)
  * fail-closed gates: feature flag default OFF + gateway-running check —
    if either fails, a domain grant for a container is refused exactly as
    before this feature existed
  * access-log ingest into marketplace_network_log (egress:// scheme,
    grant-anchored, blocked attempts flagged undeclared)

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.config import get_settings

logger = logging.getLogger(__name__)

EGRESS_GATEWAY_CONTAINER = "velaris-egress-gw"
EGRESS_PROXY_IN_NETWORK = "http://velaris-egress-gw:3128"
# The broker must never be reached through the proxy (and the proxy would
# refuse it anyway — velaris-broker-gw is not a granted domain and resolves
# to a non-global address).
NO_PROXY_HOSTS = "velaris-broker-gw,localhost,127.0.0.1"


def egress_enabled() -> bool:
    return bool(get_settings().marketplace_l2_egress_enabled)


def _egress_dir() -> Path:
    """Config dir shared with the gateway container. Override for tests /
    non-standard layouts via VELARIS_CASE_MARKETPLACE_L2_EGRESS_DIR."""
    override = get_settings().marketplace_l2_egress_dir
    if override:
        return Path(override)
    # repo_root/deploy/docker-compose/egress-gw — resolved from this file:
    # apps/ > velaris_shared/ > _shared/ > services/ > repo root (same depth as
    # its old home under case-service, so the path is unchanged)
    return Path(__file__).resolve().parents[4] / "deploy" / "docker-compose" / "egress-gw"


def _conf_path() -> Path:
    return _egress_dir() / "conf" / "allowlist.json"


def _log_path() -> Path:
    return _egress_dir() / "log" / "egress-access.log"


def _read_allowlist() -> dict:
    try:
        data = json.loads(_conf_path().read_text(encoding="utf-8"))
        apps = data.get("apps")
        return apps if isinstance(apps, dict) else {}
    except Exception:
        return {}


def _write_allowlist(apps: dict) -> None:
    """Atomic replace — the proxy re-reads per request and must never see a
    torn file (a torn file would fail-closed anyway, but cleanly is better)."""
    conf = _conf_path()
    conf.parent.mkdir(parents=True, exist_ok=True)
    tmp = conf.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"apps": apps}, indent=2), encoding="utf-8")
    os.replace(tmp, conf)


def mint_credentials() -> tuple[str, str]:
    """(raw_token, sha256_hash) — raw goes into the container env once,
    only the hash is ever stored (grant JSON + allowlist entry)."""
    raw = secrets.token_urlsafe(32)
    return raw, hashlib.sha256(raw.encode()).hexdigest()


def _ensure_log_dir() -> None:
    """The gateway (uid 65534) must be able to append its audit log through
    the bind mount — git does not preserve dir modes, so a fresh clone would
    otherwise silently lose audit lines (log_event never takes the data path
    down, by design)."""
    log_dir = _log_path().parent
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(log_dir, 0o777)
    except Exception as exc:
        logger.warning("egress log dir %s not writable for the gateway: %s", log_dir, exc)


def upsert_app(user: str, *, token_hash: str, grant_id: str = "", package_id: str,
               tenant_id: str, domains: list[str], job_id: str = "") -> None:
    """Give one container an egress identity.

    `grant_id` and `job_id` are the two things this identity can belong to — a
    marketplace capability grant, or an RPA bot job — and they are separate
    fields because they key separate tables. Passing a job id as `grant_id`
    reads fine here and then fails at the far end of the pipe, in an ingest that
    has already deleted its input.
    """
    _ensure_log_dir()
    apps = _read_allowlist()
    apps[user] = {
        "token_hash": token_hash,
        "grant_id":   grant_id,
        "job_id":     job_id,
        "package_id": package_id,
        "tenant_id":  tenant_id,
        "domains":    [str(d).lower() for d in domains],
    }
    _write_allowlist(apps)
    logger.info("Egress allowlist: upserted %s (%s) domains=%s", user, package_id, domains)


def remove_app(user: str | None) -> None:
    """Instant egress cut — the proxy sees the next read without the entry."""
    if not user:
        return
    apps = _read_allowlist()
    if apps.pop(user, None) is not None:
        _write_allowlist(apps)
        logger.info("Egress allowlist: removed %s", user)


def require_gateway_running() -> None:
    """Fail-closed: a domain grant must not be approved while nothing would
    enforce it. Raises Layer2Error unless the gateway container is running."""
    from velaris_shared.apps.runtime import Layer2Error
    from velaris_shared.apps.sandbox import _get_client
    try:
        client = _get_client()
        status = client.containers.get(EGRESS_GATEWAY_CONTAINER).status
    except Exception as exc:
        raise Layer2Error(
            f"egress gateway '{EGRESS_GATEWAY_CONTAINER}' is not available ({exc}) — "
            "outbound domains cannot be granted to a container until it runs.")
    if status != "running":
        raise Layer2Error(
            f"egress gateway '{EGRESS_GATEWAY_CONTAINER}' is {status}, not running — "
            "outbound domains cannot be granted to a container until it runs.")


def proxy_env(user: str, raw_token: str) -> dict[str, str]:
    """Injected into the app container: standard proxy vars every HTTP stack
    honours. The credential doubles as the container's egress identity."""
    url = EGRESS_PROXY_IN_NETWORK.replace("http://", f"http://{user}:{raw_token}@")
    return {
        "HTTPS_PROXY": url, "https_proxy": url,
        "HTTP_PROXY":  url, "http_proxy":  url,
        "NO_PROXY":    NO_PROXY_HOSTS, "no_proxy": NO_PROXY_HOSTS,
    }


async def _surviving_anchors(session: AsyncSession, grant_ids: set, job_ids: set) -> tuple[set, set]:
    """Which of these anchors still exist, so a row is never lost to a key.

    Both columns are foreign keys, and an anchor can be absent for ordinary
    reasons: a grant revoked and deleted while its container was still running,
    a job purged by retention, or — the way this was found — an id written into
    the wrong column entirely. A row whose anchor has gone is still evidence:
    the host, the verdict and the byte counts are the point. Losing the WHOLE
    ingest because one line names a vanished parent is not.
    """
    from velaris_shared.db.models import BotJobModel, MarketplaceCapabilityGrantModel

    live_grants: set = set()
    live_jobs: set = set()
    if grant_ids:
        live_grants = set((await session.execute(
            select(MarketplaceCapabilityGrantModel.id)
            .where(MarketplaceCapabilityGrantModel.id.in_(grant_ids))
        )).scalars().all())
    if job_ids:
        live_jobs = set((await session.execute(
            select(BotJobModel.id).where(BotJobModel.id.in_(job_ids))
        )).scalars().all())
    return live_grants, live_jobs


async def ingest_access_log(session: AsyncSession) -> int:
    """Fold the proxy's JSONL access log into marketplace_network_log.

    Rotate-then-read: the proxy opens the file per line, so appends after
    the rename land in a fresh file and nothing is lost. Best-effort —
    an ingest problem must never break the endpoint that triggered it.

    **The rotated file is deleted only once its rows are safely in the
    database.** It used to be deleted the moment it had been parsed, so a flush
    that failed afterwards destroyed both the rows and the file they came from:
    the gateway's record of which hosts a container reached simply ceased to
    exist, and every reader was told "no requests". A failed flush now puts the
    lines back where the next ingest will find them.
    """
    from velaris_shared.db.models import MarketplaceNetworkLogModel

    log = _log_path()
    if not log.exists():
        return 0
    rotated = log.with_suffix(".ingest")
    try:
        os.replace(log, rotated)
        lines = rotated.read_text(encoding="utf-8").splitlines()
    except Exception:
        return 0

    def _uuid_or_none(value):
        try:
            return uuid.UUID(str(value)) if value else None
        except Exception:
            return None

    events = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except Exception:
            continue

    grant_ids = {g for g in (_uuid_or_none(e.get("grant_id")) for e in events) if g}
    job_ids = {j for j in (_uuid_or_none(e.get("job_id")) for e in events) if j}
    try:
        live_grants, live_jobs = await _surviving_anchors(session, grant_ids, job_ids)
    except Exception:
        logger.warning("egress ingest: could not check anchors", exc_info=True)
        live_grants, live_jobs = set(), set()

    n = 0
    for ev in events:
        try:
            grant_id = _uuid_or_none(ev.get("grant_id"))
            job_id = _uuid_or_none(ev.get("job_id"))
            session.add(MarketplaceNetworkLogModel(
                workspace_id=None,
                grant_id=grant_id if grant_id in live_grants else None,
                job_id=job_id if job_id in live_jobs else None,
                package_id=str(ev.get("package_id") or ev.get("user") or "?")[:255],
                destination_url=f"egress://{ev.get('target', '?')}"[:1024],
                http_method="CONNECT",
                bytes_sent=int(ev.get("bytes_sent") or 0),
                bytes_received=int(ev.get("bytes_received") or 0),
                status="allowed" if ev.get("status") == "allowed" else "blocked",
                is_declared=ev.get("status") == "allowed",
            ))
            n += 1
        except Exception:
            continue

    try:
        await session.flush()
    except Exception:
        # Put the lines back rather than lose them: whatever went wrong, the
        # audit trail is the one thing this function exists to preserve.
        await session.rollback()
        try:
            if not log.exists():
                os.replace(rotated, log)
        except Exception:
            logger.error("egress ingest: %d entries lost — could not restore %s",
                         n, rotated, exc_info=True)
        logger.warning("egress ingest: %d entries deferred, kept for retry", n,
                       exc_info=True)
        return 0

    try:
        rotated.unlink()
    except Exception:
        pass
    if n:
        logger.info("Egress access log: ingested %d entries", n)
    return n
