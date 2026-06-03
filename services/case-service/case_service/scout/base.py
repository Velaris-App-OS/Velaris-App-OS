"""Base scanner interface.

Each source platform (Pega, Appian, Camunda) implements a
Scanner that produces a normalized ScanResult.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ArtifactType(str, Enum):
    PROCESS = "process"
    CASE_TYPE = "case_type"
    FORM = "form"
    DATA_MODEL = "data_model"
    RULE = "rule"
    INTEGRATION = "integration"
    WORKFLOW = "workflow"
    DECISION_TABLE = "decision_table"
    REPORT = "report"
    USER_INTERFACE = "user_interface"
    ROLE = "role"
    SLA = "sla"


class CompatibilityLevel(str, Enum):
    FULL = "full"                # Direct 1:1 migration
    HIGH = "high"                # Small manual tweaks
    MEDIUM = "medium"            # Significant rework
    LOW = "low"                  # Major redesign needed
    INCOMPATIBLE = "incompatible"


@dataclass
class ScannedArtifact:
    """One artifact discovered in the source platform."""
    artifact_type: ArtifactType
    name: str
    identifier: str                          # Native ID in source system
    compatibility: CompatibilityLevel
    mapped_to: str | None = None              # HELIX equivalent
    effort_hours: float = 0.0
    issues: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScanResult:
    """Complete scan output."""
    source_platform: str
    source_version: str = ""
    artifacts: list[ScannedArtifact] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def total_artifacts(self) -> int:
        return len(self.artifacts)

    @property
    def compatibility_score(self) -> float:
        """Weighted score 0.0-1.0."""
        if not self.artifacts:
            return 0.0
        weights = {
            CompatibilityLevel.FULL: 1.0,
            CompatibilityLevel.HIGH: 0.85,
            CompatibilityLevel.MEDIUM: 0.5,
            CompatibilityLevel.LOW: 0.2,
            CompatibilityLevel.INCOMPATIBLE: 0.0,
        }
        score = sum(weights.get(a.compatibility, 0) for a in self.artifacts)
        return round(score / len(self.artifacts), 3)

    @property
    def effort_weeks(self) -> int:
        """Total effort in weeks (assumes 40-hour weeks)."""
        total_hours = sum(a.effort_hours for a in self.artifacts)
        return max(1, int(total_hours / 40))

    def counts_by_type(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for a in self.artifacts:
            counts[a.artifact_type.value] = counts.get(a.artifact_type.value, 0) + 1
        return counts

    def counts_by_compatibility(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for a in self.artifacts:
            counts[a.compatibility.value] = counts.get(a.compatibility.value, 0) + 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_platform": self.source_platform,
            "source_version": self.source_version,
            "total_artifacts": self.total_artifacts,
            "compatibility_score": self.compatibility_score,
            "effort_weeks": self.effort_weeks,
            "counts_by_type": self.counts_by_type(),
            "counts_by_compatibility": self.counts_by_compatibility(),
            "artifacts": [
                {
                    "type": a.artifact_type.value,
                    "name": a.name,
                    "identifier": a.identifier,
                    "compatibility": a.compatibility.value,
                    "mapped_to": a.mapped_to,
                    "effort_hours": a.effort_hours,
                    "issues": a.issues,
                    "metadata": a.metadata,
                }
                for a in self.artifacts
            ],
            "warnings": self.warnings,
            "errors": self.errors,
        }
