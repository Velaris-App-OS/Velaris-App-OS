"""SCIM 2.0 — resource shapes, the filter subset, and PATCH application.

RFC 7643 (schema) and RFC 7644 (protocol), reduced to what Entra ID and Okta
actually send. The protocol is large and the two clients that matter use a
narrow, well-known slice of it; implementing the rest is where SCIM projects
sink without a single extra customer being provisionable.

**The filter subset is deliberate, not unfinished.** `userName eq "x"`,
`externalId eq "x"`, `active eq true`, `displayName eq "x"`, and the `and` of
two of those. Anything else is a 400 with `scimType: "invalidFilter"` — an
honest refusal the IdP surfaces to its administrator, rather than a partial
evaluation that silently provisions the wrong people. Writing a full filter
grammar here would mean writing a parser that runs on attacker-adjacent input
for cases no client sends.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from velaris_shared.db.models import AccessGroupModel, HelixUserModel, UserDirectoryModel

SCHEMA_USER = "urn:ietf:params:scim:schemas:core:2.0:User"
SCHEMA_GROUP = "urn:ietf:params:scim:schemas:core:2.0:Group"
SCHEMA_ENTERPRISE_USER = "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"
SCHEMA_LIST = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
SCHEMA_ERROR = "urn:ietf:params:scim:api:messages:2.0:Error"
SCHEMA_PATCH = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
SCHEMA_SP_CONFIG = "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"
SCHEMA_RESOURCE_TYPE = "urn:ietf:params:scim:schemas:core:2.0:ResourceType"

#: RFC 7644 §3.4.2.4 — a server MAY cap page size, and must report what it used.
MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 100


class ScimError(Exception):
    """A SCIM-shaped refusal. ``scim_type`` is the RFC 7644 §3.12 keyword."""

    def __init__(self, status: int, detail: str, scim_type: str | None = None):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.scim_type = scim_type

    def body(self) -> dict:
        out: dict[str, Any] = {
            "schemas": [SCHEMA_ERROR],
            "status": str(self.status),
            "detail": self.detail,
        }
        if self.scim_type:
            out["scimType"] = self.scim_type
        return out


# ── filters ───────────────────────────────────────────────────────────────

#: `attribute eq "value"` or `attribute eq true`. Quoted values may contain
#: anything except an unescaped quote; SCIM has no wildcard in `eq`, so nothing
#: here reaches a LIKE.
_TERM = re.compile(
    r'^\s*(?P<attr>[A-Za-z][\w.:$-]*)\s+(?P<op>eq)\s+'
    r'(?:"(?P<quoted>(?:[^"\\]|\\.)*)"|(?P<bare>true|false|null))\s*$',
    re.IGNORECASE,
)

#: Only these are filterable. An IdP asking about anything else gets told so.
FILTERABLE = {"username", "externalid", "active", "displayname", "id", "emails.value"}


@dataclass(frozen=True)
class FilterTerm:
    attribute: str          # normalised lower-case
    value: str | bool


def parse_filter(expression: str | None) -> list[FilterTerm]:
    """Parse the supported subset. Returns terms to be ANDed together.

    Raises :class:`ScimError` with ``invalidFilter`` for anything outside it —
    including `or`, `co`, `sw`, `pr` and grouping. That is the honest answer:
    silently ignoring an operator would return the wrong set, and an IdP acting
    on the wrong set deprovisions the wrong people.
    """
    if not expression or not expression.strip():
        return []

    # Split on ` and ` only. `or` is not supported, and splitting on it would
    # turn a disjunction into a conjunction — the one failure mode worse than
    # refusing.
    if re.search(r"\bor\b", expression, re.IGNORECASE):
        raise ScimError(400, "The 'or' operator is not supported.", "invalidFilter")
    if "(" in expression or ")" in expression:
        raise ScimError(400, "Grouped filters are not supported.", "invalidFilter")

    terms: list[FilterTerm] = []
    for chunk in re.split(r"\s+and\s+", expression.strip(), flags=re.IGNORECASE):
        match = _TERM.match(chunk)
        if not match:
            raise ScimError(
                400,
                "Only 'attribute eq value' filters (optionally joined by 'and') "
                "are supported.",
                "invalidFilter",
            )
        attr = match.group("attr").lower()
        if attr not in FILTERABLE:
            raise ScimError(400, f"Attribute '{match.group('attr')}' is not filterable.",
                            "invalidFilter")
        quoted = match.group("quoted")
        if quoted is not None:
            value: str | bool = quoted.replace('\\"', '"').replace("\\\\", "\\")
        else:
            bare = (match.group("bare") or "").lower()
            if bare == "null":
                raise ScimError(400, "Filtering on null is not supported.", "invalidFilter")
            value = bare == "true"
        terms.append(FilterTerm(attribute=attr, value=value))
    return terms


# ── resource serialisation ────────────────────────────────────────────────

def _iso(value) -> str | None:
    return value.isoformat() if value else None


def user_resource(
    user: HelixUserModel,
    entry: UserDirectoryModel | None,
    *,
    base_url: str,
    groups: list[tuple[str, str]] | None = None,
) -> dict:
    """One SCIM User.

    ``id`` is the Velaris UUID and stays stable across renames, which is what
    the IdP stores as its link to us. ``userName`` is the Velaris username and
    may change; ``externalId`` is the IdP's own key and is echoed back so the
    client can reconcile.
    """
    resource: dict[str, Any] = {
        "schemas": [SCHEMA_USER, SCHEMA_ENTERPRISE_USER],
        "id": str(user.id),
        "userName": user.username,
        "active": bool(user.is_active),
        "displayName": user.display_name or user.username,
        "name": {"formatted": user.display_name or user.username},
        "emails": [{"value": user.email, "primary": True, "type": "work"}],
        "meta": {
            "resourceType": "User",
            "created": _iso(user.created_at),
            "lastModified": _iso(user.updated_at),
            "location": f"{base_url}/Users/{user.id}",
        },
    }
    if user.external_id:
        resource["externalId"] = user.external_id
    if entry is not None:
        if entry.timezone:
            resource["timezone"] = entry.timezone
        if entry.skills:
            resource["urn:velaris:params:scim:schemas:extension:2.0:User"] = {
                "skills": list(entry.skills),
            }
        if entry.manager_user_id:
            resource[SCHEMA_ENTERPRISE_USER] = {
                "manager": {"value": entry.manager_user_id}
            }
    if groups:
        resource["groups"] = [
            {"value": gid, "display": name, "type": "direct"} for gid, name in groups
        ]
    return resource


def group_resource(
    group: AccessGroupModel, members: list[tuple[str, str]], *, base_url: str,
) -> dict:
    return {
        "schemas": [SCHEMA_GROUP],
        "id": str(group.id),
        "displayName": group.name,
        "members": [
            {"value": user_id, "display": username, "type": "User"}
            for user_id, username in members
        ],
        "meta": {
            "resourceType": "Group",
            "created": _iso(group.created_at),
            "lastModified": _iso(group.updated_at),
            "location": f"{base_url}/Groups/{group.id}",
        },
    }


def list_response(resources: list[dict], *, total: int, start_index: int, count: int) -> dict:
    return {
        "schemas": [SCHEMA_LIST],
        "totalResults": total,
        "startIndex": start_index,
        "itemsPerPage": count,
        "Resources": resources,
    }


def service_provider_config(base_url: str) -> dict:
    """What we actually support. Okta and Entra both read this before anything else.

    Every ``supported: false`` here is a deliberate refusal rather than an
    omission — a client that believes we do PATCH-by-filter or bulk operations
    will send them and get errors it cannot explain to its administrator.
    """
    return {
        "schemas": [SCHEMA_SP_CONFIG],
        "documentationUri": f"{base_url}/ServiceProviderConfig",
        "patch": {"supported": True},
        "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
        "filter": {"supported": True, "maxResults": MAX_PAGE_SIZE},
        "changePassword": {"supported": False},
        "sort": {"supported": False},
        "etag": {"supported": False},
        "authenticationSchemes": [{
            "type": "oauthbearertoken",
            "name": "Bearer token",
            "description": "A Velaris SCIM token, minted in Admin > Identity.",
            "primary": True,
        }],
        "meta": {"resourceType": "ServiceProviderConfig",
                 "location": f"{base_url}/ServiceProviderConfig"},
    }


def resource_types(base_url: str) -> list[dict]:
    return [
        {
            "schemas": [SCHEMA_RESOURCE_TYPE],
            "id": "User", "name": "User", "endpoint": "/Users",
            "schema": SCHEMA_USER,
            "schemaExtensions": [
                {"schema": SCHEMA_ENTERPRISE_USER, "required": False}],
            "meta": {"resourceType": "ResourceType",
                     "location": f"{base_url}/ResourceTypes/User"},
        },
        {
            "schemas": [SCHEMA_RESOURCE_TYPE],
            "id": "Group", "name": "Group", "endpoint": "/Groups",
            "schema": SCHEMA_GROUP,
            "meta": {"resourceType": "ResourceType",
                     "location": f"{base_url}/ResourceTypes/Group"},
        },
    ]


# ── inbound payloads ──────────────────────────────────────────────────────

def primary_email(payload: dict) -> str | None:
    """The address to use, preferring the one marked primary.

    An IdP that sends several and marks none is not an error — taking the first
    is what every other SP does, and refusing would block provisioning over a
    formatting detail.
    """
    emails = payload.get("emails") or []
    if isinstance(emails, str):
        return emails.strip().lower() or None
    chosen = None
    for item in emails:
        if not isinstance(item, dict):
            continue
        value = str(item.get("value") or "").strip().lower()
        if not value:
            continue
        if item.get("primary"):
            return value
        chosen = chosen or value
    return chosen


def display_name(payload: dict) -> str | None:
    if payload.get("displayName"):
        return str(payload["displayName"]).strip()
    name = payload.get("name") or {}
    if isinstance(name, dict):
        if name.get("formatted"):
            return str(name["formatted"]).strip()
        parts = [str(name.get("givenName") or "").strip(),
                 str(name.get("familyName") or "").strip()]
        joined = " ".join(p for p in parts if p)
        if joined:
            return joined
    return None


def manager_of(payload: dict) -> str | None:
    ext = payload.get(SCHEMA_ENTERPRISE_USER) or {}
    manager = ext.get("manager") if isinstance(ext, dict) else None
    if isinstance(manager, dict):
        return str(manager.get("value") or "").strip() or None
    if isinstance(manager, str):
        return manager.strip() or None
    return None


def skills_of(payload: dict) -> list[str] | None:
    ext = payload.get("urn:velaris:params:scim:schemas:extension:2.0:User") or {}
    if isinstance(ext, dict) and isinstance(ext.get("skills"), list):
        return [str(s).strip() for s in ext["skills"] if str(s).strip()]
    return None


# ── PATCH ─────────────────────────────────────────────────────────────────

@dataclass
class UserPatch:
    """The subset of a PATCH body that changes something we store."""

    active: bool | None = None
    display_name: str | None = None
    email: str | None = None
    external_id: str | None = None
    manager: str | None = None
    #: The two halves of a name, kept apart because an IdP renames one at a
    #: time. See :func:`compose_display_name`.
    given_name: str | None = None
    family_name: str | None = None
    touched: bool = False


_PATH_ALIASES = {
    "active": "active",
    "displayname": "display_name",
    "name.formatted": "display_name",
    # A rename is the single most common PATCH after the leaver signal, and
    # both products send it as one half of a name rather than a display name:
    # Entra as `path: "name.givenName"`, Okta as a pathless `{"name": {...}}`.
    # Leaving these out made that request a 400 — and Entra quarantines a
    # provisioning job after repeated 4xx, which stops the leaver signal too.
    "name.givenname": "given_name",
    "name.familyname": "family_name",
    "externalid": "external_id",
    "emails[type eq \"work\"].value": "email",
    "emails.value": "email",
    "emails[primary eq true].value": "email",
    f"{SCHEMA_ENTERPRISE_USER}:manager".lower(): "manager",
    f"{SCHEMA_ENTERPRISE_USER}:manager.value".lower(): "manager",
}


def _as_bool(value: Any) -> bool:
    # Entra sends the JSON boolean; some clients send the string. Both mean the
    # same thing to the administrator who clicked "disable".
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes"}


def parse_user_patch(body: dict) -> UserPatch:
    """RFC 7644 §3.5.2 — `add`, `replace`, `remove`, applied to what we store.

    A path we do not recognise is **ignored, not refused**: an IdP sends its
    whole mapped attribute set on every change, and rejecting the operation
    because one of them is `nickName` would break provisioning over a field
    nobody uses. What must never be ignored is `active` — that is the leaver
    signal, and dropping it is the failure this whole module exists to prevent.
    """
    operations = body.get("Operations") or body.get("operations") or []
    if not isinstance(operations, list) or not operations:
        raise ScimError(400, "A PATCH request must carry Operations.", "invalidValue")

    patch = UserPatch()
    for operation in operations:
        if not isinstance(operation, dict):
            raise ScimError(400, "Each operation must be an object.", "invalidValue")
        op = str(operation.get("op") or "").strip().lower()
        if op not in {"add", "replace", "remove"}:
            raise ScimError(400, f"Unsupported op '{op}'.", "invalidSyntax")
        path = str(operation.get("path") or "").strip()
        value = operation.get("value")

        if not path:
            # Pathless replace: the value is an object of attributes.
            if not isinstance(value, dict):
                raise ScimError(400, "A pathless operation needs an object value.",
                                "invalidValue")
            for key, item in value.items():
                _apply(patch, str(key).lower(), item, op)
            continue

        _apply(patch, path.lower(), value, op)

    if not patch.touched:
        raise ScimError(400, "No supported attribute was changed.", "invalidValue")
    return patch


#: Paths an IdP maps by default that Velaris does not store. They are
#: **acknowledged, not applied**: a PATCH carrying only one of these changes
#: nothing here, but answering it `400` would be the same quarantine trap that
#: `name.givenName` was — Entra suspends a provisioning job after repeated 4xx,
#: and a suspended job stops deactivating leavers as well as renaming them.
#: A path that is in neither table is still unrecognised, so a body that touches
#: nothing at all is still refused.
_ACKNOWLEDGED_PATHS = {
    "nickname", "title", "usertype", "preferredlanguage", "locale",
    "profileurl", "phonenumbers", "phonenumbers.value", "addresses",
    f"{SCHEMA_ENTERPRISE_USER}:department".lower(),
    f"{SCHEMA_ENTERPRISE_USER}:division".lower(),
    f"{SCHEMA_ENTERPRISE_USER}:organization".lower(),
    f"{SCHEMA_ENTERPRISE_USER}:employeenumber".lower(),
    f"{SCHEMA_ENTERPRISE_USER}:costcenter".lower(),
    "department", "division", "organization", "employeenumber", "costcenter",
}


def _apply(patch: UserPatch, path: str, value: Any, op: str) -> None:
    # `name` arriving as an object — pathless `{"name": {"givenName": …}}`, or
    # `path: "name"` with a dict value. Flattened to the dotted form so there is
    # one alias table rather than two shapes to keep in step.
    if path == "name" and isinstance(value, dict):
        for key, item in value.items():
            _apply(patch, f"name.{str(key).lower()}", item, op)
        return

    field = _PATH_ALIASES.get(path)
    if field is None:
        if path in _ACKNOWLEDGED_PATHS:
            patch.touched = True
        return
    patch.touched = True
    if op == "remove":
        # Removing `active` means "no longer active" — the one place where the
        # absence of a value is itself the instruction.
        if field == "active":
            patch.active = False
        elif field == "manager":
            patch.manager = ""
        elif field in ("given_name", "family_name"):
            setattr(patch, field, "")
        return
    if field == "active":
        patch.active = _as_bool(value)
    elif field == "email":
        patch.email = (str(value).strip().lower() or None) if value else None
    elif field == "manager":
        if isinstance(value, dict):
            patch.manager = str(value.get("value") or "").strip()
        else:
            patch.manager = str(value or "").strip()
    else:
        setattr(patch, field, str(value).strip() if value else None)


#: Where the two halves of a name are remembered between patches, on the
#: directory row's existing `metadata_json`. No new column: the parts are an
#: artefact of how SCIM sends a rename, not a fact about the person.
NAME_PARTS_KEY = "scim_name_parts"


def name_parts_of(payload: dict) -> dict:
    """The name halves carried by a full user resource (POST / PUT).

    Recorded on create and replace so that a later PATCH of one half — which is
    how a rename actually arrives — has the other half to join it to.
    """
    name = payload.get("name") or {}
    if not isinstance(name, dict):
        return {}
    parts = {
        "given": str(name.get("givenName") or "").strip(),
        "family": str(name.get("familyName") or "").strip(),
    }
    return {k: v for k, v in parts.items() if v}


def compose_display_name(
    patch: UserPatch, stored_parts: dict | None,
    current_display_name: str | None = None,
) -> tuple[str | None, dict]:
    """Resolve a PATCH's name into ``(display_name_or_None, parts_to_store)``.

    **Why the parts are kept rather than the name alone.** An IdP renames one
    half at a time — a surname change is a PATCH carrying `name.familyName` and
    nothing else. Only a whole name is stored, so rebuilding one from half a
    PATCH would mean splitting the existing string on whitespace and replacing a
    token, which is wrong for anyone with a mononym, a double-barrelled surname
    or a middle name. Remembering the halves makes the join exact instead.

    An explicit `displayName` or `name.formatted` still wins: the IdP said what
    it wanted the name to be, and that is not ours to recompute.

    **Half a name never replaces a whole one.** Accounts that predate the parts
    being recorded have no counterpart to join to, and answering a surname
    change with the surname alone would turn "Priya Raman" into "Raman". In
    that case the name is left as it is and only the part is recorded — the
    *next* patch, or the IdP's next PUT, has both halves and lands correctly.
    """
    parts = {k: v for k, v in (stored_parts or {}).items()
             if k in ("given", "family") and isinstance(v, str)}
    if patch.given_name is not None:
        parts["given"] = patch.given_name
    if patch.family_name is not None:
        parts["family"] = patch.family_name
    parts = {k: v for k, v in parts.items() if v}

    if patch.display_name:
        return patch.display_name, parts
    if patch.given_name is None and patch.family_name is None:
        return None, parts

    joined = " ".join(p for p in (parts.get("given"), parts.get("family")) if p)
    if not joined:
        return None, parts
    if len(parts) < 2 and (current_display_name or "").strip():
        return None, parts
    return joined, parts
