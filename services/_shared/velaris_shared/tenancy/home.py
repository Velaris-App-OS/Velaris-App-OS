"""A user's HOME tenant — the one sign-in gives them (2026-11-09).

Sign-in sets `user.tenant_id` from the user's directory row, found by the
account's id, then username, then email (`auth/dependencies.py`). That row is
the only thing that decides which tenant a person is in. Two screens looked as
if they decided it and did not:

* an access group's tenant — a default-tenant user put in tenant `acme`'s group
  stayed in the default tenant, i.e. the install's operator, and as `admin` saw
  every tenant and every user (reproduced on the DuckDNS server 2026-11-09);
* Tenants → Members (`tenant_memberships`), which sign-in never reads.

This module is the one place that answers "which tenant is this user in" for a
write path, by the same precedence sign-in uses, so the access-group and
directory routes can refuse a mismatch instead of storing it.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession


async def account_identifiers(session: AsyncSession, any_id: str) -> list[str]:
    """Every identifier one person is known by, in sign-in's precedence order:
    account id, username, email — then `any_id` itself if it is none of those
    (a directory-only person, a `portal:` id)."""
    from velaris_shared.db.models import HelixUserModel as U

    conds = [U.username == any_id, U.email == any_id]
    try:
        conds.append(U.id == uuid.UUID(str(any_id)))
    except (ValueError, TypeError):
        pass
    acct = (await session.execute(select(U).where(or_(*conds)))).scalars().first()
    out: list[str] = []
    if acct is not None:
        out += [str(acct.id), acct.username] + ([acct.email] if acct.email else [])
    if any_id not in out:
        out.append(any_id)
    return [i for i in out if i]


async def directory_rows(session: AsyncSession, identifiers: list[str]) -> list[Any]:
    """The directory rows for these identifiers, in the identifiers' order."""
    from velaris_shared.db.models import UserDirectoryModel as D

    rows = (await session.execute(select(D).where(D.user_id.in_(identifiers)))).scalars().all()
    rank = {i: n for n, i in enumerate(identifiers)}
    return sorted(rows, key=lambda r: rank.get(r.user_id, len(rank)))


async def home_tenant(session: AsyncSession, any_id: str) -> str | None:
    """The tenant sign-in gives this person. A row with no tenant is the
    platform's default tenant — the rule everywhere else. None: nobody by this
    id exists yet (no account, no directory row) — a membership may be recorded
    ahead of the person, as it always could; their directory row is checked
    against it when it is made."""
    from velaris_shared.db.models import HelixUserModel as U
    from velaris_shared.tenancy.identity import DEFAULT_TENANT

    idents = await account_identifiers(session, any_id)
    rows = await directory_rows(session, idents)
    if rows:
        return rows[0].tenant_id or DEFAULT_TENANT
    known = len(idents) > 1 or (await session.execute(
        select(U.id).where(U.username == any_id))).first() is not None
    return DEFAULT_TENANT if known else None


async def groups_outside(session: AsyncSession, identifiers: list[str], tenant: str) -> list[Any]:
    """This person's access groups that belong to a tenant other than `tenant`.
    A group with no tenant is shared and never counts."""
    from velaris_shared.db.models import AccessGroupModel as G, OperatorAccessGroupModel as O
    from velaris_shared.tenancy.identity import same_tenant

    groups = (await session.execute(
        select(G).join(O, O.access_group_id == G.id).where(O.operator_id.in_(identifiers))
    )).scalars().unique().all()
    return [g for g in groups
            if g.tenant_id and not await same_tenant(session, g.tenant_id, tenant)]


async def foreign_group_ids(session: AsyncSession, any_id: str) -> set:
    """Ids of this person's access groups owned by ANOTHER tenant — never to be
    applied to them. New ones are refused where memberships are written; this
    covers the ones written before that (the server's 2026-11-09 user) and any
    path not yet known. Empty for nobody-yet and for shared (no-tenant) groups."""
    from velaris_shared.tenancy.identity import DEFAULT_TENANT

    idents = await account_identifiers(session, any_id)
    tenant = await home_tenant(session, any_id) or DEFAULT_TENANT
    return {g.id for g in await groups_outside(session, idents, tenant)}
