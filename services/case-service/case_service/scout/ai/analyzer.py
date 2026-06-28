"""AI artifact analyzer — uses Ollama to read and understand source code.

Falls back to heuristic analysis when Ollama is unavailable.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from case_service.hxnexus.factory import generate_json as _ai_generate_json, check_ai_available, get_llm_backend

logger = logging.getLogger(__name__)


@dataclass
class ArtifactAnalysis:
    summary: str = ""
    business_logic: str = ""
    complexity: str = "low"
    external_calls: list[str] = field(default_factory=list)
    data_reads: list[str] = field(default_factory=list)
    data_writes: list[str] = field(default_factory=list)
    side_effects: list[str] = field(default_factory=list)
    helix_mapping: dict[str, Any] = field(default_factory=dict)
    generated_code: str = ""
    confidence: float = 0.0
    source: str = "heuristic"


ANALYSIS_PROMPT_TEMPLATE = """You are a senior migration engineer analyzing legacy BPM platform code.

Read the following __ARTIFACT_TYPE__ code from __SOURCE_PLATFORM__ and produce structured JSON analysis.

Output ONLY valid JSON with this structure:
{
  "summary": "<1-2 sentence plain-English description>",
  "business_logic": "<business rules, or 'None detected'>",
  "complexity": "<low | medium | high | extreme>",
  "external_calls": [<list of external services called>],
  "data_reads": [<list of fields READ from>],
  "data_writes": [<list of fields WRITTEN to>],
  "side_effects": [<side effects like 'sends email', 'creates file'>],
  "helix_mapping": {
    "artifact_type": "<case_type | user_task | decision_table | rule | integration | service_task>",
    "name": "<suggested snake_case name>",
    "notes": "<migration notes>"
  },
  "confidence": <0.0 to 1.0>
}

Source: __IDENTIFIER__
Code:
__CODE__"""


async def analyze_artifact(
    code: str,
    *,
    artifact_type: str = "activity",
    source_platform: str = "pega",
    identifier: str = "unknown",
    use_fallback: bool = True,
    # Legacy params kept for call-site compatibility — ignored, backend from settings
    model: str = "",
    ollama_url: str = "",
) -> ArtifactAnalysis:
    """Analyze an artifact's source code — AI if available, heuristic fallback."""
    prompt = (ANALYSIS_PROMPT_TEMPLATE
              .replace("__ARTIFACT_TYPE__", artifact_type)
              .replace("__SOURCE_PLATFORM__", source_platform)
              .replace("__IDENTIFIER__", identifier)
              .replace("__CODE__", code[:8000]))
    result = await _ai_generate_json(prompt=prompt)

    if result and isinstance(result, dict) and "summary" in result:
            return ArtifactAnalysis(
                summary=str(result.get("summary", "")),
                business_logic=str(result.get("business_logic", "")),
                complexity=str(result.get("complexity", "low")),
                external_calls=_ensure_list(result.get("external_calls")),
                data_reads=_ensure_list(result.get("data_reads")),
                data_writes=_ensure_list(result.get("data_writes")),
                side_effects=_ensure_list(result.get("side_effects")),
                helix_mapping=result.get("helix_mapping") if isinstance(result.get("helix_mapping"), dict) else {},
                confidence=float(result.get("confidence", 0.5)),
                source="llm",
            )

    if use_fallback:
        return _heuristic_analyze(code, artifact_type, source_platform)

    return ArtifactAnalysis(summary="Analysis unavailable", complexity="unknown", source="failed")


async def generate_helix_code(
    analysis: ArtifactAnalysis,
    original_code: str,
    *,
    artifact_type: str = "activity",
    source_platform: str = "pega",
    # Legacy params kept for call-site compatibility — ignored
    model: str = "",
    ollama_url: str = "",
) -> str:
    """Generate HELIX-equivalent Python code using the unified AI backend."""
    if not await check_ai_available():
        logger.info("AI backend unavailable — using heuristic code generation")
        return _heuristic_generate_code(analysis, artifact_type)

    prompt = f"""Port the following legacy {source_platform} {artifact_type} to Velaris Python code.

Target: Python async function following Velaris conventions.
Output ONLY Python code, no explanations.

Legacy code:
{original_code[:4000]}

Analysis: {analysis.summary}"""

    try:
        llm = get_llm_backend()
        code = await llm.complete(prompt, temperature=0.2, max_tokens=2048)
        code = re.sub(r"^```(?:python|py)?\n", "", code.strip())
        code = re.sub(r"\n```$", "", code)
        if not code or len(code) < 20:
            return _heuristic_generate_code(analysis, artifact_type)
        return code
    except Exception as e:
        logger.warning("Code generation failed: %s — using heuristic", e)
        return _heuristic_generate_code(analysis, artifact_type)


def _ensure_list(v) -> list[str]:
    if isinstance(v, list):
        return [str(x) for x in v]
    if isinstance(v, str):
        return [v] if v else []
    return []


# ─── Heuristic fallback ────────────────────────────────────────

INTEGRATION_PATTERNS = [
    (r"Rule-Connect-REST[:\s]+(\w+)", "REST: {0}"),
    (r"Rule-Connect-SOAP[:\s]+(\w+)", "SOAP: {0}"),
    (r"ConnectREST\s+\w+\s*=\s*tools\.getConnectRest\(['\"]([^'\"]+)['\"]", "REST: {0}"),
    (r"getConnectRest\(['\"]([^'\"]+)['\"]", "REST: {0}"),
    (r"\.get\(['\"](/[^'\"]+)['\"]", "API GET: {0}"),
    (r"\.post\(['\"](/[^'\"]+)['\"]", "API POST: {0}"),
    (r"connector\.get\(['\"]([^'\"]+)['\"]", "API GET: {0}"),
    (r"connector\.post\(['\"]([^'\"]+)['\"]", "API POST: {0}"),
    (r"fetch\(['\"]([^'\"]+)['\"]", "Fetch: {0}"),
    (r"a!httpPost|a!httpGet", "HTTP (Appian)"),
]

SIDE_EFFECT_PATTERNS = [
    (r"sendMail|sendEmail", "sends email"),
    (r"createFile|writeFile|fileWrite", "creates file"),
    (r"audit|logAudit|auditTrail", "writes audit log"),
    (r"notify|pushNotification", "sends notification"),
    (r"schedul[eE]", "schedules task"),
]

DATA_READ_PATTERNS = [
    r"\.getDouble\(['\"]([^'\"]+)['\"]",
    r"\.getString\(['\"]([^'\"]+)['\"]",
    r"tools\.getProperty\(['\"]([^'\"]+)['\"]",
    r"getProperty\(['\"]([^'\"]+)['\"]",
    r"pxGet\w*\(['\"]([^'\"]+)['\"]",
    r"getVariable\(['\"]([^'\"]+)['\"]",
]

DATA_WRITE_PATTERNS = [
    r"tools\.putProperty\(['\"]([^'\"]+)['\"]",
    r"putProperty\(['\"]([^'\"]+)['\"]",
    r"setProperty\(['\"]([^'\"]+)['\"]",
    r"pxSet\w*\(['\"]([^'\"]+)['\"]",
    r"setVariable\(['\"]([^'\"]+)['\"]",
]


def _heuristic_analyze(code: str, artifact_type: str, source_platform: str) -> ArtifactAnalysis:
    analysis = ArtifactAnalysis(source="heuristic", confidence=0.5)
    line_count = code.count("\n")
    analysis.complexity = _classify_complexity(code, line_count)

    for pattern, label in INTEGRATION_PATTERNS:
        for m in re.finditer(pattern, code):
            call = label.format(m.group(1) if m.groups() else "")
            if call not in analysis.external_calls:
                analysis.external_calls.append(call)

    for pattern, effect in SIDE_EFFECT_PATTERNS:
        if re.search(pattern, code, re.IGNORECASE):
            if effect not in analysis.side_effects:
                analysis.side_effects.append(effect)

    for pattern in DATA_READ_PATTERNS:
        for m in re.finditer(pattern, code):
            f = m.group(1)
            if f not in analysis.data_reads and len(f) < 100:
                analysis.data_reads.append(f)

    for pattern in DATA_WRITE_PATTERNS:
        for m in re.finditer(pattern, code):
            f = m.group(1)
            if f not in analysis.data_writes and len(f) < 100:
                analysis.data_writes.append(f)

    analysis.external_calls = analysis.external_calls[:20]
    analysis.data_reads = analysis.data_reads[:30]
    analysis.data_writes = analysis.data_writes[:30]

    summary_parts = []
    if analysis.external_calls:
        summary_parts.append(f"calls {len(analysis.external_calls)} external service(s)")
    if analysis.data_reads or analysis.data_writes:
        summary_parts.append(f"reads {len(analysis.data_reads)}, writes {len(analysis.data_writes)} field(s)")
    if analysis.side_effects:
        summary_parts.append(f"side effects: {', '.join(analysis.side_effects[:3])}")

    if summary_parts:
        analysis.summary = f"{artifact_type.title()} that " + "; ".join(summary_parts) + "."
    else:
        analysis.summary = f"{artifact_type.title()} ({line_count} lines of {source_platform} code)."

    if_count = len(re.findall(r"\bif\b", code))
    for_count = len(re.findall(r"\bfor\b|\bwhile\b|\bforEach\b", code))

    logic_parts = []
    if if_count > 0:
        logic_parts.append(f"{if_count} conditional branch(es)")
    if for_count > 0:
        logic_parts.append(f"{for_count} loop(s)")
    if analysis.external_calls:
        logic_parts.append("integrates with external systems")

    analysis.business_logic = "; ".join(logic_parts) if logic_parts else "Linear processing"

    if analysis.external_calls:
        mapping_type = "integration"
    elif if_count > 5 or "decision" in code.lower():
        mapping_type = "decision_table"
    elif analysis.side_effects:
        mapping_type = "service_task"
    else:
        mapping_type = "user_task"

    analysis.helix_mapping = {
        "artifact_type": mapping_type,
        "name": re.sub(r"[^a-z0-9_]", "_", artifact_type.lower()) or "artifact",
        "notes": "Heuristic mapping — review before deploying.",
    }

    return analysis


def _classify_complexity(code: str, line_count: int) -> str:
    if line_count > 500:
        return "extreme"
    if line_count > 150:
        return "high"
    if line_count > 50:
        return "medium"
    return "low"


def _heuristic_generate_code(analysis: ArtifactAnalysis, artifact_type: str) -> str:
    name = (analysis.helix_mapping.get("name") or "ported_artifact").lower()
    name = re.sub(r"[^a-z0-9_]", "_", name)

    stub = f'''async def {name}(session, case_data: dict) -> dict:
    """Ported from legacy {artifact_type}.

    {analysis.summary}

    Business logic: {analysis.business_logic}
    """
    result = dict(case_data)
'''

    for field in analysis.data_reads[:5]:
        safe = re.sub(r"[^a-z0-9_]", "_", field.lower())
        stub += f"    _{safe} = case_data.get('{field}')\n"

    if analysis.external_calls:
        stub += "\n    # TODO: Port external calls:\n"
        for call in analysis.external_calls[:5]:
            stub += f"    # - {call}\n"

    for f in analysis.data_writes[:5]:
        stub += f"    result['{f}'] = None  # TODO: compute value\n"

    if analysis.side_effects:
        stub += "\n    # TODO: Implement side effects:\n"
        for effect in analysis.side_effects[:5]:
            stub += f"    # - {effect}\n"

    stub += "\n    return result\n"
    return stub
