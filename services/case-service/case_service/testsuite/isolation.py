"""Test Suite (#27) — runner-enforced isolation (D1) + multi-identity auth (D5).

Mutating suites run ONLY inside disposable `hxtest-<runid>` tenants the runner
provisions and force-tears-down (even on failure). Tokens are minted from the
same settings the app decodes with, scoped to the ephemeral tenant.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from case_service.db.models import TenantModel, CaseInstanceModel, CaseTypeModel

logger = logging.getLogger(__name__)

EPHEMERAL_PREFIX = "hxtest-"


def mint_token(roles: list[str], tenant_id: uuid.UUID | None = None) -> str:
    """Mint a short-lived token for a test identity (decision D5)."""
    from case_service.auth.jwt_handler import create_dev_token
    from case_service.config import get_settings
    s = get_settings()
    return create_dev_token(
        user_id=str(uuid.uuid4()),
        username="hxtest-runner",
        roles=roles,
        secret=s.auth_secret,
        private_key=s.auth_rsa_private_key or "",
        expire_minutes=30,
    )


def identity_headers(tenant_a: uuid.UUID | None = None, tenant_b: uuid.UUID | None = None) -> dict[str, dict]:
    """Header sets for each test identity. 'none' carries no Authorization."""
    return {
        "admin":     {"Authorization": f"Bearer {mint_token(['admin', 'superadmin'])}"},
        "non_admin": {"Authorization": f"Bearer {mint_token([])}"},
        "tenant_a":  {"Authorization": f"Bearer {mint_token([], tenant_a)}"},
        "tenant_b":  {"Authorization": f"Bearer {mint_token([], tenant_b)}"},
        "none":      {},
    }


async def provision_ephemeral_tenant(session: AsyncSession, run_id: uuid.UUID, label: str) -> uuid.UUID:
    """Create a disposable tenant for a mutating run. Returns its id."""
    slug = f"{EPHEMERAL_PREFIX}{run_id.hex[:8]}-{label}"
    t = TenantModel(slug=slug, name=f"HxTest ephemeral ({label})", status="active",
                    description="auto-created by Test Suite; safe to delete")
    session.add(t)
    await session.flush()
    logger.info("Test Suite: provisioned ephemeral tenant %s", slug)
    return t.id


async def teardown_ephemeral_tenant(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Force-delete an ephemeral tenant + its data. Best-effort, never raises."""
    try:
        await session.execute(delete(CaseInstanceModel).where(CaseInstanceModel.tenant_id == tenant_id))
        await session.execute(delete(CaseTypeModel).where(CaseTypeModel.tenant_id == tenant_id))
        await session.execute(delete(TenantModel).where(TenantModel.id == tenant_id))
        await session.flush()
        logger.info("Test Suite: tore down ephemeral tenant %s", tenant_id)
    except Exception:  # noqa: BLE001
        logger.exception("Test Suite: ephemeral tenant teardown failed for %s", tenant_id)


async def reap_orphans(session: AsyncSession) -> int:
    """Sweeper: delete leftover hxtest-* tenants (e.g. from a crashed run)."""
    from sqlalchemy import select
    rows = (await session.execute(
        select(TenantModel).where(TenantModel.slug.like(f"{EPHEMERAL_PREFIX}%"))
    )).scalars().all()
    for t in rows:
        await teardown_ephemeral_tenant(session, t.id)
    return len(rows)
