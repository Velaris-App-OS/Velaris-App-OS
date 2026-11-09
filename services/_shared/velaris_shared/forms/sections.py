"""Reusable form sections (Form Builder §5 step 6, 2026-10-29).

A form section may link a reusable section:

    {"id": "s3", "title": "Home address", "section_ref": {"id": "<uuid>", "version": 4}, "fields": [...]}

The form always holds a copy of the linked fields, so every reader of a form
(submit, portal, pickers, lookups, the renderer) works unchanged. The copy is
made by the server: when the form is saved, and when the reusable section is
saved (then for every form that links it). A designer cannot edit linked fields
inside a form — whatever the browser sends for them is replaced by the source.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import copy
import json
import uuid
from typing import Any

from velaris_shared.forms.validate import field_key_of

MAX_FIELDS = 100
MAX_BYTES = 200_000


class SectionError(ValueError):
    """A form or section that cannot be saved; the message says why."""


def ref_of(section: Any) -> uuid.UUID | None:
    ref = section.get("section_ref") if isinstance(section, dict) else None
    try:
        return uuid.UUID(str(ref["id"])) if isinstance(ref, dict) and ref.get("id") else None
    except (ValueError, TypeError):
        raise SectionError("A linked section names an id that is not valid.") from None


def linked_ids(definition: Any) -> set[uuid.UUID]:
    out: set[uuid.UUID] = set()
    for sec in (definition or {}).get("sections") or [] if isinstance(definition, dict) else []:
        rid = ref_of(sec)
        if rid:
            out.add(rid)
    return out


def check_section_body(body: Any) -> dict[str, Any]:
    """A reusable section's own content: `{title, description, fields}`, bounded.
    Linking a section inside a section, and repeating-group nesting beyond what
    forms allow, are not offered."""
    if not isinstance(body, dict):
        raise SectionError("A section is an object with fields.")
    fields = body.get("fields")
    if not isinstance(fields, list) or not all(isinstance(f, dict) for f in fields):
        raise SectionError("A section needs a list of fields.")
    if len(fields) > MAX_FIELDS:
        raise SectionError(f"A section has at most {MAX_FIELDS} fields.")
    if len(json.dumps(body, default=str)) > MAX_BYTES:
        raise SectionError("This section is too large.")
    keys = [field_key_of(f) for f in fields]
    dup = sorted({k for k in keys if keys.count(k) > 1})
    if dup:
        raise SectionError(f"Field key used twice in the section: {', '.join(dup)}.")
    return {"title": str(body.get("title") or "")[:200], "description": str(body.get("description") or "")[:2000],
            "fields": copy.deepcopy(fields)}


def materialize(definition: Any, sources: dict[uuid.UUID, Any]) -> dict[str, Any]:
    """The form with every linked section's fields copied from its source
    (`sources`: id -> FormSectionModel-like with `version` and `section_json`).
    A link to a section not in `sources` is refused — it does not exist or
    belongs to another tenant."""
    if not isinstance(definition, dict):
        return definition
    out = copy.deepcopy(definition)
    for sec in out.get("sections") or []:
        rid = ref_of(sec)
        if rid is None:
            continue
        src = sources.get(rid)
        if src is None:
            raise SectionError("A linked section does not exist or is not available to this form.")
        body = src.section_json or {}
        sec["fields"] = copy.deepcopy(body.get("fields") or [])
        sec["section_ref"] = {"id": str(rid), "version": int(src.version)}
        if not sec.get("title"):
            sec["title"] = body.get("title") or src.name
    duplicate_keys(out)
    return out


def duplicate_keys(definition: Any) -> None:
    """Refuse a form in which two fields share a key — the case would store
    one answer for two questions (most often: the same section linked twice)."""
    keys: list[str] = []
    for sec in (definition or {}).get("sections") or []:
        for f in sec.get("fields") or [] if isinstance(sec, dict) else []:
            if isinstance(f, dict):
                keys.append(field_key_of(f))
    dup = sorted({k for k in keys if keys.count(k) > 1})
    if dup:
        raise SectionError(f"Field key used twice in the form: {', '.join(dup)} — a field of the form and a "
                           "linked section, or one section linked twice.")
