"""The part of a form a customer portal may see and fill in (Form Builder part 10, 2026-10-28).

The portal is anonymous. A connector lookup, options from a connector, or a
file upload need a signed-in caller or a case, so those fields are left out,
and nothing internal is sent: no connector ids, no input mappings, no case
variable names. What remains is drawn by the same renderer as in Studio and
checked on submit by the same validator (`velaris_shared.forms.validate`).

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from typing import Any

from velaris_shared.forms.embed import allowed as embed_allowed
from velaris_shared.forms.security import restricted
from velaris_shared.forms.validate import field_key_of

#: Field types a customer can fill in on the portal.
PORTAL_TYPES = frozenset({
    "text", "textarea", "email", "phone", "number", "currency", "slider", "rating",
    "date", "datetime", "dropdown", "radio", "multi_select", "checkbox",
    "repeating_group", "location",
    # Small controls (2026-10-29).
    "toggle", "time", "date_range", "percentage", "url", "tags", "country", "currency_code", "iban",
    "consent", "heading", "paragraph", "divider", "callout",
    "embed",          # only when the tenant allows embeds on the portal — see _portal_field
    # Batch M (2026-10-29). The pickers are not here: they search the tenant's
    # people and cases, which an anonymous visitor may not.
    "address", "signature", "rich_text", "button",
})

_FIELD_KEYS = (
    "id", "type", "label", "required", "required_message", "placeholder", "description",
    "options", "validation", "display", "width", "show_when", "conditions",
    "default_value", "hidden", "read_only",
    # Form Builder v2 (2026-10-28): formulas over the form's own fields.
    "calculation", "required_when", "read_only_when", "checks",
    "min_rows", "max_rows", "add_label",
    "text", "tone", "terms",
    "url", "height", "permissions",
    "actions",
    # Country-aware phone (2026-11-04): the default country and allowed list —
    # nothing secret, and the portal needs them to show the country selector.
    "phone",
)
_SECTION_KEYS = ("id", "title", "description", "order", "collapsible", "collapsed")
_SETTINGS_KEYS = ("columns", "submit_label")


def portal_field_allowed(field: dict[str, Any]) -> bool:
    if field.get("type") not in PORTAL_TYPES:
        return False
    # A field limited to some access roles (§5 step 7): a customer holds none.
    if restricted(field):
        return False
    src = field.get("options_source")
    return not (isinstance(src, dict) and src.get("kind") == "connector")


def _portal_field(f: Any, for_browser: bool, embed_origins: list[str] | None = None) -> dict[str, Any] | None:
    """One field as the portal has it, or None when a customer cannot use it.
    A repeating group keeps only the columns a customer can use (no nesting).
    An embed stays only when its site is in `embed_origins` (None = the tenant
    does not allow embeds on the portal: every embed is left out)."""
    if not isinstance(f, dict) or not portal_field_allowed(f):
        return None
    if f.get("type") == "embed" and (embed_origins is None or not embed_allowed(f.get("url"), embed_origins)):
        return None
    if for_browser and f.get("hidden"):
        return None
    out = {k: f[k] for k in _FIELD_KEYS if k in f}
    out["field_key"] = field_key_of(f)
    src = f.get("options_source")
    if isinstance(src, dict) and src.get("kind") == "reference":
        # Options from a reference table (v2): the server checks the choice
        # against it; the browser loads them by field key, so it never needs
        # the table's id.
        keep = ("kind", "value_column", "label_column", "filters", "fill") + (() if for_browser else ("table_id",))
        out["options_source"] = {k: src[k] for k in keep if k in src}
    if f.get("type") == "repeating_group":
        kids = [c for c in (f.get("fields") if isinstance(f.get("fields"), list) else [])
                if isinstance(c, dict) and c.get("type") != "repeating_group"]
        out["fields"] = [o for o in (_portal_field(c, for_browser, embed_origins) for c in kids) if o is not None]
    return out


def portal_definition(definition: dict[str, Any] | None, *, for_browser: bool = False,
                      embed_origins: list[str] | None = None) -> dict[str, Any]:
    """A copy of the form with only what the portal draws and checks.

    `for_browser`: also leave out hidden fields — their value is the form's
    own (`default_value`), applied on submit, and a designer's hidden default
    is not the customer's to read.
    """
    sections = []
    raw = definition.get("sections") if isinstance(definition, dict) else None
    for sec in raw if isinstance(raw, list) else []:
        if not isinstance(sec, dict):
            continue
        fields = []
        for f in sec.get("fields") if isinstance(sec.get("fields"), list) else []:
            out = _portal_field(f, for_browser, embed_origins)
            if out is not None:
                fields.append(out)
        if fields:
            s = {k: sec[k] for k in _SECTION_KEYS if k in sec}
            s["fields"] = fields
            sections.append(s)
    raw_settings = definition.get("settings") if isinstance(definition, dict) else None
    settings = {k: v for k, v in (raw_settings if isinstance(raw_settings, dict) else {}).items() if k in _SETTINGS_KEYS}
    out = {"sections": sections, **({"settings": settings} if settings else {})}
    # Translations (§5 step 7) of what the portal shows, and nothing else.
    from velaris_shared.forms.translate import languages, portal_translations
    if languages(definition):
        keys = {f["field_key"] for s in sections for f in s["fields"]}
        keys |= {f"{f['field_key']}.{field_key_of(c)}" for s in sections for f in s["fields"]
                 for c in f.get("fields") or [] if isinstance(c, dict)}
        out["translations"] = portal_translations(definition, keys, {str(s.get("id")) for s in sections})
    return out


def portal_keys(definition: dict[str, Any] | None) -> set[str]:
    """Every field key of the portal's form — the only keys a submit may store."""
    return {f["field_key"] for s in portal_definition(definition)["sections"] for f in s["fields"]}

