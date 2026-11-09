"""The shared provisioning spine — one writer for a federated principal.

**Why this module exists.** A user provisioned from outside Velaris fans out to
four tables:

    velaris_users           the login record — is_active, external_id, roles
    user_directory          the profile record — manager, skills, timezone
    tenant_memberships      which tenant the person belongs to
    operator_access_groups  what they are allowed to do

Before this module there was no single writer. ``sso_callback`` created a
``velaris_users`` row and nothing else; ``/user-directory/bulk-sync`` wrote
``user_directory`` and nothing else. The two halves of one person could
therefore disagree about ``is_active``, and they did — ``update_entry`` carries
a one-way patch (``_sync_auth_active``) that exists precisely because a leaver
disabled in the directory kept their login. Adding SAML and SCIM as further
independent writers would have made that four-way.

So every entry point calls :func:`upsert_principal` and
:func:`deactivate_principal`, and both write the login record and the profile
record **in the same transaction**. ``is_active`` cannot diverge again because
there is no code path that sets one without the other.

**Transactions.** Nothing here commits. The caller owns the transaction, which
is what makes "both tables or neither" true; it also means the caller must
commit for the ``identity_provision_log`` row to survive. A caller that refuses
a request *after* calling in (an HTTP handler raising on a rejected result) must
commit before raising, the way ``sso_callback`` already does for its security
events — otherwise the record of the refusal rolls back with it.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from velaris_shared.db.models import (
    AuthDeviceModel,
    HelixUserModel,
    IdentityGroupMappingModel,
    IdentityProvisionLogModel,
    OperatorAccessGroupModel,
    RefreshTokenModel,
    TenantMembershipModel,
    UserDirectoryModel,
)
from velaris_shared.tenancy.identity import resolve_tenant

#: The entry points allowed to provision. `source` is recorded on every log row
#: and scopes group mappings, so a mapping written for SCIM is not silently
#: honoured for a SAML assertion.
SOURCES = ("scim", "saml_jit", "oidc", "bulk_sync")

#: The sources that provision *during a login*, where the asserted identity is
#: whatever the person at the other end managed to have asserted. A directory
#: sync (`scim`, `bulk_sync`) runs on a credential an administrator issued here
#: and is authoritative about its people; a login is not.
LOGIN_SOURCES = ("saml_jit", "oidc")

#: What a brand-new principal gets when the caller names no roles. Platform
#: roles are NOT owned by the IdP (see `_apply_roles`); this is only the floor
#: for an account that would otherwise have none.
DEFAULT_ROLES = ["viewer"]

#: Never assignable from an external source, at any privilege level. An IdP
#: administrator is not a Velaris superadmin, and a directory that could assert
#: its way to one would make the whole authorization model a courtesy.
FORBIDDEN_ROLES = {"superadmin"}

_USERNAME_SAFE = re.compile(r"[^a-z0-9._-]+")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class ProvisionResult:
    """What one provisioning decision did.

    ``result`` is ``ok`` or ``error``; a refusal is ``action="rejected"`` with
    ``result="error"`` and a machine-readable ``reason``. Refusals are returned,
    not raised: the caller decides the protocol-appropriate response (SCIM wants
    a 409 body, SAML wants a redirect, bulk-sync wants a counted row), and a
    raise here would force every caller to catch.
    """

    action: str
    result: str = "ok"
    username: str | None = None
    user_id: str | None = None          # velaris_users.id, as a string
    external_id: str | None = None
    tenant_token: str | None = None
    reason: str | None = None
    groups_added: list[str] = field(default_factory=list)
    groups_removed: list[str] = field(default_factory=list)
    unmapped_groups: list[str] = field(default_factory=list)
    detail: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.result == "ok"


# ── logging ───────────────────────────────────────────────────────────────

async def _log(
    session: AsyncSession,
    *,
    source: str,
    action: str,
    result: str,
    tenant_token: str | None,
    external_id: str | None,
    username: str | None,
    detail: dict,
) -> None:
    """Record one decision. Added to the session, not committed — see module doc."""
    session.add(IdentityProvisionLogModel(
        tenant_id=str(tenant_token) if tenant_token else None,
        source=source,
        external_id=external_id,
        username=username,
        action=action,
        result=result,
        detail=detail or {},
    ))


def _rejected(
    reason: str, *, source: str, tenant_token: str | None,
    external_id: str | None, username: str | None, **detail: Any,
) -> ProvisionResult:
    return ProvisionResult(
        action="rejected", result="error", reason=reason,
        username=username, external_id=external_id, tenant_token=tenant_token,
        detail={"reason": reason, "source": source, **detail},
    )


# ── tenant ────────────────────────────────────────────────────────────────

async def _resolve(
    session: AsyncSession, tenant_token: str | uuid.UUID | None,
) -> tuple[uuid.UUID | None, str | None, bool]:
    """Resolve a tenant token to ``(uuid, slug, ok)``.

    An absent token is not an error: single-tenant deployments provision
    tenantless principals and always have. It is only a *supplied* token that
    must name a real tenant — resolving it here is what stops a caller inventing
    a tenant string and having it stored verbatim.

    Note ``resolve_tenant(None)`` falls back to the ``"default"`` slug, which
    need not exist; that is why the absent case never reaches it.
    """
    if tenant_token is None or not str(tenant_token).strip():
        return None, None, True
    found = await resolve_tenant(session, tenant_token)
    if found is None:
        return None, None, False
    return found[0], found[1], True


# ── principal lookup ──────────────────────────────────────────────────────

async def _find_principal_matched(
    session: AsyncSession, *,
    external_id: str | None, username: str | None, email: str | None,
    link_by_email: bool = True,
) -> tuple[HelixUserModel | None, str | None]:
    """Find the login record by external_id, then username, then email.

    **Precedence, never a single ``OR``/``IN`` query.** Three columns identify a
    person and they can legitimately point at different rows — a leaver's
    address reissued to a joiner, an account renamed in Velaris but not at the
    IdP. Matching them together makes the database's row order decide which
    account an external system updates; matching them in order makes the *most
    stable* identifier win every time.

    ``link_by_email=False`` drops the last step. The email fallback is an
    *identity claim from outside*: whoever controls the asserted address takes
    over whichever local account already holds it, inheriting its roles. That is
    correct for a directory that is authoritative about its people (SCIM, a bulk
    sync an administrator ran) and wrong for a login, where it turns "can set an
    email attribute at the IdP" into "is that Velaris user" — see
    ``auth_saml.saml_acs``.

    Returns ``(row, matched_by)`` so a caller can treat "matched on the address
    the IdP asserted" differently from "matched on a stable identifier".
    """
    if external_id:
        row = (await session.execute(
            select(HelixUserModel).where(HelixUserModel.external_id == external_id)
        )).scalars().first()
        if row is not None:
            return row, "external_id"

    if username:
        row = (await session.execute(
            select(HelixUserModel).where(HelixUserModel.username == username)
        )).scalar_one_or_none()
        if row is not None:
            return row, "username"

    if email and link_by_email:
        # Case-insensitive: addresses are not case-sensitive in practice, and an
        # exact match forks one person into two accounts on a capitalisation
        # difference. Same rule `sso_callback` already applies.
        row = (await session.execute(
            select(HelixUserModel).where(func.lower(HelixUserModel.email) == email.lower())
        )).scalars().first()
        if row is not None:
            return row, "email"

    return None, None


async def _find_principal(
    session: AsyncSession, *,
    external_id: str | None, username: str | None, email: str | None,
    link_by_email: bool = True,
) -> HelixUserModel | None:
    """The row from :func:`_find_principal_matched`, for callers that only want it."""
    row, _how = await _find_principal_matched(
        session, external_id=external_id, username=username, email=email,
        link_by_email=link_by_email,
    )
    return row


async def _email_held_by_other(
    session: AsyncSession, email: str, *, owner_id: uuid.UUID | None,
) -> bool:
    """Is this address already on a different account?

    ``velaris_users.email`` is unique, so assigning one that another row holds
    raises an ``IntegrityError`` from the flush — a 500 out of what is a routine
    directory event (an address reissued to a joiner while the leaver's account
    is still on file). Checked here so it comes back as a refusal instead.
    """
    row = (await session.execute(
        select(HelixUserModel.id).where(func.lower(HelixUserModel.email) == email.lower())
    )).first()
    return row is not None and (owner_id is None or row[0] != owner_id)


async def find_principal(
    session: AsyncSession, *,
    external_id: str | None = None,
    username: str | None = None,
    email: str | None = None,
) -> HelixUserModel | None:
    """Public wrapper over the precedence lookup.

    Exists for callers whose protocol distinguishes *create* from *update* —
    SCIM's ``POST /Users`` in particular. :func:`upsert_principal` is an upsert
    by design and will happily match an existing account by email, which for a
    create means silently rewriting somebody else's record and handing the IdP
    their id as if it were the new user's.
    """
    return await _find_principal(
        session,
        external_id=(external_id or "").strip() or None,
        username=(username or "").strip() or None,
        email=(email or "").strip().lower() or None,
    )


async def resolve_account(
    session: AsyncSession, identifier: str,
) -> HelixUserModel | None:
    """An account named the way an administrator names one: id, username, email.

    The uuid is tried first because it is unambiguous; the other two go through
    the same precedence every other lookup here uses.
    """
    value = (identifier or "").strip()
    if not value:
        return None
    try:
        row = await session.get(HelixUserModel, uuid.UUID(value))
    except (ValueError, AttributeError):
        row = None
    if row is not None:
        return row
    return await find_principal(session, username=value, email=value)


async def link_identity(
    session: AsyncSession,
    *,
    identifier: str,
    external_id: str | None = None,
    sso_provider: str | None = None,
    sso_subject: str | None = None,
    actor: str,
) -> ProvisionResult:
    """Bind an existing account to the identity its IdP asserts.

    **This is the deliberate half of the rule that a login may not adopt an
    account by email.** A person who already has a Velaris account and then
    starts signing in through an IdP is refused at the callback, because an
    assertion or a token naming an address is not proof of owning that account.
    Somebody holding `identity.provisioning.write` says so instead, once, on the
    record.

    **Two bindings, because the two protocols key on different things.** SAML
    (and SCIM) match on ``external_id``, the `NameID`; the OIDC callback matches
    on ``(sso_provider, sso_subject)`` and never reads ``external_id``. Linking
    only one of them would leave the other login still refused — which is
    exactly the dead end this function exists to remove. Supply either, or both.

    Refusals, returned not raised: ``not_found``, ``superadmin_protected``,
    ``nothing_to_link``, ``external_id_taken``, ``sso_identity_taken``.

    Nothing else about the account changes — no roles, no groups, no
    ``is_active``: linking is a statement about *who this is*, and authorization
    still arrives the way it always did.
    """
    external_id = (external_id or "").strip() or None
    sso_provider = (sso_provider or "").strip() or None
    sso_subject = (sso_subject or "").strip() or None

    def _reject(reason: str, *, username: str, **extra) -> ProvisionResult:
        res = _rejected(
            reason, source="admin_link", tenant_token=None,
            external_id=external_id, username=username, **extra,
        )
        return res

    async def _refuse(reason: str, *, username: str, **extra) -> ProvisionResult:
        res = _reject(reason, username=username, **extra)
        await _log(session, source="admin_link", action="rejected", result="error",
                   tenant_token=None, external_id=external_id,
                   username=username, detail=res.detail)
        return res

    if external_id is None and not (sso_provider and sso_subject):
        # A half-supplied SSO pair is the dangerous shape: `(provider, NULL)`
        # would match every account that has never used that provider.
        return await _refuse("nothing_to_link", username=identifier)

    account = await resolve_account(session, identifier)
    if account is None:
        return await _refuse("not_found", username=identifier)

    if account.is_superadmin:
        # Same rule as every external source: the recovery account is not
        # something a directory gets to be able to authenticate as.
        return await _refuse("superadmin_protected", username=account.username)

    if external_id is not None:
        # `ix_velaris_users_external` is NOT unique, and the lookup takes the
        # first row it finds — so two accounts sharing an external id makes
        # which one an assertion signs in as a matter of row order.
        clash = (await session.execute(
            select(HelixUserModel).where(
                HelixUserModel.external_id == external_id,
                HelixUserModel.id != account.id,
            )
        )).scalars().first()
        if clash is not None:
            return await _refuse("external_id_taken", username=account.username,
                                 held_by=clash.username)

    if sso_provider and sso_subject:
        clash = (await session.execute(
            select(HelixUserModel).where(
                HelixUserModel.sso_provider == sso_provider,
                HelixUserModel.sso_subject == sso_subject,
                HelixUserModel.id != account.id,
            )
        )).scalars().first()
        if clash is not None:
            return await _refuse("sso_identity_taken", username=account.username,
                                 held_by=clash.username)

    previous = {"external_id": account.external_id,
                "sso_provider": account.sso_provider,
                "sso_subject": account.sso_subject}

    if external_id is not None:
        account.external_id = external_id
        # The profile record carries the same field, and this module exists so
        # the two cannot disagree.
        entry = (await session.execute(
            select(UserDirectoryModel).where(
                UserDirectoryModel.user_id == account.username)
        )).scalar_one_or_none()
        if entry is not None:
            entry.external_id = external_id
    if sso_provider and sso_subject:
        account.sso_provider = sso_provider
        account.sso_subject = sso_subject

    await session.flush()

    detail = {"source": "admin_link", "actor": actor, "previous": previous,
              "external_id": account.external_id,
              "sso_provider": account.sso_provider,
              "sso_subject": account.sso_subject}
    await _log(session, source="admin_link", action="link", result="ok",
               tenant_token=None, external_id=account.external_id,
               username=account.username, detail=detail)

    return ProvisionResult(
        action="link",
        username=account.username,
        user_id=str(account.id),
        external_id=account.external_id,
        detail=detail,
    )


async def unlink_identity(
    session: AsyncSession, *, identifier: str, actor: str,
) -> ProvisionResult:
    """Drop **every** IdP binding on an account: `external_id` and the SSO pair.

    Deliberately all of them. "Unlink" means the directory no longer gets to
    authenticate as this account, and clearing one binding while leaving the
    other still open is the kind of half-measure that reads as done.
    """
    account = await resolve_account(session, identifier)
    if account is None:
        res = _rejected("not_found", source="admin_link", tenant_token=None,
                        external_id=None, username=identifier)
        await _log(session, source="admin_link", action="rejected", result="error",
                   tenant_token=None, external_id=None, username=identifier,
                   detail=res.detail)
        return res

    if account.is_superadmin:
        res = _rejected("superadmin_protected", source="admin_link",
                        tenant_token=None, external_id=None,
                        username=account.username)
        await _log(session, source="admin_link", action="rejected", result="error",
                   tenant_token=None, external_id=None, username=account.username,
                   detail=res.detail)
        return res

    previous = {"external_id": account.external_id,
                "sso_provider": account.sso_provider,
                "sso_subject": account.sso_subject}
    account.external_id = None
    account.sso_provider = None
    account.sso_subject = None

    entry = (await session.execute(
        select(UserDirectoryModel).where(UserDirectoryModel.user_id == account.username)
    )).scalar_one_or_none()
    if entry is not None:
        entry.external_id = None

    await session.flush()

    detail = {"source": "admin_link", "actor": actor, "previous": previous,
              "directory_entry": entry is not None}
    await _log(session, source="admin_link", action="unlink", result="ok",
               tenant_token=None, external_id=None,
               username=account.username, detail=detail)

    return ProvisionResult(
        action="unlink", username=account.username, user_id=str(account.id),
        external_id=None, detail=detail,
    )


async def _unique_username(session: AsyncSession, base: str) -> str:
    """A username nobody holds, derived from ``base``."""
    candidate = _USERNAME_SAFE.sub("", (base or "user").strip().lower()) or "user"
    candidate = candidate[:200]
    suffix = 0
    while True:
        trial = candidate if suffix == 0 else f"{candidate}{suffix}"
        taken = (await session.execute(
            select(HelixUserModel.id).where(HelixUserModel.username == trial)
        )).first()
        if taken is None:
            return trial
        suffix += 1


def _sanitise_roles(roles: Iterable[str] | None) -> list[str]:
    return [
        str(r).strip() for r in (roles or [])
        if str(r).strip() and str(r).strip().lower() not in FORBIDDEN_ROLES
    ]


# ── group mapping ─────────────────────────────────────────────────────────

async def _mapped_groups(
    session: AsyncSession, *, tenant_id: uuid.UUID | None, source: str,
    group_names: Iterable[str],
) -> tuple[dict[str, uuid.UUID], list[str]]:
    """Translate asserted group names to access-group ids.

    Returns ``(name -> access_group_id, unmapped_names)``. **An unmapped group
    grants nothing** and there is no fallback that matches an access group by
    name: the IdP administrator owns those strings, so a name fallback would let
    them mint Velaris authorization by naming a directory group after one of
    ours. Unmapped names are returned so the caller can log them rather than
    have them vanish.
    """
    names = [str(g).strip() for g in (group_names or []) if str(g).strip()]
    if not names:
        return {}, []
    if tenant_id is None:
        # Mappings are tenant-scoped by design; a tenantless principal has no
        # mapping table to consult, so every asserted group is unmapped.
        return {}, names

    rows = (await session.execute(
        select(IdentityGroupMappingModel).where(
            IdentityGroupMappingModel.tenant_id == tenant_id,
            IdentityGroupMappingModel.source == source,
            IdentityGroupMappingModel.external_group.in_(names),
        ).order_by(IdentityGroupMappingModel.priority.asc())
    )).scalars().all()

    mapped = {r.external_group: r.access_group_id for r in rows}
    unmapped = [n for n in names if n not in mapped]
    return mapped, unmapped


async def sync_groups(
    session: AsyncSession, *,
    operator_id: str,
    group_names: Iterable[str],
    tenant_id: uuid.UUID | None,
    source: str,
    actor: str = "system",
) -> tuple[list[str], list[str], list[str]]:
    """Reconcile IdP-governed group membership. Returns (added, removed, unmapped).

    **The IdP governs only the groups it can map to.** Removal is restricted to
    access groups that appear in *some* mapping for this tenant and source; a
    membership an administrator granted by hand inside Velaris is never revoked
    because the directory did not mention it. Without that restriction, the
    first SCIM sync of a user would silently strip every locally-assigned group
    they had.
    """
    mapped, unmapped = await _mapped_groups(
        session, tenant_id=tenant_id, source=source, group_names=group_names,
    )
    desired = set(mapped.values())

    # Every access group this source is entitled to touch at all.
    governed: set[uuid.UUID] = set()
    if tenant_id is not None:
        governed = set((await session.execute(
            select(IdentityGroupMappingModel.access_group_id).where(
                IdentityGroupMappingModel.tenant_id == tenant_id,
                IdentityGroupMappingModel.source == source,
            )
        )).scalars().all())

    current_rows = (await session.execute(
        select(OperatorAccessGroupModel).where(
            OperatorAccessGroupModel.operator_id == operator_id
        )
    )).scalars().all()
    current = {r.access_group_id: r for r in current_rows}

    added: list[str] = []
    for gid in desired:
        if gid in current:
            continue
        session.add(OperatorAccessGroupModel(
            operator_id=operator_id,
            access_group_id=gid,
            # Never primary from an external source: is_primary picks the
            # operator's startup portal, which is a Velaris-side choice.
            is_primary=False,
            assigned_by=f"{source}:{actor}",
        ))
        added.append(str(gid))

    removed: list[str] = []
    for gid, row in current.items():
        if gid in desired or gid not in governed:
            continue
        await session.delete(row)
        removed.append(str(gid))

    await session.flush()
    return added, removed, unmapped


# ── the spine ─────────────────────────────────────────────────────────────

async def upsert_principal(
    session: AsyncSession,
    *,
    source: str,
    external_id: str | None = None,
    username: str | None = None,
    email: str | None = None,
    display_name: str | None = None,
    tenant_token: str | uuid.UUID | None = None,
    attributes: dict | None = None,
    group_names: Iterable[str] | None = None,
    roles: Iterable[str] | None = None,
    active: bool | None = True,
    actor: str = "system",
    allow_directory_only: bool = False,
    link_by_email: bool = True,
) -> ProvisionResult:
    """Create or update one principal across the login and profile records.

    ``attributes`` carries the optional profile fields an IdP may assert:
    ``manager``, ``timezone``, ``skills``, ``metadata``.

    ``active=None`` means **leave an existing account's state alone** (a new one
    is still created active). Just-in-time login provisioning must pass it: a
    successful authentication says the person exists at the IdP, not that an
    administrator's decision to disable them here should be undone. Only a
    source that is genuinely authoritative about lifecycle — SCIM, or an
    explicit `active` in a bulk sync — should pass a boolean.

    ``link_by_email=False`` refuses to adopt an existing account that matches
    only on the asserted email address — the account is found by ``external_id``
    or ``username`` or not at all, and a taken address comes back as
    ``email_taken``. A **login** source must pass it: otherwise whoever can set
    an email attribute at the IdP signs in as whichever local account holds that
    address, with its roles.

    ``allow_directory_only`` exists for ``bulk_sync``, which has always been
    able to record a person who has no Velaris login at all — a manager named
    only as an escalation target, or an id like ``portal:someone@example.com``.
    Refusing those would have been a silent narrowing of a shipped endpoint. It
    does not weaken the invariant this module exists for: divergence needs two
    rows, and a directory-only entry has one. A later call that supplies an
    email upgrades the same person to a full principal.

    Returns a :class:`ProvisionResult`; refusals come back as
    ``action="rejected"`` rather than raising.
    """
    if source not in SOURCES:
        raise ValueError(f"unknown provisioning source: {source!r}")

    attributes = dict(attributes or {})
    email = (email or "").strip().lower() or None
    username = (username or "").strip() or None
    external_id = (external_id or "").strip() or None
    tenant_str = str(tenant_token).strip() if tenant_token is not None and str(tenant_token).strip() else None

    tenant_id, _tenant_slug, tenant_ok = await _resolve(session, tenant_token)
    if not tenant_ok:
        res = _rejected(
            "unknown_tenant", source=source, tenant_token=tenant_str,
            external_id=external_id, username=username,
        )
        await _log(session, source=source, action="rejected", result="error",
                   tenant_token=tenant_str, external_id=external_id,
                   username=username, detail=res.detail)
        return res

    existing, matched_by = await _find_principal_matched(
        session, external_id=external_id, username=username, email=email,
        link_by_email=link_by_email,
    )

    # A local password account is never taken over by an email claim from a
    # login source. `link_by_email=False` already stops SAML reaching here; this
    # is the second half of the same rule, for a deployment that has switched
    # `link_existing_by_email` back on — a row with a password is somebody who
    # signs in here directly, not a federated identity waiting to be claimed.
    if (
        existing is not None
        and matched_by == "email"
        and source in LOGIN_SOURCES
        and existing.password_hash
    ):
        res = _rejected(
            "local_account_protected", source=source, tenant_token=tenant_str,
            external_id=external_id, username=existing.username, email=email,
        )
        await _log(session, source=source, action="rejected", result="error",
                   tenant_token=tenant_str, external_id=external_id,
                   username=existing.username, detail=res.detail)
        return res

    # A superadmin is out of reach of every external source. Checked on the
    # matched row rather than on the incoming name, because the attacker-
    # controlled half of this call is what the IdP asserts, not what we hold.
    if existing is not None and existing.is_superadmin:
        res = _rejected(
            "superadmin_protected", source=source, tenant_token=tenant_str,
            external_id=external_id, username=existing.username,
        )
        await _log(session, source=source, action="rejected", result="error",
                   tenant_token=tenant_str, external_id=external_id,
                   username=existing.username, detail=res.detail)
        return res

    # A deleted account (mig 223) is not brought back by a directory: neither a
    # SCIM sync nor an SSO sign-in may re-activate or re-create it during its
    # grace period — only an administrator's undo. After the purge the row is
    # gone and the directory provisions a new account by its own mappings.
    if existing is not None and existing.deleted_at is not None:
        res = _rejected(
            "account_deleted", source=source, tenant_token=tenant_str,
            external_id=external_id, username=existing.username,
        )
        await _log(session, source=source, action="rejected", result="error",
                   tenant_token=tenant_str, external_id=external_id,
                   username=existing.username, detail=res.detail)
        return res

    # A supplied address that belongs to somebody else is a refusal, not a
    # crash. Checked before either branch writes, so nothing is half-applied.
    if email and await _email_held_by_other(
        session, email, owner_id=existing.id if existing is not None else None,
    ):
        res = _rejected(
            "email_taken", source=source, tenant_token=tenant_str,
            external_id=external_id,
            username=existing.username if existing is not None else username,
            email=email,
        )
        await _log(session, source=source, action="rejected", result="error",
                   tenant_token=tenant_str, external_id=external_id,
                   username=res.username, detail=res.detail)
        return res

    created = False
    if existing is None:
        if not email:
            if allow_directory_only and username:
                return await _upsert_directory_only(
                    session, source=source, username=username,
                    external_id=external_id, display_name=display_name,
                    tenant_token=tenant_str, attributes=attributes,
                    active=active, actor=actor,
                )
            res = _rejected(
                "email_required", source=source, tenant_token=tenant_str,
                external_id=external_id, username=username,
            )
            await _log(session, source=source, action="rejected", result="error",
                       tenant_token=tenant_str, external_id=external_id,
                       username=username, detail=res.detail)
            return res
        resolved_username = username or await _unique_username(session, email.split("@")[0])
        if username:
            # An explicit username that is already taken belongs to somebody the
            # lookup above did not match — refuse rather than collide.
            clash = (await session.execute(
                select(HelixUserModel.id).where(HelixUserModel.username == resolved_username)
            )).first()
            if clash is not None:
                res = _rejected(
                    "username_taken", source=source, tenant_token=tenant_str,
                    external_id=external_id, username=resolved_username,
                )
                await _log(session, source=source, action="rejected", result="error",
                           tenant_token=tenant_str, external_id=external_id,
                           username=resolved_username, detail=res.detail)
                return res
        existing = HelixUserModel(
            username=resolved_username,
            email=email,
            display_name=display_name or resolved_username,
            external_id=external_id,
            roles=_sanitise_roles(roles) or list(DEFAULT_ROLES),
            # `None` is "don't touch an existing account", which for a brand-new
            # one can only mean active — nothing was decided about it yet.
            is_active=True if active is None else bool(active),
            # No password. A federated account authenticates at the IdP; a
            # placeholder hash here would be a second, weaker way in.
            password_hash=None,
        )
        session.add(existing)
        await session.flush()
        created = True
    else:
        if email:
            existing.email = email
        if display_name:
            existing.display_name = display_name
        if external_id:
            existing.external_id = external_id
        if active is not None:
            # A JIT login passes None: authenticating proves the person exists
            # at the IdP, not that an administrator's decision to disable them
            # here should be silently undone.
            existing.is_active = bool(active)
        # Platform roles are deliberately NOT overwritten on update. Authorization
        # from an external directory arrives as group membership, which maps
        # through `identity_group_mappings` to access groups an administrator
        # created here. Letting the IdP rewrite `roles` would route around that
        # entirely — and would undo a local revocation on the next sync.

    await _sync_directory(
        session,
        username=existing.username,
        external_id=external_id,
        email=email,
        display_name=display_name,
        default_email=existing.email,
        default_display=existing.display_name,
        tenant_token=tenant_str,
        attributes=attributes,
        active=active,
    )

    if tenant_id is not None:
        await _ensure_membership(session, tenant_id=tenant_id, user_id=existing.username)

    added, removed, unmapped = await sync_groups(
        session,
        operator_id=existing.username,
        group_names=group_names or [],
        tenant_id=tenant_id,
        source=source,
        actor=actor,
    )

    await session.flush()

    action = "create" if created else "update"
    detail = {
        "source": source, "actor": actor,
        "groups_added": added, "groups_removed": removed,
        "unmapped_groups": unmapped, "active": existing.is_active,
    }
    await _log(session, source=source, action=action, result="ok",
               tenant_token=tenant_str, external_id=existing.external_id,
               username=existing.username, detail=detail)

    return ProvisionResult(
        action=action,
        username=existing.username,
        user_id=str(existing.id),
        external_id=existing.external_id,
        tenant_token=tenant_str,
        groups_added=added,
        groups_removed=removed,
        unmapped_groups=unmapped,
        detail=detail,
    )


async def _sync_directory(
    session: AsyncSession,
    *,
    username: str,
    external_id: str | None,
    email: str | None,
    display_name: str | None,
    default_email: str | None,
    default_display: str | None,
    tenant_token: str | None,
    active: bool | None,
    attributes: dict,
) -> UserDirectoryModel:
    """Write the profile record for a principal, in the caller's transaction.

    ``user_directory.user_id`` and ``operator_access_groups.operator_id`` both
    key on the **username**, not on ``velaris_users.id`` — that mismatch has
    already cost a live defect where access groups never loaded for a staff
    login. Everything in this module that touches those two tables passes the
    username for exactly that reason.
    """
    entry = (await session.execute(
        select(UserDirectoryModel).where(UserDirectoryModel.user_id == username)
    )).scalar_one_or_none()

    if entry is None:
        entry = UserDirectoryModel(
            user_id=username,
            external_id=external_id,
            email=email or default_email,
            display_name=display_name or default_display or username,
            tenant_id=tenant_token,
            is_active=True if active is None else bool(active),
            metadata_json={},
        )
        session.add(entry)
    else:
        if external_id:
            entry.external_id = external_id
        if email:
            entry.email = email
        if display_name:
            entry.display_name = display_name
        if tenant_token:
            # Only overwrite when a tenant was actually supplied. A tenantless
            # call must not orphan a row that already belongs somewhere.
            entry.tenant_id = tenant_token
        if active is not None:
            # Mirrors the login record: `None` leaves an existing decision alone.
            entry.is_active = bool(active)

    if "manager" in attributes:
        entry.manager_user_id = (str(attributes["manager"]).strip() or None) if attributes["manager"] else None
    if attributes.get("timezone"):
        entry.timezone = str(attributes["timezone"])[:64]
    if attributes.get("skills") is not None:
        entry.skills = [str(s) for s in (attributes.get("skills") or [])]
    if attributes.get("metadata"):
        merged = dict(entry.metadata_json or {})
        merged.update(dict(attributes["metadata"]))
        entry.metadata_json = merged

    await session.flush()
    return entry


async def _upsert_directory_only(
    session: AsyncSession,
    *,
    source: str,
    username: str,
    external_id: str | None,
    display_name: str | None,
    tenant_token: str | None,
    active: bool | None,
    attributes: dict,
    actor: str,
) -> ProvisionResult:
    """Record a person who has no Velaris login — see ``allow_directory_only``.

    No groups are synchronised here on purpose: ``operator_access_groups`` grants
    authorization to something that can sign in, and there is nothing to sign in
    as. If a login record is created for this username later, the next upsert
    takes the full path and the groups arrive with it.
    """
    existed = (await session.execute(
        select(UserDirectoryModel.id).where(UserDirectoryModel.user_id == username)
    )).first() is not None

    await _sync_directory(
        session,
        username=username,
        external_id=external_id,
        email=None,
        display_name=display_name,
        default_email=None,
        default_display=display_name,
        tenant_token=tenant_token,
        attributes=attributes,
        active=active,
    )

    action = "update" if existed else "create"
    detail = {"source": source, "actor": actor, "directory_only": True,
              "active": True if active is None else bool(active)}
    await _log(session, source=source, action=action, result="ok",
               tenant_token=tenant_token, external_id=external_id,
               username=username, detail=detail)
    return ProvisionResult(
        action=action, username=username, external_id=external_id,
        tenant_token=tenant_token, detail=detail,
    )


async def _ensure_membership(
    session: AsyncSession, *, tenant_id: uuid.UUID, user_id: str,
) -> None:
    """Idempotent tenant membership.

    ``tenant_memberships.user_id`` is free-text and the platform writes a
    username there; keeping to that here means the row joins to the directory
    entry and the access-group rows rather than becoming a third convention.
    """
    found = (await session.execute(
        select(TenantMembershipModel.id).where(
            TenantMembershipModel.tenant_id == tenant_id,
            TenantMembershipModel.user_id == user_id,
        )
    )).first()
    if found is None:
        session.add(TenantMembershipModel(
            tenant_id=tenant_id, user_id=user_id, role="member",
        ))
        await session.flush()


async def deactivate_principal(
    session: AsyncSession,
    *,
    source: str,
    external_id: str | None = None,
    username: str | None = None,
    email: str | None = None,
    tenant_token: str | uuid.UUID | None = None,
    actor: str = "system",
    allow_directory_only: bool = False,
) -> ProvisionResult:
    """Soft-deactivate a principal and end their live sessions.

    **Never a hard delete.** Cases reference their creator and assignee by user
    id; removing the row orphans that history and breaks every audit trail
    pointing at it. Deactivation sets ``is_active = False`` on the login and
    profile records together and revokes the credentials that would otherwise
    outlive the decision:

    * refresh tokens — a live refresh chain keeps minting access tokens for as
      long as it is rotated, so leaving them alone means a leaver keeps working
      sessions until the last token expires;
    * devices — the bound device row is what a refresh presents, so revoking it
      closes the chain rather than one link of it.

    Both revocations key on ``str(user.id)``, the UUID, because that is what
    ``refresh_tokens.user_id`` and ``auth_devices.user_id`` hold — unlike the
    directory tables, which key on the username.
    """
    if source not in SOURCES:
        raise ValueError(f"unknown provisioning source: {source!r}")

    email = (email or "").strip().lower() or None
    username = (username or "").strip() or None
    external_id = (external_id or "").strip() or None
    tenant_str = str(tenant_token).strip() if tenant_token is not None and str(tenant_token).strip() else None

    principal = await _find_principal(
        session, external_id=external_id, username=username, email=email,
    )
    if principal is None:
        if allow_directory_only and username:
            # The bulk-sync counterpart of a directory-only create: a person with
            # no login record still has a profile row that an escalation rule can
            # target, and disabling them must stop that.
            entry = (await session.execute(
                select(UserDirectoryModel).where(UserDirectoryModel.user_id == username)
            )).scalar_one_or_none()
            if entry is not None:
                entry.is_active = False
                await session.flush()
                detail = {"source": source, "actor": actor, "directory_only": True}
                await _log(session, source=source, action="deactivate", result="ok",
                           tenant_token=tenant_str, external_id=external_id,
                           username=username, detail=detail)
                return ProvisionResult(
                    action="deactivate", username=username,
                    external_id=external_id, tenant_token=tenant_str, detail=detail,
                )
        res = _rejected(
            "not_found", source=source, tenant_token=tenant_str,
            external_id=external_id, username=username,
        )
        await _log(session, source=source, action="rejected", result="error",
                   tenant_token=tenant_str, external_id=external_id,
                   username=username, detail=res.detail)
        return res

    if principal.is_superadmin:
        res = _rejected(
            "superadmin_protected", source=source, tenant_token=tenant_str,
            external_id=external_id, username=principal.username,
        )
        await _log(session, source=source, action="rejected", result="error",
                   tenant_token=tenant_str, external_id=external_id,
                   username=principal.username, detail=res.detail)
        return res

    principal.is_active = False

    entry = (await session.execute(
        select(UserDirectoryModel).where(UserDirectoryModel.user_id == principal.username)
    )).scalar_one_or_none()
    if entry is not None:
        entry.is_active = False

    uid = str(principal.id)
    now = _utcnow()

    revoked_tokens = (await session.execute(
        update(RefreshTokenModel)
        .where(RefreshTokenModel.user_id == uid, RefreshTokenModel.revoked_at.is_(None))
        .values(revoked_at=now, revoked_by=f"{source}:deprovision")
    )).rowcount or 0

    revoked_devices = (await session.execute(
        update(AuthDeviceModel)
        .where(AuthDeviceModel.user_id == uid, AuthDeviceModel.revoked_at.is_(None))
        .values(revoked_at=now, revoked_by=f"{source}:deprovision")
    )).rowcount or 0

    await session.flush()

    detail = {
        "source": source, "actor": actor,
        "revoked_refresh_tokens": revoked_tokens,
        "revoked_devices": revoked_devices,
        "directory_entry": entry is not None,
    }
    await _log(session, source=source, action="deactivate", result="ok",
               tenant_token=tenant_str, external_id=principal.external_id,
               username=principal.username, detail=detail)

    return ProvisionResult(
        action="deactivate",
        username=principal.username,
        user_id=uid,
        external_id=principal.external_id,
        tenant_token=tenant_str,
        detail=detail,
    )
