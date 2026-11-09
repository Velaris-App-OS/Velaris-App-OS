"""HxDraft forms speak the Form Builder's shape (2026-10-28).

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from case_service.nlp import form_builder as fb
from velaris_shared.forms import validate

BUILDER = {"settings": {"mode": "steps"}, "sections": [
    {"id": "s1", "title": "About", "order": 0, "fields": [
        {"id": "a", "field_key": "name", "label": "Name", "type": "text", "required": True,
         "width": "half", "validation": {"max_length": 40}},
        {"id": "b", "field_key": "kind", "label": "Kind", "type": "dropdown", "required": False,
         "options": [{"label": "Car", "value": "car"}]},
        {"id": "c", "field_key": "orders", "label": "Orders", "type": "table_display"},
    ]}]}


def test_a_draft_becomes_sections_the_validator_reads():
    sections = fb.draft_to_sections([
        {"id": "visitor_name", "label": "Visitor", "field_type": "text", "required": True},
        {"id": "kind", "label": "Kind", "field_type": "select", "options": ["Car", "Home"]},
        {"id": "ok", "label": "Agree", "field_type": "boolean"},
    ])
    f = sections[0]["fields"]
    assert [x["type"] for x in f] == ["text", "dropdown", "checkbox"]
    assert f[1]["options"] == [{"label": "Car", "value": "car"}, {"label": "Home", "value": "home"}]
    _, errors = validate({"sections": sections}, {"kind": "boat"})
    assert {e.field_key for e in errors} == {"visitor_name", "kind"}


def test_modify_sees_a_builder_forms_fields():
    assert [d["id"] for d in fb.builder_to_draft_fields(BUILDER)] == ["name", "kind"]


def test_modify_keeps_settings_of_kept_fields_and_leaves_display_fields_alone():
    out = fb.merge_draft_into_sections(BUILDER, [
        {"id": "name", "label": "Full name", "field_type": "text", "required": True},
        {"id": "email", "label": "Email", "field_type": "email", "required": True},
    ])
    fields = out["sections"][0]["fields"]
    assert [f["field_key"] for f in fields] == ["name", "orders", "email"]   # kind removed, email added
    assert fields[0]["label"] == "Full name" and fields[0]["width"] == "half"
    assert fields[0]["validation"] == {"max_length": 40}
    assert out["settings"] == {"mode": "steps"} and "fields" not in out
