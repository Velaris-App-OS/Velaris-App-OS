"""HxAnalytics — CSV and JSON export."""
from __future__ import annotations

import csv
import io
import json


def to_csv(series: list[dict]) -> str:
    """Convert a series list to CSV string."""
    if not series:
        return "label,value\n"
    buf = io.StringIO()
    keys = list(series[0].keys())
    writer = csv.DictWriter(buf, fieldnames=keys)
    writer.writeheader()
    writer.writerows(series)
    return buf.getvalue()


def to_json(data: dict) -> str:
    return json.dumps(data, indent=2, default=str)


def odata_response(rows: list[dict], entity_set: str = "Cases") -> dict:
    """Minimal OData v4 JSON response — compatible with PowerBI / Tableau."""
    return {
        "@odata.context": f"$metadata#{entity_set}",
        "@odata.count":   len(rows),
        "value":          rows,
    }
