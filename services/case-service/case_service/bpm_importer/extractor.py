"""Pass 1: Extract.

Accepts raw file bytes (JAR/ZIP/BPMN XML) and returns a classified manifest:
    { "files": [ { "name": str, "rule_type": str, "content": str } ] }

Security: SEC-2 (ZIP bomb, path traversal, file count limit).
"""
from __future__ import annotations

import io
import logging
import zipfile
from pathlib import PurePosixPath

from case_service.hxmigrate.security import (
    MAX_ZIP_TOTAL,
    MAX_ZIP_ENTRY,
    MAX_ZIP_FILES,
    MAX_XML_BYTES,
)

logger = logging.getLogger(__name__)

# ── Pega rule-type classifier ─────────────────────────────────────────────────

_PEGA_PREFIXES = {
    "Flow-":          "Flow",
    "Section-":       "Section",
    "DataTransform-": "DataTransform",
    "SLARule-":       "SLARule",
    "AccessGroup-":   "AccessGroup",
    "Correspondence-":"Correspondence",
    "DecisionTable-": "DecisionTable",
    "Assignment-":    "Assignment",
    "WorkType-":      "WorkType",
    "Harness-":       "Harness",
    "RuleSet-":       "RuleSet",
}

def _pega_rule_type(name: str) -> str:
    base = name.split("/")[-1]
    for prefix, rtype in _PEGA_PREFIXES.items():
        if base.startswith(prefix):
            return rtype
    return "Other"


# ── Appian classifier ─────────────────────────────────────────────────────────

_APPIAN_PATTERNS = {
    "processModel":  "ProcessModel",
    "recordType":    "RecordType",
    "interface":     "Interface",
    "group":         "Group",
    "expression":    "ExpressionRule",
    "constant":      "Constant",
    "decision":      "Decision",
    "integration":   "IntegrationObject",
}

def _appian_rule_type(name: str) -> str:
    lower = name.lower()
    for key, rtype in _APPIAN_PATTERNS.items():
        if key in lower:
            return rtype
    return "Other"


# ── ServiceNow classifier ─────────────────────────────────────────────────────

_SN_PATTERNS = {
    "workflow":      "Workflow",
    "catalog":       "Catalog",
    "business_rule": "BusinessRule",
    "script":        "ScriptInclude",
    "ui_action":     "UIAction",
    "transform_map": "TransformMap",
    "sla":           "SLADefinition",
}

def _sn_rule_type(name: str) -> str:
    lower = name.lower()
    for key, rtype in _SN_PATTERNS.items():
        if key in lower:
            return rtype
    return "Other"


# ── SEC-2: path traversal guard ───────────────────────────────────────────────

def _safe_entry_name(name: str) -> str | None:
    """Return normalised filename or None if path traversal attempt detected."""
    if not name or name.startswith("/"):
        return None
    parts = PurePosixPath(name).parts
    if ".." in parts:
        return None
    # Reject absolute Windows paths like C:\...
    if len(parts) > 0 and ":" in parts[0]:
        return None
    return name


# ── Main extractor ────────────────────────────────────────────────────────────

def extract(tool: str, file_bytes: bytes, filename: str) -> dict:
    """Pass 1: extract and classify files from the upload.

    SEC-2: ZIP bomb protection (total + per-entry size limits, file count limit, path traversal).
    Returns:
        { "tool": str, "filename": str, "files": [...], "total": int, "skipped": int }
    """
    tool = tool.lower()
    files: list[dict] = []
    skipped = 0

    # Single BPMN/XML file (not a ZIP)
    if tool in ("camunda", "jbpm", "flowable", "ibm", "oracle", "bizagi") and not _is_zip(file_bytes):
        if len(file_bytes) > MAX_XML_BYTES:
            logger.warning("XML file too large (%d bytes), skipping", len(file_bytes))
            return _manifest(tool, filename, [], 1)
        content = _try_decode(file_bytes)
        if content:
            files.append({"name": filename, "rule_type": "BpmnProcess", "content": content})
        else:
            skipped += 1
        return _manifest(tool, filename, files, skipped)

    # All others: ZIP / JAR (JAR is just a ZIP)
    try:
        buf = io.BytesIO(file_bytes)
        with zipfile.ZipFile(buf, "r") as zf:
            entries = zf.infolist()

            # SEC-2: max file count
            if len(entries) > MAX_ZIP_FILES:
                logger.warning("ZIP has %d entries (limit %d)", len(entries), MAX_ZIP_FILES)
                return _manifest(tool, filename, [], len(entries))

            total_extracted = 0

            for entry in entries:
                if entry.is_dir():
                    continue

                # SEC-2: path traversal
                safe_name = _safe_entry_name(entry.filename)
                if not safe_name:
                    logger.warning("Path traversal attempt in ZIP entry: %s", entry.filename)
                    skipped += 1
                    continue

                # SEC-2: per-entry size
                if entry.file_size > MAX_ZIP_ENTRY:
                    logger.warning("ZIP entry %s too large (%d bytes)", safe_name, entry.file_size)
                    skipped += 1
                    continue

                # SEC-2: total size
                total_extracted += entry.file_size
                if total_extracted > MAX_ZIP_TOTAL:
                    logger.warning("ZIP total extraction limit reached (%d bytes)", MAX_ZIP_TOTAL)
                    break

                name = safe_name
                ext  = name.lower().rsplit(".", 1)[-1] if "." in name else ""

                # Only process text-based rule files; skip nested ZIPs (SEC-2: no recursive)
                if ext not in ("xml", "json", "bpmn", "txt"):
                    skipped += 1
                    continue

                # SEC-2: per-file size after extraction
                if entry.file_size > MAX_XML_BYTES:
                    logger.warning("Entry %s too large after extraction", name)
                    skipped += 1
                    continue

                try:
                    raw = zf.read(entry.filename)
                    content = _try_decode(raw)
                    if not content:
                        skipped += 1
                        continue

                    if tool == "pega":
                        rule_type = _pega_rule_type(name)
                    elif tool == "appian":
                        rule_type = _appian_rule_type(name)
                    elif tool == "servicenow":
                        rule_type = _sn_rule_type(name)
                    elif tool in ("camunda", "jbpm", "flowable", "ibm", "oracle", "bizagi"):
                        rule_type = "BpmnProcess"
                    elif tool == "power_automate":
                        rule_type = "FlowDefinition"
                    elif tool == "salesforce":
                        rule_type = "SalesforceFlow"
                    elif tool == "nintex":
                        rule_type = "NintexWorkflow"
                    else:
                        rule_type = "Other"

                    files.append({"name": name, "rule_type": rule_type, "content": content})
                except Exception as e:
                    logger.warning("Skipped %s: %s", name, type(e).__name__)
                    skipped += 1

    except zipfile.BadZipFile:
        # Try as raw XML anyway (e.g. BPMN named .bpmn or .xml)
        if len(file_bytes) <= MAX_XML_BYTES:
            content = _try_decode(file_bytes)
            if content:
                files.append({
                    "name":      filename,
                    "rule_type": "BpmnProcess" if tool in ("camunda", "jbpm", "flowable", "ibm", "oracle", "bizagi") else "Other",
                    "content":   content,
                })
            else:
                skipped += 1
        else:
            logger.warning("Non-ZIP file too large: %d bytes", len(file_bytes))
            skipped += 1

    return _manifest(tool, filename, files, skipped)


def _is_zip(data: bytes) -> bool:
    return data[:4] == b"PK\x03\x04"


def _try_decode(raw: bytes) -> str | None:
    for enc in ("utf-8", "latin-1", "cp1252"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return None


def _manifest(tool: str, filename: str, files: list[dict], skipped: int) -> dict:
    type_counts: dict[str, int] = {}
    for f in files:
        type_counts[f["rule_type"]] = type_counts.get(f["rule_type"], 0) + 1
    return {
        "tool":       tool,
        "filename":   filename,
        "total":      len(files),
        "skipped":    skipped,
        "type_counts": type_counts,
        "files":      files,
    }
