"""Form translations — labels, help, options per language (Form Builder v2 §5 step 7, 2026-10-29).

A form may carry `translations`, by language code (`de`, `fr`, `pt-BR`):

    {"de": {"form":     {"submit_label": "Senden"},
            "sections": {"<section id>": {"title": …, "description": …}},
            "fields":   {"<field key>": {"label": …, "description": …, "placeholder": …,
                                         "required_message": …, "text": …, "add_label": …,
                                         "options": {"<value>": "<label>"}},
                         "<group key>.<column key>": {…}}}}

Only text is translated; values, keys and rules never change, so a case filled
in German stores what one filled in English stores. A text with no translation
stays in the form's own language. No `translations` = the form as before.

`translate` is applied by Studio (`translate.ts`, the twin) to draw the form,
and by the server so its error messages use the caller's language
(`Accept-Language`).

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import copy
import re
from typing import Any

from velaris_shared.forms.validate import field_key_of

FIELD_TEXTS = ("label", "description", "placeholder", "required_message", "text", "add_label")
SECTION_TEXTS = ("title", "description")
FORM_TEXTS = ("submit_label",)
_LANG = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?$")


def languages(definition: Any) -> list[str]:
    tr = definition.get("translations") if isinstance(definition, dict) else None
    return sorted(k for k, v in tr.items() if isinstance(v, dict) and _LANG.match(str(k))) if isinstance(tr, dict) else []


def pick_language(definition: Any, accept_language: str | None) -> str | None:
    """The first language the caller accepts that the form has (exact, then by
    its main part: `de-AT` takes `de`); None = the form's own language."""
    have = languages(definition)
    if not have or not accept_language:
        return None
    lower = {h.lower(): h for h in have}
    for part in accept_language.split(",")[:20]:
        tag = part.split(";")[0].strip().lower()
        if not tag or tag == "*":
            continue
        if tag in lower:
            return lower[tag]
        main = tag.split("-")[0]
        if main in lower:
            return lower[main]
    return None


def _text(src: dict[str, Any], key: str) -> str | None:
    v = src.get(key)
    return v if isinstance(v, str) and v.strip() else None


def _field(f: Any, texts: dict[str, Any], prefix: str = "") -> Any:
    if not isinstance(f, dict):
        return f
    key = prefix + field_key_of(f)
    t = texts.get(key) if isinstance(texts.get(key), dict) else {}
    out = dict(f)
    for k in FIELD_TEXTS:
        if _text(t, k):
            out[k] = t[k]
    opts = t.get("options") if isinstance(t.get("options"), dict) else {}
    if opts and isinstance(f.get("options"), list):
        out["options"] = [
            ({**o, "label": opts[str(o.get("value"))]} if isinstance(o, dict) and _text(opts, str(o.get("value")))
             else {"label": opts[o], "value": o} if isinstance(o, str) and _text(opts, o) else o)
            for o in f["options"]]
    if isinstance(f.get("fields"), list):
        out["fields"] = [_field(c, texts, key + ".") for c in f["fields"]]
    return out


def translate(definition: Any, lang: str | None) -> Any:
    """The form in `lang`; unchanged (the same object) when it has none."""
    if not lang or not isinstance(definition, dict):
        return definition
    tr = (definition.get("translations") or {}).get(lang) if isinstance(definition.get("translations"), dict) else None
    if not isinstance(tr, dict):
        return definition
    fields = tr.get("fields") if isinstance(tr.get("fields"), dict) else {}
    secs = tr.get("sections") if isinstance(tr.get("sections"), dict) else {}
    out = copy.copy(definition)
    sections = []
    for sec in definition.get("sections") if isinstance(definition.get("sections"), list) else []:
        if isinstance(sec, dict):
            st = secs.get(str(sec.get("id"))) if isinstance(secs.get(str(sec.get("id"))), dict) else {}
            sec = {**sec, **{k: st[k] for k in SECTION_TEXTS if _text(st, k)}}
            sec["fields"] = [_field(f, fields) for f in (sec.get("fields") or [])]
        sections.append(sec)
    out["sections"] = sections
    form = tr.get("form") if isinstance(tr.get("form"), dict) else {}
    if any(_text(form, k) for k in FORM_TEXTS):
        settings = definition.get("settings") if isinstance(definition.get("settings"), dict) else {}
        out["settings"] = {**settings, **{k: form[k] for k in FORM_TEXTS if _text(form, k)}}
    return out


def for_caller(definition: Any, accept_language: str | None) -> Any:
    return translate(definition, pick_language(definition, accept_language))


def portal_translations(definition: Any, keep_fields: set[str], keep_sections: set[str]) -> dict[str, Any]:
    """The translations a portal's form carries: only of what the portal shows."""
    out: dict[str, Any] = {}
    for lang in languages(definition):
        tr = definition["translations"][lang]
        fields = tr.get("fields") if isinstance(tr.get("fields"), dict) else {}
        secs = tr.get("sections") if isinstance(tr.get("sections"), dict) else {}
        out[lang] = {
            "fields": {k: v for k, v in fields.items() if k in keep_fields and isinstance(v, dict)},
            "sections": {k: v for k, v in secs.items() if k in keep_sections and isinstance(v, dict)},
            **({"form": tr["form"]} if isinstance(tr.get("form"), dict) else {}),
        }
    return out
