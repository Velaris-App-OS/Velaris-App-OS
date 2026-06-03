"""P44 — Pass 5: Report.

Produces the import report: what was converted automatically, what needs
manual review, and what has no Helix equivalent.
"""
from __future__ import annotations


def build_report(tool: str, filename: str, pass1: dict, pass2: dict, pass3: dict, pass4: dict) -> dict:
    extracted_total = pass1.get("total", 0)
    extracted_by_type = pass1.get("type_counts", {})

    case_types     = pass4.get("case_types", [])
    forms          = pass4.get("forms", [])
    sla_rules      = pass3.get("sla_rules", [])
    access_groups  = pass3.get("access_groups", [])
    unmapped       = pass3.get("unmapped", [])

    auto_converted = len(case_types) + len(forms) + len(sla_rules) + len(access_groups)
    needs_review   = [u for u in unmapped if u.get("needs_review")]
    no_equivalent  = [u for u in unmapped if not u.get("helix_suggestion")]

    # Confidence summary across all generated objects
    all_objects = case_types + forms + sla_rules + access_groups
    exact  = sum(1 for o in all_objects if o.get("confidence") == "exact")
    close  = sum(1 for o in all_objects if o.get("confidence") == "close")
    partial = sum(1 for o in all_objects if o.get("confidence") == "partial")
    manual = sum(1 for o in all_objects if o.get("confidence") == "manual")

    conversion_pct = round(
        100 * auto_converted / max(extracted_total, 1), 1
    )

    return {
        "tool":     tool,
        "filename": filename,
        "summary": {
            "extracted_total":    extracted_total,
            "extracted_by_type":  extracted_by_type,
            "auto_converted":     auto_converted,
            "needs_review":       len(needs_review),
            "no_equivalent":      len(no_equivalent),
            "conversion_pct":     conversion_pct,
        },
        "confidence": {
            "exact": exact, "close": close, "partial": partial, "manual": manual,
        },
        "generated": {
            "case_types":    len(case_types),
            "forms":         len(forms),
            "sla_rules":     len(sla_rules),
            "access_groups": len(access_groups),
        },
        "items_converted": [
            {"type": "case_type", "name": o["name"], "confidence": o.get("confidence", "?")}
            for o in case_types
        ] + [
            {"type": "form", "name": o["name"], "confidence": o.get("confidence", "?")}
            for o in forms
        ] + [
            {"type": "sla_rule", "name": o["name"], "confidence": "exact"}
            for o in sla_rules
        ] + [
            {"type": "access_group", "name": o["name"], "confidence": o.get("confidence", "close")}
            for o in access_groups
        ],
        "needs_review": needs_review,
        "no_equivalent": no_equivalent,
    }
