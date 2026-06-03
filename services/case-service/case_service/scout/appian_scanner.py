"""Appian scanner — analyzes Appian app exports.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import re

from case_service.scout.base import (
    ArtifactType, CompatibilityLevel, ScannedArtifact, ScanResult,
)


APPIAN_OBJECT_MAP = {
    "process_model": (ArtifactType.PROCESS, CompatibilityLevel.HIGH, "BPMN Process", 6.0),
    "record_type": (ArtifactType.CASE_TYPE, CompatibilityLevel.HIGH, "CaseType", 4.0),
    "interface": (ArtifactType.USER_INTERFACE, CompatibilityLevel.MEDIUM, "Form/UI", 5.0),
    "site": (ArtifactType.USER_INTERFACE, CompatibilityLevel.LOW, "Studio Module", 12.0),
    "constant": (ArtifactType.DATA_MODEL, CompatibilityLevel.FULL, "Constant", 0.25),
    "expression_rule": (ArtifactType.RULE, CompatibilityLevel.MEDIUM, "Expression Rule", 2.0),
    "decision": (ArtifactType.DECISION_TABLE, CompatibilityLevel.FULL, "Decision Table", 1.5),
    "data_type": (ArtifactType.DATA_MODEL, CompatibilityLevel.HIGH, "Data Model", 2.0),
    "group": (ArtifactType.ROLE, CompatibilityLevel.HIGH, "Role/Group", 0.5),
    "web_api": (ArtifactType.INTEGRATION, CompatibilityLevel.HIGH, "REST API", 1.5),
    "integration": (ArtifactType.INTEGRATION, CompatibilityLevel.MEDIUM, "HTTP Task", 3.0),
    "report": (ArtifactType.REPORT, CompatibilityLevel.MEDIUM, "Analytics Query", 2.5),
}


def scan_appian_export(content: str, filename: str = "") -> ScanResult:
    """Scan an Appian export file."""
    result = ScanResult(source_platform="appian")

    # Appian versions in ZIP comments / manifest
    version_match = re.search(r'appianVersion["\s:=]+([0-9.]+)', content)
    if version_match:
        result.source_version = version_match.group(1)

    # Object types usually appear as "objectType":"process_model" or similar
    for obj_type, (art_type, compat, mapped, hours) in APPIAN_OBJECT_MAP.items():
        # Look for references in XML or JSON-like content
        pattern1 = rf'"objectType"\s*:\s*"{obj_type}"[^}}]*?"name"\s*:\s*"([^"]+)"'
        pattern2 = rf'<{obj_type}[^>]*name="([^"]+)"'

        for pattern in (pattern1, pattern2):
            for match in re.finditer(pattern, content):
                name = match.group(1)
                result.artifacts.append(ScannedArtifact(
                    artifact_type=art_type,
                    name=name,
                    identifier=f"{obj_type}:{name}",
                    compatibility=compat,
                    mapped_to=mapped,
                    effort_hours=hours,
                    metadata={"appian_type": obj_type},
                ))

    # Detect SAIL expressions (Appian-specific, low compatibility)
    sail_count = len(re.findall(r'a!\w+\(', content))
    if sail_count > 0:
        result.artifacts.append(ScannedArtifact(
            artifact_type=ArtifactType.RULE,
            name=f"SAIL Expressions ({sail_count} found)",
            identifier="sail:bulk",
            compatibility=CompatibilityLevel.LOW,
            mapped_to="Custom Expression Rewrite",
            effort_hours=sail_count * 0.5,
            issues=["SAIL expressions require manual rewrite"],
            metadata={"sail_count": sail_count},
        ))

    if not result.artifacts and "appian" not in content.lower():
        result.warnings.append("Does not appear to be an Appian export")

    return result
