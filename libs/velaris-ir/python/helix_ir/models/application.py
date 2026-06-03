"""HELIX IR: Application model.

An Application bundles case types, processes, forms, rules, data
models, and security profiles into a single deployable unit.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ApplicationDefinition:
    """Top-level container grouping all artifacts of a logical application."""

    id: str
    name: str
    version: str
    description: str = ""
    # References (by ID) to constituent artifacts
    case_type_ids: list[str] = field(default_factory=list)
    process_ids: list[str] = field(default_factory=list)
    data_model_ids: list[str] = field(default_factory=list)
    form_ids: list[str] = field(default_factory=list)
    rule_ids: list[str] = field(default_factory=list)
    security_profile_id: str | None = None
    # Configuration
    default_locale: str = "en"
    supported_locales: list[str] = field(default_factory=lambda: ["en"])
    settings: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
