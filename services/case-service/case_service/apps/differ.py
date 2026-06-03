"""P43 — Bundle differ.

Computes a human-readable diff between two app_package bundles.
Returns structured diff per section so the Studio can render it clearly.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def _item_id(item: dict) -> str:
    return item.get("id") or item.get("name") or json.dumps(item, sort_keys=True)[:40]


def _item_checksum(item: dict) -> str:
    return hashlib.sha256(
        json.dumps(item, sort_keys=True, default=str).encode()
    ).hexdigest()[:12]


def _diff_section(
    old_items: list[dict],
    new_items: list[dict],
    label_key: str = "name",
) -> dict:
    """Diff two lists of dicts by id. Returns added/removed/changed/unchanged counts."""
    old_by_id = {_item_id(i): i for i in old_items}
    new_by_id = {_item_id(i): i for i in new_items}

    added   = [i for k, i in new_by_id.items() if k not in old_by_id]
    removed = [i for k, i in old_by_id.items() if k not in new_by_id]
    changed = []
    unchanged = []

    for k, new_item in new_by_id.items():
        if k in old_by_id:
            old_item = old_by_id[k]
            if _item_checksum(old_item) != _item_checksum(new_item):
                changed.append({
                    "id":    k,
                    "label": new_item.get(label_key, k),
                    "old_checksum": _item_checksum(old_item),
                    "new_checksum": _item_checksum(new_item),
                })
            else:
                unchanged.append(k)

    return {
        "added":     [i.get(label_key, _item_id(i)) for i in added],
        "removed":   [i.get(label_key, _item_id(i)) for i in removed],
        "changed":   changed,
        "unchanged": len(unchanged),
        "total_old": len(old_items),
        "total_new": len(new_items),
    }


SECTIONS = [
    ("case_types",         "name"),
    ("forms",              "name"),
    ("rules",              "name"),
    ("portals",            "name"),
    ("access_groups",      "name"),
    ("work_queues",        "name"),
    ("escalation_trees",   "name"),
    ("business_calendars", "name"),
]


def diff_bundles(bundle_a: dict, bundle_b: dict) -> dict:
    """Return a structured diff between two bundles.

    bundle_a = older version, bundle_b = newer version.
    """
    sections: dict[str, dict] = {}
    has_changes = False

    for section, label_key in SECTIONS:
        old_items = bundle_a.get(section, [])
        new_items = bundle_b.get(section, [])
        d = _diff_section(old_items, new_items, label_key)
        sections[section] = d
        if d["added"] or d["removed"] or d["changed"]:
            has_changes = True

    # Summary
    total_added   = sum(len(s["added"])   for s in sections.values())
    total_removed = sum(len(s["removed"]) for s in sections.values())
    total_changed = sum(len(s["changed"]) for s in sections.values())

    meta_a = bundle_a.get("meta", {})
    meta_b = bundle_b.get("meta", {})

    return {
        "has_changes":    has_changes,
        "summary": {
            "added":   total_added,
            "removed": total_removed,
            "changed": total_changed,
        },
        "packaged_at_a":  meta_a.get("packaged_at"),
        "packaged_at_b":  meta_b.get("packaged_at"),
        "sections":       sections,
    }
