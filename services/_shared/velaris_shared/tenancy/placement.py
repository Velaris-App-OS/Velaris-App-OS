"""Which tenant a newly created account belongs to (R5e step 10a).

Moved out of `case_service/api/routers/auth_real.py` when sign-in left
case-service for user-service: hxdraft (case-service) creates accounts too, and
the rule must be the same one wherever an account is made.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


async def place_in_callers_tenant(session: AsyncSession, actor: Any, new_user: Any) -> None:
    """A tenant's admin creates accounts in THEIR tenant.

    Without a directory row an account belonged to nobody, which the platform
    reads as the default tenant — the install's own — so a tenant's admin could
    mint accounts inside it (8d, 2026-09-29). The operator keeps the old
    behaviour.
    """
    from velaris_shared.db.models import UserDirectoryModel
    from velaris_shared.tenancy.identity import is_platform_operator

    if await is_platform_operator(session, actor) or not getattr(actor, "tenant_id", None):
        return
    # The tenant's `max_users` (NULL = unlimited, as before 2026-11-04).
    from velaris_shared.tenancy.limits import TenantLimitError, require_user_room
    try:
        await require_user_room(session, actor.tenant_id)
    except TenantLimitError as exc:
        from fastapi import HTTPException
        raise HTTPException(403, str(exc)) from exc
    # Memberships recorded for this name before the account existed must be
    # this tenant's (2026-11-09).
    from velaris_shared.tenancy import home
    clash = await home.groups_outside(
        session, await home.account_identifiers(session, new_user.username), str(actor.tenant_id))
    if clash:
        from fastapi import HTTPException
        raise HTTPException(409, "This name is already in access groups of another tenant: "
                                 + ", ".join(sorted(g.name for g in clash)))
    # Studio makes the directory entry FIRST (its tenant before the login
    # exists, 2026-11-09); that entry is this account's — no second row.
    existing = await home.directory_rows(
        session, [i for i in (new_user.username, new_user.email) if i])
    if existing:
        from velaris_shared.tenancy.identity import same_tenant
        if not await same_tenant(session, existing[0].tenant_id or "default", str(actor.tenant_id)):
            from fastapi import HTTPException
            raise HTTPException(409, "This name has a directory entry in another tenant.")
        return
    session.add(UserDirectoryModel(
        id=uuid.uuid4(), user_id=str(new_user.id), email=new_user.email,
        display_name=new_user.display_name or new_user.username,
        tenant_id=str(actor.tenant_id), is_active=True,
        metadata_json={"created_by": getattr(actor, "user_id", None)}))
