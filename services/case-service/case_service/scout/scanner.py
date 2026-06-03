"""Scout scanner orchestrator — picks the right scanner for the input.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
from typing import Any

from case_service.scout.base import ScanResult
from case_service.scout.pega_scanner import scan_pega_export
from case_service.scout.camunda_scanner import scan_camunda_bpmn
from case_service.scout.appian_scanner import scan_appian_export

logger = logging.getLogger(__name__)


def detect_source_platform(content: str, filename: str = "") -> str:
    """Best-guess detection of source platform from content/filename."""
    content_lower = content.lower()
    filename_lower = filename.lower()

    # Filename hints
    if filename_lower.endswith(".bpmn") or filename_lower.endswith(".bpmn20.xml"):
        return "camunda"
    if "camunda" in filename_lower or "zeebe" in filename_lower:
        return "camunda"
    if "pega" in filename_lower or filename_lower.endswith(".rap"):
        return "pega"
    if "appian" in filename_lower:
        return "appian"

    # Content hints
    if "bpmn" in content_lower and ("camunda" in content_lower or "zeebe" in content_lower):
        return "camunda"
    if "<bpmn:" in content_lower or "<bpmn2:" in content_lower:
        return "camunda"
    if "pega" in content_lower or "prpc" in content_lower or "rule-obj" in content_lower.lower():
        return "pega"
    if "appian" in content_lower or "a!" in content_lower:
        return "appian"

    # Default to Camunda if looks like BPMN
    if "<process" in content_lower and "<task" in content_lower:
        return "camunda"

    return "unknown"


def scan(content: str, source_platform: str = "", filename: str = "") -> ScanResult:
    """Run the appropriate scanner for the source platform."""
    if not source_platform:
        source_platform = detect_source_platform(content, filename)

    logger.info("Scanning %s content (%d bytes)", source_platform, len(content))

    if source_platform == "pega":
        return scan_pega_export(content, filename)
    elif source_platform == "camunda":
        return scan_camunda_bpmn(content, filename)
    elif source_platform == "appian":
        return scan_appian_export(content, filename)
    else:
        result = ScanResult(source_platform=source_platform)
        result.errors.append(f"Unknown source platform: {source_platform}")
        return result
