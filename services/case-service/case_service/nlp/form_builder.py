"""HxDraft P1 — natural language → a DRAFT form definition.

Template-driven and schema-strict: whatever the model returns is coerced into the
platform's form shape ({name, version, definition_json:{fields:[…]}}) with a
whitelisted field-type vocabulary and a hard field cap. Forms are benign relative
to rules (no actions, no expressions), so — unlike rules — an AI outage degrades
to a small sensible template instead of an error, mirroring the NLP Builder's
heuristic posture.

Copyright (c) 2024-2025 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import re
from typing import Any

from velaris_shared.llm.factory import generate_json as _ai_generate_json

logger = logging.getLogger(__name__)


def _ident(s: str) -> str:
    """snake_case identifier that PRESERVES underscores (unlike the builder's _slug)."""
    s = re.sub(r"[^0-9a-zA-Z]+", "_", str(s or "")).strip("_").lower()
    return s[:64] or "field"

MAX_PROMPT_CHARS = 4000
MAX_FIELDS = 40
MAX_OPTIONS = 30

FIELD_TYPES = {"text", "textarea", "number", "date", "datetime", "email", "phone",
               "select", "multiselect", "boolean", "checkbox", "file", "currency"}

FORM_PROMPT = """You are an expert in business form design.
Convert the user's description into ONE form as JSON. Output ONLY valid JSON,
no markdown fences, no commentary.

JSON structure (use EXACTLY these keys):
{
  "name": "<short form name>",
  "description": "<one sentence>",
  "fields": [
    {"id": "<snake_case>", "label": "<display label>",
     "field_type": "text|textarea|number|date|datetime|email|phone|select|multiselect|boolean|checkbox|file|currency",
     "required": true|false,
     "options": ["only for select/multiselect"]}
  ]
}
Ask for what the process genuinely needs — no filler fields."""


class FormDraftError(Exception):
    """Draft request failed in a way the card should state honestly."""


def validate_form_draft(draft: dict[str, Any]) -> list[str]:
    """Every reason this draft must not become a card. Empty list = renderable."""
    errors: list[str] = []
    if not isinstance(draft, dict):
        return ["Draft is not an object"]
    if not str(draft.get("name") or "").strip():
        errors.append("Form needs a name")
    fields = (draft.get("definition_json") or {}).get("fields")
    if not isinstance(fields, list) or not fields:
        return errors + ["Form needs at least one field"]
    if len(fields) > MAX_FIELDS:
        errors.append(f"Too many fields ({len(fields)} > {MAX_FIELDS})")
    seen: set[str] = set()
    for i, f in enumerate(fields):
        if not isinstance(f, dict) or not str(f.get("id") or "").strip():
            errors.append(f"Field {i + 1}: needs an id")
            continue
        if f["id"] in seen:
            errors.append(f"Field {i + 1}: duplicate id {f['id']!r}")
        seen.add(f["id"])
        ft = f.get("field_type")
        if ft not in FIELD_TYPES:
            errors.append(f"Field {i + 1}: unknown field_type {ft!r}")
        if ft in ("select", "multiselect") and not f.get("options"):
            errors.append(f"Field {i + 1}: {ft} needs options")
    return errors


def normalize_form_draft(raw: dict[str, Any], *, prompt: str = "") -> dict[str, Any]:
    """Coerce into the platform form shape; unknown keys never survive."""
    name = str(raw.get("name") or "Drafted form").strip()[:255]
    fields = []
    for f in (raw.get("fields") or (raw.get("definition_json") or {}).get("fields") or []):
        if not isinstance(f, dict):
            continue
        field: dict[str, Any] = {
            "id": _ident(str(f.get("id") or f.get("label") or "field")),
            "label": str(f.get("label") or f.get("id") or "Field").strip()[:255],
            "field_type": str(f.get("field_type") or "text"),
            "required": bool(f.get("required", False)),
        }
        opts = f.get("options")
        if isinstance(opts, list) and opts:
            field["options"] = [str(o)[:120] for o in opts[:MAX_OPTIONS]]
        fields.append(field)
    fields = fields[:MAX_FIELDS + 1]        # +1 so the validator can still say "too many"

    provenance = f'Drafted by HxNexus — "{prompt.strip()[:300]}"' if prompt else "Drafted by HxNexus"
    description = str(raw.get("description") or "").strip()[:500]
    return {
        "name": name,
        "version": "1.0.0",
        "definition_json": {
            "fields": fields,
            "description": f"{description} · {provenance}" if description else provenance,
        },
    }


def _template_form(description: str) -> dict[str, Any]:
    """AI-down fallback: a minimal honest starting point, clearly generic."""
    return {
        "name": (description.strip().split("\n")[0][:60] or "Drafted form").title(),
        "description": "Template fallback (AI unavailable) — edit before applying.",
        "fields": [
            {"id": "summary", "label": "Summary", "field_type": "text", "required": True},
            {"id": "details", "label": "Details", "field_type": "textarea", "required": False},
            {"id": "attachment", "label": "Attachment", "field_type": "file", "required": False},
        ],
    }


async def generate_form_draft(description: str) -> dict[str, Any]:
    """NL → normalized form draft + validation errors: {"draft", "errors", "source"}."""
    description = (description or "").strip()
    if not description:
        raise FormDraftError("Describe the form you want drafted")
    if len(description) > MAX_PROMPT_CHARS:
        raise FormDraftError(f"Description too long (max {MAX_PROMPT_CHARS} characters)")

    raw = await _ai_generate_json(prompt=description, system=FORM_PROMPT)
    source = "llm"
    if not raw or not isinstance(raw, dict):
        raw = _template_form(description)
        source = "template"

    draft = normalize_form_draft(raw, prompt=description)
    return {"draft": draft, "errors": validate_form_draft(draft), "source": source}


# ── Draft vocabulary ↔ Form Builder sections (2026-10-28) ─────────────────────
#
# Drafts speak a small flat vocabulary ({id, label, field_type, required,
# options[str]}). Until 2026-10-28 an applied draft was stored in that shape,
# `definition_json.fields`, which the Form Builder, the renderer and the
# server's form check never read: the form opened empty and accepted anything,
# and a "modify" draft saw no fields at all. These two translate.

_TO_BUILDER = {
    "text": "text", "textarea": "textarea", "number": "number", "date": "date",
    "datetime": "datetime", "email": "email", "phone": "phone", "select": "dropdown",
    "multiselect": "multi_select", "boolean": "checkbox", "checkbox": "checkbox",
    "file": "file_upload", "currency": "currency",
}
_TO_DRAFT = {
    "text": "text", "textarea": "textarea", "number": "number", "date": "date",
    "datetime": "datetime", "email": "email", "phone": "phone", "dropdown": "select",
    "radio": "select", "multi_select": "multiselect", "checkbox": "boolean",
    "file_upload": "file", "currency": "currency", "slider": "number", "rating": "number",
}


def _option(o: Any) -> dict[str, str]:
    if isinstance(o, dict):
        return {"label": str(o.get("label") or o.get("value") or ""), "value": str(o.get("value") or o.get("label") or "")}
    return {"label": str(o), "value": _ident(str(o))}


def draft_field_to_builder(f: dict[str, Any]) -> dict[str, Any]:
    key = _ident(str(f.get("id") or f.get("label") or "field"))
    out: dict[str, Any] = {
        "id": key, "field_key": key, "label": str(f.get("label") or key),
        "type": _TO_BUILDER.get(str(f.get("field_type") or "text"), "text"),
        "required": bool(f.get("required", False)),
    }
    if f.get("options"):
        out["options"] = [_option(o) for o in f["options"]]
    return out


def draft_to_sections(fields: list[dict[str, Any]], title: str = "Details") -> list[dict[str, Any]]:
    return [{"id": "section-1", "title": title, "order": 0,
             "fields": [draft_field_to_builder(f) for f in fields if isinstance(f, dict)]}]


def builder_to_draft_fields(definition: dict[str, Any]) -> list[dict[str, Any]]:
    """What a modify draft is shown: every input field, in the draft vocabulary."""
    out = []
    for sec in (definition or {}).get("sections", []) or []:
        for f in sec.get("fields", []) or []:
            ft = _TO_DRAFT.get(f.get("type"))
            if not ft:
                continue                      # display / lookup fields are kept as they are
            d: dict[str, Any] = {"id": f.get("field_key") or f.get("key") or f.get("id"),
                                 "label": f.get("label"), "field_type": ft,
                                 "required": bool(f.get("required"))}
            if f.get("options"):
                d["options"] = [o.get("label") if isinstance(o, dict) else o for o in f["options"]]
            out.append(d)
    return out + list((definition or {}).get("fields", []) or [])


def merge_draft_into_sections(definition: dict[str, Any], draft_fields: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply a modify draft to a sectioned form. A field the draft keeps (same
    id) keeps every Form Builder setting and its place, and takes the draft's
    label / type / required / options; a new field joins the last section; an
    input field the draft leaves out is removed. Display and lookup fields, and
    everything else in the definition, are untouched."""
    wanted = {draft_field_to_builder(f)["field_key"]: draft_field_to_builder(f) for f in draft_fields if isinstance(f, dict)}
    seen: set[str] = set()
    sections = []
    for sec in (definition or {}).get("sections", []) or []:
        fields = []
        for f in sec.get("fields", []) or []:
            key = f.get("field_key") or f.get("key") or f.get("id")
            if f.get("type") not in _TO_DRAFT:
                fields.append(f)
            elif key in wanted:
                w = wanted[key]
                merged = {**f, "label": w["label"], "required": w["required"]}
                if _TO_BUILDER.get(_TO_DRAFT[f["type"]]) != w["type"]:
                    merged["type"] = w["type"]     # the draft changed the kind of field
                if "options" in w:
                    merged["options"] = w["options"]
                fields.append(merged)
                seen.add(key)
        sections.append({**sec, "fields": fields})
    new = [w for k, w in wanted.items() if k not in seen]
    if new:
        if sections:
            sections[-1] = {**sections[-1], "fields": [*sections[-1]["fields"], *new]}
        else:
            sections = [{"id": "section-1", "title": "Details", "order": 0, "fields": new}]
    out = {k: v for k, v in (definition or {}).items() if k != "fields"}
    out["sections"] = sections
    return out

