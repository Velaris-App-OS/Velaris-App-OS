"""An archived tenant's portal is closed (R7, 2026-10-30).

Archiving a tenant set a status nothing read: its customer portal kept taking
submissions and signing customers in. Every portal router carries this
dependency; a route keyed by `{slug}` whose tenant is archived answers 404 —
the same as a slug that never existed.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.session import get_session


async def refuse_closed_portal(request: Request,
                               session: AsyncSession = Depends(get_session)) -> None:
    slug = request.path_params.get("slug")
    if not slug:
        return
    from velaris_shared.tenancy.identity import tenant_closed
    if await tenant_closed(session, slug):
        raise HTTPException(404, "Portal not found")
