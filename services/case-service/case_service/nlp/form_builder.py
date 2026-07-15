"""HxDraft P1 — natural language → a DRAFT form definition.

Template-driven and schema-strict: whatever the model returns is coerced into the
platform's form shape ({name, version, definition_json:{fields:[…]}}) with a
whitelisted field-type vocabulary and a hard field cap. Forms are benign relative
to rules (no actions, no expressions), so — unlike rules — an AI outage degrades
to a small sensible template instead of an error, mirroring the NLP Builder's
heuristic posture.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import re
from typing import Any

from case_service.hxnexus.factory import generate_json as _ai_generate_json

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
