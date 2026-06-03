"""Pega PRPC scanner.

Analyzes Pega RAP/ZIP exports and classifies artifacts by
migration compatibility.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import re
from typing import Any

from case_service.scout.base import (
    ArtifactType, CompatibilityLevel, ScannedArtifact, ScanResult,
)


# Pega class → HELIX artifact mapping
PEGA_CLASS_MAP = {
    "Rule-Obj-Class": (ArtifactType.CASE_TYPE, CompatibilityLevel.HIGH, "CaseType", 4.0),
    "Rule-Obj-CaseType": (ArtifactType.CASE_TYPE, CompatibilityLevel.HIGH, "CaseType", 4.0),
    "Rule-HTML-Section": (ArtifactType.FORM, CompatibilityLevel.MEDIUM, "FormDefinition", 3.0),
    "Rule-HTML-Harness": (ArtifactType.USER_INTERFACE, CompatibilityLevel.MEDIUM, "UI Template", 5.0),
    "Rule-Obj-Property": (ArtifactType.DATA_MODEL, CompatibilityLevel.HIGH, "DataModel.field", 0.5),
    "Rule-Declare-Expressions": (ArtifactType.RULE, CompatibilityLevel.HIGH, "Expression Rule", 1.0),
    "Rule-Declare-Constraints": (ArtifactType.RULE, CompatibilityLevel.HIGH, "Constraint Rule", 1.0),
    "Rule-Declare-DecisionTable": (ArtifactType.DECISION_TABLE, CompatibilityLevel.FULL, "Decision Table", 2.0),
    "Rule-Declare-DecisionTree": (ArtifactType.DECISION_TABLE, CompatibilityLevel.HIGH, "Decision Table", 3.0),
    "Rule-Obj-Activity": (ArtifactType.WORKFLOW, CompatibilityLevel.MEDIUM, "BPMN Activity", 4.0),
    "Rule-Obj-Flow": (ArtifactType.WORKFLOW, CompatibilityLevel.HIGH, "BPMN Process", 6.0),
    "Rule-Access-Role-Obj": (ArtifactType.ROLE, CompatibilityLevel.HIGH, "SecurityProfile.role", 1.0),
    "Rule-Access-Role-Name": (ArtifactType.ROLE, CompatibilityLevel.HIGH, "Role", 0.5),
    "Rule-Connect-REST": (ArtifactType.INTEGRATION, CompatibilityLevel.HIGH, "HTTP Task", 2.0),
    "Rule-Connect-SOAP": (ArtifactType.INTEGRATION, CompatibilityLevel.MEDIUM, "HTTP Task", 3.0),
    "Rule-Obj-Report-Definition": (ArtifactType.REPORT, CompatibilityLevel.MEDIUM, "Analytics Query", 2.5),
    "Rule-Obj-SLA": (ArtifactType.SLA, CompatibilityLevel.FULL, "SLAPolicy", 1.0),
    "Rule-Utility-Function": (ArtifactType.RULE, CompatibilityLevel.LOW, "Custom Expression", 6.0),
}


def scan_pega_export(content: str, filename: str = "") -> ScanResult:
    """Scan a Pega export (XML content).

    Accepts either full XML or a text dump with RULEINDEX lines.
    """
    result = ScanResult(source_platform="pega")

    # Try to detect version
    version_match = re.search(r'pegaVersion="([^"]+)"', content)
    if version_match:
        result.source_version = version_match.group(1)

    # Extract rule entries
    # Pattern 1: XML <Rule class="Rule-Obj-Activity" name="Foo"/>
    for match in re.finditer(r'<Rule\s+class="([^"]+)"\s+name="([^"]+)"', content):
        cls, name = match.group(1), match.group(2)
        _add_pega_artifact(result, cls, name)

    # Pattern 2: Text list "Rule-Obj-Activity: FooActivity"
    for line in content.splitlines():
        m = re.match(r'^\s*(Rule-[\w-]+)\s*[:=]\s*([^\s]+)', line)
        if m:
            _add_pega_artifact(result, m.group(1), m.group(2))

    # If no artifacts found, try to create sample based on keywords
    if not result.artifacts and ("pega" in content.lower() or "prpc" in content.lower()):
        # Fallback: crude keyword detection
        if "workflow" in content.lower() or "flow" in content.lower():
            _add_pega_artifact(result, "Rule-Obj-Flow", "DetectedFlow")
        if "case" in content.lower():
            _add_pega_artifact(result, "Rule-Obj-CaseType", "DetectedCaseType")
        if "section" in content.lower() or "form" in content.lower():
            _add_pega_artifact(result, "Rule-HTML-Section", "DetectedSection")

    if not result.artifacts:
        result.warnings.append("No recognizable Pega artifacts found in input")

    return result


def _add_pega_artifact(result: ScanResult, pega_class: str, name: str):
    """Map a Pega rule to a HELIX artifact."""
    if pega_class in PEGA_CLASS_MAP:
        art_type, compat, mapped, hours = PEGA_CLASS_MAP[pega_class]
        result.artifacts.append(ScannedArtifact(
            artifact_type=art_type,
            name=name,
            identifier=f"{pega_class}:{name}",
            compatibility=compat,
            mapped_to=mapped,
            effort_hours=hours,
            metadata={"pega_class": pega_class},
        ))
    else:
        # Unknown Pega class — treat as LOW compatibility
        result.artifacts.append(ScannedArtifact(
            artifact_type=ArtifactType.RULE,
            name=name,
            identifier=f"{pega_class}:{name}",
            compatibility=CompatibilityLevel.LOW,
            mapped_to=None,
            effort_hours=8.0,
            issues=[f"Unrecognized Pega class: {pega_class}"],
            metadata={"pega_class": pega_class},
        ))
