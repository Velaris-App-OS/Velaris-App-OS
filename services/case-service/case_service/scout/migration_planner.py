"""Migration planner — produces actionable migration plan from scan.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from typing import Any

from case_service.scout.base import (
    CompatibilityLevel, ScanResult, ArtifactType,
)


def build_migration_plan(scan: ScanResult) -> dict[str, Any]:
    """Generate a phased migration plan from scan results."""
    artifacts_by_compat: dict[str, list] = {
        "full": [], "high": [], "medium": [], "low": [], "incompatible": [],
    }
    for a in scan.artifacts:
        artifacts_by_compat[a.compatibility.value].append({
            "name": a.name,
            "type": a.artifact_type.value,
            "mapped_to": a.mapped_to,
            "effort_hours": a.effort_hours,
            "issues": a.issues,
        })

    total_hours = sum(a.effort_hours for a in scan.artifacts)

    # Phased plan
    phases = [
        {
            "phase": 1,
            "name": "Quick Wins — Full Compatibility",
            "description": "Artifacts that migrate 1:1 with minimal rework",
            "artifacts": artifacts_by_compat["full"],
            "duration_weeks": max(1, int(sum(a["effort_hours"] for a in artifacts_by_compat["full"]) / 40)),
        },
        {
            "phase": 2,
            "name": "High-Compatibility Migration",
            "description": "Small adjustments needed during port",
            "artifacts": artifacts_by_compat["high"],
            "duration_weeks": max(1, int(sum(a["effort_hours"] for a in artifacts_by_compat["high"]) / 40)),
        },
        {
            "phase": 3,
            "name": "Redesign Phase",
            "description": "Artifacts requiring significant rework",
            "artifacts": artifacts_by_compat["medium"],
            "duration_weeks": max(1, int(sum(a["effort_hours"] for a in artifacts_by_compat["medium"]) / 40)),
        },
        {
            "phase": 4,
            "name": "Complex Migration",
            "description": "Major rewrites or platform-specific features",
            "artifacts": artifacts_by_compat["low"] + artifacts_by_compat["incompatible"],
            "duration_weeks": max(1, int(sum(a["effort_hours"] for a in artifacts_by_compat["low"] + artifacts_by_compat["incompatible"]) / 40)),
        },
    ]

    # Recommendations
    recommendations = []
    if artifacts_by_compat["incompatible"]:
        recommendations.append({
            "severity": "critical",
            "title": f"{len(artifacts_by_compat['incompatible'])} incompatible artifacts",
            "description": "These features have no HELIX equivalent and require a different approach.",
        })
    if artifacts_by_compat["low"]:
        recommendations.append({
            "severity": "high",
            "title": f"{len(artifacts_by_compat['low'])} low-compatibility artifacts",
            "description": "Plan for significant manual rewrite.",
        })
    if scan.compatibility_score > 0.7:
        recommendations.append({
            "severity": "info",
            "title": "Good migration candidate",
            "description": f"Compatibility score {scan.compatibility_score:.0%} — most artifacts migrate cleanly.",
        })

    return {
        "summary": {
            "source_platform": scan.source_platform,
            "source_version": scan.source_version,
            "total_artifacts": scan.total_artifacts,
            "compatibility_score": scan.compatibility_score,
            "total_effort_hours": round(total_hours, 1),
            "total_effort_weeks": scan.effort_weeks,
            "counts_by_type": scan.counts_by_type(),
            "counts_by_compatibility": scan.counts_by_compatibility(),
        },
        "phases": phases,
        "recommendations": recommendations,
        "warnings": scan.warnings,
        "errors": scan.errors,
    }
