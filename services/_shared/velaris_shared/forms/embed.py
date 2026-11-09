"""Embed field — another site's page inside a form, from approved sites only (2026-10-29).

A form's `embed` field names a `url`. It is shown only when that url's origin
is on its tenant's list of approved sites, kept in the tenant's settings:

    settings["form_embeds"] = {"allowed_origins": ["https://www.youtube-nocookie.com"],
                               "portal": false}

Unset (every install before this) = no site is approved: an embed is never
shown, and a form naming one cannot be saved. `portal` = embeds may also be
shown on the anonymous customer portal; unset = never there.

Checked three times: when a form is saved (`forms.py`), when the portal serves
a form (`portal_definition`), and by the browser before it draws the frame
(`studio/.../SmallControls.tsx`) — the last is the one every form, however it
was written, passes through.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

EMBED = "embed"
SETTINGS_KEY = "form_embeds"
MAX_ORIGINS = 50
#: The only extra sandbox permissions a designer may grant an embed.
PERMISSIONS = ("forms", "popups")

_HOST = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+")


def origin_of(url: Any) -> str | None:
    """`https://host[:port]` of an https url with a real host name, else None.
    No user name or password in it, no IP literal, no `localhost`."""
    if not isinstance(url, str) or len(url) > 2000 or any(c.isspace() for c in url):
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.username is not None or parts.password is not None:
        return None
    if not _HOST.fullmatch(host) or host.replace(".", "").isdigit():
        return None
    return f"https://{host}" + (f":{port}" if port and port != 443 else "")


def settings_of(tenant_settings: Any) -> dict[str, Any]:
    """The tenant's embed settings, read defensively: anything malformed = none approved."""
    raw = tenant_settings.get(SETTINGS_KEY) if isinstance(tenant_settings, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    origins = raw.get("allowed_origins")
    clean = [o for o in origins if isinstance(o, str) and origin_of(o) == o] if isinstance(origins, list) else []
    return {"allowed_origins": clean[:MAX_ORIGINS], "portal": raw.get("portal") is True}


def normalise_origins(items: list[Any]) -> tuple[list[str], list[str]]:
    """(origins, refused): each item reduced to its origin; anything not https
    with a real host name is refused by what was given."""
    out: list[str] = []
    refused: list[str] = []
    for item in items:
        o = origin_of(item.strip() if isinstance(item, str) else item)
        if o is None:
            refused.append(str(item))
        elif o not in out:
            out.append(o)
    return out, refused


def allowed(url: Any, origins: list[str]) -> bool:
    o = origin_of(url)
    return o is not None and o in origins


def embed_fields(definition: Any) -> list[dict[str, Any]]:
    """Every embed field of a form, repeating-group columns included."""
    out: list[dict[str, Any]] = []

    def walk(fields: Any) -> None:
        for f in fields if isinstance(fields, list) else []:
            if not isinstance(f, dict):
                continue
            if f.get("type") == EMBED:
                out.append(f)
            walk(f.get("fields"))

    if isinstance(definition, dict):
        for sec in definition.get("sections") if isinstance(definition.get("sections"), list) else []:
            if isinstance(sec, dict):
                walk(sec.get("fields"))
        walk(definition.get("fields"))
    return out


def refused_embeds(definition: Any, origins: list[str]) -> list[str]:
    """The labels of embeds whose site is not approved — empty = the form may be saved."""
    return [str(f.get("label") or f.get("field_key") or f.get("id") or "Embed")
            for f in embed_fields(definition) if not allowed(f.get("url"), origins)]


# ── Allowed sites: the platform operator's policy (2026-11-04) ───────────────
# Admin → Security → Allowed sites. A tenant's own list (above) is one input;
# the operator's policy, kept with the DEFAULT tenant, is the other:
#
#   settings["allowed_sites"]["embeds"] = {
#       "platform_origins": [...],   # approved for every tenant
#       "blocked":          [...],   # never shown, whoever approved them
#       "tenants_may_add":  true,    # may a tenant keep its own list?
#       "allow_forms":      true,    # may an embed take typing (sandbox allow-forms)?
#   }
#
# Unset (every install before this) = tenants keep their own lists, typing
# allowed, nothing platform-wide, nothing blocked — exactly as before. A
# website demo tenant (`settings.demo`) never keeps its own list.

POLICY_KEY = "allowed_sites"


def policy_of(default_tenant_settings: Any) -> dict[str, Any]:
    """The operator's embed policy, read defensively; malformed parts = their default."""
    root = default_tenant_settings.get(POLICY_KEY) if isinstance(default_tenant_settings, dict) else None
    raw = root.get("embeds") if isinstance(root, dict) else None
    raw = raw if isinstance(raw, dict) else {}

    def _list(key: str) -> list[str]:
        v = raw.get(key)
        return [o for o in v if isinstance(o, str) and origin_of(o) == o][:MAX_ORIGINS] if isinstance(v, list) else []
    return {
        "platform_origins": _list("platform_origins"),
        "blocked": _list("blocked"),
        "tenants_may_add": raw.get("tenants_may_add") is not False,
        "allow_forms": raw.get("allow_forms") is not False,
    }


def effective(tenant_settings: Any, policy: dict[str, Any]) -> dict[str, Any]:
    """What a tenant's forms may embed: its own list (when it may keep one) and
    the platform's, minus the blocked — and whether typing is allowed."""
    own = settings_of(tenant_settings)
    is_demo = isinstance(tenant_settings, dict) and isinstance(tenant_settings.get("demo"), dict)
    may_add = policy["tenants_may_add"] and not is_demo
    mine = own["allowed_origins"] if may_add else []
    blocked = set(policy["blocked"])
    merged = [o for o in dict.fromkeys(mine + policy["platform_origins"]) if o not in blocked]
    return {"allowed_origins": merged, "portal": own["portal"], "own_origins": own["allowed_origins"],
            "platform_origins": policy["platform_origins"], "blocked": policy["blocked"],
            "may_add": may_add, "allow_forms": policy["allow_forms"]}


async def load_policy(session) -> dict[str, Any]:
    from velaris_shared.db.models import TenantModel
    from velaris_shared.tenancy.identity import DEFAULT_TENANT, resolve_tenant
    hit = await resolve_tenant(session, DEFAULT_TENANT)
    row = await session.get(TenantModel, hit[0]) if hit else None
    return policy_of(row.settings if row else None)


async def effective_for(session, tenant_row) -> dict[str, Any]:
    return effective(tenant_row.settings if tenant_row is not None else None, await load_policy(session))


async def form_embed_problem(session, tenant_uuid, definition: Any) -> str | None:
    """Why a form may not be saved with its embeds, or None (2026-11-04). The
    ONE check for every path that writes a form or reusable section — the API,
    a branch merge, an AI draft, a package or app install: each embed from a
    site the owner's effective list approves, and no typing where the operator
    switched it off. A platform form (no tenant) is checked against the
    platform's own list."""
    fields = embed_fields(definition)
    if not fields:
        return None
    from velaris_shared.db.models import TenantModel
    tenant = await session.get(TenantModel, tenant_uuid) if tenant_uuid else None
    eff = await effective_for(session, tenant)
    refused = refused_embeds(definition, eff["allowed_origins"])
    if refused:
        return (f"Embedded page not from an approved site: {', '.join(refused)}. "
                "An administrator approves sites under Admin > Security > Allowed sites.")
    if not eff["allow_forms"] and any("forms" in (f.get("permissions") or []) for f in fields):
        return ("Typing into an embedded page is switched off on this platform "
                "(Admin > Security > Allowed sites).")
    return None
