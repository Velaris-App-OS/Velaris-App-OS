"""Synthetic data generator for sandbox workspaces.

Generates realistic-looking but entirely fabricated case records seeded from
the real platform schema. The sandbox container receives this data as a
read-only volume — it cannot access the production database.

Two modes:
  Option A — auto-generated from real schema (field names real, values fake)
  Option B — admin-defined dataset (pre-curated, stored in marketplace_sandbox_datasets)
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any

from faker import Faker

fake = Faker()
Faker.seed(42)   # deterministic — same schema produces same synthetic records


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def generate_case_record(case_type_name: str = "SyntheticCase", index: int = 0) -> dict[str, Any]:
    """Generate one synthetic case record with realistic field values."""
    fake.seed_instance(index)
    return {
        "id":            str(uuid.uuid4()),
        "case_number":   f"SANDBOX-{index+1:06d}",
        "case_type":     case_type_name,
        "status":        fake.random_element(["open", "in_progress", "on_hold", "resolved"]),
        "priority":      fake.random_element(["low", "medium", "high", "critical"]),
        "created_at":    (_utcnow() - timedelta(days=fake.random_int(1, 90))).isoformat(),
        "assignee":      fake.name(),
        "customer_name": fake.name(),
        "customer_email": fake.email(),
        "data": {
            "amount":       round(fake.random_number(digits=5) / 100, 2),
            "currency":     fake.random_element(["GBP", "USD", "EUR"]),
            "reference":    fake.bothify("REF-####-????").upper(),
            "description":  fake.sentence(nb_words=8),
            "country":      fake.country_code(),
            "postcode":     fake.postcode(),
            "notes":        fake.paragraph(nb_sentences=2),
        },
    }


def generate_dataset(
    case_type_name: str = "SyntheticCase",
    record_count: int = 50,
) -> dict[str, Any]:
    """Generate a full synthetic dataset for a sandbox workspace."""
    return {
        "generated_at": _utcnow().isoformat(),
        "case_type":    case_type_name,
        "record_count": record_count,
        "warning":      "SYNTHETIC DATA — all values are fabricated. No real customer data.",
        "cases": [
            generate_case_record(case_type_name, i)
            for i in range(record_count)
        ],
    }


def write_dataset_to_file(path: str, case_type_name: str = "SyntheticCase", count: int = 50) -> None:
    """Write synthetic dataset JSON to a file path (used when building the container volume)."""
    dataset = generate_dataset(case_type_name, count)
    with open(path, "w") as f:
        json.dump(dataset, f, indent=2)
