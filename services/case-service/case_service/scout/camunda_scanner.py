"""Camunda scanner — parses BPMN 2.0 XML files.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import re
import defusedxml.ElementTree as ET

from case_service.scout.base import (
    ArtifactType, CompatibilityLevel, ScannedArtifact, ScanResult,
)


# BPMN namespaces
BPMN_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"
CAMUNDA_NS = "http://camunda.org/schema/1.0/bpmn"

# BPMN element → HELIX compatibility
BPMN_ELEMENT_MAP = {
    "process": (ArtifactType.PROCESS, CompatibilityLevel.FULL, "BPMN Process", 2.0),
    "subProcess": (ArtifactType.PROCESS, CompatibilityLevel.FULL, "BPMN Subprocess", 1.5),
    "userTask": (ArtifactType.WORKFLOW, CompatibilityLevel.FULL, "User Task", 0.5),
    "serviceTask": (ArtifactType.WORKFLOW, CompatibilityLevel.HIGH, "Service Task", 0.5),
    "scriptTask": (ArtifactType.WORKFLOW, CompatibilityLevel.MEDIUM, "Script Task (needs port)", 2.0),
    "businessRuleTask": (ArtifactType.DECISION_TABLE, CompatibilityLevel.FULL, "Decision Table", 0.5),
    "sendTask": (ArtifactType.INTEGRATION, CompatibilityLevel.FULL, "Send Task", 0.5),
    "receiveTask": (ArtifactType.WORKFLOW, CompatibilityLevel.HIGH, "Receive Task", 0.5),
    "manualTask": (ArtifactType.WORKFLOW, CompatibilityLevel.FULL, "Manual Task", 0.5),
    "exclusiveGateway": (ArtifactType.WORKFLOW, CompatibilityLevel.FULL, "Exclusive Gateway", 0.25),
    "parallelGateway": (ArtifactType.WORKFLOW, CompatibilityLevel.FULL, "Parallel Gateway", 0.25),
    "inclusiveGateway": (ArtifactType.WORKFLOW, CompatibilityLevel.FULL, "Inclusive Gateway", 0.25),
    "eventBasedGateway": (ArtifactType.WORKFLOW, CompatibilityLevel.HIGH, "Event Gateway", 0.5),
    "startEvent": (ArtifactType.WORKFLOW, CompatibilityLevel.FULL, "Start Event", 0.25),
    "endEvent": (ArtifactType.WORKFLOW, CompatibilityLevel.FULL, "End Event", 0.25),
    "intermediateCatchEvent": (ArtifactType.WORKFLOW, CompatibilityLevel.HIGH, "Intermediate Event", 0.5),
    "intermediateThrowEvent": (ArtifactType.WORKFLOW, CompatibilityLevel.HIGH, "Intermediate Event", 0.5),
    "boundaryEvent": (ArtifactType.WORKFLOW, CompatibilityLevel.HIGH, "Boundary Event", 0.5),
}


def scan_camunda_bpmn(content: str, filename: str = "") -> ScanResult:
    """Scan a Camunda BPMN XML file."""
    result = ScanResult(source_platform="camunda")

    # Detect version
    if 'camunda.org/schema/1.0' in content:
        result.source_version = "7.x"
    elif 'zeebe' in content.lower() or 'camunda.io' in content:
        result.source_version = "8.x (Zeebe)"

    try:
        # Strip namespaces for easier parsing
        content_clean = re.sub(r'xmlns[:\w]*="[^"]+"', '', content)
        content_clean = re.sub(r'<(/?)[\w]+:', r'<\1', content_clean)
        root = ET.fromstring(content_clean)

        # Find all known elements
        for element_name, (art_type, compat, mapped, hours) in BPMN_ELEMENT_MAP.items():
            for el in root.iter(element_name):
                name = el.get("name") or el.get("id") or element_name
                ident = el.get("id", name)

                issues = []
                # Check for Camunda-specific extensions
                has_listeners = any(child.tag.endswith("executionListener") or
                                   child.tag.endswith("taskListener")
                                   for child in el.iter())
                if has_listeners:
                    issues.append("Uses Camunda listeners — manual port required")
                    compat_final = CompatibilityLevel.MEDIUM if compat == CompatibilityLevel.FULL else compat
                    hours += 2.0
                else:
                    compat_final = compat

                # Check for external tasks
                if el.get("type") == "external":
                    issues.append("External task — maps to HTTP task plugin")

                result.artifacts.append(ScannedArtifact(
                    artifact_type=art_type,
                    name=name,
                    identifier=ident,
                    compatibility=compat_final,
                    mapped_to=mapped,
                    effort_hours=hours,
                    issues=issues,
                    metadata={"bpmn_element": element_name},
                ))

    except ET.ParseError as e:
        result.errors.append(f"BPMN parse error: {e}")

    # Look for DMN references
    for match in re.finditer(r'decisionRef="([^"]+)"', content):
        result.artifacts.append(ScannedArtifact(
            artifact_type=ArtifactType.DECISION_TABLE,
            name=match.group(1),
            identifier=f"dmn:{match.group(1)}",
            compatibility=CompatibilityLevel.FULL,
            mapped_to="Decision Table",
            effort_hours=1.0,
        ))

    if not result.artifacts:
        result.warnings.append("No BPMN elements recognized")

    return result
